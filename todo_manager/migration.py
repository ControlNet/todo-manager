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
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from . import clock, store
from .config import Config
from .db import dumps, tx

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
        status = _prop(page, "状态")
        by_user = (page.get("last_edited_by") or {}).get("id") not in bot_ids
        priority, confidence = _prop(page, "优先级"), _prop(page, "置信度")
        due = _prop(page, "截止日期")
        if page.get("in_trash"):
            category, action, reason = "trashed", "drop", "page is in the old trash"
        elif status in ACTIVE:
            category, action, reason = "active", "keep", f"old status {status}"
        elif status in CLOSED and by_user:
            category, action, reason = "closed_by_user", "history", "you closed it; keep so reminders cannot revive it"
        elif status in CLOSED:
            category, action, reason = "closed_by_system", "drop", "closed by the old automation"
        elif status == INBOX and (priority == "P1" or confidence in ("高", "high") or (due and due >= today)):
            category, action, reason = "inbox_candidate", "review", "old inbox, marked important or due; decide keep/drop"
        else:
            category, action, reason = "inbox_noise", "drop", "old unreviewed inbox item"
        rows.append({
            "old_page_id": page["id"], "category": category, "action": action, "title": _prop(page, "任务名称") or "",
            "old_status": status or "", "edited_by_user": "yes" if by_user else "no", "due": due or "",
            "priority": priority or "", "confidence": confidence or "", "source_type": _prop(page, "来源类型") or "",
            "old_project": ",".join(_prop(page, "项目") or []), "created": page.get("created_time", "")[:10],
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
                     "title": _prop(p, "项目名称") or "", "old_status": _prop(p, "状态") or "",
                     "domain": _prop(p, "领域") or "", "tasks": len(_prop(p, "任务") or []),
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
    return f"""# 旧 Todo Manager 迁移建议

数据快照：{s.get('fetched_at') or '未知'}（只读查询；旧页面与旧数据未做任何修改）。
旧任务 {s['old_tasks']} 条，旧项目 {s['old_projects']} 个。

| 类别 | 数量 | 默认处理 |
|---|---|---|
| 仍在进行（待办／进行中／等待中） | {c.get('active', 0)} | keep：导入为新任务（保留旧备注作背景） |
| 收件箱中标为 P1、高置信度或有未来截止日期 | {c.get('inbox_candidate', 0)} | review：需要你逐条改成 keep 或 drop |
| 你手动完成／放弃的 | {c.get('closed_by_user', 0)} | history：作为已关闭记录导入，不显示在 Notion，用来阻止旧 reminder 再建任务 |
| 旧自动化自行关闭的 | {c.get('closed_by_system', 0)} | drop |
| 其余收件箱（未复核的旧噪声） | {c.get('inbox_noise', 0)} | drop：留在旧页面，不导入 |
| 已在旧回收站 | {c.get('trashed', 0)} | drop |

处理合计：{', '.join(f'{k} {v}' for k, v in sorted(a.items()))}；保留项目 {s['projects_kept']} 个（被 keep/review 任务引用的）。

怎么改：编辑同目录的 `tasks.csv` / `projects.csv` 的 `action` 列（keep / history / drop），
`review` 必须改掉才能导入。然后运行 `todo-manager migrate apply <本目录>`（先不加 --yes 预览）。
导入只写本地数据库；新 Notion 页面在下一次同步时才出现，且只显示 keep 的任务。
"""


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
                domain=pr["domain"] or None, goal=_prop(page, "备注"))
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
            old_source = _prop(page, "来源ID")
            thread_key = resolve_thread(old_source) if (resolve_thread and old_source) else None
            report["threads_linked"] += 1 if thread_key else 0
            note = _prop(page, "备注")
            evidence = _prop(page, "证据")
            project = next((project_map[p] for p in r["old_project"].split(",") if p in project_map), None)
            if action == "keep":
                status = ACTIVE.get(r["old_status"], "todo")
                user_status = r["edited_by_user"] == "yes"
                tid = store.create_task(
                    conn, r["title"], origin="migration", actor="human" if user_status else "program",
                    cause_ref=f"old:{r['old_page_id']}", project_id=project, status=status,
                    next_actor="external" if status == "waiting" else "me", due_date=r["due"] or None,
                    due_basis="旧系统记录的截止日期（迁移时未重新核实）" if r["due"] else None,
                    summary=("旧系统备注：" + note) if note else None,
                    change_note="从旧 Todo Manager 迁移")
            else:
                tid = store.create_task(
                    conn, r["title"], origin="migration", actor="human", cause_ref=f"old:{r['old_page_id']}",
                    project_id=project, status=CLOSED.get(r["old_status"], "done"),
                    summary=("旧系统备注：" + note) if note else None)
                closed_at = page.get("last_edited_time") or clock.iso()
                conn.execute("UPDATE tasks SET closed_at=?, closed_reason=? WHERE id=?",
                             (closed_at, f"旧系统中由你标为{r['old_status']}", tid))
            item_id = f"src_mig_{r['old_page_id'].replace('-', '')}"
            conn.execute(
                "INSERT INTO source_items(id, source, account, external_id, kind, thread_key, conversation_key,"
                " occurred_at, title, url, excerpt, meta, fetched_at, state) VALUES"
                " (?, 'migration', 'old-notion', ?, 'migrated_task', ?, 'migration:old-notion', ?, ?, ?, ?, ?, ?,"
                " 'processed')",
                (item_id, r["old_page_id"], thread_key, page.get("created_time"), r["title"], r["old_url"] or None,
                 (evidence or "")[:1000], dumps({"old_source_id": old_source, "old_source_type": r["source_type"],
                                                 "priority": r["priority"], "confidence": r["confidence"],
                                                 "fingerprint": _prop(page, "指纹")}), clock.iso()))
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
