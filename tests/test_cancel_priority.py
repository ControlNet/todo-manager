"""C07: the user's latest cancel takes priority over queued messages that were not carried out.

* When a cancel is processed, every other message of the delegation that has not taken effect
  (answer, resume, accept, revision, approval, pause, supplement) is void and the user is told
  which; nothing is unblocked or reopened just to be archived.
* A message whose earlier attempt has an unknown outcome is checked on the card first: when it
  landed it stays as history (an accept that already completed the card makes the cancel too
  late, and the user is told); when it did not, it is void.
* A cancelled Task always gets a cancel, even when an accept is queued; an explicit cancel is
  never replaced by an accept, and no new answer/accept/... is queued while a cancel is pending.
* Reopening the Task before the cancel was processed withdraws the cancel and keeps the queue.
* Entry points: Notion status property, Matrix (apply-direct), CLI task update, explicit cancel
  message (CLI and Matrix).

Kanban is ``FakeKanban`` and Notion is ``FakeNotion`` (test doubles).
"""

import contextlib
import io

from todo_manager import cli, delegation, feedback_flow, store
from todo_manager.kanban import FakeKanban
from tests.test_delegation_binding import BindingBase
from tests.test_notion import NotionCase


class CancelFirstTests(BindingBase):
    def blocked(self):
        did, card = self.running()
        self.kb.worker_block(card, "截止日期是周四还是周五？")
        self.tick()
        self.assertEqual(self.d(did)["status"], "blocked")
        return did, card

    def cli(self, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            return cli.main(["--home", str(self.cfg.home), *args])

    def cancel_task_via_cli(self):
        self.assertEqual(self.cli("task", "update", self.task, "--set", "status=cancelled"), 0)

    def voided_notices(self):
        return [t for t in self.outbox_texts() if "未执行" in t or "not carried out" in t]

    def assert_archived_without(self, card, *kinds):
        self.assertEqual(self.kb.cards[card]["status"], "archived")
        for kind in kinds:
            self.assertEqual(self.events(card, kind), [], f"no {kind} before the archive")

    def test_a_queued_answer_is_not_sent_before_the_archive(self):
        did, card = self.blocked()
        mid = delegation.add_message(self.conn, did, "answer", "周五", feedback_id="fbk_a")
        self.cancel_task_via_cli()
        self.tick()
        self.assert_archived_without(card, "unblocked")
        self.assertEqual(self.msg(mid)["status"], "superseded")
        self.assertEqual(self.d(did)["status"], "cancelled")
        self.assertEqual(len(self.voided_notices()), 1)
        self.assertIn("周五", self.voided_notices()[0], "the user is told what was not carried out")

    def test_a_queued_accept_does_not_keep_the_cancel_out(self):
        did, card = self.delivered()
        mid = delegation.add_message(self.conn, did, "accept", "可以", feedback_id="fbk_acc")
        store.update_task(self.conn, self.task, {"status": "cancelled"}, actor="human", origin="cli")
        self.tick()
        self.assert_archived_without(card, "completed")
        self.assertEqual(self.msg(mid)["status"], "superseded")
        self.assertEqual(self.d(did)["status"], "cancelled")

    def test_matrix_cancel_while_an_answer_is_queued(self):
        did, card = self.blocked()
        mid = delegation.add_message(self.conn, did, "answer", "周五", feedback_id="fbk_a")
        version = store.get_task(self.conn, self.task)["version"]
        feedback_flow.apply_direct(self.conn, self.cfg, origin="matrix", text="这个不做了", source_id="$evt9", ops=[
            {"op": "update_task", "task": self.task, "based_on_version": version, "set": {"status": "cancelled"}}])
        self.tick()
        self.assert_archived_without(card, "unblocked")
        self.assertEqual(self.msg(mid)["status"], "superseded")

    def test_an_explicit_cancel_beats_a_queued_accept_and_resume(self):
        did, card = self.delivered()
        acc = delegation.add_message(self.conn, did, "accept", "可以", feedback_id="fbk_acc")
        self.assertEqual(self.cli("delegate", "message", did, "--kind", "cancel", "--body", "算了"), 0)
        self.tick()
        self.assert_archived_without(card, "completed")
        self.assertEqual(self.msg(acc)["status"], "superseded")

    def test_nothing_new_is_queued_behind_a_pending_cancel(self):
        did, card = self.delivered()
        feedback_flow.apply_direct(self.conn, self.cfg, origin="matrix", text="取消委派", source_id="$evt10", ops=[
            {"op": "message", "delegation": did, "kind": "cancel", "body": "取消"}])
        with self.assertRaises(delegation.DelegationError):
            delegation.add_message(self.conn, did, "accept", "可以")
        store.update_task(self.conn, self.task, {"status": "done"}, actor="human", origin="cli")
        self.tick()
        self.assert_archived_without(card, "completed")
        kinds = [r["kind"] for r in self.conn.execute("SELECT kind FROM delegation_messages WHERE delegation_id=?"
                                                      " AND kind != 'requirement'",
                                                      (did,))]
        self.assertEqual(kinds, ["cancel"], "the explicit cancel is not replaced by an accept")

    def test_a_queued_approval_is_revoked(self):
        did, card = self.delivered()
        v1 = delegation.deliveries(self.conn, did)[0]["id"]
        aid = delegation.record_approval(self.conn, did, v1, "send_email", "", "fbk_ok")
        store.update_task(self.conn, self.task, {"status": "cancelled"}, actor="human", origin="cli")
        self.tick()
        self.assert_archived_without(card, "review_reopened")
        self.assertIsNotNone(self.conn.execute("SELECT revoked_at FROM approvals WHERE id=?", (aid,)).fetchone()[0])

    def test_an_answer_in_flight_that_landed_stays_history(self):
        did, card = self.blocked()
        mid = delegation.add_message(self.conn, did, "answer", "周五", feedback_id="fbk_a")
        self.kb.lose_response.add("unblock")
        self.send()
        self.assertEqual(self.msg(mid)["status"], "sending")
        self.cancel_task_via_cli()
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "archived")
        self.assertEqual(len(self.events(card, "unblocked")), 1, "not repeated")
        self.assertEqual(self.msg(mid)["status"], "sent", "it did reach the card before the cancel")
        self.assertEqual(self.voided_notices(), [])

    def test_an_answer_in_flight_that_did_not_land_is_void(self):
        did, card = self.blocked()
        mid = delegation.add_message(self.conn, did, "answer", "周五", feedback_id="fbk_a")
        self.kb.lose_response.add("comment")              # comment posted, unblock never called
        self.send()
        self.assertEqual(self.msg(mid)["status"], "sending")
        self.cancel_task_via_cli()
        self.tick()
        self.assert_archived_without(card, "unblocked")
        self.assertEqual(self.msg(mid)["status"], "superseded")

    def test_an_accept_that_already_completed_the_card_makes_the_cancel_too_late(self):
        did, card = self.delivered()
        acc = delegation.add_message(self.conn, did, "accept", "可以", feedback_id="fbk_acc")
        self.kb.lose_response.add("complete")
        self.send()
        store.update_task(self.conn, self.task, {"status": "cancelled"}, actor="human", origin="cli")
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "done", "an accepted card is not archived")
        self.assertEqual(self.msg(acc)["status"], "sent")
        self.assertEqual(self.d(did)["status"], "ended")
        cancel = self.conn.execute("SELECT * FROM delegation_messages WHERE delegation_id=? AND kind='cancel'",
                                   (did,)).fetchone()
        self.assertEqual(cancel["status"], "superseded")
        self.assertTrue(self.unused_notices(), "the user is told the accept came first")

    def test_reopening_before_the_cancel_ran_keeps_the_queue(self):
        did, card = self.blocked()
        mid = delegation.add_message(self.conn, did, "answer", "周五", feedback_id="fbk_a")
        store.update_task(self.conn, self.task, {"status": "cancelled"}, actor="human", origin="cli")
        delegation.on_task_closed(self.conn, self.task, "cancelled", "不做了", None)
        store.update_task(self.conn, self.task, {"status": "in_progress"}, actor="human", origin="cli")
        self.tick()
        self.assertEqual(self.msg(mid)["status"], "sent")
        self.assertEqual(len(self.events(card, "unblocked")), 1)
        self.assertNotEqual(self.kb.cards[card]["status"], "archived")

    def test_a_cancel_before_submission_voids_the_folded_messages(self):
        did = self.delegate()
        sup = delegation.add_message(self.conn, did, "supplement", "抄送 Bob", feedback_id="fbk_s")
        delegation.add_message(self.conn, did, "cancel", "不要了", feedback_id="fbk_c")
        self.tick()
        self.assertEqual(self.kb.cards, {})
        self.assertEqual(self.msg(sup)["status"], "superseded")
        self.assertEqual(self.d(did)["status"], "cancelled")


class NotionStatusCancelTests(NotionCase):
    def test_cancelled_in_notion_while_an_answer_is_queued(self):
        kb = FakeKanban()
        task = store.create_task(self.conn, "回复 Alice", origin="intake")
        self.push(task)
        did = delegation.create(self.conn, self.cfg, task_id=task, title="起草", goal="draft")
        delegation.submit_pending(self.conn, self.cfg, kb)
        card = delegation.get(self.conn, did)["card_id"]
        kb.worker_start(card)
        kb.worker_block(card, "周四还是周五？")
        delegation.sync_cards(self.conn, self.cfg, kb)
        mid = delegation.add_message(self.conn, did, "answer", "周五", feedback_id="fbk_a")
        self.fake.user_edit(self.page_of(task), 状态="取消")
        self.pull()
        self.assertEqual(store.get_task(self.conn, task)["status"], "cancelled")
        delegation.send_messages(self.conn, self.cfg, kb)
        self.assertEqual(kb.cards[card]["status"], "archived")
        self.assertFalse([e for e in kb.events if e["task_id"] == card and e["kind"] == "unblocked"])
        self.assertEqual(self.conn.execute("SELECT status FROM delegation_messages WHERE id=?", (mid,)).fetchone()[0],
                         "superseded")
