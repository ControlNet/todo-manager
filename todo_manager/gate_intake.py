"""Pre-run gate of the intake job (Hermes cron, every few minutes).

Deterministic work only: fetch and persist the sources, accumulate and mature conversation
batches, and decide whether anything deserves a model run. Its stdout is the run's context
(``context.intake``), printed directly into the agent's prompt by Hermes; when there is
nothing to do it prints only ``{"wakeAgent": false}`` and no model runs.

Outcomes, as Hermes sees them:

* nothing to do -> ``{"wakeAgent": false}`` (a successful, silent run);
* material, due follow-ups or the commit digest -> the context (the agent records outcomes
  with ``todo-manager`` commands and ends with ``run done``);
* a source keeps failing while there is nothing else to do -> a short context that only
  reports it, at most once a day per problem (the prompt has the agent answer
  ``[CRON_FAILURE]``, Hermes' failure path); with other work the problem is listed in the context;
* the gate itself fails -> ``GateError``; the command exits non-zero, Hermes wakes the agent
  with a Script Error and the prompt has it answer ``[CRON_FAILURE]``. Never a fake idle.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import batching, clock, context, prefs, runs
from .config import Config
from .db import dumps, get_meta, loads, set_meta, tx
from .sources.base import SourceError, get_cursor, persist_fetch, record_fetch_error, retire_scopes

IDLE = '{"wakeAgent": false}'
SOURCES = ("gmail", "slack", "github", "commit")
FETCH_BUDGET_SECONDS = 200
STALE_CLAIM_MINUTES = 90       # lease of a run's claims when it never reports back
FOLLOWUP_ATTEMPTS = 2
PROBLEMS_KEY = "gate.problems_reported"


class GateError(RuntimeError):
    """The gate could not do its work; reported to Hermes as a failed script."""


def fetch_all(conn: sqlite3.Connection, adapters: list[Any], budget: float = FETCH_BUDGET_SECONDS) -> dict[str, Any]:
    """Fetch each source scope once. Optional adapter hooks:

    * ``due(cursor_row)`` returning False: not read in this run (the adapter's own cadence);
    * ``expand(cursor)``: more scopes to read (e.g. discovered Slack DMs), from this run's
      cursor, or the previous one when this run's fetch failed. They are read least recently
      read first, so scopes left over by a rate limit or the time budget go first next run.
      Scopes that left the list (and were not read through explicit configuration in this run)
      are retired: no longer reported as stale, and with ``conversation_prefix(scope)`` their
      not yet claimed batches are withdrawn (kept as superseded, counted in the report).

    After a rate-limit error the remaining scopes of that source wait for the next run."""
    started = time.monotonic()
    report: dict[str, Any] = {}
    queue = sorted(adapters, key=lambda a: _last_read(conn, a))
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
            set_deadline = getattr(adapter, 'set_fetch_deadline', None)
            if set_deadline:
                set_deadline(started + budget)
            result = adapter.fetch(cursor, (row or {}).get("last_success_at"))
        except SourceError as exc:
            with tx(conn):
                if exc.checkpoint is not None:
                    persist_fetch(conn, adapter.name, exc.checkpoint)
                    cursor = exc.checkpoint.cursor
                    # A checkpoint is progress, not a healthy completed read. Preserve
                    # failure history so a persistent outage still reaches health.
                    conn.execute('UPDATE cursors SET last_success_at=?, consecutive_failures=? '
                                 'WHERE source=? AND scope=?',
                                 ((row or {}).get('last_success_at'), (row or {}).get('consecutive_failures', 0),
                                  adapter.name, adapter.scope))
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
            queue.extend(sorted(current, key=lambda a: _last_read(conn, a)))
            if cursor != previous:
                dropped = {a.scope for a in expand(previous)} - {a.scope for a in current}
                dropped -= {k.partition(":")[2] for k in report if k.startswith(adapter.name + ":")}
                prefix = getattr(adapter, "conversation_prefix", None)
                for scope, withdrawn in retire_scopes(conn, adapter.name, dropped, prefix).items():
                    report[f"{adapter.name}:{scope}"] = {"retired": True, "withdrawn_items": withdrawn}
    return report


def _last_read(conn: sqlite3.Connection, adapter: Any) -> str:
    row = get_cursor(conn, adapter.name, adapter.scope)
    return (row or {}).get("last_attempt_at") or (row or {}).get("last_success_at") or ""


def _digest_time(cfg: Config) -> tuple[int, int]:
    hh, mm = str(cfg.get("commits.digest_time", "21:00")).split(":")
    return int(hh), int(mm)


def next_digest_at(cfg: Config) -> str:
    """Tomorrow's digest time (local wall clock, so a 23- or 25-hour day is respected), UTC."""
    hh, mm = _digest_time(cfg)
    tomorrow = clock.local_date(cfg.tz) + timedelta(days=1)
    return clock.iso(datetime(tomorrow.year, tomorrow.month, tomorrow.day, hh, mm, tzinfo=ZoneInfo(cfg.tz)))


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
            age = clock.minutes_since(row["updated_at"])
            if age is not None and age < STALE_CLAIM_MINUTES:
                return None, info  # still in flight (``run done`` without ``digest done`` clears the stamp)
        if row["attempts"] >= max_attempts:
            with tx(conn):
                conn.execute("UPDATE daily_jobs SET state='exhausted', updated_at=? WHERE job='commit_digest' AND day=?",
                             (clock.iso(), day))
            info["digest"] = "exhausted"
            return None, info
    pending = [a for a in adapters if ((get_cursor(conn, a.name, a.scope) or {}).get('meta') or {}).get('digest_read_day') != day]
    info["fetch"] = fetch_all(conn, pending, budget=120)
    # Commits are read only here, once a day: the scope is due again at tomorrow's digest time
    # (health reports it stale only after that, or at once when a due read fails).
    due_at = next_digest_at(cfg)
    with tx(conn):
        for adapter in pending:
            read = info["fetch"].get(f"{adapter.name}:{adapter.scope}") or {"skipped": "not read"}
            if "error" not in read and "skipped" not in read and not read.get("more_pending"):
                conn.execute("UPDATE cursors SET meta=json_set(COALESCE(meta, '{}'), '$.next_due_at', ?, '$.digest_read_day', ?)"
                             " WHERE source=? AND scope=?", (due_at, day, adapter.name, adapter.scope))
    if any("error" in v or "skipped" in v or v.get("more_pending") for v in info["fetch"].values()):
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
    claimed by a run and done when that run updates the task or reports it checked; a run that
    ends without either (``run done``) or never reports back (lease) leaves it due again, for a
    bounded number of attempts, then it is marked failed (reported by health). A new follow-up
    date is a new check."""
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


def problems_due(conn: sqlite3.Connection, cfg: Config, entry: str, sources: tuple[str, ...]) -> list[dict[str, Any]]:
    """Sources failing for at least ``wake.problem_minutes`` that ``entry`` did not report
    today. A problem that clears is forgotten, so a new outage is reported again."""
    minimum = float(cfg.get("wake.problem_minutes", 60))
    current = []
    for p in context.source_problems(conn, sources):
        age = clock.minutes_since(p["since"])
        if (age is None and p["failures"] >= 3) or (age is not None and age >= minimum):
            current.append(p)
    today = clock.local_date(cfg.tz).isoformat()
    key = f"{PROBLEMS_KEY}.{entry}"
    reported = loads(get_meta(conn, key), {}) or {}
    signatures = {f"{p['source']}:{p['scope']}" for p in current}
    due = [p for p in current if reported.get(f"{p['source']}:{p['scope']}") != today]
    with tx(conn):
        set_meta(conn, key, dumps({**{k: v for k, v in reported.items() if k in signatures},
                                            **{f"{p['source']}:{p['scope']}": today for p in due}}))
    return due


def run_gate(conn: sqlite3.Connection, cfg: Config, conversation_adapters: list[Any],
             commit_adapters: list[Any]) -> tuple[str, bool]:
    run_id = runs.start(conn, "intake")
    summary: dict[str, Any] = {}
    try:
        attempts = int(cfg.get("wake.max_attempts", 3))
        summary["recovery"] = batching.release_stale_claims(conn, STALE_CLAIM_MINUTES, attempts)
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

        def render(reasons: list[str], problems: list[dict[str, Any]]) -> str:
            return context.intake(conn, cfg, run_id, followups=followups, digest=digest, problems=problems,
                                  reasons=reasons)

        base_chars = 0
        if extra or conn.execute("SELECT 1 FROM batches WHERE state='mature' LIMIT 1").fetchone():
            base_chars = len(render(extra, []))   # nothing claimed yet: the fixed part
        plan = batching.plan_intake_wake(conn, cfg, extra, base_chars=base_chars)
        if plan.deferred:
            summary["deferred_by_interval"] = plan.deferred
        problems = problems_due(conn, cfg, "intake", SOURCES)
        if problems:
            summary["problems_reported"] = [f"{p['source']}:{p['scope']}" for p in problems]
        if not plan.wake:
            if problems:
                runs.finish(conn, run_id, status="woke", wake=True, reasons=["source_problems"], summary=summary)
                shown = context.final_check(context.problems_only(cfg, run_id, "intake", problems))
                runs.write_text(cfg.home, run_id, "context.md", shown)   # what the agent was shown
                return shown, True
            runs.finish(conn, run_id, status="idle", summary=summary)
            return IDLE, False
        claimed = batching.claim(conn, plan.batch_ids, run_id)
        now = clock.iso()
        with tx(conn):
            claim_followups(conn, run_id, followups)
            if digest:
                conn.execute(
                    "INSERT INTO daily_jobs(job, day, state, attempts, run_id, updated_at) VALUES"
                    " ('commit_digest', ?, 'claimed', 1, ?, ?) ON CONFLICT(job, day) DO UPDATE SET state='claimed',"
                    " attempts=daily_jobs.attempts+1, run_id=excluded.run_id, updated_at=excluded.updated_at",
                    (digest["day"], run_id, now))
        claimed, text = fit_rendered(conn, cfg, run_id, claimed, lambda: render(plan.reasons, problems))
        summary["context_chars"] = len(text)
        if len(text) > int(cfg.get("wake.max_context_chars", 60000)):
            summary["context_over_budget"] = len(text)   # one clipped item over budget: run anyway (health)
        manifest = {"run_id": run_id, "created_at": now, "batch_ids": claimed, "followups": followups,
                    "digest": digest, "reasons": plan.reasons}
        runs.write_json(cfg.home, run_id, "manifest.json", manifest)
        runs.finish(conn, run_id, status="woke", wake=True, reasons=plan.reasons, summary=summary)
        shown = context.final_check(text)
        runs.write_text(cfg.home, run_id, "context.md", shown)   # what the agent was shown
        return shown, True
    except Exception as exc:  # noqa: BLE001 - recorded, then reported to Hermes as a failed script
        runs.finish(conn, run_id, status="error", summary=summary, error=f"{type(exc).__name__}: {exc}")
        raise GateError(f"intake gate failed in run {run_id}: {type(exc).__name__}: {exc}") from exc


def fit_rendered(conn: sqlite3.Connection, cfg: Config, run_id: str, claimed: list[str],
                 render: Any) -> tuple[list[str], str]:
    """Check the rendered context against ``wake.max_context_chars``: give back the last
    claimed conversations, then split the only one left, until it fits or one message remains.
    One message is always offered (it is clipped per item), so nothing waits forever."""
    total = int(cfg.get("wake.max_context_chars", 60000))
    claimed = list(claimed)
    while True:
        text = render()
        if len(text) <= total or not claimed:
            return claimed, text
        if len(claimed) > 1:
            batching.unclaim(conn, claimed.pop(), run_id)
            continue
        count = len(context.run_items(conn, run_id))
        if count <= 1:
            return claimed, text
        batching.split_batch(conn, cfg, claimed[0], 10 ** 9, count // 2)


def manifest_for(cfg: Config, run_id: str) -> dict[str, Any]:
    manifest = runs.read_json(cfg.home, run_id, "manifest.json")
    if manifest is None:
        raise FileNotFoundError(f"no manifest for run {run_id}")
    return manifest
