"""CLI and export/backup/restore (the Hermes installer: ``test_hermes_install``)."""

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
        self.assertTrue({"status", "waiting_on"} <= set(json.loads(out)["applied"]))
        _, out = self.tm("task", "show", tid)
        self.assertEqual(json.loads(out)["status"], "waiting")
        code, out = self.tm("task", "update", tid, "--set", "bogus=1")
        self.assertEqual(code, 2)
        code, out = self.tm("history", tid)
        self.assertIn("v2", out)

    def test_gate_context_and_feedback_commands(self):
        code, out = self.tm("gate", "feedback")
        self.assertEqual(out.strip(), '{"wakeAgent": false}')
        _, out = self.tm("task", "create", "--title", "回复 Carol")
        tid = json.loads(out)["task"]
        code, out = self.tm("feedback", "record", "--origin", "cli", "--request", "req-1005-a", "--said",
                            "这个周五前要做完", "--scope", f"task:{tid}")
        self.assertEqual(code, 0, out)
        code, out = self.tm("gate", "feedback")
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertNotIn("wakeAgent", lines[-1], "a context wakes the agent")
        self.assertIn("这个周五前要做完", out, "the context is in the output: no lookup needed")
        run_id = lines[0].rsplit(" ", 1)[1]
        fid = next(line.split()[1] for line in lines if line.startswith("### fbk_"))
        update = ("task", "update", tid, "--feedback", fid, "--set", "due_date=2026-10-09")
        code, out = self.tm(*update)
        self.assertEqual(code, 0, out)
        code, out = self.tm(*update)
        self.assertEqual((code, json.loads(out)["duplicate"]), (0, True), "a retry returns the first result")
        code, out = self.tm("feedback", "resolve", fid, "--outcome", "applied")
        self.assertEqual(code, 0, out)
        code, out = self.tm("run", "done", run_id)
        self.assertEqual(code, 0, out)
        code, out = self.tm("run", "show", run_id, "--context")
        self.assertIn("这个周五前要做完", out, "what the agent was shown is kept with the run")
        _, out = self.tm("task", "show", tid)
        task = json.loads(out)
        self.assertEqual((task["due_date"], task["version"]), ("2026-10-09", 2), "applied once")

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

    def test_a_backup_keeps_the_conventions_and_settings_and_a_restore_brings_them_back(self):
        agents = self.home / "AGENTS.md"
        agents.write_text(agents.read_text() + "\n- Friday afternoons stay free.\n")
        _, out = self.tm("backup")
        backup = Path(json.loads(out)["backup"])
        self.assertEqual((backup.with_name(backup.stem + ".files") / "AGENTS.md").read_text(), agents.read_text())
        kept = agents.read_text()
        agents.write_text("# Replaced by mistake\n")
        code, out = self.tm("restore", str(backup), "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(json.loads(out)["files_restored"]), ["AGENTS.md", "config.toml"])
        self.assertEqual(agents.read_text(), kept)
        safety = Path(json.loads(out)["safety_backup"])
        self.assertEqual((safety.with_name(safety.stem + ".files") / "AGENTS.md").read_text(), "# Replaced by mistake\n")

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
