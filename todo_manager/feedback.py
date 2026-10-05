"""Human feedback: persisted before use, applied once, with its outcome recorded.

Sources: Notion comments / property edits / page edits (the feedback job), Matrix and CLI (the
main agent records what the user said with each change it makes, ``ops.context``). Each row keeps
the original text, source id + version, author, scope and outcome.

``pending`` rows need interpretation by the model (the feedback job wakes for them);
deterministic edits (e.g. a changed date property) are applied by code and stored as
``applied`` right away.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

from . import clock, effects, i18n, ids
from .db import dumps, loads, tx

ORIGINS = ("notion_comment", "notion_property", "notion_page", "matrix", "cli", "kanban")
FINAL = ("applied", "clarify", "ignored", "superseded", "failed")


class FeedbackError(ValueError):
    pass


def record(conn: sqlite3.Connection, *, origin: str, source_id: str, text: str | None, scope_type: str,
           scope_id: str | None = None, source_version: str = "", author: str | None = None,
           author_is_user: bool = False, kind: str | None = None, payload: dict[str, Any] | None = None,
           status: str = "pending", reply_to: str | None = None, result: Any = None) -> tuple[str, bool]:
    """Insert a feedback row. Returns (id, created). The same source id + version is stored once.

    ``author_is_user`` defaults to False: only a caller that verified the author (configured
    identity, or the user's own Matrix/CLI session) may mark feedback as the user's."""
    if origin not in ORIGINS:
        raise FeedbackError(f"invalid origin {origin!r}")
    fid = ids.new_id("feedback")
    now = clock.iso()
    with tx(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO feedback(id, origin, source_id, source_version, author, author_is_user, received_at,"
            " text, payload, scope_type, scope_id, kind, status, result, applied_at, rules_version, reply_to)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fid, origin, source_id, source_version, author, 1 if author_is_user else 0, now, text,
             dumps(payload or {}), scope_type, scope_id, kind, status,
             dumps(result) if result is not None else None, now if status in FINAL else None,
             None, reply_to))
        if cur.rowcount == 1:
            return fid, True
        row = conn.execute("SELECT id FROM feedback WHERE origin=? AND source_id=? AND source_version=?",
                           (origin, source_id, source_version)).fetchone()
        return row["id"], False


def get(conn: sqlite3.Connection, feedback_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM feedback WHERE id=?", (feedback_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["payload"] = loads(row["payload"], {})
    item["result"] = loads(row["result"], None)
    return item


def pending(conn: sqlite3.Connection, include_claimed: bool = False, stale_minutes: int = 60) -> list[dict[str, Any]]:
    sql = "SELECT * FROM feedback WHERE status='pending'"
    params: list[Any] = []
    if not include_claimed:
        cutoff = clock.iso(clock.now() - timedelta(minutes=stale_minutes))
        sql += " AND (claimed_run IS NULL OR claimed_at < ?)"
        params.append(cutoff)
    sql += " ORDER BY received_at"
    return [{**dict(r), "payload": loads(r["payload"], {})} for r in conn.execute(sql, params)]


def claim(conn: sqlite3.Connection, run_id: str, feedback_ids: list[str]) -> None:
    with tx(conn):
        for fid in feedback_ids:
            conn.execute("UPDATE feedback SET claimed_run=?, claimed_at=?, attempts=attempts+1 WHERE id=?"
                         " AND status='pending'", (run_id, clock.iso(), fid))


def claimed_by(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM feedback WHERE claimed_run=? AND status='pending' ORDER BY received_at",
                        (run_id,)).fetchall()
    return [{**dict(r), "payload": loads(r["payload"], {})} for r in rows]


def resolve(conn: sqlite3.Connection, feedback_id: str, status: str, result: Any = None,
            error: str | None = None) -> None:
    if status not in FINAL:
        raise FeedbackError(f"invalid final status {status!r}")
    conn.execute("UPDATE feedback SET status=?, result=?, applied_at=?, last_error=? WHERE id=?",
                 (status, dumps(result) if result is not None else None, clock.iso(), error, feedback_id))


def fail(conn: sqlite3.Connection, feedback_id: str, attempts: int) -> None:
    """Give up on an item no run resolved; the user is told on its task."""
    row = conn.execute("SELECT scope_type, scope_id FROM feedback WHERE id=?", (feedback_id,)).fetchone()
    resolve(conn, feedback_id, "failed", error=f"not resolved after {attempts} runs")
    if row is not None and row["scope_type"] == "task" and row["scope_id"]:
        effects.task_comment(conn, row["scope_id"], f"feedback-failed:{feedback_id}",
                             i18n.t("fb.failed_alert", n=attempts, id=feedback_id))


def release(conn: sqlite3.Connection, feedback_ids: list[str], max_attempts: int) -> dict[str, int]:
    """Claimed items a finished run left unresolved: offered to a later run, or failed once
    ``max_attempts`` runs had them (inside the caller's transaction)."""
    stats = {"released": 0, "failed": 0}
    for fid in feedback_ids:
        row = conn.execute("SELECT attempts FROM feedback WHERE id=? AND status='pending'", (fid,)).fetchone()
        if row is None:
            continue
        if row["attempts"] >= max_attempts:
            fail(conn, fid, row["attempts"])
            stats["failed"] += 1
        else:
            conn.execute("UPDATE feedback SET claimed_run=NULL, claimed_at=NULL WHERE id=?", (fid,))
            stats["released"] += 1
    return stats


def supersede(conn: sqlite3.Connection, feedback_ids: list[str], reason: str, by: str | None = None) -> int:
    """Pending rows (claimed or not) that a newer revision of the same text replaced or withdrew.

    A run that claimed one of them skips it at apply time; the reason and successor are kept."""
    n = 0
    for fid in feedback_ids:
        n += conn.execute("UPDATE feedback SET status='superseded', applied_at=?, result=? WHERE id=? AND status='pending'",
                          (clock.iso(), dumps({"reason": reason, "superseded_by": by}), fid)).rowcount
    return n


def supersede_older(conn: sqlite3.Connection, origin: str, source_id: str, keep_id: str,
                    kind: str | None = None, reason: str = "edited") -> int:
    """A newer revision replaces the unprocessed earlier ones of the same stream."""
    sql = "SELECT id FROM feedback WHERE origin=? AND source_id=? AND id != ? AND status='pending'"
    params: list[Any] = [origin, source_id, keep_id]
    if kind is not None:
        sql += " AND kind=?"
        params.append(kind)
    return supersede(conn, [r["id"] for r in conn.execute(sql, params)], reason, keep_id)


def recent(conn: sqlite3.Connection, scope_type: str, scope_id: str | None, limit: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM feedback WHERE scope_type=? AND scope_id IS ? ORDER BY received_at DESC LIMIT ?",
                        (scope_type, scope_id, limit)).fetchall()
    return [{**dict(r), "payload": loads(r["payload"], {}), "result": loads(r["result"], None)} for r in rows]
