"""Migration proposal from the previous Notion Todo Manager (read-only on the old pages).

Nothing is copied wholesale and nothing old is deleted. The flow:

1. ``fetch_old``  - read-only query of the old Tasks/Projects data sources into
   ``TODO_HOME/migration/<stamp>/old/``.
2. ``propose``    - classify every old page and write ``summary.md``, ``tasks.csv`` and
   ``projects.csv``. The ``action`` column is the only thing the user edits.
3. ``apply``      - after review: import ``keep`` rows as open tasks and ``history`` rows
   (closed by the user) as closed tasks that are never shown in Notion but stop old
   reminders from re-creating them. ``drop`` rows are only recorded as decisions.

Old-system judgements (priority, confidence, bot notes) are kept as context, not trusted
as facts. Re-run ``apply`` safely: pages already imported are skipped.

``LEGACY`` describes the old schema (property names and status values, which are data of that
system, in its language). Adapt it to migrate from a differently named legacy database.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from . import clock, i18n, store
from .config import Config
from .db import dumps, tx

# The previous Notion Todo Manager's schema (data values, not UI text).
LEGACY = {
    "task": {"title": "任务名称", "status": "状态", "priority": "优先级", "confidence": "置信度", "due": "截止日期",
             "project": "项目", "source_type": "来源类型", "source_id": "来源ID", "note": "备注", "evidence": "证据",
             "fingerprint": "指纹"},
    "project": {"title": "项目名称", "status": "状态", "domain": "领域", "tasks": "任务", "note": "备注"},
    "high_confidence": ("高", "high"),
}
ACTIVE = {"待办": "todo", "进行中": "in_progress", "等待中": "waiting"}
CLOSED = {"已完成": "done", "已放弃": "cancelled"}
INBOX = "收件箱"
ACTIONS = ("keep", "history", "drop", "review")
TASK_COLUMNS = ("old_page_id", "category", "action", "title", "old_status", "edited_by_user", "due", "priority",
                "confidence", "source_type", "old_project", "created", "last_edited", "old_url", "reason")


def _prop(page: dict[str, Any], name: str) -> Any:
    v = (page.get("properties") or {}).get(name)
    if not v:
        return None
    kind = v.get("type")
    raw = v.get(kind)
    if kind in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in raw or []).strip() or None
    if kind in ("select", "status"):
        return (raw or {}).get("name")
    if kind == "date":
        return ((raw or {}).get("start") or "")[:10] or None
    if kind == "relation":
        return [r["id"] for r in raw or []]
    if kind in ("checkbox", "url"):
        return raw
    return None


def _task(page: dict[str, Any], field: str) -> Any:
    return _prop(page, LEGACY["task"][field])


def _project(page: dict[str, Any], field: str) -> Any:
    return _prop(page, LEGACY["project"][field])


# --------------------------------------------------------------------------- fetch

def fetch_old(client: Any, cfg: Config, tasks_data_source: str, projects_data_source: str) -> Path:
    root = cfg.home / "migration" / clock.now().strftime("%Y%m%dT%H%M%SZ")
    old = root / "old"
    old.mkdir(parents=True, exist_ok=True)
    meta = {"fetched_at": clock.iso(), "bot": client.me().get("id"),
            "tasks_data_source": tasks_data_source, "projects_data_source": projects_data_source}
    for name, ds in (("tasks", tasks_data_source), ("projects", projects_data_source)):
        pages = client.query_data_source(ds)
        (old / f"{name}.json").write_text(json.dumps({"results": pages}, ensure_ascii=False), encoding="utf-8")
        meta[name] = len(pages)
    (old / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return root


# --------------------------------------------------------------------------- classify

def classify(tasks: list[dict[str, Any]], bot_ids: set[str], today: str) -> list[dict[str, Any]]:
    rows = []
    for page in tasks:
        status = _task(page, "status")
        by_user = (page.get("last_edited_by") or {}).get("id") not in bot_ids
        priority, confidence = _task(page, "priority"), _task(page, "confidence")
        due = _task(page, "due")
        if page.get("in_trash"):
            category, action = "trashed", "drop"
        elif status in ACTIVE:
            category, action = "active", "keep"
        elif status in CLOSED and by_user:
            category, action = "closed_by_user", "history"
        elif status in CLOSED:
            category, action = "closed_by_system", "drop"
        elif status == INBOX and (priority == "P1" or confidence in LEGACY["high_confidence"] or (due and due >= today)):
            category, action = "inbox_candidate", "review"
        else:
            category, action = "inbox_noise", "drop"
        reason = i18n.t(f"mig.reason.{category}", status=status or "")
        rows.append({
            "old_page_id": page["id"], "category": category, "action": action, "title": _task(page, "title") or "",
            "old_status": status or "", "edited_by_user": "yes" if by_user else "no", "due": due or "",
            "priority": priority or "", "confidence": confidence or "", "source_type": _task(page, "source_type") or "",
            "old_project": ",".join(_task(page, "project") or []), "created": page.get("created_time", "")[:10],
            "last_edited": page.get("last_edited_time", "")[:10], "old_url": page.get("url") or "", "reason": reason})
    return rows


def propose(root: Path, cfg: Config, extra_bot_ids: set[str] | None = None) -> dict[str, Any]:
    old = root / "old"
    tasks = json.loads((old / "tasks.json").read_text(encoding="utf-8"))["results"]
    projects = json.loads((old / "projects.json").read_text(encoding="utf-8"))["results"]
    meta = json.loads((old / "meta.json").read_text(encoding="utf-8")) if (old / "meta.json").exists() else {}
    bot_ids = set(extra_bot_ids or ()) | ({meta["bot"]} if meta.get("bot") else set())
    if not bot_ids:
        raise ValueError("the old integration's bot user id is needed to tell your edits from the automation's")
    rows = classify(tasks, bot_ids, clock.local_date(cfg.tz).isoformat())
    kept_projects = {p for r in rows if r["action"] in ("keep", "review") for p in r["old_project"].split(",") if p}
    project_rows = [{"old_page_id": p["id"], "action": "keep" if p["id"] in kept_projects else "drop",
                     "title": _project(p, "title") or "", "old_status": _project(p, "status") or "",
                     "domain": _project(p, "domain") or "", "tasks": len(_project(p, "tasks") or []),
                     "old_url": p.get("url") or ""} for p in projects]
    with (root / "tasks.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, TASK_COLUMNS)
        w.writeheader()
        order = {"active": 0, "inbox_candidate": 1, "closed_by_user": 2, "closed_by_system": 3, "inbox_noise": 4,
                 "trashed": 5}
        w.writerows(sorted(rows, key=lambda r: (order[r["category"]], r["created"])))
    with (root / "projects.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, ("old_page_id", "action", "title", "old_status", "domain", "tasks", "old_url"))
        w.writeheader()
        w.writerows(project_rows)
    counts = Counter(r["category"] for r in rows)
    summary = {"old_tasks": len(rows), "old_projects": len(projects), "by_category": dict(counts),
               "by_action": dict(Counter(r["action"] for r in rows)),
               "projects_kept": sum(1 for p in project_rows if p["action"] == "keep"),
               "fetched_at": meta.get("fetched_at")}
    (root / "summary.md").write_text(_summary_md(summary), encoding="utf-8")
    return summary


def _summary_md(s: dict[str, Any]) -> str:
    c, a = s["by_category"], s["by_action"]
    counts = {k: c.get(k, 0) for k in ("active", "inbox_candidate", "closed_by_user", "closed_by_system",
                                         "inbox_noise", "trashed")}
    return i18n.t("mig.summary", fetched_at=s.get("fetched_at") or "?", tasks=s["old_tasks"],
                  projects=s["old_projects"], actions=", ".join(f"{k} {v}" for k, v in sorted(a.items())),
                  projects_kept=s["projects_kept"], **counts)


# --------------------------------------------------------------------------- apply

def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def apply(conn: sqlite3.Connection, cfg: Config, root: Path, *, dry_run: bool = True,
          resolve_thread: Callable[[str], str | None] | None = None) -> dict[str, Any]:
    """Import the reviewed proposal. ``resolve_thread(old_source_id)`` may map an old Gmail id to
    the new system's thread key so later mail in that thread links to the migrated task."""
    rows = _read_csv(root / "tasks.csv")
    project_rows = _read_csv(root / "projects.csv")
    bad = [r["old_page_id"] for r in rows if r["action"] not in ACTIONS]
    pending = [r["old_page_id"] for r in rows if r["action"] == "review"]
    if bad or pending:
        raise ValueError(f"{len(pending)} rows still marked review and {len(bad)} rows with an unknown action;"
                         " set every action to keep, history or drop")
    old_tasks = {p["id"]: p for p in json.loads((root / "old" / "tasks.json").read_text(encoding="utf-8"))["results"]}
    old_projects = {p["id"]: p for p in json.loads((root / "old" / "projects.json").read_text(encoding="utf-8"))["results"]}
    report: dict[str, Any] = {"projects": 0, "keep": 0, "history": 0, "drop": 0, "skipped_existing": 0,
                              "threads_linked": 0, "dry_run": dry_run}
    with tx(conn):
        project_map: dict[str, str] = {}
        for pr in project_rows:
            if pr["action"] != "keep":
                continue
            existing = conn.execute("SELECT id FROM projects WHERE origin='migration' AND notion_page_id IS NULL"
                                    " AND title=?", (pr["title"],)).fetchone()
            if existing:
                project_map[pr["old_page_id"]] = existing["id"]
                continue
            page = old_projects.get(pr["old_page_id"], {})
            project_map[pr["old_page_id"]] = store.create_project(
                conn, pr["title"], origin="migration", actor="program", cause_ref=f"old:{pr['old_page_id']}",
                domain=pr["domain"] or None, goal=_project(page, "note"))
            report["projects"] += 1
        for r in rows:
            action = r["action"]
            if action == "drop":
                report["drop"] += 1
                continue
            if conn.execute("SELECT 1 FROM source_items WHERE source='migration' AND external_id=?",
                            (r["old_page_id"],)).fetchone():
                report["skipped_existing"] += 1
                continue
            page = old_tasks[r["old_page_id"]]
            old_source = _task(page, "source_id")
            thread_key = resolve_thread(old_source) if (resolve_thread and old_source) else None
            report["threads_linked"] += 1 if thread_key else 0
            note = _task(page, "note")
            evidence = _task(page, "evidence")
            summary = i18n.t("mig.old_note", note=note) if note else None
            project = next((project_map[p] for p in r["old_project"].split(",") if p in project_map), None)
            if action == "keep":
                status = ACTIVE.get(r["old_status"], "todo")
                user_status = r["edited_by_user"] == "yes"
                tid = store.create_task(
                    conn, r["title"], origin="migration", actor="human" if user_status else "program",
                    cause_ref=f"old:{r['old_page_id']}", project_id=project, status=status,
                    next_actor="external" if status == "waiting" else "me", due_date=r["due"] or None,
                    due_basis=i18n.t("mig.old_due") if r["due"] else None,
                    summary=summary, change_note=i18n.t("mig.change_note"))
            else:
                tid = store.create_task(
                    conn, r["title"], origin="migration", actor="human", cause_ref=f"old:{r['old_page_id']}",
                    project_id=project, status=CLOSED.get(r["old_status"], "done"), summary=summary)
                closed_at = page.get("last_edited_time") or clock.iso()
                conn.execute("UPDATE tasks SET closed_at=?, closed_reason=? WHERE id=?",
                             (closed_at, i18n.t("mig.closed_reason", status=r["old_status"]), tid))
            item_id = f"src_mig_{r['old_page_id'].replace('-', '')}"
            conn.execute(
                "INSERT INTO source_items(id, source, account, external_id, kind, thread_key, conversation_key,"
                " occurred_at, title, url, excerpt, meta, fetched_at, state) VALUES"
                " (?, 'migration', 'old-notion', ?, 'migrated_task', ?, 'migration:old-notion', ?, ?, ?, ?, ?, ?,"
                " 'processed')",
                (item_id, r["old_page_id"], thread_key, page.get("created_time"), r["title"], r["old_url"] or None,
                 (evidence or "")[:1000], dumps({"old_source_id": old_source, "old_source_type": r["source_type"],
                                                 "priority": r["priority"], "confidence": r["confidence"],
                                                 "fingerprint": _task(page, "fingerprint")}), clock.iso()))
            conn.execute("INSERT INTO source_bodies(item_id, body) VALUES (?,?)",
                         (item_id, "\n\n".join(x for x in (evidence, note) if x)))
            store.link_evidence(conn, "task", tid, item_id, "origin", "migrated from the old Todo Manager")
            report[action] += 1
        if dry_run:
            raise _DryRun(report)
    return report


class _DryRun(Exception):
    def __init__(self, report: dict[str, Any]):
        super().__init__("dry run")
        self.report = report


def preview(conn: sqlite3.Connection, cfg: Config, root: Path, **kw: Any) -> dict[str, Any]:
    """Run ``apply`` inside a transaction that is rolled back; returns what would happen."""
    try:
        apply(conn, cfg, root, dry_run=True, **kw)
    except _DryRun as done:
        return done.report
    raise AssertionError("dry run did not roll back")
