"""Localization: catalogs, language selection, English setup, cross-language parsing."""

import json
import os
import re
import string
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_manager import config, delegation, i18n, prompts, store
from todo_manager.notion import body, schema, setup
from todo_manager.notion.client import NotionClient
from todo_manager.notion.sync import NotionSync, load_state
from tests.fake_notion import USER_ID, FakeNotion
from tests.helpers import WorkspaceCase
from tests.test_notion import NotionCase

PACKAGE = Path(__file__).resolve().parent.parent / "todo_manager"
CJK = re.compile(r"[㐀-鿿＀-￯]")


def placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


class CatalogTests(unittest.TestCase):
    def test_every_language_has_the_same_keys_and_placeholders(self):
        en = i18n.catalog("en")
        for lang in i18n.LANGUAGES:
            other = i18n.catalog(lang)
            self.assertEqual(set(other), set(en), lang)
            for key, text in en.items():
                self.assertEqual(placeholders(other[key]), placeholders(text), f"{lang}:{key}")

    def test_option_labels_are_unique_within_a_group(self):
        for lang in i18n.LANGUAGES:
            for group in ("task_status", "next_actor", "project_status", "delegation"):
                names = list(schema.labels(group, lang).values())
                self.assertEqual(len(names), len(set(names)), f"{lang}:{group}")

    def test_normalize_and_environment(self):
        self.assertEqual(i18n.normalize("zh_CN.UTF-8"), "zh-CN")
        self.assertEqual(i18n.normalize("zh-Hans"), "zh-CN")
        self.assertEqual(i18n.normalize("en_AU.UTF-8"), "en")
        self.assertIsNone(i18n.normalize("fr_FR"))
        with mock.patch.dict(os.environ, {"TODO_MANAGER_LANG": "", "LC_ALL": "", "LC_MESSAGES": "",
                                          "LANG": "zh_CN.UTF-8"}):
            self.assertEqual(i18n.from_environment(), "zh-CN")
        with mock.patch.dict(os.environ, {"TODO_MANAGER_LANG": "", "LC_ALL": "", "LC_MESSAGES": "", "LANG": "C"}):
            self.assertEqual(i18n.from_environment(), "en")

    def test_unknown_key_is_an_error(self):
        with self.assertRaises(KeyError):
            i18n.t("no.such.key", "en")

    def test_source_has_no_hardcoded_user_text(self):
        """User-facing text lives in the catalogs. Allowed: the legacy-migration schema (data of the
        old system), the reply-quote parser (data) and the language's own display name."""
        allowed = {"migration.py": ("LEGACY", "ACTIVE", "CLOSED", "INBOX", '"task":', '"project":',
                                    '"fingerprint"', "high_confidence"),
                   "intake.py": ("QUOTE_START",), "i18n.py": ("LANGUAGES",)}
        offenders = []
        for path in PACKAGE.rglob("*"):
            if path.suffix not in (".py", ".md") or "locales" in path.parts:
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if CJK.search(line) and not any(a in line for a in allowed.get(path.name, ())):
                    offenders.append(f"{path.relative_to(PACKAGE)}:{n}: {line.strip()[:80]}")
        self.assertEqual(offenders, [])


class LanguageSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tm-i18n-"))
        self.addCleanup(lambda: i18n.set_language(i18n.DEFAULT))

    def test_config_language_and_environment_override(self):
        (self.tmp / "config.toml").write_text('language = "zh-CN"\n', encoding="utf-8")
        with mock.patch.dict(os.environ, {"TODO_MANAGER_LANG": ""}):
            cfg = config.load(self.tmp)
        self.assertEqual((cfg.language, i18n.language()), ("zh-CN", "zh-CN"))
        self.assertEqual(i18n.t("label.task_status.done"), "完成")
        with mock.patch.dict(os.environ, {"TODO_MANAGER_LANG": "en"}):
            self.assertEqual(config.load(self.tmp).language, "en")
        self.assertEqual(i18n.t("label.task_status.done"), "Done")

    def test_prompts_stay_english_and_name_the_language(self):
        i18n.set_language("zh-CN")
        text = prompts.load("intake_decisions.md") + prompts.load("feedback_decisions.md")
        self.assertIn("Simplified Chinese", text)
        self.assertNotIn("{language}", text)
        protocol = delegation.WORKER_PROTOCOL.format(allowed="read", pause="X", language=i18n.language_name())
        self.assertIn("summary in Simplified Chinese", protocol)
        i18n.set_language("en")
        self.assertIn("is in English", prompts.load("intake_decisions.md"))


class EnglishSetupTests(NotionCase):
    overrides = {"language": "en", "identity": {"notion_user_ids": [USER_ID]}}

    def test_structure_is_created_in_english_and_round_trips(self):
        names = set(self.fake.data_sources[self.tasks_ds]["properties"])
        self.assertTrue({"Task", "Status", "Next by", "Project", "TM ID", "Agent work"} <= names, names)
        options = [o["name"] for o in self.fake.data_sources[self.tasks_ds]["properties"]["Status"]["select"]["options"]]
        self.assertEqual(options, ["To do", "In progress", "Waiting", "To decide", "To review", "Done", "Cancelled"])
        self.assertEqual(self.sync.state.language, "en")
        home = self.fake.markdown[self.sync.state.home_page_id]
        self.assertIn("Data freshness", home)
        self.assertIn("## ⭐ Focus", home)
        self.assertTrue(any("linked view" in s for s in setup.plan("parent")))

        with self.conn:
            tid = store.create_task(self.conn, "Reply to Alice", origin="cli", actor="human", cause_ref="t",
                                    responsibility="Alice asked me")
        self.push(tid)
        page = self.page_of(tid)
        self.assertEqual(self.fake.prop(page, "Status"), "To do")
        self.assertIn("Why this is mine", self.fake.markdown[page])
        self.fake.user_edit(page, Status="Waiting")
        self.pull()
        self.assertEqual(store.get_task(self.conn, tid)["status"], "waiting")

    def test_language_stays_fixed_after_setup_and_any_language_parses(self):
        i18n.set_language("zh-CN")      # config switched later: the Notion structure keeps English
        state = load_state(self.conn, self.cfg)
        self.assertEqual(state.language, "en")
        with self.conn:
            tid = store.create_task(self.conn, "Check data", origin="cli", actor="human", cause_ref="t")
        self.push(tid)
        page = self.page_of(tid)
        self.assertEqual(self.fake.prop(page, "Status"), "To do")
        self.fake.user_edit(page, Status="完成")   # an option name of another language is understood
        self.sync = NotionSync(self.conn, self.cfg, self.client, load_state(self.conn, self.cfg))
        self.sync.pull("task")
        self.assertEqual(store.get_task(self.conn, tid)["status"], "done")


class MarkerTests(unittest.TestCase):
    def test_markers_of_every_language_are_recognised(self):
        for lang in i18n.LANGUAGES:
            region = body.render({"goal": "x"}, lang=lang)
            doc = "my notes\n\n" + region + "\n\nmore notes"
            self.assertEqual(body.extract(doc), region, lang)
            home = body.render({"extra": [("a", "b")]}, marker="home.marker", lang=lang)
            self.assertEqual(body.extract("intro\n" + home, "home.marker"), home, lang)
            self.assertIsNone(body.extract("intro\n" + home), "task marker does not match the home box")


class InitLanguageTests(unittest.TestCase):
    def test_init_writes_the_chosen_language(self):
        from todo_manager import cli
        tmp = Path(tempfile.mkdtemp(prefix="tm-init-"))
        self.addCleanup(lambda: i18n.set_language(i18n.DEFAULT))
        with mock.patch("sys.stdout"):
            self.assertEqual(cli.main(["--home", str(tmp), "init", "--language", "zh_CN"]), 0)
        self.assertIn('language = "zh-CN"', (tmp / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(config.load(tmp).language, "zh-CN")
        self.assertIn("todo-manager", (tmp / "AGENTS.md").read_text(encoding="utf-8"))
        (tmp / "AGENTS.md").write_text("local edits", encoding="utf-8")
        with mock.patch("sys.stdout"):
            cli.main(["--home", str(tmp), "init"])
        self.assertEqual((tmp / "AGENTS.md").read_text(encoding="utf-8"), "local edits", "never overwritten")


if __name__ == "__main__":
    unittest.main()
