"""A cancel settles every approval already handed to a run: cancelling stops further work, it
does not show that an approved external action did not happen.

The approval was delivered normally (message sent, ``consumed_at`` set); its run then ended or
was still going, and the sync had not read that yet when the cancel - processed first in
``gate_feedback.sync_all`` - archived the card. Cancelled delegations are not synced again, so
the cancel settles each handed approval whose outcome is still open from the card's events
(all of them, not only those the sync cursor covers), for the run that approval went to:

* that run delivered a result -> ``reported`` (no unknown notice; the cancel stands);
* that run ended without one, or was still going (or waiting for an answer) when the card was
  archived -> ``unknown``: the user is told once; no pause, no resume, nothing replayed;
* no run started after the approval reached the card (Hermes claims a card before it starts a
  worker) -> ``not_run``.

An outcome already settled is kept. Kanban is ``FakeKanban`` (test double); the installed
Hermes is covered in ``tests/integration/test_hermes_kanban.py``.
"""

from todo_manager import db, delegation, health, store
from todo_manager.kanban import KanbanError
from tests.test_approval_late_confirmation import LateConfirmationBase


class CancelBase(LateConfirmationBase):
    def handed(self, did, n=1):
        """``n`` approvals for the latest version, delivered normally."""
        for i in range(n):
            self.approve(did, ("send_email", "comment_github")[i], "", f"fbk_{i}")
        self.sync_all()
        rows = self.approvals(did)
        self.assertTrue(all(r["mstatus"] == "sent" and r["consumed_at"] for r in rows))
        return rows

    def cancel(self, did):
        delegation.add_message(self.conn, did, "cancel", "不用做了", feedback_id="fbk_c")

    def outcomes(self, did):
        return [r["outcome"] for r in self.approvals(did)]

    def assert_cancelled(self, did, card):
        self.assertEqual(self.kb.cards[card]["status"], "archived")
        self.assertEqual(self.d(did)["status"], "cancelled")
        self.assertEqual(self.auto_pauses(did), [], "no pause after a cancel")
        self.assertEqual(self.events(card, "unblocked"), [], "nothing resumed")
        self.assertEqual(len(self.events(card, "review_reopened")), 1, "nothing reopened again")


class RunEndedBeforeTheCancelTests(CancelBase):
    def test_a_crash_not_read_yet_is_unknown(self):
        did, card = self.delivered()
        self.handed(did)
        self.kb.worker_start(card)
        self.kb.worker_crash(card)
        self.cancel(did)
        self.sync_all()
        self.sync_all()
        self.assertEqual(self.outcomes(did), ["unknown"])
        self.assertEqual(len(self.unknown_notices()), 1)
        self.assert_cancelled(did, card)
        a = self.approvals(did)[0]
        self.assertTrue(any(a["id"] in p for p in health.report(self.conn, self.cfg)["problems"]))

    def test_a_result_not_read_yet_is_reported_and_the_cancelled_task_stays_cancelled(self):
        did, card = self.delivered()
        self.handed(did)
        self.kb.worker_start(card)
        self.deliver_next(card)
        store.update_task(self.conn, self.task, {"status": "cancelled"}, actor="human", origin="cli")
        self.sync_all()
        self.sync_all()
        self.assertEqual(self.outcomes(did), ["reported"])
        self.assertEqual(self.unknown_notices(), [])
        self.assert_cancelled(did, card)
        self.assertEqual(store.get_task(self.conn, self.task)["status"], "cancelled")

    def test_every_approval_of_the_run_is_settled(self):
        did, card = self.delivered()
        self.handed(did, 2)
        self.kb.worker_start(card)
        self.kb.worker_crash(card)
        self.cancel(did)
        self.sync_all()
        self.assertEqual(self.outcomes(did), ["unknown", "unknown"])
        self.assertEqual(len(self.unknown_notices()), 2, "one per approved action")
        self.assert_cancelled(did, card)

    def test_the_notice_does_not_promise_a_pause(self):
        did, card = self.delivered()
        self.handed(did)
        self.kb.worker_start(card)
        self.kb.worker_crash(card)
        self.cancel(did)
        self.sync_all()
        [text] = self.unknown_notices()
        self.assertNotIn("已暂停", text)


class RunNotEndedAtTheCancelTests(CancelBase):
    def test_a_run_still_going_when_the_card_is_archived_is_unknown(self):
        did, card = self.delivered()
        self.handed(did)
        self.kb.worker_start(card)
        self.cancel(did)
        self.sync_all()
        self.sync_all()
        self.assertEqual(self.outcomes(did), ["unknown"], "archiving does not show it was not done")
        self.assertEqual(len(self.unknown_notices()), 1)
        self.assert_cancelled(did, card)

    def test_a_run_waiting_for_an_answer_is_unknown(self):
        did, card = self.delivered()
        self.handed(did)
        self.kb.worker_start(card)
        self.kb.worker_block(card, "发给哪个地址？")
        self.cancel(did)
        self.sync_all()
        self.assertEqual(self.outcomes(did), ["unknown"], "it may have acted before asking")
        self.assert_cancelled(did, card)

    def test_no_run_started_after_the_approval_is_not_run(self):
        did, card = self.delivered()
        self.handed(did)
        self.cancel(did)                                 # the card was back in ready, never claimed
        self.sync_all()
        self.sync_all()
        self.assertEqual(self.outcomes(did), ["not_run"])
        self.assertEqual(self.unknown_notices(), [])
        self.assertFalse([p for p in health.report(self.conn, self.cfg)["problems"] if "unknown outcome" in p])
        self.assert_cancelled(did, card)


class OtherRunsAndSettledOutcomesTests(CancelBase):
    def test_an_outcome_already_settled_is_kept(self):
        did, card = self.delivered()
        self.handed(did)
        self.kb.worker_start(card)
        self.deliver_next(card)
        self.sync_all()                                  # read: reported
        self.handed(did)                                 # a second approval, for v2
        self.kb.worker_start(card)
        self.kb.worker_crash(card)
        self.cancel(did)
        self.sync_all()
        self.assertEqual(self.outcomes(did), ["reported", "unknown"])
        self.assertEqual(len(self.unknown_notices()), 1)

    def test_an_earlier_failed_run_is_not_taken_for_the_approval(self):
        did = self.delegate()
        self.tick()
        card = self.d(did)["card_id"]
        self.kb.worker_start(card)
        self.kb.worker_crash(card)                       # before v1: not this approval's run
        self.tick()
        delegation.add_message(self.conn, did, "answer", "重试", feedback_id="fbk_retry")
        self.tick()
        self.kb.worker_start(card)
        self.kb.worker_review(card, "v1", {"deliverable_markdown": "Dear Alice v1"})
        self.tick()
        self.handed(did)
        self.cancel(did)
        self.sync_all()
        self.assertEqual(self.outcomes(did), ["not_run"])
        self.assertEqual(self.unknown_notices(), [])


class RetriesAndRestartsTests(CancelBase):
    def crashed_then_cancelled(self, n=1):
        did, card = self.delivered()
        self.handed(did, n)
        self.kb.worker_start(card)
        self.kb.worker_crash(card)
        self.cancel(did)
        return did, card

    def test_repeated_ticks_and_a_reopened_database_change_nothing(self):
        did, card = self.crashed_then_cancelled()
        self.sync_all()
        settled = self.state(did, card)
        self.conn.close()
        self.conn = db.connect(self.cfg.db_path)
        for _ in range(3):
            self.sync_all()
        self.assertEqual(self.state(did, card), settled)
        self.assertEqual(self.outcomes(did), ["unknown"])
        self.assertEqual(len(self.unknown_notices()), 1)

    def test_a_lost_archive_response(self):
        did, card = self.crashed_then_cancelled(2)
        self.kb.lose_response.add("archive")
        self.sync_all()
        self.sync_all()
        self.sync_all()
        self.assertEqual(self.outcomes(did), ["unknown", "unknown"])
        self.assertEqual(len(self.unknown_notices()), 2)
        self.assert_cancelled(did, card)
        self.assertEqual(len(self.events(card, "archived")), 1)

    def test_the_card_cannot_be_read_after_the_archive(self):
        did, card = self.crashed_then_cancelled()
        real = self.kb.transition

        def transition(card_id, action, anchor=None, summary=None):
            result = real(card_id, action, anchor, summary)
            if action == "archive":
                self.kb.transition = real
                self.kb.fail_next["snapshot"] = KanbanError("bridge unavailable")
            return result

        self.kb.transition = transition
        self.sync_all()
        self.assertNotEqual(self.d(did)["status"], "cancelled", "not finished before the card was read")
        self.sync_all()
        self.assertEqual(self.outcomes(did), ["unknown"])
        self.assertEqual(len(self.unknown_notices()), 1)
        self.assert_cancelled(did, card)
        self.assertEqual(len(self.events(card, "archived")), 1)

    def test_a_process_restart_between_the_archive_and_its_record(self):
        did, card = self.crashed_then_cancelled()
        real = self.kb.transition

        def transition(card_id, action, anchor=None, summary=None):
            result = real(card_id, action, anchor, summary)
            if action == "archive":
                self.kb.transition = real
                raise SystemExit("process killed")
            return result

        self.kb.transition = transition
        with self.assertRaises(SystemExit):
            self.sync_all()
        self.conn.close()
        self.conn = db.connect(self.cfg.db_path)
        self.sync_all()
        self.sync_all()
        self.assertEqual(self.outcomes(did), ["unknown"])
        self.assertEqual(len(self.unknown_notices()), 1)
        self.assert_cancelled(did, card)


class PauseAndLateConfirmationTests(CancelBase):
    def test_a_queued_pause_is_voided_and_not_replaced(self):
        did, card = self.delivered()
        self.handed(did)
        self.kb.worker_start(card)
        self.kb.worker_crash(card)
        delegation.add_message(self.conn, did, "pause", "先停", feedback_id="fbk_p")
        self.cancel(did)
        self.sync_all()
        self.assertEqual(self.outcomes(did), ["unknown"])
        pauses = self.conn.execute("SELECT status FROM delegation_messages WHERE delegation_id=? AND kind='pause'",
                                   (did,)).fetchall()
        self.assertEqual([p["status"] for p in pauses], ["superseded"], "the cancel voids it; none is added")
        self.assert_cancelled(did, card)
