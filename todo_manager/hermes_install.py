"""Install and inspect the Hermes integration: skill, pre-run scripts, cron jobs, launcher.

Ownership rules:

* Skill: Hermes (or the user) may evolve an installed skill, so an existing ``todo-manager``
  skill is NEVER overwritten. The copy this installer wrote is kept as a baseline under
  ``TODO_HOME/state/hermes/``; when the shipped skill changes, a proposal (new text plus
  diffs against the baseline and the live skill) is written for review instead.
* Pre-run scripts, the cron job definitions and the launcher belong to todo-manager and are
  overwritten (a diff is shown first).
* New cron jobs are created paused. Existing jobs keep their paused/enabled state and their
  skill references (the curator may have renamed the skill). Resuming is a separate command.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Callable

from . import clock, config

SKILL_NAME = "todo-manager"
SKILL_CATEGORY = "productivity"
LAUNCHER_MARK = "# todo-manager launcher (generated; overwritten by `todo-manager hermes install`)"
SCRIPT_MARK = "# todo-manager pre-run gate (generated; overwritten by `todo-manager hermes install`)"
JOBS = {
    "intake": {"name": "todo-manager-intake", "schedule": "*/5 * * * *"},
    "feedback": {"name": "todo-manager-feedback", "schedule": "2-59/5 * * * *"},
}
Runner = Callable[..., subprocess.CompletedProcess]


def asset(name: str) -> str:
    return resources.files("todo_manager.hermes_assets").joinpath(name).read_text(encoding="utf-8")


def _sha(text: str | None) -> str | None:
    return hashlib.sha256(text.encode()).hexdigest()[:16] if text is not None else None


def _read(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.exists() else None


def _diff(old: str | None, new: str, name: str) -> str:
    return "".join(difflib.unified_diff((old or "").splitlines(True), new.splitlines(True),
                                        f"{name} (current)", f"{name} (new)", n=2))


@dataclass
class Paths:
    hermes_home: Path
    todo_home: Path
    launcher: Path
    source_root: Path = config.SOURCE_ROOT
    python: str = sys.executable

    @classmethod
    def from_config(cls, cfg: config.Config) -> "Paths":
        hh = cfg.get("hermes.home") or os.environ.get("HERMES_HOME") or "~/.hermes"
        return cls(Path(hh).expanduser(), cfg.home,
                   Path(cfg.get("hermes.launcher") or "~/.local/bin/todo-manager").expanduser())

    @property
    def skills_root(self) -> Path:
        return self.hermes_home / "skills"

    @property
    def install_skill_path(self) -> Path:
        return self.skills_root / SKILL_CATEGORY / SKILL_NAME / "SKILL.md"

    @property
    def state_dir(self) -> Path:
        return self.todo_home / "state" / "hermes"

    @property
    def baseline(self) -> Path:
        return self.state_dir / "skill-baseline" / "SKILL.md"

    @property
    def proposal_dir(self) -> Path:
        return self.state_dir / "skill-proposal"

    def script(self, entry: str) -> Path:
        return self.hermes_home / "scripts" / f"todo_manager_gate_{entry}.py"

    @property
    def jobs_file(self) -> Path:
        return self.hermes_home / "cron" / "jobs.json"


# --------------------------------------------------------------------------- generated files

def launcher_text(p: Paths) -> str:
    return f"""#!{p.python}
{LAUNCHER_MARK}
import os
import sys

sys.dont_write_bytecode = True
os.environ.setdefault("TODO_HOME", {str(p.todo_home)!r})
sys.path.insert(0, {str(p.source_root)!r})
from todo_manager.cli import main

sys.exit(main())
"""


def script_text(p: Paths, entry: str) -> str:
    return f"""{SCRIPT_MARK}
# Hermes runs this before the {JOBS[entry]['name']} job; the last stdout line is the wake gate.
import os
import sys

sys.dont_write_bytecode = True
os.environ["TODO_HOME"] = {str(p.todo_home)!r}
sys.path.insert(0, {str(p.source_root)!r})
from todo_manager.cli import main

sys.exit(main(["gate", {entry!r}]))
"""


# --------------------------------------------------------------------------- inspection

def _frontmatter_name(text: str) -> str | None:
    if not text.startswith("---"):
        return None
    for line in text.split("\n---", 1)[0].splitlines()[1:]:
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return None


def find_live_skills(p: Paths, names: set[str]) -> dict[str, Path]:
    found: dict[str, Path] = {}
    if not p.skills_root.exists():
        return found
    for path in sorted(p.skills_root.rglob("SKILL.md")):
        if ".archive" in path.parts or "archive" in path.parts:
            continue
        try:
            name = _frontmatter_name(path.read_text(encoding="utf-8")) or path.parent.name
        except OSError:
            continue
        if name in names and name not in found:
            found[name] = path
    return found


def load_jobs(p: Paths) -> dict[str, dict[str, Any]]:
    if not p.jobs_file.exists():
        return {}
    data = json.loads(p.jobs_file.read_text(encoding="utf-8"))
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    wanted = {spec["name"] for spec in JOBS.values()}
    return {j["name"]: j for j in jobs if j.get("name") in wanted}


def skill_status(p: Paths, jobs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    shipped = asset("skill/SKILL.md")
    refs: set[str] = {SKILL_NAME}
    for j in jobs.values():
        refs.update(j.get("skills") or [])
    live = find_live_skills(p, refs)
    live_path = live.get(SKILL_NAME) or next(iter(live.values()), None)
    live_text = _read(live_path) if live_path else None
    baseline = _read(p.baseline)
    status = {"shipped": _sha(shipped), "baseline": _sha(baseline), "live": _sha(live_text),
              "live_path": str(live_path) if live_path else None,
              "job_skill_refs": sorted({s for j in jobs.values() for s in (j.get("skills") or [])})}
    if live_text is None:
        status["state"] = "missing"
    elif baseline is None:
        status["state"] = "unmanaged" if live_text != shipped else "same-as-shipped"
    elif live_text == baseline and shipped == baseline:
        status["state"] = "up-to-date"
    elif live_text == baseline:
        status["state"] = "update-available"
    elif shipped == baseline:
        status["state"] = "evolved-in-hermes"
    else:
        status["state"] = "both-changed"
    return status


# --------------------------------------------------------------------------- plan

@dataclass
class Action:
    kind: str
    target: str
    summary: str
    diff: str = ""
    run: Callable[[], Any] | None = None


@dataclass
class Installer:
    cfg: config.Config
    paths: Paths
    runner: Runner = subprocess.run
    log: list[str] = field(default_factory=list)

    def hermes(self, *args: str) -> str:
        cmd = [self.cfg.get("notify.hermes_bin", "hermes"), *args]
        proc = self.runner(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd[:3])} failed (exit {proc.returncode}): {(proc.stdout or '')[-300:]}")
        return proc.stdout

    # ---------------------------------------------------------------- files

    def _write(self, path: Path, text: str, mode: int = 0o644) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, mode)
        tmp.replace(path)

    def file_action(self, path: Path, text: str, mark: str, mode: int = 0o644) -> Action:
        current = _read(path)
        if current == text:
            return Action("noop", str(path), "up to date")
        if current is not None and mark not in current:
            return Action("conflict", str(path), "exists and was not written by todo-manager; left untouched")
        verb = "update" if current is not None else "create"
        return Action(verb, str(path), f"{verb} generated file", _diff(current, text, path.name),
                      lambda: self._write(path, text, mode))

    # ---------------------------------------------------------------- skill

    def skill_action(self, jobs: dict[str, dict[str, Any]]) -> Action:
        st = skill_status(self.paths, jobs)
        shipped = asset("skill/SKILL.md")
        if st["state"] == "missing":
            def install() -> None:
                self._write(self.paths.install_skill_path, shipped)
                self._write(self.paths.baseline, shipped)
            return Action("create", str(self.paths.install_skill_path), "install skill (user-owned)", run=install)
        if st["state"] in ("up-to-date", "same-as-shipped"):
            def adopt_baseline() -> None:
                if _read(self.paths.baseline) != shipped:
                    self._write(self.paths.baseline, shipped)
            return Action("noop", st["live_path"], "skill matches the shipped version", run=adopt_baseline)
        if st["state"] == "evolved-in-hermes":
            return Action("keep", st["live_path"], "live skill was changed in Hermes; kept (shipped skill unchanged)")
        live_text = _read(Path(st["live_path"])) or ""
        baseline = _read(self.paths.baseline)

        def propose() -> None:
            d = self.paths.proposal_dir
            self._write(d / "SKILL.md", shipped)
            self._write(d / "shipped-vs-live.diff", _diff(live_text, shipped, "SKILL.md"))
            if baseline is not None:
                self._write(d / "baseline-vs-shipped.diff", _diff(baseline, shipped, "SKILL.md"))
                self._write(d / "baseline-vs-live.diff", _diff(baseline, live_text, "SKILL.md"))
            self._write(d / "README.txt",
                        f"Skill state: {st['state']} ({clock.iso()}).\nThe live skill at {st['live_path']} was NOT"
                        " changed. Review the diffs; to adopt the shipped text, merge it into the live skill by hand"
                        f" and then record it as the new baseline by copying it to {self.paths.baseline}.\n")
        return Action("propose", str(self.paths.proposal_dir),
                      f"skill exists ({st['state']}); NOT overwritten, proposal written for review",
                      _diff(live_text, shipped, "SKILL.md"), propose)

    # ---------------------------------------------------------------- cron jobs

    def job_args(self, entry: str, existing: dict[str, Any] | None) -> list[str]:
        spec = JOBS[entry]
        args = ["--name", spec["name"], "--prompt", asset(f"cron/{entry}.md").strip(),
                "--script", self.paths.script(entry).name, "--interpreter", self.paths.python,
                "--deliver", "local"]
        failure = self.cfg.get("hermes.failure_deliver") or self.cfg.get("notify.matrix_target")
        if failure:
            args += ["--failure-deliver", failure]
        for key, flag in (("hermes.model", "--model"), ("hermes.provider", "--provider"),
                          ("hermes.reasoning_effort", "--reasoning-effort")):
            if self.cfg.get(key):
                args += [flag, str(self.cfg.get(key))]
        if existing is None or not (existing.get("skills") or []):
            args += ["--skill", SKILL_NAME]
        return args

    def job_action(self, entry: str, jobs: dict[str, dict[str, Any]]) -> Action:
        spec = JOBS[entry]
        existing = jobs.get(spec["name"])
        args = self.job_args(entry, existing)
        if existing is None:
            prompt = asset(f"cron/{entry}.md").strip()
            cmd = ["cron", "create", "--paused", "--paused-reason", "installed by todo-manager; resume after review",
                   *_without(args, "--prompt"), spec["schedule"], prompt]
            return Action("create", spec["name"], f"create cron job (paused), schedule {spec['schedule']}",
                          run=lambda: self.hermes(*cmd))
        changes = []
        if (existing.get("prompt") or "").strip() != asset(f"cron/{entry}.md").strip():
            changes.append("prompt")
        if existing.get("script") != self.paths.script(entry).name:
            changes.append("script")
        sched = (existing.get("schedule") or {}).get("expr")
        if sched != spec["schedule"]:
            changes.append("schedule")
        if existing.get("deliver") != "local":
            changes.append("deliver")
        if existing.get("workdir"):
            changes.append("workdir (cleared: no AGENTS.md for this job)")
        cmd = ["cron", "edit", existing["id"], "--schedule", spec["schedule"], *args]
        if existing.get("workdir"):
            cmd += ["--workdir", ""]
        state = "paused" if existing.get("state") == "paused" or not existing.get("enabled", True) else "enabled"
        summary = (f"update cron job {existing['id']} ({state}, kept as is): " + ", ".join(changes)) if changes \
            else f"cron job {existing['id']} ({state}) definition re-applied"
        return Action("update", spec["name"], summary, run=lambda: self.hermes(*cmd))

    # ---------------------------------------------------------------- plan / apply

    def plan(self) -> list[Action]:
        jobs = load_jobs(self.paths)
        actions = [self.file_action(self.paths.launcher, launcher_text(self.paths), LAUNCHER_MARK, 0o755)]
        for entry in JOBS:
            actions.append(self.file_action(self.paths.script(entry), script_text(self.paths, entry), SCRIPT_MARK))
        actions.append(self.skill_action(jobs))
        for entry in JOBS:
            actions.append(self.job_action(entry, jobs))
        return actions

    def apply(self, actions: list[Action]) -> list[str]:
        done = []
        for a in actions:
            if a.run is None or a.kind in ("conflict", "keep"):
                continue
            a.run()
            done.append(f"{a.kind} {a.target}")
        record = {"installed_at": clock.iso(), "source_root": str(self.paths.source_root), "python": self.paths.python,
                  "jobs": {n: j.get("id") for n, j in load_jobs(self.paths).items()},
                  "skill": skill_status(self.paths, load_jobs(self.paths))}
        self._write(self.paths.state_dir / "install.json", json.dumps(record, ensure_ascii=False, indent=1))
        return done

    def set_jobs_state(self, state: str) -> list[str]:
        out = []
        for name, job in load_jobs(self.paths).items():
            self.hermes("cron", "resume" if state == "resume" else "pause", job["id"])
            out.append(f"{state} {name} ({job['id']})")
        return out


def _without(args: list[str], flag: str) -> list[str]:
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a == flag:
            skip = True
            continue
        out.append(a)
    return out


def status(cfg: config.Config) -> dict[str, Any]:
    p = Paths.from_config(cfg)
    jobs = load_jobs(p)
    files = {}
    for entry in JOBS:
        current = _read(p.script(entry))
        files[str(p.script(entry))] = "missing" if current is None else (
            "current" if current == script_text(p, entry) else "outdated")
    launcher = _read(p.launcher)
    files[str(p.launcher)] = "missing" if launcher is None else (
        "current" if launcher == launcher_text(p) else ("outdated" if LAUNCHER_MARK in launcher else "foreign"))
    return {"skill": skill_status(p, jobs), "files": files,
            "jobs": {n: {k: j.get(k) for k in ("id", "state", "enabled", "skills", "script", "last_status",
                                                "last_run_at", "last_error", "next_run_at")} for n, j in jobs.items()},
            "missing_jobs": [s["name"] for s in JOBS.values() if s["name"] not in jobs]}


# --------------------------------------------------------------------------- CLI

def add_parser(sub: Any) -> None:
    s = sub.add_parser("hermes", help="Hermes integration: status / install / resume / pause")
    hs = s.add_subparsers(dest="action", required=True)
    hs.add_parser("status")
    x = hs.add_parser("install", help="show the plan, then install (skill only if absent; jobs created paused)")
    x.add_argument("--dry-run", action="store_true")
    x.add_argument("--yes", action="store_true", help="apply without the interactive confirmation")
    for name in ("resume", "pause"):
        x = hs.add_parser(name, help=f"{name} both todo-manager cron jobs")
        x.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_hermes)


def _confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def cmd_hermes(args: argparse.Namespace) -> int:
    cfg = config.load(args.home)
    if args.action == "status":
        print(json.dumps(status(cfg), ensure_ascii=False, indent=1))
        return 0
    inst = Installer(cfg, Paths.from_config(cfg))
    if args.action in ("resume", "pause"):
        if not args.yes and not _confirm(f"{args.action} the todo-manager cron jobs now?"):
            print("not confirmed; nothing changed (pass --yes to skip the prompt)")
            return 2
        print("\n".join(inst.set_jobs_state(args.action)))
        return 0
    actions = inst.plan()
    for a in actions:
        print(f"[{a.kind}] {a.target}: {a.summary}")
        if a.diff and (args.dry_run or a.kind != "create"):
            print(a.diff.rstrip())
    if args.dry_run:
        return 0
    if not any(a.run and a.kind not in ("noop", "conflict", "keep") for a in actions):
        inst.apply(actions)
        print("nothing to change")
        return 0
    if not args.yes and not _confirm("Apply these changes?"):
        print("not confirmed; nothing changed (pass --yes to skip the prompt)")
        return 2
    for line in inst.apply(actions):
        print(f"done: {line}")
    return 0
