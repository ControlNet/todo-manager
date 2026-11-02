"""What the Web UI writes, always as the user (it is reached only with the user's login).

* A button on a task is the user's edit of its fields, applied like the same edit on the Notion
  page or board (``board``): Done / My to-do / Cancel take effect at once (and settle agent
  work), Assign to Agent becomes feedback for the feedback job. What else the user decides is
  said to Hermes in a comment, as the board has no Agree / Decline since 2026-10-12.
* Pause / resume of agent work are the user's requests (``ops.delegate_message``).
* What the user types is a comment on the task: feedback for the feedback job, like a Notion
  comment; ``to: agent`` marks it as meant for the agent working on the task.

Each write carries a request id chosen by the page (``rid``): sending it again (a retry after a
lost response) does nothing more.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .. import board, delegation, effects, feedback, ops, store
from ..config import Config
from ..db import get_meta, tx
from ..i18n import t
from . import views

RID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MAX_TEXT = 8000
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AGENT_KINDS = ("answer", "review", "running")


class ActionError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def _lang(conn: sqlite3.Connection, cfg: Config) -> str:
    """Feedback and closing reasons are written in the language of the Notion structure."""
    return get_meta(conn, "notion.language") or cfg.language


def _date(params: dict[str, Any], key: str, *, optional: bool = False) -> str | None:
    value = params.get(key)
    if value in (None, "") and optional:
        return None
    if not isinstance(value, str) or not DATE.match(value):
        raise ActionError("INVALID", f"{key} must be a date YYYY-MM-DD")
    return value


def _text(value: Any, name: str, *, required: bool = False) -> str | None:
    text = value.strip() if isinstance(value, str) else ""
    if required and not text:
        raise ActionError("INVALID", f"{name} is required")
    if len(text) > MAX_TEXT:
        raise ActionError("INVALID", f"{name} is longer than {MAX_TEXT} characters")
    return text or None


def check_rid(rid: Any) -> str:
    if not isinstance(rid, str) or not RID.match(rid):
        raise ActionError("INVALID", "rid: a request id of 8-64 letters, digits, - or _")
    return rid


def changes_for(conn: sqlite3.Connection, task: dict[str, Any], action: str, params: dict[str, Any],
                ) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The field edit (with board choices) a button stands for, and a hand-over request."""
    kind, _ = views.kind_of(conn, task)
    on_board = kind == "decision"
    agent_work = kind in AGENT_KINDS   # on the Agent progress board
    if action == "agent":
        request = {"note": _text(params.get("note"), "note"),
                   "done_criteria": _text(params.get("done_criteria"), "done_criteria"),
                   "start_after": _text(params.get("start_after"), "start_after")}
        request = {k: v for k, v in request.items() if v} or None
        if on_board:
            return {"decision": "agent"}, request
        if task["status"] in store.CLOSED_STATUSES:
            raise ActionError("INVALID", "the task is closed: reopen it first")
        status = task["status"] if task["status"] in ("todo", "in_progress") else "todo"
        return {"status": status, "next_actor": "agent"}, request
    if action in ("todo", "done", "cancel"):
        if on_board:
            return {"decision": action}, None
        if agent_work:
            return {"agent_progress": action}, None
        if action == "todo":
            return {"status": "todo", "next_actor": "me"}, None
        return {"status": "done" if action == "done" else "cancelled"}, None
    if action == "reopen":
        return {"status": "todo", "next_actor": "me"}, None
    if action == "snooze":
        return {"snooze_until": _date(params, "until", optional=True)}, None
    if action == "due":
        return {"due_date": _date(params, "date", optional=True)}, None
    if action == "follow_up":
        return {"follow_up_date": _date(params, "date", optional=True)}, None
    if action == "focus":
        return {"focus": 1 if params.get("on") else 0}, None
    if action == "wait":
        return {"status": "waiting", "next_actor": "external",
                "waiting_on": _text(params.get("on"), "on", required=True),
                "follow_up_date": _date(params, "follow_up", optional=True)}, None
    if action == "project":
        project = params.get("project") or None
        if project is not None and store.get_project(conn, project) is None:
            raise ActionError("NOT_FOUND", f"no project {project}", 404)
        return {"project_id": project}, None
    raise ActionError("INVALID", f"unknown action {action!r}")


def act(conn: sqlite3.Connection, cfg: Config, task_id: str, action: str, params: dict[str, Any],
        rid: str) -> dict[str, Any]:
    rid = check_rid(rid)
    task = store.get_task(conn, task_id)
    if task is None:
        raise ActionError("NOT_FOUND", f"no task {task_id}", 404)
    if action in ("pause", "resume"):
        return _pause(conn, cfg, task, action, rid)
    changed, request = changes_for(conn, task, action, params)
    return edit(conn, cfg, task_id, changed, rid=rid, request=request, action=action)


def edit(conn: sqlite3.Connection, cfg: Config, task_id: str, changed: dict[str, Any], *, rid: str,
         request: dict[str, Any] | None = None, action: str | None = None) -> dict[str, Any]:
    lang = _lang(conn, cfg)
    with tx(conn):
        before = store.get_task(conn, task_id)
        payload: dict[str, Any] = {"changes": changed, "before": {k: before.get(k) for k in changed}}
        if action:
            payload["action"] = action
        if request:
            payload["request"] = request
        fid, created = feedback.record(conn, origin="web", source_id=f"edit:{task_id}", source_version=rid, text=None,
                                       scope_type="task", scope_id=task_id, kind="property_edit", author="user",
                                       author_is_user=True, payload=payload, status="applied")
        if not created:
            return {"task": task_id, "feedback": fid, "duplicate": True}
        prepared = board.prepare(conn, task_id, changed, lang)
        try:
            result, followups = board.apply(conn, task_id, prepared, changed, fid=fid, author="user", origin="web",
                                            source_id=task_id, origin_label="web", lang=lang, request=request)
        except store.StoreError as exc:
            raise ActionError("INVALID", str(exc)) from None
        feedback.resolve(conn, fid, "applied", {"applied": result.applied, "rejected": result.rejected,
                                                "followups": followups})
        effects.task_push(conn, task_id)
    return {"task": task_id, "feedback": fid, "applied": sorted(result.applied), "rejected": result.rejected,
            "followups": followups}


def _pause(conn: sqlite3.Connection, cfg: Config, task: dict[str, Any], action: str, rid: str) -> dict[str, Any]:
    active = delegation.for_task(conn, task["id"], active_only=True)
    if not active:
        raise ActionError("INVALID", "the task has no agent work to pause or resume")
    said = ("pause" if action == "pause" else "resume") + " the agent work on: " + task["title"]
    try:
        ctx = ops.context(conn, cfg, request=f"web-{rid}", said=said, origin="web", scope=f"task:{task['id']}")
        return ops.delegate_message(conn, cfg, ctx, active[-1]["id"], kind=action, body=said)
    except ops.OpError as exc:
        raise ActionError(exc.code, "; ".join(exc.details)) from None


def comment(conn: sqlite3.Connection, cfg: Config, task_id: str, text: Any, to: Any, rid: Any) -> dict[str, Any]:
    """What the user wrote on a task: for the feedback job (to Hermes, or meant for the agent).
    A copy goes to the task's Notion page (as the integration, marked as the user's), and the
    reply is posted in its discussion (``ops._queue_reply``)."""
    rid = check_rid(rid)
    if store.get_task(conn, task_id) is None:
        raise ActionError("NOT_FOUND", f"no task {task_id}", 404)
    body = _text(text, "text", required=True)
    if to not in ("hermes", "agent"):
        raise ActionError("INVALID", "to is hermes or agent")
    with tx(conn):
        fid, created = feedback.record(conn, origin="web", source_id=f"comment:{task_id}", source_version=rid,
                                       text=body, scope_type="task", scope_id=task_id, kind="comment", author="user",
                                       author_is_user=True, payload={"to": to} if to == "agent" else {})
        if created:
            said = t("web.comment_to_agent" if to == "agent" else "web.comment", _lang(conn, cfg), text=body)
            effects.task_comment(conn, task_id, f"web:{fid}", said)
    return {"feedback": fid, "duplicate": not created}
