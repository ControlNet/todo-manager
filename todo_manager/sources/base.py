"""Normalised source items, persistence and batch accumulation.

Fetch checkpoint vs. processed checkpoint:

* An adapter returns items plus the cursor that covers them. ``persist_fetch`` stores
  the items (with full bodies) and advances the cursor in ONE transaction, so the
  cursor never moves past content that is not durably saved.
* Items are later marked ``processed`` only when the semantic decisions that cover
  them are committed (see ``intake.apply_decisions``).
* A failed fetch records the error on the cursor and leaves the position untouched;
  freshness is reported, never treated as "no changes".
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

from .. import clock, ids
from ..db import dumps, loads, tx


@dataclass
class Item:
    source: str                      # gmail | slack | github | commit | manual
    external_id: str
    conversation_key: str            # batching unit (thread / channel / repo#n)
    account: str = ""
    version: str = ""                # edit version; "" for immutable messages
    kind: str = "message"            # message | edit | deletion | review | event | commit
    thread_key: str | None = None    # cross-source matter key (e.g. github:o/r#12)
    occurred_at: str | None = None
    author: str | None = None
    author_is_me: bool = False
    title: str | None = None
    url: str | None = None
    body: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    # How the item enters semantic processing:
    #   batch    -> accumulate into a conversation batch (default)
    #   digest   -> only consumed by a daily digest (commits)
    #   context  -> stored as context only, never wakes the model (e.g. my own reactions)
    #   if_open_task -> batch when the matter is already evidence of an open task (so that task
    #               gets updated), otherwise context (e.g. my own PR was merged)
    #   superseded -> duplicate of an item another source covers (kept for audit)
    route: str = "batch"

    def excerpt(self, limit: int = 400) -> str:
        text = " ".join(self.body.split())
        return text[:limit]


@dataclass
class FetchResult:
    scope: str
    items: list[Item]
    cursor: str | None               # new cursor (None = unchanged)
    complete: bool = True            # False when more pages remain for the next run
    meta: dict[str, Any] = field(default_factory=dict)


class SourceError(Exception):
    """Fetch failed; the cursor must not advance."""

    def __init__(self, message: str, retry_after: float | None = None, cursor_invalid: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.cursor_invalid = cursor_invalid


def get_cursor(conn: sqlite3.Connection, source: str, scope: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM cursors WHERE source=? AND scope=?", (source, scope)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["meta"] = loads(row["meta"], {})
    return item


def record_fetch_error(conn: sqlite3.Connection, source: str, scope: str, error: str) -> None:
    now = clock.iso()
    with tx(conn):
        conn.execute(
            "INSERT INTO cursors(source, scope, last_attempt_at, last_error, consecutive_failures)"
            " VALUES (?,?,?,?,1) ON CONFLICT(source, scope) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,"
            " last_error=excluded.last_error, consecutive_failures=cursors.consecutive_failures+1,"
            " meta=json_remove(COALESCE(cursors.meta, '{}'), '$.retired')",
            (source, scope, now, error[:1000]))


def retire_scopes(conn: sqlite3.Connection, source: str, scopes: set[str],
                  prefix_of: Any = None) -> dict[str, int]:
    """Scopes that left a discovered list (e.g. a blacklisted channel): keep the cursor, stop
    reporting it as stale. Reading the scope again clears the mark (``persist_fetch`` replaces meta).
    With ``prefix_of(scope)`` (its conversation key prefix), batches not yet given to a run are
    withdrawn: the batch fails with the reason, its items become ``superseded`` (kept, not
    analysed). Returns withdrawn item counts per scope."""
    withdrawn: dict[str, int] = {}
    with tx(conn):
        for scope in sorted(scopes):
            conn.execute("UPDATE cursors SET meta=json_set(COALESCE(meta, '{}'), '$.retired', json('true'))"
                         " WHERE source=? AND scope=?", (source, scope))
            prefix = prefix_of(scope) if prefix_of else None
            if not prefix:
                continue
            batch_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM batches WHERE source=? AND state IN ('accumulating','mature')"
                " AND (conversation_key=? OR conversation_key LIKE ? ESCAPE '\\')",
                (source, prefix, prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ":%"))]
            count = 0
            for batch_id in batch_ids:
                count += conn.execute("UPDATE source_items SET state='superseded' WHERE batch_id=? AND state='batched'",
                                      (batch_id,)).rowcount
                conn.execute("UPDATE batches SET state='failed', last_error=? WHERE id=?",
                             (f"withdrawn: {scope} left the discovered list (blacklisted or left)", batch_id))
            withdrawn[scope] = count
    return withdrawn


def persist_fetch(conn: sqlite3.Connection, source: str, result: FetchResult) -> dict[str, int]:
    """Store items and advance the cursor atomically. Returns counters."""
    stats = {"new": 0, "duplicate": 0, "batched": 0}
    now = clock.iso()
    with tx(conn):
        for item in result.items:
            if item.route == "if_open_task":
                item = replace(item, route="batch" if _open_task_matter(conn, item) else "context")
            if item.route == "batch" and _copy_of_github_event(conn, item):
                item = replace(item, route="superseded")
            inserted_id = _insert_item(conn, item, now)
            if inserted_id is None:
                stats["duplicate"] += 1
                continue
            stats["new"] += 1
            if item.route == "batch":
                attach_to_batch(conn, inserted_id, item.source, item.conversation_key,
                                item.occurred_at or now)
                stats["batched"] += 1
        meta = dict(result.meta)
        meta["complete"] = result.complete
        conn.execute(
            "INSERT INTO cursors(source, scope, cursor, last_attempt_at, last_success_at, last_error,"
            " consecutive_failures, meta) VALUES (?,?,?,?,?,NULL,0,?)"
            " ON CONFLICT(source, scope) DO UPDATE SET cursor=COALESCE(excluded.cursor, cursors.cursor),"
            " last_attempt_at=excluded.last_attempt_at, last_success_at=excluded.last_success_at,"
            " last_error=NULL, consecutive_failures=0, meta=excluded.meta",
            (source, result.scope, result.cursor, now, now, dumps(meta)))
    return stats


def _copy_of_github_event(conn: sqlite3.Connection, item: Item) -> bool:
    """The same GitHub event (``github_event``: opened, comment/review/review_comment/event id)
    of the same matter already read as news through the other channel (API or notification
    mail): the later copy is kept for audit but not batched. The matter key only groups; an
    item whose event is unknown is never treated as a copy."""
    event = item.meta.get("github_event")
    if not item.conversation_key.startswith("github:") or not event:
        return False
    rows = conn.execute("SELECT meta FROM source_items WHERE conversation_key=? AND source != ?"
                        " AND state != 'superseded'", (item.conversation_key, item.source)).fetchall()
    for r in rows:
        meta = loads(r["meta"], {})
        if meta.get("github_event") == event and meta.get("route") == "batch":
            return True
    return False


def _open_task_matter(conn: sqlite3.Connection, item: Item) -> bool:
    """Is this item's matter (thread key, else conversation) evidence of a task that is still open?"""
    key = item.thread_key or item.conversation_key
    return conn.execute(
        "SELECT 1 FROM evidence_links e JOIN source_items s ON s.id=e.source_item_id"
        " JOIN tasks t ON t.id=e.entity_id WHERE e.entity_type='task' AND t.status NOT IN ('done','cancelled')"
        " AND (s.thread_key=? OR s.conversation_key=?) LIMIT 1", (key, key)).fetchone() is not None


def _insert_item(conn: sqlite3.Connection, item: Item, now: str) -> str | None:
    item_id = ids.new_id("source")
    state = {"batch": "pending", "digest": "pending", "context": "processed",
             "superseded": "superseded"}[item.route]
    meta = dict(item.meta)
    meta["route"] = item.route
    cur = conn.execute(
        "INSERT OR IGNORE INTO source_items(id, source, account, external_id, version, kind, thread_key,"
        " conversation_key, occurred_at, author, author_is_me, title, url, content_hash, excerpt, meta,"
        " fetched_at, state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (item_id, item.source, item.account, item.external_id, item.version, item.kind, item.thread_key,
         item.conversation_key, item.occurred_at, item.author, 1 if item.author_is_me else 0, item.title,
         item.url, ids.content_hash(item.title, item.body), item.excerpt(), dumps(meta), now, state))
    if cur.rowcount != 1:
        return None
    conn.execute("INSERT INTO source_bodies(item_id, body) VALUES (?,?)", (item_id, item.body))
    return item_id


def attach_to_batch(conn: sqlite3.Connection, item_id: str, source: str, conversation_key: str,
                    activity_at: str) -> str:
    """Append an item to the open (accumulating) batch of its conversation, creating it."""
    row = conn.execute(
        "SELECT * FROM batches WHERE source=? AND conversation_key=? AND state='accumulating'",
        (source, conversation_key)).fetchone()
    now = clock.iso()
    if row is None:
        batch_id = ids.new_id("batch")
        conn.execute(
            "INSERT INTO batches(id, source, conversation_key, state, first_item_at, last_item_at,"
            " last_activity_at, created_at) VALUES (?,?,?,'accumulating',?,?,?,?)",
            (batch_id, source, conversation_key, activity_at, activity_at, activity_at, now))
    else:
        batch_id = row["id"]
        last = max(filter(None, [row["last_activity_at"], activity_at]))
        conn.execute("UPDATE batches SET last_item_at=?, last_activity_at=? WHERE id=?",
                     (last, last, batch_id))
    conn.execute("UPDATE source_items SET state='batched', batch_id=? WHERE id=?", (batch_id, item_id))
    return batch_id


def quote(text: str) -> str:
    """Render untrusted source text as a quote block, so none of its lines can pose as a
    section or instruction of the surrounding model context."""
    return "\n".join(f"> {line}" if line.strip() else ">" for line in (text or "").splitlines()) or ">"


def item_body(conn: sqlite3.Connection, item_id: str) -> str:
    row = conn.execute("SELECT body FROM source_bodies WHERE item_id=?", (item_id,)).fetchone()
    return row["body"] if row else ""


def items_for_batch(conn: sqlite3.Connection, batch_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM source_items WHERE batch_id=? ORDER BY occurred_at, fetched_at",
                        (batch_id,)).fetchall()
    return [{**dict(r), "meta": loads(r["meta"], {})} for r in rows]


def conversation_history(conn: sqlite3.Connection, source: str, conversation_key: str,
                         exclude_batch: str | None = None, limit: int = 30,
                         before: str | None = None) -> list[dict[str, Any]]:
    """Latest messages of a conversation (oldest first), optionally only those before ``before``
    (the last new message of a batch: a later segment's messages are not shown as earlier)."""
    rows = conn.execute(
        "SELECT * FROM source_items WHERE source=? AND conversation_key=? AND (batch_id IS NULL OR batch_id != ?)"
        " AND state IN ('processed','batched','pending') AND (? IS NULL OR occurred_at < ?)"
        " ORDER BY occurred_at DESC LIMIT ?",
        (source, conversation_key, exclude_batch or "", before, before, limit)).fetchall()
    return [{**dict(r), "meta": loads(r["meta"], {})} for r in reversed(rows)]


def conversation_root(conn: sqlite3.Connection, source: str, conversation_key: str,
                      exclude_batch: str | None = None, before: str | None = None) -> dict[str, Any] | None:
    """The first stored message of a conversation (an issue's body, a thread's parent)."""
    row = conn.execute(
        "SELECT * FROM source_items WHERE source=? AND conversation_key=? AND (batch_id IS NULL OR batch_id != ?)"
        " AND state IN ('processed','batched','pending') AND (? IS NULL OR occurred_at < ?)"
        " ORDER BY occurred_at, fetched_at LIMIT 1",
        (source, conversation_key, exclude_batch or "", before, before)).fetchone()
    return {**dict(row), "meta": loads(row["meta"], {})} if row else None


def known_external_ids(conn: sqlite3.Connection, source: str, account: str, external_ids: Iterable[str]) -> set[str]:
    keys = list(set(external_ids))
    found: set[str] = set()
    for start in range(0, len(keys), 500):
        chunk = keys[start:start + 500]
        marks = ",".join("?" for _ in chunk)
        found.update(r["external_id"] for r in conn.execute(
            f"SELECT external_id FROM source_items WHERE source=? AND account=? AND external_id IN ({marks})",
            (source, account, *chunk)))
    return found
