"""A verified user's edit of a task, the same from every surface that offers it (a Notion page or
board, the Web UI).

The decision board's choices (Cancel, Done, My to-do) and the Agent progress board's (Done, My
to-do, Cancel) take effect here without a model run; Assign to Agent, Agree and Decline become
feedback for the feedback job (``delegate_request`` / ``decision_choice``), as does setting the
next actor to Agent while no agent work is active. Closing a task stops its agent work
(``delegation.on_task_closed``). The caller records the edit itself as feedback first (``fid``)
and resolves it with the returned follow-ups.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from . import delegation, feedback, store
from .db import loads
from .i18n import t
from .notion import schema

BOARD_STATUS = {"cancel": "cancelled", "done": "done", "todo": "todo"}   # board choice -> status


@dataclass
class Edit:
    local: dict[str, Any]            # the field changes to write
    choice: str | None               # the decision-board choice, if any
    progress: str | None             # the Agent progress board choice, if any
    closed_reason: str | None        # set when a choice closes the task


def prepare(conn: sqlite3.Connection, task_id: str, changed: dict[str, Any], lang: str) -> Edit:
    """Turn the edited values (``decision`` / ``agent_progress`` hold a board choice) into field
    changes. A status set in the same edit wins over a choice."""
    local = dict(changed)
    choice = local.get('decision') if 'decision' in local else None
    if choice == 'agent':
        existing = store.get_task(conn, task_id)
        desired_status = local.get('status', existing['status'])
        if existing['status'] not in store.CLOSED_STATUSES and desired_status not in store.CLOSED_STATUSES:
            local.update(status=desired_status if desired_status in ('todo', 'in_progress') else 'todo',
                         next_actor='agent')
        local['decision'] = None
    closed_reason = None
    # The Agent progress board: Done / My to-do / Cancel act like the decision board's choices
    # and also settle the agent work (``delegation.board_choice``); the program's own columns
    # are not choices.
    progress = local.pop('agent_progress', None) if 'agent_progress' in local else None
    progress = progress if progress in BOARD_STATUS else None
    if progress and 'status' not in local:
        local['status'] = BOARD_STATUS[progress]
        if progress == 'todo':
            local['next_actor'] = 'me'
        elif local['status'] in store.CLOSED_STATUSES:
            closed_reason = t('sync.agent_board_closed', lang, choice=schema.label('agent_progress', progress, lang))
    if choice in BOARD_STATUS:
        # The choice is cleared, so the handled card leaves the board.
        if 'status' not in local:
            local['status'] = BOARD_STATUS[choice]
            if choice == 'todo':
                local['next_actor'] = 'me'
            elif local['status'] in store.CLOSED_STATUSES:
                closed_reason = t('sync.board_closed', lang, choice=schema.label('decision', choice, lang))
        local['decision'] = None
    return Edit(local, choice, progress, closed_reason)


def apply(conn: sqlite3.Connection, task_id: str, edit: Edit, changed: dict[str, Any], *, fid: str,
          author: str | None, origin: str, source_id: str, origin_label: str, lang: str,
          request: dict[str, Any] | None = None) -> tuple[store.WriteResult, list[str]]:
    """Write ``edit`` as the user (inside the caller's transaction) and queue what needs the
    model. ``origin`` / ``source_id``: where derived feedback comes from; ``origin_label``: the
    history origin of the field changes (notion, web); ``request``: what the user added to a
    hand-over to the agent (``note``, ``done_criteria``, ``start_after``), all optional.
    Returns the write result and follow-ups."""
    before = store.get_task(conn, task_id)
    choice, progress = edit.choice, edit.progress
    board_notes = delegation.board_choice(conn, task_id, progress, fid) if progress else []
    result = store.update_task(conn, task_id, edit.local, actor="human", origin=origin_label, cause_ref=fid)
    if edit.closed_reason and store.get_task(conn, task_id)['status'] in store.CLOSED_STATUSES:
        store.set_closed_reason(conn, task_id, edit.closed_reason)
    after = store.get_task(conn, task_id)
    if 'decision' in changed:
        stale = [r['id'] for r in conn.execute(
            "SELECT id,payload FROM feedback WHERE scope_type='task' AND scope_id=? AND status='pending'",
            (task_id,)) if loads(r['payload'], {}).get('decision_event')]
        feedback.supersede(conn, stale, 'replaced by a newer decision', by=fid)
        # Preserve creation-body completeness while replacing the entire interpretation
        # identity when its original choice is withdrawn.
        creations = conn.execute(
            "SELECT * FROM feedback WHERE scope_type='task' AND scope_id=? "
            "AND kind='new_task' AND status='pending'", (task_id,)).fetchall()
        for creation in creations:
            payload = loads(creation['payload'], {})
            payload['values'] = {**payload.get('values', {}), **edit.local,
                                 **({'decision': choice} if choice else {})}
            text = _with_request(creation['text'] or '', payload, request if choice == 'agent' else None) or None
            replacement, _ = feedback.record(
                conn, origin='notion_page', source_id=creation['source_id'],
                source_version='creation-choice:' + fid, text=text,
                scope_type='task', scope_id=task_id, kind='new_task', payload=payload,
                author=author, author_is_user=True)
            feedback.supersede(conn, [creation['id']], 'creation choice changed', by=replacement)
        if not creations and after['status'] not in store.CLOSED_STATUSES and choice in ('agree', 'decline', 'agent'):
            payload = {'decision_event': fid, 'decision': choice, 'task_version': after['version']}
            text = _with_request(t('sync.decision_choice', lang, choice=schema.label('decision', choice, lang)),
                                 payload, request if choice == 'agent' else None)
            feedback.record(conn, origin=origin, source_id=source_id, source_version='decision:' + fid,
                            scope_type='task', scope_id=task_id,
                            kind='delegate_request' if choice == 'agent' else 'decision_choice',
                            author=author, author_is_user=True, text=text, payload=payload)
    # A board drag already queues the same durable delegation request.
    semantic_changes = dict(changed)
    if choice == 'agent':
        semantic_changes.pop('next_actor', None)
    if progress:
        semantic_changes.update({k: edit.local[k] for k in ('status', 'next_actor') if k in edit.local})
        semantic_changes.pop('next_actor', None)   # settled by board_choice, not a delegate request
    return result, board_notes + semantics(conn, before, after, semantic_changes, fid, author, origin=origin,
                                           origin_label=origin_label, lang=lang,
                                           request=request)


def semantics(conn: sqlite3.Connection, before: dict[str, Any], after: dict[str, Any], changed: dict[str, Any],
              fid: str, author: str | None, *, origin: str, origin_label: str, lang: str,
              request: dict[str, Any] | None = None) -> list[str]:
    """Deterministic consequences of a verified user's edit; ambiguous ones go to the model as
    derived feedback that keeps the original author."""
    notes: list[str] = []
    active = delegation.for_task(conn, after["id"], active_only=True)
    if "status" in changed and after["status"] in store.CLOSED_STATUSES:
        status = schema.label("task_status", after["status"], lang)
        key = "sync.closed_in_notion" if origin_label == "notion" else "web.closed"
        return delegation.on_task_closed(conn, after["id"], after["status"], t(key, lang, status=status), fid)
    field = schema.prop("task", "next_actor").name(lang)
    if "next_actor" in changed and after["next_actor"] == "agent" and before["next_actor"] != "agent" and not active:
        payload: dict[str, Any] = {"from_feedback": fid}
        text = _with_request(t("sync.set_to_agent", lang, field=field), payload, request)
        feedback.record(conn, origin=origin, source_id=after["id"], source_version=f"delegate:{fid}", text=text,
                        scope_type="task", scope_id=after["id"], kind="delegate_request", author=author,
                        author_is_user=True, payload=payload)
        notes.append("delegate_request")
    elif "next_actor" in changed and before["next_actor"] == "agent" and after["next_actor"] != "agent" and active:
        feedback.record(conn, origin=origin, source_id=after["id"], source_version=f"unassign:{fid}",
                        text=t("sync.unassigned", lang, field=field), scope_type="task", scope_id=after["id"],
                        kind="delegate_unassigned", author=author, author_is_user=True,
                        payload={"from_feedback": fid, "delegations": [d["id"] for d in active]})
        notes.append("delegate_unassigned")
    return notes


def _with_request(text: str, payload: dict[str, Any], request: dict[str, Any] | None) -> str:
    """Add what the user wrote with a hand-over: the note to the text, the rest to the payload."""
    request = request or {}
    payload.update({k: request[k] for k in ("done_criteria", "start_after") if request.get(k)})
    return text + "\n" + request["note"] if request.get("note") else text
