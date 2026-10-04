"""Helpers that queue outbound effects with stable dedupe keys (see ``outbox``).

Pushes are idempotent "make Notion match local state" requests: a newer push supersedes
older pending ones for the same entity, and the handler diffs against the page, so the
key only needs to be unique per request.
"""

from __future__ import annotations

import sqlite3

from . import ids, outbox
from .config import Config


def task_push(conn: sqlite3.Connection, task_id: str) -> str | None:
    row = conn.execute("SELECT version FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return None
    return outbox.enqueue(conn, target="notion", op="push_task", entity_type="task", entity_id=task_id,
                          dedupe_key=f"push:task:{task_id}:v{row['version']}:{ids.new_id('outbox')}",
                          payload={"task_id": task_id}, supersede_prefix=f"push:task:{task_id}:")


def project_push(conn: sqlite3.Connection, project_id: str) -> str | None:
    row = conn.execute("SELECT version FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        return None
    return outbox.enqueue(conn, target="notion", op="push_project", entity_type="project", entity_id=project_id,
                          dedupe_key=f"push:project:{project_id}:v{row['version']}:{ids.new_id('outbox')}",
                          payload={"project_id": project_id}, supersede_prefix=f"push:project:{project_id}:")


def task_comment(conn: sqlite3.Connection, task_id: str, key: str, text: str, discussion_id: str | None = None) -> None:
    """A short bot comment on the task page. ``key`` identifies the logical comment once."""
    outbox.enqueue(conn, target="notion", op="comment", entity_type="task", entity_id=task_id,
                   dedupe_key=f"comment:{key}", payload={"task_id": task_id, "text": text,
                                                          "discussion_id": discussion_id, "key": key})


def page_comment(conn: sqlite3.Connection, page_id: str, key: str, text: str, discussion_id: str | None = None) -> None:
    """A bot comment on any page (e.g. a reply on the home page)."""
    outbox.enqueue(conn, target="notion", op="comment", entity_type="page", entity_id=page_id,
                   dedupe_key=f"comment:{key}", payload={"page_id": page_id, "text": text,
                                                          "discussion_id": discussion_id, "key": key})


def notify(conn: sqlite3.Connection, cfg: Config, key: str, text: str, task_id: str | None = None) -> None:
    """A short Matrix reminder; the task's Notion link is appended when it is sent."""
    if not cfg.get("notify.matrix"):
        return
    outbox.enqueue(conn, target="matrix", op="notify", dedupe_key=f"notify:{key}",
                   payload={"text": text, "task_id": task_id})
