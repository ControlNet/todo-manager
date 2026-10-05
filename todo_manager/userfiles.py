"""Files the user owns after install: installed from a template, upgraded by 3-way merge.

The Hermes skill, the two cron prompts and the workspace ``AGENTS.md`` start as Todo Manager
templates and then belong to the user (and to Hermes acting on the user's request). Todo
Manager keeps the text it installed last as a baseline under
``TODO_HOME/state/hermes/baseline/<name>``; an upgrade merges baseline -> new template into the
user's current text:

* nothing there yet          -> the template is installed (``created``)
* user text = template       -> nothing to do (``current``)
* user text = baseline       -> the template replaces it (``updated``)
* template = baseline        -> the user's text is kept (``kept``)
* both changed, no overlap   -> merged (``merged``)
* both changed the same part -> the user's text is kept, the merge with conflict markers is
                                written to ``state/hermes/conflicts/<name>`` (``conflict``)
* no baseline and a text that matches no known earlier template -> kept, the template is written
  next to it for review (``unknown-base``)

The baseline moves to the new template only when the result contains it (created, updated,
merged); after ``kept``/``conflict``/``unknown-base`` the next upgrade tries again from the old
baseline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import clock
from .merge3 import merge3

APPLIES = ("created", "updated", "merged")


@dataclass
class Upgrade:
    name: str
    state: str                      # created | current | updated | kept | merged | conflict | unknown-base
    text: str | None = None         # the new text of the user's file (None: leave it as it is)
    conflict_text: str | None = None
    base: str | None = None         # the baseline the merge used

    @property
    def writes(self) -> bool:
        return self.state in APPLIES

    def summary(self) -> str:
        return {
            "created": "installed from the template",
            "current": "matches the template",
            "updated": "unchanged since install: replaced by the new template",
            "kept": "your changes kept (the template did not change)",
            "merged": "your changes kept and the template changes merged in",
            "conflict": "your changes and the template change the same lines: your file is kept; see the conflict file",
            "unknown-base": "changed by you and no record of what was installed: kept; the new template is written"
                            " next to the conflict notes for review",
        }[self.state]


def write_text(path: Path, text: str, mode: int = 0o644) -> None:
    """Atomic replace (a reader never sees a half-written file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    tmp.replace(path)


def read_text(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def plan(name: str, live: str | None, baseline: str | None, shipped: str,
         earlier_templates: tuple[str, ...] = ()) -> Upgrade:
    """``earlier_templates``: texts this file had in releases that kept no baseline, newest first;
    a live text equal to one of them is treated as unchanged since install, otherwise the newest
    one is the merge base."""
    if live is None:
        return Upgrade(name, "created", shipped)
    if live == shipped:
        return Upgrade(name, "current", base=baseline)
    if baseline is None:
        baseline = next((t for t in earlier_templates if t == live), None)
        if baseline is None and earlier_templates:
            result = merge3(earlier_templates[0], live, shipped)
            if result.clean:
                return Upgrade(name, "merged", result.text, base=earlier_templates[0])
            return Upgrade(name, "conflict", conflict_text=result.text, base=earlier_templates[0])
        if baseline is None:
            return Upgrade(name, "unknown-base", conflict_text=shipped)
    if live == baseline:
        return Upgrade(name, "updated", shipped, base=baseline)
    if shipped == baseline:
        return Upgrade(name, "kept", base=baseline)
    result = merge3(baseline, live, shipped)
    if result.clean:
        return Upgrade(name, "merged", result.text, base=baseline)
    return Upgrade(name, "conflict", conflict_text=result.text, base=baseline)


@dataclass
class Store:
    """Baselines and conflict notes of one workspace."""
    home: Path

    @property
    def root(self) -> Path:
        return self.home / "state" / "hermes"

    def baseline_path(self, name: str) -> Path:
        return self.root / "baseline" / name

    def conflict_path(self, name: str) -> Path:
        return self.root / "conflicts" / name

    def baseline(self, name: str, legacy: Path | None = None) -> str | None:
        return read_text(self.baseline_path(name)) or read_text(legacy)

    def record(self, upgrade: Upgrade, shipped: str) -> None:
        """After the user's file was written (or found equal to the template): baseline = template,
        and an earlier conflict note is obsolete."""
        if upgrade.state in (*APPLIES, "current"):
            if read_text(self.baseline_path(upgrade.name)) != shipped:
                write_text(self.baseline_path(upgrade.name), shipped)
            for path in (self.conflict_path(upgrade.name), self.conflict_note(upgrade.name)):
                if path.exists():
                    path.unlink()
        elif upgrade.state in ("conflict", "unknown-base") and upgrade.conflict_text is not None:
            write_text(self.conflict_path(upgrade.name), upgrade.conflict_text)
            write_text(self.conflict_note(upgrade.name),
                       f"{clock.iso()} {upgrade.name}: {upgrade.summary()}.\n"
                       f"The file in use was not changed. {self.conflict_path(upgrade.name)} holds "
                       + ("the merge with conflict markers (<<<<<<< yours / ||||||| installed before / ======= /"
                          " >>>>>>> new template)" if upgrade.state == "conflict" else "the new template")
                       + ". Resolve it into the file in use, then run `todo-manager install` again: once the file"
                         " contains the template changes the note is cleared.\n")

    def conflict_note(self, name: str) -> Path:
        return self.root / "conflicts" / f"{name}.README.txt"

    def conflicts(self) -> list[str]:
        folder = self.root / "conflicts"
        if not folder.exists():
            return []
        return sorted(str(p.relative_to(folder))[: -len(".README.txt")]
                      for p in folder.rglob("*.README.txt"))
