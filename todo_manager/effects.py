"""Helpers that queue outbound effects with stable dedupe keys (see ``outbox``).

Pushes describe the latest local projection. Repeated scans reuse the same unfinished
projection and retain its attempts, uncertainty and retry deadline. A changed projection
can replace unsent work; uncertain older writes reconcile first. Hermes owns channel sends.
"""

from __future__ import annotations

import sqlite3

from . import ids, outbox, store
from .db import dumps, loads, get_meta
import hashlib


def _push(conn: sqlite3.Connection, kind: str, entity_id: str) -> str | None:
    entity = (store.get_task if kind == 'task' else store.get_project)(conn, entity_id)
    if entity is None:
        return None
    # Polling/provenance/link timestamps are not an outbound projection change.
    projection = {'entity': {k: v for k, v in entity.items()
                             if k not in ('version', 'updated_at', 'created_at', 'field_meta', 'notion_page_id')}}
    if kind == 'task':
        from . import delegation
        projection['pending_project'] = loads(get_meta(conn, 'notion.project_pending:' + entity_id), None)
        projection['label'] = delegation.task_label(conn, entity_id)
        projection['delegations'] = [dict(r) for r in conn.execute(
            'SELECT id,title,status FROM delegations WHERE task_id=? ORDER BY created_at,id', (entity_id,))]
        projection['evidence'] = store.evidence_for(conn, 'task', entity_id)
        projection['deliveries'] = [dict(r) for r in conn.execute(
            'SELECT dl.id,dl.seq FROM deliveries dl JOIN delegations d ON d.id=dl.delegation_id '
            'WHERE d.task_id=? ORDER BY dl.id', (entity_id,))]
    digest = hashlib.sha256(dumps(projection).encode()).hexdigest()
    old = conn.execute("SELECT id,status,payload FROM outbox WHERE target='notion' AND op=? "
                       "AND entity_id=? AND status!='superseded' ORDER BY rowid DESC LIMIT 1",
                       ('push_' + kind, entity_id)).fetchall()
    for row in old:
        if row['status'] == 'done':
            continue
        payload = loads(row['payload'], {})
        if payload.get('projection') == digest:
            return None
        if 'projection' not in payload:
            payload['projection'] = digest
            conn.execute('UPDATE outbox SET payload=? WHERE id=?', (dumps(payload), row['id']))
            return None
    return outbox.enqueue(conn, target='notion', op='push_' + kind, entity_type=kind, entity_id=entity_id,
                          dedupe_key=f'push:{kind}:{entity_id}:{digest}:{ids.new_id("outbox")}',
                          payload={kind + '_id': entity_id, 'projection': digest},
                          supersede_prefix=f'push:{kind}:{entity_id}:')


def task_push(conn: sqlite3.Connection, task_id: str) -> str | None:
    return _push(conn, 'task', task_id)


def project_push(conn: sqlite3.Connection, project_id: str) -> str | None:
    return _push(conn, 'project', project_id)


def task_comment(conn: sqlite3.Connection, task_id: str, key: str, text: str, discussion_id: str | None = None) -> None:
    """A short bot comment on the task page. ``key`` identifies the logical comment once."""
    outbox.enqueue(conn, target="notion", op="comment", entity_type="task", entity_id=task_id,
                   dedupe_key=f"comment:{key}", payload={"task_id": task_id, "text": text,
                                                          "discussion_id": discussion_id, "key": key, "comment_protocol": 1})


def page_comment(conn: sqlite3.Connection, page_id: str, key: str, text: str, discussion_id: str | None = None) -> None:
    """A bot comment on any page (e.g. a reply on the home page)."""
    outbox.enqueue(conn, target="notion", op="comment", entity_type="page", entity_id=page_id,
                   dedupe_key=f"comment:{key}", payload={"page_id": page_id, "text": text,
                                                          "discussion_id": discussion_id, "key": key, "comment_protocol": 1})
