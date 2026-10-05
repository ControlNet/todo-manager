"""Hermes install / upgrade / uninstall.

What todo-manager installed and what the user changed afterwards (in Hermes, by hand or by
asking the agent) are told apart by the install record and the baselines:

* framework files (gate scripts, launcher) are ours and follow the version;
* the skill, the cron prompts and ``AGENTS.md`` are the user's: an upgrade merges 3-way, a
  conflict keeps the user's text;
* a cron job setting changed in Hermes is the user's and is kept;
* the jobs run with ``TODO_HOME`` as working directory, so Hermes loads ``AGENTS.md``;
* Hermes' job store is never written: every change is a ``hermes cron`` command;
* uninstall removes only what was installed and unchanged, keeps all data.

``hermes`` is a TEST DOUBLE (``tests.fake_hermes_cron``) in a temporary HERMES_HOME.
"""

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from todo_manager import cli, clock, config, db, health, hermes_install, prefs, store, templates
from tests.fake_hermes_cron import FakeCron, write_executable
from tests.helpers import T0


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(list(argv))
    return code, out.getvalue()


class Hermes:
    """TEST DOUBLE runner: the ``hermes cron`` commands on a jobs.json; records every call."""

    def __init__(self, hermes_home: Path):
        self.cron = FakeCron(hermes_home / "cron" / "jobs.json")
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd[1:]))
        code, out = self.cron(list(cmd[1:]))
        return subprocess.CompletedProcess(cmd, code, out, "")

    def edits(self):
        return [c for c in self.calls if c[:2] == ["cron", "edit"]]


def flag(cmd, name):
    return cmd[cmd.index(name) + 1] if name in cmd else None


class InstallCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tm-hermes-"))
        self.home = self.tmp / "workspace"
        self.hermes_home = self.tmp / "hermes"
        clock.set_now(T0)
        code, _ = run("--home", str(self.home), "init", "--language", "zh-CN")
        self.assertEqual(code, 0)
        (self.home / "config.toml").write_text(
            f'language = "zh-CN"\ntimezone = "Pacific/Auckland"\n[hermes]\nhome = "{self.hermes_home}"\n'
            f'launcher = "{self.tmp / "bin" / "todo-manager"}"\n')
        self.hermes = Hermes(self.hermes_home)
        self.cfg = config.load(self.home)
        self.paths = hermes_install.Paths.from_config(self.cfg)

    def tearDown(self):
        clock.set_now(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def installer(self):
        self.cfg = config.load(self.home)
        return hermes_install.Installer(self.cfg, self.paths, runner=self.hermes)

    def install(self):
        inst = self.installer()
        actions = inst.plan()
        inst.apply(actions)
        return actions

    def job(self, entry):
        return self.hermes.cron.job(hermes_install.JOBS[entry]["name"])

    def user_edit(self, entry, *args):
        """The user changes the job in Hermes (``hermes cron edit``)."""
        code, out = self.hermes.cron(["cron", "edit", self.job(entry)["id"], *args])
        self.assertEqual(code, 0, out)

    def ship(self, name, old_text):
        """Make the installed baseline an earlier template, as if this version shipped a change."""
        self.paths.store.baseline_path(name).write_text(old_text)

    def skill(self):
        return self.paths.install_skill_path.read_text()


class FreshInstallTests(InstallCase):
    def test_jobs_are_created_paused_with_the_workspace_as_working_directory(self):
        self.install()
        creates = [c for c in self.hermes.calls if c[:2] == ["cron", "create"]]
        self.assertEqual(len(creates), 2)
        for c in creates:
            self.assertIn("--paused", c)
            self.assertEqual(flag(c, "--workdir"), str(self.home), "Hermes loads TODO_HOME/AGENTS.md as context")
            self.assertEqual(flag(c, "--deliver"), "local")
            self.assertEqual(flag(c, "--skill"), "todo-manager")
        for entry in ("intake", "feedback"):
            job = self.job(entry)
            self.assertEqual((job["state"], job["workdir"]), ("paused", str(self.home)))
            self.assertEqual(job["prompt"], templates.cron_prompt(entry))
            self.assertIn(f'main(["gate", {entry!r}])', self.paths.script(entry).read_text())
        self.assertTrue(self.paths.launcher.stat().st_mode & 0o100)
        self.assertEqual(self.skill(), hermes_install.asset("skill/SKILL.md"))
        manifest = json.loads(self.paths.manifest.read_text())
        self.assertEqual(manifest["version"], 2)
        self.assertEqual(manifest["jobs"]["intake"]["installed"]["workdir"], str(self.home))
        self.assertEqual(set(manifest["user_files"]),
                         {"skill/SKILL.md", "cron/intake.md", "cron/feedback.md", "workspace/AGENTS.md"})
        self.assertFalse((self.hermes_home / "cron" / "jobs.json.bak").exists())

    def test_a_second_run_changes_nothing(self):
        self.install()
        self.hermes.calls.clear()
        actions = self.install()
        self.assertEqual({a.kind for a in actions}, {"noop"}, [(a.kind, a.summary) for a in actions])
        self.assertEqual(self.hermes.calls, [])

    def test_a_new_workspace_starts_from_the_template_in_its_language(self):
        self.assertEqual(prefs.show(self.home), templates.workspace_agents("zh-CN"))
        self.assertEqual(self.paths.store.baseline(prefs.USERFILE), templates.workspace_agents("zh-CN"))
        text = prefs.show(self.home)
        for academic in ("CFP", "审稿", "review invitation", "论文"):
            self.assertNotIn(academic, text, "academic conventions are an example, not a default")

    def test_a_launcher_written_by_someone_else_is_left_alone(self):
        self.paths.launcher.parent.mkdir(parents=True, exist_ok=True)
        self.paths.launcher.write_text("#!/bin/sh\necho someone else\n")
        actions = self.install()
        launcher = next(a for a in actions if a.target == str(self.paths.launcher))
        self.assertEqual(launcher.kind, "conflict")
        self.assertIn("someone else", self.paths.launcher.read_text())


class UserFileUpgradeTests(InstallCase):
    def test_a_skill_improved_in_hermes_is_kept_and_the_new_template_merged_in(self):
        self.install()
        shipped = self.skill()
        learned = shipped + "\n## Learned\n\n- Prefer one-line replies in Matrix.\n"
        self.paths.install_skill_path.write_text(learned)
        self.assertEqual(hermes_install.status(self.cfg)["user_files"]["skill/SKILL.md"]["state"], "kept")
        self.install()
        self.assertEqual(self.skill(), learned, "nothing shipped changed: the user's skill stays as it is")
        older = shipped.replace("The user's personal task system.", "The user's task system.")
        self.assertNotEqual(older, shipped)
        self.ship("skill/SKILL.md", older)
        self.paths.install_skill_path.write_text(older + "\n## Learned\n\n- Prefer one-line replies in Matrix.\n")
        self.install()
        self.assertEqual(self.skill(), learned, "the template change merged into the user's skill")
        self.assertEqual(self.paths.store.baseline("skill/SKILL.md"), shipped)

    def test_a_skill_conflict_keeps_the_users_text_and_is_reported(self):
        self.install()
        shipped = self.skill()
        older = shipped.replace("The user's personal task system.", "The user's task system.")
        mine = shipped.replace("The user's personal task system.", "My own task system, my rules.")
        self.ship("skill/SKILL.md", older)
        self.paths.install_skill_path.write_text(mine)
        actions = self.install()
        skill = next(a for a in actions if a.record.get("user_file") == "skill/SKILL.md")
        self.assertEqual(skill.kind, "conflict")
        self.assertEqual(self.skill(), mine)
        self.assertIn("<<<<<<<", self.paths.store.conflict_path("skill/SKILL.md").read_text())
        self.assertIn("skill/SKILL.md", hermes_install.status(self.cfg)["conflicts"])
        self.assertTrue(any("skill/SKILL.md" in p for p in prefs.problems(self.home)), "health reports it")

    def test_a_prompt_edited_in_hermes_is_kept_and_a_new_template_merged(self):
        self.install()
        shipped = templates.cron_prompt("intake")
        mine = shipped + "\n\nAlways answer in two sentences."
        self.user_edit("intake", "--prompt", mine)
        self.hermes.calls.clear()
        self.install()
        self.assertEqual(self.hermes.edits(), [], "an edited prompt is the user's")
        self.assertEqual(self.job("intake")["prompt"], mine)
        first_line = shipped.splitlines()[0]
        older = shipped.replace(first_line, first_line + " (old wording)")
        self.ship("cron/intake.md", older)
        self.user_edit("intake", "--prompt", older + "\n\nAlways answer in two sentences.")
        self.install()
        [edit] = self.hermes.edits()
        self.assertEqual(flag(edit, "--prompt"), mine, "merged: the template change plus the user's line")
        self.assertEqual(self.job("intake")["prompt"], mine)

    def test_agents_md_edited_by_the_user_is_merged_never_replaced(self):
        self.install()
        shipped = prefs.show(self.home)
        mine = shipped + "\n## 我的约定\n\n- 周五下午不安排新任务。\n"
        prefs.agents_path(self.home).write_text(mine)
        older = shipped.replace(shipped.splitlines()[0], shipped.splitlines()[0] + "（旧版）")
        self.ship(prefs.USERFILE, older)
        prefs.agents_path(self.home).write_text(older + "\n## 我的约定\n\n- 周五下午不安排新任务。\n")
        self.install()
        self.assertEqual(prefs.show(self.home), mine)


class CronSettingTests(InstallCase):
    def test_settings_changed_in_hermes_are_kept_on_upgrade(self):
        self.install()
        self.user_edit("intake", "--schedule", "*/15 * * * *", "--deliver", "matrix", "--model", "fast-model",
                       "--skill", "productivity-umbrella")
        (self.home / "config.toml").write_text((self.home / "config.toml").read_text()
                                               + 'reasoning_effort = "low"\n')
        self.hermes.calls.clear()
        actions = self.install()
        [edit] = [c for c in self.hermes.edits() if c[2] == self.job("intake")["id"]]
        self.assertEqual(flag(edit, "--reasoning-effort"), "low", "a setting the user did not touch follows")
        for user_flag in ("--schedule", "--deliver", "--model", "--skill", "--prompt"):
            self.assertNotIn(user_flag, edit)
        job = self.job("intake")
        self.assertEqual((job["schedule"]["expr"], job["deliver"], job["model"], job["skills"]),
                         ("*/15 * * * *", "matrix", "fast-model", ["productivity-umbrella"]))
        update = next(a for a in actions if a.target == "todo-manager-intake")
        self.assertEqual(set(update.kept), {"schedule", "deliver", "model", "skills"})
        self.hermes.calls.clear()
        self.install()
        self.assertEqual(self.hermes.calls, [], "kept settings are not re-proposed")

    def test_a_setting_reset_to_the_default_by_the_user_is_followed_again(self):
        self.install()
        self.user_edit("feedback", "--schedule", "*/30 * * * *")
        self.install()
        self.user_edit("feedback", "--schedule", "2-59/5 * * * *")
        self.hermes.calls.clear()
        self.install()
        self.assertEqual(self.hermes.calls, [])
        self.assertEqual(json.loads(self.paths.manifest.read_text())["jobs"]["feedback"]["installed"]["schedule"],
                         "2-59/5 * * * *")

    def test_jobs_of_an_earlier_install_get_the_working_directory_and_keep_the_users_delivery(self):
        earlier = {e: templates.earlier_cron_prompts(e)[0] for e in hermes_install.JOBS}
        for entry, spec in hermes_install.JOBS.items():          # what the installer before this one made
            code, out = self.hermes.cron(["cron", "create", "--paused", "--name", spec["name"], "--script",
                                          self.paths.script(entry).name, "--interpreter", self.paths.python,
                                          "--deliver", "local", "--skill", "todo-manager", spec["schedule"],
                                          earlier[entry]])
            self.assertEqual(code, 0, out)
        self.user_edit("intake", "--deliver", "matrix")
        self.hermes.calls.clear()
        self.install()
        edits = {c[2]: c for c in self.hermes.edits()}
        self.assertEqual(len(edits), 2)
        intake = edits[self.job("intake")["id"]]
        self.assertEqual(flag(intake, "--workdir"), str(self.home))
        self.assertEqual(flag(intake, "--prompt"), templates.cron_prompt("intake"), "an untouched prompt is updated")
        self.assertNotIn("--deliver", intake)
        self.assertEqual(self.job("intake")["deliver"], "matrix")
        self.assertFalse(any(c[:2] == ["cron", "create"] for c in self.hermes.calls))
        self.assertEqual(self.job("intake")["state"], "paused", "the job's state is the user's")

    def test_a_job_moved_out_of_the_workspace_is_reported_and_kept(self):
        self.install()
        self.user_edit("feedback", "--workdir", "")
        conn = db.connect(self.cfg.db_path)
        try:
            problems = health.report(conn, config.load(self.home))["problems"]
        finally:
            conn.close()
        self.assertTrue(any("todo-manager-feedback runs in no working directory" in p for p in problems), problems)
        self.hermes.calls.clear()
        self.install()
        self.assertEqual(self.hermes.edits(), [], "the user's choice is kept, only reported")

    def test_the_job_store_is_only_changed_through_hermes_commands(self):
        self.install()
        self.user_edit("intake", "--schedule", "*/10 * * * *")
        before = self.hermes.cron.jobs_file.read_text()
        inst = self.installer()
        inst.runner = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "")   # commands do nothing
        (self.home / "config.toml").write_text((self.home / "config.toml").read_text() + 'model = "m2"\n')
        inst.cfg = config.load(self.home)
        inst.apply(inst.plan())
        self.assertEqual(self.hermes.cron.jobs_file.read_text(), before)


class UninstallTests(InstallCase):
    def setUp(self):
        super().setUp()
        for name in ("USER.md", "MEMORY.md", "SOUL.md"):
            (self.hermes_home / name).parent.mkdir(parents=True, exist_ok=True)
            (self.hermes_home / name).write_text(f"{name} of the user\n")
        conn = db.connect(self.cfg.db_path)
        self.task = store.create_task(conn, "Keep me", origin="manual", actor="human")
        conn.close()

    def test_uninstall_removes_what_was_installed_and_keeps_all_data(self):
        self.install()
        inst = self.installer()
        done = inst.uninstall(inst.uninstall_plan())
        self.assertEqual(self.hermes.cron.jobs(), [])
        self.assertEqual(len([d for d in done if d.startswith("remove")]), 6, done)   # 2 jobs, 2 scripts, launcher, skill
        self.assertFalse(self.paths.install_skill_path.exists())
        self.assertFalse(self.paths.launcher.exists())
        for name in ("USER.md", "MEMORY.md", "SOUL.md"):
            self.assertEqual((self.hermes_home / name).read_text(), f"{name} of the user\n")
        self.assertTrue(self.cfg.db_path.exists())
        for name in ("config.toml", "AGENTS.md"):
            self.assertTrue((self.home / name).exists())
        code, out = run("--home", str(self.home), "export")
        self.assertEqual(code, 0, out)
        bundle = Path(json.loads(out)["export"])
        self.assertTrue((bundle / "tasks" / f"{self.task}.md").exists())
        self.assertTrue((bundle / "workspace" / "AGENTS.md").exists())

    def test_a_skill_changed_after_install_is_kept(self):
        self.install()
        self.paths.install_skill_path.write_text(self.skill() + "\n## Learned\n- mine\n")
        plan = self.installer().uninstall_plan()
        skill = next(a for a in plan if a.target == str(self.paths.install_skill_path))
        self.assertEqual(skill.kind, "keep")
        self.installer().uninstall(plan)
        self.assertIn("## Learned", self.skill())

    def test_reinstall_after_uninstall(self):
        self.install()
        inst = self.installer()
        inst.uninstall(inst.uninstall_plan())
        self.install()
        self.assertEqual(len(self.hermes.cron.jobs()), 2)
        self.assertTrue(self.paths.install_skill_path.exists())

    def test_the_command_asks_first_and_shows_what_stays(self):
        self.install()
        fake = write_executable(self.tmp / "bin" / "hermes", self.hermes.cron.jobs_file)
        (self.home / "config.toml").write_text((self.home / "config.toml").read_text() + f'bin = "{fake}"\n')
        code, out = run("--home", str(self.home), "uninstall", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("[remove] todo-manager-intake", out)
        self.assertIn("USER.md, MEMORY.md, SOUL.md", out)
        self.assertEqual(len(self.hermes.cron.jobs()), 2, "a dry run changes nothing")
        code, out = run("--home", str(self.home), "uninstall")
        self.assertEqual(code, 2, "not confirmed without a terminal or --yes")
        self.assertEqual(len(self.hermes.cron.jobs()), 2)
        code, out = run("--home", str(self.home), "uninstall", "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.hermes.cron.jobs(), [])
        self.assertTrue(self.cfg.db_path.exists())


if __name__ == "__main__":
    unittest.main()
