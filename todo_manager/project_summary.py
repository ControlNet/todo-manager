"""A project's "Next step", "Blockers" and "Recent progress", derived from its tasks.

Written by Todo Manager on every push of the project page (docs/notion-schema.md); nobody types
them. The commit digest still records its text in ``projects.recent_activity`` (stamped
``recent_activity_at``) and shows up here as one dated line while it is recent.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

from . import clock, delegation, i18n, store

RECENT_DAYS = 14
MAX_LINES = 5
MAX_STEP_CHARS = 60


def summarize(conn: sqlite3.Connection, project: dict[str, Any], tz: str, lang: str | None) -> dict[str, Any]:
    """``next_step``, ``blockers``, ``recent_activity`` (text or None) and ``recent_activity_at`` (a
    local date: the latest task done, agent result or commit digest of the project, however old)."""
    t = lambda key, **v: i18n.t(key, lang, **v)   # noqa: E731
    today = clock.local_date(tz).isoformat()
    day = lambda iso: clock.local_dt(tz, clock.parse(iso)).strftime("%m-%d")   # noqa: E731
    tasks = store.list_tasks(conn, store.OPEN_STATUSES, project_id=project["id"])
    return {"next_step": _next_step(tasks, today, t),
            "blockers": _blockers(conn, tasks, today, t),
            **_recent(conn, project, tz, day, t)}


def _next_step(tasks: list[dict[str, Any]], today: str, t: Any) -> str:
    snoozed = [x for x in tasks if (x.get("snooze_until") or "") > today]
    mine = [x for x in tasks if x["next_actor"] == "me" and x not in snoozed]
    if mine:
        first = min(mine, key=lambda x: (x.get("due_date") or "9999", not x.get("focus"),
                                         x.get("follow_up_date") or "9999", x["created_at"]))
        step = first.get("next_step") or ""
        if len(step) > MAX_STEP_CHARS:
            step = step[:MAX_STEP_CHARS].rstrip() + "…"
        text = first["title"] + (t("project.step_detail", step=step) if step else "")
        if first.get("due_date"):
            key = "project.due_passed" if first["due_date"] < today else "project.due"
            text += t(key, date=first["due_date"][5:10])
        return text + (t("project.more_mine", n=len(mine) - 1) if len(mine) > 1 else "")
    others = [t(key, n=n) for key, n in (
        ("project.agent_count", sum(x["next_actor"] == "agent" and x not in snoozed for x in tasks)),
        ("project.waiting_count", sum(x["next_actor"] == "external" and x not in snoozed for x in tasks)),
        ("project.snoozed_count", len(snoozed))) if n]
    return t("project.sep").join(others) or t("project.no_open")


def _blockers(conn: sqlite3.Connection, tasks: list[dict[str, Any]], today: str, t: Any) -> str | None:
    answers, decisions = [], []
    waiting: dict[Any, list[dict[str, Any]]] = {}    # by the people waited on, earliest follow-up first
    for x in sorted(tasks, key=lambda x: (x.get("follow_up_date") or "9999", x["created_at"])):
        if delegation.task_progress(conn, x["id"]) == "answer":
            answers.append(t("project.blocker_answer", title=x["title"]))
        elif x["status"] == "decision":
            decisions.append(t("project.blocker_decision", title=x["title"]))
        elif x["status"] == "waiting":
            waiting.setdefault(x.get("waiting_on") or x["id"], []).append(x)
    lines = []
    for group in waiting.values():
        first, many = group[0], len(group) > 1
        if many:
            line = t("project.blocker_waiting_many", who=first["waiting_on"], n=len(group))
        elif first.get("waiting_on"):
            line = t("project.blocker_waiting", who=first["waiting_on"], title=first["title"])
        else:
            line = t("project.blocker_waiting_anyone", title=first["title"])
        if first.get("follow_up_date"):
            key = "project.follow_up" + ("_first" if many else "") + ("_overdue" if first["follow_up_date"] < today else "")
            line += t(key, date=first["follow_up_date"][5:10])
        lines.append(line)
    return _lines(answers + decisions + lines, t)


def _recent(conn: sqlite3.Connection, project: dict[str, Any], tz: str, day: Any, t: Any) -> dict[str, Any]:
    latest_by_task: dict[str, tuple[str, str]] = {}     # one line per task: its latest event
    for r in conn.execute(
            "SELECT id, title, closed_at AS at, 'done' AS kind FROM tasks WHERE project_id=? AND status='done'"
            " AND closed_at IS NOT NULL UNION ALL SELECT t.id, t.title, MAX(v.received_at), 'delivered'"
            " FROM deliveries v JOIN delegations d ON d.id=v.delegation_id JOIN tasks t ON t.id=d.task_id"
            " WHERE t.project_id=? AND v.kind='result' GROUP BY t.id", (project["id"], project["id"])):
        if r["id"] not in latest_by_task or r["at"] > latest_by_task[r["id"]][0]:
            latest_by_task[r["id"]] = (r["at"], t(f"project.recent_{r['kind']}", title=r["title"]))
    events = list(latest_by_task.values())
    if project.get("recent_activity") and project.get("recent_activity_at"):
        events.append((project["recent_activity_at"], t("project.recent_commits", text=project["recent_activity"])))
    events.sort(key=lambda e: e[0], reverse=True)
    since = clock.iso(clock.now() - timedelta(days=RECENT_DAYS))
    latest = clock.local_date(tz, clock.parse(events[0][0])).isoformat() if events else None
    return {"recent_activity": _lines([f"{day(at)} {text}" for at, text in events if at >= since], t),
            "recent_activity_at": latest}


def _lines(lines: list[str], t: Any) -> str | None:
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES - 1] + [t("project.more", n=len(lines) - MAX_LINES + 1)]
    return "\n".join(lines) or None
