"""Pre-run gate for entry point A (information intake), executed by Hermes cron every 5 min.

Deterministic work only: fetch + persist sources, accumulate and mature batches, decide
whether anything deserves a model run. Prints a short, safe context (IDs and counts, never
source text) followed by the wake line ``{"wakeAgent": ...}`` as the LAST stdout line.

Wake reasons: mature batches (per-source policy in ``batching``), follow-ups due, the daily
commit digest, and bounded retries of semantic work that did not complete. An API failure
is recorded (freshness/health) and never turned into "no changes".
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from . import batching, clock, runs
from .config import Config
from .db import tx
from .sources.base import SourceError, get_cursor, persist_fetch, record_fetch_error

FETCH_BUDGET_SECONDS = 200
STALE_CLAIM_MINUTES = 90


def fetch_all(conn: sqlite3.Connection, adapters: list[Any], budget: float = FETCH_BUDGET_SECONDS) -> dict[str, Any]:
    started = time.monotonic()
    report: dict[str, Any] = {}
    for adapter in adapters:
        key = f"{adapter.name}:{adapter.scope}"
        if time.monotonic() - started > budget:
            report[key] = {"skipped": "time budget"}
            continue
        cursor = get_cursor(conn, adapter.name, adapter.scope) or {}
        try:
            result = adapter.fetch(cursor.get("cursor"), cursor.get("last_success_at"))
        except SourceError as exc:
            record_fetch_error(conn, adapter.name, adapter.scope, str(exc))
            report[key] = {"error": str(exc)[:200]}
            continue
        except Exception as exc:  # noqa: BLE001 - adapter bug must not stop other sources
            record_fetch_error(conn, adapter.name, adapter.scope, f"{type(exc).__name__}: {exc}")
            report[key] = {"error": f"{type(exc).__name__}"}
            continue
        report[key] = persist_fetch(conn, adapter.name, result)
        if not result.complete:
            report[key]["more_pending"] = True
    return report


def _digest_time(cfg: Config) -> tuple[int, int]:
    hh, mm = str(cfg.get("commits.digest_time", "21:00")).split(":")
    return int(hh), int(mm)


def commit_digest_check(conn: sqlite3.Connection, cfg: Config, adapters: list[Any]) -> tuple[dict[str, Any] | None, dict]:
    """Return a digest manifest when today's digest should run now (at most one model
    attempt per day plus ``max_attempts_per_day - 1`` retries of a run that never completed)."""
    info: dict[str, Any] = {}
    if not adapters:
        return None, info
    local = clock.local_dt(cfg.tz)
    hh, mm = _digest_time(cfg)
    if (local.hour, local.minute) < (hh, mm):
        return None, info
    day = local.date().isoformat()
    row = conn.execute("SELECT * FROM daily_jobs WHERE job='commit_digest' AND day=?", (day,)).fetchone()
    max_attempts = int(cfg.get("commits.max_attempts_per_day", 2))
    if row is not None:
        if row["state"] in ("done", "skipped", "exhausted"):
            return None, info
        if row["state"] == "claimed":
            run = runs.get(conn, row["run_id"]) if row["run_id"] else None
            age = clock.minutes_since(row["updated_at"]) or 0
            if run and run.get("agent_completed_at"):
                return None, info
            if age < STALE_CLAIM_MINUTES:
                return None, info  # still in flight
        if row["attempts"] >= max_attempts:
            with tx(conn):
                conn.execute("UPDATE daily_jobs SET state='exhausted', updated_at=? WHERE job='commit_digest' AND day=?",
                             (clock.iso(), day))
            info["digest"] = "exhausted"
            return None, info
    info["fetch"] = fetch_all(conn, adapters, budget=120)
    if any("error" in v for v in info["fetch"].values()):
        return None, info  # retry fetching next tick; no model call on partial data
    item_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM source_items WHERE source='commit' AND state='pending' ORDER BY occurred_at")]
    now = clock.iso()
    if not item_ids:
        with tx(conn):
            conn.execute("INSERT INTO daily_jobs(job, day, state, attempts, detail, updated_at) VALUES"
                         " ('commit_digest', ?, 'skipped', 0, 'no new commits', ?)"
                         " ON CONFLICT(job, day) DO UPDATE SET state='skipped', updated_at=excluded.updated_at", (day, now))
        info["digest"] = "skipped_no_commits"
        return None, info
    return {"day": day, "item_ids": item_ids}, info


def due_followups(conn: sqlite3.Connection, cfg: Config) -> list[str]:
    today = clock.local_date(cfg.tz).isoformat()
    rows = conn.execute(
        "SELECT t.id, t.follow_up_date FROM tasks t WHERE t.status IN ('todo','in_progress','waiting')"
        " AND t.follow_up_date IS NOT NULL AND substr(t.follow_up_date,1,10) <= ?"
        " AND (t.snooze_until IS NULL OR substr(t.snooze_until,1,10) <= ?)"
        " AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.target_id=t.id AND d.op='followup_surfaced'"
        "   AND d.created_at >= substr(t.follow_up_date,1,10))", (today, today)).fetchall()
    return [r["id"] for r in rows]


def run_gate(conn: sqlite3.Connection, cfg: Config, conversation_adapters: list[Any],
             commit_adapters: list[Any]) -> tuple[str, bool]:
    run_id = runs.start(conn, "intake")
    summary: dict[str, Any] = {}
    try:
        summary["recovery"] = batching.release_stale_claims(conn, STALE_CLAIM_MINUTES)
        summary["fetch"] = fetch_all(conn, conversation_adapters)
        summary["matured"] = len(batching.mature_batches(conn, cfg))
        digest, digest_info = commit_digest_check(conn, cfg, commit_adapters)
        if digest_info:
            summary["digest"] = digest_info
        followups = due_followups(conn, cfg)
        extra = []
        if digest:
            extra.append(f"commit_digest:{len(digest['item_ids'])}")
        if followups:
            extra.append(f"followups_due:{len(followups)}")
        plan = batching.plan_intake_wake(conn, cfg, extra)
        if plan.deferred:
            summary["deferred_by_interval"] = plan.deferred
        if not plan.wake:
            runs.finish(conn, run_id, status="idle", summary=summary)
            return '{"wakeAgent": false}', False
        claimed = batching.claim(conn, plan.batch_ids, run_id)
        now = clock.iso()
        with tx(conn):
            for tid in followups:
                conn.execute(
                    "INSERT INTO decisions(id, run_id, op, target_type, target_id, created_at) VALUES (?,?,?,?,?,?)",
                    (f"dec_fu_{run_id}_{tid}", run_id, "followup_surfaced", "task", tid, now))
            if digest:
                conn.execute(
                    "INSERT INTO daily_jobs(job, day, state, attempts, run_id, updated_at) VALUES"
                    " ('commit_digest', ?, 'claimed', 1, ?, ?) ON CONFLICT(job, day) DO UPDATE SET state='claimed',"
                    " attempts=daily_jobs.attempts+1, run_id=excluded.run_id, updated_at=excluded.updated_at",
                    (digest["day"], run_id, now))
        manifest = {"run_id": run_id, "created_at": now, "batch_ids": claimed, "followups": followups,
                    "digest": digest, "reasons": plan.reasons}
        runs.write_json(cfg.home, run_id, "manifest.json", manifest)
        runs.finish(conn, run_id, status="woke", wake=True, reasons=plan.reasons, summary=summary)
        lines = [
            f"Todo Manager intake run: {run_id}",
            f"Wake reasons: {', '.join(plan.reasons)}",
            f"Conversations claimed: {len(claimed)}; follow-ups due: {len(followups)};"
            f" commit digest: {'yes' if digest else 'no'}",
            f"Do: load skill todo-manager, run `todo-manager intake show {run_id}`, decide, then `todo-manager intake apply {run_id} --file <decisions.json>`.",
            '{"wakeAgent": true}',
        ]
        return "\n".join(lines), True
    except Exception as exc:  # noqa: BLE001 - never wake the model for a gate crash
        runs.finish(conn, run_id, status="error", summary=summary, error=f"{type(exc).__name__}: {exc}")
        return '{"wakeAgent": false}', False


def manifest_for(cfg: Config, run_id: str) -> dict[str, Any]:
    manifest = runs.read_json(cfg.home, run_id, "manifest.json")
    if manifest is None:
        raise FileNotFoundError(f"no manifest for run {run_id}")
    return manifest

