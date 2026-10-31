"""What the Web UI reads: plain JSON built from the database (no writes here).

A task is shown by what it needs from the user (``kind``): ``decision`` (to decide), ``todo``
(the user's own to-do), ``answer`` / ``review`` (agent work waiting for the user: a question, a
delivery), ``running`` (agent work going on or about to start), ``waiting`` (on someone else),
``closed``. The home groups follow the Notion home sections.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .. import clock, delegation, store
from ..config import Config
from ..db import get_meta, loads

GROUPS = ("decide", "mine", "agent_wait", "agent_run", "waiting")
CHOICE_KINDS = ("decision_choice", "delegate_request")
DERIVED_KINDS = ("delegate_unassigned",)   # the program's wording only: shown by its reply
THREAD_LIMIT = 60
HISTORY_LIMIT = 30
SEARCH_LIMIT = 20
TEXT_PREVIEW_BYTES = 256 * 1024


def today(cfg: Config) -> date:
    return clock.local_date(cfg.tz)


def _day(value: str | None) -> str | None:
    return value[:10] if value else None


def _snoozed(task: dict[str, Any], on: date) -> bool:
    return bool(task.get("snooze_until")) and task["snooze_until"][:10] > on.isoformat()


def kind_of(conn: sqlite3.Connection, task: dict[str, Any]) -> tuple[str, str | None]:
    """(kind, agent label): what the task needs from the user, and its "Agent work" label."""
    if task["status"] in store.CLOSED_STATUSES:
        return "closed", None
    label = delegation.task_label(conn, task["id"])
    progress = delegation.PROGRESS.get(label or "")
    if progress == "answer":
        return "answer", label
    if progress == "review":
        return "review", label
    if task["status"] == "decision" or task.get("decision") in ("pending", "agree", "decline"):
        return "decision", label
    if progress == "working" or task["next_actor"] == "agent":
        return "running", label
    if task["status"] == "waiting":
        return "waiting", label
    return "todo", label


def _projects(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {p["id"]: {"id": p["id"], "title": p["title"], "icon": p.get("icon"), "status": p["status"]}
            for p in (dict(r) for r in conn.execute("SELECT id, title, icon, status FROM projects"))}


def _sources(conn: sqlite3.Connection, task_ids: list[str]) -> dict[str, str]:
    """The source of each task's first evidence (gmail, slack, github...)."""
    out: dict[str, str] = {}
    for i in range(0, len(task_ids), 500):
        chunk = task_ids[i:i + 500]
        for r in conn.execute(
                f"SELECT e.entity_id, s.source FROM evidence_links e JOIN source_items s ON s.id=e.source_item_id"
                f" WHERE e.entity_type='task' AND e.entity_id IN ({','.join('?' * len(chunk))})"
                " ORDER BY s.occurred_at", chunk):
            out.setdefault(r["entity_id"], r["source"])
    return out


def card(conn: sqlite3.Connection, task: dict[str, Any], projects: dict[str, dict[str, Any]],
         sources: dict[str, str], on: date) -> dict[str, Any]:
    kind, label = kind_of(conn, task)
    due = _day(task.get("due_date"))
    follow = _day(task.get("follow_up_date"))
    return {
        "id": task["id"], "title": task["title"], "kind": kind, "agent": label,
        "status": task["status"], "next_actor": task["next_actor"], "decision": task.get("decision"),
        "project": projects.get(task.get("project_id") or ""),
        "next_step": task.get("next_step"), "waiting_on": task.get("waiting_on"),
        "due": due, "overdue": bool(due and due < on.isoformat() and kind != "closed"),
        "follow_up": follow, "follow_up_due": bool(follow and follow <= on.isoformat()),
        "snooze_until": _day(task.get("snooze_until")), "snoozed": _snoozed(task, on),
        "focus": bool(task.get("focus")), "change_note": task.get("change_note"),
        "source": sources.get(task["id"]), "updated_at": task["updated_at"], "version": task["version"],
    }


def _open_cards(conn: sqlite3.Connection, cfg: Config) -> list[dict[str, Any]]:
    on = today(cfg)
    tasks = store.list_tasks(conn, store.OPEN_STATUSES)
    projects = _projects(conn)
    sources = _sources(conn, [t["id"] for t in tasks])
    return [card(conn, t, projects, sources, on) for t in tasks]


def _order(cards: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    far = "9999-12-31"
    if key == "waiting":
        return sorted(cards, key=lambda c: (not c["follow_up_due"], c["follow_up"] or far, c["title"]))
    return sorted(cards, key=lambda c: (not c["focus"], c["due"] or far, c["updated_at"]), reverse=False)


def home(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    cards = _open_cards(conn, cfg)
    groups: dict[str, list[dict[str, Any]]] = {g: [] for g in GROUPS}
    for c in cards:
        if c["snoozed"] and c["kind"] in ("todo", "waiting", "decision"):
            continue
        group = {"decision": "decide", "todo": "mine", "answer": "agent_wait", "review": "agent_wait",
                 "running": "agent_run", "waiting": "waiting"}.get(c["kind"])
        if group == "mine" and c["next_actor"] != "me":
            continue
        if group:
            groups[group].append(c)
    out = {g: _order(v, g) for g, v in groups.items()}
    week_ago = clock.iso(clock.now() - timedelta(days=7))
    done = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='done' AND closed_at >= ?", (week_ago,)).fetchone()[0]
    snoozed = sum(1 for c in cards if c["snoozed"])
    return {"groups": out, "counts": {g: len(v) for g, v in out.items()}, "done_week": done, "snoozed": snoozed,
            "open": len(cards), "projects": project_list(conn, cfg, cards=cards), "fresh": freshness(conn)}


def task_list(conn: sqlite3.Connection, cfg: Config, *, project: str | None = None,
              closed: bool = False) -> list[dict[str, Any]]:
    on = today(cfg)
    statuses = store.TASK_STATUSES if closed else store.OPEN_STATUSES
    tasks = store.list_tasks(conn, statuses, project_id=project)
    projects = _projects(conn)
    sources = _sources(conn, [t["id"] for t in tasks])
    cards = [card(conn, t, projects, sources, on) for t in tasks]
    return sorted(cards, key=lambda c: (c["kind"] == "closed", c["due"] or "9999-12-31", c["title"]))


def freshness(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT source, MAX(last_success_at) at, MAX(consecutive_failures) failures"
                        " FROM cursors GROUP BY source ORDER BY source").fetchall()
    return {"sources": [{"source": r["source"], "at": r["at"], "failures": r["failures"]} for r in rows]}


def project_list(conn: sqlite3.Connection, cfg: Config, *, cards: list[dict[str, Any]] | None = None,
                 ) -> list[dict[str, Any]]:
    cards = _open_cards(conn, cfg) if cards is None else cards
    counts: dict[str, dict[str, int]] = {}
    for c in cards:
        if c["project"]:
            per = counts.setdefault(c["project"]["id"], {"open": 0, "decision": 0, "agent": 0, "waiting": 0})
            per["open"] += 1
            if c["kind"] == "decision":
                per["decision"] += 1
            elif c["kind"] in ("answer", "review", "running"):
                per["agent"] += 1
            elif c["kind"] == "waiting":
                per["waiting"] += 1
    out = []
    for p in store.list_projects(conn):
        out.append({k: p.get(k) for k in ("id", "title", "icon", "status", "domain", "goal", "next_step",
                                          "blockers", "recent_activity", "updated_at")}
                   | {"counts": counts.get(p["id"], {"open": 0, "decision": 0, "agent": 0, "waiting": 0})})
    order = {"active": 0, "paused": 1}
    return sorted(out, key=lambda p: (order.get(p["status"], 2), -p["counts"]["open"], p["title"]))


def project_detail(conn: sqlite3.Connection, cfg: Config, project_id: str) -> dict[str, Any] | None:
    p = store.get_project(conn, project_id)
    if p is None:
        return None
    repos = [dict(r) for r in conn.execute("SELECT * FROM project_repos WHERE project_id=?", (project_id,))]
    return {**{k: p.get(k) for k in ("id", "title", "icon", "status", "domain", "goal", "next_step", "blockers",
                                     "recent_activity", "updated_at")},
            "repos": [r["repo"] for r in repos],
            "tasks": task_list(conn, cfg, project=project_id, closed=True)[:200]}


# --------------------------------------------------------------------------- task detail

def _delegation_view(conn: sqlite3.Connection, d: dict[str, Any]) -> dict[str, Any]:
    versions = []
    for v in delegation.deliveries(conn, d["id"]):
        versions.append({"id": v["id"], "kind": v["kind"], "seq": v["seq"], "summary": v["summary"],
                         "received_at": v["received_at"],
                         "files": [{"index": i, "name": Path(a.get("path") or a.get("source") or "").name,
                                    "size": a.get("size"), "available": bool(a.get("path"))}
                                   for i, a in enumerate(v["artifacts"]) if isinstance(a, dict)]})
    question = d["block_reason"] if d["status"] == "blocked" else None
    return {"id": d["id"], "title": d["title"], "status": d["status"], "goal": d["goal"],
            "done_criteria": d["done_criteria"], "deliverable": d["deliverable"],
            "allowed": d["allowed_actions"], "start_after": d.get("start_after"),
            "pause_requested": bool(d.get("pause_requested")), "question": question,
            "created_at": d["created_at"], "versions": versions}


def _handover_question(conn: sqlite3.Connection, task_id: str) -> str | None:
    """The question the feedback run asked instead of starting agent work."""
    row = conn.execute("SELECT result FROM feedback WHERE scope_type='task' AND scope_id=? AND kind='delegate_request'"
                       " AND status='clarify' ORDER BY applied_at DESC LIMIT 1", (task_id,)).fetchone()
    return (loads(row["result"], {}) or {}).get("reply") if row else None


def thread(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    """The task's conversation: what the user said (any surface) with the replies, the program's
    own comments, and what the agent asked or delivered."""
    entries: list[dict[str, Any]] = []
    dids = [d["id"] for d in delegation.for_task(conn, task_id)]
    scopes = [("task", task_id)] + [("delegation", d) for d in dids]
    for scope_type, scope_id in scopes:
        for r in conn.execute("SELECT * FROM feedback WHERE scope_type=? AND scope_id=? AND status!='superseded'"
                              " AND kind NOT IN ('property_edit') ORDER BY received_at", (scope_type, scope_id)):
            result = loads(r["result"], None)
            result = result if isinstance(result, dict) else {}
            payload = loads(r["payload"], {}) or {}
            text, choice = r["text"], None
            if r["kind"] in DERIVED_KINDS:
                text = None
            elif r["kind"] in CHOICE_KINDS:
                # The first line is the program's wording of the choice; the rest is the user's note.
                choice = payload.get("decision") or "agent"
                text = (text or "").partition("\n")[2]
            if text or choice:
                entries.append({"at": r["received_at"], "who": "me" if r["author_is_user"] else "other",
                                "origin": r["origin"], "kind": r["kind"], "text": text, "choice": choice,
                                "status": r["status"], "id": r["id"], "to": payload.get("to")})
            if result.get("reply"):
                entries.append({"at": r["applied_at"] or r["received_at"], "who": "hermes", "kind": "reply",
                                "text": result["reply"], "reply_to": r["id"]})
    for r in conn.execute("SELECT payload, created_at FROM outbox WHERE target='notion' AND op='comment'"
                          " AND entity_type='task' AND entity_id=? ORDER BY created_at", (task_id,)):
        p = loads(r["payload"], {})
        if str(p.get("key") or "").startswith(("reply:", "dlv:")):
            continue   # shown with the feedback it answers / as the agent's delivery
        entries.append({"at": r["created_at"], "who": "hermes", "kind": "note", "text": p.get("text") or ""})
    for did in dids:
        for v in delegation.deliveries(conn, did):
            entries.append({"at": v["received_at"], "who": "agent", "kind": v["kind"], "text": v["summary"] or "",
                            "delivery": v["id"], "seq": v["seq"]})
    entries.sort(key=lambda e: e["at"] or "")
    return entries[-THREAD_LIMIT:]


def task_detail(conn: sqlite3.Connection, cfg: Config, task_id: str) -> dict[str, Any] | None:
    task = store.get_task(conn, task_id)
    if task is None:
        return None
    on = today(cfg)
    base = card(conn, task, _projects(conn), _sources(conn, [task_id]), on)
    works = delegation.for_task(conn, task_id)
    current = works[-1] if works else None
    notes = conn.execute("SELECT notes_text FROM notion_pages WHERE entity_type='task' AND entity_id=?",
                         (task_id,)).fetchone()
    evidence = [{"id": e["id"], "source": e["source"], "role": e["role"], "title": e["title"] or e["excerpt"],
                 "author": e["author"], "url": e["url"], "at": e["occurred_at"]}
                for e in store.evidence_for(conn, "task", task_id)]
    pending = conn.execute("SELECT COUNT(*) FROM feedback WHERE scope_type='task' AND scope_id=? AND status='pending'",
                           (task_id,)).fetchone()[0]
    return {**base,
            **{k: task.get(k) for k in ("summary", "responsibility", "done_criteria", "due_basis", "focus_reason",
                                        "closed_reason", "closed_at", "created_at", "origin")},
            "agent_work": _delegation_view(conn, current) if current else None,
            "agent_question": (_delegation_view(conn, current)["question"] if current else None)
            or (_handover_question(conn, task_id) if base["agent"] == "blocked" else None),
            "sources": evidence, "thread": thread(conn, task_id), "pending_feedback": pending,
            "notes": notes["notes_text"] if notes else None,
            "notion_url": delegation._notion_url(conn, task),
            "links": [{"task": l["other_task_id"], "kind": l["kind"],
                       "title": (store.get_task(conn, l["other_task_id"]) or {}).get("title")}
                      for l in store.task_links(conn, task_id)],
            "history": store.history(conn, "task", task_id)[-HISTORY_LIMIT:]}


# --------------------------------------------------------------------------- deliveries

def delivery(conn: sqlite3.Connection, delivery_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
    if row is None:
        return None
    d = delegation.get(conn, row["delegation_id"])
    task = store.get_task(conn, d["task_id"])
    view = _delegation_view(conn, d)
    current = next(v for v in view["versions"] if v["id"] == delivery_id)
    return {"task": {"id": task["id"], "title": task["title"]}, "delegation": view, "version": current,
            "body": row["body"], "latest": view["versions"][-1]["id"] == delivery_id}


def delivery_file(conn: sqlite3.Connection, cfg: Config, delivery_id: str, index: int) -> Path | None:
    """The kept copy of a delivered file (``files.keep_artifacts``), inside the workspace."""
    row = conn.execute("SELECT artifacts FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
    if row is None:
        return None
    artifacts = loads(row["artifacts"], [])
    if not (0 <= index < len(artifacts)) or not isinstance(artifacts[index], dict) or not artifacts[index].get("path"):
        return None
    root = (cfg.home / "deliveries").resolve()
    path = (cfg.home / artifacts[index]["path"]).resolve()
    return path if path.is_file() and path.is_relative_to(root) else None


# --------------------------------------------------------------------------- search

def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def search(conn: sqlite3.Connection, cfg: Config, query: str) -> dict[str, Any]:
    terms = [t for t in query.split() if t][:6]
    if not terms:
        return {"tasks": [], "projects": [], "sources": []}
    on = today(cfg)
    projects = _projects(conn)

    def where(columns: list[str]) -> tuple[str, list[str]]:
        parts, params = [], []
        for term in terms:
            parts.append("(" + " OR ".join(f"COALESCE({c},'') LIKE ? ESCAPE '\\'" for c in columns) + ")")
            params += [_like(term)] * len(columns)
        return " AND ".join(parts), params

    sql, params = where(["title", "next_step", "summary", "waiting_on"])
    tasks = [dict(r) for r in conn.execute(
        f"SELECT * FROM tasks WHERE {sql} ORDER BY status IN ('done','cancelled'), updated_at DESC LIMIT ?",
        (*params, SEARCH_LIMIT))]
    sources = _sources(conn, [t["id"] for t in tasks])
    sql, params = where(["title", "goal", "next_step"])
    found_projects = [projects[r["id"]] for r in conn.execute(
        f"SELECT id FROM projects WHERE {sql} ORDER BY updated_at DESC LIMIT ?", (*params, SEARCH_LIMIT))]
    sql, params = where(["s.title", "s.excerpt", "s.author"])
    items = []
    for r in conn.execute(
            f"SELECT s.id, s.source, s.title, s.excerpt, s.author, s.occurred_at, s.url,"
            f" (SELECT e.entity_id FROM evidence_links e WHERE e.source_item_id=s.id AND e.entity_type='task' LIMIT 1)"
            f" AS task_id FROM source_items s WHERE {sql} ORDER BY s.occurred_at DESC LIMIT ?",
            (*params, SEARCH_LIMIT)):
        items.append({"id": r["id"], "source": r["source"], "title": r["title"] or r["excerpt"], "author": r["author"],
                      "at": r["occurred_at"], "url": r["url"], "task": r["task_id"]})
    return {"tasks": [card(conn, t, projects, sources, on) for t in tasks], "projects": found_projects,
            "sources": items}


def stamp(conn: sqlite3.Connection) -> str:
    """Changes whenever something the UI shows may have changed (polled for live refresh)."""
    row = conn.execute(
        "SELECT (SELECT MAX(updated_at) FROM tasks), (SELECT MAX(updated_at) FROM projects),"
        " (SELECT MAX(updated_at) FROM delegations), (SELECT MAX(received_at) FROM deliveries),"
        " (SELECT MAX(COALESCE(applied_at, received_at)) FROM feedback), (SELECT COUNT(*) FROM feedback),"
        " (SELECT MAX(created_at) FROM outbox WHERE op='comment'), (SELECT MAX(last_success_at) FROM cursors)"
    ).fetchone()
    return "|".join(str(v or "") for v in row)


def me(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    return {"language": cfg.language, "today": today(cfg).isoformat(), "timezone": cfg.tz,
            "notion": bool(get_meta(conn, "notion.home_page_id"))}
