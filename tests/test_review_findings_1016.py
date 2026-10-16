"""Findings of the trial observer's reviews of 2026-10-14..16. Synthetic workspace, mail, Notion
(FakeNotion), Kanban (FakeKanban) and Gmail (FakeHttp) only."""

from pathlib import Path

from todo_manager import clock, gate_intake, health, i18n, runs, store
from todo_manager.hermes_assets import __file__ as assets_init
from todo_manager.net import HttpError
from todo_manager.sources.gmail import GmailSource
from tests.helpers import WorkspaceCase
from tests.test_delegation_reliability import Base
from tests.test_notion import NotionCase
from tests.test_sources import FakeHttp, gmail_message


class FailureNoteTests(Base):
    """A card blocked by a provider failure said "Agent 等你补充信息" on the task (seen
    2026-10-16): the user had nothing to add, only retry or cancel."""

    def test_a_failure_says_retry_or_cancel(self):
        did = self.delegate()
        self.tick()
        card = self.d(did)["card_id"]
        while self.kb.cards[card]["status"] != "blocked":      # the failure breaker parks it
            self.kb.worker_start(card)
            self.kb.worker_crash(card)
        self.tick()
        self.assertEqual(self.d(did)["status"], "blocked")
        self.assertEqual(store.get_task(self.conn, self.task)["change_note"], i18n.t("dlg.failed_note"))

    def test_a_question_still_asks_for_information(self):
        did = self.delegate()
        self.tick()
        card = self.d(did)["card_id"]
        self.kb.worker_start(card)
        self.kb.worker_block(card, "周四还是周五？")
        self.tick()
        self.assertEqual(store.get_task(self.conn, self.task)["change_note"], i18n.t("dlg.needs_input"))


class AgentWorkDueCheckTests(Base):
    """A task had agent work parked until 10-17 09:00 and a follow-up date of
    10-17: intake was offered the follow-up at local midnight, skipped it twice (the work was
    already booked) and health reported the check as failed."""

    def test_no_follow_up_or_due_check_while_agent_work_is_in_progress(self):
        today = clock.local_date(self.cfg.tz).isoformat()
        store.update_task(self.conn, self.task, {"follow_up_date": today, "due_date": "2026-10-01"},
                          actor="agent", origin="test")
        did = self.delegate(start_after="2026-10-06T06:00:00.000Z")
        self.tick()
        self.assertEqual(gate_intake.due_checks(self.conn, self.cfg), [])
        self.conn.execute("UPDATE delegations SET status='ended' WHERE id=?", (did,))   # the work is over
        self.assertEqual([k for _, _, k in gate_intake.due_checks(self.conn, self.cfg)], ["followup"])


class ProblemsOnlyWakeTests(WorkspaceCase):
    """A problems-only wake is told to make no changes, so it never reports `run done`; health
    still listed it as a run that left work unfinished."""

    def wake(self, entry, reasons):
        run_id = runs.start(self.conn, entry)
        runs.finish(self.conn, run_id, status="woke", wake=True, reasons=reasons)
        return run_id

    def never_done(self):
        return [p for p in health.report(self.conn, self.cfg)["problems"] if "never reported" in p]

    def test_a_problems_only_wake_is_not_an_unfinished_run(self):
        self.wake("feedback", ["problems"])
        self.wake("intake", ["source_problems"])
        clock.advance(minutes=120)
        self.assertEqual(self.never_done(), [])

    def test_a_business_wake_without_run_done_is_still_reported(self):
        run_id = self.wake("feedback", ["feedback:1"])
        self.wake("feedback", ["problems"])
        clock.advance(minutes=120)
        self.assertEqual(len(self.never_done()), 1)
        self.assertIn(run_id, self.never_done()[0])


class GmailAuthorshipTests(WorkspaceCase):
    """A Google Forms receipt from forms-receipts-noreply@google.com carried SENT and was stored as
    the user's own mail (author_is_me), so it came back in `source search --mine`."""

    def source(self, routes):
        return GmailSource(account="primary", address="me@example.edu", token=lambda: "t", my_addresses=[],
                           http=FakeHttp(routes), initial_lookback_days=3)

    SEND_AS = ("settings/sendAs", {"sendAs": [{"sendAsEmail": "me@example.edu"},
                                              {"sendAsEmail": "Lab@example.edu"}]})

    def test_sent_from_someone_else_is_not_the_users(self):
        src = self.source([self.SEND_AS])
        receipt = src.normalize(gmail_message("m1", "t1", sender="Forms receipts <forms-receipts-noreply@google.com>",
                                              labels=("INBOX", "SENT")))
        self.assertFalse(receipt.author_is_me)

    def test_sent_from_a_send_as_alias_is_the_users_and_the_list_is_read_once(self):
        src = self.source([self.SEND_AS])
        lab = src.normalize(gmail_message("m1", "t1", sender="Lab <lab@example.edu>", labels=("SENT",)))
        again = src.normalize(gmail_message("m2", "t2", sender="Lab <lab@example.edu>", labels=("SENT",)))
        self.assertTrue(lab.author_is_me and again.author_is_me)
        self.assertEqual(sum(1 for c in src.http.calls if "sendAs" in c[1]), 1)

    def test_own_mail_needs_no_lookup_and_a_refused_lookup_leaves_the_identities(self):
        src = self.source([("settings/sendAs", HttpError(403, "insufficient scope"))])
        self.assertTrue(src.normalize(gmail_message("m1", "t1", sender="Me <me@example.edu>",
                                                    labels=("SENT",))).author_is_me)
        self.assertEqual(src.http.calls, [])
        self.assertFalse(src.normalize(gmail_message("m2", "t2", sender="Lab <lab@example.edu>",
                                                     labels=("SENT",))).author_is_me)


class MovedPageTests(NotionCase):
    """The user's card was dropped into the home page's freshness callout: the page left the tasks
    database and lost its properties. Pushing it failed with "invalid status None"."""

    def setUp(self):
        super().setUp()
        self.task = store.create_task(self.conn, "协助整理资料", origin="intake", next_step="确认整理范围")
        self.push(self.task)
        self.page = self.page_of(self.task)

    def move_out(self):
        page = self.fake.pages[self.page]
        page["parent"] = {"type": "block_id", "block_id": "callout-1"}
        page["properties"] = {k: v for k, v in page["properties"].items() if v.get("type") == "title"}

    def test_the_push_says_the_page_was_moved_and_fields_are_not_cleared(self):
        self.move_out()
        clock.advance(minutes=3)
        store.update_task(self.conn, self.task, {"status": "cancelled"}, actor="agent", origin="intake")
        stats = self.push(self.task)
        self.assertEqual(stats["failed"], 1)
        error = self.conn.execute("SELECT last_error FROM outbox WHERE status='failed'").fetchone()[0]
        self.assertIn("moved out of its database", error)
        self.assertIn("callout-1", error)
        task = store.get_task(self.conn, self.task)
        self.assertEqual((task["status"], task["next_step"]), ("cancelled", "确认整理范围"))

    def test_moved_back_its_properties_are_written_again_not_imported_as_cleared(self):
        self.move_out()
        clock.advance(minutes=3)
        store.update_task(self.conn, self.task, {"status": "cancelled"}, actor="agent", origin="intake")
        self.push(self.task)
        self.fake.pages[self.page]["parent"] = {"type": "data_source_id", "data_source_id": self.tasks_ds}
        clock.advance(minutes=10)
        self.conn.execute("UPDATE outbox SET next_attempt_at=? WHERE status='failed'", (clock.iso(),))
        self.drain()
        task = store.get_task(self.conn, self.task)
        self.assertEqual((task["status"], task["next_step"]), ("cancelled", "确认整理范围"))
        self.assertEqual(self.fake.prop(self.page, "状态"), "取消")
        self.assertEqual(self.fake.prop(self.page, "TM ID"), self.task)

    def test_a_direct_read_of_the_moved_page_imports_nothing(self):
        self.move_out()
        clock.advance(minutes=3)
        sync = self.new_sync()
        self.assertEqual(sync.import_page("task", self.fake.pages[self.page]), "ignored")
        self.assertTrue(any("moved out of its database" in p for p in sync.problems))
        self.assertEqual(store.get_task(self.conn, self.task)["next_step"], "确认整理范围")


class FeedbackWaitingTests(WorkspaceCase):
    """A plan task: the user said they submitted the plan and now wait for approval; the feedback run set
    waiting without a follow-up date, so nothing would bring the task back if no reply came."""

    def test_the_feedback_prompt_asks_for_a_follow_up_date_when_the_task_starts_waiting(self):
        text = (Path(assets_init).parent / "prompts" / "feedback.md").read_text(encoding="utf-8")
        self.assertIn("now waits for someone", text)
        self.assertIn("a `follow_up_date`", text)
        self.assertIn("suggested check, not a deadline", text)
