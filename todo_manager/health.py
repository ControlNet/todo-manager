"""Operational health: source freshness, queues, stuck work and recent runs."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import clock, config, hermes_install, prefs
from .config import Config
from .db import dumps, get_meta, loads, set_meta, tx

STALE_SOURCE_MINUTES = 30
GROUP_STALE_SCOPES = 3   # more stale scopes than this of one source are reported as one line
ENTRIES = ("intake", "feedback")


def stale_sources(conn: sqlite3.Connection, sources: tuple[str, ...] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Failing or stale source scopes, grouped by source. A scope may go ``STALE_SOURCE_MINUTES``
    plus its own polling interval (cursor meta ``poll_interval_minutes``) without a success; a
    scope read on a schedule (meta ``next_due_at``, e.g. the daily commit digest) only once its
    next read was due, and a channel of the gate itself (meta ``channel``: Notion comments and
    bodies, the Kanban steps) only while it fails. Any failure since the last success counts.
    Retired scopes (dropped from a discovered list) are left out."""
    found: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute("SELECT * FROM cursors ORDER BY source, scope"):
        meta = loads(r["meta"], {})
        if (sources is not None and r["source"] not in sources) or meta.get("retired"):
            continue
        if r["consecutive_failures"]:
            stale = True
        elif meta.get("channel"):
            stale = False
        elif meta.get("next_due_at"):
            stale = clock.now() > clock.parse(meta["next_due_at"]) + timedelta(minutes=STALE_SOURCE_MINUTES)
        else:
            age = clock.minutes_since(r["last_success_at"])
            stale = age is None or age > STALE_SOURCE_MINUTES + int(meta.get("poll_interval_minutes") or 0)
        if stale:
            found.setdefault(r["source"], []).append(dict(r))
    return found


def record_channel(conn: sqlite3.Connection, source: str, scope: str, error: str | None) -> None:
    """Persist the outcome of one of the gate's own channels (``source``/``scope`` in the
    cursors table, marked ``channel``): an error adds a consecutive failure, a clean pass
    clears them. Reported like a failing source; never wakes the model."""
    now = clock.iso()
    with tx(conn):
        if error is None:
            conn.execute(
                "INSERT INTO cursors(source, scope, last_attempt_at, last_success_at, consecutive_failures, meta)"
                " VALUES (?,?,?,?,0,'{\"channel\":true}') ON CONFLICT(source, scope) DO UPDATE SET"
                " last_attempt_at=excluded.last_attempt_at, last_success_at=excluded.last_success_at,"
                " last_error=NULL, consecutive_failures=0,"
                " meta=json_set(COALESCE(cursors.meta, '{}'), '$.channel', json('true'))",
                (source, scope, now, now))
        else:
            conn.execute(
                "INSERT INTO cursors(source, scope, last_attempt_at, last_error, consecutive_failures, meta)"
                " VALUES (?,?,?,?,1,'{\"channel\":true}') ON CONFLICT(source, scope) DO UPDATE SET"
                " last_attempt_at=excluded.last_attempt_at, last_error=excluded.last_error,"
                " consecutive_failures=cursors.consecutive_failures+1,"
                " meta=json_set(COALESCE(cursors.meta, '{}'), '$.channel', json('true'))",
                (source, scope, now, error[:1000]))


def record_problems(conn: sqlite3.Connection, origin: str, found: list[str]) -> None:
    """Keep the problems one gate run found in ``origin`` (e.g. Notion pages it could not use)
    until the next run replaces them: reported by ``health`` and the Notion home page. A run
    that finds none clears them."""
    key = f"health.problems.{origin}"
    found = list(dict.fromkeys(found))[:20]
    with tx(conn):
        if found:
            set_meta(conn, key, dumps({"at": clock.iso(), "problems": found}))
        else:
            conn.execute("DELETE FROM meta WHERE key=?", (key,))


def report(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    out: dict[str, Any] = {"at": clock.iso(), "schema": get_meta(conn, "schema_version"),
                           "conventions": {"path": str(prefs.agents_path(cfg.home))}}
    out["sources"] = [{"source": r["source"], "scope": r["scope"], "last_success_at": r["last_success_at"],
                       "minutes_since_success": round(clock.minutes_since(r["last_success_at"]) or -1),
                       "failures": r["consecutive_failures"], "error": (r["last_error"] or "")[:200],
                       "channel": bool(loads(r["meta"], {}).get("channel")),
                       "next_due_at": loads(r["meta"], {}).get("next_due_at")}
                      for r in conn.execute("SELECT * FROM cursors ORDER BY source, scope")]
    stale = {(r["source"], r["scope"]) for rows in stale_sources(conn).values() for r in rows}
    out["stale_sources"] = {}
    for s in out["sources"]:
        if (s["source"], s["scope"]) in stale:
            out["stale_sources"].setdefault(s["source"], []).append(s)
    out["outbox"] = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM outbox GROUP BY status")}
    out["outbox_dead"] = [{"id": r["id"], "op": f"{r['target']}/{r['op']}", "entity": r["entity_id"],
                           "error": (r["last_error"] or "")[:200]}
                          for r in conn.execute("SELECT * FROM outbox WHERE status='dead' ORDER BY updated_at DESC"
                                                " LIMIT 20")]
    out["feedback"] = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM feedback GROUP BY status")}
    out["feedback_held"] = [{"id": r["id"], "reason": r["hold_reason"]} for r in conn.execute(
        "SELECT id, hold_reason FROM feedback WHERE status='pending' AND hold_reason IS NOT NULL"
        " ORDER BY received_at LIMIT 20")]
    out["delegations"] = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM delegations GROUP BY status")}
    out["delegation_attention"] = {
        "cancel_unconfirmed": [r["id"] for r in conn.execute(
            "SELECT id FROM delegations WHERE cancel_state='unconfirmed' ORDER BY updated_at DESC LIMIT 20")],
        "approval_outcome_unknown": [r["id"] for r in conn.execute(
            "SELECT id FROM approvals WHERE outcome='unknown' ORDER BY consumed_at DESC LIMIT 20")],
        "messages_unconfirmed": conn.execute(
            "SELECT COUNT(*) FROM delegation_messages WHERE status='sending'").fetchone()[0]}
    out["batches"] = {r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) n FROM batches GROUP BY state")}
    out["followups_failed"] = [r["task_id"] for r in conn.execute(
        "SELECT f.task_id FROM followup_checks f JOIN tasks t ON t.id=f.task_id WHERE f.state='failed'"
        " AND t.status IN ('todo','in_progress','waiting') AND f.due=substr(t.follow_up_date,1,10)"
        " ORDER BY f.updated_at DESC LIMIT 20")]
    out["runs"] = {}
    for entry in ENTRIES:
        last = conn.execute("SELECT * FROM runs WHERE entry=? ORDER BY started_at DESC LIMIT 1", (entry,)).fetchone()
        woke = conn.execute("SELECT * FROM runs WHERE entry=? AND wake=1 ORDER BY started_at DESC LIMIT 1",
                            (entry,)).fetchone()
        out["runs"][entry] = {
            "last": dict(id=last["id"], at=last["started_at"], status=last["status"], error=last["error"])
            if last else None,
            "last_wake": dict(id=woke["id"], at=woke["started_at"], completed=bool(woke["agent_completed_at"]),
                              over_budget=loads(woke["summary"], {}).get("context_over_budget"))
            if woke else None}
    out["delegations_held"] = loads(get_meta(conn, "delegations.hold"), None)
    # Information, not problems: how the user's Hermes settings treat agent work.
    out["notes"] = ((loads(get_meta(conn, "kanban.settings"), None) or {}).get("notes", [])
                    + config.removed_keys(cfg.home))
    out["conventions_problems"] = prefs.problems(cfg.home) + hermes_install.context_problems(cfg)
    out["notion_ready"] = bool(get_meta(conn, "notion.tasks_data_source_id") or cfg.get("notion.tasks_data_source_id"))
    if cfg.get('kanban.enabled'):
        from .kanban import BridgeKanban, KanbanError
        try:
            if not cfg.get('kanban.assignee') or not BridgeKanban.from_config(cfg).profile_valid(cfg.get('kanban.assignee')):
                out['conventions_problems'].append('kanban.assignee: configure a valid dispatchable Hermes profile; delegations are not ready')
        except (KanbanError, OSError) as exc:
            out['conventions_problems'].append(f'kanban.assignee: could not validate profile: {exc}')
    out["recorded_problems"] = {r["key"].split(".", 2)[2]: loads(r["value"], {})
                                for r in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'health.problems.%'")}
    items = problem_items(out, cfg)
    out["problems"] = [text for _, text in items]
    out["problem_keys"] = [key for key, _ in items]
    return out


def problems(rep: dict[str, Any], cfg: Config) -> list[str]:
    return [text for _, text in problem_items(rep, cfg)]


def problem_items(rep: dict[str, Any], cfg: Config) -> list[tuple[str, str]]:
    """Current problems as (identity, text). The identity names what is wrong (a source scope,
    a kind of problem) and never the details that change from run to run (error text, counts,
    minutes), so it stays one problem however its text changes."""
    found: list[tuple[str, str]] = []

    def add(key: str, text: str) -> None:
        found.append((key, text.strip()))

    for source, scopes in rep["stale_sources"].items():
        s = scopes[0]
        if len(scopes) > GROUP_STALE_SCOPES:
            add(f"source:{source}:*", f"source {source}: {len(scopes)} scopes stale, e.g. {s['scope']}"
                f" ({s['minutes_since_success']} min, {s['failures']} failures) {s['error']}")
            continue
        for s in scopes:
            state = "failing" if s.get("channel") else "stale"
            add(f"source:{source}:{s['scope']}", f"source {source}:{s['scope']} {state}"
                f" ({s['minutes_since_success']} min, {s['failures']} failures) {s['error']}")
    for origin, recorded in sorted((rep.get("recorded_problems") or {}).items()):
        for text in recorded.get("problems", []):
            add(f"{origin}:{text}", f"{origin}: {text}")
    for text in rep.get("conventions_problems") or []:
        add(f"conventions:{text.split(':', 1)[0]}", f"conventions: {text}")
    if rep.get("notion_ready") and not cfg.get("identity.notion_user_ids"):
        add("identity.notion_user_ids", "identity.notion_user_ids is empty: Notion comments and edits are treated as"
            " evidence only, never as the user's instructions")
    if rep["outbox_dead"]:
        add("outbox_dead", f"{len(rep['outbox_dead'])} outbox effects gave up")
    if rep.get("feedback_held"):
        add("feedback_held", "feedback held, not handled until resolved: " + "; ".join(
            f"{h['id']} ({h['reason']})" for h in rep["feedback_held"][:5]))
    if rep["feedback"].get("failed"):
        add("feedback_failed", f"{rep['feedback']['failed']} feedback items failed")
    if rep["delegations"].get("quarantined"):
        add("delegations_quarantined", f"{rep['delegations']['quarantined']} delegations quarantined")
    if rep.get("delegations_held"):
        hold = rep["delegations_held"]
        add("delegations_held", f"delegations held since {hold.get('reason')} at {hold.get('at')}: check the Kanban"
            " board, then `todo-manager delegate release-hold`")
    attention = rep.get("delegation_attention") or {}
    if attention.get("cancel_unconfirmed"):
        add("cancel_unconfirmed", f"cancelled delegations whose worker was not confirmed stopped: "
            f"{', '.join(attention['cancel_unconfirmed'])}")
    if attention.get("approval_outcome_unknown"):
        add("approval_outcome_unknown", f"approved external actions with unknown outcome (paused, check manually): "
            f"{', '.join(attention['approval_outcome_unknown'])}")
    if rep.get("followups_failed"):
        add("followups_failed", f"follow-ups no run handled (set a new follow-up date to retry): "
            f"{', '.join(rep['followups_failed'])}")
    for entry, info in rep["runs"].items():
        last = info["last"]
        if last is None:
            continue
        age = clock.minutes_since(last["at"]) or 0
        if age > int(cfg.get("health.gate_idle_minutes", 60)):
            add(f"gate_idle:{entry}", f"{entry} gate has not run for {round(age)} min")
        if last["status"] == "error":
            add(f"gate_error:{entry}", f"{entry} gate error: {last['error']}")
        woke = info["last_wake"]
        if woke and woke.get("over_budget"):
            setting = "wake.max_context_chars" if entry == "intake" else "feedback.max_context_chars"
            add(f"over_budget:{entry}", f"{entry} run {woke['id']} context was over budget ({woke['over_budget']}"
                f" chars) even with one conversation or item, clipped: it ran anyway; fewer open tasks/projects or"
                f" a larger {setting}")
        if woke and not woke["completed"] and (clock.minutes_since(woke["at"]) or 0) > 90:
            add(f"never_done:{entry}:{woke['id']}", f"{entry} run {woke['id']} woke the model but never reported"
                " `todo-manager run done`; what it left was offered again after the claim expired")
    return found


def metrics(conn: sqlite3.Connection, days: int = 7) -> dict[str, Any]:
    since = clock.iso(clock.now() - timedelta(days=days))
    rows = conn.execute("SELECT entry, status, wake, agent_completed_at, summary FROM runs WHERE started_at >= ?",
                        (since,)).fetchall()
    out: dict[str, Any] = {"days": days}
    for entry in ENTRIES:
        mine = [r for r in rows if r["entry"] == entry]
        out[entry] = {"runs": len(mine), "woke": sum(r["wake"] for r in mine),
                      "completed": sum(1 for r in mine if r["agent_completed_at"]),
                      "errors": sum(1 for r in mine if r["status"] == "error")}
    out["tasks_created"] = conn.execute("SELECT COUNT(*) FROM tasks WHERE created_at >= ?", (since,)).fetchone()[0]
    out["feedback_applied"] = conn.execute("SELECT COUNT(*) FROM feedback WHERE applied_at >= ? AND status='applied'",
                                           (since,)).fetchone()[0]
    out["deliveries"] = conn.execute("SELECT COUNT(*) FROM deliveries WHERE received_at >= ?", (since,)).fetchone()[0]
    out["wake_reasons"] = {}
    for r in conn.execute("SELECT wake_reasons FROM runs WHERE wake=1 AND started_at >= ?", (since,)):
        for reason in loads(r["wake_reasons"], []):
            key = reason.split(":", 1)[0]
            out["wake_reasons"][key] = out["wake_reasons"].get(key, 0) + 1
    return out


def usage(cfg: Config, job_ids: list[str], days: int = 7) -> dict[str, Any]:
    """Model usage of the todo-manager cron jobs from Hermes' cron usage audit (read-only)."""
    hermes_home = Path(cfg.get("hermes.home") or "~/.hermes").expanduser()
    path = hermes_home / "cron" / "usage_audit.jsonl"
    out = {"runs": 0, "prompt_tokens": 0, "completion_tokens": 0, "errors": 0, "source": str(path)}
    if not path.exists() or not job_ids:
        return out
    since = clock.now() - timedelta(days=days)
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("job_id") not in job_ids:
            continue
        when = clock.parse(rec.get("ts")) if isinstance(rec.get("ts"), str) else None
        if when is not None and when < since:
            continue
        out["runs"] += 1
        out["prompt_tokens"] += int(rec.get("prompt_tokens") or 0)
        out["completion_tokens"] += int(rec.get("completion_tokens") or 0)
        out["errors"] += 1 if rec.get("error") else 0
    return out
