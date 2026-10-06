"""Bounded installation backup, never a copy of the Hermes profile."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path


def capture(paths, jobs: dict) -> Path:
    root = paths.todo_home / "backups" / "framework"
    root.mkdir(parents=True, exist_ok=True)
    dest = Path(tempfile.mkdtemp(prefix="upgrade-", dir=root))
    for name in ("AGENTS.md", "rules-archive.md", "state/hermes"):
        src = paths.todo_home / name
        target = dest / name
        if src.is_dir():
            shutil.copytree(src, target)
        elif src.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
    from .hermes_install import live_skill
    skill = live_skill(paths, jobs)
    if skill:
        shutil.copyfile(skill, dest / "SKILL.md")
    (dest / "cron.json").write_text(json.dumps(jobs, ensure_ascii=False, indent=2))
    database = paths.todo_home / "state" / "todo.sqlite"
    if database.is_file():
        source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        target = sqlite3.connect(dest / "todo.sqlite")
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
    (dest / "migration.json").write_text(json.dumps({
        "framework_backup": True, "preferences_imported": False,
        "instructions": "Ask Hermes explicitly to review archived preferences into native USER.md; "
                        "do not import entire files or change memory limits."
    }, indent=2))
    return dest
