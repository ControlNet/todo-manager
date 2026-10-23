"""Durable outbound side effects.

Anything that changes an external system (Notion page/comment, Kanban card, Matrix
notification) is first written here inside the same local transaction as the state
change that caused it. A deterministic worker then performs the effect with retries.

* ``dedupe_key`` is fixed per logical effect, so re-enqueueing is a no-op.
* A request whose outcome is unknown (timeout after send) becomes ``uncertain`` and is
  reconciled by its handler (e.g. by searching for the marker it embedded) before any
  retry, so a lost response never produces a duplicate comment or block. It stays
  ``uncertain`` until a reconciling attempt settles it (a result, or ``Permanent``): a
  failed lookup is retried as another reconciliation, never as a blind replay.
* Failures keep the payload and back off; they never re-run the semantic work.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any, Callable

from . import clock, ids
from .db import dumps, loads, tx

MAX_ATTEMPTS = 12


class Uncertain(Exception):
    """The effect may or may not have happened (e.g. timeout after sending)."""


class Retryable(Exception):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class Permanent(Exception):
    """The effect can never succeed as specified (e.g. page deleted)."""


def enqueue(conn: sqlite3.Connection, *, target: str, op: str, dedupe_key: str, payload: dict[str, Any],
            entity_type: str | None = None, entity_id: str | None = None, supersede_prefix: str | None = None) -> str | None:
    """Queue an effect. Returns the outbox id, or None if the dedupe key already exists.

    ``supersede_prefix`` marks older *pending* or given-up effects with that key prefix as
    superseded (e.g. an older full re-render of the same Notion block that was never sent).
    """
    now = clock.iso()
    with tx(conn):
        next_attempt = now
        if supersede_prefix:
            delayed = conn.execute("SELECT MAX(next_attempt_at) FROM outbox WHERE target=? AND op=? "
                                   "AND entity_id=? AND status IN ('pending','failed','uncertain','in_flight')",
                                   (target, op, entity_id)).fetchone()[0]
            if delayed and delayed > next_attempt:
                next_attempt = delayed
        if supersede_prefix:
            conn.execute(
                "UPDATE outbox SET status='superseded', updated_at=? WHERE status IN ('pending','failed','dead')"
                " AND dedupe_key LIKE ? AND dedupe_key != ?",
                (now, supersede_prefix + "%", dedupe_key))
        oid = ids.new_id("outbox")
        cur = conn.execute(
            "INSERT OR IGNORE INTO outbox(id, target, op, entity_type, entity_id, dedupe_key, payload, status,"
            " attempts, next_attempt_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,'pending',0,?,?,?)",
            (oid, target, op, entity_type, entity_id, dedupe_key, dumps(payload), next_attempt, now, now))
        return oid if cur.rowcount == 1 else None


def due(conn: sqlite3.Connection, target: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    now = clock.iso()
    sql = ("SELECT * FROM outbox WHERE status IN ('pending','failed','uncertain','in_flight')"
           " AND (next_attempt_at IS NULL OR next_attempt_at <= ?)")
    params: list[Any] = [now]
    if target:
        sql += " AND target=?"
        params.append(target)
    sql += (" AND NOT EXISTS (SELECT 1 FROM outbox older WHERE older.target=outbox.target "
            "AND older.op=outbox.op AND older.entity_id=outbox.entity_id AND older.rowid<outbox.rowid "
            "AND outbox.op IN ('push_task','push_project') "
            "AND older.status IN ('uncertain','in_flight')) ORDER BY created_at LIMIT ?")
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [{**dict(r), "payload": loads(r["payload"], {})} for r in rows]


def pending_count(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT target, status, COUNT(*) AS n FROM outbox WHERE status NOT IN ('done','superseded')"
        " GROUP BY target, status").fetchall()
    return {f"{r['target']}:{r['status']}": r["n"] for r in rows}


Handler = Callable[[dict[str, Any], bool], Any]


def process(conn: sqlite3.Connection, handlers: dict[tuple[str, str], Handler], target: str | None = None,
            limit: int = 50) -> dict[str, int]:
    """Run due effects. ``handler(item, reconcile)`` returns a result (stored) or raises.

    ``reconcile`` is True when a previous attempt was in flight/uncertain: the handler
    must first check whether the effect already happened.
    """
    stats = {"done": 0, "failed": 0, "uncertain": 0, "dead": 0, "skipped": 0}
    for item in due(conn, target, limit):
        handler = handlers.get((item["target"], item["op"]))
        if handler is None:
            stats["skipped"] += 1
            continue
        reconcile = item["status"] in ("uncertain", "in_flight")
        with tx(conn):
            conn.execute("UPDATE outbox SET status='in_flight', attempts=attempts+1, updated_at=? WHERE id=?",
                         (clock.iso(), item["id"]))
        try:
            result = handler(item, reconcile)
        except Uncertain as exc:
            _mark(conn, item, "uncertain", str(exc), delay=60)
            stats["uncertain"] += 1
        except Permanent as exc:
            _mark(conn, item, "dead", str(exc), delay=None)
            stats["dead"] += 1
        except Retryable as exc:
            _mark(conn, item, "failed", str(exc), delay=exc.retry_after, reconcile=reconcile)
            stats["uncertain" if reconcile else "failed"] += 1
        except Exception as exc:  # noqa: BLE001 - unknown errors are retried with backoff
            _mark(conn, item, "failed", f"{type(exc).__name__}: {exc}", delay=None, backoff=True, reconcile=reconcile)
            stats["uncertain" if reconcile else "failed"] += 1
        else:
            with tx(conn):
                conn.execute("UPDATE outbox SET status='done', result=?, last_error=NULL, updated_at=? WHERE id=?",
                             (dumps(result) if result is not None else None, clock.iso(), item["id"]))
            stats["done"] += 1
    return stats


def _mark(conn: sqlite3.Connection, item: dict[str, Any], status: str, error: str, *, delay: float | None,
          backoff: bool = True, reconcile: bool = False) -> None:
    attempts = item["attempts"] + 1
    if status == "failed" and attempts >= MAX_ATTEMPTS:
        status = "dead"
    elif status == "failed" and reconcile:
        status = "uncertain"   # the reconciliation itself failed: the earlier outcome is still unknown
    if delay is None and backoff:
        delay = min(3600, 30 * (2 ** min(attempts, 7)))
    next_at = clock.iso(clock.now() + timedelta(seconds=delay)) if delay else None
    with tx(conn):
        conn.execute("UPDATE outbox SET status=?, last_error=?, next_attempt_at=?, updated_at=? WHERE id=?",
                     (status, error[:2000], next_at, clock.iso(), item["id"]))
