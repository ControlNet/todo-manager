"""Delegations: explicit work handed to an agent, executed as ONE card on the
``todo-manager`` Hermes Kanban board.

Identity and duplicates
  * ``delegation_id`` is assigned once; the Kanban idempotency key is derived from it and
    never from timestamps or retries. The card body carries a marker line with the id.
  * Submission (query -> create -> record link) runs under the ``kanban-submit`` file lock
    used by every entry point. Before creating, existing cards are looked up by key/marker
    INCLUDING archived ones; a failed lookup never leads to a create.
  * The state is set to ``reconcile`` before the create call, so a crash or lost response
    is repaired by the next lookup instead of a second create. Multiple matches quarantine
    the delegation and are reported; nothing is dispatched twice "to clean up later".

Follow-up instructions (``delegation_messages``) are stored first and then delivered:
  supplement -> comment (a running worker receives it as a steer)
  answer     -> only against the block it answers (``target_block_seq``); comment + unblock
                (or respecify when the Kanban loop breaker parked it in triage)
  revision   -> comment + reopen-review
  pause      -> comment + block (our marker); resume -> unblock of our own pause only
  cancel     -> archive (terminates a running worker); accept -> complete the review card
Every comment carries ``[tm:<message id>]`` so a retry after a lost response does not
comment twice. Card completion only ends the delegation; the Task's own completion is
decided from its done criteria and later evidence.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import clock, effects, ids, store
from .config import Config
from .db import dumps, loads, row_dict, tx
from .kanban import Kanban, KanbanError
from .locks import file_lock, lock_path

ACTIVE = ("pending_submit", "queued", "running", "blocked", "review", "paused", "reconcile")
EXECUTING = ("queued", "running")
PAUSE_MARK = "[todo-manager pause]"
KNOWN_ACTIONS = {
    "read", "analyze", "draft", "edit_local_files", "run_tests", "git_commit_local", "push_branch", "open_pr",
    "merge_pr", "comment_github", "close_issue", "send_email", "edit_notion_doc", "post_public",
}
EXTERNAL_ACTIONS = {"push_branch", "open_pr", "merge_pr", "comment_github", "close_issue", "send_email",
                    "edit_notion_doc", "post_public"}

WORKER_PROTOCOL = """\
## Todo Manager delegation protocol
- Do ONLY this delegation. Do not widen the scope, do not handle unrelated matters, do not create or decompose kanban cards.
- Allowed actions: {allowed}. Anything else is NOT allowed. External actions (sending email, public comments, push, opening/merging PRs, closing issues, publishing) are allowed only when listed here, or when a later comment by `todo-manager` says `APPROVED <action> for delivery <delivery id>`; an approval covers exactly that delivered version - any substantive change voids it.
- Never change the Notion task or its status yourself; the Todo Manager owns that.
- Need information or a decision from the user? Call kanban_block(kind="needs_input", reason="<one concrete question in Simplified Chinese>"). Do not guess.
- Deliverable ready? Call kanban_request_review(summary="<2-3 sentence summary in Simplified Chinese>", metadata={{"deliverable_markdown": "<the full deliverable in Markdown>", "external_actions_done": [...], "notes": "..."}}, artifacts=[<absolute paths of files to keep>]). Do NOT call kanban_complete: the user reviews in Notion.
- Comments by `todo-manager` are the user's supplements, answers, revision requests or approvals: follow them. A comment starting with {pause} means stop immediately and take no further action.
- Scratch workspaces are deleted when the card ends: keep needed files in artifacts.
"""


class DelegationError(ValueError):
    pass


def get(conn: sqlite3.Connection, delegation_id: str) -> dict[str, Any] | None:
    row = row_dict(conn.execute("SELECT * FROM delegations WHERE id=?", (delegation_id,)).fetchone())
    if row:
        row["allowed_actions"] = loads(row["allowed_actions"], [])
    return row


def for_task(conn: sqlite3.Connection, task_id: str, active_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM delegations WHERE task_id=?"
    if active_only:
        sql += f" AND status IN ({','.join(repr(s) for s in ACTIVE)})"
    rows = conn.execute(sql + " ORDER BY created_at", (task_id,)).fetchall()
    return [{**dict(r), "allowed_actions": loads(r["allowed_actions"], [])} for r in rows]


def card_body(d: dict[str, Any], task: dict[str, Any], notion_url: str | None, extra: list[str]) -> str:
    allowed = ", ".join(d["allowed_actions"]) or "read, analyze"
    parts = [
        f"todo-manager-delegation: {d['id']}",
        f"todo-manager-task: {task['id']}" + (f"  notion: {notion_url}" if notion_url else ""),
        f"requirement-version: {d['requirement_version']}",
        "",
        f"## Goal\n{d['goal']}",
    ]
    for title, key in (("Context", "context"), ("Constraints", "constraints"), ("Deliverable", "deliverable"),
                       ("Done when", "done_criteria")):
        if d.get(key):
            parts.append(f"## {title}\n{d[key]}")
    parts.append(f"## Task background\n{task['title']}" + (f"\n{task['summary']}" if task.get("summary") else ""))
    if extra:
        parts.append("## Later supplements\n" + "\n".join(f"- {e}" for e in extra))
    parts.append(WORKER_PROTOCOL.format(allowed=allowed, pause=PAUSE_MARK))
    return "\n\n".join(parts)


def create(conn: sqlite3.Connection, cfg: Config, *, task_id: str, title: str, goal: str, context: str | None = None,
           constraints: str | None = None, deliverable: str | None = None, done_criteria: str | None = None,
           allowed_actions: list[str] | None = None, feedback_id: str | None = None, actor: str = "human") -> str:
    task = store.get_task(conn, task_id)
    if task is None:
        raise DelegationError(f"task {task_id} not found")
    if task["status"] in store.CLOSED_STATUSES:
        raise DelegationError("task is closed; reopen it or create a follow-up task first")
    if not goal.strip():
        raise DelegationError("goal is required")
    allowed = allowed_actions or ["read", "analyze", "draft"]
    unknown = {a.split(":", 1)[0] for a in allowed} - KNOWN_ACTIONS
    if unknown:
        raise DelegationError(f"unknown actions {sorted(unknown)}; known: {sorted(KNOWN_ACTIONS)}")
    if feedback_id:
        existing = conn.execute("SELECT id FROM delegations WHERE from_feedback_id=?", (feedback_id,)).fetchone()
        if existing:
            return existing["id"]
    did = ids.new_id("delegation")
    now = clock.iso()
    with tx(conn):
        conn.execute(
            "INSERT INTO delegations(id, task_id, title, goal, context, constraints, deliverable, done_criteria,"
            " allowed_actions, assignee, requirement_version, status, board, idem_key, from_feedback_id,"
            " base_task_version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,1,'pending_submit',?,?,?,?,?,?)",
            (did, task_id, title.strip(), goal.strip(), context, constraints, deliverable, done_criteria,
             dumps(allowed), cfg.get("kanban.assignee") or None, cfg.get("kanban.board", "todo-manager"),
             ids.delegation_idempotency_key(did), feedback_id, task["version"], now, now))
        conn.execute(
            "INSERT INTO delegation_messages(id, delegation_id, kind, feedback_id, body, requirement_version, status,"
            " created_at, sent_at) VALUES (?,?,?,?,?,1,'sent',?,?)",
            (ids.new_id("event"), did, "requirement", feedback_id, goal.strip(), now, now))
        _task_state(conn, did, "in_progress", "agent", "委派给 Agent：" + title.strip(), actor=actor,
                    cause=feedback_id or did)
        # Human edits made AFTER this point win over delegation-driven status changes.
        conn.execute("UPDATE delegations SET base_task_version=(SELECT version FROM tasks WHERE id=?) WHERE id=?",
                     (task_id, did))
    return did


def _task_state(conn: sqlite3.Connection, delegation_id: str, status: str, next_actor: str, note: str | None,
                actor: str = "program", cause: str | None = None) -> None:
    d = conn.execute("SELECT task_id, base_task_version FROM delegations WHERE id=?", (delegation_id,)).fetchone()
    task = store.get_task(conn, d["task_id"])
    if task is None or task["status"] in store.CLOSED_STATUSES:
        return
    meta = loads(task["field_meta"], {})
    human_v = max((int(meta.get(f, {}).get("v", 0)) for f in ("status", "next_actor")
                   if meta.get(f, {}).get("by") == "human"), default=0)
    override = actor == "human" or human_v <= d["base_task_version"]
    changes: dict[str, Any] = {"status": status, "next_actor": next_actor}
    if note:
        changes["change_note"] = note
    result = store.update_task(conn, task["id"], changes, actor=actor, origin="kanban", cause_ref=cause or delegation_id,
                               based_on_version=task["version"], override_human=override)
    if result.applied:
        if "change_note" in result.applied:
            conn.execute("UPDATE tasks SET change_note_at=? WHERE id=?", (clock.iso(), task["id"]))
        effects.task_push(conn, task["id"])


def _set(conn: sqlite3.Connection, delegation_id: str, **fields: Any) -> None:
    fields["updated_at"] = clock.iso()
    assignments = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE delegations SET {assignments}, version=version+1 WHERE id=?", (*fields.values(), delegation_id))


def _executing_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM delegations WHERE status IN ('queued','running') AND card_id IS NOT NULL"
                        ).fetchone()[0]


def capacity(conn: sqlite3.Connection, cfg: Config) -> int:
    return int(cfg.get("kanban.max_running", 1)) - _executing_count(conn)


# --------------------------------------------------------------------------- submission

def submit_pending(conn: sqlite3.Connection, cfg: Config, kb: Kanban) -> dict[str, int]:
    stats = {"submitted": 0, "linked": 0, "waiting_capacity": 0, "errors": 0, "quarantined": 0}
    with file_lock(lock_path(cfg.home, "kanban-submit"), blocking=True, timeout=60):
        rows = conn.execute("SELECT id FROM delegations WHERE status IN ('pending_submit','reconcile')"
                            " AND pause_requested=0 ORDER BY created_at").fetchall()
        for row in rows:
            d = get(conn, row["id"])
            task = store.get_task(conn, d["task_id"])
            marker = f"todo-manager-delegation: {d['id']}"
            try:
                matches = kb.find(d["idem_key"], marker)
            except KanbanError as exc:
                with tx(conn):
                    _set(conn, d["id"], last_error=f"lookup failed: {exc}")
                stats["errors"] += 1
                continue
            if len(matches) > 1:
                with tx(conn):
                    _set(conn, d["id"], status="quarantined",
                         last_error="multiple cards match: " + ",".join(m["id"] for m in matches))
                    effects.task_comment(conn, d["task_id"], f"quarantine:{d['id']}",
                                         f"⚠️ 委派 {d['title']} 在看板上出现多张匹配卡片，已隔离等待人工处理。")
                stats["quarantined"] += 1
                continue
            if matches:
                card = matches[0]
                with tx(conn):
                    status = "cancelled" if card["status"] == "archived" else _status_from_card(card["status"], d)
                    _set(conn, d["id"], card_id=card["id"], card_status=card["status"], status=status, last_error=None)
                stats["linked"] += 1
                continue
            if capacity(conn, cfg) <= 0:
                stats["waiting_capacity"] += 1
                continue
            extra = [m["body"] for m in conn.execute(
                "SELECT body FROM delegation_messages WHERE delegation_id=? AND kind='supplement' AND status='pending'"
                " ORDER BY created_at", (d["id"],))]
            body = card_body(d, task, _notion_url(conn, task), extra)
            with tx(conn):
                _set(conn, d["id"], status="reconcile", submit_attempts=d["submit_attempts"] + 1)
            try:
                card_id = kb.create(title=f"{d['title']} [{d['id']}]", body=body, idem_key=d["idem_key"],
                                    assignee=d.get("assignee"), tenant="todo-manager",
                                    workspace=cfg.get("kanban.workspace") or None)
            except KanbanError as exc:
                with tx(conn):
                    _set(conn, d["id"], status="reconcile" if exc.uncertain else "pending_submit",
                         last_error=f"create failed: {exc}")
                stats["errors"] += 1
                continue
            with tx(conn):
                _set(conn, d["id"], card_id=card_id, card_status="ready", status="queued", last_error=None)
                conn.execute("UPDATE delegation_messages SET status='sent', sent_at=? WHERE delegation_id=?"
                             " AND kind='supplement' AND status='pending'", (clock.iso(), d["id"]))
            stats["submitted"] += 1
    return stats


def _notion_url(conn: sqlite3.Connection, task: dict[str, Any]) -> str | None:
    page = task.get("notion_page_id")
    return f"https://www.notion.so/{page.replace('-', '')}" if page else None


def _status_from_card(card_status: str, d: dict[str, Any]) -> str:
    if card_status in ("ready", "todo", "scheduled"):
        return "queued"
    if card_status == "running":
        return "running"
    if card_status in ("blocked", "triage"):
        return "paused" if d.get("pause_requested") else "blocked"
    if card_status == "review":
        return "review"
    if card_status == "done":
        return "ended"
    if card_status == "archived":
        return "cancelled"
    return d.get("status", "queued")


# --------------------------------------------------------------------------- result sync

def sync_cards(conn: sqlite3.Connection, cfg: Config, kb: Kanban) -> dict[str, int]:
    stats = {"checked": 0, "deliveries": 0, "questions": 0, "failures": 0, "errors": 0}
    rows = conn.execute("SELECT id FROM delegations WHERE card_id IS NOT NULL AND status NOT IN"
                        " ('ended','cancelled','quarantined')").fetchall()
    for row in rows:
        d = get(conn, row["id"])
        try:
            snap = kb.snapshot(d["card_id"], after_event=d["event_cursor"])
        except KanbanError as exc:
            with tx(conn):
                _set(conn, d["id"], last_error=f"snapshot failed: {exc}")
            stats["errors"] += 1
            continue
        stats["checked"] += 1
        with tx(conn):
            _apply_snapshot(conn, cfg, d, snap, stats)
    return stats


def _latest_run(snap: dict[str, Any]) -> dict[str, Any]:
    runs = snap.get("runs") or []
    return runs[-1] if runs else {}


def _apply_snapshot(conn: sqlite3.Connection, cfg: Config, d: dict[str, Any], snap: dict[str, Any],
                    stats: dict[str, int]) -> None:
    cursor = d["event_cursor"]
    block_seq = d["block_seq"]
    block_reason = d["block_reason"]
    for ev in snap["events"]:
        cursor = max(cursor, ev["id"])
        kind = ev["kind"]
        payload = ev.get("payload") or {}
        if kind in ("blocked", "block_loop_detected"):
            reason = payload.get("reason") or ""
            if reason.startswith(PAUSE_MARK):
                continue
            block_seq += 1
            block_reason = reason
            failure = payload.get("kind") is None and not reason
            delivery_kind = "failure" if failure else "question"
            dlv = _record_delivery(conn, d, snap, kind=delivery_kind, summary=reason or "执行失败", body=reason,
                                   seq=block_seq)
            if dlv:
                stats["questions" if delivery_kind == "question" else "failures"] += 1
                text = (f"❓ Agent 需要你补充（{d['title']}）：{reason}" if delivery_kind == "question"
                        else f"⚠️ Agent 执行失败（{d['title']}），需要你决定重试或取消。")
                effects.task_comment(conn, d["task_id"], f"dlv:{dlv}", text)
                effects.notify(conn, cfg, f"dlv:{dlv}", text)
        elif kind in ("crashed", "timed_out", "spawn_failed"):
            _set(conn, d["id"], last_error=f"{kind}: {payload.get('error') or payload.get('reason') or ''}"[:500])
        elif kind == "gave_up":
            # The failure breaker parked the card (blocked, untyped): the user decides retry/cancel.
            block_seq += 1
            block_reason = f"执行失败：{payload.get('error') or payload.get('reason') or kind}"[:500]
            dlv = _record_delivery(conn, d, snap, kind="failure", summary=block_reason, body=block_reason,
                                   seq=block_seq)
            if dlv:
                stats["failures"] += 1
                text = f"⚠️ Agent 执行失败（{d['title']}）：{block_reason}。回复“重试”或“取消”。"
                effects.task_comment(conn, d["task_id"], f"dlv:{dlv}", text)
                effects.notify(conn, cfg, f"dlv:{dlv}", text)
        elif kind == "completed" and _accepted_by_us(conn, d["id"]):
            continue
        elif kind in ("review_requested", "completed"):
            run = _latest_run(snap)
            meta = run.get("metadata") or {}
            summary = run.get("summary") or payload.get("summary") or ""
            body = meta.get("deliverable_markdown") or summary
            artifacts = list(meta.get("artifacts") or []) + [a.get("path") for a in snap.get("attachments", [])]
            seq = conn.execute("SELECT COUNT(*) FROM deliveries WHERE delegation_id=? AND kind='result'",
                               (d["id"],)).fetchone()[0] + 1
            dlv = _record_delivery(conn, d, snap, kind="result", summary=summary, body=body, seq=seq,
                                   artifacts=artifacts, run_id=str(run.get("id", "")))
            if dlv:
                stats["deliveries"] += 1
                # A new version voids approvals given for an earlier version.
                conn.execute("UPDATE approvals SET revoked_at=? WHERE delegation_id=? AND delivery_id != ?"
                             " AND consumed_at IS NULL AND revoked_at IS NULL", (clock.iso(), d["id"], dlv))
                text = f"📦 Agent 交付 v{seq}（{d['title']}）：{summary[:300]}"
                effects.task_comment(conn, d["task_id"], f"dlv:{dlv}", text)
                effects.notify(conn, cfg, f"dlv:{dlv}", text)
    card_status = snap["task"]["status"]
    new_status = _status_from_card(card_status, {**d, "status": d["status"]})
    if card_status == "done" and d["status"] != "ended":
        new_status = "ended"
    _set(conn, d["id"], event_cursor=cursor, card_status=card_status, status=new_status, block_seq=block_seq,
         block_reason=block_reason)
    if new_status != d["status"]:
        if new_status in ("queued", "running"):
            _task_state(conn, d["id"], "in_progress", "agent", None)
        elif new_status in ("blocked", "review"):
            _task_state(conn, d["id"], "review", "me",
                        "Agent 等你补充信息" if new_status == "blocked" else "Agent 交付待验收")
        elif new_status in ("ended", "cancelled") and not for_task_active_other(conn, d):
            _task_state(conn, d["id"], "todo", "me",
                        "Agent 委派已结束" if new_status == "ended" else "Agent 委派已取消")
        effects.task_push(conn, d["task_id"])


def _accepted_by_us(conn: sqlite3.Connection, delegation_id: str) -> bool:
    return conn.execute("SELECT 1 FROM delegation_messages WHERE delegation_id=? AND kind IN ('accept','cancel')"
                        " AND status IN ('sending','sent')", (delegation_id,)).fetchone() is not None


def for_task_active_other(conn: sqlite3.Connection, d: dict[str, Any]) -> bool:
    return conn.execute(
        f"SELECT 1 FROM delegations WHERE task_id=? AND id != ? AND status IN ({','.join(repr(s) for s in ACTIVE)})",
        (d["task_id"], d["id"])).fetchone() is not None


def _record_delivery(conn: sqlite3.Connection, d: dict[str, Any], snap: dict[str, Any], *, kind: str, summary: str,
                     body: str, seq: int, artifacts: list[str] | None = None, run_id: str = "") -> str | None:
    dlv = ids.new_id("delivery")
    cur = conn.execute(
        "INSERT OR IGNORE INTO deliveries(id, delegation_id, card_id, run_id, seq, kind, summary, body, artifacts,"
        " requirement_version, received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (dlv, d["id"], d["card_id"], run_id, seq, kind, summary, body, dumps([a for a in (artifacts or []) if a]),
         d["requirement_version"], clock.iso()))
    return dlv if cur.rowcount == 1 else None


def deliveries(conn: sqlite3.Connection, delegation_id: str) -> list[dict[str, Any]]:
    return [{**dict(r), "artifacts": loads(r["artifacts"], [])} for r in conn.execute(
        "SELECT * FROM deliveries WHERE delegation_id=? ORDER BY received_at", (delegation_id,))]


# --------------------------------------------------------------------------- follow-up messages

MESSAGE_KINDS = ("supplement", "answer", "revision", "pause", "resume", "cancel", "accept", "approval")


def add_message(conn: sqlite3.Connection, delegation_id: str, kind: str, body: str,
                feedback_id: str | None = None) -> str:
    if kind not in MESSAGE_KINDS:
        raise DelegationError(f"invalid message kind {kind!r}")
    d = get(conn, delegation_id)
    if d is None:
        raise DelegationError(f"delegation {delegation_id} not found")
    if d["status"] in ("ended", "cancelled") and kind not in ("supplement",):
        raise DelegationError(f"delegation is {d['status']}; create a new linked delegation for new work")
    with tx(conn):
        if feedback_id:
            existing = conn.execute("SELECT id FROM delegation_messages WHERE delegation_id=? AND feedback_id=? AND kind=?",
                                    (delegation_id, feedback_id, kind)).fetchone()
            if existing:
                return existing["id"]
        mid = ids.new_id("event")
        requirement_version = d["requirement_version"] + (1 if kind in ("supplement", "revision", "answer") else 0)
        conn.execute(
            "INSERT INTO delegation_messages(id, delegation_id, kind, feedback_id, body, requirement_version,"
            " target_block_seq, status, created_at) VALUES (?,?,?,?,?,?,?,'pending',?)",
            (mid, delegation_id, kind, feedback_id, body, requirement_version,
             d["block_seq"] if kind == "answer" else None, clock.iso()))
        if kind in ("supplement", "revision", "answer"):
            _set(conn, delegation_id, requirement_version=requirement_version)
        if kind == "pause":
            _set(conn, delegation_id, pause_requested=1)
        elif kind == "resume":
            _set(conn, delegation_id, pause_requested=0)
    return mid


def record_approval(conn: sqlite3.Connection, delegation_id: str, delivery_id: str, action: str, detail: str,
                    feedback_id: str | None) -> str:
    row = conn.execute("SELECT * FROM deliveries WHERE id=? AND delegation_id=?", (delivery_id, delegation_id)).fetchone()
    if row is None:
        raise DelegationError("approval must name a delivery of this delegation")
    latest = conn.execute("SELECT id FROM deliveries WHERE delegation_id=? AND kind='result' ORDER BY seq DESC LIMIT 1",
                          (delegation_id,)).fetchone()
    if latest is None or latest["id"] != delivery_id:
        raise DelegationError("approval must name the latest delivered version")
    aid = ids.new_id("approval")
    with tx(conn):
        conn.execute("INSERT INTO approvals(id, delegation_id, delivery_id, action, detail, feedback_id, granted_at)"
                     " VALUES (?,?,?,?,?,?,?)", (aid, delegation_id, delivery_id, action, detail, feedback_id,
                                                 clock.iso()))
    add_message(conn, delegation_id, "approval",
                f"APPROVED {action} for delivery {delivery_id} (v{row['seq']}). {detail}".strip(), feedback_id)
    return aid


def _comment_once(kb: Kanban, d: dict[str, Any], mid: str, text: str, already: bool) -> None:
    if already:
        snap = kb.snapshot(d["card_id"], after_event=10**12)
        if any(f"[tm:{mid}]" in (c.get("body") or "") for c in snap.get("comments", [])):
            return
    kb.comment(d["card_id"], f"{text}\n[tm:{mid}]")


def send_messages(conn: sqlite3.Connection, cfg: Config, kb: Kanban) -> dict[str, int]:
    stats = {"sent": 0, "waiting": 0, "superseded": 0, "failed": 0}
    rows = conn.execute("SELECT * FROM delegation_messages WHERE status IN ('pending','sending','waiting_capacity')"
                        " AND kind != 'requirement' ORDER BY created_at").fetchall()
    for msg in rows:
        d = get(conn, msg["delegation_id"])
        if d["card_id"] is None or d["status"] in ("pending_submit", "reconcile"):
            continue  # folded into the card body at submission
        try:
            outcome = _deliver(conn, cfg, kb, d, dict(msg))
        except KanbanError as exc:
            with tx(conn):
                conn.execute("UPDATE delegation_messages SET status=?, error=? WHERE id=?",
                             ("sending" if exc.uncertain or msg["status"] == "sending" else "pending", str(exc)[:500],
                              msg["id"]))
            stats["failed"] += 1
            continue
        stats[outcome] = stats.get(outcome, 0) + 1
    return stats


def _finish(conn: sqlite3.Connection, mid: str, status: str, error: str | None = None) -> None:
    with tx(conn):
        conn.execute("UPDATE delegation_messages SET status=?, sent_at=?, error=? WHERE id=?",
                     (status, clock.iso() if status == "sent" else None, error, mid))


def _deliver(conn: sqlite3.Connection, cfg: Config, kb: Kanban, d: dict[str, Any], msg: dict[str, Any]) -> str:
    kind, mid = msg["kind"], msg["id"]
    retry = msg["status"] == "sending"
    status = d["status"]
    needs_capacity = (kind == "answer" and status == "blocked") or (kind == "revision" and status == "review") or \
        (kind == "resume" and status == "paused") or (kind == "approval" and status == "review")
    if needs_capacity and d["pause_requested"] and kind != "resume":
        with tx(conn):
            conn.execute("UPDATE delegation_messages SET status='waiting_capacity', error='paused by user' WHERE id=?", (mid,))
        return "waiting"
    if needs_capacity and capacity(conn, cfg) <= 0:
        with tx(conn):
            conn.execute("UPDATE delegation_messages SET status='waiting_capacity' WHERE id=?", (mid,))
        return "waiting"
    if kind == "answer" and status == "blocked" and msg["target_block_seq"] != d["block_seq"]:
        _finish(conn, mid, "superseded", "answer targets an earlier question")
        return "superseded"
    with tx(conn):
        conn.execute("UPDATE delegation_messages SET status='sending' WHERE id=?", (mid,))
    labels = {"supplement": "补充", "answer": "回答", "revision": "修订要求", "approval": "批准",
              "resume": "继续", "accept": "已验收", "cancel": "取消"}
    if kind == "pause":
        if status in EXECUTING:
            _comment_once(kb, d, mid, f"{PAUSE_MARK} PAUSE: stop now and take no further actions. {msg['body']}", retry)
            snap = kb.snapshot(d["card_id"], after_event=10**12)
            if snap["task"]["status"] in ("running", "ready"):
                kb.block(d["card_id"], f"{PAUSE_MARK} {msg['body']}"[:500], None)
        _finish(conn, mid, "sent")
        with tx(conn):
            _set(conn, d["id"], status="paused" if status in EXECUTING else status)
        return "sent"
    if kind == "cancel":
        if status not in ("ended", "cancelled"):
            kb.archive(d["card_id"])
        _finish(conn, mid, "sent")
        with tx(conn):
            _set(conn, d["id"], status="cancelled")
            if not for_task_active_other(conn, d):
                _task_state(conn, d["id"], "todo", "me", "Agent 委派已取消")
        return "sent"
    if kind == "accept":
        if status != "review":
            _finish(conn, mid, "failed", f"cannot accept while delegation is {status}")
            return "failed"
        kb.complete(d["card_id"], f"accepted by user: {msg['body']}"[:500])
        _finish(conn, mid, "sent")
        with tx(conn):
            _set(conn, d["id"], status="ended")
            if not for_task_active_other(conn, d):
                _task_state(conn, d["id"], "todo", "me", "Agent 交付已验收")
        return "sent"
    if kind == "resume":
        if status == "paused":
            snap = kb.snapshot(d["card_id"], after_event=10**12)
            if snap["task"]["status"] == "blocked":
                kb.unblock(d["card_id"])
        _finish(conn, mid, "sent")
        return "sent"
    # supplement / answer / revision / approval: comment, then state transition if applicable.
    _comment_once(kb, d, mid, f"[{labels[kind]}] {msg['body']}", retry)
    snap = kb.snapshot(d["card_id"], after_event=10**12)
    card_status = snap["task"]["status"]
    if kind == "answer" and status == "blocked":
        if card_status == "blocked":
            kb.unblock(d["card_id"])
        elif card_status == "triage":
            kb.respecify(d["card_id"])
    elif kind in ("revision", "approval") and status == "review" and card_status == "review":
        kb.reopen_review(d["card_id"])
    _finish(conn, mid, "sent")
    return "sent"


def summary_line(d: dict[str, Any]) -> str:
    return f"{d['id']} [{d['status']}] {d['title']} card={d.get('card_id') or '-'}"


def to_json(d: dict[str, Any]) -> str:
    return json.dumps(d, ensure_ascii=False, default=str)
