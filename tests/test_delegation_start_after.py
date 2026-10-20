"""Agent work the user wants started later ("do it after 5pm").

The card is created parked in Hermes' ``scheduled`` state (never dispatchable in between), and
the feedback gate's deterministic Kanban step lets it go once the time has passed: no model run
and no worker until then. Kanban is ``FakeKanban`` (test double).
"""

import contextlib
import io
from datetime import timedelta

from todo_manager import cli, clock, delegation, ops, store
from tests.test_delegation_reliability import Base

# T0 is 13:00 in Auckland; the user said "after 19:00".
LATER = "2026-10-05T06:00:00.000Z"


class StartAfterTests(Base):
    def tick(self):
        delegation.send_messages(self.conn, self.cfg, self.kb)
        delegation.submit_pending(self.conn, self.cfg, self.kb)
        delegation.start_due(self.conn, self.cfg, self.kb)
        delegation.sync_cards(self.conn, self.cfg, self.kb)

    def test_the_card_is_parked_until_the_time_and_never_dispatched_before(self):
        did = self.delegate(start_after=LATER)
        self.tick()
        card = self.d(did)["card_id"]
        self.assertEqual(self.kb.cards[card]["status"], "scheduled")
        self.assertEqual([e["kind"] for e in self.kb.events if e["task_id"] == card][:2], ["created", "scheduled"],
                         "parked in the same create, before any dispatcher tick could see it ready")
        self.assertIn(f"SCHEDULED_UNTIL={LATER}", self.events(card, "scheduled")[0]["payload"]["reason"])
        self.assertEqual(self.d(did)["status"], "queued")
        for _ in range(3):
            clock.advance(minutes=5)
            self.tick()
            self.assertNotIn(card, self.kb.dispatch_tick())
        self.assertEqual(self.kb.cards[card]["status"], "scheduled")

    def test_the_gate_lets_it_go_once_the_time_has_passed(self):
        did = self.delegate(start_after=LATER)
        self.tick()
        card = self.d(did)["card_id"]
        clock.advance(hours=6, minutes=1)
        self.tick()
        self.assertIn(card, self.kb.dispatch_tick())
        self.assertIsNone(self.d(did)["start_after"])
        self.kb.worker_start(card)
        self.tick()
        self.assertEqual(self.d(did)["status"], "running")
        clock.advance(minutes=5)
        self.tick()
        self.assertEqual(len(self.events(card, "unblocked")), 1, "released once")

    def test_a_worker_block_after_the_start_is_not_lifted(self):
        did = self.delegate(start_after=LATER)
        self.tick()
        card = self.d(did)["card_id"]
        clock.advance(hours=7)
        self.tick()
        self.kb.worker_start(card)
        self.kb.worker_block(card, "Deck A or B?")
        self.tick()
        clock.advance(minutes=5)
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "blocked")
        self.assertEqual(self.d(did)["status"], "blocked")

    def test_a_time_already_passed_starts_at_once(self):
        did = self.delegate(start_after=clock.iso(clock.now() - timedelta(minutes=1)))
        self.tick()
        self.assertIn(self.d(did)["card_id"], self.kb.dispatch_tick())
        self.assertIsNone(self.d(did)["start_after"])

    def test_the_task_says_when_the_agent_starts(self):
        self.delegate(start_after=LATER)
        note = store.get_task(self.conn, self.task)["change_note"]
        self.assertIn("10-05 19:00", note, "in the workspace time zone")

    def test_a_pause_holds_the_start_and_a_resume_lets_the_time_apply(self):
        did = self.delegate(start_after=LATER)
        self.tick()
        card = self.d(did)["card_id"]
        delegation.add_message(self.conn, did, "pause", "wait")
        self.tick()
        clock.advance(hours=7)
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "scheduled", "paused: not released")
        delegation.add_message(self.conn, did, "resume", "go on")
        self.tick()
        self.tick()
        self.assertIn(card, self.kb.dispatch_tick())

    def test_a_lost_create_response_is_found_and_stays_parked(self):
        did = self.delegate(start_after=LATER)
        self.kb.lose_response.add("create")
        self.tick()
        self.tick()
        card = self.d(did)["card_id"]
        self.assertEqual(self.kb.cards[card]["status"], "scheduled")
        self.assertNotIn(card, self.kb.dispatch_tick())

    def test_a_staged_card_left_by_an_interrupted_create_is_parked_not_released(self):
        """Interruption (test double): the create stopped after the staging block, before the park."""
        did = self.delegate(start_after=LATER)
        self.kb.interrupt_schedule = True
        self.tick()
        self.tick()
        card = self.d(did)["card_id"]
        self.assertEqual(self.kb.cards[card]["status"], "scheduled")
        self.assertEqual(self.d(did)["status"], "queued")

    def test_cancel_before_the_start_archives_the_parked_card(self):
        did = self.delegate(start_after=LATER)
        self.tick()
        card = self.d(did)["card_id"]
        delegation.add_message(self.conn, did, "cancel", "not needed")
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "archived")
        clock.advance(hours=7)
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "archived")

    def test_a_local_time_is_in_the_workspace_time_zone(self):
        self.assertEqual(ops.start_time(self.cfg, "2026-10-05 19:00"), LATER)
        self.assertEqual(ops.start_time(self.cfg, "2026-10-05T19:00:00+13:00"), LATER)

    def test_cli_takes_the_start_time(self):
        args = ["--home", str(self.cfg.home), "delegate", "create", "--task", self.task, "--title", "起草",
                "--allow", "read,analyze,draft", "--human", "--brief", "Draft a reply", "--no-submit",
                "--start-after", "2026-10-05T19:00+13:00"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(args), 0)
        [d] = delegation.for_task(self.conn, self.task)
        self.assertEqual(d["start_after"], LATER)

    def test_cli_refuses_an_unreadable_time(self):
        args = ["--home", str(self.cfg.home), "delegate", "create", "--task", self.task, "--title", "起草",
                "--allow", "read,analyze,draft", "--human", "--brief", "Draft a reply", "--no-submit",
                "--start-after", "after five"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(cli.main(args), 0)
        self.assertEqual(delegation.for_task(self.conn, self.task), [])
