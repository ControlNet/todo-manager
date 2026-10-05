"""C10: a Notion comment deleted before it was handled is not applied.

After a COMPLETE list of a page's comments (the list is all-or-nothing: a failed page of
results fails the whole list, and then nothing is judged), each comment whose feedback is still
pending but is no longer listed is retrieved by id:

* 404 -> deleted: its pending feedback (claimed or not, every revision) is superseded; the
  comment row records ``presence='deleted'``. Applied feedback is history and is not undone.
* found but unlisted -> resolved discussion: the feedback is held (``hold_reason``), neither
  claimed nor applied; the user is told once and health lists it; reopening the discussion
  (listed again) releases it, deleting it supersedes it.
* any other answer (error, missing capability) -> held as unverified and checked again next run.

What the real API answers for a deleted or resolved comment is not documented
(developers.notion.com documents ``GET /v1/comments/{id}`` and 404 for an id that does not
exist): ``FakeNotion`` (test double) implements the assumption above.
"""

from unittest import mock

from todo_manager import feedback, health, store
from todo_manager.net import HttpError
from tests.test_feedback import GateCase


class CommentValidityBase(GateCase):
    def comment(self, text="把截止日期改到 10-16"):
        return self.fake.user_comment(self.page, text)

    def poll(self):
        return self.new_sync().poll_comments()

    def fb_of(self, cid, status="pending"):
        return [f for f in feedback.pending(self.conn, include_claimed=True) if f["source_id"] == cid] \
            if status == "pending" else \
            [dict(r) for r in self.conn.execute("SELECT * FROM feedback WHERE source_id=? ORDER BY received_at", (cid,))]

    def presence(self, cid):
        return self.conn.execute("SELECT presence FROM notion_comments WHERE comment_id=?", (cid,)).fetchone()[0]

    def change_due(self, run_id, fid):
        task = store.get_task(self.conn, self.task)
        return self.decide(run_id, [{"feedback": fid, "outcome": "applied", "ops": [
            {"op": "update_task", "task": self.task, "based_on_version": task["version"],
             "set": {"due_date": "2026-10-16"}}]}])


class DeletedCommentTests(CommentValidityBase):
    def test_a_pending_comment_deleted_before_the_run_is_withdrawn(self):
        cid = self.comment()
        self.poll()
        [fb] = self.fb_of(cid)
        self.fake.user_delete_comment(cid)
        _, woke, _ = self.gate()
        self.assertFalse(woke)
        self.assertEqual(feedback.get(self.conn, fb["id"])["status"], "superseded")
        self.assertIn("deleted", feedback.get(self.conn, fb["id"])["result"]["reason"])
        self.assertEqual(self.presence(cid), "deleted")

    def test_a_claimed_comment_deleted_before_apply_is_not_applied(self):
        cid = self.comment()
        _, _, run_id = self.gate()
        [fb] = feedback.claimed_by(self.conn, run_id)
        self.fake.user_delete_comment(cid)
        self.gate()
        report = self.change_due(run_id, fb["id"])
        self.assertEqual(report["superseded"], [fb["id"]])
        self.assertEqual(store.get_task(self.conn, self.task)["due_date"], "2026-10-09")

    def test_an_applied_comment_deleted_later_is_not_undone(self):
        cid = self.comment()
        _, _, run_id = self.gate()
        [fb] = feedback.claimed_by(self.conn, run_id)
        self.change_due(run_id, fb["id"])
        self.fake.user_delete_comment(cid)
        self.gate()
        self.assertEqual(store.get_task(self.conn, self.task)["due_date"], "2026-10-16")
        self.assertEqual(feedback.get(self.conn, fb["id"])["status"], "applied")

    def test_an_edited_then_deleted_comment_withdraws_every_revision(self):
        cid = self.comment()
        self.poll()
        self.fake.user_edit_comment(cid, "改成 10-20")
        self.poll()
        self.fake.user_delete_comment(cid)
        _, woke, _ = self.gate()
        self.assertFalse(woke)
        self.assertEqual({f["status"] for f in self.fb_of(cid, status="all")}, {"superseded"})

    def test_an_incomplete_list_judges_nothing(self):
        cid = self.comment()
        for n in range(120):                                    # two pages of results
            self.fake.user_comment(self.page, f"笔记 {n}")
        self.poll()
        self.fake.user_delete_comment(cid)
        real = self.fake._paged

        def second_page_fails(items, params):
            if params.get("start_cursor"):
                raise HttpError(502, "bad gateway")
            return real(items, params)

        with mock.patch.object(self.fake, "_paged", second_page_fails), \
                mock.patch("todo_manager.notion.client.time.sleep"):        # the client retries the 502s
            self.poll()
        [fb] = self.fb_of(cid)
        self.assertIsNone(fb["hold_reason"], "not judged from a partial list")
        self.assertEqual(self.presence(cid), "listed")


class UnlistedCommentTests(CommentValidityBase):
    def test_a_resolved_discussion_holds_the_request_until_reopened(self):
        cid = self.comment()
        self.poll()
        disc = next(c["discussion_id"] for c in self.fake.comments if c["id"] == cid)
        self.fake.resolve_discussion(disc)
        _, woke, _ = self.gate()
        self.assertFalse(woke, "held: not handed to the model")
        [fb] = self.fb_of(cid)
        self.assertIn("resolved", fb["hold_reason"])
        self.assertEqual(self.presence(cid), "resolved")
        self.assertTrue(any("held" in p for p in health.report(self.conn, self.cfg)["problems"]))
        self.assertTrue([t for t in self.outbox_texts() if "resolved" in t or "已解决" in t], "the user is told")
        self.fake.resolve_discussion(disc, resolved=False)
        _, woke, run_id = self.gate()
        self.assertTrue(woke)
        self.assertEqual([f["id"] for f in feedback.claimed_by(self.conn, run_id)], [fb["id"]])
        self.assertIsNone(feedback.get(self.conn, fb["id"])["hold_reason"])

    def test_a_claimed_request_held_meanwhile_is_deferred_at_apply(self):
        cid = self.comment()
        _, _, run_id = self.gate()
        [fb] = feedback.claimed_by(self.conn, run_id)
        self.fake.resolve_discussion(next(c["discussion_id"] for c in self.fake.comments if c["id"] == cid))
        self.gate()
        report = self.change_due(run_id, fb["id"])
        self.assertEqual(report["deferred"], [fb["id"]])
        self.assertEqual(store.get_task(self.conn, self.task)["due_date"], "2026-10-09")

    def test_a_failed_check_holds_it_and_retries(self):
        cid = self.comment()
        self.poll()
        self.fake.user_delete_comment(cid)
        self.fake.fail_next[("GET", f"/comments/{cid}")] = HttpError(
            403, "insufficient capabilities", body={"code": "restricted_resource"})
        _, woke, _ = self.gate()
        self.assertFalse(woke)
        [fb] = self.fb_of(cid)
        self.assertIn("could not be checked", fb["hold_reason"])
        self.assertEqual(self.presence(cid), "missing")
        self.gate()
        self.assertEqual(feedback.get(self.conn, fb["id"])["status"], "superseded")
        self.assertFalse(any("held" in p for p in health.report(self.conn, self.cfg)["problems"]))

    def outbox_texts(self):
        return [r["payload"] for r in self.conn.execute("SELECT payload FROM outbox ORDER BY created_at")]
