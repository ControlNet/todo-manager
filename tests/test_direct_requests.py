"""B05: a direct (Matrix/terminal) request carries an explicit identity: the Matrix event id, or
a request id minted with ``todo-manager feedback request-id`` once per request and reused when
the same call is retried. A retry is applied once; two genuine requests with the same words are
both applied; nothing is deduplicated by text."""

import contextlib
import io
import json

from todo_manager import cli, feedback, feedback_flow, store
from todo_manager.feedback_flow import FeedbackDecisionError
from tests.helpers import WorkspaceCase


class DirectRequestTests(WorkspaceCase):
    def create(self, source_id, text="帮我加一个任务：周五前交报销单"):
        return feedback_flow.apply_direct(self.conn, self.cfg, origin="matrix", text=text, source_id=source_id,
                                          ops=[{"op": "create_task", "title": "交报销单"}])

    def tasks(self):
        return [t for t in store.list_tasks(self.conn) if t["title"] == "交报销单"]

    def test_a_request_without_identity_is_refused_and_nothing_is_recorded(self):
        with self.assertRaises(FeedbackDecisionError) as ctx:
            self.create(None)
        self.assertIn("request-id", str(ctx.exception))
        self.assertEqual(self.tasks(), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0], 0)

    def test_a_retry_with_the_same_identity_is_applied_once(self):
        first = self.create("$evt:matrix.org")
        again = self.create("$evt:matrix.org")
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["feedback"], first["feedback"])
        self.assertEqual(len(self.tasks()), 1)

    def test_two_genuine_requests_with_the_same_words_are_both_applied(self):
        self.create("req_one")
        self.create("req_two")
        self.assertEqual(len(self.tasks()), 2)

    def test_an_identity_reused_for_different_words_is_refused(self):
        self.create("req_one")
        with self.assertRaises(FeedbackDecisionError) as ctx:
            self.create("req_one", text="另外再加一个任务：订机票")
        self.assertEqual(ctx.exception.code, "REQUEST_ID_REUSED")
        self.assertEqual(len(self.tasks()), 1)

    def test_a_request_recorded_for_later_and_then_applied_directly_is_applied_once(self):
        feedback.record(self.conn, origin="matrix", source_id="req_rec", text="帮我加一个任务：周五前交报销单",
                        scope_type="global", kind="direct", author="user", author_is_user=True)
        self.create("req_rec")
        self.create("req_rec")
        self.assertEqual(len(self.tasks()), 1)


class DirectRequestCliTests(WorkspaceCase):
    def tm(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--home", str(self.tmp), *argv])
        return code, out.getvalue()

    def test_request_ids_are_fresh_and_required(self):
        _, a = self.tm("feedback", "request-id")
        _, b = self.tm("feedback", "request-id")
        self.assertNotEqual(json.loads(a)["request_id"], json.loads(b)["request_id"])
        code, out = self.tm("feedback", "apply-direct", "--origin", "matrix", "--text", "x")
        self.assertNotEqual(code, 0)
        self.assertIn("request-id", out)
        code, out = self.tm("feedback", "record", "--origin", "matrix", "--text", "x")
        self.assertNotEqual(code, 0)
        rid = json.loads(a)["request_id"]
        code, out = self.tm("feedback", "record", "--origin", "matrix", "--text", "x", "--source-id", rid)
        self.assertEqual(code, 0)
        code, out = self.tm("feedback", "record", "--origin", "matrix", "--text", "x", "--source-id", rid)
        self.assertFalse(json.loads(out)["created"], "a retried record is stored once")
