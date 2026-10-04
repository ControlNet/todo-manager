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
from datetime import timedelta
from typing import Any

from . import batching, clock, intake, runs
from .config import Config
from .db import tx
from .sources.base import SourceError, get_cursor, persist_fetch, record_fetch_error, retire_scopes

FETCH_BUDGET_SECONDS = 200
STALE_CLAIM_MINUTES = 90
FOLLOWUP_ATTEMPTS = 2


def fetch_all(conn: sqlite3.Connection, adapters: list[Any], budget: float = FETCH_BUDGET_SECONDS) -> dict[str, Any]:
    """Fetch each source scope once. Optional adapter hooks:

    * ``due(cursor_row)`` returning False: not read in this run (the adapter's own cadence);
    * ``expand(cursor)``: more scopes to read (e.g. discovered Slack DMs), from this run's
      cursor, or the previous one when this run's fetch failed. Scopes that left the list are
      retired (no longer reported as stale).

    After a rate-limit error the remaining scopes of that source wait for the next run."""
    started = time.monotonic()
    report: dict[str, Any] = {}
    queue = list(adapters)
    throttled: set[str] = set()
    while queue:
        adapter = queue.pop(0)
        key = f"{adapter.name}:{adapter.scope}"
        if key in report:
            continue
        if time.monotonic() - started > budget:
            report[key] = {"skipped": "time budget"}
            continue
        if adapter.name in throttled:
            report[key] = {"skipped": "rate limited"}
            continue
        row = get_cursor(conn, adapter.name, adapter.scope)
        due = getattr(adapter, "due", None)
        if due is not None and not due(row):
            report[key] = {"skipped": "not due"}
            continue
        cursor = previous = (row or {}).get("cursor")
        try:
            result = adapter.fetch(cursor, (row or {}).get("last_success_at"))
        except SourceError as exc:
            record_fetch_error(conn, adapter.name, adapter.scope, str(exc))
            report[key] = {"error": str(exc)[:200]}
            if exc.retry_after:
                throttled.add(adapter.name)
        except Exception as exc:  # noqa: BLE001 - adapter bug must not stop other sources
            record_fetch_error(conn, adapter.name, adapter.scope, f"{type(exc).__name__}: {exc}")
            report[key] = {"error": f"{type(exc).__name__}"}
        else:
            report[key] = persist_fetch(conn, adapter.name, result)
            if not result.complete:
                report[key]["more_pending"] = True
            if result.cursor is not None:
                cursor = result.cursor
        expand = getattr(adapter, "expand", None)
        if expand is not None:
            current = expand(cursor)
            queue.extend(current)
            if cursor != previous:
                dropped = {a.scope for a in expand(previous)} - {a.scope for a in current}
                retire_scopes(conn, adapter.name, dropped)
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
    """Open tasks whose follow-up date has come. One check per (task, follow-up date): it is
    claimed by a run and done only when that run's decisions are applied; a run that never
    applies releases it after ``STALE_CLAIM_MINUTES`` for a bounded number of attempts, then it
    is marked failed (reported by health). A new follow-up date is a new check."""
    today = clock.local_date(cfg.tz).isoformat()
    cutoff = clock.iso(clock.now() - timedelta(minutes=STALE_CLAIM_MINUTES))
    rows = conn.execute(
        "SELECT t.id, substr(t.follow_up_date,1,10) AS due, f.state, f.claimed_at, f.attempts FROM tasks t"
        " LEFT JOIN followup_checks f ON f.task_id=t.id AND f.due=substr(t.follow_up_date,1,10)"
        " WHERE t.status IN ('todo','in_progress','waiting') AND t.follow_up_date IS NOT NULL"
        " AND substr(t.follow_up_date,1,10) <= ? AND (t.snooze_until IS NULL OR substr(t.snooze_until,1,10) <= ?)",
        (today, today)).fetchall()
    due = []
    for r in rows:
        if r["state"] is None:
            due.append(r["id"])
        elif r["state"] == "claimed" and (r["claimed_at"] or "") < cutoff:
            if r["attempts"] < FOLLOWUP_ATTEMPTS:
                due.append(r["id"])
            else:
                with tx(conn):
                    conn.execute("UPDATE followup_checks SET state='failed', updated_at=? WHERE task_id=? AND due=?",
                                 (clock.iso(), r["id"], r["due"]))
    return due


def claim_followups(conn: sqlite3.Connection, run_id: str, task_ids: list[str]) -> None:
    now = clock.iso()
    for tid in task_ids:
        conn.execute(
            "INSERT INTO followup_checks(task_id, due, state, run_id, attempts, claimed_at, updated_at)"
            " SELECT id, substr(follow_up_date,1,10), 'claimed', ?, 1, ?, ? FROM tasks WHERE id=?"
            " ON CONFLICT(task_id, due) DO UPDATE SET state='claimed', run_id=excluded.run_id,"
            " attempts=followup_checks.attempts+1, claimed_at=excluded.claimed_at, updated_at=excluded.updated_at",
            (run_id, now, now, tid))


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
        draft = {"followups": followups, "digest": digest}
        base_chars = 0
        if extra or conn.execute("SELECT 1 FROM batches WHERE state='mature' LIMIT 1").fetchone():
            base_chars = len(intake.render_context(conn, cfg, run_id, draft))   # nothing claimed yet
        plan = batching.plan_intake_wake(conn, cfg, extra, base_chars=base_chars)
        if plan.deferred:
            summary["deferred_by_interval"] = plan.deferred
        if not plan.wake:
            runs.finish(conn, run_id, status="idle", summary=summary)
            return '{"wakeAgent": false}', False
        claimed = batching.claim(conn, plan.batch_ids, run_id)
        claimed, context_chars = fit_rendered(conn, cfg, run_id, claimed, draft)
        summary["context_chars"] = context_chars
        total = int(cfg.get("wake.max_context_chars", 60000))
        if context_chars > total:
            summary["context_over_budget"] = context_chars
        now = clock.iso()
        with tx(conn):
            claim_followups(conn, run_id, followups)
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
            f"Context: {context_chars} chars (budget {total})" + (
                "; over budget: the fixed part (rules, open tasks, follow-ups, digest, format) is too large"
                if context_chars > total else ""),
            f"Do: load skill todo-manager, run `todo-manager intake show {run_id}`, decide, then `todo-manager intake apply {run_id} --file <decisions.json>`.",
            '{"wakeAgent": true}',
        ]
        return "\n".join(lines), True
    except Exception as exc:  # noqa: BLE001 - never wake the model for a gate crash
        runs.finish(conn, run_id, status="error", summary=summary, error=f"{type(exc).__name__}: {exc}")
        return '{"wakeAgent": false}', False


def fit_rendered(conn: sqlite3.Connection, cfg: Config, run_id: str, claimed: list[str],
                 draft: dict[str, Any]) -> tuple[list[str], int]:
    """Check the real rendered context against ``wake.max_context_chars``: give back the last
    claimed conversations, then split the only one left, until it fits or one message remains
    (then the size is reported as over budget, never silently cut)."""
    total = int(cfg.get("wake.max_context_chars", 60000))
    claimed = list(claimed)
    while True:
        size = len(intake.render_context(conn, cfg, run_id, draft))
        if size <= total or not claimed:
            return claimed, size
        if len(claimed) > 1:
            batching.unclaim(conn, claimed.pop(), run_id)
            continue
        count = len(intake.run_new_items(conn, run_id))
        if count <= 1:
            return claimed, size
        batching.split_batch(conn, claimed[0], 10 ** 9, count // 2)


def manifest_for(cfg: Config, run_id: str) -> dict[str, Any]:
    manifest = runs.read_json(cfg.home, run_id, "manifest.json")
    if manifest is None:
        raise FileNotFoundError(f"no manifest for run {run_id}")
    return manifest

