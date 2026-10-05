"""C08: a pause does not stop the read-only lookup of a card whose create response was lost.

* ``reconcile`` (the create may have happened) always looks the card up, paused or not; the card
  found is linked and the pause is applied to it right away (a ready/running card is blocked by
  our pause before it runs on; a card in review/blocked keeps its state and transitions wait).
* Confirmed not created: the delegation stays unsubmitted (``pending_submit``) and nothing is
  created while the pause is requested.
* A lookup failure keeps ``reconcile`` and retries; no second card is ever created.
* "Pause requested" is not reported as "paused": a card blocked by the worker's question stays
  ``blocked``.

Kanban is ``FakeKanban`` (test double).
"""

from todo_manager import delegation
from todo_manager.kanban import KanbanError
from tests.test_delegation_binding import BindingBase


class PauseAfterLostCreateTests(BindingBase):
    def lost_create(self):
        did = self.delegate()
        self.kb.lose_response.add("create")
        delegation.submit_pending(self.conn, self.cfg, self.kb)
        self.assertEqual(self.d(did)["status"], "reconcile")
        self.assertIsNone(self.d(did)["card_id"])
        [card] = self.kb.cards
        return did, card

    def pause(self, did):
        return delegation.add_message(self.conn, did, "pause", "先停一下", feedback_id="fbk_p")

    def test_a_ready_card_is_found_and_paused_before_it_runs(self):
        did, card = self.lost_create()
        mid = self.pause(did)
        self.tick()
        self.assertEqual(self.d(did)["card_id"], card)
        self.assertEqual(len(self.kb.cards), 1, "no second card")
        self.assertEqual(self.kb.cards[card]["status"], "blocked", "blocked by our pause")
        self.assertEqual(self.d(did)["status"], "paused")
        self.assertEqual(self.msg(mid)["status"], "sent")

    def test_a_card_that_started_running_meanwhile_is_paused(self):
        did, card = self.lost_create()
        self.kb.worker_start(card)
        self.pause(did)
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "blocked")
        self.assertEqual(self.d(did)["status"], "paused")
        self.assertTrue([c for c in self.kb.comments if "PAUSE" in c["body"]])

    def test_a_card_in_review_is_linked_and_its_transitions_wait(self):
        did, card = self.lost_create()
        self.kb.worker_start(card)
        self.kb.worker_review(card, "v1", {"deliverable_markdown": "Dear Alice"})
        self.pause(did)
        self.tick()
        self.assertEqual(self.d(did)["status"], "review")
        self.assertEqual(len(delegation.deliveries(self.conn, did)), 1)
        rev = delegation.add_message(self.conn, did, "revision", "改正式一点", feedback_id="fbk_r")
        self.tick()
        self.assertEqual(self.events(card, "review_reopened"), [], "paused: no new run")
        self.assertEqual(self.msg(rev)["status"], "waiting_capacity")

    def test_a_card_blocked_by_a_question_is_not_reported_as_paused(self):
        did, card = self.lost_create()
        self.kb.worker_start(card)
        self.kb.worker_block(card, "周四还是周五？")
        self.pause(did)
        self.tick()
        self.assertEqual(self.d(did)["status"], "blocked", "the question is open; the pause is only requested")
        self.assertEqual(self.d(did)["pause_requested"], 1)
        self.assertIn("pause requested", delegation.summary_line(self.d(did)))

    def test_an_archived_card_ends_it_and_the_pause_is_not_used(self):
        did, card = self.lost_create()
        self.kb.archive(card)
        mid = self.pause(did)
        self.tick()
        self.assertEqual(self.d(did)["status"], "cancelled")
        self.assertEqual(self.msg(mid)["status"], "superseded")

    def test_a_failed_lookup_keeps_it_in_reconcile(self):
        did, card = self.lost_create()
        self.pause(did)
        self.kb.fail_next["find"] = KanbanError("bridge down")
        delegation.submit_pending(self.conn, self.cfg, self.kb)
        self.assertEqual(self.d(did)["status"], "reconcile")
        self.assertIn("lookup failed", self.d(did)["last_error"])
        self.tick()
        self.assertEqual(self.d(did)["card_id"], card)
        self.assertEqual(self.d(did)["status"], "paused")
        self.assertEqual(len(self.kb.cards), 1)

    def test_confirmed_not_created_stays_unsubmitted_while_paused(self):
        did = self.delegate()
        self.kb.fail_next["create"] = KanbanError("bridge timeout during create", uncertain=True)
        delegation.submit_pending(self.conn, self.cfg, self.kb)
        self.assertEqual(self.d(did)["status"], "reconcile")
        self.assertEqual(self.kb.cards, {})
        self.pause(did)
        self.tick()
        self.tick()
        self.assertEqual(self.kb.cards, {}, "nothing created while the pause is requested")
        self.assertEqual(self.d(did)["status"], "pending_submit")
        self.assertIn("pause requested", delegation.summary_line(self.d(did)))
        delegation.add_message(self.conn, did, "resume", "继续", feedback_id="fbk_r")
        self.tick()
        self.assertEqual(len(self.kb.cards), 1)
        self.assertEqual(self.d(did)["status"], "queued")


class PauseRequestedIsNotPausedTests(BindingBase):
    def test_a_pause_on_a_card_waiting_for_an_answer_keeps_it_blocked(self):
        did, card = self.running()
        self.kb.worker_block(card, "周四还是周五？")
        self.tick()
        delegation.add_message(self.conn, did, "pause", "先停一下", feedback_id="fbk_p")
        self.tick()
        self.assertEqual(self.d(did)["status"], "blocked", "the open question is still what the card waits for")
        self.assertIn("pause requested", delegation.summary_line(self.d(did)))

    def test_our_pause_block_is_paused(self):
        did, card = self.running()
        delegation.add_message(self.conn, did, "pause", "先停一下", feedback_id="fbk_p")
        self.tick()
        self.assertEqual(self.d(did)["status"], "paused")
        self.assertNotIn("pause requested", delegation.summary_line(self.d(did)))
