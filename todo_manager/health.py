"""Operational health: source freshness, queues, stuck work and recent runs."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import clock, effects, i18n
from .config import Config
from .db import get_meta, loads

STALE_SOURCE_MINUTES = 30
GROUP_STALE_SCOPES = 3   # more stale scopes than this of one source are reported as one line
ENTRIES = ("intake", "feedback")


def stale_sources(conn: sqlite3.Connection, sources: tuple[str, ...] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Failing or stale source scopes, grouped by source. A scope may go ``STALE_SOURCE_MINUTES``
    plus its own polling interval (cursor meta ``poll_interval_minutes``) without a success."""
    found: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute("SELECT * FROM cursors ORDER BY source, scope"):
        if sources is not None and r["source"] not in sources:
            continue
        age = clock.minutes_since(r["last_success_at"])
        allowed = STALE_SOURCE_MINUTES + int(loads(r["meta"], {}).get("poll_interval_minutes") or 0)
        if r["consecutive_failures"] or age is None or age > allowed:
            found.setdefault(r["source"], []).append(dict(r))
    return found


def report(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    out: dict[str, Any] = {"at": clock.iso(), "schema": get_meta(conn, "schema_version"),
                           "rules_version": int(get_meta(conn, "rules_version", "0") or 0)}
    out["sources"] = [{"source": r["source"], "scope": r["scope"], "last_success_at": r["last_success_at"],
                       "minutes_since_success": round(clock.minutes_since(r["last_success_at"]) or -1),
                       "failures": r["consecutive_failures"], "error": (r["last_error"] or "")[:200]}
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
    out["delegations"] = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM delegations GROUP BY status")}
    out["batches"] = {r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) n FROM batches GROUP BY state")}
    out["runs"] = {}
    for entry in ENTRIES:
        last = conn.execute("SELECT * FROM runs WHERE entry=? ORDER BY started_at DESC LIMIT 1", (entry,)).fetchone()
        woke = conn.execute("SELECT * FROM runs WHERE entry=? AND wake=1 ORDER BY started_at DESC LIMIT 1",
                            (entry,)).fetchone()
        out["runs"][entry] = {
            "last": dict(id=last["id"], at=last["started_at"], status=last["status"], error=last["error"]) if last else None,
            "last_wake": dict(id=woke["id"], at=woke["started_at"], completed=bool(woke["agent_completed_at"]))
            if woke else None}
    out["notion_ready"] = bool(get_meta(conn, "notion.tasks_data_source_id") or cfg.get("notion.tasks_data_source_id"))
    out["problems"] = problems(out, cfg)
    return out


def problems(rep: dict[str, Any], cfg: Config) -> list[str]:
    found = []
    for source, scopes in rep["stale_sources"].items():
        s = scopes[0]
        if len(scopes) > GROUP_STALE_SCOPES:
            found.append(f"source {source}: {len(scopes)} scopes stale, e.g. {s['scope']} ({s['minutes_since_success']} min, "
                         f"{s['failures']} failures) {s['error']}".strip())
            continue
        for s in scopes:
            found.append(f"source {source}:{s['scope']} stale ({s['minutes_since_success']} min, "
                         f"{s['failures']} failures) {s['error']}".strip())
    if rep["outbox_dead"]:
        found.append(f"{len(rep['outbox_dead'])} outbox effects gave up")
    if rep["feedback"].get("failed"):
        found.append(f"{rep['feedback']['failed']} feedback items failed")
    if rep["delegations"].get("quarantined"):
        found.append(f"{rep['delegations']['quarantined']} delegations quarantined")
    for entry, info in rep["runs"].items():
        last = info["last"]
        if last is None:
            continue
        age = clock.minutes_since(last["at"]) or 0
        if age > int(cfg.get("notify.failure_alert_after_minutes", 60)):
            found.append(f"{entry} gate has not run for {round(age)} min")
        if last["status"] == "error":
            found.append(f"{entry} gate error: {last['error']}")
        woke = info["last_wake"]
        if woke and not woke["completed"] and (clock.minutes_since(woke["at"]) or 0) > 90:
            found.append(f"{entry} run {woke['id']} woke the model but never applied its decisions")
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


def raise_alerts(conn: sqlite3.Connection, cfg: Config) -> dict[str, int]:
    """Queue a Matrix reminder for each current problem, at most once per problem kind per day."""
    found = report(conn, cfg)["problems"]
    day = clock.local_date(cfg.tz).isoformat()
    for text in found:
        kind = hashlib.sha1(re.sub(r"\d+", "#", text).encode()).hexdigest()[:12]
        effects.notify(conn, cfg, f"health:{kind}:{day}", i18n.t("health.alert", text=text))
    return {"problems": len(found)}


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
