"""Boundary 3: every text a model run reads (both cron prompts, the skill and the runtime
AGENTS.md template) forbids changing the program, its skill or cron jobs, config.toml or Hermes
settings, and says to stop and report when a command fails. The installed Hermes prompt
scanner check is in tests/integration/test_hermes_prompts.py."""

import unittest

from todo_manager import hermes_install


class SelfModificationRuleTests(unittest.TestCase):
    def texts(self):
        names = [f"cron/{job}.md" for job in hermes_install.JOBS] + ["skill/SKILL.md", "workspace/AGENTS.md"]
        return {name: hermes_install.asset(name) for name in names}

    def test_every_model_facing_text_forbids_changing_the_system_itself(self):
        for name, text in self.texts().items():
            flat = " ".join(text.split())
            with self.subTest(name):
                self.assertIn("todo-manager program", flat)
                for thing in ("skill", "cron jobs", "config.toml", "Hermes settings"):
                    self.assertIn(thing, flat)
                self.assertRegex(flat, r"(?i)command fails, stop and report")
