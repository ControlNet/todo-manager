"""Pre-run gate for entry point B (human feedback + execution state), Hermes cron every 5 min.

Deterministic steps, each isolated so one failing system does not stall the others:

1. Notion: pull human property/page edits (projects, then tasks) and poll comments.
2. Kanban: send queued messages (answers/supplements/pauses), submit new delegations,
   read card events (questions, deliveries, failures).
3. Dirty scan + outbox: push local changes to Notion, post bot comments, Matrix reminders.
4. Home page freshness callout (rewritten only when its content changes).
5. Claim pending feedback that needs interpretation and wake the model for it.

Stdout carries only ids and counts (never comment text) and ends with ``{"wakeAgent": ...}``.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any, Callable

from . import clock, delegation, effects, feedback, outbox, runs
from .config import Config
from .db import tx
from .notion import schema

Step = Callable[[], Any]
FRESH_MINUTES = 30
SOURCE_SCOPES = ("gmail", "slack", "github", "commit", "notion")


def _step(summary: dict[str, Any], problems: list[str], name: str, fn: Step) -> Any:
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 - isolate systems from each other
        problems.append(f"{name}: {type(exc).__name__}")
        summary[name] = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        return None
    summary[name] = result
    return result


def fail_exhausted_claims(conn: sqlite3.Connection, cfg: Config) -> dict[str, int]:
    """Feedback whose model run never completed after ``max_attempts`` claims is marked failed."""
    stale = int(cfg.get("feedback.stale_claim_minutes", 60))
    max_attempts = int(cfg.get("feedback.max_attempts", 3))
    cutoff = clock.iso(clock.now() - timedelta(minutes=stale))
    rows = conn.execute("SELECT id, scope_type, scope_id FROM feedback WHERE status='pending' AND claimed_run IS NOT NULL"
                        " AND claimed_at < ? AND attempts >= ?", (cutoff, max_attempts)).fetchall()
    with tx(conn):
        for r in rows:
            feedback.resolve(conn, r["id"], "failed", error=f"not applied after {max_attempts} model runs")
            effects.notify(conn, cfg, f"feedback-failed:{r['id']}",
                           f"⚠️ 一条反馈连续 {max_attempts} 次未能处理（{r['id']}），请查看 todo-manager health。",
                           task_id=r["scope_id"] if r["scope_type"] == "task" else None)
    return {"failed": len(rows)}


def freshness_sections(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    """Content of the home freshness callout. Stable within an hour when nothing is wrong."""
    local = clock.local_dt(cfg.tz)
    lines: list[tuple[str, str]] = []
    marks = ",".join("?" for _ in SOURCE_SCOPES)
    for r in conn.execute(f"SELECT * FROM cursors WHERE source IN ({marks}) ORDER BY source, scope", SOURCE_SCOPES):
        age = clock.minutes_since(r["last_success_at"])
        if r["consecutive_failures"] or age is None or age > FRESH_MINUTES:
            label = schema.SOURCE_LABELS.get(r["source"], r["source"])
            when = (f"{clock.local_dt(cfg.tz, clock.parse(r['last_success_at'])):%m-%d %H:%M}"
                    if r["last_success_at"] else "从未成功")
            lines.append((f"⚠️ {label}（{r['scope']}）", f"上次成功 {when}；连续失败 {r['consecutive_failures']} 次"))
    dead = conn.execute("SELECT COUNT(*) FROM outbox WHERE status='dead'").fetchone()[0]
    if dead:
        lines.append(("⚠️ 写入失败", f"{dead} 项 Notion／提醒写入已放弃（todo-manager health 查看）"))
    quarantined = conn.execute("SELECT COUNT(*) FROM delegations WHERE status='quarantined'").fetchone()[0]
    if quarantined:
        lines.append(("⚠️ 委派异常", f"{quarantined} 个委派已隔离，需人工处理"))
    failed = conn.execute("SELECT COUNT(*) FROM feedback WHERE status='failed' AND applied_at >= ?",
                          (clock.iso(clock.now() - timedelta(days=7)),)).fetchone()[0]
    if failed:
        lines.append(("⚠️ 反馈未处理", f"近 7 天有 {failed} 条反馈处理失败"))
    status = "所有数据源同步正常" if not lines else f"{len(lines)} 项需要注意"
    return {"extra": [("状态", status), *lines, ("最近确认", f"{local:%Y-%m-%d %H}:00")]}


def run_gate(conn: sqlite3.Connection, cfg: Config, *, notion: Any | None, kanban: Any | None,
             notifier: Any | None) -> tuple[str, bool]:
    """``notion``: a NotionSync (or None when not configured); ``kanban``: a Kanban client."""
    run_id = runs.start(conn, "feedback")
    summary: dict[str, Any] = {}
    problems: list[str] = []
    try:
        _step(summary, problems, "claims", lambda: fail_exhausted_claims(conn, cfg))
        if notion is not None:
            _step(summary, problems, "pull_projects", lambda: notion.pull("project"))
            _step(summary, problems, "pull_tasks", lambda: notion.pull("task"))
            _step(summary, problems, "comments", lambda: notion.poll_comments(
                int(cfg.get("notion.cold_comment_pages_per_run", 30))))
        if kanban is not None:
            _step(summary, problems, "kanban_messages", lambda: delegation.send_messages(conn, cfg, kanban))
            _step(summary, problems, "kanban_submit", lambda: delegation.submit_pending(conn, cfg, kanban))
            _step(summary, problems, "kanban_cards", lambda: delegation.sync_cards(conn, cfg, kanban))
        handlers: dict[tuple[str, str], Any] = {}
        if notion is not None:
            _step(summary, problems, "dirty", notion.enqueue_dirty)
            handlers.update(notion.handlers())
        if notifier is not None:
            handlers[("matrix", "notify")] = notifier
        _step(summary, problems, "outbox", lambda: outbox.process(conn, handlers))
        if notion is not None:
            _step(summary, problems, "home", lambda: notion.sync_home(freshness_sections(conn, cfg)))
            if notion.problems:
                summary["notion_problems"] = notion.problems[:20]
        items = feedback.pending(conn, stale_minutes=int(cfg.get("feedback.stale_claim_minutes", 60)))
        items = items[: int(cfg.get("feedback.max_items_per_run", 20))]
        if problems:
            summary["problems"] = problems
        if not items:
            runs.finish(conn, run_id, status="idle", summary=summary)
            return '{"wakeAgent": false}', False
        ids = [f["id"] for f in items]
        feedback.claim(conn, run_id, ids)
        kinds: dict[str, int] = {}
        for f in items:
            kinds[f["kind"] or "other"] = kinds.get(f["kind"] or "other", 0) + 1
        manifest = {"run_id": run_id, "created_at": clock.iso(), "feedback_ids": ids}
        runs.write_json(cfg.home, run_id, "manifest.json", manifest)
        reasons = [f"feedback:{len(ids)}"]
        runs.finish(conn, run_id, status="woke", wake=True, reasons=reasons, summary=summary)
        lines = [
            f"Todo Manager feedback run: {run_id}",
            f"Feedback to interpret: {len(ids)} (" + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())) + ")",
            f"Do: load skill todo-manager, run `todo-manager feedback show {run_id}`, decide, then"
            f" `todo-manager feedback apply {run_id} --file <decisions.json>`.",
            '{"wakeAgent": true}',
        ]
        return "\n".join(lines), True
    except Exception as exc:  # noqa: BLE001 - never wake the model for a gate crash
        runs.finish(conn, run_id, status="error", summary=summary, error=f"{type(exc).__name__}: {exc}")
        return '{"wakeAgent": false}', False
