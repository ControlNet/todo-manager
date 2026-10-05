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
  answer     -> only against the block event it answers (``target_event_id``); comment +
                unblock (or respecify when the Kanban loop breaker parked it in triage)
  revision   -> comment + reopen-review of the delivery it revises
  approval   -> only while its approval record is valid; comment + reopen-review, used once
  pause      -> comment + block (our marker); resume -> unblock of our own pause event only
  cancel     -> archive (terminates a running worker; confirmed from the termination event)
  accept     -> complete the review card of the latest delivery
Every comment carries ``[tm:<message id>]`` so a retry after a lost response does not
comment twice. State transitions are bound to the Kanban event they answer and checked
right before they are applied (``kanban.transition_verdict``): a retry after a lost
response finds the transition already done instead of acting on a newer question/version.
Card completion only ends the delegation; the Task's own completion is decided from its
done criteria and later evidence.

Lifecycle: every entry point (CLI, Notion, feedback, Matrix) changes the Task through the
store; ``enforce_task_lifecycle`` turns a closed Task into exactly one accept/cancel per
active delegation before any card is touched, so no entry can forget it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import clock, effects, i18n, ids, store
from .config import Config
from .db import dumps, get_meta, loads, row_dict, set_meta, tx
from .files import keep_artifacts
from .kanban import Kanban, KanbanError, transition_verdict
from .locks import file_lock, lock_path

ACTIVE = ("pending_submit", "queued", "running", "blocked", "review", "paused", "reconcile")
EXECUTING = ("queued", "running")
CAPACITY_KINDS = ("answer", "revision", "resume", "approval")
RUN_ENDED_EVENTS = ("crashed", "timed_out", "spawn_failed", "gave_up", "reclaimed")
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
- Need information or a decision from the user? Call kanban_block(kind="needs_input", reason="<one concrete question in {language}>"). Do not guess.
- Deliverable ready? Call kanban_request_review(summary="<2-3 sentence summary in {language}>", metadata={{"deliverable_markdown": "<the full deliverable in Markdown>", "external_actions_done": [...], "notes": "..."}}, artifacts=[<absolute paths of files to keep>]). Do NOT call kanban_complete: the user reviews in Notion.
- Comments by `todo-manager` are the user's supplements, answers, revision requests or approvals: follow them. A comment starting with {pause} means stop immediately and take no further action.
- An external action whose outcome you cannot confirm (timeout, crash, unclear reply) must NOT be repeated: report it with kanban_block(kind="needs_input") and let the user check. The same applies when an earlier run of this card ended without a result after an APPROVED comment: assume the approved action may have happened and ask instead of doing it again.
- Scratch workspaces are deleted when the card ends: keep needed files in artifacts.
"""


class DelegationError(ValueError):
    pass


def action_names(allowed: list[str] | None) -> set[str]:
    """Action names of an allowed-actions list; ``send_email:bob@x.org`` is ``send_email``.
    The one parser for validation, external-action checks and approvals."""
    return {str(a).split(":", 1)[0].strip() for a in allowed or [] if str(a).strip()}


def external_actions(allowed: list[str] | None) -> set[str]:
    return action_names(allowed) & EXTERNAL_ACTIONS


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
    parts.append(WORKER_PROTOCOL.format(allowed=allowed, pause=PAUSE_MARK, language=i18n.language_name()))
    return "\n\n".join(parts)


def create(conn: sqlite3.Connection, cfg: Config, *, task_id: str, title: str, goal: str, context: str | None = None,
           constraints: str | None = None, deliverable: str | None = None, done_criteria: str | None = None,
           allowed_actions: list[str] | None = None, feedback_id: str | None = None, actor: str = "human",
           op_key: str | None = None) -> str:
    """``op_key`` (feedback id + op index) identifies the request: applying the same op again
    returns the same delegation, another op of the same feedback creates its own."""
    task = store.get_task(conn, task_id)
    if task is None:
        raise DelegationError(f"task {task_id} not found")
    if task["status"] in store.CLOSED_STATUSES:
        raise DelegationError("task is closed; reopen it or create a follow-up task first")
    if not goal.strip():
        raise DelegationError("goal is required")
    allowed = allowed_actions or ["read", "analyze", "draft"]
    unknown = action_names(allowed) - KNOWN_ACTIONS
    if unknown:
        raise DelegationError(f"unknown actions {sorted(unknown)}; known: {sorted(KNOWN_ACTIONS)}")
    if op_key:
        existing = conn.execute("SELECT id FROM delegations WHERE op_key=?", (op_key,)).fetchone()
    elif feedback_id:
        existing = conn.execute("SELECT id FROM delegations WHERE from_feedback_id=?", (feedback_id,)).fetchone()
    else:
        existing = None
    if existing:
        return existing["id"]
    did = ids.new_id("delegation")
    now = clock.iso()
    with tx(conn):
        conn.execute(
            "INSERT INTO delegations(id, task_id, title, goal, context, constraints, deliverable, done_criteria,"
            " allowed_actions, assignee, requirement_version, status, board, idem_key, from_feedback_id,"
            " base_task_version, created_at, updated_at, op_key) VALUES (?,?,?,?,?,?,?,?,?,?,1,'pending_submit',?,?,?,?,?,?,?)",
            (did, task_id, title.strip(), goal.strip(), context, constraints, deliverable, done_criteria,
             dumps(allowed), cfg.get("kanban.assignee") or None, cfg.get("kanban.board", "todo-manager"),
             ids.delegation_idempotency_key(did), feedback_id, task["version"], now, now, op_key))
        conn.execute(
            "INSERT INTO delegation_messages(id, delegation_id, kind, feedback_id, body, requirement_version, status,"
            " created_at, sent_at) VALUES (?,?,?,?,?,1,'sent',?,?)",
            (ids.new_id("event"), did, "requirement", feedback_id, goal.strip(), now, now))
        _task_state(conn, did, "in_progress", "agent", i18n.t("dlg.assigned", title=title.strip()), actor=actor,
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


def _executing_count(conn: sqlite3.Connection, exclude: str | None = None) -> int:
    """Cards that are (or may be) executing, plus reservations: an uncertain create, and a
    resume-type transition that is in flight or whose outcome is not known yet."""
    return conn.execute(
        "SELECT COUNT(*) FROM delegations d WHERE d.id IS NOT ? AND ("
        " (d.status IN ('queued','running') AND d.card_id IS NOT NULL) OR d.status='reconcile'"
        " OR (d.status NOT IN ('ended','cancelled','quarantined') AND EXISTS (SELECT 1 FROM delegation_messages m"
        "     WHERE m.delegation_id=d.id AND m.status='sending' AND m.kind IN ('answer','revision','resume','approval'))))",
        (exclude,)).fetchone()[0]


def capacity(conn: sqlite3.Connection, cfg: Config, exclude: str | None = None) -> int:
    return int(cfg.get("kanban.max_running", 1)) - _executing_count(conn, exclude)


# --------------------------------------------------------------------------- lifecycle

def _closing_message(conn: sqlite3.Connection, d: dict[str, Any], task_status: str, reason: str,
                     feedback_id: str | None) -> str | None:
    """Exactly one accept/cancel per delegation of a closed Task."""
    if conn.execute("SELECT 1 FROM delegation_messages WHERE delegation_id=? AND kind IN ('accept','cancel')"
                    " AND status IN ('pending','sending','sent','waiting_capacity')", (d["id"],)).fetchone():
        return None
    kind = "accept" if d["status"] == "review" and task_status == "done" else "cancel"
    add_message(conn, d["id"], kind, reason, feedback_id=feedback_id, cause="task_closed")
    return kind


def enforce_task_lifecycle(conn: sqlite3.Connection) -> dict[str, int]:
    """Shared by every entry point: runs before any card is touched in a cycle."""
    stats = {"closing": 0, "withdrawn": 0}
    rows = conn.execute(
        f"SELECT d.id, t.status AS task_status FROM delegations d JOIN tasks t ON t.id=d.task_id"
        f" WHERE d.status IN ({','.join(repr(x) for x in ACTIVE)})").fetchall()
    for r in rows:
        d = get(conn, r["id"])
        if r["task_status"] in store.CLOSED_STATUSES:
            if _closing_message(conn, d, r["task_status"], i18n.t("dlg.task_closed"), None):
                stats["closing"] += 1
            continue
        with tx(conn):
            cur = conn.execute("UPDATE delegation_messages SET status='superseded', error=? WHERE delegation_id=?"
                               " AND cause='task_closed' AND status IN ('pending','waiting_capacity')",
                               (i18n.t("dlg.reopened_withdraw"), d["id"]))
        stats["withdrawn"] += cur.rowcount
    return stats


# --------------------------------------------------------------------------- submission

HOLD_KEY = "delegations.hold"


def hold(conn: sqlite3.Connection, reason: str) -> None:
    """Stop submitting cards and sending messages until the user releases the hold (after a
    restore or an import: the board may have moved on, or be a different one). Card results
    are still read."""
    set_meta(conn, HOLD_KEY, dumps({"reason": reason, "at": clock.iso()}))


def held(conn: sqlite3.Connection) -> dict[str, Any] | None:
    return loads(get_meta(conn, HOLD_KEY), None)


def release_hold(conn: sqlite3.Connection) -> dict[str, Any] | None:
    previous = held(conn)
    with tx(conn):
        conn.execute("DELETE FROM meta WHERE key=?", (HOLD_KEY,))
    return previous


# Global Hermes switches the board protocol needs (docs/board-protocol.md). todo-manager only
# reads them: with review_dispatch on, an sdlc-review worker takes every review card (the user's
# review); with auto_decompose on, a card parked in triage is fanned out into new cards.
REQUIRED_SETTINGS = {"review_dispatch": False, "auto_decompose": False}
SETTINGS_KEY = "kanban.settings_problems"


def check_settings(conn: sqlite3.Connection, kb: Kanban) -> list[str]:
    """Read the Hermes switches and record what is wrong (shown by ``health``); never changes them.
    An unreadable value raises KanbanError and counts as not verified."""
    values = kb.settings()
    wrong = [f"kanban.{key} is {str(values.get(key, True)).lower()}; the board needs {str(want).lower()}"
             f" (`hermes config set kanban.{key} {str(want).lower()}`, a global Hermes change you make)"
             for key, want in REQUIRED_SETTINGS.items() if bool(values.get(key, True)) != want]
    with tx(conn):
        if wrong:
            set_meta(conn, SETTINGS_KEY, dumps({"problems": wrong, "at": clock.iso()}))
        else:
            conn.execute("DELETE FROM meta WHERE key=?", (SETTINGS_KEY,))
    return wrong


def _failed(stats: dict[str, Any], counter: str, error: str) -> None:
    """Count a failed Kanban call; the first error of the pass is kept for channel health."""
    stats[counter] += 1
    stats.setdefault("error", error[:500])


def settings_problems(conn: sqlite3.Connection) -> list[str]:
    return (loads(get_meta(conn, SETTINGS_KEY), None) or {}).get("problems", [])


def submit_pending(conn: sqlite3.Connection, cfg: Config, kb: Kanban) -> dict[str, Any]:
    stats: dict[str, Any] = {"submitted": 0, "linked": 0, "waiting_capacity": 0, "errors": 0, "quarantined": 0, "held": 0,
             "unsafe_settings": 0}
    settings_ok: bool | None = None
    if held(conn):
        stats["held"] = conn.execute("SELECT COUNT(*) FROM delegations WHERE status IN ('pending_submit','reconcile')"
                                     ).fetchone()[0]
        return stats
    with file_lock(lock_path(cfg.home, "kanban-submit"), blocking=True, timeout=60):
        rows = conn.execute("SELECT id FROM delegations WHERE status IN ('pending_submit','reconcile')"
                            " AND pause_requested=0 ORDER BY created_at").fetchall()
        for row in rows:
            d = get(conn, row["id"])
            task = store.get_task(conn, d["task_id"])
            closing = conn.execute("SELECT 1 FROM delegation_messages WHERE delegation_id=? AND kind='cancel'"
                                   " AND status IN ('pending','sending','waiting_capacity')", (d["id"],)).fetchone()
            if d["status"] == "pending_submit" and (task is None or task["status"] in store.CLOSED_STATUSES or closing):
                stats["held"] += 1  # never create a card for a closed Task; send_messages cancels it
                continue
            marker = f"todo-manager-delegation: {d['id']}"
            try:
                matches = kb.find(d["idem_key"], marker)
            except KanbanError as exc:
                with tx(conn):
                    _set(conn, d["id"], last_error=f"lookup failed: {exc}")
                _failed(stats, "errors", f"lookup failed: {exc}")
                continue
            if len(matches) > 1:
                with tx(conn):
                    _set(conn, d["id"], status="quarantined",
                         last_error="multiple cards match: " + ",".join(m["id"] for m in matches))
                    effects.task_comment(conn, d["task_id"], f"quarantine:{d['id']}",
                                         i18n.t("dlg.quarantine", title=d["title"]))
                stats["quarantined"] += 1
                continue
            if matches:
                card = matches[0]
                with tx(conn):
                    status = "cancelled" if card["status"] == "archived" else _status_from_card(card["status"], d)
                    _set(conn, d["id"], card_id=card["id"], card_status=card["status"], status=status, last_error=None)
                stats["linked"] += 1
                continue
            if task is None or task["status"] in store.CLOSED_STATUSES or closing:
                # An uncertain create that did not happen, for a Task closed meanwhile.
                with tx(conn):
                    _set(conn, d["id"], status="pending_submit", last_error=None)
                stats["held"] += 1
                continue
            if capacity(conn, cfg, exclude=d["id"]) <= 0:
                stats["waiting_capacity"] += 1
                continue
            if settings_ok is None:
                try:
                    settings_ok = not check_settings(conn, kb)
                except KanbanError as exc:
                    with tx(conn):
                        _set(conn, d["id"], last_error=f"Hermes settings not verified: {exc}")
                    _failed(stats, "errors", f"Hermes settings not verified: {exc}")
                    settings_ok = False
                    continue
            if not settings_ok:
                stats["unsafe_settings"] += 1
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
                                    workspace=cfg.get("kanban.workspace") or None,
                                    max_retries=int(cfg.get("kanban.max_retries", 1)) or None)
            except KanbanError as exc:
                with tx(conn):
                    _set(conn, d["id"], status="reconcile" if exc.uncertain else "pending_submit",
                         last_error=f"create failed: {exc}")
                _failed(stats, "errors", f"create failed: {exc}")
                continue
            with tx(conn):
                _set(conn, d["id"], card_id=card_id, card_status="ready", status="queued", last_error=None)
                # Supplements are in the card body; a pause/resume pair before submission is spent.
                # Position 0: in the card body, read by every run of the card.
                conn.execute("UPDATE delegation_messages SET status='sent', sent_at=?, card_event_id=0 WHERE"
                             " delegation_id=? AND kind IN ('supplement','pause','resume') AND status='pending'",
                             (clock.iso(), d["id"]))
            stats["submitted"] += 1
    return stats


def _notion_url(conn: sqlite3.Connection, task: dict[str, Any]) -> str | None:
    row = conn.execute("SELECT page_id, url FROM notion_pages WHERE entity_type='task' AND entity_id=?",
                       (task["id"],)).fetchone()
    if row is None:
        return None
    return row["url"] or f"https://www.notion.so/{row['page_id'].replace('-', '')}"


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

def sync_cards(conn: sqlite3.Connection, cfg: Config, kb: Kanban) -> dict[str, Any]:
    stats: dict[str, Any] = {"checked": 0, "deliveries": 0, "questions": 0, "failures": 0, "errors": 0}
    rows = conn.execute("SELECT id FROM delegations WHERE card_id IS NOT NULL AND status NOT IN"
                        " ('ended','cancelled','quarantined')").fetchall()
    for row in rows:
        d = get(conn, row["id"])
        try:
            snap = kb.snapshot(d["card_id"], after_event=d["event_cursor"])
        except KanbanError as exc:
            with tx(conn):
                _set(conn, d["id"], last_error=f"snapshot failed: {exc}")
            _failed(stats, "errors", f"snapshot failed: {exc}")
            continue
        stats["checked"] += 1
        # While a cancel is on its way, whatever the card still does is history only.
        quiet = conn.execute("SELECT 1 FROM delegation_messages WHERE delegation_id=? AND kind='cancel'"
                             " AND status IN ('pending','sending','waiting_capacity')", (d["id"],)).fetchone() is not None
        with tx(conn):
            _apply_snapshot(conn, cfg, d, snap, stats, quiet=quiet)
    return stats


def _event_run(snap: dict[str, Any], ev: dict[str, Any]) -> dict[str, Any]:
    """The run that produced ``ev`` (never simply the latest one: several results can be
    waiting in one snapshot)."""
    run_id = ev.get("run_id") or (ev.get("payload") or {}).get("run_id")
    for run in snap.get("runs") or []:
        if run_id is not None and str(run.get("id")) == str(run_id):
            return run
    return {}


def _latest_result(conn: sqlite3.Connection, delegation_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM deliveries WHERE delegation_id=? AND kind='result' AND after_cancel=0"
                        " ORDER BY seq DESC LIMIT 1", (delegation_id,)).fetchone()


def _apply_snapshot(conn: sqlite3.Connection, cfg: Config, d: dict[str, Any], snap: dict[str, Any],
                    stats: dict[str, int], quiet: bool = False) -> None:
    """Record what happened on the card. ``quiet``: the delegation was cancelled; late
    results are kept as history but change nothing and are not announced."""
    cursor = d["event_cursor"]
    block_seq = d["block_seq"]
    block_reason = d["block_reason"]
    block_event = d["block_event_id"]
    pause_event = d["pause_event_id"]

    def announce(dlv: str, text: str) -> None:
        if not quiet:
            effects.task_comment(conn, d["task_id"], f"dlv:{dlv}", text)
            effects.notify(conn, cfg, f"dlv:{dlv}", text, task_id=d["task_id"])

    for ev in snap["events"]:
        cursor = max(cursor, ev["id"])
        kind = ev["kind"]
        payload = ev.get("payload") or {}
        if kind in ("blocked", "block_loop_detected"):
            reason = payload.get("reason") or ""
            if reason.startswith(PAUSE_MARK):
                pause_event = ev["id"]
                continue
            block_seq += 1
            block_reason = reason
            block_event = ev["id"]
            failure = payload.get("kind") is None and not reason
            delivery_kind = "failure" if failure else "question"
            dlv = _record_delivery(conn, d, kind=delivery_kind, summary=reason or i18n.t("dlg.failed_short"),
                                   body=reason, seq=block_seq, event_id=ev["id"], run_id=str(ev.get("run_id") or ""),
                                   after_cancel=quiet)
            if dlv:
                stats["questions" if delivery_kind == "question" else "failures"] += 1
                announce(dlv, i18n.t("dlg.question", title=d["title"], reason=reason) if delivery_kind == "question"
                         else i18n.t("dlg.failed", title=d["title"]))
        elif kind in RUN_ENDED_EVENTS:
            if kind == "gave_up":
                # The failure breaker parked the card (blocked, untyped): the user decides retry/cancel.
                block_seq += 1
                block_event = ev["id"]
                block_reason = i18n.t("dlg.failure_reason",
                                      reason=payload.get("error") or payload.get("reason") or kind)[:500]
                dlv = _record_delivery(conn, d, kind="failure", summary=block_reason, body=block_reason,
                                       seq=block_seq, event_id=ev["id"], run_id=str(ev.get("run_id") or ""),
                                       after_cancel=quiet)
                if dlv:
                    stats["failures"] += 1
                    announce(dlv, i18n.t("dlg.failed_retry", title=d["title"], reason=block_reason))
            else:
                _set(conn, d["id"], last_error=f"{kind}: {payload.get('error') or payload.get('reason') or ''}"[:500])
            if not quiet:
                _hold_unknown_external_action(conn, cfg, d, kind)
        elif kind == "completed" and _accepted_by_us(conn, d["id"]):
            continue
        elif kind in ("review_requested", "completed"):
            run = _event_run(snap, ev)
            meta = run.get("metadata") or {}
            summary = run.get("summary") or payload.get("summary") or ""
            body = meta.get("deliverable_markdown") or summary
            artifacts = list(meta.get("artifacts") or []) + [a.get("path") for a in snap.get("attachments", [])]
            seq = conn.execute("SELECT COUNT(*) FROM deliveries WHERE delegation_id=? AND kind='result'",
                               (d["id"],)).fetchone()[0] + 1
            dlv = _record_delivery(conn, d, kind="result", summary=summary, body=body, seq=seq, artifacts=artifacts,
                                   run_id=str(run.get("id") or ev.get("run_id") or ""), event_id=ev["id"],
                                   after_cancel=quiet, home=cfg.home)
            if dlv:
                stats["deliveries"] += 1
                _settle_approvals(conn, cfg, d, dlv)
                announce(dlv, i18n.t("dlg.delivered", seq=seq, title=d["title"], summary=summary[:300]))
    card_status = snap["task"]["status"]
    if quiet:
        _set(conn, d["id"], event_cursor=cursor, card_status=card_status, block_seq=block_seq,
             block_reason=block_reason, block_event_id=block_event, pause_event_id=pause_event)
        return
    new_status = _status_from_card(card_status, {**d, "status": d["status"]})
    if card_status == "done" and d["status"] != "ended":
        new_status = "ended"
    _set(conn, d["id"], event_cursor=cursor, card_status=card_status, status=new_status, block_seq=block_seq,
         block_reason=block_reason, block_event_id=block_event, pause_event_id=pause_event)
    if new_status != d["status"]:
        if new_status in ("queued", "running"):
            _task_state(conn, d["id"], "in_progress", "agent", None)
        elif new_status in ("blocked", "review"):
            _task_state(conn, d["id"], "review", "me",
                        i18n.t("dlg.needs_input" if new_status == "blocked" else "dlg.to_review"))
        elif new_status in ("ended", "cancelled") and not for_task_active_other(conn, d):
            _task_state(conn, d["id"], "todo", "me",
                        i18n.t("dlg.ended" if new_status == "ended" else "dlg.cancelled"))
        effects.task_push(conn, d["task_id"])


def _settle_approvals(conn: sqlite3.Connection, cfg: Config, d: dict[str, Any], delivery_id: str) -> None:
    """A new version arrived: approvals already sent are used up (the worker reports what
    it did in this delivery); approvals not sent yet are void and their messages withdrawn."""
    conn.execute("UPDATE approvals SET outcome='reported' WHERE delegation_id=? AND consumed_at IS NOT NULL"
                 " AND outcome IS NULL", (d["id"],))
    for a in conn.execute("SELECT a.*, m.id AS mid, m.status AS mstatus FROM approvals a LEFT JOIN delegation_messages m"
                          " ON m.approval_id=a.id WHERE a.delegation_id=? AND a.delivery_id != ?"
                          " AND a.consumed_at IS NULL AND a.revoked_at IS NULL", (d["id"], delivery_id)).fetchall():
        if a["mstatus"] == "sending":
            continue  # in flight: its retry reconciles against the review event it was bound to
        _void_approval(conn, d, a, "newer_version")


def _void_approval(conn: sqlite3.Connection, d: dict[str, Any], a: sqlite3.Row, reason: str) -> None:
    conn.execute("UPDATE approvals SET revoked_at=COALESCE(revoked_at, ?) WHERE id=?", (clock.iso(), a["id"]))
    conn.execute("UPDATE delegation_messages SET status='superseded', error=? WHERE approval_id=?"
                 " AND status IN ('pending','waiting_capacity','sending')", (f"approval void: {reason}", a["id"]))
    effects.task_comment(conn, d["task_id"], f"approval-void:{a['id']}",
                         i18n.t("dlg.approval_void", action=a["action"], delivery=a["delivery_id"],
                                reason=i18n.t(f"dlg.reason.{reason}")))


def _hold_unknown_external_action(conn: sqlite3.Connection, cfg: Config, d: dict[str, Any], event: str) -> None:
    """A run that was handed an approval ended without reporting back: the external action
    may or may not have happened. Pause the card so it is not retried automatically, and ask."""
    handed = conn.execute("SELECT * FROM approvals WHERE delegation_id=? AND consumed_at IS NOT NULL AND outcome IS NULL"
                          " ORDER BY consumed_at, rowid", (d["id"],)).fetchall()
    if not handed:
        return
    text = ""
    for a in handed:                       # approvals of one version go out to the same run
        conn.execute("UPDATE approvals SET outcome='unknown' WHERE id=?", (a["id"],))
        text = i18n.t("dlg.approval_unknown", action=a["action"], title=d["title"], event=event)
        effects.task_comment(conn, d["task_id"], f"approval-unknown:{a['id']}", text)
        effects.notify(conn, cfg, f"approval-unknown:{a['id']}", text, task_id=d["task_id"])
    if not d["pause_requested"]:
        add_message(conn, d["id"], "pause", text, cause="approval_unknown")


def _accepted_by_us(conn: sqlite3.Connection, delegation_id: str) -> bool:
    return conn.execute("SELECT 1 FROM delegation_messages WHERE delegation_id=? AND kind IN ('accept','cancel')"
                        " AND status IN ('sending','sent')", (delegation_id,)).fetchone() is not None


def for_task_active_other(conn: sqlite3.Connection, d: dict[str, Any]) -> bool:
    return conn.execute(
        f"SELECT 1 FROM delegations WHERE task_id=? AND id != ? AND status IN ({','.join(repr(s) for s in ACTIVE)})",
        (d["task_id"], d["id"])).fetchone() is not None


def _record_delivery(conn: sqlite3.Connection, d: dict[str, Any], *, kind: str, summary: str, body: str, seq: int,
                     event_id: int, artifacts: list[str] | None = None, run_id: str = "",
                     after_cancel: bool = False, home: Path | None = None) -> str | None:
    """One delivery per Kanban event: a replay (older cursor after a restore) is ignored. Its
    files are copied into the workspace (relative path + SHA-256): a worker's scratch space is
    deleted when the card ends."""
    dlv = ids.new_id("delivery")
    paths = [a for a in (artifacts or []) if a]
    cur = conn.execute(
        "INSERT OR IGNORE INTO deliveries(id, delegation_id, card_id, run_id, seq, kind, summary, body, artifacts,"
        " requirement_version, received_at, source_event_id, after_cancel) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (dlv, d["id"], d["card_id"], run_id, seq, kind, summary, body, dumps(paths),
         received_requirement_version(conn, d, event_id), clock.iso(), event_id, 1 if after_cancel else 0))
    if cur.rowcount != 1:
        return None
    if paths and home is not None:
        conn.execute("UPDATE deliveries SET artifacts=? WHERE id=?", (dumps(keep_artifacts(home, dlv, paths)), dlv))
    return dlv


def deliveries(conn: sqlite3.Connection, delegation_id: str) -> list[dict[str, Any]]:
    return [{**dict(r), "artifacts": loads(r["artifacts"], [])} for r in conn.execute(
        "SELECT * FROM deliveries WHERE delegation_id=? ORDER BY received_at, rowid", (delegation_id,))]


# --------------------------------------------------------------------------- follow-up messages

MESSAGE_KINDS = ("supplement", "answer", "revision", "pause", "resume", "cancel", "accept", "approval")


def _bound_delivery(conn: sqlite3.Connection, delegation_id: str, delivery_id: str | None) -> sqlite3.Row | None:
    if delivery_id is None:
        return _latest_result(conn, delegation_id)
    row = conn.execute("SELECT * FROM deliveries WHERE id=? AND delegation_id=? AND kind='result'",
                       (delivery_id, delegation_id)).fetchone()
    if row is None:
        raise DelegationError(f"{delivery_id} is not a delivered version of {delegation_id}")
    return row


REQUIREMENT_KINDS = ("supplement", "revision", "answer")


def received_requirement_version(conn: sqlite3.Connection, d: dict[str, Any], event_id: int | None,
                                 legacy_received: bool = True) -> int:
    """The requirement version the result at card event ``event_id`` was built on: every
    requirement message up to it was on the card before that event - in the card body (read
    when a run starts; position 0) or as a comment placed before it (Hermes steers comments into
    a running worker). Written to the card is not the same as adopted: a comment that landed
    after the worker submitted, or one whose place is not known yet, is not part of that run.
    Messages sent before positions were recorded count as received (as they did), unless
    ``legacy_received`` is False (used when raising an earlier version's number)."""
    current = conn.execute("SELECT requirement_version FROM delegations WHERE id=?", (d["id"],)).fetchone()[0]
    missing: list[int] = []
    for m in conn.execute("SELECT requirement_version, status, card_event_id, card_event_before FROM delegation_messages"
                          f" WHERE delegation_id=? AND kind IN ({','.join(repr(k) for k in REQUIREMENT_KINDS)})"
                          " AND status NOT IN ('superseded','failed')", (d["id"],)):
        if m["card_event_id"] is not None:
            received = event_id is None or int(m["card_event_id"]) < int(event_id)
        else:
            received = legacy_received and m["status"] == "sent" and m["card_event_before"] is None
        if not received:
            missing.append(int(m["requirement_version"]))
    return current if not missing else min(current, min(missing) - 1)


def _placed(conn: sqlite3.Connection, delegation_id: str, mid: str, event_id: int) -> None:
    """Record where a message's comment is on the card. A result recorded while its place was
    not known did not count it; when it was in fact there before, that result's version rises."""
    with tx(conn):
        conn.execute("UPDATE delegation_messages SET card_event_id=? WHERE id=?", (event_id, mid))
        for dl in conn.execute("SELECT id, requirement_version, source_event_id FROM deliveries WHERE delegation_id=?"
                               " AND source_event_id > ?", (delegation_id, event_id)).fetchall():
            known = received_requirement_version(conn, {"id": delegation_id}, dl["source_event_id"],
                                                 legacy_received=False)
            if known > (dl["requirement_version"] or 0):
                conn.execute("UPDATE deliveries SET requirement_version=? WHERE id=?", (known, dl["id"]))


def add_message(conn: sqlite3.Connection, delegation_id: str, kind: str, body: str,
                feedback_id: str | None = None, approval_id: str | None = None, cause: str | None = None,
                delivery_id: str | None = None, op_key: str | None = None) -> str:
    """Queue a message. Its target is fixed here and kept for every retry: an answer binds to
    the open question, accept/revision to ``delivery_id`` (the version the user replied to) or
    else the latest delivered version, resume to the latest pause. ``op_key`` (feedback id + op
    index) makes a retried op return its message while a second op of the same kind in the
    same feedback gets its own; without it, one message per feedback item and kind."""
    if kind not in MESSAGE_KINDS:
        raise DelegationError(f"invalid message kind {kind!r}")
    d = get(conn, delegation_id)
    if d is None:
        raise DelegationError(f"delegation {delegation_id} not found")
    if d["status"] in ("ended", "cancelled") and kind not in ("supplement",):
        raise DelegationError(f"delegation is {d['status']}; create a new linked delegation for new work")
    if kind == "accept":
        # Shared by every entry point: an accept is for a version the user has seen, never for
        # whatever the worker delivers next.
        if d["status"] != "review":
            raise DelegationError(f"nothing to accept: the delegation is {d['status']}; only a delivered version"
                                  " under review can be accepted")
        if _bound_delivery(conn, delegation_id, delivery_id) is None:
            raise DelegationError("nothing to accept: no version has been delivered")
    with tx(conn):
        if op_key:
            existing = conn.execute("SELECT id FROM delegation_messages WHERE op_key=?", (op_key,)).fetchone()
        elif feedback_id:
            existing = conn.execute("SELECT id FROM delegation_messages WHERE delegation_id=? AND feedback_id=? AND kind=?",
                                    (delegation_id, feedback_id, kind)).fetchone()
        else:
            existing = None
        if existing:
            return existing["id"]
        mid = ids.new_id("event")
        changes_requirement = kind in ("supplement", "revision", "answer")
        requirement_version = d["requirement_version"] + (1 if changes_requirement else 0)
        answering = kind == "answer" and d["status"] == "blocked"
        target_event = int(d["block_event_id"]) if answering and d["block_event_id"] is not None else None
        target_seq = target_message = None
        if kind in ("revision", "accept"):
            bound = _bound_delivery(conn, delegation_id, delivery_id)
            target_seq = bound["seq"] if bound else 0          # 0: no version delivered yet
            target_event = bound["source_event_id"] if bound else None
        elif kind == "resume":
            pause = conn.execute("SELECT id FROM delegation_messages WHERE delegation_id=? AND kind='pause'"
                                 " AND status NOT IN ('superseded','failed') ORDER BY created_at DESC, rowid DESC LIMIT 1",
                                 (delegation_id,)).fetchone()
            target_message = pause["id"] if pause else None
        conn.execute(
            "INSERT INTO delegation_messages(id, delegation_id, kind, feedback_id, body, requirement_version,"
            " target_block_seq, target_event_id, target_seq, target_message_id, approval_id, cause, status, created_at,"
            " op_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)",
            (mid, delegation_id, kind, feedback_id, body, requirement_version,
             d["block_seq"] if answering else None, target_event, target_seq, target_message,
             approval_id, cause, clock.iso(), op_key))
        if changes_requirement:
            _set(conn, delegation_id, requirement_version=requirement_version)
            # A substantive change voids approvals that have not gone out yet.
            for a in conn.execute("SELECT a.*, m.status AS mstatus FROM approvals a LEFT JOIN delegation_messages m"
                                  " ON m.approval_id=a.id WHERE a.delegation_id=? AND a.consumed_at IS NULL"
                                  " AND a.revoked_at IS NULL", (delegation_id,)).fetchall():
                if a["mstatus"] != "sending":
                    _void_approval(conn, d, a, "requirement_changed")
        if kind == "pause":
            _set(conn, delegation_id, pause_requested=1)
        elif kind == "resume":
            _set(conn, delegation_id, pause_requested=0)
    return mid


def on_task_closed(conn: sqlite3.Connection, task_id: str, status: str, reason: str,
                   feedback_id: str | None) -> list[str]:
    """The user closed the task: accept a delivered delegation (done) and cancel the others."""
    notes = []
    for d in for_task(conn, task_id, active_only=True):
        kind = _closing_message(conn, d, status, reason, feedback_id)
        if kind:
            notes.append(f"{kind}:{d['id']}")
    return notes


def record_approval(conn: sqlite3.Connection, delegation_id: str, delivery_id: str, action: str, detail: str,
                    feedback_id: str | None, op_key: str | None = None) -> str:
    """Record one approval with its own message. ``op_key`` (feedback id + op index) makes a
    retried op return its approval; an identical approval that has not gone out yet is the same
    approval. All approvals of a version go to the worker together (``_send_approvals``), so an
    approval is refused here - not dropped later - once that version's run has been started
    or is being started."""
    with tx(conn):
        if op_key:
            mine = conn.execute("SELECT approval_id FROM delegation_messages WHERE op_key=?", (op_key,)).fetchone()
            if mine and mine["approval_id"]:
                return mine["approval_id"]
        row = conn.execute("SELECT * FROM deliveries WHERE id=? AND delegation_id=?",
                           (delivery_id, delegation_id)).fetchone()
        if row is None:
            raise DelegationError("approval must name a delivery of this delegation")
        latest = _latest_result(conn, delegation_id)
        if latest is None or latest["id"] != delivery_id:
            raise DelegationError("approval must name the latest delivered version")
        d = get(conn, delegation_id)
        if row["requirement_version"] != d["requirement_version"]:
            raise DelegationError("the requirements changed after this version; wait for the next delivery")
        if d["status"] != "review":
            raise DelegationError(f"the delegation is {d['status']}: this version is no longer waiting for review;"
                                  " approve the next version")
        if conn.execute("SELECT 1 FROM delegation_messages WHERE delegation_id=? AND kind='approval'"
                        " AND status='sending'", (delegation_id,)).fetchone():
            raise DelegationError("approvals for this version are being delivered to the worker; approve the next"
                                  " version")
        same = conn.execute("SELECT a.id FROM approvals a JOIN delegation_messages m ON m.approval_id=a.id"
                            " WHERE a.delegation_id=? AND a.delivery_id=? AND a.action=? AND a.detail=?"
                            " AND a.consumed_at IS NULL AND a.revoked_at IS NULL"
                            " AND m.status IN ('pending','waiting_capacity')",
                            (delegation_id, delivery_id, action, detail)).fetchone()
        if same:
            return same["id"]
        aid = ids.new_id("approval")
        conn.execute("INSERT INTO approvals(id, delegation_id, delivery_id, action, detail, feedback_id, granted_at)"
                     " VALUES (?,?,?,?,?,?,?)", (aid, delegation_id, delivery_id, action, detail, feedback_id,
                                                 clock.iso()))
        # Its own message even when one feedback item approves several actions.
        add_message(conn, delegation_id, "approval",
                    f"APPROVED {action} for delivery {delivery_id} (v{row['seq']}). {detail}".strip(), feedback_id,
                    approval_id=aid, op_key=op_key or f"approval:{aid}")
    return aid


def _approval_problem(conn: sqlite3.Connection, d: dict[str, Any], msg: dict[str, Any]) -> str | None:
    """Why an approval message must not go out now (checked at send time, not only when recorded)."""
    a = conn.execute("SELECT * FROM approvals WHERE id=?", (msg["approval_id"],)).fetchone() \
        if msg.get("approval_id") else None
    if a is None:
        return "unbound"
    if a["revoked_at"]:
        return "revoked"
    if a["consumed_at"]:
        return "used"
    latest = _latest_result(conn, d["id"])
    if latest is None or latest["id"] != a["delivery_id"]:
        return "newer_version"
    if latest["requirement_version"] != d["requirement_version"]:
        return "requirement_changed"
    if d["status"] != "review":
        return "not_in_review"
    return None


def _has_marker(snap: dict[str, Any], mid: str) -> bool:
    return any(f"[tm:{mid}]" in (c.get("body") or "") for c in snap.get("comments", []))


AUTHOR = "todo-manager"
# Events after which a card that was in review is no longer waiting there.
LEAVES_REVIEW = ("review_reopened", "completed", "archived", "changes_requested", "claimed")


def _last_event(snap: dict[str, Any]) -> int:
    return max((int(e["id"]) for e in snap.get("events") or []), default=0)


def _comment_event(snap: dict[str, Any], msg: dict[str, Any], length: int) -> int | None:
    """The card event of the comment an earlier attempt posted for ``msg`` (its response was
    lost): the first ``commented`` event by us after the last event seen before posting it. Our
    comments are posted one at a time (kanban-submit lock), so no other one of ours is between."""
    before = msg.get("card_event_before")
    if before is None:
        return None
    for e in sorted(snap.get("events") or [], key=lambda e: int(e["id"])):
        payload = e.get("payload") or {}
        if e["kind"] == "commented" and int(e["id"]) > int(before) and payload.get("author") == AUTHOR \
                and payload.get("len") in (None, length):
            return int(e["id"])
    return None


def _post_comment(conn: sqlite3.Connection, kb: Kanban, d: dict[str, Any], msg: dict[str, Any], text: str,
                  snap: dict[str, Any]) -> int | None:
    """Put ``text`` on the card once (``[tm:<message id>]`` makes a retry find it) and record
    where it landed among the card's events. ``snap``: all events and comments, taken right
    before. Returns the comment's card event id, None when its place cannot be told."""
    mid = msg["id"]
    body = f"{text}\n[tm:{mid}]"
    if _has_marker(snap, mid):
        event_id = _comment_event(snap, msg, len(body))
    else:
        before = _last_event(snap)
        with tx(conn):
            conn.execute("UPDATE delegation_messages SET card_event_before=? WHERE id=?", (before, mid))
        msg["card_event_before"] = before
        result = kb.comment(d["card_id"], body)
        event_id = result.get("event_id") if isinstance(result, dict) else None
    if event_id is not None:
        msg["card_event_id"] = event_id
        _placed(conn, d["id"], mid, int(event_id))
    return event_id


def _parked_in_review(events: list[dict[str, Any]], comment_event: int) -> int | None:
    """The review event of a version that was waiting in review when our comment landed (no run
    could read it), or None when the comment reached a running worker or the next run."""
    reviews = [int(e["id"]) for e in events if e["kind"] == "review_requested" and int(e["id"]) < comment_event]
    if not reviews:
        return None
    review = max(reviews)
    if any(review < int(e["id"]) < comment_event and e["kind"] in LEAVES_REVIEW for e in events):
        return None
    return review


def _hold_back(conn: sqlite3.Connection, cfg: Config, d: dict[str, Any], msg: dict[str, Any],
               attempted: bool) -> str | None:
    """A transition that would start the card again waits while the user paused the delegation
    or no execution slot is free (an attempted one already holds its slot)."""
    if d["pause_requested"] and msg["kind"] != "resume":
        with tx(conn):
            conn.execute("UPDATE delegation_messages SET status='waiting_capacity', error='paused by user' WHERE id=?",
                         (msg["id"],))
        return "waiting"
    if not attempted and capacity(conn, cfg, exclude=d["id"]) <= 0:
        with tx(conn):
            conn.execute("UPDATE delegation_messages SET status='waiting_capacity' WHERE id=?", (msg["id"],))
        return "waiting"
    return None


def _sending(conn: sqlite3.Connection, mid: str) -> None:
    with tx(conn):
        conn.execute("UPDATE delegation_messages SET status='sending' WHERE id=?", (mid,))


def send_messages(conn: sqlite3.Connection, cfg: Config, kb: Kanban) -> dict[str, Any]:
    stats: dict[str, Any] = {"sent": 0, "waiting": 0, "superseded": 0, "failed": 0}
    if held(conn):
        stats["held"] = conn.execute("SELECT COUNT(*) FROM delegation_messages WHERE status IN"
                                     " ('pending','sending','waiting_capacity') AND kind != 'requirement'").fetchone()[0]
        return stats
    # Same lock as submission: capacity reservations and card links are decided one at a time.
    with file_lock(lock_path(cfg.home, "kanban-submit"), blocking=True, timeout=60):
        enforce_task_lifecycle(conn)
        rows = conn.execute("SELECT * FROM delegation_messages WHERE status IN ('pending','sending','waiting_capacity')"
                            " AND kind != 'requirement' ORDER BY created_at").fetchall()
        for msg in rows:
            msg = conn.execute("SELECT * FROM delegation_messages WHERE id=?", (msg["id"],)).fetchone()
            if msg["status"] not in ("pending", "sending", "waiting_capacity"):
                continue  # settled by an earlier message in this round (e.g. a voided approval)
            d = get(conn, msg["delegation_id"])
            try:
                if d["card_id"] is None:
                    if msg["kind"] != "cancel":
                        continue  # folded into the card body at submission
                    outcome = _cancel_unsubmitted(conn, cfg, kb, d, dict(msg))
                elif d["status"] in ("pending_submit", "reconcile"):
                    continue
                else:
                    outcome = _deliver(conn, cfg, kb, d, dict(msg))
            except KanbanError as exc:
                with tx(conn):
                    conn.execute("UPDATE delegation_messages SET status=?, error=? WHERE id=?",
                                 ("sending" if exc.uncertain or msg["status"] == "sending" else "pending",
                                  str(exc)[:500], msg["id"]))
                _failed(stats, "failed", f"{msg['kind']} not delivered: {exc}")
                continue
            stats[outcome] = stats.get(outcome, 0) + 1
    return stats


def _finish(conn: sqlite3.Connection, mid: str, status: str, error: str | None = None) -> None:
    with tx(conn):
        conn.execute("UPDATE delegation_messages SET status=?, sent_at=?, error=? WHERE id=?",
                     (status, clock.iso() if status == "sent" else None, error, mid))


def _unused(conn: sqlite3.Connection, d: dict[str, Any], msg: dict[str, Any], reason: str, **values: Any) -> str:
    """The message could not be applied to the state it was meant for: say so, never drop it silently."""
    _finish(conn, msg["id"], "superseded", reason)
    with tx(conn):
        effects.task_comment(conn, d["task_id"], f"unused:{msg['id']}", i18n.t(
            "dlg.msg_unused", kind=i18n.t(f"dlg.msg.{msg['kind']}"), title=d["title"],
            reason=i18n.t(f"dlg.reason.{reason}", **values), body=(msg["body"] or "")[:300]))
    return "superseded"


def _raced(conn: sqlite3.Connection, cfg: Config, d: dict[str, Any], msg: dict[str, Any]) -> None:
    """The transition was applied, but to a newer question/version that a dashboard action put
    on the card between our check and the call (``transition_raced``): the user is told."""
    text = i18n.t("dlg.raced", kind=i18n.t(f"dlg.msg.{msg['kind']}"), title=d["title"], body=(msg["body"] or "")[:300])
    with tx(conn):
        conn.execute("UPDATE delegation_messages SET error=? WHERE id=?",
                     ("raced: applied to a newer question or version on the card", msg["id"]))
        effects.task_comment(conn, d["task_id"], f"raced:{msg['id']}", text)
        effects.notify(conn, cfg, f"raced:{msg['id']}", text, task_id=d["task_id"])


def _cancel_unsubmitted(conn: sqlite3.Connection, cfg: Config, kb: Kanban, d: dict[str, Any],
                        msg: dict[str, Any]) -> str:
    if d["status"] == "reconcile":
        # A create may have happened with its response lost: look before deciding.
        matches = kb.find(d["idem_key"], f"todo-manager-delegation: {d['id']}")
        if len(matches) > 1:
            return "waiting"  # submit_pending quarantines it; nothing is cancelled blindly
        if matches:
            card = matches[0]
            with tx(conn):
                _set(conn, d["id"], card_id=card["id"], card_status=card["status"],
                     status="cancelled" if card["status"] == "archived" else _status_from_card(card["status"], d))
            return _deliver(conn, cfg, kb, get(conn, d["id"]), msg)
    with tx(conn):
        conn.execute("UPDATE delegation_messages SET status='sent', sent_at=? WHERE id=?", (clock.iso(), msg["id"]))
        _set(conn, d["id"], status="cancelled", cancel_state="confirmed")
        if not for_task_active_other(conn, d):
            _task_state(conn, d["id"], "todo", "me", i18n.t("dlg.cancelled"))
    return "sent"


def _transition_target(conn: sqlite3.Connection, d: dict[str, Any], msg: dict[str, Any]) -> tuple[str, int | None] | None:
    """(Kanban transition, the event it answers) for a message, or None for comment-only."""
    kind, status = msg["kind"], d["status"]
    if kind == "answer" and (msg.get("target_event_id") is not None or msg.get("target_block_seq") is not None):
        return "unblock", msg.get("target_event_id")
    if kind == "resume" and status == "paused" and d.get("pause_event_id") is not None:
        return "unblock", int(d["pause_event_id"])
    if kind == "revision" and msg.get("target_seq"):
        # The version it was written for, whatever our cache says: the card decides.
        return "reopen_review", msg.get("target_event_id")
    return None


def _newer_than_bound(conn: sqlite3.Connection, d: dict[str, Any], msg: dict[str, Any]) -> bool:
    """An accept/revision written for version N while version N+1 has been recorded since.
    (A revision queued before any delivery is a requirement change: the version delivered
    without it is the one it revises.)"""
    if msg["kind"] not in ("revision", "accept") or not msg.get("target_seq"):
        return False
    latest = _latest_result(conn, d["id"])
    return latest is not None and latest["seq"] != msg["target_seq"]


def _pause_anchor(kb: Kanban, d: dict[str, Any], conn: sqlite3.Connection,
                  msg: dict[str, Any]) -> tuple[str, int | None]:
    """The block event our pause message caused: ("found", id), ("none", None) when the pause
    never blocked the card, ("wait", None) while the pause itself has not gone out."""
    pause = conn.execute("SELECT * FROM delegation_messages WHERE id=?", (msg["target_message_id"],)).fetchone()
    if pause is None or pause["status"] in ("superseded", "failed"):
        return "none", None
    if pause["status"] != "sent":
        return "wait", None
    snap = kb.snapshot(d["card_id"], after_event=0)
    tag = f"[tm:{pause['id']}]"
    reason = lambda ev: (ev.get("payload") or {}).get("reason") or ""  # noqa: E731
    for ev in reversed(snap["events"]):
        if ev["kind"] in ("blocked", "block_loop_detected") and tag in reason(ev):
            return "found", int(ev["id"])
    legacy = next((e for e in snap["events"] if d.get("pause_event_id") is not None
                   and int(e["id"]) == int(d["pause_event_id"])), None)
    if legacy is not None and "[tm:" not in reason(legacy):
        return "found", int(legacy["id"])                  # a pause blocked before blocks carried the tag
    return "none", None


def _deliver(conn: sqlite3.Connection, cfg: Config, kb: Kanban, d: dict[str, Any], msg: dict[str, Any]) -> str:
    kind, mid = msg["kind"], msg["id"]
    attempted = msg["status"] == "sending"
    status = d["status"]
    if kind == "approval":
        return _send_approvals(conn, cfg, kb, d, msg)
    if kind == "cancel":
        return _send_cancel(conn, cfg, kb, d, msg)
    if not attempted and _newer_than_bound(conn, d, msg):
        return _unused(conn, d, msg, "newer_version")
    if kind == "accept":
        return _send_accept(conn, cfg, kb, d, msg)
    if kind == "pause":
        if status in EXECUTING:
            snap = kb.snapshot(d["card_id"], after_event=0)
            _sending(conn, mid)
            _post_comment(conn, kb, d, msg, f"{PAUSE_MARK} PAUSE: stop now and take no further actions. {msg['body']}",
                          snap)
            # The tag lets a resume find this block even before a sync has recorded it.
            kb.block(d["card_id"], f"{PAUSE_MARK} [tm:{mid}] {msg['body']}"[:500], None)  # False: card left running/ready
        _finish(conn, mid, "sent")
        with tx(conn):
            _set(conn, d["id"], status="paused" if status in EXECUTING else status)
        return "sent"
    if kind == "revision" and not msg.get("target_seq"):
        return _send_requirement_change(conn, cfg, kb, d, msg)
    if kind == "resume" and msg.get("target_message_id"):
        found, anchor = _pause_anchor(kb, d, conn, msg)
        if found == "wait":
            return "waiting"
        target = ("unblock", anchor) if found == "found" else None
    else:
        target = _transition_target(conn, d, msg)
    if target is None:
        if kind == "resume":
            _finish(conn, mid, "sent")  # nothing of ours to lift (the pause never blocked the card)
            return "sent"
        snap = kb.snapshot(d["card_id"], after_event=0)
        _sending(conn, mid)
        _post_comment(conn, kb, d, msg, f"[{i18n.t('dlg.msg.' + kind)}] {msg['body']}", snap)
        _finish(conn, mid, "sent")
        return "sent"

    action, anchor = target
    snap = kb.snapshot(d["card_id"], after_event=0)
    ours = attempted or _has_marker(snap, mid)
    if anchor is None and kind == "answer" and msg.get("target_block_seq") != d["block_seq"]:
        return _unused(conn, d, msg, "newer_question")  # pre-v2 message without an event binding
    verdict = transition_verdict(snap["task"]["status"], snap["events"], action, anchor)
    if verdict == "already":
        if not ours and kind == "revision":
            # That version was sent back on the board already: the rework is that version's
            # revision, so it gets the comment - unless a newer version came out of it meanwhile.
            newer = any(e["kind"] == "review_requested" and int(e["id"]) > int(anchor) for e in snap["events"])
            if newer or snap["task"]["status"] in ("review", "done", "archived"):
                return _unused(conn, d, msg, "newer_version" if newer else "card_moved", status=snap["task"]["status"])
            _sending(conn, mid)
            _post_comment(conn, kb, d, msg, f"[{i18n.t('dlg.msg.revision')}] {msg['body']}", snap)
            _finish(conn, mid, "sent")
            return "sent"
        if not ours:
            return _unused(conn, d, msg, "answered_elsewhere")
        if kind != "resume" and _has_marker(snap, mid):
            _post_comment(conn, kb, d, msg, f"[{i18n.t('dlg.msg.' + kind)}] {msg['body']}", snap)  # its place
        return _sent_transition(conn, d, msg, reserved=False)
    if verdict == "mismatch":
        return _unused(conn, d, msg, "newer_question" if kind in ("answer", "resume") else "card_moved",
                       status=snap["task"]["status"])
    held_back = _hold_back(conn, cfg, d, msg, attempted)
    if held_back:
        return held_back
    _sending(conn, mid)
    if kind != "resume":
        _post_comment(conn, kb, d, msg, f"[{i18n.t('dlg.msg.' + kind)}] {msg['body']}", snap)
    return _apply_transition(conn, cfg, kb, d, msg, action, anchor)


def _apply_transition(conn: sqlite3.Connection, cfg: Config, kb: Kanban, d: dict[str, Any], msg: dict[str, Any],
                      action: str, anchor: int | None) -> str:
    kind = msg["kind"]
    result = kb.transition(d["card_id"], action, anchor)
    if result["outcome"] == "mismatch":
        return _unused(conn, d, msg, "newer_question" if kind in ("answer", "resume") else "card_moved",
                       status=result["status"])
    sent = _sent_transition(conn, d, msg, reserved=result["outcome"] in ("done", "raced"))
    if result["outcome"] == "raced":
        _raced(conn, cfg, d, msg)
    return sent


def _send_requirement_change(conn: sqlite3.Connection, cfg: Config, kb: Kanban, d: dict[str, Any],
                             msg: dict[str, Any]) -> str:
    """A revision not bound to a delivered version (written while the work was running, or from
    before versions were bound): a change of the requirements for the current execution. Our
    cache may be behind the card, so the card itself decides: the comment goes on the card (a
    running worker has it steered in, the next run reads it); when the card was waiting in
    review when it landed - before our check, or because the worker submitted while we posted -
    that version was built without it and its review is reopened."""
    mid, attempted = msg["id"], msg["status"] == "sending"
    snap = kb.snapshot(d["card_id"], after_event=0)
    status = snap["task"]["status"]
    posted = _has_marker(snap, mid)
    if status in ("done", "archived") and not posted:
        return _unused(conn, d, msg, "card_moved", status=status)
    checked = status == "review" and not posted
    if checked:
        held_back = _hold_back(conn, cfg, d, msg, attempted)   # the reopen below would need a slot
        if held_back:
            return held_back
    _sending(conn, mid)
    event_id = _post_comment(conn, kb, d, msg, f"[{i18n.t('dlg.msg.revision')}] {msg['body']}", snap)
    if not posted:
        snap = kb.snapshot(d["card_id"], after_event=0)     # what happened while we posted
    if event_id is not None:
        parked = _parked_in_review(snap["events"], int(event_id))
    else:
        # Place unknown (an attempt from before positions were kept): assume the version in
        # review has not seen it - at worst one extra run, never a version taken as revised.
        parked = max((int(e["id"]) for e in snap["events"] if e["kind"] == "review_requested"), default=None) \
            if snap["task"]["status"] == "review" else None
    if parked is None:
        _finish(conn, mid, "sent")
        return "sent"
    if not checked:
        held_back = _hold_back(conn, cfg, d, msg, attempted)
        if held_back:
            return held_back
    return _apply_transition(conn, cfg, kb, d, msg, "reopen_review", parked)


def _refuse_approval(conn: sqlite3.Connection, d: dict[str, Any], msg: dict[str, Any], problem: str) -> None:
    a = conn.execute("SELECT * FROM approvals WHERE id=?", (msg["approval_id"],)).fetchone() \
        if msg.get("approval_id") else None
    with tx(conn):
        if a is not None and problem != "used":
            _void_approval(conn, d, a, problem)
        conn.execute("UPDATE delegation_messages SET status='superseded', error=? WHERE id=?",
                     (f"approval void: {problem}", msg["id"]))


def _send_approvals(conn: sqlite3.Connection, cfg: Config, kb: Kanban, d: dict[str, Any],
                    msg: dict[str, Any]) -> str:
    """All valid approvals for the version ``msg`` approves go to the worker together: a comment
    each, then one reopen of that version's review. One reopen per approval would start a run
    with only the first, and leave the others no version in review to go to. A retry (some in
    ``sending``) asks the card first: once the review was reopened, the approvals that were
    posted before it are sent; one that was not is void (that run started without it)."""
    a = conn.execute("SELECT * FROM approvals WHERE id=?", (msg["approval_id"],)).fetchone() \
        if msg.get("approval_id") else None
    if a is None:
        _refuse_approval(conn, d, msg, "unbound")
        return "superseded"
    group = []
    for m in conn.execute("SELECT m.* FROM delegation_messages m JOIN approvals a ON a.id=m.approval_id"
                          " WHERE m.delegation_id=? AND m.kind='approval' AND a.delivery_id=?"
                          " AND m.status IN ('pending','sending','waiting_capacity') ORDER BY m.created_at, m.rowid",
                          (d["id"], a["delivery_id"])).fetchall():
        m = dict(m)
        problem = None if m["status"] == "sending" else _approval_problem(conn, d, m)
        if problem:
            _refuse_approval(conn, d, m, problem)
        else:
            group.append(m)
    if not group:
        return "superseded"
    anchor = conn.execute("SELECT source_event_id FROM deliveries WHERE id=?", (a["delivery_id"],)).fetchone()[0]
    attempted = any(m["status"] == "sending" for m in group)
    snap = kb.snapshot(d["card_id"], after_event=0)
    verdict = transition_verdict(snap["task"]["status"], snap["events"], "reopen_review", anchor)
    if verdict == "already":
        for m in group:
            if m["status"] == "sending" or _has_marker(snap, m["id"]):
                if _has_marker(snap, m["id"]):                    # records its place; nothing is posted
                    _post_comment(conn, kb, d, m, f"[{i18n.t('dlg.msg.approval')}] {m['body']}", snap)
                _sent_transition(conn, d, m, reserved=False)
            else:
                _refuse_approval(conn, d, m, "sent_back_without_it")
        return "sent"
    if verdict == "mismatch":
        newer = any(e["kind"] == "review_requested" and int(e["id"]) > int(anchor) for e in snap["events"])
        for m in group:
            _refuse_approval(conn, d, m, "newer_version" if newer else "not_in_review")
        return "superseded"
    if d["pause_requested"] or (not attempted and capacity(conn, cfg, exclude=d["id"]) <= 0):
        with tx(conn):
            for m in group:
                conn.execute("UPDATE delegation_messages SET status='waiting_capacity', error=? WHERE id=?",
                             ("paused by user" if d["pause_requested"] else None, m["id"]))
        return "waiting"
    for m in group:
        _sending(conn, m["id"])
    for m in group:
        _post_comment(conn, kb, d, m, f"[{i18n.t('dlg.msg.approval')}] {m['body']}", snap)
        snap = kb.snapshot(d["card_id"], after_event=0)   # the next one is placed after this one
    result = kb.transition(d["card_id"], "reopen_review", anchor)
    if result["outcome"] == "mismatch":
        for m in group:
            _refuse_approval(conn, d, m, "not_in_review")
        return "superseded"
    for n, m in enumerate(group):
        _sent_transition(conn, d, m, reserved=n == 0 and result["outcome"] in ("done", "raced"))
    if result["outcome"] == "raced":
        _raced(conn, cfg, d, msg)
    return "sent"


def _sent_transition(conn: sqlite3.Connection, d: dict[str, Any], msg: dict[str, Any], reserved: bool) -> str:
    with tx(conn):
        conn.execute("UPDATE delegation_messages SET status='sent', sent_at=?, error=NULL WHERE id=?",
                     (clock.iso(), msg["id"]))
        if msg["kind"] == "approval" and msg.get("approval_id"):
            conn.execute("UPDATE approvals SET consumed_at=COALESCE(consumed_at, ?) WHERE id=?",
                         (clock.iso(), msg["approval_id"]))
        if reserved:
            # The card is back in the pool: count it against max_running until the next sync.
            _set(conn, d["id"], status="queued")
    return "sent"


def _send_cancel(conn: sqlite3.Connection, cfg: Config, kb: Kanban, d: dict[str, Any], msg: dict[str, Any]) -> str:
    with tx(conn):
        conn.execute("UPDATE delegation_messages SET status='sending' WHERE id=?", (msg["id"],))
    result = kb.transition(d["card_id"], "archive")
    termination = result.get("termination")
    confirmed = termination is None or bool(termination.get("terminated"))
    # Whatever the card did before the archive is kept as history; it changes nothing now.
    late = {"deliveries": 0, "questions": 0, "failures": 0}
    try:
        snap = kb.snapshot(d["card_id"], after_event=d["event_cursor"])
    except KanbanError:
        snap = None
    with tx(conn):
        if snap is not None:
            _apply_snapshot(conn, cfg, d, snap, late, quiet=True)
        conn.execute("UPDATE delegation_messages SET status='sent', sent_at=?, error=NULL WHERE id=?",
                     (clock.iso(), msg["id"]))
        _set(conn, d["id"], status="cancelled", card_status=result["status"],
             cancel_state="confirmed" if confirmed else "unconfirmed")
        if not confirmed:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(termination.items()) if v not in (None, False))
            text = i18n.t("dlg.cancel_unconfirmed", title=d["title"], detail=detail or "-")
            effects.task_comment(conn, d["task_id"], f"cancel-unconfirmed:{d['id']}", text)
            effects.notify(conn, cfg, f"cancel-unconfirmed:{d['id']}", text, task_id=d["task_id"])
        if not for_task_active_other(conn, d):
            _task_state(conn, d["id"], "todo", "me", i18n.t("dlg.cancelled"))
    return "sent"


def _accept_anchor(conn: sqlite3.Connection, d: dict[str, Any], msg: dict[str, Any]) -> int | None:
    """The review event of the version an accept was given for. ``target_seq`` > 0: bound when
    queued. NULL: queued before versions were bound - the latest version received before it
    was queued, never a later one (a version received in the same instant is ambiguous and not
    used). 0: stored without a version (older code) - nothing to accept."""
    seq = msg.get("target_seq")
    if seq:
        return msg.get("target_event_id")
    if seq is None:
        row = conn.execute("SELECT source_event_id FROM deliveries WHERE delegation_id=? AND kind='result'"
                           " AND after_cancel=0 AND received_at < ? ORDER BY seq DESC LIMIT 1",
                           (d["id"], msg["created_at"])).fetchone()
        tie = conn.execute("SELECT 1 FROM deliveries WHERE delegation_id=? AND kind='result' AND after_cancel=0"
                           " AND received_at = ?", (d["id"], msg["created_at"])).fetchone()
        return row["source_event_id"] if row and not tie else None
    return None


def _send_accept(conn: sqlite3.Connection, cfg: Config, kb: Kanban, d: dict[str, Any], msg: dict[str, Any]) -> str:
    """Complete the review card of the accepted version. A retry (``sending``: the outcome of
    the earlier call is unknown) asks the card first, through the same bound check: done by
    our earlier call -> sent, even when a sync already recorded the card as done. Only a new
    accept is refused once the delegation left review."""
    attempted = msg["status"] == "sending"
    anchor = _accept_anchor(conn, d, msg)
    if anchor is None:
        return _unused(conn, d, msg, "nothing_to_accept")
    if not attempted and d["status"] != "review":
        _finish(conn, msg["id"], "failed", f"cannot accept while delegation is {d['status']}")
        return "failed"
    _sending(conn, msg["id"])
    result = kb.transition(d["card_id"], "complete", anchor, summary=f"accepted by user: {msg['body']}"[:500])
    if result["outcome"] == "mismatch":
        return _unused(conn, d, msg, "card_moved", status=result["status"])
    with tx(conn):
        conn.execute("UPDATE delegation_messages SET status='sent', sent_at=?, error=NULL WHERE id=?",
                     (clock.iso(), msg["id"]))
        _set(conn, d["id"], status="ended", card_status=result["status"])
        if not for_task_active_other(conn, d):
            _task_state(conn, d["id"], "todo", "me", i18n.t("dlg.accepted"))
    if result["outcome"] == "raced":
        _raced(conn, cfg, d, msg)
    return "sent"


def summary_line(d: dict[str, Any]) -> str:
    return f"{d['id']} [{d['status']}] {d['title']} card={d.get('card_id') or '-'}"


def to_json(d: dict[str, Any]) -> str:
    return json.dumps(d, ensure_ascii=False, default=str)
