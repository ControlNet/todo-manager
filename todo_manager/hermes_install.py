"""Install, upgrade and remove the Hermes integration: skill, cron jobs, gate scripts, launcher.

Ownership (recorded in ``TODO_HOME/state/hermes/install.json``):

* Framework files - the two pre-run gate scripts and the launcher - belong to todo-manager and
  are overwritten (a file at that place without our mark is left alone and reported).
* User files - the skill, the two cron prompts and the workspace ``AGENTS.md`` - start as
  templates and then belong to the user (and to Hermes acting for the user). An upgrade merges
  the new template into them, 3-way against the text installed last (``userfiles``); a conflict
  keeps the user's text and is reported (``install``, ``health``).
* Cron job settings: a field whose live value differs from what todo-manager installed
  (schedule, delivery, working directory, skills, model, provider, reasoning effort) is the
  user's and is kept; the others follow the new version. Hermes' job store is only read; every
  change goes through ``hermes cron create|edit|remove``.
* New cron jobs are created paused, delivering locally, with ``TODO_HOME`` as their working
  directory (Hermes loads its ``AGENTS.md`` as the jobs' context). Resuming is a separate step.
* ``uninstall`` removes only what todo-manager installed: the cron jobs, gate scripts and
  launcher, and the skill when it is unchanged. The workspace data stays; Hermes profile files
  (``USER.md``, ``MEMORY.md``, ``SOUL.md``) are never touched.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Callable

from . import clock, config, prefs, templates, userfiles

SKILL_NAME = "todo-manager"
SKILL_CATEGORY = "productivity"
LAUNCHER_MARK = "# todo-manager launcher (generated; overwritten by `todo-manager hermes install`)"
SCRIPT_MARK = "# todo-manager pre-run gate (generated; overwritten by `todo-manager hermes install`)"
JOBS = {
    "intake": {"name": "todo-manager-intake", "schedule": "*/5 * * * *"},
    "feedback": {"name": "todo-manager-feedback", "schedule": "2-59/5 * * * *"},
}
Runner = Callable[..., subprocess.CompletedProcess]


SKILL_FILE = "skill/SKILL.md"           # its name in the baseline store
# Cron job fields todo-manager sets; a live value that differs from the installed one is the user's.
JOB_FIELDS = ("schedule", "deliver", "workdir", "skills", "model", "provider", "reasoning_effort")
FLAGS = {"schedule": "--schedule", "deliver": "--deliver", "workdir": "--workdir", "model": "--model",
         "provider": "--provider", "reasoning_effort": "--reasoning-effort"}


def asset(name: str) -> str:
    return resources.files("todo_manager.hermes_assets").joinpath(name).read_text(encoding="utf-8")


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
    def legacy_skill_baseline(self) -> Path:
        """Where installers before the ownership record kept the skill they installed."""
        return self.state_dir / "skill-baseline" / "SKILL.md"

    @property
    def manifest(self) -> Path:
        return self.state_dir / "install.json"

    @property
    def store(self) -> userfiles.Store:
        return userfiles.Store(self.todo_home)

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
    """Our two jobs as Hermes stores them (read only: changes go through ``hermes cron``)."""
    if not p.jobs_file.exists():
        return {}
    data = json.loads(p.jobs_file.read_text(encoding="utf-8"))
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    wanted = {spec["name"] for spec in JOBS.values()}
    return {j["name"]: j for j in jobs if j.get("name") in wanted}


def load_manifest(p: Paths) -> dict[str, Any]:
    try:
        data = json.loads(p.manifest.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("version") == 2 else {}


def live_skill(p: Paths, jobs: dict[str, dict[str, Any]]) -> Path | None:
    """The installed skill: by name, else one the jobs refer to (a curator may have renamed it)."""
    refs: set[str] = {SKILL_NAME}
    for j in jobs.values():
        refs.update(j.get("skills") or [])
    live = find_live_skills(p, refs)
    return live.get(SKILL_NAME) or next(iter(live.values()), None)


def skill_upgrade(p: Paths, jobs: dict[str, dict[str, Any]]) -> tuple[userfiles.Upgrade, Path | None]:
    path = live_skill(p, jobs)
    return userfiles.plan(SKILL_FILE, userfiles.read_text(path), p.store.baseline(SKILL_FILE, p.legacy_skill_baseline),
                          asset(SKILL_FILE), templates.earlier(SKILL_FILE)), path


def prompt_upgrade(p: Paths, entry: str, job: dict[str, Any] | None) -> userfiles.Upgrade:
    name = f"cron/{entry}.md"
    live = (job.get("prompt") or "").strip() if job is not None else None
    baseline = p.store.baseline(name)
    return userfiles.plan(name, live, baseline.strip() if baseline is not None else None, templates.cron_prompt(entry),
                          templates.earlier_cron_prompts(entry))


def live_fields(job: dict[str, Any]) -> dict[str, Any]:
    return {"schedule": (job.get("schedule") or {}).get("expr"), "deliver": job.get("deliver") or None,
            "workdir": job.get("workdir") or None, "skills": list(job.get("skills") or []),
            "model": job.get("model") or None, "provider": job.get("provider") or None,
            "reasoning_effort": job.get("reasoning_effort") or None}


# --------------------------------------------------------------------------- plan

@dataclass
class Action:
    kind: str           # create | update | remove | noop | keep | conflict
    target: str
    summary: str
    diff: str = ""
    run: Callable[[], Any] | None = None
    record: dict[str, Any] = field(default_factory=dict)    # what the manifest keeps when applied
    kept: tuple[str, ...] = ()      # job settings the user changed in Hermes, left as they are

    @property
    def changes(self) -> bool:
        """Changes something outside todo-manager's own state (needs the user's confirmation)."""
        return self.run is not None and self.kind in ("create", "update", "remove")


@dataclass
class Installer:
    cfg: config.Config
    paths: Paths
    runner: Runner = subprocess.run
    log: list[str] = field(default_factory=list)

    def hermes(self, *args: str) -> str:
        cmd = [self.cfg.get("hermes.bin", "hermes"), *args]
        proc = self.runner(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd[:3])} failed (exit {proc.returncode}): {(proc.stdout or '')[-300:]}")
        return proc.stdout

    # ---------------------------------------------------------------- framework files

    def _write(self, path: Path, text: str, mode: int = 0o644) -> None:
        userfiles.write_text(path, text, mode)

    def file_action(self, path: Path, text: str, mark: str, mode: int = 0o644) -> Action:
        current = _read(path)
        if current == text:
            return Action("noop", str(path), "up to date", record={"file": str(path)})
        if current is not None and mark not in current:
            return Action("conflict", str(path), "exists and was not written by todo-manager; left untouched")
        verb = "update" if current is not None else "create"
        return Action(verb, str(path), f"{verb} generated file", _diff(current, text, path.name),
                      lambda: self._write(path, text, mode), record={"file": str(path)})

    # ---------------------------------------------------------------- user files

    def user_file_action(self, up: userfiles.Upgrade, target: str, write: Callable[[str], None] | None,
                         shipped: str, live: str | None = None) -> Action:
        """``write``: puts the merged text in place (None: the target cannot be written now);
        ``live``: the text in use now (the diff shows what the upgrade changes in it)."""
        store = self.paths.store
        record = {"user_file": up.name, "target": target, "state": up.state}

        def keep_records() -> None:
            store.record(up, shipped)

        if up.writes and write is not None:
            def run() -> None:
                write(up.text or "")
                store.record(up, shipped)
            kind = "create" if up.state == "created" else "update"
            return Action(kind, target, f"{up.name}: {up.summary()}", _diff(live, up.text or "", up.name), run, record)
        kind = "conflict" if up.state in ("conflict", "unknown-base") else "keep" if up.state == "kept" else "noop"
        summary = f"{up.name}: {up.summary()}"
        if kind == "conflict":
            summary += f" ({store.conflict_note(up.name)})"
        return Action(kind, target, summary, run=keep_records, record=record)

    def skill_action(self, jobs: dict[str, dict[str, Any]]) -> Action:
        up, path = skill_upgrade(self.paths, jobs)
        target = path or self.paths.install_skill_path
        return self.user_file_action(up, str(target), lambda text: self._write(target, text), asset(SKILL_FILE),
                                     userfiles.read_text(path))

    def agents_action(self) -> Action:
        language = self.cfg.language
        up = prefs.plan_upgrade(self.paths.todo_home, language)
        path = prefs.agents_path(self.paths.todo_home)
        return self.user_file_action(up, str(path), lambda text: userfiles.write_text(path, text),
                                     templates.workspace_agents(language), userfiles.read_text(path))

    # ---------------------------------------------------------------- cron jobs

    def wanted_fields(self) -> dict[str, dict[str, Any]]:
        out = {}
        for entry, spec in JOBS.items():
            out[entry] = {"schedule": spec["schedule"], "deliver": "local", "workdir": str(self.paths.todo_home),
                          "skills": [SKILL_NAME], "model": self.cfg.get("hermes.model") or None,
                          "provider": self.cfg.get("hermes.provider") or None,
                          "reasoning_effort": self.cfg.get("hermes.reasoning_effort") or None}
        return out

    def earlier_fields(self, entry: str) -> dict[str, Any]:
        """What installers before the ownership record set: the same, without a working directory."""
        return {**self.wanted_fields()[entry], "workdir": None}

    def job_action(self, entry: str, jobs: dict[str, dict[str, Any]], manifest: dict[str, Any]) -> list[Action]:
        spec = JOBS[entry]
        existing = jobs.get(spec["name"])
        want = self.wanted_fields()[entry]
        up = prompt_upgrade(self.paths, entry, existing)
        shipped = templates.cron_prompt(entry)
        if existing is None:
            cmd = ["cron", "create", "--paused", "--paused-reason", "installed by todo-manager; resume after review",
                   "--name", spec["name"], "--script", self.paths.script(entry).name,
                   "--interpreter", self.paths.python, "--deliver", want["deliver"], "--workdir", want["workdir"],
                   "--skill", SKILL_NAME]
            for key in ("model", "provider", "reasoning_effort"):
                if want[key]:
                    cmd += [FLAGS[key], str(want[key])]
            cmd += [want["schedule"], shipped]
            fresh = userfiles.Upgrade(up.name, "created", shipped)

            def create() -> None:
                self.hermes(*cmd)
                self.paths.store.record(fresh, shipped)
            return [Action("create", spec["name"], f"create cron job (paused), schedule {want['schedule']}, working"
                                                   f" directory {want['workdir']}", run=create,
                           record={"job": entry, "installed": want, "user_file": up.name,
                                   "target": f"{spec['name']} prompt", "state": "created"})]
        installed = ((manifest.get("jobs") or {}).get(entry) or {}).get("installed") or self.earlier_fields(entry)
        live = live_fields(existing)
        changes: dict[str, Any] = {}
        kept: list[str] = []
        record: dict[str, Any] = {}
        for key in JOB_FIELDS:
            if live[key] == want[key]:
                record[key] = want[key]
            elif live[key] != installed.get(key):
                kept.append(key)                       # changed in Hermes: the user's
                record[key] = installed.get(key)
            else:
                changes[key] = want[key]
                record[key] = want[key]
        framework = []
        if existing.get("script") != self.paths.script(entry).name:
            framework += ["--script", self.paths.script(entry).name]
        if existing.get("interpreter") != self.paths.python:
            framework += ["--interpreter", self.paths.python]
        cmd = ["cron", "edit", existing["id"], *framework]
        for key, value in changes.items():
            if key == "skills":
                cmd += [a for s in value for a in ("--skill", s)]
            else:
                cmd += [FLAGS[key], "" if value is None else str(value)]
        prompt_action = self.user_file_action(up, f"{spec['name']} prompt",
                                              lambda text: self.hermes(*cmd, "--prompt", text), shipped,
                                              (existing.get("prompt") or "").strip())
        state = "paused" if existing.get("state") == "paused" or not existing.get("enabled", True) else "enabled"
        note = f" (kept as you set them: {', '.join(kept)})" if kept else ""
        rec = {"job": entry, "installed": record}
        if prompt_action.kind in ("create", "update"):      # one edit carries the settings and the prompt
            names = [*changes, *(["script/interpreter"] if framework else []), "prompt"]
            prompt_action.summary = (f"update cron job {existing['id']} ({state}): {', '.join(names)}{note};"
                                     f" {prompt_action.summary}")
            prompt_action.record = {**prompt_action.record, **rec}
            prompt_action.kind, prompt_action.kept = "update", tuple(kept)
            return [prompt_action]
        if changes or framework:
            names = [*changes, *(["script/interpreter"] if framework else [])]
            action = Action("update", spec["name"], f"update cron job {existing['id']} ({state}): {', '.join(names)}{note}",
                            run=lambda: self.hermes(*cmd), record=rec, kept=tuple(kept))
        else:
            action = Action("noop", spec["name"], f"cron job {existing['id']} ({state}) up to date{note}", record=rec,
                            kept=tuple(kept))
        return [action, prompt_action]

    # ---------------------------------------------------------------- plan / apply

    def plan(self) -> list[Action]:
        jobs = load_jobs(self.paths)
        manifest = load_manifest(self.paths)
        actions = [self.file_action(self.paths.launcher, launcher_text(self.paths), LAUNCHER_MARK, 0o755)]
        for entry in JOBS:
            actions.append(self.file_action(self.paths.script(entry), script_text(self.paths, entry), SCRIPT_MARK))
        actions.append(self.skill_action(jobs))
        actions.append(self.agents_action())
        for entry in JOBS:
            actions += self.job_action(entry, jobs, manifest)
        return actions

    def apply(self, actions: list[Action]) -> list[str]:
        """Carry out the plan (the caller confirmed the actions that ``changes``) and record what
        todo-manager installed."""
        done = []
        previous = load_manifest(self.paths)
        record: dict[str, Any] = {"version": 2, "installed_at": clock.iso(), "todo_home": str(self.paths.todo_home),
                                  "source_root": str(self.paths.source_root), "python": self.paths.python,
                                  "framework_files": [], "user_files": {}, "jobs": dict(previous.get("jobs") or {})}
        for a in actions:
            if a.run is not None:
                a.run()
                if a.changes:
                    done.append(f"{a.kind} {a.target}")
            if "file" in a.record:
                record["framework_files"].append(a.record["file"])
            if "user_file" in a.record:
                record["user_files"][a.record["user_file"]] = {"target": a.record["target"], "state": a.record["state"]}
            if "job" in a.record:
                record["jobs"][a.record["job"]] = {"installed": a.record["installed"]}
        jobs = load_jobs(self.paths)
        for entry, spec in JOBS.items():
            if entry in record["jobs"] and spec["name"] in jobs:
                record["jobs"][entry].update(name=spec["name"], id=jobs[spec["name"]]["id"])
        self._write(self.paths.manifest, json.dumps(record, ensure_ascii=False, indent=1))
        return done

    def set_jobs_state(self, state: str) -> list[str]:
        out = []
        for name, job in load_jobs(self.paths).items():
            self.hermes("cron", "resume" if state == "resume" else "pause", job["id"])
            out.append(f"{state} {name} ({job['id']})")
        return out

    # ---------------------------------------------------------------- uninstall

    def uninstall_plan(self) -> list[Action]:
        """What ``uninstall`` removes: only what todo-manager installed and the user did not take over."""
        jobs = load_jobs(self.paths)
        actions = []
        for name, job in sorted(jobs.items()):
            actions.append(Action("remove", name, f"remove cron job {job['id']}",
                                  run=lambda jid=job["id"]: self.hermes("cron", "remove", jid)))
        for path, mark in [(self.paths.script(e), SCRIPT_MARK) for e in JOBS] + [(self.paths.launcher, LAUNCHER_MARK)]:
            text = _read(path)
            if text is None:
                continue
            if mark in text:
                actions.append(Action("remove", str(path), "remove generated file", run=path.unlink))
            else:
                actions.append(Action("keep", str(path), "not written by todo-manager; left in place"))
        up, path = skill_upgrade(self.paths, jobs)
        if path is not None:
            installed = self.paths.store.baseline(SKILL_FILE, self.paths.legacy_skill_baseline)
            if installed is not None and _read(path) == installed:
                def remove_skill(p: Path = path) -> None:
                    p.unlink()
                    if p.parent.is_dir() and not any(p.parent.iterdir()):
                        p.parent.rmdir()
                actions.append(Action("remove", str(path), "remove the skill (unchanged since install)", run=remove_skill))
            else:
                actions.append(Action("keep", str(path), "the skill was changed after install (by you or Hermes):"
                                                         " kept; remove it in Hermes if you no longer want it"))
        return actions

    def uninstall(self, actions: list[Action]) -> list[str]:
        done = []
        for a in actions:
            if a.changes:
                a.run()
                done.append(f"{a.kind} {a.target}")
        record = load_manifest(self.paths) or {"version": 2}
        record.update(uninstalled_at=clock.iso(), jobs={}, framework_files=[])
        self._write(self.paths.manifest, json.dumps(record, ensure_ascii=False, indent=1))
        return done


def kept_after_uninstall(cfg: config.Config, active_delegations: int) -> list[str]:
    out = [f"kept: the workspace {cfg.home} (database, config.toml, AGENTS.md, rules-archive.md, exports, backups),"
           " the Notion pages and the Hermes board",
           "not touched: Hermes' USER.md, MEMORY.md, SOUL.md and other skills",
           "to take the data elsewhere: `todo-manager export`; to delete it, remove the workspace directory yourself"]
    if active_delegations:
        out.append(f"{active_delegations} delegation(s) are still open on the board and Hermes keeps working on them:"
                   " close them first (`todo-manager delegate cancel <id>`) or archive them in Hermes")
    return out


def context_problems(cfg: config.Config) -> list[str]:
    """Installed jobs that run without the workspace conventions (their working directory is not
    ``TODO_HOME``, so Hermes does not load its ``AGENTS.md``)."""
    p = Paths.from_config(cfg)
    if not load_manifest(p):
        return []
    try:
        jobs = load_jobs(p)
    except (OSError, ValueError) as exc:
        return [f"cannot read {p.jobs_file}: {exc}"]
    out = []
    for name, job in sorted(jobs.items()):
        workdir = job.get("workdir")
        if not workdir or Path(workdir).expanduser().resolve() != p.todo_home.resolve():
            out.append(f"{name} runs in {workdir or 'no working directory'}, so Hermes does not give it"
                       f" {prefs.agents_path(p.todo_home)} (`hermes cron edit {job['id']} --workdir {p.todo_home}`)")
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
    skill, path = skill_upgrade(p, jobs)
    user_files = {SKILL_FILE: {"path": str(path) if path else None, "state": skill.state},
                  prefs.USERFILE: {"path": str(prefs.agents_path(p.todo_home)),
                                   "state": prefs.plan_upgrade(p.todo_home, cfg.language).state}}
    for entry, spec in JOBS.items():
        user_files[f"cron/{entry}.md"] = {"path": spec["name"], "state": prompt_upgrade(p, entry, jobs.get(spec["name"])).state}
    return {"user_files": user_files, "conflicts": p.store.conflicts(), "files": files,
            "jobs": {n: {k: j.get(k) for k in ("id", "state", "enabled", "skills", "script", "workdir", "last_status",
                                                "last_run_at", "last_error", "next_run_at")} for n, j in jobs.items()},
            "missing_jobs": [s["name"] for s in JOBS.values() if s["name"] not in jobs]}


# --------------------------------------------------------------------------- CLI

def add_parser(sub: Any) -> None:
    s = sub.add_parser("hermes", help="Hermes integration: status / install / resume / pause")
    hs = s.add_subparsers(dest="action", required=True)
    hs.add_parser("status")
    x = hs.add_parser("install", help="show the plan, then install or upgrade (your changes to the skill, prompts,"
                                      " AGENTS.md and job settings are kept; new jobs are created paused)")
    x.add_argument("--dry-run", action="store_true")
    x.add_argument("--yes", action="store_true", help="apply without the interactive confirmation")
    for name in ("resume", "pause"):
        x = hs.add_parser(name, help=f"{name} both todo-manager cron jobs")
        x.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_hermes)
    u = sub.add_parser("uninstall", help="remove the cron jobs, gate scripts, launcher and the unchanged skill;"
                                         " all data stays")
    u.add_argument("--dry-run", action="store_true")
    u.add_argument("--yes", action="store_true", help="remove without the interactive confirmation")
    u.set_defaults(fn=cmd_uninstall)


def _confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def _print_plan(actions: list[Action], dry_run: bool) -> None:
    for a in actions:
        print(f"[{a.kind}] {a.target}: {a.summary}")
        if a.diff and (dry_run or a.kind != "create"):        # a new file's diff is the whole file
            print(a.diff.rstrip())


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
    _print_plan(actions, dry_run=args.dry_run)
    if args.dry_run:
        return 0
    if not any(a.changes for a in actions):
        inst.apply(actions)
        print("nothing to change")
        return 0
    if not args.yes and not _confirm("Apply these changes?"):
        print("not confirmed; nothing changed (pass --yes to skip the prompt)")
        return 2
    for line in inst.apply(actions):
        print(f"done: {line}")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    from . import db, delegation
    cfg = config.load(args.home)
    inst = Installer(cfg, Paths.from_config(cfg))
    actions = inst.uninstall_plan()
    _print_plan(actions, dry_run=False)
    active = 0
    if cfg.db_path.exists():
        conn = db.connect(cfg.db_path)
        try:
            active = conn.execute(f"SELECT COUNT(*) FROM delegations WHERE status IN"
                                  f" ({','.join('?' * len(delegation.ACTIVE))})", delegation.ACTIVE).fetchone()[0]
        finally:
            conn.close()
    notes = kept_after_uninstall(cfg, active)
    print("\n".join(notes))
    if args.dry_run:
        return 0
    if not any(a.changes for a in actions):
        print("nothing of todo-manager is installed in Hermes")
        return 0
    if not args.yes and not _confirm("Remove these from Hermes?"):
        print("not confirmed; nothing changed (pass --yes to skip the prompt)")
        return 2
    for line in inst.uninstall(actions):
        print(f"done: {line}")
    return 0
