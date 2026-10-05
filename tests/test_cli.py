"""CLI, export/backup and Hermes installer (temporary HERMES_HOME; ``hermes`` is a test double)."""

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from todo_manager import cli, clock, config, hermes_install
from tests.helpers import T0


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(list(argv))
    return code, out.getvalue()


class FakeHermes:
    """Test double for the ``hermes`` CLI: records calls and maintains a jobs.json."""

    def __init__(self, hermes_home: Path):
        self.calls = []
        self.jobs_file = hermes_home / "cron" / "jobs.json"
        self.jobs_file.parent.mkdir(parents=True, exist_ok=True)
        self.jobs_file.write_text(json.dumps({"jobs": []}))

    def __call__(self, cmd, **kw):
        self.calls.append(cmd[1:])
        data = json.loads(self.jobs_file.read_text())
        args = cmd[1:]
        if args[:2] == ["cron", "create"]:
            opts = dict(zip(args[2::2], args[3::2]))
            name = args[args.index("--name") + 1]
            skills = [args[i + 1] for i, a in enumerate(args) if a == "--skill"]
            data["jobs"].append({"id": f"job{len(data['jobs'])}", "name": name, "skills": skills,
                                 "script": args[args.index("--script") + 1], "state": "paused", "enabled": False,
                                 "prompt": args[-1], "schedule": {"expr": args[-2]}, "deliver": "local",
                                 "workdir": None})
            del opts
        self.jobs_file.write_text(json.dumps(data))
        return subprocess.CompletedProcess(cmd, 0, "ok", "")


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tm-cli-"))
        self.home = self.tmp / "workspace"
        self.hermes_home = self.tmp / "hermes"
        clock.set_now(T0)
        code, _ = run("--home", str(self.home), "init")
        self.assertEqual(code, 0)
        (self.home / "config.toml").write_text(
            f'timezone = "Pacific/Auckland"\n[hermes]\nhome = "{self.hermes_home}"\n'
            f'launcher = "{self.tmp / "bin" / "todo-manager"}"\n')

    def tearDown(self):
        clock.set_now(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tm(self, *argv):
        return run("--home", str(self.home), *argv)


class CliTests(CliCase):
    def test_home_inside_source_tree_is_refused(self):
        code, out = run("--home", str(config.SOURCE_ROOT / "state-here"), "health")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["error"], "CONFIG")
        self.assertFalse((config.SOURCE_ROOT / "state-here").exists())

    def test_task_lifecycle_and_history(self):
        code, out = self.tm("task", "create", "--title", "准备组会", "--set", "due_date=2026-10-08",
                            "--set", "next_step=做三页幻灯片")
        tid = json.loads(out)["task"]
        code, out = self.tm("task", "list")
        self.assertIn(tid, out)
        self.assertIn("due 2026-10-08", out)
        code, out = self.tm("task", "update", tid, "--set", "status=waiting", "--set", "waiting_on=导师确认时间")
        self.assertEqual(json.loads(out)["applied"]["status"], "waiting")
        code, out = self.tm("task", "update", tid, "--set", "bogus=1")
        self.assertEqual(code, 2)
        code, out = self.tm("history", tid)
        self.assertIn("v2", out)

    def test_gate_prints_only_the_wake_line_and_direct_feedback_flow(self):
        code, out = self.tm("gate", "feedback")
        self.assertEqual(out.strip(), '{"wakeAgent": false}')
        _, out = self.tm("task", "create", "--title", "回复 Carol")
        tid = json.loads(out)["task"]
        _, out = self.tm("feedback", "request-id")
        self.tm("feedback", "record", "--origin", "cli", "--text", "这个周五前要做完", "--scope", f"task:{tid}",
                "--source-id", json.loads(out)["request_id"])
        code, out = self.tm("gate", "feedback")
        lines = out.strip().splitlines()
        self.assertEqual(lines[-1], '{"wakeAgent": true}')
        self.assertNotIn("周五", out)
        run_id = lines[0].split(": ")[1]
        code, out = self.tm("feedback", "show", run_id)
        self.assertIn("这个周五前要做完", out)
        self.assertIn("Feedback decisions format", out)
        fid = next(line.split()[1] for line in out.splitlines() if line.startswith("### fbk_"))
        decisions = self.home / "runs" / run_id / "decisions.json"
        self.assertIn(str(decisions), out)
        decisions.write_text(json.dumps({"run_id": run_id, "rules_version": 0, "items": [
            {"feedback": fid, "outcome": "applied", "ops": [
                {"op": "update_task", "task": tid, "based_on_version": 1, "set": {"due_date": "2026-10-09"}}]}]}))
        code, out = self.tm("feedback", "apply", run_id, "--file", str(decisions))
        self.assertEqual(code, 0, out)
        _, out = self.tm("task", "show", tid)
        self.assertEqual(json.loads(out)["due_date"], "2026-10-09")
        code, out = self.tm("feedback", "apply", run_id, "--file", str(decisions))
        self.assertEqual((code, json.loads(out)["duplicate"]), (0, True), "same file again: the first report")
        _, out = self.tm("task", "show", tid)
        self.assertEqual(json.loads(out)["version"], 2, "applied once")

    def test_export_verify_backup_restore(self):
        _, out = self.tm("task", "create", "--title", "A")
        tid = json.loads(out)["task"]
        code, out = self.tm("export", "--verify")
        result = json.loads(out)
        self.assertTrue(result["rebuild"]["ok"], result)
        root = Path(result["export"])
        self.assertTrue((root / "tasks" / f"{tid}.md").exists())
        self.assertTrue(str(root).startswith(str(self.home)))
        _, out = self.tm("backup")
        backup = json.loads(out)["backup"]
        self.tm("task", "create", "--title", "B")
        code, out = self.tm("restore", backup)
        self.assertEqual(code, 2, "restore needs --yes")
        code, out = self.tm("restore", backup, "--yes")
        self.assertEqual(code, 0, out)
        _, out = self.tm("task", "list")
        self.assertIn(" A", out)
        self.assertNotIn(" B", out)
        _, out = self.tm("health")
        self.assertIn("delegate release-hold", out, "a restore holds delegations until the user checks the board")
        code, out = self.tm("delegate", "release-hold")
        self.assertEqual((code, json.loads(out)["released"]["reason"]), (0, "restore"))

    def test_export_imports_into_a_new_directory(self):
        _, out = self.tm("task", "create", "--title", "Moving task")
        tid = json.loads(out)["task"]
        _, out = self.tm("export")
        bundle = json.loads(out)["export"]
        new_home = self.home.parent / "moved-home"
        code, out = run("--home", str(new_home), "import", bundle, "--into", str(new_home))
        self.assertEqual(code, 0, out)
        self.assertTrue(json.loads(out)["delegations_held"])
        code, out = run("--home", str(new_home), "task", "show", tid)
        self.assertEqual(json.loads(out)["title"], "Moving task")
        code, out = run("--home", str(new_home), "import", bundle, "--into", str(new_home))
        self.assertEqual(code, 2, "an existing workspace is never overwritten")


class HermesInstallTests(CliCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeHermes(self.hermes_home)
        self.cfg = config.load(self.home)
        self.paths = hermes_install.Paths.from_config(self.cfg)

    def installer(self):
        return hermes_install.Installer(self.cfg, self.paths, runner=self.fake)

    def test_fresh_install_creates_paused_jobs_scripts_skill_and_launcher(self):
        inst = self.installer()
        inst.apply(inst.plan())
        self.assertTrue(self.paths.install_skill_path.exists())
        self.assertEqual(self.paths.baseline.read_text(), self.paths.install_skill_path.read_text())
        for entry in ("intake", "feedback"):
            text = self.paths.script(entry).read_text()
            self.assertIn(f'main(["gate", {entry!r}])', text)
            self.assertIn(str(self.home), text)
        self.assertTrue(self.paths.launcher.stat().st_mode & 0o100)
        creates = [c for c in self.fake.calls if c[:2] == ["cron", "create"]]
        self.assertEqual(len(creates), 2)
        for c in creates:
            self.assertIn("--paused", c)
            self.assertIn("--skill", c)
            self.assertNotIn("--workdir", c)
            self.assertEqual(c[c.index("--deliver") + 1], "local")
        self.assertEqual({c[-2] for c in creates}, {"*/5 * * * *", "2-59/5 * * * *"})
        # Re-running changes nothing on disk and only re-applies job definitions.
        actions = self.installer().plan()
        self.assertEqual({a.kind for a in actions if a.target.endswith((".py", "todo-manager", "SKILL.md"))}, {"noop"})

    def test_existing_skill_is_never_overwritten(self):
        inst = self.installer()
        inst.apply(inst.plan())
        evolved = self.paths.install_skill_path.read_text() + "\n## Learned\n- prefer short replies\n"
        self.paths.install_skill_path.write_text(evolved)
        status = hermes_install.status(self.cfg)
        self.assertEqual(status["skill"]["state"], "evolved-in-hermes")
        self.installer().apply(self.installer().plan())
        self.assertEqual(self.paths.install_skill_path.read_text(), evolved)
        # A shipped update while Hermes also evolved it: proposal only.
        self.paths.baseline.write_text("old baseline\n")
        inst = self.installer()
        actions = inst.plan()
        skill = next(a for a in actions if "skill" in a.summary)
        self.assertEqual(skill.kind, "propose")
        inst.apply(actions)
        self.assertEqual(self.paths.install_skill_path.read_text(), evolved)
        self.assertTrue((self.paths.proposal_dir / "shipped-vs-live.diff").exists())

    def test_existing_jobs_are_edited_in_place_keeping_state_and_skill_refs(self):
        inst = self.installer()
        inst.apply(inst.plan())
        data = json.loads(self.fake.jobs_file.read_text())
        for j in data["jobs"]:
            j["skills"] = ["productivity-umbrella"]   # curator consolidated the skill
            j["prompt"] = "old prompt"
        self.fake.jobs_file.write_text(json.dumps(data))
        self.fake.calls.clear()
        inst = self.installer()
        inst.apply(inst.plan())
        edits = [c for c in self.fake.calls if c[:2] == ["cron", "edit"]]
        self.assertEqual(len(edits), 2)
        for c in edits:
            self.assertNotIn("--skill", c, "a curator-rewritten skill reference is kept")
            self.assertNotIn("resume", c)
        self.assertFalse(any(c[:2] == ["cron", "create"] for c in self.fake.calls))

    def test_delivery_is_left_to_the_hermes_job(self):
        inst = self.installer()
        inst.apply(inst.plan())
        data = json.loads(self.fake.jobs_file.read_text())
        for j in data["jobs"]:
            j["deliver"] = "matrix"                  # set in Hermes: deliver run results to the home channel
        self.fake.jobs_file.write_text(json.dumps(data))
        self.fake.calls.clear()
        inst = self.installer()
        actions = inst.plan()
        inst.apply(actions)
        edits = [c for c in self.fake.calls if c[:2] == ["cron", "edit"]]
        self.assertEqual(len(edits), 2)
        for c in edits:
            self.assertNotIn("--deliver", c)
            self.assertNotIn("--failure-deliver", c)
        self.assertFalse([a for a in actions if "deliver" in a.summary])

    def test_foreign_launcher_is_left_alone(self):
        self.paths.launcher.parent.mkdir(parents=True, exist_ok=True)
        self.paths.launcher.write_text("#!/bin/sh\necho someone else\n")
        actions = self.installer().plan()
        launcher = next(a for a in actions if a.target == str(self.paths.launcher))
        self.assertEqual(launcher.kind, "conflict")
