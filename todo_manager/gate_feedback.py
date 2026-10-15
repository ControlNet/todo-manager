"""Pre-run gate of the feedback job (Hermes cron, every few minutes): sync, then feedback.

Deterministic steps, each isolated so one failing system does not stall the others:

1. Notion: pull the user's property/page edits (projects, then tasks) and poll comments.
2. Kanban: send queued messages (answers, supplements, pauses), create the cards of new
   delegations, let parked cards go once the start time the user gave has passed, read card
   events (questions, deliveries, failures). The cards themselves run in Hermes whether or not
   this job runs; their results are read when it runs again.
3. Dirty scan + outbox: push local changes to Notion, post bot comments; with ``email.labels``,
   the Gmail labels of mail linked to tasks (``gmail_labels``).
4. Home page freshness callout (rewritten only when its content changes).
5. Claim the feedback that needs interpretation and print it as the run's context
   (``context.feedback``); nothing to interpret -> ``{"wakeAgent": false}``.

No model runs for steps 1-4. A failing channel is reported in Notion and by ``health``; one
that keeps failing with nothing else to do wakes a short report at most once a day
(``[CRON_FAILURE]``). A failure of the gate itself raises ``GateError`` (a failed script).
"""

from __future__ import annotations

import sqlite3
import subprocess
from datetime import timedelta
from typing import Any, Callable

from . import clock, context, delegation, feedback, gmail_labels, health, i18n, ops, outbox, prefs, runs
from .config import Config
from .db import tx
from .gate_intake import IDLE, GateError, problems_due
from .locks import LockBusy, file_lock, lock_path
from .notion import schema

Step = Callable[[], Any]
SOURCE_SCOPES = ("gmail", "slack", "github", "commit", "notion", "kanban", "daemon")
# Steps whose failures are kept as channel health here (``health.record_channel``). The Notion
# pulls and the comment poll record their own (``notion``/``projects``, ``tasks``, ``bodies``,
# ``comments``) since a partly failed pass does not raise.
CHANNELS = {"claims": ("gate", "claims"),
            "kanban_messages": ("kanban", "messages"), "kanban_submit": ("kanban", "submit"),
            "kanban_start": ("kanban", "start"),
            "kanban_cards": ("kanban", "cards"), "dirty": ("notion", "dirty"),
            "outbox": ("gate", "outbox"), "gmail_labels": ("gate", "gmail_labels"), "home": ("notion", "home")}


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
    """Feedback no run resolved after ``max_attempts`` claims (lease expired) is marked failed."""
    stale = int(cfg.get("feedback.stale_claim_minutes", 60))
    max_attempts = int(cfg.get("feedback.max_attempts", 3))
    cutoff = clock.iso(clock.now() - timedelta(minutes=stale))
    rows = conn.execute("SELECT id, attempts FROM feedback WHERE status='pending' AND claimed_run IS NOT NULL"
                        " AND claimed_at < ? AND attempts >= ?", (cutoff, max_attempts)).fetchall()
    with tx(conn):
        for r in rows:
            feedback.fail(conn, r["id"], r["attempts"])
    return {"failed": len(rows)}


def freshness_sections(conn: sqlite3.Connection, cfg: Config, lang: str | None = None) -> dict[str, Any]:
    """Content of the home freshness callout. Stable within an hour when nothing is wrong."""
    def t(key: str, **values: Any) -> str:
        return i18n.t(key, lang, **values)

    local = clock.local_dt(cfg.tz)
    lines: list[tuple[str, str]] = []
    for source, scopes in health.stale_sources(conn, SOURCE_SCOPES).items():
        label = (t("home.kanban_label") if source == "kanban" else t("home.daemon_label") if source == "daemon"
                 else schema.label("source", source, lang))
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


def notion_pull_steps(step: Callable[[str, Step], Any], notion: Any, cfg: Config) -> None:
    """Notion -> local: property edits (projects, then tasks, with body reads) and comments."""
    step("pull_projects", lambda: notion.pull("project"))
    step("pull_tasks", lambda: notion.pull("task"))
    step("comments", lambda: notion.poll_comments(int(cfg.get("notion.cold_comment_pages_per_run", 30))))


def kanban_send_steps(step: Callable[[str, Step], Any], conn: sqlite3.Connection, cfg: Config, kanban: Any) -> None:
    """Local -> board: queued messages, cards of new delegations, parked cards whose time has come."""
    step("kanban_messages", lambda: delegation.send_messages(conn, cfg, kanban))
    step("kanban_submit", lambda: delegation.submit_pending(conn, cfg, kanban))
    step("kanban_start", lambda: delegation.start_due(conn, cfg, kanban))


def kanban_read_steps(step: Callable[[str, Step], Any], conn: sqlite3.Connection, cfg: Config, kanban: Any) -> None:
    """Board -> local: card events (questions, deliveries, failures) and the board's switches."""
    step("kanban_cards", lambda: delegation.sync_cards(conn, cfg, kanban))
    step("kanban_settings", lambda: delegation.observe_settings(conn, kanban))


def push_steps(step: Callable[[str, Step], Any], conn: sqlite3.Connection, notion: Any | None) -> None:
    """Local -> Notion: the dirty scan, then the outbox."""
    handlers: dict[tuple[str, str], Any] = {}
    if notion is not None:
        step("dirty", notion.enqueue_dirty)
        handlers.update(notion.handlers())
    step("outbox", lambda: outbox.process(conn, handlers))


def label_step(step: Callable[[str, Step], Any], conn: sqlite3.Connection, cfg: Config) -> None:
    """Local -> Gmail: labels of the mail linked to tasks, when ``email.labels`` is on."""
    if gmail_labels.enabled(cfg):
        step("gmail_labels", lambda: gmail_labels.sync(conn, cfg))


def home_step(step: Callable[[str, Step], Any], conn: sqlite3.Connection, cfg: Config, notion: Any,
              summary: dict[str, Any]) -> None:
    step("home", lambda: notion.sync_home(freshness_sections(conn, cfg, notion.state.language)))
    # Kept until the next run replaces them: shown by `health` and the next home page refresh.
    health.record_problems(conn, "notion", notion.problems)
    if notion.problems:
        summary["notion_problems"] = notion.problems[:20]


def sync_all(conn: sqlite3.Connection, cfg: Config, *, notion: Any | None,
             kanban: Any | None) -> tuple[dict[str, Any], list[str]]:
    """Steps 1-4 (no model). ``notion``: a NotionSync or None; ``kanban``: a Kanban client or None."""
    summary: dict[str, Any] = {}
    problems: list[str] = []

    def step(name: str, fn: Step) -> Any:
        return _step(conn, summary, problems, name, fn)

    step("claims", lambda: fail_exhausted_claims(conn, cfg))
    if notion is not None:
        notion_pull_steps(step, notion, cfg)
    if kanban is not None:
        kanban_send_steps(step, conn, cfg, kanban)
        kanban_read_steps(step, conn, cfg, kanban)
    push_steps(step, conn, notion)
    label_step(step, conn, cfg)
    if notion is not None:
        home_step(step, conn, cfg, notion, summary)
    if problems:
        summary["problems"] = problems
    return summary, problems


def sync_or_leave(conn: sqlite3.Connection, cfg: Config, *, notion: Any | None, kanban: Any | None,
                  runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Steps 1-4, unless a healthy daemon does them (docs/realtime-sync.md). A daemon that is
    enabled but unhealthy is reported (and its unit restarted) and the gate syncs as before. The
    ``sync`` lock keeps the gate, the daemon and ``todo-manager sync`` from syncing at once."""
    from . import daemon
    state = daemon.health_of(cfg)
    if state["healthy"]:
        health.record_channel(conn, "daemon", "alive", None)
        return {"sync": "daemon"}
    if state["enabled"]:
        daemon.revive(conn, cfg, state["reason"], runner)
    try:
        with file_lock(lock_path(cfg.home, "sync")):
            summary, _ = sync_all(conn, cfg, notion=notion, kanban=kanban)
    except LockBusy:
        return {"sync": "busy"}
    if state["enabled"]:
        summary["daemon"] = state["reason"]
    return summary


def fit_context(conn: sqlite3.Connection, cfg: Config, run_id: str, items: list[dict[str, Any]],
                problems: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """The oldest items whose rendered context fits ``feedback.max_context_chars`` (the rest
    stays pending for the next run). The first one is always taken: item text is clipped with
    a pointer to the full text, so no item waits forever."""
    budget = int(cfg.get("feedback.max_context_chars", 60000))
    incomplete = {f["id"]: ops.incomplete_reason(f) for f in items}
    for n in range(len(items), 0, -1):
        text = context.feedback(conn, cfg, run_id, items[:n], incomplete, problems)
        if len(text) <= budget or n == 1:
            return items[:n], text
    return [], ""


def run_gate(conn: sqlite3.Connection, cfg: Config, *, notion: Any | None,
             kanban: Any | None) -> tuple[str, bool]:
    run_id = runs.start(conn, "feedback")
    summary: dict[str, Any] = {}
    try:
        summary = sync_or_leave(conn, cfg, notion=notion, kanban=kanban)
        problems = problems_due(conn, cfg, "feedback", ("notion", "kanban", "gate"))
        if problems:
            summary["problems_reported"] = [f"{p['source']}:{p['scope']}" for p in problems]
        items = feedback.pending(conn, stale_minutes=int(cfg.get("feedback.stale_claim_minutes", 60)))
        waiting = [f["id"] for f in items if ops.not_ready(conn, cfg, f)]
        if waiting:
            summary["not_ready"] = len(waiting)   # held, or waiting for a body read that covers the last edit
        items = [f for f in items if f["id"] not in waiting][: int(cfg.get("feedback.max_items_per_run", 20))]
        if not items:
            if problems:
                runs.finish(conn, run_id, status="woke", wake=True, reasons=["problems"], summary=summary)
                shown = context.final_check(context.problems_only(cfg, run_id, "feedback", problems))
                runs.write_text(cfg.home, run_id, "context.md", shown)   # what the agent was shown
                return shown, True
            runs.finish(conn, run_id, status="idle", summary=summary)
            return IDLE, False
        feedback.claim(conn, run_id, [f["id"] for f in items])
        items, text = fit_context(conn, cfg, run_id, items, problems)
        claimed = {f["id"] for f in items}
        with tx(conn):    # what did not fit goes back without counting as an attempt
            for fb in feedback.claimed_by(conn, run_id):
                if fb["id"] not in claimed:
                    conn.execute("UPDATE feedback SET claimed_run=NULL, claimed_at=NULL, attempts=MAX(attempts-1, 0)"
                                 " WHERE id=?", (fb["id"],))
        summary["context_chars"] = len(text)
        if len(text) > int(cfg.get("feedback.max_context_chars", 60000)):
            summary["context_over_budget"] = len(text)   # one clipped item over budget: run anyway (health)
        manifest = {"run_id": run_id, "created_at": clock.iso(), "feedback_ids": sorted(claimed)}
        runs.write_json(cfg.home, run_id, "manifest.json", manifest)
        runs.finish(conn, run_id, status="woke", wake=True, reasons=[f"feedback:{len(claimed)}"], summary=summary)
        shown = context.final_check(text)
        runs.write_text(cfg.home, run_id, "context.md", shown)   # what the agent was shown
        return shown, True
    except Exception as exc:  # noqa: BLE001 - recorded, then reported to Hermes as a failed script
        runs.finish(conn, run_id, status="error", summary=summary, error=f"{type(exc).__name__}: {exc}")
        raise GateError(f"feedback gate failed in run {run_id}: {type(exc).__name__}: {exc}") from exc
