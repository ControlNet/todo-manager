"""``todo-manager install``: one re-runnable command. Settings are inferred from Hermes (its config,
credentials, himalaya mailboxes, ``gh``); only real choices are asked (several mailboxes, the
Notion home page, several Notion people), and every change outside the workspace is shown and
needs a yes. Everything outside the workspace is a test double here (TEST DOUBLES: ``World``);
no real Hermes, Notion, Gmail, Slack or GitHub is touched.
"""

import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from tests.fake_notion import USER_ID, FakeNotion
from tests.helpers import T0
from todo_manager import cli, clock, i18n, install
from todo_manager.hermes_install import Installer, Paths
from todo_manager.kanban import FakeKanban
from todo_manager.net import HttpError
from todo_manager.notion.client import NotionClient

SECRET_KEYS = ("NOTION_API_KEY", "SLACK_USER_TOKEN", "TODO_MANAGER_LANG", "HERMES_HOME", "TODO_HOME")


class WorldKanban(FakeKanban):
    """TEST DOUBLE: FakeKanban plus the bridge's ping / create_board."""

    def __init__(self):
        super().__init__()
        self.board_exists = False
        self.settings_values = {"review_dispatch": True, "auto_decompose": False}   # Hermes' defaults differ

    def ping(self):
        return {"board_exists": self.board_exists}

    def create_board(self, name, description):
        self.board_exists = True
        return {"slug": self.board, "name": name}


class World:
    """TEST DOUBLES for everything install reads or changes outside the workspace."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.hermes_home = tmp / "hermes"
        (self.hermes_home / "credentials").mkdir(parents=True)
        (self.hermes_home / "cron").mkdir(parents=True)
        self.jobs_file = self.hermes_home / "cron" / "jobs.json"
        self.jobs_file.write_text(json.dumps({"jobs": []}))
        self.env_file = self.hermes_home / ".env"
        self.env_file.write_text("NOTION_API_KEY=test-notion-token\n")          # TEST VALUE, not a real token
        (self.hermes_home / "credentials" / "slack_user_token").write_text("test-slack-token\n")
        self.hermes_config = {"display.language": "zh", "timezone": "Pacific/Auckland"}
        self.helper = str(tmp / "token-helper")
        self.mailboxes = {"work": ("test-gmail-work", "me@work.example")}
        self.himalaya = tmp / "himalaya.toml"
        self.write_himalaya()
        self.gh_token = "test-gh-token"
        self.slack = {"ok": True, "user_id": "U0ME", "team_id": "T0LAB", "team": "Lab", "user": "me"}
        self.notion = FakeNotion()
        self.kanban = WorldKanban()
        self.calls: list[list[str]] = []
        self.http_calls: list[str] = []

    def write_himalaya(self):
        self.himalaya.write_text("".join(
            f'[accounts.{name}]\nemail = "{addr}"\nbackend.type = "imap"\n'
            f'gmail.auth.token.command = ["{self.helper}", "{name}"]\n' for name, (_, addr) in self.mailboxes.items())
            + '[accounts.plain]\nemail = "other@example.org"\nbackend.type = "imap"\n')

    # ------------------------------------------------------------------ local commands
    def runner(self, cmd, **kw):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        ok = lambda out="": subprocess.CompletedProcess(cmd, 0, out, "")
        fail = subprocess.CompletedProcess(cmd, 1, "", "error")
        if cmd[0] == self.helper:
            return ok(self.mailboxes[cmd[1]][0] + "\n") if cmd[1] in self.mailboxes else fail
        if cmd[:3] == ["gh", "auth", "token"]:
            return ok(self.gh_token + "\n") if self.gh_token else fail
        if cmd[0] != "hermes":
            return fail
        args = cmd[1:]
        if args[:2] == ["config", "get"]:
            return ok(self.hermes_config.get(args[2], "") + "\n")
        if args[:2] == ["config", "env-path"]:
            return ok(f"{self.env_file}\n")
        if args[:2] == ["config", "set"]:
            self.hermes_config[args[2]] = args[3]
            if args[2].startswith("kanban."):
                self.kanban.settings_values[args[2].split(".", 1)[1]] = args[3] == "true"
            return ok()
        if args[0] == "cron":
            return self.cron(args, ok)
        return fail

    def cron(self, args, ok):
        data = json.loads(self.jobs_file.read_text())
        if args[1] == "create":
            flag = lambda f: args[args.index(f) + 1]
            data["jobs"].append({"id": f"job{len(data['jobs'])}", "name": flag("--name"), "skills": [flag("--skill")],
                                 "script": flag("--script"), "interpreter": flag("--interpreter"), "state": "paused",
                                 "enabled": False, "prompt": args[-1], "schedule": {"expr": args[-2]},
                                 "deliver": flag("--deliver"), "workdir": None})
        elif args[1] in ("resume", "pause"):
            for j in data["jobs"]:
                if j["id"] == args[2]:
                    j["state"], j["enabled"] = ("scheduled", True) if args[1] == "resume" else ("paused", False)
        self.jobs_file.write_text(json.dumps(data))
        return ok("ok")

    def hermes_calls(self, *prefix):
        return [c for c in self.calls if c[0] == "hermes" and c[1:1 + len(prefix)] == list(prefix)]

    # ------------------------------------------------------------------ web APIs
    def http(self, method, url, headers=None, timeout=None, **kw):
        token = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        self.http_calls.append(url)
        if url == install.GMAIL_PROFILE:
            for real, addr in self.mailboxes.values():
                if token == real:
                    return {"emailAddress": addr}
            raise HttpError(401, "invalid token")
        if url == install.SLACK_AUTH_TEST:
            return self.slack if token == "test-slack-token" else {"ok": False, "error": "invalid_auth"}
        if url == install.GITHUB_USER:
            return {"login": "me-gh"}
        raise HttpError(404, url)

    # ------------------------------------------------------------------ wiring
    def env(self):
        return install.Env(
            runner=self.runner, http=self.http,
            notion=lambda cfg: NotionClient(transport=self.notion, min_interval=0),
            kanban=lambda cfg: self.kanban,
            installer=lambda cfg: Installer(cfg, Paths(self.hermes_home, cfg.home, self.tmp / "bin" / "todo-manager"),
                                            runner=self.runner),
            himalaya_config=self.himalaya)

    def notion_writes(self):
        return [c for c in self.notion.calls if c[0] in ("POST", "PATCH") and c[1] != "/search"]


class InstallCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tm-install-"))
        self.home = self.tmp / "workspace"
        self.world = World(self.tmp)
        clock.set_now(T0)
        saved = {k: os.environ.pop(k) for k in SECRET_KEYS if k in os.environ}
        self.addCleanup(os.environ.update, saved)
        os.environ["HERMES_HOME"] = str(self.world.hermes_home)
        self.addCleanup(os.environ.pop, "HERMES_HOME", None)

    def tearDown(self):
        clock.set_now(None)
        i18n.set_language(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def install(self, answers=None, yes=("all",), no=()):
        prompter = install.AnswerPrompter(answers or {}, set(yes), set(no), output=lambda _line: None)
        run = install.Install(self.home, prompter, self.world.env())
        run.run()
        return prompter, run

    def page(self, title="Todo", markdown=""):
        return self.world.notion.user_top_level_page(title, markdown)

    def config(self):
        return tomllib.loads((self.home / "config.toml").read_text(encoding="utf-8"))


class FreshInstallTests(InstallCase):
    def test_a_fresh_install_asks_only_the_home_page(self):
        home = self.page("我的待办")
        prompter, run = self.install({"notion.home_page": "1"})
        self.assertEqual([a for a in prompter.asked if "." in a], ["notion.home_page"])
        cfg = self.config()
        self.assertEqual((cfg["timezone"], cfg["language"]), ("Pacific/Auckland", "zh-CN"), "from Hermes' config")
        self.assertEqual(cfg["email"]["accounts"], [{"name": "work", "address": "me@work.example",
                                                      "token_command": [self.world.helper, "work"]}])
        self.assertEqual(cfg["identity"]["emails"], ["me@work.example"])
        self.assertEqual(cfg["identity"]["slack_user_ids"], ["U0ME"])
        self.assertEqual(cfg["identity"]["github_logins"], ["me-gh"])
        self.assertEqual(cfg["identity"]["notion_user_ids"], [USER_ID], "the workspace's only person")
        self.assertEqual(cfg["slack"]["token_file"], str(self.world.hermes_home / "credentials" / "slack_user_token"))
        self.assertEqual(cfg["slack"]["team"], "T0LAB")
        self.assertEqual(cfg["notion"]["env_file"], str(self.world.env_file))
        for source in ("email", "slack", "github", "notion", "kanban"):
            self.assertTrue(cfg[source]["enabled"], source)
        self.assertNotIn("test-", (self.home / "config.toml").read_text(), "no secret is written")
        # Notion: the chosen page became the home page.
        self.assertIn("数据新鲜度", self.world.notion.markdown[home])
        self.assertEqual(len(self.world.notion.data_sources), 2)
        # Kanban: board created; Hermes' global switches are explained, never changed.
        self.assertEqual(self.world.hermes_calls("config", "set"), [])
        self.assertTrue(self.world.kanban.board_exists)
        self.assertTrue(any("kanban.review_dispatch is on" in n for n in run.report), run.report)
        # Hermes: jobs created paused, then started.
        jobs = json.loads(self.world.jobs_file.read_text())["jobs"]
        self.assertEqual(len(jobs), 2)
        self.assertEqual({j["state"] for j in jobs}, {"scheduled"})
        self.assertEqual({j["deliver"] for j in jobs}, {"local"})
        self.assertTrue((self.world.hermes_home / "skills" / "productivity" / "todo-manager" / "SKILL.md").exists())

    def test_a_rerun_changes_nothing_and_asks_nothing(self):
        self.page()
        self.install({"notion.home_page": "1"})
        before = (self.home / "config.toml").read_text()
        self.world.calls.clear()
        self.world.notion.calls.clear()
        prompter, run = self.install(yes=())
        self.assertEqual(prompter.asked, [])
        self.assertEqual((self.home / "config.toml").read_text(), before)
        self.assertEqual(self.world.notion_writes(), [])
        self.assertEqual(self.world.hermes_calls("config", "set") + self.world.hermes_calls("cron", "create")
                         + self.world.hermes_calls("cron", "edit") + self.world.hermes_calls("cron", "resume"), [])

    def test_the_terminal_shows_numbered_choices_and_asks_for_yes(self):
        self.page("Todo home")
        replies = iter(["1", "y", "y", "y", "y"])
        shown = []
        prompter = install.TerminalPrompter(input_fn=lambda prompt: (shown.append(prompt), next(replies))[1],
                                            output=shown.append)
        install.Install(self.home, prompter, self.world.env()).run()
        text = "\n".join(shown)
        self.assertIn("  1. Todo home", text)
        self.assertEqual(sum("[y/N]" in s for s in shown), 4, "notion, kanban, hermes, schedule")
        self.assertTrue(self.config()["notion"]["enabled"])

    def test_the_terminal_asks_again_after_an_unusable_answer(self):
        self.page("Todo home")
        replies = iter(["7", "no-such-page", "1", "n", "n", "n", "n"])
        shown = []
        prompter = install.TerminalPrompter(input_fn=lambda prompt: next(replies), output=shown.append)
        install.Install(self.home, prompter, self.world.env()).run()
        self.assertIn("not one of the options; try again.", shown)
        self.assertTrue(any("cannot be read" in s for s in shown))
        self.assertNotIn("enabled", self.config().get("notion", {}), "declined: not enabled")


class QuestionTests(InstallCase):
    def test_several_mailboxes_are_a_choice(self):
        self.world.mailboxes["lab"] = ("test-gmail-lab", "lab@lab.example")
        self.world.write_himalaya()
        with self.assertRaises(install.NeedsAnswer) as caught:
            self.install()
        self.assertEqual(caught.exception.key, "email.accounts")
        self.assertTrue(caught.exception.multiple)
        self.assertEqual([v for v, _ in caught.exception.options], ["work", "lab"])
        self.assertNotIn("email", self.config(), "nothing about mail is written before the choice")
        self.page()
        self.install({"email.accounts": "2", "notion.home_page": "1"})
        cfg = self.config()
        self.assertEqual([a["name"] for a in cfg["email"]["accounts"]], ["lab"])
        self.assertEqual(cfg["identity"]["emails"], ["lab@lab.example"])

    def test_choosing_no_mailbox_disables_mail(self):
        self.world.mailboxes["lab"] = ("test-gmail-lab", "lab@lab.example")
        self.world.write_himalaya()
        self.page()
        self.install({"email.accounts": "", "notion.home_page": "1"})
        self.assertEqual((self.config()["email"]["enabled"], self.config()["email"]["accounts"]), (False, []))

    def test_the_home_page_is_asked_even_when_only_one_page_is_shared(self):
        self.page()
        with self.assertRaises(install.NeedsAnswer) as caught:
            self.install()
        out = caught.exception.to_json("--yes all")
        self.assertEqual(out["key"], "notion.home_page")
        self.assertEqual(len(out["options"]), 1)
        self.assertEqual(out["answer_with"], "todo-manager install --yes all --answer notion.home_page=<value>")
        self.assertEqual(self.world.notion_writes(), [])
        self.assertTrue(self.config()["slack"]["enabled"], "the steps before it are kept")

    def test_pages_inside_other_pages_and_trashed_pages_are_not_offered(self):
        top = self.page("Top")
        trashed = self.page("Old")
        self.world.notion.pages[trashed]["in_trash"] = True
        self.world.notion._new_page({"type": "page_id", "page_id": top}, {}, "", USER_ID)
        with self.assertRaises(install.NeedsAnswer) as caught:
            self.install()
        self.assertEqual([v for v, _ in caught.exception.options], [top])

    def test_a_page_id_not_in_the_list_is_accepted_when_readable(self):
        self.page("Listed")
        other = self.world.notion._new_page({"type": "page_id", "page_id": "elsewhere"}, {}, "", USER_ID)["id"]
        self.install({"notion.home_page": other})
        self.assertIn("数据新鲜度", self.world.notion.markdown[other])

    def test_an_unreadable_page_is_asked_again(self):
        self.page()
        with self.assertRaises(install.NeedsAnswer) as caught:
            self.install({"notion.home_page": "missing-page"})
        self.assertIn("cannot be read", caught.exception.error)

    def test_no_shared_page_explains_what_to_do(self):
        _, run = self.install()
        [line] = [r for r in run.report if r.startswith("notion:")]
        self.assertIn("Connections", line)
        self.assertNotIn("enabled", self.config().get("notion", {}))
        self.assertEqual(self.world.notion_writes(), [])
        self.assertTrue(self.config()["kanban"]["enabled"], "later steps still run")

    def test_several_notion_people_are_a_choice(self):
        self.world.notion.people.append({"object": "user", "id": "user-0002", "type": "person", "name": "Colleague"})
        self.page()
        with self.assertRaises(install.NeedsAnswer) as caught:
            self.install({"notion.home_page": "1"})
        self.assertEqual(caught.exception.key, "identity.notion_user")
        self.install({"notion.home_page": "1", "identity.notion_user": "2"})
        self.assertEqual(self.config()["identity"]["notion_user_ids"], ["user-0002"])

    def test_the_time_zone_is_asked_when_neither_hermes_nor_the_machine_tell_it(self):
        del self.world.hermes_config["timezone"]
        with mock.patch("todo_manager.config.local_timezone", return_value="Etc/UTC"):
            with self.assertRaises(install.NeedsAnswer) as caught:
                self.install()
            self.assertEqual(caught.exception.key, "timezone")
            self.assertFalse(self.home.joinpath("config.toml").exists())
            with self.assertRaises(install.NeedsAnswer) as caught:
                self.install({"timezone": "Mars/Base"})
            self.assertIn("not an IANA", caught.exception.error)
            self.page()
            self.install({"timezone": "Pacific/Chatham", "notion.home_page": "1"})
        self.assertEqual(self.config()["timezone"], "Pacific/Chatham")

    def test_the_machine_time_zone_is_used_when_it_is_not_utc(self):
        del self.world.hermes_config["timezone"]
        self.page()
        with mock.patch("todo_manager.config.local_timezone", return_value="Europe/Berlin"):
            prompter, _ = self.install({"notion.home_page": "1"})
        self.assertNotIn("timezone", prompter.asked)
        self.assertEqual(self.config()["timezone"], "Europe/Berlin")

    def test_page_ids_and_urls(self):
        h = "0123456789abcdef0123456789abcdef"
        dashed = "01234567-89ab-cdef-0123-456789abcdef"
        for value in (h, dashed, f"https://www.notion.so/Todo-{h}", f"https://www.notion.so/{h}?pvs=4"):
            self.assertEqual(install._page_id(value), dashed, value)


class ConfirmationTests(InstallCase):
    def test_declining_notion_writes_nothing_to_notion(self):
        self.page()
        self.install({"notion.home_page": "1"}, yes=("kanban", "hermes", "schedule"), no=("notion",))
        self.assertEqual(self.world.notion_writes(), [])
        self.assertNotIn("enabled", self.config().get("notion", {}))

    def test_declining_kanban_changes_no_hermes_setting(self):
        self.page()
        self.install({"notion.home_page": "1"}, yes=("notion", "hermes", "schedule"), no=("kanban",))
        self.assertEqual(self.world.hermes_calls("config", "set"), [])
        self.assertFalse(self.world.kanban.board_exists)
        self.assertNotIn("kanban", self.config())

    def test_each_confirmation_is_asked_in_turn(self):
        self.page()
        with self.assertRaises(install.NeedsConfirmation) as caught:
            self.install({"notion.home_page": "1"}, yes=())
        self.assertEqual(caught.exception.step, "notion")
        self.assertTrue(any("数据库" in c or "database" in c.lower() for c in caught.exception.changes))
        with self.assertRaises(install.NeedsConfirmation) as caught:
            self.install({"notion.home_page": "1"}, yes=("notion",))
        self.assertEqual(caught.exception.step, "kanban")
        self.assertEqual(caught.exception.changes, ["create the Hermes board 'todo-manager'"])
        with self.assertRaises(install.NeedsConfirmation) as caught:
            self.install(yes=("kanban",))
        self.assertEqual(caught.exception.step, "hermes")
        with self.assertRaises(install.NeedsConfirmation) as caught:
            self.install(yes=("hermes",))
        self.assertEqual(caught.exception.step, "schedule")
        self.assertEqual({j["state"] for j in json.loads(self.world.jobs_file.read_text())["jobs"]}, {"paused"})
        self.install(yes=("schedule",))
        self.assertEqual({j["state"] for j in json.loads(self.world.jobs_file.read_text())["jobs"]}, {"scheduled"})

    def test_existing_page_content_is_mentioned(self):
        self.page("Home", markdown="我自己写的说明")
        with self.assertRaises(install.NeedsConfirmation) as caught:
            self.install({"notion.home_page": "1"}, yes=())
        self.assertIn("already has content", caught.exception.changes[-1])

    def test_an_interrupted_notion_setup_is_finished_on_the_same_page(self):
        home = self.page()
        self.world.notion.fail_next[("POST", "/databases")] = HttpError(400, "boom", body={"code": "validation_error",
                                                                                           "message": "boom"})
        with self.assertRaises(Exception):
            self.install({"notion.home_page": "1"})
        self.page("Another")
        prompter, _ = self.install()
        self.assertNotIn("notion.home_page", prompter.asked)
        self.assertEqual(len(self.world.notion.data_sources), 2)
        self.assertEqual(self.world.notion.markdown[home].count("数据新鲜度"), 1)


class ExistingConfigTests(InstallCase):
    def test_what_the_user_set_is_kept(self):
        self.home.mkdir(parents=True)
        (self.home / "config.toml").write_text(
            '# my notes\ntimezone = "Europe/Berlin"\nlanguage = "en"\n\n'
            '[identity]\nemails = ["old@example.org"]   # mine\n\n'
            '[email]\nenabled = false   # not now\n\n'
            '[slack]\nenabled = true\ntoken_env = "MY_SLACK"\nchannels = false\n', encoding="utf-8")
        self.page()
        self.install({"notion.home_page": "1"})
        text = (self.home / "config.toml").read_text()
        for kept in ("# my notes\n", 'emails = ["old@example.org"]   # mine\n', "enabled = false   # not now\n",
                     'token_env = "MY_SLACK"\nchannels = false\n'):
            self.assertIn(kept, text)
        cfg = self.config()
        self.assertEqual((cfg["timezone"], cfg["language"]), ("Europe/Berlin", "en"))
        self.assertNotIn("accounts", cfg["email"])
        self.assertNotIn("slack_user_ids", cfg["identity"], "slack as configured: not inferred")
        self.assertEqual(cfg["identity"]["github_logins"], ["me-gh"])
        self.assertFalse([u for u in self.world.http_calls if u in (install.GMAIL_PROFILE, install.SLACK_AUTH_TEST)])

    def test_identities_are_only_added_to(self):
        self.home.mkdir(parents=True)
        (self.home / "config.toml").write_text('[identity]\ngithub_logins = ["older-login"]\n', encoding="utf-8")
        self.page()
        self.install({"notion.home_page": "1"})
        self.assertEqual(self.config()["identity"]["github_logins"], ["older-login", "me-gh"])

    def test_missing_credentials_leave_sources_off(self):
        self.world.mailboxes.clear()
        self.world.write_himalaya()
        self.world.gh_token = None
        (self.world.hermes_home / "credentials" / "slack_user_token").unlink()
        self.world.env_file.write_text("")
        _, run = self.install()
        cfg = self.config()
        for source in ("email", "slack", "github", "notion"):
            self.assertNotIn(source, {k for k, v in cfg.items() if isinstance(v, dict) and v.get("enabled")}, source)
        self.assertTrue(any(r.startswith("notion: no NOTION_API_KEY") for r in run.report))

    def test_a_refused_slack_token_is_not_used(self):
        self.world.slack = {"ok": False, "error": "token_revoked"}
        self.world.http_calls.clear()
        (self.world.hermes_home / "credentials" / "slack_user_token").write_text("revoked\n")
        self.page()
        self.install({"notion.home_page": "1"})
        self.assertNotIn("slack", self.config())


class CliTests(InstallCase):
    def run_cli(self, *argv):
        out = []
        with mock.patch.object(install, "Env", return_value=self.world.env()), \
                mock.patch("sys.stdin.isatty", return_value=False), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            code = cli.main(["--home", str(self.home), "install", *argv])
        return code, "\n".join(out)

    def test_without_a_terminal_a_question_stops_with_exit_3_and_json(self):
        self.page()
        code, out = self.run_cli()
        self.assertEqual(code, cli.EXIT_NEEDS_INPUT)
        data = json.loads(out)
        self.assertEqual((data["status"], data["key"]), ("needs_answer", "notion.home_page"))
        self.assertIn(f"--home {self.home}", data["answer_with"])
        self.assertTrue(any(d.startswith("slack:") for d in data["done"]))
        code, out = self.run_cli("--answer", "notion.home_page=1")
        data = json.loads(out)
        self.assertEqual((code, data["status"], data["step"]), (3, "needs_confirmation", "notion"))
        self.assertIn("--answer notion.home_page=1 --yes notion", data["confirm_with"])
        code, out = self.run_cli("--answer", "notion.home_page=1", "--yes", "all")
        self.assertEqual((code, json.loads(out)["status"]), (0, "complete"))

    def test_unknown_steps_are_refused(self):
        code, out = self.run_cli("--yes", "everything")
        self.assertEqual(code, cli.EXIT_INVALID)
        self.assertIn("unknown step", out)
        self.assertFalse(self.home.exists())

    def test_init_still_creates_only_the_workspace(self):
        with mock.patch("builtins.print"):
            self.assertEqual(cli.main(["--home", str(self.home), "init", "--language", "zh-CN"]), 0)
        self.assertEqual(self.config()["language"], "zh-CN")
        self.assertEqual(self.world.calls, [], "init asks Hermes nothing")


if __name__ == "__main__":
    unittest.main()
