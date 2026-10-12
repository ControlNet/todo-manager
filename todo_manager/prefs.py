"""The user's Todo preferences file, the managed workspace context and the legacy archive.

``PREFERENCES.md`` is the user's: edited directly (by the user, or by Hermes on the user's explicit
request) and printed by the gates into every woken run. Native personal memory is never read or
written here."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from . import clock, i18n, prompt_safety, templates, userfiles
from .db import dumps, get_meta, set_meta, tx

AGENTS = "AGENTS.md"
ARCHIVE = "rules-archive.md"
PREFERENCES = "PREFERENCES.md"
PREFERENCES_CHARS = 8000              # shown in a run's context; the rest is pointed to
USERFILE = "workspace/AGENTS.md"      # its name in the baseline store
MIGRATION_FROM = "prefs.migration_from"
MIGRATED_AT = "prefs.rules_migrated_at"


def agents_path(home: Path) -> Path:
    return Path(home) / AGENTS


def archive_path(home: Path) -> Path:
    return Path(home) / ARCHIVE


def preferences_path(home: Path) -> Path:
    return Path(home) / PREFERENCES


def preferences(home: Path) -> str:
    """The user's Todo preferences as written ("" when there are none)."""
    return (userfiles.read_text(preferences_path(home)) or "").strip()


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
    ``todo-manager install`` (managed backup and replacement)."""
    if agents_path(home).exists():
        return None
    upgrade = plan_upgrade(home, language)
    apply_upgrade(home, language, upgrade)
    return upgrade


def problems(home: Path) -> list[str]:
    """Diagnostics for the managed workspace instructions and the preferences file."""
    out = []
    text = userfiles.read_text(agents_path(home))
    if text is None:
        out.append(f"{agents_path(home)} is missing: managed project instructions are unavailable"
                   " (`todo-manager install` creates it)")
    else:
        hits = prompt_safety.context_hits(text)
        if hits:
            out.append(f"{agents_path(home)} contains text Hermes refuses in a context file ({', '.join(hits)}): Hermes"
                       " replaces the whole file by a BLOCKED note; reword those lines")
    text, path = preferences(home), preferences_path(home)
    if len(text) > PREFERENCES_CHARS:
        out.append(f"{path} has {len(text)} characters: runs see only the first {PREFERENCES_CHARS}; shorten it")
    hits = prompt_safety.cron_hits(text)
    if hits:
        out.append(f"{path} contains text Hermes refuses in a cron prompt ({', '.join(hits)}): runs see a"
                   " withheld marker instead; reword those lines")
    for name in userfiles.Store(Path(home)).conflicts():
        out.append(f"upgrade conflict in {name}: see {userfiles.Store(Path(home)).conflict_note(name)}")
    return out


# --------------------------------------------------------------------------- rules migration

# Text of the migrated section (catalog keys; the section is written in the workspace language).
BUILTIN = ("prefs.migrated_cfp", "prefs.migrated_review", "prefs.migrated_questions")


def migrate_rules(conn: sqlite3.Connection, home: Path, language: str, from_version: int) -> dict[str, Any]:
    """Archive all legacy data verbatim; semantic import belongs to an explicitly asked Hermes agent."""
    if get_meta(conn, MIGRATED_AT):
        return {"skipped": "already migrated"}
    rules = [dict(r) for r in conn.execute("SELECT * FROM rules ORDER BY created_at, id")]
    revisions = [dict(r) for r in conn.execute("SELECT * FROM rule_revisions ORDER BY rule_id, revision")]
    import json
    target = archive_path(home)
    old = userfiles.read_text(target) or ""
    text = "# Legacy preferences: not imported into native memory\n\n"
    text += "Ask Hermes explicitly to review these records into this profile's native USER.md. "
    text += "Respect capacity and approval. Uncertain or unimported material stays here.\n\n"
    text += "```json\n" + json.dumps({"rules": rules, "revisions": revisions,
                                       "former_defaults": [i18n.t(k, language) for k in BUILTIN]
                                       if from_version <= 7 else []}, ensure_ascii=False, indent=2) + "\n```\n"
    userfiles.write_text(target, old + ("\n\n" if old else "") + text)
    report = {"from_schema": from_version, "archived": len(rules), "preferences_imported": False,
              "archive": str(target)}
    with tx(conn):
        set_meta(conn, MIGRATED_AT, clock.iso())
        set_meta(conn, "prefs.rules_migration", dumps(report))
    return report


def ensure_migrated(conn: sqlite3.Connection, home: Path, language: str) -> dict[str, Any] | None:
    """Run the rules migration once after the database was upgraded from schema <= 7."""
    origin = get_meta(conn, MIGRATION_FROM)
    if origin is None or get_meta(conn, MIGRATED_AT):
        return None
    return migrate_rules(conn, home, language, int(origin))
