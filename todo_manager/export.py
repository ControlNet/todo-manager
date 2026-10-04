"""Exports, rebuild verification, import, backup and restore. Everything is written under TODO_HOME.

* ``export_snapshot`` first copies the database with SQLite's online backup (one consistent
  read, safe while gates and applies run), then writes from that copy only: human-readable
  Markdown (tasks/, projects/, rules/, views/), ``data.json`` (every table) and ``workspace/``
  (config.toml without secrets, AGENTS.md, the run context ``runs/<id>`` of the runs in the
  snapshot, the delivery files the snapshot's deliveries refer to) into ``exports/<stamp>/``;
  ``manifest.json`` lists every file with its SHA-256, every table with its row hash, kept
  files that differ from their delivery record, and the history check.
* ``integrity_problems``: each task's and project's history holds exactly the versions 1..its
  version, and no foreign key dangles. Checked by export, rebuild, import (refused on a
  problem), backup check and restore (reported).
* ``verify_rebuild`` loads ``data.json`` into a fresh database and compares every table,
  proving the export alone is enough to rebuild the state.
* ``import_bundle`` builds a new workspace in an empty directory from an export (checked
  against the manifest first; nothing is written on a mismatch).
* ``backup`` / ``restore`` use SQLite's online backup API (safe while gates run).
* After an import or a restore, ``reconcile_after_restore`` releases claims of runs that no
  longer exist, holds Matrix reminders and Notion comments that may already have been sent
  (state pushes stay: they converge), and holds delegation submissions and messages until the
  user checks the board and runs ``todo-manager delegate release-hold``.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import clock, db, delegation, i18n, rules, store
from .db import loads
from .config import Config
from .files import sha256_file
from .notion.schema import label as option_label

EXPORT_FORMAT = "todo-manager-export/2"
WORKSPACE_FILES = ("config.toml", "AGENTS.md")
SECRET_KEY = re.compile(r"(token|secret|password|passwd|api_?key|private_?key|credential)", re.I)
SAFE_KEYS = {"token_env", "token_file"}   # names of where a secret lives, not the secret


class ImportError_(RuntimeError):
    """An export bundle cannot be imported (integrity, version or target problem)."""


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

def _t(key: str, **values: Any) -> str:
    return i18n.t(f"export.{key}", **values)


def _task_md(conn: sqlite3.Connection, t: dict[str, Any], projects: dict[str, str]) -> str:
    lines = [f"# {t['title']}", "",
             f"- ID: `{t['id']}` · {_t('status')}: {option_label('task_status', t['status'])}"
             f" · {_t('next_actor')}: {option_label('next_actor', t['next_actor'])}"
             f" · {_t('version')}: {t['version']}"]
    if t.get("project_id"):
        lines.append(f"- {_t('project')}: {projects.get(t['project_id'], t['project_id'])} (`{t['project_id']}`)")
    dates = [f"{_t(label)}: {t[k][:10]}" for k, label in (("due_date", "due"), ("follow_up_date", "follow_up"),
                                                          ("snooze_until", "snooze")) if t.get(k)]
    if dates:
        lines.append("- " + " · ".join(dates))
    if t.get("focus"):
        lines.append(f"- {_t('focus')}" + (f": {t['focus_reason']}" if t.get("focus_reason") else ""))
    if t.get("waiting_on"):
        lines.append(f"- {_t('waiting_on')}: {t['waiting_on']}")
    for key in ("next_step", "responsibility", "done_criteria", "due_basis", "summary", "change_note"):
        if t.get(key):
            lines += ["", f"## {_t(key)}", "", str(t[key])]
    ev = store.evidence_for(conn, "task", t["id"])
    if ev:
        lines += ["", f"## {_t('sources')}", ""]
        for e in ev:
            lines.append(f"- [{e.get('role')}] {e.get('source')} {e.get('title') or ''} {e.get('url') or ''}".rstrip())
    ds = delegation.for_task(conn, t["id"])
    if ds:
        lines += ["", f"## {_t('delegations')}", ""]
        for d in ds:
            lines.append(f"- `{d['id']}` [{d['status']}] {d['title']}")
            for v in delegation.deliveries(conn, d["id"]):
                lines.append(f"  - {v['kind']} v{v['seq']} {v['received_at'][:16]}: {(v['summary'] or '')[:200]}")
    hist = store.history(conn, "task", t["id"])[-20:]
    if hist:
        lines += ["", f"## {_t('history')}", ""]
        for h in hist:
            changes = json.dumps(h["changes"], ensure_ascii=False)
            lines.append(f"- v{h['version']} {h['at'][:16]} {h['actor']}/{h['origin']}: {changes[:300]}")
    return "\n".join(lines) + "\n"


def _project_md(conn: sqlite3.Connection, p: dict[str, Any]) -> str:
    lines = [f"# {p['title']}", "", f"- ID: `{p['id']}` · {_t('status')}: {option_label('project_status', p['status'])}"
             + (f" · {_t('domain')}: {p['domain']}" if p.get("domain") else "")]
    if p.get("repos"):
        lines.append(f"- {_t('repos')}: " + ", ".join(p["repos"]))
    for key in ("goal", "next_step", "blockers", "recent_activity"):
        if p.get(key):
            lines += ["", f"## {_t(key)}", "", str(p[key])]
    tasks = store.list_tasks(conn, store.TASK_STATUSES, project_id=p["id"])
    if tasks:
        lines += ["", f"## {_t('tasks')}", ""]
        lines += [f"- `{t['id']}` [{option_label('task_status', t['status'])}] {t['title']}" for t in tasks]
    return "\n".join(lines) + "\n"


def view_rows(conn: sqlite3.Connection, cfg: Config) -> dict[str, list[dict[str, Any]]]:
    """The home-page sections computed locally (same filters as the Notion views), by section key."""
    today = clock.local_date(cfg.tz).isoformat()
    week = (clock.local_date(cfg.tz) + timedelta(days=7)).isoformat()
    since = clock.iso(clock.now() - timedelta(days=7))
    open_ = store.list_tasks(conn)

    def awake(t: dict[str, Any]) -> bool:
        return not t.get("snooze_until") or t["snooze_until"][:10] <= today

    agent_ids = {r["task_id"] for r in conn.execute(
        "SELECT task_id FROM delegations WHERE status IN ('queued','running','paused','quarantined','pending_submit')")}
    return {
        "focus": [t for t in open_ if t["status"] in ("todo", "in_progress") and t["next_actor"] == "me"
                    and awake(t) and (t["focus"] or (t.get("due_date") and t["due_date"][:10] <= week))],
        "waiting": [t for t in open_ if t["status"] == "waiting" and awake(t)],
        "decision": [t for t in open_ if t["status"] == "decision"],
        "agent": [t for t in open_ if t["id"] in agent_ids],
        "review": [t for t in open_ if t["status"] == "review"],
        "recent": [dict(r) for r in conn.execute(
            "SELECT * FROM tasks WHERE updated_at >= ? AND (status IN ('done','cancelled') OR change_note IS NOT NULL)"
            " ORDER BY updated_at DESC", (since,))],
    }


def integrity_problems(conn: sqlite3.Connection) -> list[str]:
    """History and references that do not add up (empty when the state is whole)."""
    found = []
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        found.append(f"{len(fk)} foreign key errors")
    for kind, table in (("task", "tasks"), ("project", "projects")):
        for r in conn.execute(
                f"SELECT e.id, e.version, COUNT(h.id) AS n, COUNT(DISTINCT h.version) AS d, MIN(h.version) AS lo,"
                f" MAX(h.version) AS hi FROM {table} e LEFT JOIN history h ON h.entity_type=? AND h.entity_id=e.id"
                f" GROUP BY e.id, e.version ORDER BY e.id", (kind,)):
            if not (r["n"] == r["d"] == r["version"] and r["lo"] == 1 and r["hi"] == r["version"]):
                found.append(f"{kind} {r['id']} is at version {r['version']} but its history has {r['n']} entries"
                             f" for versions {r['lo']}..{r['hi']}")
    return found


def _snapshot(conn: sqlite3.Connection, path: Path) -> sqlite3.Connection:
    """A read-only copy of the database taken in one step of the online backup API."""
    dest = sqlite3.connect(str(path))
    try:
        conn.backup(dest)
        dest.execute("PRAGMA journal_mode = DELETE")
    finally:
        dest.close()
    snap = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
    snap.row_factory = sqlite3.Row
    return snap


def export_snapshot(conn: sqlite3.Connection, cfg: Config, out_dir: Path | None = None) -> Path:
    root = out_dir or cfg.home / "exports" / _stamp()
    root.mkdir(parents=True, exist_ok=True)
    snap_path = root.parent / f".{root.name}.snapshot.sqlite"
    snap = _snapshot(conn, snap_path)
    try:
        _write_export(snap, cfg, root)
    finally:
        snap.close()
        for leftover in (snap_path, Path(f"{snap_path}-journal")):
            leftover.unlink(missing_ok=True)
    return root


def _write_export(conn: sqlite3.Connection, cfg: Config, root: Path) -> None:
    """Everything below reads ``conn``, the snapshot: Markdown, data and files agree."""
    for sub in ("tasks", "projects", "rules", "views"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    projects = {p["id"]: p["title"] for p in store.list_projects(conn, ("active", "paused", "done", "archived"))}
    for t in store.list_tasks(conn, store.TASK_STATUSES):
        (root / "tasks" / f"{t['id']}.md").write_text(_task_md(conn, t, projects), encoding="utf-8")
    for pid in projects:
        (root / "projects" / f"{pid}.md").write_text(_project_md(conn, store.get_project(conn, pid)), encoding="utf-8")
    rule_lines = [f"# {_t('rules')}", ""]
    for status in ("active", "suggested", "retired"):
        rule_lines += [f"## {status}", "", rules.render_rules(rules.list_rules(conn, (status,))), ""]
    (root / "rules" / "rules.md").write_text("\n".join(rule_lines), encoding="utf-8")
    view_lines = [f"# {_t('views_title', when=f'{clock.local_dt(cfg.tz):%Y-%m-%d %H:%M}')}", ""]
    for key, rows in view_rows(conn, cfg).items():
        view_lines += [f"## {i18n.t(f'home.section.{key}')}", ""] + [
            f"- `{t['id']}` {t['title']}" + (f" · {_t('due')} {t['due_date'][:10]}" if t.get("due_date") else "")
            + (f" · {t['next_step']}" if t.get("next_step") else "") for t in rows] + [_t("empty") if not rows else "", ""]
    (root / "views" / "home.md").write_text("\n".join(view_lines), encoding="utf-8")
    data = dump(conn)
    (root / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    run_ids = [r["id"] for r in data.get("runs", [])]
    artifacts = [a for d in data.get("deliveries", []) for a in loads(d["artifacts"], []) if isinstance(a, dict)]
    redacted, artifact_problems = _export_workspace(cfg.home, root / "workspace", run_ids, artifacts)
    files = {str(p.relative_to(root)): {"sha256": sha256_file(p), "size": p.stat().st_size}
             for p in sorted(root.rglob("*")) if p.is_file() and p != root / "manifest.json"}
    manifest = {"format": EXPORT_FORMAT, "exported_at": clock.iso(), "schema_version": db.schema_version(conn),
                "tables": {k: {"rows": len(v), "hash": _table_hash(v)} for k, v in data.items()},
                "files": files, "redacted_config_keys": redacted, "artifact_problems": artifact_problems,
                "integrity": integrity_problems(conn),
                "not_exported": ["secrets (environment variables, token files)", "backups/", "state/locks",
                                 "state/hermes (machine-specific install record)",
                                 "files no run or delivery of the snapshot refers to (written after it)"]}
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")


def _scrub_config(text: str) -> tuple[str, list[str]]:
    """config.toml with values of secret-looking keys replaced; returns (text, redacted keys)."""
    redacted, out, section = [], [], ""
    for line in text.splitlines():
        head = re.match(r"\s*\[([^\]]+)\]", line)
        if head:
            section = head.group(1).strip()
        match = re.match(r"(\s*)([A-Za-z0-9_.-]+)(\s*=\s*)(.+)$", line)
        if match and SECRET_KEY.search(match.group(2)) and match.group(2) not in SAFE_KEYS:
            redacted.append(f"{section}.{match.group(2)}" if section else match.group(2))
            line = f'{match.group(1)}{match.group(2)}{match.group(3)}"<redacted: set it again after import>"'
        out.append(line)
    return "\n".join(out) + "\n", redacted


def _export_workspace(home: Path, dest: Path, run_ids: list[str],
                      artifacts: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Config (secrets redacted), AGENTS.md, the context of ``run_ids`` and the kept delivery
    files in ``artifacts``. Returns (redacted config keys, kept files that are missing or no
    longer match their delivery record)."""
    dest.mkdir(parents=True, exist_ok=True)
    redacted: list[str] = []
    for name in WORKSPACE_FILES:
        src = home / name
        if not src.is_file():
            continue
        if name == "config.toml":
            text, redacted = _scrub_config(src.read_text(encoding="utf-8"))
            (dest / name).write_text(text, encoding="utf-8")
        else:
            shutil.copyfile(src, dest / name)
    for run_id in run_ids:
        if (home / "runs" / run_id).is_dir():
            shutil.copytree(home / "runs" / run_id, dest / "runs" / run_id, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("*.tmp"))
    problems: list[str] = []
    for art in artifacts:
        rel = art.get("path")
        if not rel:
            continue
        src = home / rel
        if not src.is_file():
            problems.append(f"{rel}: missing")
            continue
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest / rel)
        if sha256_file(dest / rel) != art.get("sha256"):
            problems.append(f"{rel}: differs from its delivery record")
    return redacted, problems


def check_bundle(root: Path) -> dict[str, Any]:
    """Compare an export with its manifest: every listed file present with the same SHA-256,
    no unlisted workspace file, every table hash equal."""
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise ImportError_(f"{root} has no manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != EXPORT_FORMAT:
        raise ImportError_(f"unsupported export format {manifest.get('format')!r} (need {EXPORT_FORMAT})")
    problems = []
    for rel, info in (manifest.get("files") or {}).items():
        path = root / rel
        if not path.is_file():
            problems.append(f"missing {rel}")
        elif sha256_file(path) != info["sha256"]:
            problems.append(f"changed {rel}")
    listed = set(manifest.get("files") or {})
    for path in (root / "workspace").rglob("*") if (root / "workspace").is_dir() else []:
        if path.is_file() and str(path.relative_to(root)) not in listed:
            problems.append(f"unlisted {path.relative_to(root)}")
    data = json.loads((root / "data.json").read_text(encoding="utf-8")) if (root / "data.json").is_file() else {}
    for name, info in (manifest.get("tables") or {}).items():
        if _table_hash(data.get(name, [])) != info["hash"]:
            problems.append(f"table {name} differs from the manifest")
    if int(manifest.get("schema_version") or 0) > db.SCHEMA_VERSION:
        problems.append(f"schema {manifest.get('schema_version')} is newer than this code ({db.SCHEMA_VERSION})")
    if problems:
        raise ImportError_("export bundle does not match its manifest: " + "; ".join(problems[:20]))
    return {"manifest": manifest, "data": data}


def import_bundle(root: Path, target: Path) -> dict[str, Any]:
    """Build a new workspace in ``target`` (absent or empty) from an export. The bundle is
    checked first; the workspace is assembled next to the target and moved into place only when
    complete. Afterwards nothing is replayed: see ``reconcile_after_restore``."""
    root, target = Path(root).expanduser(), Path(target).expanduser()
    if target.exists() and any(target.iterdir()):
        raise ImportError_(f"{target} is not empty; import only into a new directory")
    bundle = check_bundle(root)
    manifest, data = bundle["manifest"], bundle["data"]
    staging = target.parent / f".{target.name}.importing"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        if (root / "workspace").is_dir():
            shutil.copytree(root / "workspace", staging, dirs_exist_ok=True)
        (staging / "state").mkdir(exist_ok=True)
        conn = sqlite3.connect(str(staging / "state" / "todo.sqlite"), isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            db.migrate(conn, target=int(manifest["schema_version"]))
            conn.execute("PRAGMA foreign_keys = OFF")
            with db.tx(conn):
                conn.execute("DELETE FROM meta")
                for name, rows in data.items():
                    for row in rows:
                        cols = list(row)
                        conn.execute(f"INSERT OR REPLACE INTO {name}({','.join(cols)}) VALUES"
                                     f" ({','.join('?' for _ in cols)})", [row[c] for c in cols])
            mismatched = [k for k, rows in dump(conn).items() if k in data and _table_hash(rows) != _table_hash(data[k])]
            if mismatched:
                raise ImportError_(f"rebuilt tables differ from the export: {mismatched}")
            conn.execute("PRAGMA foreign_keys = ON")
            broken = integrity_problems(conn)
            if broken:
                raise ImportError_("the exported state is not whole (history or references): " + "; ".join(broken[:20])
                                   + ". Fix the source workspace (or restore a backup there) and export again")
            db.migrate(conn)
            report = reconcile_after_restore(conn, "import")
        finally:
            conn.close()
        staging.rename(target) if not target.exists() else _move_into(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"ok": True, "home": str(target), "schema_version": db.SCHEMA_VERSION,
            "from_schema": manifest["schema_version"], "tables": len(data),
            "files": len(manifest.get("files") or {}), "redacted_config_keys": manifest.get("redacted_config_keys"),
            "artifact_problems": manifest.get("artifact_problems", []), "integrity": [], **report}


def _move_into(staging: Path, target: Path) -> None:
    for child in staging.iterdir():
        child.rename(target / child.name)
    staging.rmdir()


def reconcile_after_restore(conn: sqlite3.Connection, reason: str) -> dict[str, Any]:
    """After an import or a restore: runs that held claims do not exist any more; effects that
    may already have happened are not replayed; workers are not started or messaged until the
    user releases the hold."""
    now = clock.iso()
    report: dict[str, Any] = {}
    with db.tx(conn):
        report["released_batches"] = conn.execute(
            "UPDATE batches SET state='mature', claimed_run=NULL, claimed_at=NULL WHERE state='claimed'").rowcount
        report["released_feedback"] = conn.execute(
            "UPDATE feedback SET claimed_run=NULL, claimed_at=NULL WHERE status='pending' AND claimed_run IS NOT NULL"
        ).rowcount
        report["released_followups"] = conn.execute("DELETE FROM followup_checks WHERE state='claimed'").rowcount
        conn.execute("UPDATE daily_jobs SET updated_at='1970-01-01T00:00:00.000Z' WHERE state='claimed'")
        report["held_effects"] = conn.execute(
            "UPDATE outbox SET status='dead', last_error=?, updated_at=? WHERE status IN"
            " ('pending','in_flight','uncertain','failed') AND (target='matrix' OR op='comment')",
            (f"held after {reason}: not replayed because it may already have been sent; re-send by hand if needed",
             now)).rowcount
        delegation.hold(conn, reason)
    report["delegations_held"] = True
    return report


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
            broken = integrity_problems(conn)
            rebuilt = dump(conn)
        finally:
            conn.close()
    mismatched = [k for k in data if _table_hash(data[k]) != _table_hash(rebuilt.get(k, []))]
    return {"tables": len(data), "rows": sum(len(v) for v in data.values()), "mismatched": mismatched,
            "foreign_key_errors": len(fk), "integrity_problems": broken, "ok": not mismatched and not broken}


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
    src.row_factory = sqlite3.Row
    try:
        integrity = src.execute("PRAGMA integrity_check").fetchone()[0]
        version = src.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        counts = {t: src.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("tasks", "projects", "feedback")}
        broken = integrity_problems(src) if integrity == "ok" else []
    finally:
        src.close()
    return {"integrity": integrity, "schema_version": int(version[0]) if version else None, "counts": counts,
            "integrity_problems": broken}


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
    # Reported, not refused: a restore is the way back, and the safety backup keeps what was live.
    return {**info, "safety_backup": str(safety), **reconcile_after_restore(conn, "restore"),
            "integrity_problems": integrity_problems(conn)}
