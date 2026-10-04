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

from . import clock, delegation, effects, feedback, feedback_flow, health, i18n, outbox, runs
from .config import Config
from .db import tx
from .notion import schema

Step = Callable[[], Any]
SOURCE_SCOPES = ("gmail", "slack", "github", "commit", "notion", "kanban")
# Steps whose failures are kept as channel health here (``health.record_channel``). The Notion
# pulls and the comment poll record their own (``notion``/``projects``, ``tasks``, ``bodies``,
# ``comments``) since a partly failed pass does not raise.
CHANNELS = {"claims": ("gate", "claims"), "kanban_settings": ("kanban", "settings"),
            "kanban_messages": ("kanban", "messages"), "kanban_submit": ("kanban", "submit"),
            "kanban_cards": ("kanban", "cards"), "alerts": ("gate", "alerts"), "dirty": ("notion", "dirty"),
            "outbox": ("gate", "outbox"), "home": ("notion", "home")}


def _step(conn: sqlite3.Connection, summary: dict[str, Any], problems: list[str], name: str, fn: Step) -> Any:
    channel = CHANNELS.get(name)
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 - isolate systems from each other
        problems.append(f"{name}: {type(exc).__name__}")
        summary[name] = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        if channel:
            health.record_channel(conn, *channel, f"{type(exc).__name__}: {exc}")
        return None
    summary[name] = result
    if channel:
        health.record_channel(conn, *channel, (result or {}).get("error") if isinstance(result, dict) else None)
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
                           i18n.t("fb.failed_alert", n=max_attempts, id=r["id"]),
                           task_id=r["scope_id"] if r["scope_type"] == "task" else None)
    return {"failed": len(rows)}


def freshness_sections(conn: sqlite3.Connection, cfg: Config, lang: str | None = None) -> dict[str, Any]:
    """Content of the home freshness callout. Stable within an hour when nothing is wrong."""
    def t(key: str, **values: Any) -> str:
        return i18n.t(key, lang, **values)

    local = clock.local_dt(cfg.tz)
    lines: list[tuple[str, str]] = []
    for source, scopes in health.stale_sources(conn, SOURCE_SCOPES).items():
        label = t("home.kanban_label") if source == "kanban" else schema.label("source", source, lang)
        for r in scopes:
            when = (f"{clock.local_dt(cfg.tz, clock.parse(r['last_success_at'])):%m-%d %H:%M}"
                    if r["last_success_at"] else t("home.never_succeeded"))
            detail = t("home.source_stale_detail", when=when, n=r["consecutive_failures"])
            if len(scopes) > health.GROUP_STALE_SCOPES:
                lines.append((t("home.sources_stale", label=label, n=len(scopes)),
                              t("home.sources_stale_detail", scope=r["scope"], detail=detail)))
                break
            lines.append((t("home.source_stale", label=label, scope=r["scope"]), detail))
    dead = conn.execute("SELECT COUNT(*) FROM outbox WHERE status='dead'").fetchone()[0]
    if dead:
        lines.append((t("home.dead_writes"), t("home.dead_writes_detail", n=dead)))
    quarantined = conn.execute("SELECT COUNT(*) FROM delegations WHERE status='quarantined'").fetchone()[0]
    if quarantined:
        lines.append((t("home.quarantined"), t("home.quarantined_detail", n=quarantined)))
    failed = conn.execute("SELECT COUNT(*) FROM feedback WHERE status='failed' AND applied_at >= ?",
                          (clock.iso(clock.now() - timedelta(days=7)),)).fetchone()[0]
    if failed:
        lines.append((t("home.failed_feedback"), t("home.failed_feedback_detail", n=failed)))
    status = t("home.all_ok") if not lines else t("home.attention", n=len(lines))
    return {"extra": [(t("home.status"), status), *lines, (t("home.last_checked"), f"{local:%Y-%m-%d %H}:00")]}


def sync_all(conn: sqlite3.Connection, cfg: Config, *, notion: Any | None, kanban: Any | None,
             notifier: Any | None) -> tuple[dict[str, Any], list[str]]:
    """Steps 1-4 (no model). ``notion``: a NotionSync or None; ``kanban``: a Kanban client or None."""
    summary: dict[str, Any] = {}
    problems: list[str] = []

    def step(name: str, fn: Step) -> Any:
        return _step(conn, summary, problems, name, fn)

    step("claims", lambda: fail_exhausted_claims(conn, cfg))
    if notion is not None:
        step("pull_projects", lambda: notion.pull("project"))
        step("pull_tasks", lambda: notion.pull("task"))
        step("comments", lambda: notion.poll_comments(int(cfg.get("notion.cold_comment_pages_per_run", 30))))
    if kanban is not None:
        on_board = conn.execute("SELECT 1 FROM delegations WHERE card_id IS NOT NULL AND status IN (%s) LIMIT 1"
                                % ",".join("?" * len(delegation.ACTIVE)), delegation.ACTIVE).fetchone()
        if on_board:  # cards in flight are exposed to the global switches; re-read them every run
            step("kanban_settings", lambda: delegation.check_settings(conn, kanban))
        step("kanban_messages", lambda: delegation.send_messages(conn, cfg, kanban))
        step("kanban_submit", lambda: delegation.submit_pending(conn, cfg, kanban))
        step("kanban_cards", lambda: delegation.sync_cards(conn, cfg, kanban))
    step("alerts", lambda: health.raise_alerts(conn, cfg))
    handlers: dict[tuple[str, str], Any] = {}
    if notion is not None:
        step("dirty", notion.enqueue_dirty)
        handlers.update(notion.handlers())
    if notifier is not None:
        handlers[("matrix", "notify")] = notifier
    step("outbox", lambda: outbox.process(conn, handlers))
    if notion is not None:
        step("home", lambda: notion.sync_home(freshness_sections(conn, cfg, notion.state.language)))
        # Kept until the next run replaces them: shown by `health`, alerted by the next alert step.
        health.record_problems(conn, "notion", notion.problems)
        if notion.problems:
            summary["notion_problems"] = notion.problems[:20]
    if problems:
        summary["problems"] = problems
    return summary, problems


def fit_context(conn: sqlite3.Connection, cfg: Config, run_id: str,
                items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """The oldest items whose rendered context fits ``feedback.max_context_chars`` (the rest
    stays pending for the next run), with that size. When not even the first fits, it is
    returned alone with its (over budget) size for the caller to defer or, if opted in, run."""
    budget = int(cfg.get("feedback.max_context_chars", 60000))
    for n in range(len(items), 0, -1):
        size = len(feedback_flow.render_context(conn, cfg, run_id, items[:n]))
        if size <= budget or n == 1:
            return items[:n], size
    return [], 0


def run_gate(conn: sqlite3.Connection, cfg: Config, *, notion: Any | None, kanban: Any | None,
             notifier: Any | None) -> tuple[str, bool]:
    run_id = runs.start(conn, "feedback")
    summary: dict[str, Any] = {}
    try:
        summary, _ = sync_all(conn, cfg, notion=notion, kanban=kanban, notifier=notifier)
        items = feedback.pending(conn, stale_minutes=int(cfg.get("feedback.stale_claim_minutes", 60)))
        items = items[: int(cfg.get("feedback.max_items_per_run", 20))]
        if not items:
            runs.finish(conn, run_id, status="idle", summary=summary)
            return '{"wakeAgent": false}', False
        items, context_chars = fit_context(conn, cfg, run_id, items)
        summary["context_chars"] = context_chars
        budget = int(cfg.get("feedback.max_context_chars", 60000))
        if context_chars > budget:
            summary["context_over_budget"] = context_chars
            if cfg.get("feedback.over_budget", "defer") != "proceed":
                summary["deferred_over_budget"] = budget   # nothing claimed; health reports and alerts it
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
