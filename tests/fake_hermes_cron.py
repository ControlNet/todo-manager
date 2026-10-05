"""TEST DOUBLE for the ``hermes cron`` commands todo-manager runs, applied to a ``jobs.json`` the
way Hermes 0.21 applies them (``hermes_cli/cron.py``, ``cron/jobs.py``):

* ``create [flags] <schedule> <prompt>``: ``--paused`` creates the job paused; a working directory
  must be an existing absolute directory; ``reasoning_effort``/``interpreter`` are stored only
  when given.
* ``edit <id> [flags]``: only the given fields change; ``--skill`` (repeatable) replaces the
  list; an empty value clears ``workdir``/``model``/``provider``/``reasoning_effort``/``script``.
* ``remove <id>``, ``pause <id>``, ``resume <id>``.

An unknown job or flag exits 1 like the real command. Also runnable as a ``hermes`` executable
(``write_executable``) for tests that go through the command line.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

VALUE_FLAGS = {"--name": "name", "--deliver": "deliver", "--script": "script", "--workdir": "workdir",
               "--model": "model", "--provider": "provider", "--reasoning-effort": "reasoning_effort",
               "--interpreter": "interpreter", "--paused-reason": "paused_reason", "--prompt": "prompt",
               "--schedule": "schedule"}
OPTIONAL = ("reasoning_effort", "interpreter")


class CronError(Exception):
    pass


def _parse(rest: list[str]) -> tuple[dict, list[str] | None, list[str], set[str]]:
    opts: dict = {}
    skills: list[str] | None = None
    positional: list[str] = []
    flags: set[str] = set()
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("--paused", "--clear-skills"):
            flags.add(a)
            i += 1
        elif a == "--skill":
            skills = (skills or []) + [rest[i + 1]]
            i += 2
        elif a in VALUE_FLAGS:
            opts[VALUE_FLAGS[a]] = rest[i + 1]
            i += 2
        elif a.startswith("--"):
            raise CronError(f"unrecognized arguments: {a}")
        else:
            positional.append(a)
            i += 1
    return opts, skills, positional, flags


def _workdir(value: str | None) -> str | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise CronError(f"Cron workdir does not exist: {value}")
    return str(path)


class FakeCron:
    def __init__(self, jobs_file: Path):
        self.jobs_file = Path(jobs_file)
        self.jobs_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.jobs_file.exists():
            self.jobs_file.write_text(json.dumps({"jobs": []}))

    def jobs(self) -> list[dict]:
        return json.loads(self.jobs_file.read_text())["jobs"]

    def job(self, name: str) -> dict | None:
        return next((j for j in self.jobs() if j["name"] == name), None)

    def _save(self, jobs: list[dict]) -> None:
        self.jobs_file.write_text(json.dumps({"jobs": jobs}))

    def __call__(self, args: list[str]) -> tuple[int, str]:
        """``args`` after ``hermes``; returns (exit code, output)."""
        try:
            return 0, self._run(args)
        except CronError as exc:
            return 1, str(exc)

    def _run(self, args: list[str]) -> str:
        if args[:1] != ["cron"] or len(args) < 2:
            raise CronError(f"not a cron command: {args}")
        action, rest = args[1], args[2:]
        jobs = self.jobs()
        if action == "create":
            opts, skills, positional, flags = _parse(rest)
            if len(positional) != 2:
                raise CronError(f"create expects <schedule> <prompt>, got {positional}")
            paused = "--paused" in flags
            ids = {j["id"] for j in jobs}
            n = len(jobs)
            while f"job{n}" in ids:
                n += 1
            job = {"id": f"job{n}", "name": opts.get("name") or positional[1][:50], "prompt": positional[1].strip(),
                   "skills": skills or [], "skill": (skills or [None])[0], "model": opts.get("model") or None,
                   "provider": opts.get("provider") or None, "script": opts.get("script") or None,
                   "schedule": {"kind": "cron", "expr": positional[0]}, "enabled": not paused,
                   "state": "paused" if paused else "scheduled", "deliver": opts.get("deliver") or "local",
                   "workdir": _workdir(opts.get("workdir"))}
            for key in OPTIONAL:
                if opts.get(key):
                    job[key] = opts[key]
            self._save(jobs + [job])
            return f"Created job: {job['id']}"
        if not rest:
            raise CronError(f"{action} expects a job id")
        job = next((j for j in jobs if j["id"] == rest[0]), None)
        if job is None:
            raise CronError(f"Job not found: {rest[0]}")
        if action == "edit":
            opts, skills, positional, flags = _parse(rest[1:])
            if positional:
                raise CronError(f"unrecognized arguments: {positional}")
            for key, value in opts.items():
                if key == "schedule":
                    job["schedule"] = {"kind": "cron", "expr": value}
                elif key == "prompt":
                    job["prompt"] = value.strip()
                elif key == "workdir":
                    job["workdir"] = _workdir(value)
                elif key in ("model", "provider", "script", "reasoning_effort", "interpreter", "deliver", "name"):
                    job[key] = value or None
            if "--clear-skills" in flags:
                skills = []
            if skills is not None:
                job["skills"], job["skill"] = skills, (skills or [None])[0]
        elif action == "remove":
            jobs = [j for j in jobs if j["id"] != rest[0]]
        elif action in ("pause", "resume"):
            job["state"], job["enabled"] = ("paused", False) if action == "pause" else ("scheduled", True)
        else:
            raise CronError(f"unknown cron action {action}")
        self._save(jobs)
        return f"{action}: {rest[0]}"


def main(jobs_file: str, argv: list[str]) -> int:
    code, out = FakeCron(Path(jobs_file))(argv)
    print(out)
    return code


def write_executable(path: Path, jobs_file: Path) -> Path:
    """A ``hermes`` executable (TEST DOUBLE) that runs the cron commands on ``jobs_file``."""
    repo = Path(__file__).resolve().parent.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\nimport sys\nsys.dont_write_bytecode = True\n"
                    f"sys.path.insert(0, {str(repo)!r})\nfrom tests.fake_hermes_cron import main\n"
                    f"sys.exit(main({str(jobs_file)!r}, sys.argv[1:]))\n")
    path.chmod(0o755)
    return path
