"""Reads shared by the command line and the HTTP API: each returns what the command prints, so
both interfaces answer alike. (The Web page's own read model is ``web/views.py``.)"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import clock, delegation, health, prefs
from .config import Config


def status(conn: sqlite3.Connection, cfg: Config, *, details: bool = False, days: int = 7) -> dict[str, Any]:
    """``todo-manager status``: counts, waiting material, agent work, last runs and problems."""
    from . import batching, hermes_install, runs
    report = health.report(conn, cfg)
    installation = hermes_install.status(cfg)
    paused_idle = {f'gate_idle:{entry}' for entry, spec in hermes_install.JOBS.items()
                   if spec['name'] in installation['paused_jobs']}
    problems = [text for key, text in health.problem_items(report, cfg) if key not in paused_idle]
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")}
    out = {
        "tasks": counts,
        "material": {"waiting": batching.backlog(conn),
                     "deferred": conn.execute("SELECT COUNT(*) FROM batches WHERE state='mature' AND not_before > ?",
                                              (clock.iso(),)).fetchone()[0]},
        "feedback_pending": conn.execute("SELECT COUNT(*) FROM feedback WHERE status='pending'").fetchone()[0],
        "delegation_config": {
            "enabled": bool(cfg.get('kanban.enabled')),
            "board": cfg.get('kanban.board'),
            "assignee": cfg.get('kanban.assignee') or None,
            "message": ('Agent delegation is enabled; see health problems for readiness.'
                        if cfg.get('kanban.enabled') else
                        'Agent delegation is disabled; source intake and Notion management remain available. '
                        'Run todo-manager install to configure it.'),
        },
        "agent_work": {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM delegations GROUP BY status")},
        "last_runs": {entry: [{k: r[k] for k in ("id", "started_at", "status", "wake_reasons")}
                              for r in runs.recent(conn, entry, 3)] for entry in ("intake", "feedback")},
        "conventions": {"file": str(prefs.agents_path(cfg.home)),
                        "preferences_file": str(prefs.preferences_path(cfg.home)),
                        "problems": prefs.problems(cfg.home)},
        "installation": installation,
        "problems": list(dict.fromkeys(problems + installation['problems'])),
    }
    if details:
        out['paths'] = {'home': str(cfg.home), 'database': str(cfg.db_path), 'language': cfg.language}
        out['health'] = report
        out['metrics'] = health.metrics(conn, days)
        manifest = hermes_install.load_manifest(hermes_install.Paths.from_config(cfg))
        out['usage'] = health.usage(cfg, [j['id'] for j in (manifest.get('jobs') or {}).values() if j.get('id')], days)
    return out


def source_search(conn: sqlite3.Connection, words: str, *, mine: bool = False, since: str | None = None,
                  limit: int = 20) -> dict[str, Any]:
    """``source search``: stored material containing the words, with an excerpt each."""
    from .sources.base import search_items
    rows = search_items(conn, words, mine=mine, since=since, limit=limit)
    return {"items": [{**{k: v for k, v in r.items() if k != "text"}, "excerpt": " ".join(r["text"].split())[:300]}
                      for r in rows], "content_role": "untrusted source material"}


def source_show(conn: sqlite3.Connection, item_id: str) -> dict[str, Any] | None:
    """``source show``: one item of material with its full text."""
    from .sources.base import item_body
    row = conn.execute("SELECT * FROM source_items WHERE id=?", (item_id,)).fetchone()
    if row is None:
        return None
    return {**dict(row), "body": item_body(conn, item_id) or row['excerpt'] or '',
            "content_role": "untrusted source material"}


def delegate_list(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """``delegate list``: agent work in progress."""
    active = conn.execute(f"SELECT id FROM delegations WHERE status IN ({','.join(repr(s) for s in delegation.ACTIVE)})"
                          " ORDER BY created_at").fetchall()
    return [delegation.get(conn, r["id"]) for r in active]


def delegate_show(conn: sqlite3.Connection, delegation_id: str) -> dict[str, Any] | None:
    """``delegate show``: one delegation with its messages, sub-cards, deliveries and approvals."""
    d = delegation.get(conn, delegation_id)
    if d is None:
        return None
    d["messages"] = [{k: m[k] for k in ("id", "kind", "status", "created_at", "sent_at", "error", "body")}
                     for m in conn.execute("SELECT * FROM delegation_messages WHERE delegation_id=?"
                                           " ORDER BY created_at, rowid", (delegation_id,))]
    d["children"] = [dict(r) for r in conn.execute(
        "SELECT * FROM delegation_children WHERE delegation_id=? ORDER BY card_id", (delegation_id,))]
    d["deliveries"] = delegation.deliveries(conn, delegation_id)
    d["approvals"] = [dict(a) for a in conn.execute("SELECT * FROM approvals WHERE delegation_id=?"
                                                     " ORDER BY granted_at", (delegation_id,))]
    return d
