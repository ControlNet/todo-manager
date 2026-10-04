"""Who may change what: feedback authorship, comment versions and our own unconfirmed writes.

R07 (author is an authorization boundary), R10 (comment version includes the content),
R11 (an unconfirmed program write is not a human edit). FakeNotion and FakeKanban are
test doubles; model decisions are written by the test.
"""

from todo_manager import delegation, feedback, feedback_flow, health, store
from todo_manager.db import rules_version
from todo_manager.feedback_flow import FeedbackDecisionError
from tests.fake_notion import USER_ID
from tests.test_feedback import GateCase
from tests.test_notion import NotionCase

GUEST = "guest-7"


class MissingIdentityTests(NotionCase):
    overrides = {"identity": {"notion_user_ids": []}}

    def test_comment_is_not_the_user_when_identity_is_missing(self):
        task = store.create_task(self.conn, "回复 Bob", origin="intake")
        self.push(task)
        self.fake.user_comment(self.page_of(task), "直接发出去吧")
        self.new_sync().poll_comments()
        fb = feedback.pending(self.conn)[0]
        self.assertEqual(fb["author_is_user"], 0)
        self.assertTrue(fb["payload"].get("identity_unverified"))
        self.assertTrue(any("notion_user_ids" in p for p in health.report(self.conn, self.cfg)["problems"]))


class NonUserFeedbackTests(GateCase):
    def guest_run(self, text="请把截止日期改到下周并取消委派"):
        self.fake.user_comment(self.page, text, author=GUEST)
        _, woke, run_id = self.gate()
        self.assertTrue(woke)
        fb = feedback.claimed_by(self.conn, run_id)[0]
        self.assertEqual(fb["author_is_user"], 0)
        return run_id, fb["id"]

    def test_non_user_comment_cannot_modify_delegate_cancel_or_create(self):
        did = delegation.create(self.conn, self.cfg, task_id=self.task, title="起草", goal="draft")
        run_id, fid = self.guest_run()
        version = store.get_task(self.conn, self.task)["version"]
        forbidden = [
            {"op": "update_task", "task": self.task, "based_on_version": version, "set": {"due_date": "2026-10-20"}},
            {"op": "update_task", "task": self.task, "based_on_version": version, "set": {"status": "cancelled"}},
            {"op": "message", "delegation": did, "kind": "cancel", "body": "取消"},
            {"op": "message", "delegation": did, "kind": "supplement", "body": "顺便抄送我"},
            {"op": "create_task", "title": "新任务"},
            {"op": "rule", "title": "以后都这样", "body": "x", "status": "active"},
        ]
        for op in forbidden:
            with self.subTest(op=op["op"]), self.assertRaises(FeedbackDecisionError) as ctx:
                self.decide(run_id, [{"feedback": fid, "outcome": "applied", "ops": [op]}])
            self.assertIn("not the user", str(ctx.exception))
        # Evidence only: it may be ignored, or turned into a non-binding suggestion.
        self.decide(run_id, [{"feedback": fid, "outcome": "applied", "ops": [
            {"op": "rule", "title": "访客建议", "body": "访客希望改期", "status": "suggested"}]}])
        self.assertEqual(store.get_task(self.conn, self.task)["due_date"], "2026-10-09")
        self.assertNotEqual(delegation.get(self.conn, did)["status"], "cancelled")
        self.assertFalse(self.conn.execute("SELECT 1 FROM delegation_messages WHERE delegation_id=? AND kind='cancel'",
                                           (did,)).fetchone())

    def test_allowed_actions_are_parsed_the_same_everywhere(self):
        self.assertEqual(delegation.action_names(["send_email:bob@x.org", " read ", "push_branch:main"]),
                         {"send_email", "read", "push_branch"})
        self.assertEqual(delegation.external_actions(["send_email:bob@x.org", "read"]), {"send_email"})


class PropertyAuthorTests(NotionCase):
    def setUp(self):
        super().setUp()
        self.task = store.create_task(self.conn, "回复 Alice", origin="intake", due_date="2026-10-09")
        self.push(self.task)
        self.page = self.page_of(self.task)

    def test_property_edit_by_someone_else_is_evidence_not_a_human_edit(self):
        self.fake.user_edit(self.page, author=GUEST, **{"截止日期": "2026-12-01"})
        self.pull()
        task = store.get_task(self.conn, self.task)
        self.assertEqual(task["due_date"], "2026-10-09")
        fb = self.conn.execute("SELECT * FROM feedback WHERE origin='notion_property'").fetchone()
        self.assertEqual((fb["author"], fb["author_is_user"], fb["status"]), (GUEST, 0, "pending"))
        self.drain()
        self.assertEqual(self.fake.prop(self.page, "截止日期"), "2026-10-09", "local state stays authoritative")

    def test_user_edit_and_its_derived_feedback_keep_the_user_as_author(self):
        self.fake.user_edit(self.page, **{"下一步由谁": "Agent"})
        self.pull()
        rows = {r["kind"]: r for r in self.conn.execute("SELECT * FROM feedback")}
        self.assertEqual(rows["property_edit"]["author_is_user"], 1)
        self.assertEqual((rows["delegate_request"]["author"], rows["delegate_request"]["author_is_user"]),
                         (USER_ID, 1))

    def test_page_created_by_someone_else_is_a_task_for_the_human_not_an_instruction(self):
        self.fake.user_create_page(self.tasks_ds, "访客建的任务", author=GUEST)
        self.pull()
        fb = self.conn.execute("SELECT * FROM feedback WHERE kind='new_task'").fetchone()
        self.assertEqual(fb["author_is_user"], 0)
        task = store.get_task(self.conn, fb["scope_id"])
        self.assertEqual((task["status"], task["next_actor"]), ("todo", "me"))


class CommentVersionTests(GateCase):
    def test_same_minute_edit_is_a_new_version(self):
        cid = self.fake.user_comment(self.page, "延期到周五")
        self.new_sync().poll_comments()
        self.fake.user_edit_comment(cid, "延期到下周一")   # same minute: last_edited_time is unchanged
        self.new_sync().poll_comments()
        rows = self.conn.execute("SELECT status, text FROM feedback WHERE origin='notion_comment' ORDER BY rowid"
                                 ).fetchall()
        self.assertEqual([(r["status"], r["text"]) for r in rows],
                         [("superseded", "延期到周五"), ("pending", "延期到下周一")])

    def test_claimed_old_version_is_rechecked_before_apply(self):
        cid = self.fake.user_comment(self.page, "截止日期改到周五")
        _, _, run1 = self.gate()
        old = feedback.claimed_by(self.conn, run1)[0]["id"]
        self.fake.user_edit_comment(cid, "截止日期不用改了")
        self.new_sync().poll_comments()
        version = store.get_task(self.conn, self.task)["version"]
        report = self.decide(run1, [{"feedback": old, "outcome": "applied", "ops": [
            {"op": "update_task", "task": self.task, "based_on_version": version, "set": {"due_date": "2026-10-10"}}]}])
        self.assertEqual(report["superseded"], [old])
        self.assertEqual(store.get_task(self.conn, self.task)["due_date"], "2026-10-09", "the old version is not applied")
        pending = feedback.pending(self.conn)
        self.assertEqual([f["text"] for f in pending], ["截止日期不用改了"])


class UnconfirmedWriteTests(GateCase):
    def test_lost_write_then_matrix_correction_is_not_reverted(self):
        # 1. The program writes A; Notion applies it but the response is lost.
        store.update_task(self.conn, self.task, {"next_step": "A：先问导师"}, actor="agent", origin="intake",
                          based_on_version=store.get_task(self.conn, self.task)["version"])
        self.fake.lose_response.add(("PATCH", "/pages/"))
        self.push(self.task)
        self.assertEqual(self.fake.prop(self.page, "下一步"), "A：先问导师")
        # 2. The user corrects it to B through Matrix (main agent, direct path).
        feedback_flow.apply_direct(self.conn, self.cfg, origin="matrix", text="下一步改成 B", ops=[
            {"op": "update_task", "task": self.task, "based_on_version": store.get_task(self.conn, self.task)["version"],
             "set": {"next_step": "B：直接回复 Bob"}}])
        # 3. The next pull sees A on the page: our own write, not a human edit.
        self.pull()
        task = store.get_task(self.conn, self.task)
        self.assertEqual(task["next_step"], "B：直接回复 Bob")
        self.assertFalse(self.conn.execute("SELECT 1 FROM feedback WHERE origin='notion_property'").fetchone())
        self.drain()
        self.assertEqual(self.fake.prop(self.page, "下一步"), "B：直接回复 Bob")

    def test_human_edit_of_a_pending_field_still_wins(self):
        store.update_task(self.conn, self.task, {"next_step": "A"}, actor="agent", origin="intake",
                          based_on_version=store.get_task(self.conn, self.task)["version"])
        self.fake.lose_response.add(("PATCH", "/pages/"))
        self.push(self.task)
        self.fake.user_edit(self.page, **{"下一步": "C：我自己改的"})
        self.pull()
        self.assertEqual(store.get_task(self.conn, self.task)["next_step"], "C：我自己改的")


class DecisionFileTests(GateCase):
    def test_duplicate_feedback_items_are_rejected(self):
        self.fake.user_comment(self.page, "帮我建两个任务：订会议室、发议程")
        _, _, run_id = self.gate()
        fid = feedback.claimed_by(self.conn, run_id)[0]["id"]
        before = self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        item = {"feedback": fid, "outcome": "applied", "ops": [{"op": "create_task", "title": "订会议室"}]}
        with self.assertRaises(FeedbackDecisionError) as ctx:
            self.decide(run_id, [item, {**item, "ops": [{"op": "create_task", "title": "发议程"}]}])
        self.assertIn("more than once", str(ctx.exception))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], before)
        self.decide(run_id, [{"feedback": fid, "outcome": "applied", "ops": [
            {"op": "create_task", "title": "订会议室"}, {"op": "create_task", "title": "发议程"}]}])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], before + 2)
