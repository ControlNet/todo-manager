"""An approval confirmed as delivered only after the sync has read what its run did.

The reopen that hands approvals to the worker takes effect on the card, but its response is
lost (``KanbanError(uncertain=True)``): the approval messages stay ``sending`` and the
approvals do not count as handed yet. Within the same gate tick (``gate_feedback.sync_all``:
send_messages -> submit_pending -> sync_cards) the worker starts and ends, and the sync reads
that end and moves its event cursor past it. The next tick's retry confirms the reopen on the
card; the outcome of the run the approvals were handed to is then settled from the card's
events (the first run end or result after that reopen and after the approval's comment), not
lost behind the cursor:

* the run ended without a result -> ``unknown``: the user is told, the delegation is paused,
  nothing is replayed;
* the run delivered the next version -> ``reported``, no unknown notice, no pause.

Kanban is ``FakeKanban`` (test double); the interleaving is made by wrapping its
``transition``. Real Hermes coverage of the same interleaving is in
``tests/integration/test_hermes_kanban.py``.
"""

from todo_manager import db, delegation, gate_feedback, health
from todo_manager.kanban import KanbanError
from tests.test_delegation_binding import BindingBase

UNKNOWN = "结果未知"


class LateConfirmationBase(BindingBase):
    def sync_all(self):
        gate_feedback.sync_all(self.conn, self.cfg, notion=None, kanban=self.kb, notifier=None)

    def latest(self, did):
        return delegation.deliveries(self.conn, did)[-1]["id"]

    def approve(self, did, action="send_email", detail="发给 Alice", feedback="fbk_a"):
        """An approval of the latest delivered version."""
        return delegation.record_approval(self.conn, did, self.latest(did), action, detail, feedback)

    def lose_reopen_response(self, card, worker, unavailable=False):
        """The reopen lands on the card, the worker runs (``worker``: what it does), then the
        response of the reopen is lost. ``unavailable``: the next bridge call fails too - with
        several approvals, the next one's message would otherwise retry in the same pass and
        confirm the group before the sync."""
        real = self.kb.transition

        def transition(card_id, action, anchor=None, summary=None):
            result = real(card_id, action, anchor, summary)
            if action == "reopen_review" and result["outcome"] == "done":
                self.kb.transition = real
                self.kb.worker_start(card)
                worker(card)
                if unavailable:
                    self.kb.fail_next["snapshot"] = KanbanError("bridge unavailable")
                raise KanbanError("lost response", uncertain=True)
            return result

        self.kb.transition = transition
        return real

    def crash(self, card):
        self.kb.worker_crash(card)

    def deliver_next(self, card):
        self.kb.worker_review(card, "v2", {"deliverable_markdown": "Dear Alice v2",
                                           "external_actions_done": ["send_email"]})

    def interleave(self, card, worker, unavailable=False):
        """Tick 1 with the lost response and the run; ticks 2 and 3 as usual."""
        real = self.lose_reopen_response(card, worker, unavailable)
        self.sync_all()
        self.kb.transition = real
        self.sync_all()
        self.sync_all()

    def approvals(self, did):
        return self.conn.execute("SELECT a.*, m.status AS mstatus FROM approvals a JOIN delegation_messages m"
                                 " ON m.approval_id=a.id WHERE a.delegation_id=? ORDER BY a.granted_at, a.rowid",
                                 (did,)).fetchall()

    def unknown_notices(self):
        return [t for t in self.outbox_texts() if UNKNOWN in t]

    def auto_pauses(self, did):
        return self.conn.execute("SELECT * FROM delegation_messages WHERE delegation_id=? AND kind='pause'"
                                 " AND cause='approval_unknown'", (did,)).fetchall()

    def approved_comments(self, card):
        return [c for c in self.kb.comments if c["task_id"] == card and "APPROVED" in c["body"]]

    def state(self, did, card):
        """Everything a repeated tick must leave unchanged."""
        return ([tuple(r) for r in self.approvals(did)],
                [tuple(r) for r in self.conn.execute("SELECT id, kind, status FROM delegation_messages"
                                                     " WHERE delegation_id=? ORDER BY rowid", (did,))],
                len(self.outbox_texts()), len(self.kb.comments), len(self.kb.events))


class RunEndedWithoutReportTests(LateConfirmationBase):
    def test_a_crash_read_before_the_late_confirmation_is_unknown_and_paused(self):
        did, card = self.delivered()
        self.approve(did)
        self.interleave(card, self.crash)
        [a] = self.approvals(did)
        self.assertEqual((a["mstatus"], bool(a["consumed_at"])), ("sent", True))
        self.assertEqual(a["outcome"], "unknown", "the run it was handed to ended without reporting")
        self.assertEqual(self.d(did)["pause_requested"], 1, "no automatic retry")
        self.assertEqual(len(self.auto_pauses(did)), 1)
        self.assertEqual(len(self.unknown_notices()), 1, "the user is told once")
        self.assertTrue(any(a["id"] in p for p in health.report(self.conn, self.cfg)["problems"]),
                        "health lists it for a manual check")
        self.assertEqual(len(self.approved_comments(card)), 1, "the approval is not posted again")
        self.assertEqual(len(self.events(card, "review_reopened")), 1, "the review is not reopened again")

    def test_with_retries_left_the_card_is_paused_before_it_runs_again(self):
        self.cfg.data.setdefault("kanban", {})["max_retries"] = 2
        did, card = self.delivered()
        self.approve(did)
        self.interleave(card, self.crash)
        self.assertEqual(self.approvals(did)[0]["outcome"], "unknown")
        self.assertNotIn(card, self.kb.dispatch_tick(), "the retry does not start: the action may have happened")
        self.assertEqual(self.d(did)["status"], "paused")
        self.assertEqual(len(self.events(card, "claimed")), 2, "only the run that had the approval ran")

    def test_every_approval_handed_to_the_run_is_settled(self):
        did, card = self.delivered()
        self.approve(did, "send_email", "发给 Alice", "fbk_a")
        self.approve(did, "comment_github", "在 issue 下回复", "fbk_b")
        real = self.lose_reopen_response(card, self.crash, unavailable=True)
        self.sync_all()
        self.assertEqual([r["mstatus"] for r in self.approvals(did)], ["sending", "sending"],
                         "neither confirmed before the sync read the crash")
        self.kb.transition = real
        self.sync_all()
        self.sync_all()
        rows = self.approvals(did)
        self.assertEqual([r["outcome"] for r in rows], ["unknown", "unknown"])
        self.assertEqual(len(self.unknown_notices()), 2, "one notice per approved action")
        self.assertEqual(len(self.auto_pauses(did)), 1, "one pause for the delegation")
        self.assertEqual(len(self.approved_comments(card)), 2)
        self.assertEqual(len(self.events(card, "review_reopened")), 1)


class OtherOrderingsTests(LateConfirmationBase):
    """The same lost response when the sync has NOT read the run's end before the retry: the
    sync settles it as usual, once."""

    def test_the_sync_failed_in_the_same_tick(self):
        did, card = self.delivered()
        self.approve(did)
        self.interleave(card, self.crash, unavailable=True)    # tick 1's sync cannot read the card
        self.assertEqual(self.approvals(did)[0]["outcome"], "unknown")
        self.assertEqual(len(self.unknown_notices()), 1)
        self.assertEqual(len(self.auto_pauses(did)), 1)

    def test_the_run_was_still_going_when_the_retry_confirmed_it(self):
        did, card = self.delivered()
        self.approve(did)
        self.interleave(card, lambda card: None)                # started, not ended
        self.assertIsNone(self.approvals(did)[0]["outcome"])
        self.kb.worker_crash(card)
        self.sync_all()
        self.sync_all()
        self.assertEqual(self.approvals(did)[0]["outcome"], "unknown")
        self.assertEqual(len(self.unknown_notices()), 1)
        self.assertEqual(len(self.auto_pauses(did)), 1)


class RunReportedTests(LateConfirmationBase):
    def test_a_result_read_before_the_late_confirmation_is_reported(self):
        did, card = self.delivered()
        self.approve(did)
        self.interleave(card, self.deliver_next)
        [a] = self.approvals(did)
        self.assertEqual((a["mstatus"], a["outcome"]), ("sent", "reported"))
        self.assertEqual(self.unknown_notices(), [])
        self.assertEqual(self.d(did)["pause_requested"], 0)
        self.assertEqual(self.auto_pauses(did), [])
        self.assertEqual(len(delegation.deliveries(self.conn, did)), 2)

    def test_every_approval_of_the_run_is_reported(self):
        did, card = self.delivered()
        self.approve(did, "send_email", "发给 Alice", "fbk_a")
        self.approve(did, "comment_github", "在 issue 下回复", "fbk_b")
        self.interleave(card, self.deliver_next, unavailable=True)
        self.assertEqual([r["outcome"] for r in self.approvals(did)], ["reported", "reported"])
        self.assertEqual(self.unknown_notices(), [])


class RecoveryIsIdempotentTests(LateConfirmationBase):
    def test_repeated_ticks_and_a_reopened_database_change_nothing(self):
        did, card = self.delivered()
        self.approve(did)
        real = self.lose_reopen_response(card, self.crash)
        self.sync_all()
        self.kb.transition = real
        self.conn.close()                                # process restart before the retry
        self.conn = db.connect(self.cfg.db_path)
        self.sync_all()
        self.sync_all()
        settled = self.state(did, card)
        self.assertEqual(self.approvals(did)[0]["outcome"], "unknown")
        self.conn.close()                                # and again after it
        self.conn = db.connect(self.cfg.db_path)
        for _ in range(3):
            self.sync_all()
        self.assertEqual(self.state(did, card), settled)
        self.assertEqual(len(self.unknown_notices()), 1)

    def test_a_settled_outcome_is_not_overwritten_by_a_later_retry(self):
        did, card = self.delivered()
        self.approve(did)
        self.interleave(card, self.deliver_next)
        self.assertEqual(self.approvals(did)[0]["outcome"], "reported")
        # The next version's run fails later: it is not the run the approval was handed to.
        delegation.add_message(self.conn, did, "revision", "再改一下语气", feedback_id="fbk_rev")
        self.sync_all()
        self.kb.worker_start(card)
        self.kb.worker_crash(card)
        self.sync_all()
        self.sync_all()
        self.assertEqual(self.approvals(did)[0]["outcome"], "reported")
        self.assertEqual(self.unknown_notices(), [])


class OtherRunsTests(LateConfirmationBase):
    def test_an_earlier_failed_run_is_not_taken_for_this_approval(self):
        did = self.delegate()
        self.tick()
        card = self.d(did)["card_id"]
        self.kb.worker_start(card)
        self.kb.worker_crash(card)                       # run 1 fails: parked for the user
        self.tick()
        delegation.add_message(self.conn, did, "answer", "重试", feedback_id="fbk_retry")
        self.tick()
        self.kb.worker_start(card)
        self.kb.worker_review(card, "v1", {"deliverable_markdown": "Dear Alice v1"})
        self.tick()
        self.approve(did)
        self.interleave(card, self.deliver_next)
        [a] = self.approvals(did)
        self.assertEqual(a["outcome"], "reported", "run 1's crash came before the approval's run")
        self.assertEqual(self.unknown_notices(), [])
        self.assertEqual(self.auto_pauses(did), [])

    def test_an_earlier_approval_keeps_its_outcome_and_the_new_one_gets_its_own(self):
        did, card = self.delivered()
        self.approve(did, "send_email", "发给 Alice", "fbk_a")
        self.tick()                                      # handed normally
        self.kb.worker_start(card)
        self.deliver_next(card)
        self.tick()
        first = self.approvals(did)[0]
        self.assertEqual(first["outcome"], "reported")
        self.approve(did, "comment_github", "在 issue 下回复", "fbk_b")    # for v2
        self.interleave(card, self.crash)
        rows = self.approvals(did)
        self.assertEqual([r["outcome"] for r in rows], ["reported", "unknown"])
        self.assertEqual(len(self.unknown_notices()), 1)
        self.assertEqual(rows[0]["consumed_at"], first["consumed_at"])

    def test_the_result_of_the_approved_version_itself_is_not_its_report(self):
        did, card = self.delivered()
        self.approve(did)
        self.interleave(card, self.crash)
        self.assertNotEqual(self.approvals(did)[0]["outcome"], "reported",
                            "v1's own review event precedes the run the approval was handed to")


class CancelAndPauseTests(LateConfirmationBase):
    def test_a_cancel_queued_before_the_retry_still_goes_first(self):
        did, card = self.delivered()
        self.approve(did)
        real = self.lose_reopen_response(card, self.crash)
        self.sync_all()
        self.kb.transition = real
        delegation.add_message(self.conn, did, "cancel", "不用做了", feedback_id="fbk_c")
        self.sync_all()
        self.sync_all()
        self.assertEqual(self.kb.cards[card]["status"], "archived")
        self.assertEqual(self.d(did)["status"], "cancelled")
        [a] = self.approvals(did)
        self.assertEqual((a["mstatus"], a["outcome"]), ("sent", "unknown"),
                         "landed before the cancel: kept as history, its outcome still unknown")
        self.assertEqual(len(self.unknown_notices()), 1, "the user still learns it may have happened")
        self.assertEqual(self.auto_pauses(did), [], "the archive stops the card; no pause after a cancel")
        self.assertEqual(len(self.approved_comments(card)), 1)
        self.assertEqual(len(self.events(card, "review_reopened")), 1)

    def test_a_pause_requested_meanwhile_is_not_doubled(self):
        did, card = self.delivered()
        self.approve(did)
        real = self.lose_reopen_response(card, self.crash)
        self.sync_all()
        self.kb.transition = real
        delegation.add_message(self.conn, did, "pause", "先停一下", feedback_id="fbk_p")
        self.sync_all()
        self.sync_all()
        self.assertEqual(self.approvals(did)[0]["outcome"], "unknown")
        pauses = self.conn.execute("SELECT * FROM delegation_messages WHERE delegation_id=? AND kind='pause'",
                                   (did,)).fetchall()
        self.assertEqual(len(pauses), 1, "the user's pause already protects it")
        self.assertEqual(self.d(did)["pause_requested"], 1)
        self.assertEqual(len(self.unknown_notices()), 1)
