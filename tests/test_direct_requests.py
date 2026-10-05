"""B05: a direct (Matrix/terminal) request carries an explicit identity: ``--request <id>``, the
Matrix event id of the user's message or one id the agent chooses for the request and reuses
when the same call is retried. A retry is applied once; two genuine requests with the same
words are both applied; nothing is deduplicated by text."""

import contextlib
import io
import json

from todo_manager import cli, feedback, ops, store
from todo_manager.ops import OpError
from tests.flow import apply_direct
from tests.helpers import WorkspaceCase


class DirectRequestTests(WorkspaceCase):
    def create(self, source_id, text="帮我加一个任务：周五前交报销单"):
        [result] = apply_direct(self.conn, self.cfg, text=text, source_id=source_id,
                                op_list=[{"op": "create_task", "title": "交报销单"}])
        return result

    def tasks(self):
        return [t for t in store.list_tasks(self.conn) if t["title"] == "交报销单"]

    def test_the_users_words_without_a_request_id_are_refused_and_nothing_is_recorded(self):
        with self.assertRaises(OpError) as ctx:
            ops.context(self.conn, self.cfg, said="帮我加一个任务：周五前交报销单")
        self.assertIn("--request", str(ctx.exception))
        self.assertEqual(self.tasks(), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0], 0)

    def test_a_retry_with_the_same_identity_is_applied_once(self):
        first = self.create("$evt:matrix.org")
        again = self.create("$evt:matrix.org")
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["task"], first["task"])
        self.assertEqual(len(self.tasks()), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0], 1)

    def test_two_genuine_requests_with_the_same_words_are_both_applied(self):
        self.create("req_one")
        self.create("req_two")
        self.assertEqual(len(self.tasks()), 2)

    def test_an_identity_reused_for_different_words_is_refused(self):
        self.create("req_one")
        with self.assertRaises(OpError) as ctx:
            self.create("req_one", text="另外再加一个任务：订机票")
        self.assertEqual(ctx.exception.code, "REQUEST_ID_REUSED")
        self.assertEqual(len(self.tasks()), 1)

    def test_a_request_recorded_for_later_and_then_handled_directly_is_handled_once(self):
        recorded = ops.feedback_record(self.conn, request="req_rec", said="帮我加一个任务：周五前交报销单",
                                       scope=None)
        self.create("req_rec")
        self.create("req_rec")
        self.assertEqual(len(self.tasks()), 1)
        row = feedback.get(self.conn, recorded["feedback"])
        self.assertEqual(row["status"], "superseded", "the feedback job does not handle it a second time")
        self.assertEqual(feedback.pending(self.conn), [])


class DirectRequestCliTests(WorkspaceCase):
    def tm(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(["--home", str(self.tmp), *argv])
            except SystemExit as exc:          # argparse: a required option is missing
                code = exc.code
        return code, out.getvalue() + err.getvalue()

    def test_request_ids_are_required_and_retries_are_stored_once(self):
        code, out = self.tm("task", "create", "--title", "交报销单", "--said", "帮我加一个任务")
        self.assertNotEqual(code, 0)
        self.assertIn("--request", out)
        code, out = self.tm("feedback", "record", "--said", "x")
        self.assertNotEqual(code, 0)
        code, out = self.tm("feedback", "record", "--request", "req-1005-ab", "--said", "x")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["created"])
        code, out = self.tm("feedback", "record", "--request", "req-1005-ab", "--said", "x")
        self.assertFalse(json.loads(out)["created"], "a retried record is stored once")
        code, out = self.tm("task", "create", "--title", "交报销单", "--request", "req-1005-cd", "--said", "帮我加一个任务")
        self.assertEqual(code, 0, out)
        again = self.tm("task", "create", "--title", "交报销单", "--request", "req-1005-cd", "--said", "帮我加一个任务")
        self.assertTrue(json.loads(again[1])["duplicate"])
        self.assertEqual(len([t for t in store.list_tasks(self.conn) if t["title"] == "交报销单"]), 1)
