"""C01: a requirement change is carried out, and a delivery records only the requirements its
run actually had.

* Sending a revision checks the card itself, not our last sync: when the worker already
  submitted (the card is in review, whatever our cache says), the version on the card was built
  without the revision and its review is reopened. A comment that lands while the card waits in
  review is written on the card but adopted by no run.
* A delivery's requirement version comes from the order of card events: a requirement message
  counts for a result only when its comment is on the card before the result event (in the card
  body, read when a run starts, or steered into the running worker). "Sent before the sync that
  recorded the delivery" is not enough.
* Covered orders: send -> sync, sync -> send, the worker submitting while our comment is being
  posted, lost comment/transition responses retried later.

Kanban is ``FakeKanban`` (test double); tests/integration runs the same flow on Hermes.
"""

from todo_manager import delegation
from tests.test_delegation_binding import BindingBase

REVISION = "必须增加新的附件"


class RevisionOnTheCardTests(BindingBase):
    def queued_revision(self):
        did, card = self.running()
        mid = delegation.add_message(self.conn, did, "revision", REVISION, feedback_id="fbk_rev")
        return did, card, mid

    def v1_rv(self, did):
        return delegation.deliveries(self.conn, did)[0]["requirement_version"]

    def test_a_revision_sent_after_the_worker_submitted_reopens_the_review(self):
        did, card, mid = self.queued_revision()
        self.kb.worker_review(card, "v1", {"deliverable_markdown": "no attachment"})   # our cache: running
        self.send()
        self.sync()
        self.assertEqual(len(self.events(card, "review_reopened")), 1, "the version without it goes back")
        self.assertEqual(self.msg(mid)["status"], "sent")
        self.assertEqual(self.v1_rv(did), 1, "v1 was built without the revision")
        self.assertEqual(self.d(did)["requirement_version"], 2)
        self.kb.worker_start(card)
        self.kb.worker_review(card, "v2", {"deliverable_markdown": "with attachment"})
        self.sync()
        self.assertEqual(delegation.deliveries(self.conn, did)[1]["requirement_version"], 2)

    def test_the_same_when_the_sync_runs_first(self):
        did, card, mid = self.queued_revision()
        self.kb.worker_review(card, "v1", {"deliverable_markdown": "no attachment"})
        self.sync()
        self.send()
        self.assertEqual(len(self.events(card, "review_reopened")), 1)
        self.assertEqual(self.msg(mid)["status"], "sent")
        self.assertEqual(self.v1_rv(did), 1)

    def test_a_result_submitted_while_the_comment_is_posted_is_reopened(self):
        did, card, mid = self.queued_revision()
        self.kb.interleave["comment"] = lambda: self.kb.worker_review(card, "v1", {"deliverable_markdown": "x"})
        self.send()                                   # the card was running when we looked
        self.assertEqual(len(self.events(card, "review_reopened")), 1)
        self.sync()
        self.assertEqual(self.v1_rv(did), 1, "the comment landed after the result")

    def test_a_revision_steered_into_the_running_worker_is_not_reopened(self):
        did, card, mid = self.queued_revision()
        self.send()                                   # comment while running: Hermes steers it in
        self.assertEqual(self.msg(mid)["status"], "sent")
        self.kb.worker_review(card, "v1", {"deliverable_markdown": "with attachment"})
        self.sync()
        self.assertEqual(self.events(card, "review_reopened"), [])
        self.assertEqual(self.v1_rv(did), 2, "the comment was on the card before the result")

    def test_a_lost_comment_response_with_the_result_in_between_reopens_on_retry(self):
        did, card, mid = self.queued_revision()
        self.kb.interleave["comment"] = lambda: self.kb.worker_review(card, "v1", {"deliverable_markdown": "x"})
        self.kb.lose_response.add("comment")
        self.send()
        self.assertEqual(self.msg(mid)["status"], "sending")
        self.sync()
        self.assertEqual(self.v1_rv(did), 1, "an unconfirmed message is not counted")
        self.send()
        self.assertEqual(len([c for c in self.kb.comments if f"[tm:{mid}]" in c["body"]]), 1, "commented once")
        self.assertEqual(len(self.events(card, "review_reopened")), 1)
        self.assertEqual(self.msg(mid)["status"], "sent")

    def test_a_lost_comment_response_before_the_result_is_counted_on_retry(self):
        did, card, mid = self.queued_revision()
        self.kb.lose_response.add("comment")
        self.send()
        self.kb.worker_review(card, "v1", {"deliverable_markdown": "with attachment"})
        self.send()                                   # comment found; it was there before the result
        self.sync()
        self.assertEqual(self.events(card, "review_reopened"), [])
        self.assertEqual(self.msg(mid)["status"], "sent")
        self.assertEqual(self.v1_rv(did), 2, "recomputed once the comment's place is known")

    def test_a_lost_reopen_response_is_not_repeated(self):
        did, card, mid = self.queued_revision()
        self.kb.worker_review(card, "v1", {"deliverable_markdown": "no attachment"})
        self.kb.lose_response.add("reopen_review")
        self.send()
        self.assertEqual(self.msg(mid)["status"], "sending")
        self.sync()
        self.send()
        self.assertEqual(len(self.events(card, "review_reopened")), 1)
        self.assertEqual(self.msg(mid)["status"], "sent")

    def test_a_paused_delegation_does_not_reopen_until_resumed(self):
        did, card, mid = self.queued_revision()
        self.kb.worker_review(card, "v1", {"deliverable_markdown": "no attachment"})
        self.sync()
        delegation.add_message(self.conn, did, "pause", "先停一下", feedback_id="fbk_p")
        self.send()
        self.assertEqual(self.events(card, "review_reopened"), [])
        self.assertEqual(self.msg(mid)["status"], "waiting_capacity")


class SupplementPlacementTests(BindingBase):
    def test_a_supplement_written_while_the_version_waits_for_review_is_not_part_of_it(self):
        did, card = self.running()
        mid = delegation.add_message(self.conn, did, "supplement", "抄送 Bob", feedback_id="fbk_sup")
        self.kb.worker_review(card, "v1", {"deliverable_markdown": "Dear Alice"})
        self.send()                                   # lands on a card in review: nobody runs it
        self.sync()
        self.assertEqual(self.msg(mid)["status"], "sent")
        [v1] = delegation.deliveries(self.conn, did)
        self.assertEqual(v1["requirement_version"], 1)
        with self.assertRaises(delegation.DelegationError):
            delegation.record_approval(self.conn, did, v1["id"], "send_email", "", "fbk_ok")

    def test_supplements_in_the_card_body_count_for_the_first_run(self):
        did = self.delegate()
        delegation.add_message(self.conn, did, "supplement", "抄送 Bob", feedback_id="fbk_sup")
        self.tick()
        card = self.d(did)["card_id"]
        self.kb.worker_start(card)
        self.kb.worker_review(card, "v1", {"deliverable_markdown": "Dear Alice, cc Bob"})
        self.tick()
        self.assertEqual(delegation.deliveries(self.conn, did)[0]["requirement_version"], 2)
