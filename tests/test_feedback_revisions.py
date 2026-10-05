"""B01/B02: every Notion feedback stream (a comment, the managed box) has a persistent revision
number, so a later read can tell an unchanged re-read from a revert and a run that interpreted
an older revision cannot apply it. The page notes are context, not feedback: they are kept as
their latest text (with a revision count) and never queued.

FakeNotion (test double) stamps edits with a minute-truncated ``last_edited_time``, which makes
two edits within one minute share a timestamp; that is a property of the fake used to force
the ordering, not a claim about the precision of the real API.
"""

from todo_manager import clock, feedback, store
from tests.test_feedback import GateCase
from tests.test_notion import NotionCase


def pending(conn, kind=None):
    return [f for f in feedback.pending(conn, include_claimed=True) if kind is None or f["kind"] == kind]


class CommentRevisionTests(NotionCase):
    def setUp(self):
        super().setUp()
        self.task = store.create_task(self.conn, "回复 Bob", origin="intake")
        self.push(self.task)
        self.page = self.page_of(self.task)

    def poll(self):
        self.sync = self.new_sync()
        return self.sync.poll_comments()

    def test_unchanged_re_read_is_not_a_new_revision(self):
        self.fake.user_comment(self.page, "截止日期改到周五")
        self.poll()
        self.poll()
        [fb] = pending(self.conn)
        self.assertEqual(self.conn.execute("SELECT revision FROM notion_comments").fetchone()[0], 1)
        self.assertEqual(fb["text"], "截止日期改到周五")

    def test_same_timestamp_a_b_a_restores_a(self):
        cid = self.fake.user_comment(self.page, "A: 截止日期改到周五")
        self.poll()
        self.fake.user_edit_comment(cid, "B: 截止日期改到周一")
        self.poll()
        self.fake.user_edit_comment(cid, "A: 截止日期改到周五")
        self.poll()
        rows = pending(self.conn)
        self.assertEqual([f["text"] for f in rows], ["A: 截止日期改到周五"], "the reverted text is what stands")
        self.assertEqual(self.conn.execute("SELECT revision FROM notion_comments WHERE comment_id=?", (cid,)).fetchone()[0], 3)
        b = self.conn.execute("SELECT status, result FROM feedback WHERE text LIKE 'B:%'").fetchone()
        self.assertEqual(b["status"], "superseded")
        self.assertIn(rows[0]["id"], b["result"], "the superseded row names its successor")


class CommentRunTests(GateCase):
    def test_a_run_that_claimed_b_cannot_apply_it_after_a_revert_to_a(self):
        cid = self.fake.user_comment(self.page, "A: 截止日期改到 10-12")
        self.gate()
        self.fake.user_edit_comment(cid, "B: 截止日期改到 10-20")
        _, woke, run_b = self.gate()
        [fb_b] = feedback.claimed_by(self.conn, run_b)
        self.assertTrue(fb_b["text"].startswith("B:"))
        self.fake.user_edit_comment(cid, "A: 截止日期改到 10-12")   # same minute as the edit to B
        self.gate()
        task = store.get_task(self.conn, self.task)
        report = self.decide(run_b, [{"feedback": fb_b["id"], "outcome": "applied", "ops": [
            {"op": "update_task", "task": self.task, "based_on_version": task["version"],
             "set": {"due_date": "2026-10-20"}}]}])
        self.assertEqual(report["superseded"], [fb_b["id"]])
        self.assertNotEqual(store.get_task(self.conn, self.task)["due_date"], "2026-10-20", "B is never applied")
        [current] = pending(self.conn)
        self.assertTrue(current["text"].startswith("A:"))


class BoxRevisionTests(NotionCase):
    def setUp(self):
        super().setUp()
        self.task = store.create_task(self.conn, "准备组会材料", origin="intake", responsibility="导师安排给我")
        self.push(self.task)
        self.page = self.page_of(self.task)
        self.original = self.fake.markdown[self.page]

    def edit_box(self, text):
        self.fake.user_set_markdown(self.page, self.original.replace("导师安排给我", text))
        self.pull()

    def test_a_then_b_keeps_only_b(self):
        self.edit_box("A 其实是同学请我帮忙")
        self.edit_box("B 其实是 Bob 请我帮忙")
        rows = pending(self.conn, "body_edit")
        self.assertEqual(len(rows), 1)
        self.assertIn("B 其实是 Bob", rows[0]["payload"]["region"])

    def test_a_b_a_restores_a(self):
        self.edit_box("A 其实是同学请我帮忙")
        self.edit_box("B 其实是 Bob 请我帮忙")
        self.edit_box("A 其实是同学请我帮忙")
        rows = pending(self.conn, "body_edit")
        self.assertEqual(len(rows), 1)
        self.assertIn("A 其实是同学", rows[0]["payload"]["region"])
        self.assertEqual(self.conn.execute("SELECT box_revision FROM notion_pages WHERE page_id=?",
                                           (self.page,)).fetchone()[0], 3)

    def test_a_revert_to_our_text_withdraws_the_edit(self):
        self.edit_box("A 其实是同学请我帮忙")
        self.fake.user_set_markdown(self.page, self.original)
        self.pull()
        self.assertEqual(pending(self.conn, "body_edit"), [])
        row = self.conn.execute("SELECT status, result FROM feedback WHERE kind='body_edit'").fetchone()
        self.assertEqual(row["status"], "superseded")
        self.assertIn("withdrawn", row["result"])

    def test_unchanged_re_read_records_nothing_new(self):
        self.edit_box("A 其实是同学请我帮忙")
        self.fake.user_set_markdown(self.page, self.fake.markdown[self.page] + "\n\n笔记")
        self.pull()
        self.assertEqual(len(pending(self.conn, "body_edit")), 1)


class NotesRevisionTests(NotionCase):
    def setUp(self):
        super().setUp()
        self.task = store.create_task(self.conn, "准备组会材料", origin="intake")
        self.push(self.task)
        self.page = self.page_of(self.task)
        self.base = self.fake.markdown[self.page]

    def notes(self, *lines):
        self.fake.user_set_markdown(self.page, self.base + "\n\n" + "\n".join(lines))
        self.pull()

    def test_the_notes_are_always_the_latest_text_and_never_feedback(self):
        row = lambda: self.conn.execute("SELECT notes_text, notes_revision FROM notion_pages"  # noqa: E731
                                        " WHERE page_id=?", (self.page,)).fetchone()
        start = row()["notes_revision"]
        for lines, expected in ((["要求：周五前完成"], "要求：周五前完成"), (["要求：周一前完成"], "要求：周一前完成"),
                                (["要求：周五前完成"], "要求：周五前完成"), ([], "")):
            self.notes(*lines)
            self.assertEqual(row()["notes_text"], expected)
        self.notes()                                    # unchanged re-read
        self.assertEqual(row()["notes_revision"], start + 4)
        self.assertEqual(pending(self.conn), [])


class EditAfterClaimTests(GateCase):
    def setUp(self):
        super().setUp()
        self.original = self.fake.markdown[self.page]

    def edit_box(self, text):
        self.fake.user_set_markdown(self.page, self.original.replace("Bob 直接问我", text))

    def test_box_edited_after_claim_is_not_applied(self):
        self.edit_box("其实是 Alice 问我")
        clock.advance(minutes=1)                       # the gate's read covers the edit's minute
        _, _, run_id = self.gate()
        [fb] = feedback.claimed_by(self.conn, run_id)
        self.edit_box("其实是 Carol 问我")
        clock.advance(minutes=1)
        self.gate()
        task = store.get_task(self.conn, self.task)
        report = self.decide(run_id, [{"feedback": fb["id"], "outcome": "applied", "ops": [
            {"op": "update_task", "task": self.task, "based_on_version": task["version"],
             "set": {"responsibility": "Alice 问我"}}]}])
        self.assertEqual(report["superseded"], [fb["id"]])
        self.assertNotEqual(store.get_task(self.conn, self.task)["responsibility"], "Alice 问我")
        [current] = pending(self.conn, "body_edit")
        self.assertIn("Carol", current["payload"]["region"])
        self.assertIsNotNone(self.conn.execute("SELECT managed_hash FROM notion_pages WHERE page_id=?",
                                               (self.page,)).fetchone()[0], "the box stays held for Carol's edit")

    def test_an_edit_not_yet_read_defers_the_claimed_interpretation(self):
        self.cfg.data.setdefault("notion", {})["body_reads_per_run"] = 10
        self.edit_box("其实是 Alice 问我")
        clock.advance(minutes=1)
        _, _, run_id = self.gate()
        [fb] = feedback.claimed_by(self.conn, run_id)
        clock.advance(minutes=2)
        self.edit_box("其实是 Carol 问我")
        self.cfg.data["notion"]["body_reads_per_run"] = 0      # the page is pulled, its body not yet read
        self.pull()
        task = store.get_task(self.conn, self.task)
        report = self.decide(run_id, [{"feedback": fb["id"], "outcome": "applied", "ops": [
            {"op": "update_task", "task": self.task, "based_on_version": task["version"],
             "set": {"responsibility": "Alice 问我"}}]}])
        self.assertEqual(report["deferred"], [fb["id"]])
        self.assertNotEqual(store.get_task(self.conn, self.task)["responsibility"], "Alice 问我")
        row = feedback.get(self.conn, fb["id"])
        self.assertEqual((row["status"], row["claimed_run"]), ("pending", None), "released for a re-read")
        self.cfg.data["notion"]["body_reads_per_run"] = 10
        self.pull()
        self.assertEqual(feedback.get(self.conn, fb["id"])["status"], "superseded")
        [current] = pending(self.conn, "body_edit")
        self.assertIn("Carol", current["payload"]["region"])
