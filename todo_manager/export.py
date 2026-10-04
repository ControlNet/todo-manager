"""Exports, rebuild verification, backup and restore. Everything is written under TODO_HOME.

* ``export_snapshot`` writes human-readable Markdown (tasks/, projects/, rules/, views/)
  plus ``data.json`` (every table) into ``exports/<stamp>/``.
* ``verify_rebuild`` loads ``data.json`` into a fresh database and compares every table,
  proving the export alone is enough to rebuild the state.
* ``backup`` / ``restore`` use SQLite's online backup API (safe while gates run).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import clock, db, delegation, rules, store
from .config import Config
from .notion.schema import NEXT_ACTOR_LABELS, PROJECT_STATUS_LABELS, TASK_STATUS_LABELS


def _stamp() -> str:
    return clock.now().strftime("%Y%m%dT%H%M%SZ")


def tables(conn: sqlite3.Connection) -> list[str]:
    return [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def dump(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    out = {}
    for name in tables(conn):
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({name})")]
        out[name] = [dict(r) for r in conn.execute(f"SELECT * FROM {name} ORDER BY {', '.join(cols)}")]
    return out


def _table_hash(rows: list[dict[str, Any]]) -> str:
    canon = sorted(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in rows)
    return hashlib.sha256("\n".join(canon).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- markdown

def _task_md(conn: sqlite3.Connection, t: dict[str, Any], projects: dict[str, str]) -> str:
    lines = [f"# {t['title']}", "",
             f"- ID: `{t['id']}` · 状态: {TASK_STATUS_LABELS.get(t['status'], t['status'])}"
             f" · 下一步由谁: {NEXT_ACTOR_LABELS.get(t['next_actor'], t['next_actor'])}"
             f" · 版本: {t['version']}"]
    if t.get("project_id"):
        lines.append(f"- 项目: {projects.get(t['project_id'], t['project_id'])} (`{t['project_id']}`)")
    dates = [f"{label}: {t[k][:10]}" for k, label in (("due_date", "截止"), ("follow_up_date", "跟进"),
                                                      ("snooze_until", "暂缓至")) if t.get(k)]
    if dates:
        lines.append("- " + " · ".join(dates))
    if t.get("focus"):
        lines.append("- 精选" + (f"：{t['focus_reason']}" if t.get("focus_reason") else ""))
    if t.get("waiting_on"):
        lines.append(f"- 等待对象: {t['waiting_on']}")
    for key, label in (("next_step", "下一步"), ("responsibility", "为什么是我的事"), ("done_criteria", "完成标准"),
                       ("due_basis", "截止依据"), ("summary", "背景"), ("change_note", "变化")):
        if t.get(key):
            lines += ["", f"## {label}", "", str(t[key])]
    ev = store.evidence_for(conn, "task", t["id"])
    if ev:
        lines += ["", "## 来源", ""]
        for e in ev:
            lines.append(f"- [{e.get('role')}] {e.get('source')} {e.get('title') or ''} {e.get('url') or ''}".rstrip())
    ds = delegation.for_task(conn, t["id"])
    if ds:
        lines += ["", "## 委派", ""]
        for d in ds:
            lines.append(f"- `{d['id']}` [{d['status']}] {d['title']}")
            for v in delegation.deliveries(conn, d["id"]):
                lines.append(f"  - {v['kind']} v{v['seq']} {v['received_at'][:16]}: {(v['summary'] or '')[:200]}")
    hist = store.history(conn, "task", t["id"])[-20:]
    if hist:
        lines += ["", "## 历史（最近 20 条）", ""]
        for h in hist:
            changes = json.dumps(h["changes"], ensure_ascii=False)
            lines.append(f"- v{h['version']} {h['at'][:16]} {h['actor']}/{h['origin']}: {changes[:300]}")
    return "\n".join(lines) + "\n"


def _project_md(conn: sqlite3.Connection, p: dict[str, Any]) -> str:
    lines = [f"# {p['title']}", "", f"- ID: `{p['id']}` · 状态: {PROJECT_STATUS_LABELS.get(p['status'], p['status'])}"
             + (f" · 领域: {p['domain']}" if p.get("domain") else "")]
    if p.get("repos"):
        lines.append("- 仓库: " + ", ".join(p["repos"]))
    for key, label in (("goal", "目标"), ("next_step", "下一步"), ("blockers", "阻碍"), ("recent_activity", "近期进展")):
        if p.get(key):
            lines += ["", f"## {label}", "", str(p[key])]
    tasks = store.list_tasks(conn, store.TASK_STATUSES, project_id=p["id"])
    if tasks:
        lines += ["", "## 任务", ""]
        lines += [f"- `{t['id']}` [{TASK_STATUS_LABELS[t['status']]}] {t['title']}" for t in tasks]
    return "\n".join(lines) + "\n"


def view_rows(conn: sqlite3.Connection, cfg: Config) -> dict[str, list[dict[str, Any]]]:
    """The home-page sections computed locally (same filters as the Notion views)."""
    today = clock.local_date(cfg.tz).isoformat()
    week = (clock.local_date(cfg.tz) + timedelta(days=7)).isoformat()
    since = clock.iso(clock.now() - timedelta(days=7))
    open_ = store.list_tasks(conn)

    def awake(t: dict[str, Any]) -> bool:
        return not t.get("snooze_until") or t["snooze_until"][:10] <= today

    agent_ids = {r["task_id"] for r in conn.execute(
        "SELECT task_id FROM delegations WHERE status IN ('queued','running','paused','quarantined','pending_submit')")}
    return {
        "⭐ 精选待办": [t for t in open_ if t["status"] in ("todo", "in_progress") and t["next_actor"] == "me"
                    and awake(t) and (t["focus"] or (t.get("due_date") and t["due_date"][:10] <= week))],
        "⏳ 等待别人": [t for t in open_ if t["status"] == "waiting" and awake(t)],
        "🤔 需要决定": [t for t in open_ if t["status"] == "decision"],
        "🤖 Agent 排队／处理中": [t for t in open_ if t["id"] in agent_ids],
        "✅ 待我验收／待我补充": [t for t in open_ if t["status"] == "review"],
        "🕘 最近完成与重要变化": [dict(r) for r in conn.execute(
            "SELECT * FROM tasks WHERE updated_at >= ? AND (status IN ('done','cancelled') OR change_note IS NOT NULL)"
            " ORDER BY updated_at DESC", (since,))],
    }


def export_snapshot(conn: sqlite3.Connection, cfg: Config, out_dir: Path | None = None) -> Path:
    root = out_dir or cfg.home / "exports" / _stamp()
    for sub in ("tasks", "projects", "rules", "views"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    projects = {p["id"]: p["title"] for p in store.list_projects(conn, ("active", "paused", "done", "archived"))}
    for t in store.list_tasks(conn, store.TASK_STATUSES):
        (root / "tasks" / f"{t['id']}.md").write_text(_task_md(conn, t, projects), encoding="utf-8")
    for pid in projects:
        (root / "projects" / f"{pid}.md").write_text(_project_md(conn, store.get_project(conn, pid)), encoding="utf-8")
    rule_lines = ["# 规则", ""]
    for status in ("active", "suggested", "retired"):
        rule_lines += [f"## {status}", "", rules.render_rules(rules.list_rules(conn, (status,))), ""]
    (root / "rules" / "rules.md").write_text("\n".join(rule_lines), encoding="utf-8")
    view_lines = [f"# 首页视图快照 {clock.local_dt(cfg.tz):%Y-%m-%d %H:%M}", ""]
    for name, rows in view_rows(conn, cfg).items():
        view_lines += [f"## {name}", ""] + [
            f"- `{t['id']}` {t['title']}" + (f" · 截止 {t['due_date'][:10]}" if t.get("due_date") else "")
            + (f" · {t['next_step']}" if t.get("next_step") else "") for t in rows] + ["(空)" if not rows else "", ""]
    (root / "views" / "home.md").write_text("\n".join(view_lines), encoding="utf-8")
    data = dump(conn)
    (root / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    manifest = {"exported_at": clock.iso(), "schema_version": db.schema_version(conn),
                "tables": {k: {"rows": len(v), "hash": _table_hash(v)} for k, v in data.items()}}
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return root


def verify_rebuild(data_path: Path) -> dict[str, Any]:
    """Rebuild a scratch database from ``data.json`` and compare every table with the export."""
    data = json.loads(Path(data_path).read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="tm-rebuild-") as tmp:
        conn = db.connect(Path(tmp) / "rebuild.sqlite")
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            with db.tx(conn):
                conn.execute("DELETE FROM meta")
                for name, rows in data.items():
                    for row in rows:
                        cols = list(row)
                        conn.execute(f"INSERT OR REPLACE INTO {name}({','.join(cols)}) VALUES"
                                     f" ({','.join('?' for _ in cols)})", [row[c] for c in cols])
            conn.execute("PRAGMA foreign_keys = ON")
            fk = [tuple(r) for r in conn.execute("PRAGMA foreign_key_check")]
            rebuilt = dump(conn)
        finally:
            conn.close()
    mismatched = [k for k in data if _table_hash(data[k]) != _table_hash(rebuilt.get(k, []))]
    return {"tables": len(data), "rows": sum(len(v) for v in data.values()), "mismatched": mismatched,
            "foreign_key_errors": len(fk), "ok": not mismatched and not fk}


def backup(conn: sqlite3.Connection, cfg: Config, keep: int | None = None, prefix: str = "todo") -> Path:
    target_dir = cfg.home / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{prefix}-{_stamp()}.sqlite"
    n = 1
    while path.exists():   # never overwrite an earlier backup taken within the same second
        n += 1
        path = target_dir / f"{prefix}-{_stamp()}-{n}.sqlite"
    dest = sqlite3.connect(str(path))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    if keep:
        for old in sorted(target_dir.glob("todo-*.sqlite"))[:-keep]:
            old.unlink()
    return path


def check_backup(path: Path) -> dict[str, Any]:
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = src.execute("PRAGMA integrity_check").fetchone()[0]
        version = src.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        counts = {t: src.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("tasks", "projects", "feedback")}
    finally:
        src.close()
    return {"integrity": integrity, "schema_version": int(version[0]) if version else None, "counts": counts}


def restore(conn: sqlite3.Connection, cfg: Config, path: Path) -> dict[str, Any]:
    """Replace the live database with ``path``. A safety backup of the current state is made first."""
    info = check_backup(path)
    if info["integrity"] != "ok":
        raise RuntimeError(f"backup failed integrity check: {info['integrity']}")
    if info["schema_version"] is None or info["schema_version"] > db.SCHEMA_VERSION:
        raise RuntimeError(f"backup schema {info['schema_version']} is not restorable by this code")
    safety = backup(conn, cfg, prefix="pre-restore")
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        src.backup(conn)
    finally:
        src.close()
    db.migrate(conn)
    return {**info, "safety_backup": str(safety)}
