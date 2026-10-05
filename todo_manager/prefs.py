"""The user's Todo conventions: ``TODO_HOME/AGENTS.md``.

The file belongs to the user (and to Hermes acting on the user's request). Hermes loads it as the
project context of the two cron jobs, whose working directory is ``TODO_HOME``; in a chat the main
agent reads it with ``todo-manager prefs show``. Todo Manager only creates it from a template,
upgrades it by 3-way merge (``userfiles``), checks it against Hermes' context-file scan, and
records its hash per run (``file_hash``) so that a run can tell when the conventions changed
while it worked.

``migrate_rules`` retires the rules engine of earlier versions (schema <= 7): active rules, and
the built-in categories and limits those versions applied, become a "Migrated conventions"
section of ``AGENTS.md``; suggested and retired rules (and any rule Hermes would refuse to load)
go to ``rules-archive.md``, which Hermes does not read. The old tables stay for audit and export.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from . import clock, i18n, prompt_safety, templates, userfiles
from .db import dumps, get_meta, set_meta, tx

AGENTS = "AGENTS.md"
ARCHIVE = "rules-archive.md"
USERFILE = "workspace/AGENTS.md"      # its name in the baseline store
MIGRATION_FROM = "prefs.migration_from"
MIGRATED_AT = "prefs.rules_migrated_at"


def agents_path(home: Path) -> Path:
    return Path(home) / AGENTS


def archive_path(home: Path) -> Path:
    return Path(home) / ARCHIVE


def file_hash(home: Path) -> str:
    """Identity of the conventions as they are now ("absent" when there is no file)."""
    try:
        data = agents_path(home).read_bytes()
    except FileNotFoundError:
        return "absent"
    return hashlib.sha256(data).hexdigest()[:16]


def show(home: Path) -> str:
    return userfiles.read_text(agents_path(home)) or ""


def plan_upgrade(home: Path, language: str) -> userfiles.Upgrade:
    store = userfiles.Store(Path(home))
    return userfiles.plan(USERFILE, userfiles.read_text(agents_path(home)), store.baseline(USERFILE),
                          templates.workspace_agents(language), templates.earlier(USERFILE))


def apply_upgrade(home: Path, language: str, upgrade: userfiles.Upgrade) -> None:
    shipped = templates.workspace_agents(language)
    if upgrade.writes and upgrade.text is not None:
        userfiles.write_text(agents_path(home), upgrade.text)
    userfiles.Store(Path(home)).record(upgrade, shipped)


def ensure(home: Path, language: str) -> userfiles.Upgrade | None:
    """Create the file from the template when it is missing (``init``); an existing file is left to
    ``todo-manager install`` (upgrade with merge)."""
    if agents_path(home).exists():
        return None
    upgrade = plan_upgrade(home, language)
    apply_upgrade(home, language, upgrade)
    return upgrade


def problems(home: Path) -> list[str]:
    """What keeps the conventions from reaching Hermes as written."""
    out = []
    text = userfiles.read_text(agents_path(home))
    if text is None:
        out.append(f"{agents_path(home)} is missing: the cron runs work without your conventions"
                   " (`todo-manager install` creates it)")
    else:
        hits = prompt_safety.context_hits(text)
        if hits:
            out.append(f"{agents_path(home)} contains text Hermes refuses in a context file ({', '.join(hits)}): Hermes"
                       " replaces the whole file by a BLOCKED note; reword those lines")
    for name in userfiles.Store(Path(home)).conflicts():
        out.append(f"upgrade conflict in {name}: see {userfiles.Store(Path(home)).conflict_note(name)}")
    return out


# --------------------------------------------------------------------------- rules migration

# Text of the migrated section (catalog keys; the section is written in the workspace language).
BUILTIN = ("prefs.migrated_cfp", "prefs.migrated_review", "prefs.migrated_questions")


def _scope(conn: sqlite3.Connection, rule: sqlite3.Row) -> str:
    if rule["scope_type"] == "global":
        return ""
    title = None
    if rule["scope_type"] == "project":
        row = conn.execute("SELECT title FROM projects WHERE id=?", (rule["scope_id"],)).fetchone()
        title = row["title"] if row else None
    elif rule["scope_type"] == "task":
        row = conn.execute("SELECT title FROM tasks WHERE id=?", (rule["scope_id"],)).fetchone()
        title = row["title"] if row else None
    return f"; scope: {rule['scope_type']} {rule['scope_id']}" + (f" \"{title}\"" if title else "")


def _bullet(conn: sqlite3.Connection, rule: sqlite3.Row, lang: str) -> str:
    body = " ".join((rule["body"] or "").split())
    title = " ".join((rule["title"] or "").split())
    return f"- {title}: {body} " + i18n.t("prefs.migrated_rule", lang, id=rule["id"], rev=rule["revision"],
                                          scope=_scope(conn, rule))


def migrate_rules(conn: sqlite3.Connection, home: Path, language: str, from_version: int) -> dict[str, Any]:
    """Move the rules of a workspace created by an earlier version into its files. Idempotent:
    done once (``MIGRATED_AT``); the old tables are left as they are."""
    if get_meta(conn, MIGRATED_AT):
        return {"skipped": "already migrated"}
    lang = "zh-CN" if language == "zh-CN" else "en"
    heading = i18n.t("prefs.migrated_heading", lang)
    date = clock.local_date("UTC").isoformat()
    rules = conn.execute("SELECT * FROM rules ORDER BY created_at, id").fetchall()
    active = [r for r in rules if r["status"] == "active"]
    blocked = [(r, prompt_safety.context_hits(f"{r['title']}\n{r['body']}")) for r in active]
    blocked = [(r, hits) for r, hits in blocked if hits]
    carried = [r for r in active if r["id"] not in {b[0]["id"] for b in blocked}]
    report: dict[str, Any] = {"from_schema": from_version, "active": len(carried), "blocked": len(blocked),
                              "archived": len(rules) - len(active)}

    upgrade = plan_upgrade(home, language)
    apply_upgrade(home, language, upgrade)
    report["agents_md"] = upgrade.state
    lines = [heading, "", i18n.t("prefs.migrated_intro", lang, date=date), ""]
    if 0 < from_version <= 7:
        lines += [f"- {i18n.t(key, lang)}" for key in BUILTIN]
    lines += [_bullet(conn, r, lang) for r in carried]
    if len(lines) > 4:
        current = userfiles.read_text(agents_path(home)) or ""
        if heading not in current:
            userfiles.write_text(agents_path(home), current.rstrip("\n") + "\n\n" + "\n".join(lines) + "\n")
            report["written"] = str(agents_path(home))

    if len(rules) > len(carried):
        _write_archive(conn, home, rules, blocked, date)
        report["archive"] = str(archive_path(home))
    with tx(conn):
        set_meta(conn, MIGRATED_AT, clock.iso())
        set_meta(conn, "prefs.rules_migration", dumps(report))
    return report


def _write_archive(conn: sqlite3.Connection, home: Path, rules: list[sqlite3.Row],
                   blocked: list[tuple[sqlite3.Row, list[str]]], date: str) -> None:
    def entry(r: sqlite3.Row) -> str:
        origin = f", from feedback {r['from_feedback_id']}" if r["from_feedback_id"] else ""
        return (f"- {' '.join(r['title'].split())}: {' '.join(r['body'].split())}"
                f" (rule {r['id']}, revision {r['revision']}{_scope(conn, r)}{origin})")

    out = ["# Rules archive", "",
           f"Rules of the previous Todo Manager version that were not carried into `AGENTS.md` when this workspace"
           f" was upgraded on {date}. Hermes does not read this file. To use one, copy it into `AGENTS.md` (or ask"
           " Hermes to).", ""]
    for status, title in (("suggested", "Suggested (never confirmed by you)"), ("retired", "Retired")):
        group = [r for r in rules if r["status"] == status]
        if group:
            out += [f"## {title}", "", *[entry(r) for r in group], ""]
    if blocked:
        out += ["## Active, not carried over", "",
                "These were active, but their wording matches patterns for which Hermes refuses a whole context file."
                " Reword before adding them to `AGENTS.md`.", ""]
        out += [f"{entry(r)} [patterns: {', '.join(hits)}]" for r, hits in blocked] + [""]
    revisions = conn.execute("SELECT * FROM rule_revisions ORDER BY rules_version, rule_id, revision").fetchall()
    if revisions:
        out += ["## History", "", "| rule | revision | status | by | at | feedback |", "|---|---|---|---|---|---|"]
        out += [f"| {r['rule_id']} | {r['revision']} | {r['status']} | {r['actor']} | {r['at'][:16]} |"
                f" {r['feedback_id'] or ''} |" for r in revisions] + [""]
    userfiles.write_text(archive_path(home), "\n".join(out))


def ensure_migrated(conn: sqlite3.Connection, home: Path, language: str) -> dict[str, Any] | None:
    """Run the rules migration once after the database was upgraded from schema <= 7."""
    origin = get_meta(conn, MIGRATION_FROM)
    if origin is None or get_meta(conn, MIGRATED_AT):
        return None
    return migrate_rules(conn, home, language, int(origin))
