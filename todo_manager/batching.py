"""Batch maturity and wake policy for the intake entry point (A).

Per-source policy:

* slack  - a conversation (thread, or channel/DM for unthreaded chat) matures only after
           ``quiet_minutes`` (>= 30) without new relevant activity. No forced upper bound:
           an ongoing chat keeps accumulating (visible in health output, never dropped).
* email  - a thread matures after ``quiet_minutes``; email alone wakes the model at most
           every ``min_wake_interval_minutes``. Mature email joins any wake that happens.
* github - same shape as email with its own values.
* manual - matures immediately.

Mature batches of every source are merged into one model run. Waking for one source
never pulls in immature batches of another (``mature`` is decided per batch first).
A ``forced`` batch (explicit "process this now") matures immediately.

Budget: ``wake.max_context_chars`` bounds the whole rendered context and
``wake.max_evidence_chars`` the new evidence in it (the evidence gets what the fixed part -
rules, open tasks, follow-ups, digest, format - leaves). A conversation larger than one run is
split into segments in message order; the later segment is a separate mature batch that waits
while an earlier segment of the same conversation is in flight, and is never marked processed
before it was rendered and decided.
"""

from __future__ import annotations

import itertools
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from . import clock, ids
from .config import Config
from .db import loads, tx

QUIET_DEFAULTS = {"slack": 30, "gmail": 15, "github": 10, "manual": 0}
SECTION = {"gmail": "email", "slack": "slack", "github": "github"}


def quiet_minutes(cfg: Config, source: str) -> float:
    section = SECTION.get(source)
    if section:
        value = cfg.get(f"{section}.quiet_minutes")
        if value is not None:
            if source == "slack":
                return max(30.0, float(value))  # agreed floor
            return float(value)
    return float(QUIET_DEFAULTS.get(source, 0))


def min_wake_interval(cfg: Config, source: str) -> float:
    section = SECTION.get(source)
    if section in ("email", "github"):
        return float(cfg.get(f"{section}.min_wake_interval_minutes", 0) or 0)
    return 0.0


def mature_batches(conn: sqlite3.Connection, cfg: Config) -> list[str]:
    """Move accumulating batches whose quiet window elapsed to ``mature``. Returns ids."""
    now = clock.now()
    matured = []
    rows = conn.execute("SELECT * FROM batches WHERE state='accumulating'").fetchall()
    with tx(conn):
        for row in rows:
            last = clock.parse(row["last_activity_at"]) or clock.parse(row["created_at"])
            quiet = timedelta(minutes=quiet_minutes(cfg, row["source"]))
            if row["forced"] or (last is not None and now - last >= quiet):
                conn.execute("UPDATE batches SET state='mature', matured_at=? WHERE id=? AND state='accumulating'",
                             (clock.iso(), row["id"]))
                matured.append(row["id"])
    return matured


def force_conversation(conn: sqlite3.Connection, source: str, conversation_key: str) -> int:
    """Explicit user request to process a discussion now."""
    with tx(conn):
        cur = conn.execute(
            "UPDATE batches SET forced=1 WHERE source=? AND conversation_key=? AND state='accumulating'",
            (source, conversation_key))
    return cur.rowcount


@dataclass
class WakePlan:
    wake: bool
    reasons: list[str] = field(default_factory=list)
    batch_ids: list[str] = field(default_factory=list)
    deferred: dict[str, int] = field(default_factory=dict)


def last_wake_for_source(conn: sqlite3.Connection, source: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(b.claimed_at) AS at FROM batches b WHERE b.source=? AND b.claimed_at IS NOT NULL",
        (source,)).fetchone()
    return row["at"] if row else None


def evidence_budget(cfg: Config, base_chars: int = 0) -> int:
    """Characters of new evidence one run may render, after the fixed part of the context."""
    total = int(cfg.get("wake.max_context_chars", 60000))
    return max(0, min(int(cfg.get("wake.max_evidence_chars", 40000)), total - base_chars))


def plan_intake_wake(conn: sqlite3.Connection, cfg: Config, extra_reasons: list[str] | None = None,
                     base_chars: int = 0) -> WakePlan:
    """Decide which mature batches go into this run and whether to wake at all. ``base_chars``
    is the size of the context without evidence."""
    plan = WakePlan(wake=False, reasons=list(extra_reasons or []))
    budget = evidence_budget(cfg, base_chars)
    # A later segment of a conversation waits while an earlier one is in flight (message order).
    rows = conn.execute(
        "SELECT id, source FROM batches b WHERE state='mature' AND NOT EXISTS (SELECT 1 FROM batches o"
        " WHERE o.source=b.source AND o.conversation_key=b.conversation_key AND o.state='claimed')"
        " ORDER BY matured_at, first_item_at").fetchall()
    by_source: dict[str, list[str]] = {}
    for row in rows:
        by_source.setdefault(row["source"], []).append(row["id"])
    gated: dict[str, list[str]] = {}
    for source, batch_ids in by_source.items():
        interval = min_wake_interval(cfg, source)
        last = clock.minutes_since(last_wake_for_source(conn, source))
        if interval and last is not None and last < interval:
            if sum(estimate_chars(conn, b) for b in batch_ids) <= budget:
                gated[source] = batch_ids  # may still ride along with another reason
                continue
            # More than one run's worth is waiting (e.g. the first backfill): do not hold it back.
            plan.reasons.append(f"backlog:{source}:{len(batch_ids)}")
            plan.batch_ids.extend(batch_ids)
            continue
        plan.reasons.append(f"mature:{source}:{len(batch_ids)}")
        plan.batch_ids.extend(batch_ids)
    if plan.reasons:
        plan.wake = True
        for source, batch_ids in gated.items():
            plan.batch_ids.extend(batch_ids)
            plan.reasons.append(f"ride_along:{source}:{len(batch_ids)}")
    else:
        plan.deferred = {source: len(v) for source, v in gated.items()}
    if plan.batch_ids:
        source_of = {row["id"]: row["source"] for row in rows}
        plan.batch_ids = _fit_batches(conn, _round_robin(plan.batch_ids, source_of), source_of,
                                      int(cfg.get("wake.max_items_per_run", 60)), budget)
    return plan


def _round_robin(batch_ids: list[str], source_of: dict[str, str]) -> list[str]:
    """Alternate between sources (each in its own order), so that a backlog of one source
    (e.g. the first email backfill) cannot crowd chat out of every run's budget."""
    groups: dict[str, list[str]] = {}
    for batch_id in batch_ids:
        groups.setdefault(source_of[batch_id], []).append(batch_id)
    return [b for row in itertools.zip_longest(*groups.values()) for b in row if b is not None]


ITEM_OVERHEAD = 200     # header, recipients and subject lines per rendered item
BATCH_OVERHEAD = 300    # conversation heading, link, linked tasks


EARLIER_SHOWN = 6       # earlier messages rendered as context per conversation


def _item_sizes(conn: sqlite3.Connection, batch_id: str) -> list[tuple[str, int]]:
    """(item id, rendered size bound) of a batch's new items in message order."""
    from .intake import BULK_ITEM_CHARS, MAX_ITEM_CHARS
    sizes = []
    for r in conn.execute("SELECT s.id, s.meta, LENGTH(b.body) AS n FROM source_items s LEFT JOIN source_bodies b"
                          " ON b.item_id=s.id WHERE s.batch_id=? AND s.state='batched' ORDER BY s.occurred_at,"
                          " s.fetched_at, s.id", (batch_id,)):
        limit = BULK_ITEM_CHARS if loads(r["meta"], {}).get("bulk_hint") else MAX_ITEM_CHARS
        sizes.append((r["id"], min(r["n"] or 0, limit) + ITEM_OVERHEAD))
    return sizes


def _earlier_chars(conn: sqlite3.Connection, batch_id: str) -> int:
    from .intake import MAX_EARLIER_CHARS
    batch = conn.execute("SELECT source, conversation_key, last_item_at FROM batches WHERE id=?", (batch_id,)).fetchone()
    if batch is None:
        return 0
    shown = ("SELECT s.id, LENGTH(b.body) AS n FROM source_items s LEFT JOIN source_bodies b ON b.item_id=s.id"
             " WHERE s.source=? AND s.conversation_key=? AND (s.batch_id IS NULL OR s.batch_id != ?)"
             " AND s.state IN ('processed','batched','pending') AND s.occurred_at < ?")
    params = (batch["source"], batch["conversation_key"], batch_id, batch["last_item_at"] or "")
    rows = conn.execute(shown + " ORDER BY s.occurred_at DESC LIMIT ?", (*params, EARLIER_SHOWN)).fetchall()
    root = conn.execute(shown + " ORDER BY s.occurred_at, s.fetched_at LIMIT 1", params).fetchone()
    if root and root["id"] not in {r["id"] for r in rows}:
        rows.append(root)   # the first message is always shown
    return sum(min(r["n"] or 0, MAX_EARLIER_CHARS) + ITEM_OVERHEAD for r in rows)


def estimate_chars(conn: sqlite3.Connection, batch_id: str) -> int:
    """Upper bound of the evidence text one batch adds to the intake context (see intake.render_context)."""
    return BATCH_OVERHEAD + _earlier_chars(conn, batch_id) + sum(n for _, n in _item_sizes(conn, batch_id))


def split_batch(conn: sqlite3.Connection, batch_id: str, max_chars: int, max_items: int) -> str | None:
    """Keep the leading messages of a batch that fit ``max_chars``/``max_items`` (at least one)
    and move the rest, unread, into a new mature batch of the same conversation. Returns the new
    batch id, or None when everything fits."""
    sizes = _item_sizes(conn, batch_id)
    room = max_chars - BATCH_OVERHEAD - _earlier_chars(conn, batch_id)
    keep, used = 0, 0
    for _, size in sizes:
        if keep and (keep >= max_items or used + size > room):
            break
        keep += 1
        used += size
    tail = [item_id for item_id, _ in sizes[keep:]]
    if not tail:
        return None
    new_id = ids.new_id("batch")
    with tx(conn):
        first, last = conn.execute(
            f"SELECT MIN(occurred_at), MAX(occurred_at) FROM source_items WHERE id IN ({','.join('?' * len(tail))})",
            tail).fetchone()
        conn.execute(
            "INSERT INTO batches(id, source, conversation_key, state, first_item_at, last_item_at, last_activity_at,"
            " matured_at, forced, created_at) SELECT ?, source, conversation_key, 'mature', ?, ?, last_activity_at,"
            " matured_at, forced, ? FROM batches WHERE id=?", (new_id, first, last, clock.iso(), batch_id))
        for start in range(0, len(tail), 500):
            chunk = tail[start:start + 500]
            conn.execute(f"UPDATE source_items SET batch_id=? WHERE id IN ({','.join('?' * len(chunk))})",
                         (new_id, *chunk))
        head_last = conn.execute("SELECT MAX(occurred_at) FROM source_items WHERE batch_id=?", (batch_id,)).fetchone()[0]
        conn.execute("UPDATE batches SET last_item_at=? WHERE id=?", (head_last, batch_id))
    return new_id


def _fit_batches(conn: sqlite3.Connection, batch_ids: list[str], source_of: dict[str, str], max_items: int,
                 max_chars: int) -> list[str]:
    """Batches in order until the item or character budget is reached; a batch that would fit
    a later run waits for it. A batch that cannot fit any run on its own is split: its segment
    gets the room left, at most an even share per waiting source (so one huge thread cannot hold
    every run), and at least one message when nothing else was chosen."""
    sources = len({source_of[b] for b in batch_ids}) or 1
    chosen, items, chars = [], 0, 0
    for batch_id in batch_ids:
        sizes = _item_sizes(conn, batch_id)
        size = estimate_chars(conn, batch_id)
        if items + len(sizes) <= max_items and chars + size <= max_chars:
            chosen.append(batch_id)
            items += len(sizes)
            chars += size
            continue
        if sizes and (len(sizes) > max_items or size > max_chars):   # oversize: no run could take it whole
            room_chars = min(max_chars - chars, max_chars // sources)
            room_items = min(max_items - items, max(1, max_items // sources))
            first = BATCH_OVERHEAD + _earlier_chars(conn, batch_id) + sizes[0][1]
            if not chosen or (room_items > 0 and room_chars >= first):
                split_batch(conn, batch_id, room_chars, max(room_items, 1))
                chosen.append(batch_id)
                items += min(len(sizes), max(room_items, 1))
                chars += estimate_chars(conn, batch_id)
                continue
        break
    return chosen


def unclaim(conn: sqlite3.Connection, batch_id: str, run_id: str) -> None:
    """Undo this run's claim of a batch that did not fit the rendered budget (not an attempt)."""
    with tx(conn):
        conn.execute("UPDATE batches SET state='mature', claimed_run=NULL, claimed_at=NULL, attempts=attempts-1"
                     " WHERE id=? AND claimed_run=? AND state='claimed'", (batch_id, run_id))


def claim(conn: sqlite3.Connection, batch_ids: list[str], run_id: str) -> list[str]:
    claimed = []
    now = clock.iso()
    with tx(conn):
        for batch_id in batch_ids:
            cur = conn.execute(
                "UPDATE batches SET state='claimed', claimed_run=?, claimed_at=?, attempts=attempts+1"
                " WHERE id=? AND state='mature'", (run_id, now, batch_id))
            if cur.rowcount == 1:
                claimed.append(batch_id)
    return claimed


def release_stale_claims(conn: sqlite3.Connection, stale_minutes: float, max_attempts: int = 3) -> dict[str, int]:
    """Claimed batches whose run never applied decisions go back to mature (bounded)."""
    stats = {"released": 0, "failed": 0}
    cutoff = clock.iso(clock.now() - timedelta(minutes=stale_minutes))
    rows = conn.execute("SELECT * FROM batches WHERE state='claimed' AND claimed_at < ?", (cutoff,)).fetchall()
    with tx(conn):
        for row in rows:
            if row["attempts"] >= max_attempts:
                conn.execute("UPDATE batches SET state='failed', last_error=? WHERE id=?",
                             ("semantic processing did not complete after retries", row["id"]))
                stats["failed"] += 1
            else:
                conn.execute("UPDATE batches SET state='mature', claimed_run=NULL WHERE id=?", (row["id"],))
                stats["released"] += 1
    return stats


def backlog(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT source, state, COUNT(*) AS n, MIN(first_item_at) AS oldest FROM batches"
        " WHERE state IN ('accumulating','mature','claimed','failed') GROUP BY source, state").fetchall()
    return [dict(r) for r in rows]
