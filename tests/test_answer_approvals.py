"""An approval given with the answer to a worker's question.

Workers use Hermes' native ``kanban_block`` to ask before an external action they were not
allowed at creation ("may I send the email?"). The user answers with ``delegate answer
--approve action[:detail]``: the approval goes to the worker in the answer's comment, on the
card that asked, however late; it is used at most once and settled like an approval of a
delivered version:

* the run it was handed to delivers a result -> ``reported``;
* that run ends without one -> ``unknown``: the user is told, the card is paused, nothing is
  replayed;
* the answer never reaches the question (answered on the board, a newer question) -> the
  approval is void and never posted.

Kanban is ``FakeKanban`` (test double).
"""

import contextlib
import io
import json

from todo_manager import cli, delegation, ops, runs
from todo_manager.ops import OpError
from tests.test_approval_late_confirmation import LateConfirmationBase


class AnswerApprovalBase(LateConfirmationBase):
    def asking(self, question="可以直接把邮件发给 Alice 吗？"):
        did, card = self.running()
        self.kb.worker_block(card, question)
        self.sync_all()
        return did, card

    def answer(self, did, body="可以，发吧", approve=("send_email:只发给 Alice",), request="$req_1"):
        ctx = ops.context(self.conn, self.cfg, request=request, said=body)
        return ops.delegate_answer(self.conn, self.cfg, ctx, did, body=body, approve=list(approve))

    def answer_approvals(self, did):
        return self.conn.execute("SELECT * FROM approvals WHERE delegation_id=? AND answer_message_id IS NOT NULL"
                                 " ORDER BY rowid", (did,)).fetchall()


class AnswerApprovalTests(AnswerApprovalBase):
    def test_the_approval_goes_with_the_answer_and_is_reported_by_the_result(self):
        did, card = self.asking()
        result = self.answer(did)
        self.assertEqual(len(result["approvals"]), 1)
        self.sync_all()
        [comment] = [c for c in self.kb.comments if f"[tm:{result['message']}]" in c["body"]]
        self.assertIn("APPROVED send_email for question #1. 只发给 Alice", comment["body"])
        self.assertEqual(self.kb.cards[card]["status"], "ready")
        [a] = self.answer_approvals(did)
        self.assertIsNotNone(a["consumed_at"])
        self.kb.worker_start(card)
        self.kb.worker_complete(card, "已发送", {"external_actions_done": ["send_email"]})
        self.sync_all()
        self.assertEqual(self.answer_approvals(did)[0]["outcome"], "reported")
        self.assertEqual(self.unknown_notices(), [])

    def test_a_run_that_ends_without_a_report_is_unknown_and_paused(self):
        did, card = self.asking()
        self.answer(did)
        self.sync_all()
        self.kb.worker_start(card)
        self.kb.worker_crash(card)
        self.sync_all()
        self.assertEqual(self.answer_approvals(did)[0]["outcome"], "unknown")
        self.assertEqual(len(self.unknown_notices()), 1)
        self.assertEqual(len(self.auto_pauses(did)), 1)
        self.assertNotIn(card, self.kb.dispatch_tick(), "the external action is not replayed")

    def test_an_answer_that_misses_its_question_voids_the_approval(self):
        did, card = self.asking()
        self.kb.unblock(card)                          # answered on the board meanwhile
        self.answer(did)
        self.sync_all()
        [a] = self.answer_approvals(did)
        self.assertIsNotNone(a["revoked_at"])
        self.assertIsNone(a["consumed_at"])
        self.assertFalse(self.approved_comments(card), "a void approval is never posted")
        self.assertTrue(any("#1" in t and "send_email" in t for t in self.outbox_texts()), "the user is told")

    def test_a_retried_request_records_one_approval(self):
        did, card = self.asking()
        first = self.answer(did)
        again = self.answer(did)
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["approvals"], first["approvals"])
        self.assertEqual(len(self.answer_approvals(did)), 1)

    def test_a_scheduled_run_cannot_approve_and_unknown_actions_are_refused(self):
        did, card = self.asking()
        with self.assertRaises(OpError) as ctx:
            self.answer(did, approve=("launch_rocket",))
        self.assertEqual(ctx.exception.code, "INVALID")
        run_ctx = ops.context(self.conn, self.cfg, run=runs.start(self.conn, "feedback"))
        with self.assertRaises(OpError) as ctx:
            ops.delegate_answer(self.conn, self.cfg, run_ctx, did, body="可以", approve=["send_email"])
        self.assertEqual(ctx.exception.code, "NOT_AUTHORIZED", "a scheduled run never grants permissions")
        with self.assertRaises(OpError) as ctx:
            ops.delegate_approve(self.conn, self.cfg, run_ctx, did, delivery="x", action="send_email")
        self.assertEqual(ctx.exception.code, "NOT_AUTHORIZED")
        self.assertEqual(self.answer_approvals(did), [])

    def test_a_cancel_before_the_answer_goes_out_revokes_its_approval(self):
        did, card = self.asking()
        self.answer(did)
        delegation.add_message(self.conn, did, "cancel", "不用了", feedback_id="fbk_c")
        self.sync_all()
        [a] = self.answer_approvals(did)
        self.assertIsNotNone(a["revoked_at"])
        self.assertFalse(self.approved_comments(card))


class AnswerApprovalCliTests(AnswerApprovalBase):
    def test_delegate_answer_takes_repeatable_approvals(self):
        did, card = self.asking()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--home", str(self.tmp), "delegate", "answer", did, "--body", "可以",
                             "--approve", "send_email:只发给 Alice", "--approve", "comment_github",
                             "--request", "$req_cli", "--said", "可以，邮件和评论都发吧"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(len(json.loads(out.getvalue())["approvals"]), 2)
        self.assertEqual([a["action"] for a in self.answer_approvals(did)], ["send_email", "comment_github"])
