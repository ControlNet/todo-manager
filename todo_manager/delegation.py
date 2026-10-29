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
- Do only this delegation: do not widen the scope or handle unrelated matters. If you split work natively, record creator lineage, copy essential authorization and this protocol into children, and integrate their partial results here. Child external actions require separate explicit approval; do not transfer a root approval to a child.
- Allowed actions: {allowed}. An external action not listed here (sending email, public comments, push, opening/merging PRs, closing issues, publishing, editing shared documents) needs the user's approval first: ask with kanban_block(kind="needs_input", reason="<exactly what you would do, in {language}>") and wait. An approval is a comment by `todo-manager` saying `APPROVED <action> for question #<n>` (your question) or `APPROVED <action> for delivery <id>` (a version you delivered); it allows that action once, as described, and any later change of the requirements voids it.
- Need information or a decision from the user? kanban_block(kind="needs_input", reason="<one concrete question in {language}>"). Do not guess.
- Finished? kanban_complete(summary="<2-3 sentences in {language}>", metadata={{"deliverable_markdown": "<the full deliverable in Markdown>", "external_actions_done": [...], "notes": "..."}}, artifacts=[<absolute paths of files to keep>]). Native review is an execution review stage, not user acceptance. For human input block with a concrete question. Either way the user sees it in Notion.
- The Notion task and its status are the user's: do not change them. Your delivery does not complete the task.
- The task and this brief describe the state when the card was made. Before you write that something is still unsent, unapproved or needed, look for newer material (`todo-manager source search "<words>" [--mine]`); if there is none, say up to when your statement holds.
- Comments by `todo-manager` are the user's supplements, answers, revision requests or approvals: follow them. A comment starting with {pause} means stop immediately and take no further action.
- An external action whose outcome you cannot confirm (timeout, crash, unclear reply) must NOT be repeated: report it with kanban_block(kind="needs_input") and let the user check. The same applies when an earlier run of this card ended without a result after an APPROVED comment: assume the approved action may have happened and ask instead of doing it again.
- Scratch workspaces are deleted when the card ends: keep needed files in artifacts.
"""


class DelegationError(ValueError):
    pass


def card_retries(cfg: Config, d: dict[str, Any]) -> int | None:
    """Hermes' per-card ``max_retries``: 1 for a card allowed an external action (a respawn could
    repeat an action whose outcome is unknown), else ``kanban.max_retries`` (0: Hermes' default)."""
    if external_actions(d["allowed_actions"]):
        return 1
    return int(cfg.get("kanban.max_retries", 1)) or None


def action_names(allowed: list[str] | None) -> set[str]:
    """Action names of an allowed-actions list; ``send_email:bob@x.example`` is ``send_email``.
    The one parser for validation, external-action checks and approvals."""
    return {str(a).split(":", 1)[0].strip() for a in allowed or [] if str(a).strip()}


def external_actions(allowed: list[str] | None) -> set[str]:
    return action_names(allowed) & EXTERNAL_ACTIONS


def get(conn: sqlite3.Connection, delegation_id: str) -> dict[str, Any] | None:
    row = row_dict(conn.execute("SELECT * FROM delegations WHERE id=?", (delegation_id,)).fetchone())
    if row:
        row["allowed_actions"] = loads(row["allowed_actions"], [])
    return row


def unfinished_sql(alias: str = 'delegations') -> str:
    """Business ownership remains live until descendant discovery and stopping finish."""
    return (f"({alias}.status IN ({','.join(repr(s) for s in ACTIVE)}) OR "
            f"({alias}.status IN ('ended','cancelled') AND ({alias}.children_sync_pending=1 OR EXISTS "
            f"(SELECT 1 FROM delegation_children c WHERE c.delegation_id={alias}.id "
            "AND c.status NOT IN ('done','archived')))) OR "
            f"{alias}.cancel_state='unconfirmed')")


def for_task(conn: sqlite3.Connection, task_id: str, active_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM delegations WHERE task_id=?"
    if active_only:
        sql += " AND " + unfinished_sql()
    rows = conn.execute(sql + " ORDER BY created_at", (task_id,)).fetchall()
    return [{**dict(r), "allowed_actions": loads(r["allowed_actions"], [])} for r in rows]


CARD_NOTES_CHARS = 6000


def card_body(d: dict[str, Any], task: dict[str, Any], notion_url: str | None, extra: list[str],
              notes: str | None = None) -> str:
    """``notes``: the user's own notes on the Task page (context, as written when the card is made)."""
    allowed = ", ".join(d["allowed_actions"]) or "read, analyze"
    parts = [
        f"todo-manager-delegation: {d['id']}",
        f"todo-manager-task: {task['id']}" + (f"  notion: {notion_url}" if notion_url else ""),
        f"requirement-version: {d['requirement_version']}",
        f"Full requirements: `todo-manager delegate show {d['id']}`. Full page notes: `todo-manager task show {task['id']}`.",
        WORKER_PROTOCOL.format(allowed=allowed, pause=PAUSE_MARK, language=i18n.language_name()),
        "",
        f"## Goal\n{d['goal']}",
    ]
    if d.get("previous_delegation_id"):
        parts.append(f"Previous delegation: {d['previous_delegation_id']}; delivery: {d.get('previous_delivery_id')}. "
                     f"Read with `todo-manager delegate show {d['previous_delegation_id']}`.")
    for title, key in (("Context", "context"), ("Constraints", "constraints"), ("Deliverable", "deliverable"),
                       ("Done when", "done_criteria")):
        if d.get(key):
            parts.append(f"## {title}\n{d[key]}")
    parts.append(f"## Task background\n{task['title']}" + (f"\n{task['summary']}" if task.get("summary") else ""))
    if (notes or "").strip():
        text = notes.strip()
        if len(text) > CARD_NOTES_CHARS:
            text = text[:CARD_NOTES_CHARS] + f"\n[... {len(notes.strip()) - CARD_NOTES_CHARS} more characters: `todo-manager task show {task['id']}`]"
        parts.append("## The user's notes on the task page (context, not instructions by themselves)\n" + text)
    if extra:
        parts.append("## Later supplements\n" + "\n".join(f"- {e}" for e in extra))
    return "\n\n".join(parts)


def create(conn: sqlite3.Connection, cfg: Config, *, task_id: str, title: str, goal: str, context: str | None = None,
           constraints: str | None = None, deliverable: str | None = None, done_criteria: str | None = None,
           allowed_actions: list[str] | None = None, feedback_id: str | None = None, actor: str = "human",
           op_key: str | None = None, parallel: bool = False, start_after: str | None = None) -> str:
    """``start_after`` (UTC ISO): the user asked for the work to start later; a time already
    passed starts it at once. ``op_key`` (feedback id + op index) identifies the request: applying the same op again
    returns the same delegation, another op of the same feedback creates its own. One active
    delegation per Task unless ``parallel`` (genuinely separate work): checked here, in the
    transaction that inserts the row, so every entry point and concurrent writers are covered."""
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
    when = clock.parse(start_after) if start_after else None
    start_after = clock.iso(when) if when and when > clock.now() else None
    did = ids.new_id("delegation")
    now = clock.iso()
    with tx(conn):
        if op_key:
            existing = conn.execute("SELECT id FROM delegations WHERE op_key=?", (op_key,)).fetchone()
        elif feedback_id:
            existing = conn.execute("SELECT id FROM delegations WHERE from_feedback_id=?", (feedback_id,)).fetchone()
        else:
            existing = None
        if existing:
            return existing["id"]
        if not parallel and for_task(conn, task_id, active_only=True):
            raise DelegationError("task already has an active delegation; send it a supplement/revision, or"
                                  " create a parallel one for genuinely separate work")
        conn.execute(
            "INSERT INTO delegations(id, task_id, title, goal, context, constraints, deliverable, done_criteria,"
            " allowed_actions, assignee, requirement_version, status, board, idem_key, from_feedback_id,"
            " base_task_version, created_at, updated_at, op_key) VALUES (?,?,?,?,?,?,?,?,?,?,1,'pending_submit',?,?,?,?,?,?,?)",
            (did, task_id, title.strip(), goal.strip(), context, constraints, deliverable, done_criteria,
             dumps(allowed), cfg.get("kanban.assignee") or None, cfg.get("kanban.board", "todo-manager"),
             ids.delegation_idempotency_key(did), feedback_id, task["version"], now, now, op_key))
        conn.execute("UPDATE delegations SET start_after=? WHERE id=?", (start_after, did))
        try:
            route = notify_route(cfg)
        except KanbanError as exc:
            raise DelegationError(str(exc)) from exc
        conn.execute("UPDATE delegations SET notification_route=? WHERE id=?", (dumps(route), did))
        previous = conn.execute("SELECT id FROM delegations WHERE task_id=? AND id!=? AND status='ended' "
                                "ORDER BY created_at DESC,rowid DESC LIMIT 1", (task_id, did)).fetchone()
        if previous:
            delivery = conn.execute("SELECT id FROM deliveries WHERE delegation_id=? AND kind='result' "
                                    "ORDER BY received_at DESC,rowid DESC LIMIT 1", (previous["id"],)).fetchone()
            conn.execute("UPDATE delegations SET previous_delegation_id=?,previous_delivery_id=? WHERE id=?",
                         (previous["id"], delivery["id"] if delivery else None, did))
        conn.execute(
            "INSERT INTO delegation_messages(id, delegation_id, kind, feedback_id, body, requirement_version, status,"
            " created_at, sent_at) VALUES (?,?,?,?,?,1,'sent',?,?)",
            (ids.new_id("event"), did, "requirement", feedback_id, goal.strip(), now, now))
        note = (i18n.t("dlg.assigned_later", title=title.strip(), when=_local_time(cfg, start_after)) if start_after
                else i18n.t("dlg.assigned", title=title.strip()))
        _next_actor(conn, did, "agent", note, actor=actor, cause=feedback_id or did, initial=True)
        if start_after:
            _task_note(conn, task_id, note, feedback_id or did)   # also when the next actor did not change
        # A next actor the user sets AFTER this point is never changed by agent work.
        conn.execute("UPDATE delegations SET base_task_version=(SELECT version FROM tasks WHERE id=?) WHERE id=?",
                     (task_id, did))
    return did


def _next_actor(conn: sqlite3.Connection, delegation_id: str, to: str, note: str | None,
                actor: str = "program", cause: str | None = None, initial: bool = False) -> None:
    """The one link from agent work to the personal Task: its next actor. Delegating makes the
    agent the next actor (``initial``); when the work waits for the user (question, delivery,
    failure) or stops, it goes back to ``me``, and to ``agent`` again when the work resumes -
    unless the user set the next actor after the delegation started. The Task's status is the
    user's and is never changed by agent work."""
    d = conn.execute("SELECT task_id, base_task_version FROM delegations WHERE id=?", (delegation_id,)).fetchone()
    task = store.get_task(conn, d["task_id"])
    if task is None or task["status"] in store.CLOSED_STATUSES or task["next_actor"] == to:
        return
    if store.normalize_state(task["status"], to)[1] != to:
        return          # the Task's status keeps its own next actor (waiting, review, decision)
    if not initial:
        meta = loads(task["field_meta"], {}).get("next_actor") or {}
        if meta.get("by") == "human" and int(meta.get("v", 0)) > int(d["base_task_version"] or 0):
            return
    changes: dict[str, Any] = {"next_actor": to}
    if note:
        changes["change_note"] = note
    result = store.update_task(conn, task["id"], changes, actor=actor, origin="kanban", cause_ref=cause or delegation_id,
                               based_on_version=task["version"], override_human=True)
    if not result.applied:
        return
    if "change_note" in result.applied:
        conn.execute("UPDATE tasks SET change_note_at=? WHERE id=?", (clock.iso(), task["id"]))
    effects.task_push(conn, task["id"])


def _task_note(conn: sqlite3.Connection, task_id: str, note: str, cause: str) -> None:
    task = store.get_task(conn, task_id)
    if task is None or task["status"] in store.CLOSED_STATUSES:
        return
    result = store.update_task(conn, task_id, {"change_note": note}, actor="program", origin="kanban", cause_ref=cause,
                               based_on_version=task["version"], override_human=True)
    if result.applied:
        conn.execute("UPDATE tasks SET change_note_at=? WHERE id=?", (clock.iso(), task_id))
        effects.task_push(conn, task_id)


def _local_time(cfg: Config, value: str) -> str:
    return clock.local_dt(cfg.tz, clock.parse(value)).strftime("%m-%d %H:%M")


def waiting_start(d: dict[str, Any]) -> bool:
    """The user asked for this work to start later, and that time has not come."""
    when = clock.parse(d.get("start_after"))
    return when is not None and clock.now() < when


def _set(conn: sqlite3.Connection, delegation_id: str, **fields: Any) -> None:
    fields["updated_at"] = clock.iso()
    assignments = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE delegations SET {assignments}, version=version+1 WHERE id=?", (*fields.values(), delegation_id))


# --------------------------------------------------------------------------- lifecycle

def _closing_message(conn: sqlite3.Connection, d: dict[str, Any], task_status: str, reason: str,
                     feedback_id: str | None) -> str | None:
    """A closed Task stops its agent work: one cancel per active delegation. Closing never
    accepts a delivery. An accept the user gave explicitly before closing a Task as done is
    still carried out instead; a cancelled Task is cancelled even then (the cancel voids the
    accept when it is processed, or finds it already carried out)."""
    closing = {r["kind"] for r in conn.execute(
        "SELECT kind FROM delegation_messages WHERE delegation_id=? AND kind IN ('accept','cancel')"
        " AND status IN ('pending','sending','sent','waiting_capacity')", (d["id"],))}
    if "cancel" in closing and d.get('cancel_state') == 'unconfirmed':
        conn.execute("UPDATE delegation_messages SET status='pending' WHERE delegation_id=? AND kind='cancel' AND status='sent'", (d['id'],))
        return 'cancel'
    if "cancel" in closing or ("accept" in closing and task_status != "cancelled"):
        return None
    add_message(conn, d["id"], "cancel", reason, feedback_id=feedback_id, cause="task_closed")
    return "cancel"


def enforce_task_lifecycle(conn: sqlite3.Connection) -> dict[str, int]:
    """Shared by every entry point: runs before any card is touched in a cycle."""
    stats = {"closing": 0, "withdrawn": 0}
    rows = conn.execute(
        f"SELECT d.id, t.status AS task_status FROM delegations d JOIN tasks t ON t.id=d.task_id"
        f" WHERE {unfinished_sql('d')}").fetchall()
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


# Global Hermes switches that change how Hermes treats our cards. They are the user's Hermes
# choices: todo-manager reads them (at most once a day), shows what they mean in ``health`` and
# never changes them or holds cards back because of them.
SWITCH_NOTES = {
    "review_dispatch": "kanban.review_dispatch is on: Hermes' review worker reviews delivered agent work; "
                       "review is execution review, not user acceptance. Done revisions use a linked follow-up.",
    "auto_decompose": "kanban.auto_decompose is on: native triage may create children. Todo Manager bridges "
                      "proven child questions and partial results, but only the root integrates the aggregate result.",
}
SETTINGS_KEY = "kanban.settings"
SETTINGS_CHECK_HOURS = 24


def observe_settings(conn: sqlite3.Connection, kb: Kanban, force: bool = False) -> dict[str, Any] | None:
    """Read the Hermes switches (unset means on, as the installed dispatcher reads them) and keep
    what they mean for ``health``. Returns None when the last reading is recent enough."""
    last = loads(get_meta(conn, SETTINGS_KEY), None) or {}
    age = clock.minutes_since(last.get("at")) if last.get("at") else None
    if not force and age is not None and age < SETTINGS_CHECK_HOURS * 60:
        return None
    values = kb.settings()
    notes = [text for key, text in SWITCH_NOTES.items() if bool(values.get(key, True))]
    for name, enabled in zip(("MEMORY.md", "USER.md"), values.get("memory_flags", (True, True))):
        if not enabled:
            notes.append(f"Native {name} memory is disabled in the board host profile; no preference loader substitutes it")
    record = {"values": {k: bool(values.get(k, True)) for k in SWITCH_NOTES}, "notes": notes, "at": clock.iso()}
    with tx(conn):
        set_meta(conn, SETTINGS_KEY, dumps(record))
        conn.execute("DELETE FROM meta WHERE key='kanban.settings_problems'")   # kept by earlier versions
    return record


def settings_notes(conn: sqlite3.Connection) -> list[str]:
    return (loads(get_meta(conn, SETTINGS_KEY), None) or {}).get("notes", [])


def _failed(stats: dict[str, Any], counter: str, error: str) -> None:
    """Count a failed Kanban call; the first error of the pass is kept for channel health."""
    stats[counter] += 1
    stats.setdefault("error", error[:500])


def notify_route(cfg: Config) -> dict | None:
    route = {"platform": cfg.get("kanban.notify_platform"), "chat_id": cfg.get("kanban.notify_chat_id"),
             "thread_id": cfg.get("kanban.notify_thread_id") or None,
             "notifier_profile": cfg.get("kanban.notify_profile")}
    if not any(route.values()):
        return None
    if not all(route[k] for k in ("platform", "chat_id", "notifier_profile")):
        raise KanbanError("notification route requires platform, chat_id and notifier_profile")
    if route["platform"] == "api_server":
        raise KanbanError("api_server requires wake delivery; passive notification route is unsupported")
    return route


def route_for(conn: sqlite3.Connection, cfg: Config, d: dict) -> dict | None:
    """Pin the explicit route once; old delegations adopt config on their first upgraded sync."""
    raw = d.get("notification_route")
    if raw is not None:
        return loads(raw, None)
    route = notify_route(cfg)
    with tx(conn):
        conn.execute("UPDATE delegations SET notification_route=? WHERE id=? AND notification_route IS NULL",
                     (dumps(route), d["id"]))
    return route


def may_release_staging(conn: sqlite3.Connection, d: dict[str, Any]) -> bool:
    """Every recovery release obeys the same local execution barriers."""
    current = get(conn, d['id'])
    return _may_run(conn, current) and not current.get('start_after')


def _may_run(conn: sqlite3.Connection, d: dict[str, Any]) -> bool:
    return not held(conn) and not d['pause_requested'] and not cancel_pending(conn, d)


def repair_notification(conn: sqlite3.Connection, cfg: Config, kb: Kanban,
                        d: dict[str, Any], card: str, release: bool = False) -> dict | None:
    """Notification health is independent from business event ingestion."""
    from . import health
    scope = f'notifications:{card}'
    try:
        route = route_for(conn, cfg, d)
        if not route:
            return None
        result = kb.notify(card, route, release=release and may_release_staging(conn, d))
    except KanbanError as exc:
        prior = conn.execute("SELECT consecutive_failures FROM cursors WHERE source='kanban' AND scope=?", (scope,)).fetchone()
        exhausted = prior and prior['consecutive_failures'] >= 11
        prefix = 'notification registration exhausted; manual repair required' if exhausted else 'notification registration failed'
        health.record_channel(conn, 'kanban', scope, f'{prefix}: {exc}')
        return None
    health.record_channel(conn, 'kanban', scope, None)
    if result['history_gap']:
        with tx(conn):
            conn.execute("UPDATE cursors SET meta=json_set(COALESCE(meta,'{}'), '$.history_gap', ?) "
                         "WHERE source='kanban' AND scope=?", (clock.iso(), scope))
    return result


def submit_pending(conn: sqlite3.Connection, cfg: Config, kb: Kanban, only: str | None = None) -> dict[str, Any]:
    """Create the cards of new delegations (``only``: just that one, right after it was created)."""
    stats: dict[str, Any] = {"submitted": 0, "linked": 0, "errors": 0, "quarantined": 0, "held": 0}
    if held(conn):
        stats["held"] = conn.execute("SELECT COUNT(*) FROM delegations WHERE status IN ('pending_submit','reconcile')"
                                     ).fetchone()[0]
        return stats
    with file_lock(lock_path(cfg.home, "kanban-submit"), blocking=True, timeout=60):
        # A pause holds back a create, never the read-only lookup of one that may have happened.
        rows = conn.execute("SELECT id FROM delegations WHERE (status='reconcile'"
                            " OR (status='pending_submit' AND pause_requested=0)) AND (? IS NULL OR id=?)"
                            " ORDER BY created_at", (only, only)).fetchall()
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
                info = repair_notification(conn, cfg, kb, d, card['id'], release=True)
                if info:
                    card['status'] = info['status']
                if d["start_after"] and card["status"] == "blocked":
                    try:   # left staged by an interrupted create: park it (start_due releases it)
                        card["status"] = kb.schedule(card["id"], d["start_after"])
                    except KanbanError as exc:
                        _failed(stats, "errors", f"park failed: {exc}")
                with tx(conn):
                    # Our pause has not reached this card yet: a block on it is the worker's.
                    status = "cancelled" if card["status"] == "archived" else _status_from_card(card["status"], d)
                    _set(conn, d["id"], card_id=card["id"], card_status=card["status"], status=status, last_error=None)
                stats["linked"] += 1
                if d["pause_requested"]:
                    _pause_linked(conn, cfg, kb, d["id"], stats)
                continue
            if task is None or task["status"] in store.CLOSED_STATUSES or closing or d["pause_requested"]:
                # An uncertain create that did not happen, for a Task closed or paused meanwhile.
                with tx(conn):
                    _set(conn, d["id"], status="pending_submit", last_error=None)
                stats["held"] += 1
                continue
            # Lookup above must succeed with no match before repairing an unsubmitted assignee.
            # A remotely accepted create is linked unchanged, including an uncertain response.
            candidate = cfg.get('kanban.assignee') or None
            try:
                if not kb.profile_valid(d.get('assignee')):
                    if not kb.profile_valid(candidate):
                        raise KanbanError('configure a valid kanban.assignee profile before submitting', code='NO_PROFILE')
                    with tx(conn):
                        old = d.get('assignee')
                        _set(conn, d['id'], assignee=candidate)
                        effects.task_comment(conn, d['task_id'], f"profile-repair:{d['id']}:{d['version']}",
                                             f"Unsubmitted delegation profile repaired: {old!r} -> {candidate!r}; remote lookup found no card.")
                    d = get(conn, d['id'])
            except KanbanError as exc:
                with tx(conn):
                    _set(conn, d['id'], status='pending_submit', last_error=f'profile validation failed: {exc}')
                _failed(stats, 'errors', str(exc))
                continue
            later = waiting_start(d)
            with tx(conn):
                # Read with the check, in one transaction: a message queued from here on
                # (add_message does not take the kanban-submit lock) is not in the card and is
                # sent once the card is linked. Not paused here: a pause/resume read is a spent pair.
                folded = conn.execute("SELECT id, kind, body FROM delegation_messages WHERE delegation_id=?"
                                      " AND kind IN ('supplement','pause','resume') AND status='pending'"
                                      " ORDER BY created_at", (d["id"],)).fetchall() \
                    if _may_run(conn, get(conn, d['id'])) else None
                if folded is not None:
                    _set(conn, d["id"], status="reconcile", submit_attempts=d["submit_attempts"] + 1,
                         start_after=d["start_after"] if later else None)
            if folded is None:
                stats['held'] += 1
                continue
            notes = conn.execute("SELECT notes_text FROM notion_pages WHERE entity_type='task' AND entity_id=?",
                                 (task["id"],)).fetchone()
            body = card_body(d, task, _notion_url(conn, task), [m["body"] for m in folded if m["kind"] == "supplement"],
                             notes["notes_text"] if notes else None)
            try:
                card_id = kb.create(title=f"{d['title']} [{d['id']}]", body=body, idem_key=d["idem_key"],
                                    assignee=d.get("assignee"), tenant="todo-manager",
                                    workspace=cfg.get("kanban.workspace") or None,
                                    max_retries=card_retries(cfg, d), notify=route_for(conn, cfg, d),
                                    skills=cfg.get("kanban.skills") or None,
                                    start_after=d["start_after"] if later else None)
            except KanbanError as exc:
                with tx(conn):
                    _set(conn, d["id"], status="reconcile" if exc.uncertain else "pending_submit",
                         last_error=f"create failed: {exc}")
                _failed(stats, "errors", f"create failed: {exc}")
                continue
            with tx(conn):
                _set(conn, d["id"], card_id=card_id, card_status="scheduled" if later else "ready", status="queued",
                     last_error=None)
                # Supplements are in the card body; a pause/resume pair before submission is spent.
                # Position 0: in the card body, read by every run of the card.
                conn.executemany("UPDATE delegation_messages SET status='sent', sent_at=?, card_event_id=0 WHERE"
                                 " id=? AND status='pending'", [(clock.iso(), m["id"]) for m in folded])
            stats["submitted"] += 1
    return stats


def start_due(conn: sqlite3.Connection, cfg: Config, kb: Kanban) -> dict[str, Any]:
    """Cards parked until a start time the user gave: let each go once its time has passed
    (once: ``start_after`` is cleared), unless paused, cancelled or held; keep one that is not
    parked yet (an interrupted create) parked. No model and no worker run before the time."""
    stats: dict[str, Any] = {"started": 0, "parked": 0, "waiting": 0, "errors": 0}
    rows = conn.execute("SELECT id FROM delegations WHERE start_after IS NOT NULL AND card_id IS NOT NULL"
                        " AND status NOT IN ('ended','cancelled','quarantined') ORDER BY start_after").fetchall()
    for row in rows:
        d = get(conn, row["id"])
        due = not waiting_start(d)
        if not _may_run(conn, d) or (not due and d["card_status"] == "scheduled"):
            stats["waiting"] += 1
            continue
        try:
            status = kb.release_schedule(d["card_id"]) if due else kb.schedule(d["card_id"], d["start_after"])
        except KanbanError as exc:
            with tx(conn):
                _set(conn, d["id"], last_error=f"{'start' if due else 'park'} failed: {exc}")
            _failed(stats, "errors", f"{'start' if due else 'park'} failed: {exc}")
            continue
        with tx(conn):
            if due:
                _set(conn, d["id"], start_after=None, card_status=status)
                _task_note(conn, d["task_id"], i18n.t("dlg.started_later", title=d["title"]), d["id"])
            else:
                _set(conn, d["id"], card_status=status, status=_status_from_card(status, d))
        stats["started" if due else "parked"] += 1
    return stats


def _pause_linked(conn: sqlite3.Connection, cfg: Config, kb: Kanban, delegation_id: str,
                  stats: dict[str, Any]) -> None:
    """A card found after a lost create response, for a delegation the user paused meanwhile:
    apply the pause now, before a ready card is picked up (a failure leaves it to send_messages)."""
    for msg in conn.execute("SELECT * FROM delegation_messages WHERE delegation_id=? AND kind='pause'"
                            " AND status IN ('pending','sending') ORDER BY created_at", (delegation_id,)).fetchall():
        try:
            _deliver(conn, cfg, kb, get(conn, delegation_id), dict(msg))
        except KanbanError as exc:
            with tx(conn):
                conn.execute("UPDATE delegation_messages SET status=?, error=? WHERE id=?",
                             ("sending" if exc.uncertain or msg["status"] == "sending" else "pending",
                              str(exc)[:500], msg["id"]))
            _failed(stats, "errors", f"pause not delivered: {exc}")


def _notion_url(conn: sqlite3.Connection, task: dict[str, Any]) -> str | None:
    row = conn.execute("SELECT page_id, url FROM notion_pages WHERE entity_type='task' AND entity_id=?",
                       (task["id"],)).fetchone()
    if row is None:
        return None
    return row["url"] or f"https://www.notion.so/{row['page_id'].replace('-', '')}"


def _status_from_card(card_status: str, d: dict[str, Any], paused_block: bool | None = None) -> str:
    """``paused_block``: whether the newest block event seen is our pause (None: no block seen
    now, the earlier state holds). A requested pause alone is not "paused": a card blocked by
    the worker's question stays blocked."""
    if card_status in ("ready", "todo", "scheduled"):
        return "queued"
    if card_status == "running":
        return "running"
    if card_status in ("blocked", "triage"):
        ours = paused_block if paused_block is not None else d.get("status") == "paused"
        return "paused" if ours else "blocked"
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
                        " ('quarantined') AND (status NOT IN ('ended','cancelled') OR children_sync_pending=1 OR EXISTS "
                        "(SELECT 1 FROM delegation_children c WHERE c.delegation_id=delegations.id "
                        "AND c.status NOT IN ('done','archived')) OR EXISTS "
                        "(SELECT 1 FROM cursors n WHERE n.source='kanban' AND n.consecutive_failures>0 "
                        "AND n.last_error LIKE 'notification registration failed:%' AND "
                        "(n.scope='notifications:' || delegations.card_id OR n.scope IN "
                        "(SELECT 'notifications:' || c.card_id FROM delegation_children c "
                        "WHERE c.delegation_id=delegations.id))))").fetchall()
    for row in rows:
        d = get(conn, row["id"])
        try:
            repair_notification(conn, cfg, kb, d, d['card_id'], release=True)
            snap = kb.snapshot(d["card_id"], after_event=d["event_cursor"])
        except KanbanError as exc:
            with tx(conn):
                _set(conn, d["id"], last_error=f"snapshot failed: {exc}")
            _failed(stats, "errors", f"snapshot failed: {exc}")
            continue
        stats["checked"] += 1
        # While a cancel is on its way, whatever the card still does is history only.
        quiet = d['status'] == 'cancelled' or conn.execute("SELECT 1 FROM delegation_messages WHERE delegation_id=? AND kind='cancel'"
                             " AND status IN ('pending','sending','waiting_capacity')", (d["id"],)).fetchone() is not None
        with tx(conn):
            # Commit retry responsibility with root progress, before any child I/O.
            _set(conn, d['id'], children_sync_pending=1)
            _apply_snapshot(conn, cfg, d, snap, stats, quiet=quiet)
        try:
            from . import child_links
            child_links.sync(conn, cfg, kb, get(conn, d["id"]), quiet=quiet)
        except KanbanError as exc:
            _failed(stats, "errors", f"child sync failed: {exc}")
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


def _blocked_note(conn: sqlite3.Connection, delegation_id: str) -> str:
    """A card blocked by a failure waits for retry or cancel, not for information."""
    last = conn.execute("SELECT kind FROM deliveries WHERE delegation_id=? AND kind IN ('question','failure')"
                        " ORDER BY rowid DESC LIMIT 1", (delegation_id,)).fetchone()
    return "dlg.failed_note" if last and last["kind"] == "failure" else "dlg.needs_input"


def _apply_snapshot(conn: sqlite3.Connection, cfg: Config, d: dict[str, Any], snap: dict[str, Any],
                    stats: dict[str, int], quiet: bool = False) -> None:
    """Record what happened on the card. ``quiet``: the delegation was cancelled; late
    results are kept as history but change nothing and are not announced."""
    cursor = d["event_cursor"]
    block_seq = d["block_seq"]
    block_reason = d["block_reason"]
    block_event = d["block_event_id"]
    pause_event = d["pause_event_id"]
    paused_block: bool | None = None   # whether the newest block seen now is our pause

    def announce(dlv: str, text: str) -> None:
        if not quiet:
            effects.task_comment(conn, d["task_id"], f"dlv:{dlv}", text)

    for ev in snap["events"]:
        cursor = max(cursor, ev["id"])
        kind = ev["kind"]
        payload = ev.get("payload") or {}
        if kind in ("blocked", "block_loop_detected"):
            reason = payload.get("reason") or ""
            if reason == "initial_status" and payload.get("actor") == "todo-manager":
                continue  # native staging for subscription registration, not a worker question
            paused_block = reason.startswith(PAUSE_MARK)
            if paused_block:
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
                paused_block = False
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
            # Also while a cancel is pending: a cancel does not show the approved action was not done.
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
    new_status = _status_from_card(card_status, d, paused_block)
    if card_status == "done" and d["status"] != "ended":
        new_status = "ended"
    _set(conn, d["id"], event_cursor=cursor, card_status=card_status, status=new_status, block_seq=block_seq,
         block_reason=block_reason, block_event_id=block_event, pause_event_id=pause_event)
    if new_status != d["status"]:
        if new_status in ("queued", "running"):
            _next_actor(conn, d["id"], "agent", None)
        elif new_status in ("blocked", "review"):
            _next_actor(conn, d["id"], "me", i18n.t(_blocked_note(conn, d["id"]) if new_status == "blocked"
                                                    else "dlg.to_review"))
        elif new_status in ("ended", "cancelled") and not for_task_active_other(conn, d):
            note = ("dlg.cancelled" if new_status == "cancelled"
                    else "dlg.to_review" if _latest_result(conn, d["id"]) else "dlg.ended")
            _next_actor(conn, d["id"], "me", i18n.t(note))
        effects.task_push(conn, d["task_id"])


def _settle_approvals(conn: sqlite3.Connection, cfg: Config, d: dict[str, Any], delivery_id: str) -> None:
    """A new version arrived: approvals already sent are used up (the worker reports what
    it did in this delivery); approvals not sent yet are void and their messages withdrawn."""
    conn.execute("UPDATE approvals SET outcome='reported' WHERE delegation_id=? AND consumed_at IS NOT NULL"
                 " AND outcome IS NULL", (d["id"],))
    for a in _unsent_approvals(conn, d["id"], besides=delivery_id):
        if a["mstatus"] == "sending":
            continue  # in flight: its retry reconciles against the card event it was bound to
        _void_approval(conn, d, a, "newer_version")


def _void_approval(conn: sqlite3.Connection, d: dict[str, Any], a: sqlite3.Row, reason: str, **values: Any) -> None:
    conn.execute("UPDATE approvals SET revoked_at=COALESCE(revoked_at, ?) WHERE id=?", (clock.iso(), a["id"]))
    conn.execute("UPDATE delegation_messages SET status='superseded', error=? WHERE approval_id=?"
                 " AND status IN ('pending','waiting_capacity','sending')", (f"approval void: {reason}", a["id"]))
    why = i18n.t(f"dlg.reason.{reason}", **values)
    if a["answer_message_id"]:
        seq = conn.execute("SELECT seq FROM deliveries WHERE id=?", (a["delivery_id"],)).fetchone()
        text = i18n.t("dlg.approval_void_answer", action=a["action"], seq=seq[0] if seq else "?", reason=why)
    else:
        text = i18n.t("dlg.approval_void", action=a["action"], delivery=a["delivery_id"], reason=why)
    effects.task_comment(conn, d["task_id"], f"approval-void:{a['id']}", text)


def _hold_unknown_external_action(conn: sqlite3.Connection, cfg: Config, d: dict[str, Any], event: str,
                                  handed: list[sqlite3.Row] | None = None) -> None:
    """A run that was handed an approval ended without reporting back: the external action
    may or may not have happened. Pause the card so it is not retried automatically, and ask.
    ``handed``: the approvals that run had (default: every handed one not settled yet). A
    delegation that is being cancelled or has ended is not paused: nothing runs it again."""
    if handed is None:
        handed = conn.execute("SELECT * FROM approvals WHERE delegation_id=? AND consumed_at IS NOT NULL"
                              " AND outcome IS NULL ORDER BY consumed_at, rowid", (d["id"],)).fetchall()
    if not handed:
        return
    now = get(conn, d["id"])               # a pause queued meanwhile (by the user or for another approval) holds it
    stopped = now["status"] in ("ended", "cancelled") or cancel_pending(conn, now)
    text = ""
    for a in handed:                       # approvals of one version go out to the same run
        conn.execute("UPDATE approvals SET outcome='unknown' WHERE id=? AND outcome IS NULL", (a["id"],))
        text = i18n.t("dlg.approval_unknown_stopped" if stopped else "dlg.approval_unknown",
                      action=a["action"], title=d["title"], event=event)
        effects.task_comment(conn, d["task_id"], f"approval-unknown:{a['id']}", text)
    if not now["pause_requested"] and not stopped:
        add_message(conn, d["id"], "pause", text, cause="approval_unknown")


def _handed_reopen(conn: sqlite3.Connection, a: sqlite3.Row, events: list[dict[str, Any]]) -> int | None:
    """The card event that handed approval ``a`` to the worker: for an approval of a delivered
    version, the first ``review_reopened`` after that version's review event (a review is
    reopened once); for one given with an answer, the first unblock after the question."""
    row = conn.execute("SELECT kind, source_event_id FROM deliveries WHERE id=?", (a["delivery_id"],)).fetchone()
    if row is None or row["source_event_id"] is None:
        return None
    kinds = ("unblocked", "specified") if row["kind"] == "question" else ("review_reopened",)
    return min((int(e["id"]) for e in events
                if e["kind"] in kinds and int(e["id"]) > int(row["source_event_id"])), default=None)


# Card events that end the run approvals were handed to: a result, or the run ending without one
# (a question is not a report: the worker may have acted before asking).
HANDED_RUN_ENDS = ("review_requested", "completed") + RUN_ENDED_EVENTS


def _handed_run_end(conn: sqlite3.Connection, d: dict[str, Any], a: sqlite3.Row, msg: dict[str, Any],
                    events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The card event that ended the run approval ``a`` was handed to: the first result or run
    end after the event that handed it (``_handed_reopen``) and after the comment that carried
    it. Earlier runs and the approved version's own result come before it. None: not ended yet
    (or the handing event is not among ``events``)."""
    ordered = sorted(events, key=lambda e: int(e["id"]))
    reopen = _handed_reopen(conn, a, ordered)
    if reopen is None:
        return None
    start = max(reopen, int(msg.get("card_event_id") or 0))
    for e in ordered:
        if int(e["id"]) <= start or e["kind"] not in HANDED_RUN_ENDS:
            continue
        if e["kind"] == "completed" and _accepted_by_us(conn, d["id"]):
            continue                       # as in the sync: our own accept/cancel is not a result
        return e
    return None


def _settle_late(conn: sqlite3.Connection, cfg: Config, d: dict[str, Any], msg: dict[str, Any],
                 snap: dict[str, Any]) -> None:
    """Approvals confirmed as handed only now (the response of the reopen or unblock that
    carried them was lost; a retry found it on the card): the sync may already have read the
    end of their run - and moved its cursor past it - while they did not count as handed yet.
    Settle them from the card's events (``snap``: all of them, taken after that sync) as the
    sync would have; an end the sync has not read yet is left to it."""
    if msg["kind"] == "answer":
        handed = answer_approvals(conn, msg["id"])
    else:
        handed = conn.execute("SELECT * FROM approvals WHERE id=?", (msg.get("approval_id"),)).fetchall()
    cursor = conn.execute("SELECT event_cursor FROM delegations WHERE id=?", (d["id"],)).fetchone()[0]
    for a in handed:
        if a["consumed_at"] is None or a["outcome"] is not None:
            continue
        end = _handed_run_end(conn, d, a, msg, snap.get("events") or [])
        if end is None or int(end["id"]) > int(cursor or 0):
            continue
        if end["kind"] in RUN_ENDED_EVENTS:
            _hold_unknown_external_action(conn, cfg, d, end["kind"], handed=[a])
        else:
            conn.execute("UPDATE approvals SET outcome='reported' WHERE id=? AND outcome IS NULL", (a["id"],))


def _open_approvals(conn: sqlite3.Connection, delegation_id: str) -> list[sqlite3.Row]:
    """Approvals handed to a run whose outcome is not known yet."""
    return conn.execute("SELECT a.*, m.card_event_id FROM approvals a LEFT JOIN delegation_messages m"
                        " ON m.approval_id=a.id AND m.kind='approval' WHERE a.delegation_id=?"
                        " AND a.consumed_at IS NOT NULL AND a.outcome IS NULL ORDER BY a.consumed_at, a.rowid",
                        (delegation_id,)).fetchall()


def _settle_on_cancel(conn: sqlite3.Connection, cfg: Config, d: dict[str, Any], events: list[dict[str, Any]]) -> None:
    """The card was archived and is not synced again: settle every approval handed to a run
    whose outcome is still open, from all of the card's events (``events``, read after the
    archive, whether or not the sync cursor covers them). A result of the run it went to ->
    reported; that run ended without one, or was still going or waiting for an answer when the
    card was archived -> unknown (archiving does not show the action was not done; the user is
    told, nothing is paused or retried); no run started after it reached the card (Hermes claims
    a card before it starts a worker) -> not_run."""
    for a in _open_approvals(conn, d["id"]):
        end = _handed_run_end(conn, d, a, {"card_event_id": a["card_event_id"]}, events)
        if end is not None and end["kind"] not in RUN_ENDED_EVENTS:
            conn.execute("UPDATE approvals SET outcome='reported' WHERE id=? AND outcome IS NULL", (a["id"],))
            continue
        reopen = _handed_reopen(conn, a, events)
        if end is None and reopen is not None and not any(
                e["kind"] == "claimed" and int(e["id"]) > reopen for e in events):
            conn.execute("UPDATE approvals SET outcome='not_run' WHERE id=? AND outcome IS NULL", (a["id"],))
            continue
        _hold_unknown_external_action(conn, cfg, d, end["kind"] if end is not None else "archived", handed=[a])


def _accepted_by_us(conn: sqlite3.Connection, delegation_id: str) -> bool:
    return conn.execute("SELECT 1 FROM delegation_messages WHERE delegation_id=? AND kind IN ('accept','cancel')"
                        " AND status IN ('sending','sent')", (delegation_id,)).fetchone() is not None


LABEL_ORDER = ("quarantined", "blocked", "review", "running", "queued", "paused")
NEEDS_USER_LABELS = ("blocked", "review", "delivered")


def task_label(conn: sqlite3.Connection, task_id: str) -> str | None:
    """A Task's "Agent work": the most urgent state of its active delegations, else
    "delivered" while its latest delegation ended with a result (the draft is the user's to
    use; the Task stays open until the user closes it), else None."""
    task = store.get_task(conn, task_id)
    if task is None or task['status'] in store.CLOSED_STATUSES:
        return None
    statuses = {"queued" if d["status"] in ("pending_submit", "reconcile") else d["status"]
                for d in for_task(conn, task_id, active_only=True)}
    attention = conn.execute("SELECT c.attention FROM delegation_children c JOIN delegations d ON d.id=c.delegation_id "
                             "WHERE d.task_id=? AND d.status NOT IN ('cancelled','quarantined') "
                             "AND c.attention IS NOT NULL", (task_id,)).fetchall()
    if attention:
        statuses.add('blocked')
    for status in LABEL_ORDER:
        if status in statuses:
            return status
    row = conn.execute("SELECT id, status, handled_at, created_at FROM delegations WHERE task_id=?"
                       " ORDER BY created_at DESC, rowid DESC LIMIT 1", (task_id,)).fetchone()
    if row is not None and row["status"] == "ended" and not row["handled_at"] \
            and _latest_result(conn, row["id"]) is not None:
        return "delivered"
    return _handover_label(conn, task, row["created_at"] if row is not None else None)


def _handover_label(conn: sqlite3.Connection, task: dict[str, Any], last_work_at: str | None) -> str | None:
    """A Task handed to the agent with no agent work yet: "queued" while the hand-over (or the
    user's later word on the task) waits for a run, "blocked" when the run asked the user a
    question instead of starting work."""
    if task["next_actor"] != "agent":
        return None
    if conn.execute("SELECT 1 FROM feedback WHERE scope_type='task' AND scope_id=? AND status='pending'",
                    (task["id"],)).fetchone():
        return "queued"
    request = conn.execute("SELECT status, applied_at FROM feedback WHERE scope_type='task' AND scope_id=?"
                           " AND kind='delegate_request' AND status!='superseded'"
                           " ORDER BY received_at DESC, rowid DESC LIMIT 1", (task["id"],)).fetchone()
    if request is not None and request["status"] == "clarify" \
            and (last_work_at is None or last_work_at < (request["applied_at"] or "")):
        return "blocked"
    return None


# The Agent progress board column of a "Agent work" label: what the user is asked for.
PROGRESS = {"blocked": "answer", "review": "review", "delivered": "review",
            "queued": "working", "running": "working", "paused": "working", "quarantined": "working"}


def task_progress(conn: sqlite3.Connection, task_id: str) -> str | None:
    return PROGRESS.get(task_label(conn, task_id) or "")


def board_choice(conn: sqlite3.Connection, task_id: str, choice: str, feedback_id: str) -> list[str]:
    """The user's drag on the Agent progress board: ``done`` / ``todo`` accept a version waiting
    for review; ``todo`` also stops work still going (the user takes the task over); closing the
    task (``done``, ``cancel``) stops the rest through ``on_task_closed``. The latest delivery is
    handled either way, so the task leaves the board. Called before the task's status changes."""
    notes = []
    for d in for_task(conn, task_id, active_only=True):
        kind = ("accept" if choice in ("done", "todo") and d["status"] == "review"
                else "cancel" if choice == "todo" and d["status"] in ACTIVE else None)
        if kind is None:
            continue
        try:
            add_message(conn, d["id"], kind, i18n.t("dlg.board_accept" if kind == "accept" else "dlg.board_take_over"),
                        feedback_id=feedback_id)
        except DelegationError as exc:   # e.g. being cancelled already: the task change still applies
            notes.append(f"{kind}_refused:{d['id']}:{exc}")
            continue
        notes.append(f"{kind}:{d['id']}")
    conn.execute("UPDATE delegations SET handled_at=? WHERE id=(SELECT id FROM delegations WHERE task_id=?"
                 " ORDER BY created_at DESC, rowid DESC LIMIT 1) AND handled_at IS NULL", (clock.iso(), task_id))
    return notes


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
                          " AND status NOT IN ('superseded','failed') AND (target_card_id IS NULL OR target_card_id="
                          "(SELECT card_id FROM delegations WHERE id=delegation_messages.delegation_id))", (d["id"],)):
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


# Kinds that would act on the work; refused while a cancel is pending (pause/supplement are harmless).
CONFLICTS_WITH_CANCEL = ("answer", "revision", "resume", "accept", "approval")


def cancel_pending(conn: sqlite3.Connection, d: dict[str, Any]) -> bool:
    """A cancel that will be processed: explicit, or for a Task that is still closed (reopening
    the Task withdraws a task_closed cancel)."""
    task = store.get_task(conn, d["task_id"])
    task_closed = task is not None and task["status"] in store.CLOSED_STATUSES
    return any(r["cause"] != "task_closed" or task_closed for r in conn.execute(
        "SELECT cause FROM delegation_messages WHERE delegation_id=? AND kind='cancel'"
        " AND status IN ('pending','sending','waiting_capacity')", (d["id"],)))


def add_message(conn: sqlite3.Connection, delegation_id: str, kind: str, body: str,
                feedback_id: str | None = None, approval_id: str | None = None, cause: str | None = None,
                delivery_id: str | None = None, op_key: str | None = None,
                approve: list[tuple[str, str]] | None = None) -> str:
    """Queue a message. Its target is fixed here and kept for every retry: an answer binds to
    the open question, accept/revision to ``delivery_id`` (the version the user replied to) or
    else the latest delivered version, resume to the latest pause. ``op_key`` (feedback id + op
    index) makes a retried op return its message while a second op of the same kind in the
    same feedback gets its own; without it, one message per feedback item and kind.
    ``approve`` (answers only): external actions ``(action, detail)`` the user allows with this
    answer; they go to the worker with it and are used at most once (``answer_approvals``)."""
    if kind not in MESSAGE_KINDS:
        raise DelegationError(f"invalid message kind {kind!r}")
    if approve and kind != "answer":
        raise DelegationError("approvals go with an answer to a question, or with `delegate approve` for a version")
    unknown = action_names([a for a, _ in approve or []]) - KNOWN_ACTIONS
    if unknown:
        raise DelegationError(f"unknown actions {sorted(unknown)}; known: {sorted(KNOWN_ACTIONS)}")
    d = get(conn, delegation_id)
    if d is None:
        raise DelegationError(f"delegation {delegation_id} not found")
    if d["status"] in ("ended", "cancelled") and kind not in ("supplement", "cancel"):
        raise DelegationError(f"delegation is {d['status']}; create a new linked delegation for new work")
    if kind in CONFLICTS_WITH_CANCEL and cancel_pending(conn, d):
        raise DelegationError("the delegation is being cancelled; nothing more is sent to it")
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
            if kind == 'answer':
                bound = conn.execute('SELECT source_event_id FROM deliveries WHERE id=? AND delegation_id=?',
                                     (delivery_id, delegation_id)).fetchone() if delivery_id else None
                anchor = bound['source_event_id'] if bound else d['block_event_id']
                existing = conn.execute("SELECT id FROM delegation_messages WHERE delegation_id=? AND feedback_id=? "
                                        "AND kind='answer' AND target_event_id IS ?",
                                        (delegation_id, feedback_id, anchor)).fetchone()
            else:
                existing = conn.execute("SELECT id FROM delegation_messages WHERE delegation_id=? AND feedback_id=? AND kind=?",
                                        (delegation_id, feedback_id, kind)).fetchone()
        else:
            existing = None
        if existing:
            return existing["id"]
        if kind == 'answer':
            question_row = conn.execute("SELECT * FROM deliveries WHERE delegation_id=? AND card_id=? AND kind IN ('question','failure') "
                                        "AND id=COALESCE(?,id) AND source_event_id=?",
                                        (delegation_id, d['card_id'], delivery_id, d['block_event_id'])).fetchone()
            if d['status'] != 'blocked' or question_row is None:
                raise DelegationError('earlier question is stale or no longer open; no answer queued')
            if approve and question_row['requirement_version'] != d['requirement_version']:
                raise DelegationError('requirements changed; answer without approval and request fresh authorization')
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
        question = None
        if approve:
            question = conn.execute("SELECT id FROM deliveries WHERE delegation_id=? AND kind='question'"
                                    " AND source_event_id=?", (delegation_id, target_event)).fetchone() \
                if target_event is not None else None
            if question is None:
                raise DelegationError("there is no open question to answer with an approval")
        conn.execute(
            "INSERT INTO delegation_messages(id, delegation_id, kind, feedback_id, body, requirement_version,"
            " target_block_seq, target_event_id, target_seq, target_message_id, approval_id, cause, status, created_at,"
            " op_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)",
            (mid, delegation_id, kind, feedback_id, body, requirement_version,
             d["block_seq"] if answering else None, target_event, target_seq, target_message,
             approval_id, cause, clock.iso(), op_key))
        if kind == 'answer':
            conn.execute('UPDATE delegation_messages SET target_card_id=? WHERE id=?', (d['card_id'], mid))
        if changes_requirement:
            _set(conn, delegation_id, requirement_version=requirement_version)
            # A substantive change voids approvals that have not gone out yet.
            for a in _unsent_approvals(conn, delegation_id):
                if a["mstatus"] != "sending":
                    _void_approval(conn, d, a, "requirement_changed")
        for action, detail in approve or []:
            conn.execute("INSERT INTO approvals(id, delegation_id, delivery_id, action, detail, feedback_id, granted_at,"
                         " answer_message_id) VALUES (?,?,?,?,?,?,?,?)",
                         (ids.new_id("approval"), delegation_id, question["id"], action, detail or "", feedback_id,
                          clock.iso(), mid))
        if kind == "pause":
            _set(conn, delegation_id, pause_requested=1)
        elif kind == "resume":
            _set(conn, delegation_id, pause_requested=0)
    return mid


def _unsent_approvals(conn: sqlite3.Connection, delegation_id: str, *, besides: str | None = None) -> list[sqlite3.Row]:
    """Approvals not handed to the worker yet, with the status of the message that carries
    them (its own approval message, or the answer it was given with)."""
    return conn.execute(
        "SELECT a.*, m.id AS mid, m.status AS mstatus FROM approvals a LEFT JOIN delegation_messages m"
        " ON m.id = COALESCE(a.answer_message_id, (SELECT x.id FROM delegation_messages x WHERE x.approval_id=a.id))"
        " WHERE a.delegation_id=? AND a.consumed_at IS NULL AND a.revoked_at IS NULL AND a.delivery_id IS NOT ?",
        (delegation_id, besides)).fetchall()


def answer_approvals(conn: sqlite3.Connection, message_id: str) -> list[sqlite3.Row]:
    """The approvals given with an answer, still valid."""
    return conn.execute("SELECT * FROM approvals WHERE answer_message_id=? AND revoked_at IS NULL"
                        " ORDER BY granted_at, rowid", (message_id,)).fetchall()


def on_task_closed(conn: sqlite3.Connection, task_id: str, status: str, reason: str,
                   feedback_id: str | None) -> list[str]:
    """The user closed the task: its active agent work is cancelled (``_closing_message``)."""
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
    (status ``waiting_capacity``, error "paused by user": the name is kept from the schema).
    How many cards run at once is Hermes' scheduling, not ours."""
    if d["pause_requested"] and msg["kind"] != "resume":
        with tx(conn):
            conn.execute("UPDATE delegation_messages SET status='waiting_capacity', error='paused by user' WHERE id=?",
                         (msg["id"],))
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
    # Same lock as submission: card links and transitions are decided one at a time.
    with file_lock(lock_path(cfg.home, "kanban-submit"), blocking=True, timeout=60):
        enforce_task_lifecycle(conn)
        rows = conn.execute("SELECT * FROM delegation_messages WHERE status IN ('pending','sending','waiting_capacity')"
                            " AND kind != 'requirement' ORDER BY created_at").fetchall()
        # Cancels first: what the cancel voids must not be carried out before it.
        rows = sorted(rows, key=lambda r: r["kind"] != "cancel")
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
                elif msg["target_card_id"] and msg["target_card_id"] != d["card_id"]:
                    from . import child_links
                    outcome = child_links.deliver_answer(conn, cfg, kb, d, dict(msg))
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
    """The message could not be applied to the state it was meant for: say so, never drop it
    silently. Approvals given with an answer are void with it."""
    _finish(conn, msg["id"], "superseded", reason)
    with tx(conn):
        for a in answer_approvals(conn, msg["id"]):
            if a["consumed_at"] is None:
                _void_approval(conn, d, a, reason, **values)
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
    _settle_for_cancel(conn, cfg, kb, d, msg)
    with tx(conn):
        conn.execute("UPDATE delegation_messages SET status='sent', sent_at=? WHERE id=?", (clock.iso(), msg["id"]))
        _set(conn, d["id"], status="cancelled", cancel_state="confirmed")
        if not for_task_active_other(conn, d):
            _next_actor(conn, d["id"], "me", i18n.t("dlg.cancelled"))
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
    if kind in ("pause", "resume") and status in ("ended", "cancelled") and not attempted:
        return _unused(conn, d, msg, "card_moved", status=d.get("card_status") or status)
    if kind == "pause":
        descendants = kb.owned(d["card_id"])
        if descendants:
            with tx(conn):
                effects.task_comment(conn, d["task_id"], f"pause-scope:{mid}",
                    i18n.t("dlg.child_pause_scope", cards=", ".join(descendants)))
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
        _post_comment(conn, kb, d, msg, _message_text(conn, msg), snap)
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
            _post_comment(conn, kb, d, msg, _message_text(conn, msg), snap)  # records its place
        return _sent_transition(conn, d, msg, requeued=False, cfg=cfg, snap=snap)
    if verdict == "mismatch":
        return _unused(conn, d, msg, "newer_question" if kind in ("answer", "resume") else "card_moved",
                       status=snap["task"]["status"])
    held_back = _hold_back(conn, cfg, d, msg, attempted)
    if held_back:
        return held_back
    _sending(conn, mid)
    if kind != "resume":
        _post_comment(conn, kb, d, msg, _message_text(conn, msg), snap)
    return _apply_transition(conn, cfg, kb, d, msg, action, anchor)


def _message_text(conn: sqlite3.Connection, msg: dict[str, Any]) -> str:
    """The comment for a message; an answer carries the approvals given with it, one line each
    (built when it is posted, so a voided approval is not in it)."""
    text = f"[{i18n.t('dlg.msg.' + msg['kind'])}] {msg['body']}"
    if msg["kind"] == "answer":
        seq = msg.get("target_block_seq")
        text += "".join(f"\nAPPROVED {a['action']} for question #{seq}. {a['detail'] or ''}".rstrip()
                        for a in answer_approvals(conn, msg["id"]))
    return text


def _apply_transition(conn: sqlite3.Connection, cfg: Config, kb: Kanban, d: dict[str, Any], msg: dict[str, Any],
                      action: str, anchor: int | None) -> str:
    kind = msg["kind"]
    result = kb.transition(d["card_id"], action, anchor)
    if result["outcome"] == "mismatch":
        return _unused(conn, d, msg, "newer_question" if kind in ("answer", "resume") else "card_moved",
                       status=result["status"])
    sent = _sent_transition(conn, d, msg, requeued=result["outcome"] in ("done", "raced"))
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
    posted before it are sent; one that was not is void (that run started without it). While
    the version still waits in review nothing was handed over: an attempted approval is checked
    again, and one void meanwhile is not sent (its comment, if it landed, is withdrawn)."""
    a = conn.execute("SELECT * FROM approvals WHERE id=?", (msg["approval_id"],)).fetchone() \
        if msg.get("approval_id") else None
    if a is None:
        _refuse_approval(conn, d, msg, "unbound")
        return "superseded"
    group, problems = [], {}
    for m in conn.execute("SELECT m.* FROM delegation_messages m JOIN approvals a ON a.id=m.approval_id"
                          " WHERE m.delegation_id=? AND m.kind='approval' AND a.delivery_id=?"
                          " AND m.status IN ('pending','sending','waiting_capacity') ORDER BY m.created_at, m.rowid",
                          (d["id"], a["delivery_id"])).fetchall():
        m = dict(m)
        problem = _approval_problem(conn, d, m)
        if problem and m["status"] != "sending":
            _refuse_approval(conn, d, m, problem)
        else:
            group.append(m)   # an attempted one may have been handed over: the card tells below
            if problem:
                problems[m["id"]] = problem
    if not group:
        return "superseded"
    anchor = conn.execute("SELECT source_event_id FROM deliveries WHERE id=?", (a["delivery_id"],)).fetchone()[0]
    snap = kb.snapshot(d["card_id"], after_event=0)
    verdict = transition_verdict(snap["task"]["status"], snap["events"], "reopen_review", anchor)
    if verdict == "already":
        for m in group:
            if _has_marker(snap, m["id"]):
                _post_comment(conn, kb, d, m, f"[{i18n.t('dlg.msg.approval')}] {m['body']}", snap)  # records its place
                _sent_transition(conn, d, m, requeued=False, cfg=cfg, snap=snap)
            else:
                _refuse_approval(conn, d, m, "sent_back_without_it")
        return "sent"
    if verdict == "mismatch":
        newer = any(e["kind"] == "review_requested" and int(e["id"]) > int(anchor) for e in snap["events"])
        for m in group:
            _refuse_approval(conn, d, m, "newer_version" if newer else "not_in_review")
        return "superseded"
    for m in [m for m in group if m["id"] in problems]:
        if _has_marker(snap, m["id"]) and not _has_marker(snap, f"{m['id']}:withdrawn"):
            ap = conn.execute("SELECT * FROM approvals WHERE id=?", (m["approval_id"],)).fetchone()
            kb.comment(d["card_id"], f"[{i18n.t('dlg.msg.approval')}] WITHDRAWN {ap['action']} for delivery"
                       f" {ap['delivery_id']}: {i18n.t('dlg.reason.' + problems[m['id']])}. This approval is void;"
                       f" do not act on it.\n[tm:{m['id']}:withdrawn]")
        _refuse_approval(conn, d, m, problems[m["id"]])
        group.remove(m)
    if not group:
        return "superseded"
    if d["pause_requested"]:
        with tx(conn):
            for m in group:
                conn.execute("UPDATE delegation_messages SET status='waiting_capacity', error='paused by user'"
                             " WHERE id=?", (m["id"],))
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
        _sent_transition(conn, d, m, requeued=n == 0 and result["outcome"] in ("done", "raced"), cfg=cfg, snap=snap)
    if result["outcome"] == "raced":
        _raced(conn, cfg, d, msg)
    return "sent"


def _sent_transition(conn: sqlite3.Connection, d: dict[str, Any], msg: dict[str, Any], requeued: bool,
                     cfg: Config | None = None, snap: dict[str, Any] | None = None) -> str:
    """``cfg``/``snap`` (all card events, taken after the last sync): an approval is settled
    from them in the same transaction when its run already ended (``_settle_late``)."""
    with tx(conn):
        conn.execute("UPDATE delegation_messages SET status='sent', sent_at=?, error=NULL WHERE id=?",
                     (clock.iso(), msg["id"]))
        if msg["kind"] == "approval" and msg.get("approval_id"):
            conn.execute("UPDATE approvals SET consumed_at=COALESCE(consumed_at, ?) WHERE id=?",
                         (clock.iso(), msg["approval_id"]))
        elif msg["kind"] == "answer":
            conn.execute("UPDATE approvals SET consumed_at=COALESCE(consumed_at, ?) WHERE answer_message_id=?"
                         " AND revoked_at IS NULL", (clock.iso(), msg["id"]))
        if msg["kind"] in ("approval", "answer") and cfg is not None and snap is not None:
            _settle_late(conn, cfg, d, msg, snap)
        if requeued:
            # The card is back in Hermes' queue (shown as such until the next sync reads it).
            _set(conn, d["id"], status="queued")
            _next_actor(conn, d["id"], "agent", None)
    return "sent"


def _landed(conn: sqlite3.Connection, d: dict[str, Any], m: dict[str, Any], snap: dict[str, Any]) -> bool:
    """Whether a message whose earlier attempt has an unknown outcome took effect on the card,
    judged by the event it was bound to (and its marker for those that post a comment)."""
    status, events, kind = snap["task"]["status"], snap["events"], m["kind"]
    if kind == "accept":
        anchor = _accept_anchor(conn, d, m)
        return anchor is not None and transition_verdict(status, events, "complete", anchor) == "already"
    if kind == "resume":
        anchor = d.get("pause_event_id")
        return anchor is not None and transition_verdict(status, events, "unblock", int(anchor)) == "already"
    if not _has_marker(snap, m["id"]):
        return False
    if kind == "approval":
        row = conn.execute("SELECT dl.source_event_id FROM deliveries dl JOIN approvals a ON a.delivery_id=dl.id"
                           " WHERE a.id=?", (m.get("approval_id"),)).fetchone()
        return row is not None and transition_verdict(status, events, "reopen_review",
                                                      row["source_event_id"]) == "already"
    if kind == "answer" and m.get("target_event_id") is not None:
        return transition_verdict(status, events, "unblock", m["target_event_id"]) == "already"
    if kind == "revision" and m.get("target_seq"):
        return transition_verdict(status, events, "reopen_review", m["target_event_id"]) == "already"
    return True   # its comment is what it does


def _settle_for_cancel(conn: sqlite3.Connection, cfg: Config, kb: Kanban, d: dict[str, Any],
                       cancel: dict[str, Any]) -> bool:
    """The cancel goes first: every other message of the delegation that has not taken effect is
    void, and the user is told which - nothing is unblocked or reopened only to be archived. One
    in flight is checked on the card first and kept as history when it landed. Returns False when
    an accept of ours had already completed the card: the cancel came too late."""
    others = [dict(m) for m in conn.execute(
        "SELECT * FROM delegation_messages WHERE delegation_id=? AND id != ? AND kind NOT IN ('requirement','cancel')"
        " AND status IN ('pending','sending','waiting_capacity') ORDER BY created_at, rowid", (d["id"], cancel["id"]))]
    if not others:
        return True
    snap = kb.snapshot(d["card_id"], after_event=0) \
        if d["card_id"] and any(m["status"] == "sending" for m in others) else None
    accepted, voided = False, []
    for m in others:
        if snap is None or m["status"] != "sending" or not _landed(conn, d, m, snap):
            voided.append(m)
        elif m["kind"] == "accept":
            _accepted(conn, d, m, snap["task"]["status"])
            accepted = True
        else:
            _sent_transition(conn, d, m, requeued=False, cfg=cfg, snap=snap)
    if voided:
        items = "; ".join(f"{i18n.t('dlg.msg.' + m['kind'])}: {(m['body'] or '')[:120]}" for m in voided)
        with tx(conn):
            for m in voided:
                conn.execute("UPDATE delegation_messages SET status='superseded', error=? WHERE id=?",
                             (f"void: cancelled ({cancel['id']}) before it was carried out", m["id"]))
                conn.execute("UPDATE approvals SET revoked_at=COALESCE(revoked_at, ?) WHERE (id=? OR"
                             " answer_message_id=?) AND consumed_at IS NULL", (clock.iso(), m.get("approval_id"), m["id"]))
            effects.task_comment(conn, d["task_id"], f"cancel-voided:{cancel['id']}:{voided[0]['id']}",
                                 i18n.t("dlg.cancel_voided", title=d["title"], items=items))
    return not accepted


def _send_cancel(conn: sqlite3.Connection, cfg: Config, kb: Kanban, d: dict[str, Any], msg: dict[str, Any]) -> str:
    if not _settle_for_cancel(conn, cfg, kb, d, msg):
        return _unused(conn, d, msg, "accepted_before_cancel")
    with tx(conn):
        conn.execute("UPDATE delegation_messages SET status='sending' WHERE id=?", (msg["id"],))
    result = kb.transition(d["card_id"], "archive")
    termination = result.get("termination")
    from . import child_links
    children_confirmed = child_links.cancel_owned(conn, kb, d)
    child_links.sync(conn, cfg, kb, d, quiet=True)
    confirmed = children_confirmed and (termination is None or bool(termination.get("terminated")))
    # Whatever the card did before the archive is kept as history; it changes nothing now.
    late = {"deliveries": 0, "questions": 0, "failures": 0}
    try:
        snap = kb.snapshot(d["card_id"], after_event=0)
    except KanbanError:
        if _open_approvals(conn, d["id"]):
            raise     # what their run did is on the card: finished by the retry (the archive is idempotent)
        snap = None
    with tx(conn):
        if snap is not None:
            _settle_on_cancel(conn, cfg, d, snap["events"])      # the cancel is still pending: nothing is paused
            unread = [e for e in snap["events"] if int(e["id"]) > int(d["event_cursor"] or 0)]
            _apply_snapshot(conn, cfg, d, {**snap, "events": unread}, late, quiet=True)
        conn.execute("UPDATE delegation_messages SET status='sent', sent_at=?, error=NULL WHERE id=?",
                     (clock.iso(), msg["id"]))
        _set(conn, d["id"], status="cancelled", card_status=result["status"],
             cancel_state="confirmed" if confirmed else "unconfirmed")
        if not confirmed:
            detail = ", ".join(f"{k}={v}" for k, v in sorted((termination or {"children": "termination or stable ownership not confirmed"}).items()) if v not in (None, False))
            text = i18n.t("dlg.cancel_unconfirmed", title=d["title"], detail=detail or "-")
            effects.task_comment(conn, d["task_id"], f"cancel-unconfirmed:{d['id']}", text)
        if not for_task_active_other(conn, d):
            _next_actor(conn, d["id"], "me", i18n.t("dlg.cancelled"))
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
    _accepted(conn, d, msg, result["status"])
    if result["outcome"] == "raced":
        _raced(conn, cfg, d, msg)
    return "sent"


def _accepted(conn: sqlite3.Connection, d: dict[str, Any], msg: dict[str, Any], card_status: str) -> None:
    with tx(conn):
        conn.execute("UPDATE delegation_messages SET status='sent', sent_at=?, error=NULL WHERE id=?",
                     (clock.iso(), msg["id"]))
        _set(conn, d["id"], status="ended", card_status=card_status, handled_at=clock.iso())
        if not for_task_active_other(conn, d):
            _next_actor(conn, d["id"], "me", i18n.t("dlg.accepted"))


def summary_line(d: dict[str, Any]) -> str:
    requested = ", pause requested" if d.get("pause_requested") and d["status"] != "paused" else ""
    return f"{d['id']} [{d['status']}{requested}] {d['title']} card={d.get('card_id') or '-'}"


def to_json(d: dict[str, Any]) -> str:
    return json.dumps(d, ensure_ascii=False, default=str)
