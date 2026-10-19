"""A project's "Next step", "Blockers" and "Recent progress", derived from its tasks.

Written by Todo Manager on every push of the project page (docs/notion-schema.md); nobody types
them. Each is one short line for the project table: counts and the one task that matters; the
tasks themselves are listed on the project page. The commit digest still records its text in
``projects.recent_activity`` (stamped ``recent_activity_at``) and counts as recent progress
while it is recent.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

from . import clock, delegation, i18n, store

RECENT_DAYS = 14
MAX_TITLE_CHARS = 28


def summarize(conn: sqlite3.Connection, project: dict[str, Any], tz: str, lang: str | None) -> dict[str, Any]:
    """``next_step``, ``blockers``, ``recent_activity`` (text or None) and ``recent_activity_at`` (a
    local date: the latest task done, agent result or commit digest of the project, however old)."""
    t = lambda key, **v: i18n.t(key, lang, **v)   # noqa: E731
    today = clock.local_date(tz).isoformat()
    tasks = store.list_tasks(conn, store.OPEN_STATUSES, project_id=project["id"])
    return {"next_step": _next_step(tasks, today, t),
            "blockers": _blockers(conn, tasks, today, t),
            **_recent(conn, project, tz, t)}


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TITLE_CHARS else text[:MAX_TITLE_CHARS].rstrip() + "…"


def _day(date: str) -> str:
    """'2026-10-09' -> '10/9'."""
    return f"{int(date[5:7])}/{int(date[8:10])}"


def _next_step(tasks: list[dict[str, Any]], today: str, t: Any) -> str:
    snoozed = [x for x in tasks if (x.get("snooze_until") or "") > today]
    mine = [x for x in tasks if x["next_actor"] == "me" and x not in snoozed]
    if not tasks:
        return t("project.no_open")
    if mine:
        first = min(mine, key=lambda x: (x.get("due_date") or "9999", not x.get("focus"),
                                         x.get("follow_up_date") or "9999", x["created_at"]))
        parts = [_clip(first["title"])]
        if first.get("due_date"):
            key = "project.due_passed" if first["due_date"] < today else "project.due"
            parts.append(t(key, date=_day(first["due_date"])))
        if len(mine) > 1:
            parts.append(t("project.more_mine", n=len(mine) - 1))
    else:
        agent = sum(x["next_actor"] == "agent" and x not in snoozed for x in tasks)
        parts = [t("project.none_mine")] + ([t("project.agent_count", n=agent)] if agent else [])
        parts += [t("project.snoozed_count", n=len(snoozed))] if snoozed else []
    return t("project.sep").join(parts)


def _blockers(conn: sqlite3.Connection, tasks: list[dict[str, Any]], today: str, t: Any) -> str | None:
    answers = sum(delegation.task_progress(conn, x["id"]) == "answer" for x in tasks)
    decisions = sum(x["status"] == "decision" for x in tasks)
    waiting = [x for x in tasks if x["status"] == "waiting"]
    overdue = sum(bool(x.get("follow_up_date")) and x["follow_up_date"] < today for x in waiting)
    parts = ([t("project.blocker_answer", n=answers)] if answers else []) + \
            ([t("project.blocker_decision", n=decisions)] if decisions else [])
    if waiting:
        parts.append(t("project.blocker_waiting", n=len(waiting))
                     + (t("project.follow_up_overdue", n=overdue) if overdue else ""))
    return t("project.sep").join(parts) or None


def _recent(conn: sqlite3.Connection, project: dict[str, Any], tz: str, t: Any) -> dict[str, Any]:
    latest_by_task: dict[str, tuple[str, str, str]] = {}     # task -> (at, kind, title): its latest event
    for r in conn.execute(
            "SELECT id, title, closed_at AS at, 'done' AS kind FROM tasks WHERE project_id=? AND status='done'"
            " AND closed_at IS NOT NULL UNION ALL SELECT t.id, t.title, MAX(v.received_at), 'delivered'"
            " FROM deliveries v JOIN delegations d ON d.id=v.delegation_id JOIN tasks t ON t.id=d.task_id"
            " WHERE t.project_id=? AND v.kind='result' GROUP BY t.id", (project["id"], project["id"])):
        if r["id"] not in latest_by_task or r["at"] > latest_by_task[r["id"]][0]:
            latest_by_task[r["id"]] = (r["at"], r["kind"], r["title"])
    events = list(latest_by_task.values())
    if project.get("recent_activity") and project.get("recent_activity_at"):
        events.append((project["recent_activity_at"], "commits", project["recent_activity"]))
    events.sort(key=lambda e: e[0], reverse=True)
    local = lambda at: clock.local_date(tz, clock.parse(at)).isoformat()   # noqa: E731
    since = clock.iso(clock.now() - timedelta(days=RECENT_DAYS))
    recent = [e for e in events if e[0] >= since]
    text = None
    if recent:
        counts = [t(f"project.recent_{kind}", n=n) for kind in ("done", "delivered", "commits")
                  if (n := sum(e[1] == kind for e in recent))]
        at, _, title = recent[0]
        text = (t("project.recent_window", days=RECENT_DAYS, what=t("project.recent_joiner").join(counts))
                + t("project.latest", title=_clip(title), date=_day(local(at))))
    return {"recent_activity": text, "recent_activity_at": local(events[0][0]) if events else None}
