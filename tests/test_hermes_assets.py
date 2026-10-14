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

    def test_todo_preferences_are_edited_in_the_workspace_file(self):
        skill = flat("skill/SKILL.md")
        self.assertIn("PREFERENCES.md", skill.split("## Command reference")[0], "named before the commands")
        self.assertIn("edit it directly", skill)
        self.assertIn("not the memory tool", skill)
        for scope in ("## General", "## Intake", "## Feedback", "## Delegation"):
            self.assertIn(scope, skill)
        self.assertIn("preferences", hermes_install.asset("skill/SKILL.md").split("---")[1].lower(),
                      "the skill description makes a chat about preferences load it")
        self.assertIn("PREFERENCES.md", flat("cron/feedback.md"))
        self.assertIn("not AGENTS.md, PREFERENCES.md, config.toml", flat("cron/intake.md"))
        for name in ("workspace/AGENTS.md", "workspace/AGENTS.zh-CN.md"):
            self.assertIn("PREFERENCES.md", flat(name))



class LaterStartTests(unittest.TestCase):
    """"Do it after 5pm": the start time is passed to the card, not written into the brief
    (an agent that reads it only learns to wait while it already runs; 2026-10-12)."""

    def test_the_runs_pass_a_later_start_instead_of_writing_it_into_the_brief(self):
        for name in ("cron/feedback.md", "skill/SKILL.md"):
            text = flat(name)
            self.assertIn('--start-after "YYYY-MM-DD HH:MM"', text, name)
            self.assertIn("brief", text[text.index("--start-after"):], name)


class TaskRoutingTests(unittest.TestCase):
    """The user decides who does a task: only in-person or offline work starts as a todo; the rest
    is offered on the decision board, saying what an agent would deliver (user's choice, 2026-10-12)."""

    def test_intake_offers_work_on_the_decision_board_unless_it_is_in_person(self):
        text = flat("cron/intake.md")
        self.assertIn("in person or offline", text)
        self.assertIn("Agent can:", text)
        self.assertIn("never ask the user whether something was done", text.lower())

    def test_the_board_choices_are_named_where_the_runs_read_them(self):
        for name in ("cron/intake.md", "cron/feedback.md", "skill/SKILL.md"):
            text = flat(name)
            for choice in ("Cancel", "Done", "My to-do", "Assign to Agent"):
                self.assertIn(choice, text, name)
        self.assertIn("take effect without a run", flat("cron/feedback.md"))


class OptionalInvitationTests(unittest.TestCase):
    """A survey, ranking, marketing or opt-in request where no reply simply means no is not a task,
    even with a date in it (a ranking-survey consent request became a decision, 2026-10-12)."""

    def test_intake_dismisses_optional_invitations(self):
        text = flat("cron/intake.md")
        self.assertIn("no reply simply means no", text)
        self.assertIn("surveys, rankings", text)

    def test_recurring_meetings_are_not_tasks(self):
        text = flat("cron/intake.md")
        self.assertIn("A recurring meeting", text)
        self.assertIn("nor is one occurrence of it", text)

    def test_automated_account_and_security_notices_are_not_tasks(self):
        text = flat("cron/intake.md")
        self.assertIn("new sign-in or device", text)
        self.assertIn("third-party app or OAuth access granted", text)
        self.assertIn("GitHub, Google", text)
        self.assertIn("the user sees them directly", text)


class ExistingCommitmentTests(unittest.TestCase):
    """Observer reviews 2026-10-11..14: progress from another thread or channel was dismissed
    instead of linked (a status report in Slack, "you were added" notices of a research system, a deadline
    reminder dismissed as "no commitment"); backfilled older mail recreated requests later mail had
    settled; a follow-up task lost its earlier conditions; a finished stage kept its date; a field
    the user set stayed out of date after plain evidence (the user chose to let evidence update it,
    with a change note, 2026-10-14)."""

    def test_existing_tasks_are_searched_across_conversations_before_create_or_dismiss(self):
        text = flat("cron/intake.md")
        self.assertIn("whatever conversation or channel the news comes from", text)
        self.assertIn("Before you create a task or dismiss an item", text)
        self.assertIn("Progress on an existing task is linked to it, never dismissed", text)
        self.assertIn('never "no commitment"', text)

    def test_later_material_that_settles_a_request_is_looked_for(self):
        text = flat("cron/intake.md")
        self.assertIn("later material already settles it", text)
        self.assertIn("handled_elsewhere", text)

    def test_a_continuing_task_keeps_the_earlier_conditions(self):
        self.assertIn("keeps the conditions still in force", flat("cron/intake.md"))

    def test_a_new_stage_replaces_the_finished_stage_date(self):
        self.assertIn("A passed date of a finished stage is not the task's due date", flat("cron/intake.md"))

    def test_plain_newer_evidence_may_update_a_field_the_user_set(self):
        text = flat("cron/intake.md")
        self.assertIn("--override-human --evidence", text)
        self.assertIn("never on inference alone", text)
        self.assertIn("do not close a task whose status the user set", text)
        self.assertIn("Do not reopen a task the user closed", text)
