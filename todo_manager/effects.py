"""Helpers that queue outbound effects with stable dedupe keys (see ``outbox``)."""

from __future__ import annotations

import sqlite3

from . import outbox
from .config import Config


def task_push(conn: sqlite3.Connection, task_id: str) -> None:
    row = conn.execute("SELECT version FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return
    outbox.enqueue(conn, target="notion", op="push_task", entity_type="task", entity_id=task_id,
                   dedupe_key=f"push:task:{task_id}:v{row['version']}", payload={"task_id": task_id},
                   supersede_prefix=f"push:task:{task_id}:")


def project_push(conn: sqlite3.Connection, project_id: str) -> None:
    row = conn.execute("SELECT version FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        return
    outbox.enqueue(conn, target="notion", op="push_project", entity_type="project", entity_id=project_id,
                   dedupe_key=f"push:project:{project_id}:v{row['version']}", payload={"project_id": project_id},
                   supersede_prefix=f"push:project:{project_id}:")


def task_comment(conn: sqlite3.Connection, task_id: str, key: str, text: str, discussion_id: str | None = None) -> None:
    """A short bot comment on the task page. ``key`` identifies the logical comment once."""
    outbox.enqueue(conn, target="notion", op="comment", entity_type="task", entity_id=task_id,
                   dedupe_key=f"comment:{key}", payload={"task_id": task_id, "text": text,
                                                          "discussion_id": discussion_id, "key": key})


def notify(conn: sqlite3.Connection, cfg: Config, key: str, text: str) -> None:
    if not cfg.get("notify.matrix"):
        return
    outbox.enqueue(conn, target="matrix", op="notify", dedupe_key=f"notify:{key}", payload={"text": text})
