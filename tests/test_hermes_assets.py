"""Boundary 3: authority depends on the scenario. Every text a scheduled run reads (both cron
prompts, the skill) says that a scheduled run changes Todo data only and never the program,
its skill, the cron jobs or Hermes settings; the skill routes what the user explicitly asks to
change to where it takes effect (conventions in AGENTS.md, collection settings through
``todo-manager config set``, schedule and skill only in a chat). The installed Hermes prompt
scanner check is in tests/integration/test_hermes_prompts.py."""

import unittest

from todo_manager import hermes_install


def flat(name: str) -> str:
    return " ".join(hermes_install.asset(name).split())


class ScheduledRunAuthorityTests(unittest.TestCase):
    def test_cron_prompts_own_workflows_and_failure_handling(self):
        for entry in hermes_install.JOBS:
            text = flat(f"cron/{entry}.md")
            self.assertIn("exactly [CRON_FAILURE]", text)
            self.assertIn("Script Error", text)
            self.assertIn("only reports problems", text)
            self.assertIn("[SILENT]", text)
            for thing in ("todo-manager program", "skill", "cron jobs", "Hermes settings"):
                self.assertIn(thing, text)
        self.assertIn("capacity and approval", flat("cron/feedback.md"))
        self.assertIn("session snapshot", flat("cron/feedback.md"))
        self.assertIn("## Intake run", flat("cron/intake.md"))
        self.assertIn("## Delegation", flat("cron/feedback.md"))

    def test_skill_is_a_command_reference_and_native_memory_is_not_replaced(self):
        skill = flat("skill/SKILL.md")
        self.assertIn("memories/USER.md", skill)
        self.assertNotIn("## Intake run", skill)
        self.assertNotIn("## Feedback run", skill)
        self.assertNotIn("Conventions live in `AGENTS.md`", skill)
        for name in ("workspace/AGENTS.md", "workspace/AGENTS.zh-CN.md"):
            text = flat(name)
            self.assertIn("todo-manager config set <key> <value>", text)
            self.assertIn("state/", text)
            self.assertNotIn("My Todo conventions", text)
