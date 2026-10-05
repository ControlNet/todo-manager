"""R09: an edit of only the remote page body is noticed (FakeNotion is a test double).

The managed box and the user's own notes are told apart: an edit inside the box is a
body_edit, an edit of the notes is page_notes; both go to the model as feedback, which
decides whether the text is information, a requirement for the task or its delegation,
or (only when stated as such) a lasting rule.
"""

from todo_manager import clock, delegation, feedback, feedback_flow, store
from todo_manager.kanban import FakeKanban
from todo_manager.notion import body
from tests.test_feedback import GateCase
from tests.test_notion import NotionCase


class BodyDetectionTests(NotionCase):
    def setUp(self):
        super().setUp()
        self.task = store.create_task(self.conn, "准备组会材料", origin="intake", responsibility="导师安排给我")
        self.push(self.task)
        self.page = self.page_of(self.task)

    def kinds(self):
        return [f["kind"] for f in feedback.pending(self.conn)]

    def test_notes_edit_alone_becomes_feedback_once(self):
        self.fake.user_set_markdown(self.page, self.fake.markdown[self.page] + "\n\n补充：材料里要放上周的实验结果")
        self.pull()
        self.pull()
        rows = feedback.pending(self.conn)
        self.assertEqual([f["kind"] for f in rows], ["page_notes"])
        self.assertIn("上周的实验结果", rows[0]["payload"]["added"])
        self.assertEqual(rows[0]["author_is_user"], 1)

    def test_box_edit_is_detected_without_any_local_change(self):
        self.fake.user_set_markdown(self.page, self.fake.markdown[self.page].replace("导师安排给我", "其实是同学请我帮忙"))
        self.pull()
        self.assertEqual(self.kinds(), ["body_edit"])

    def test_our_own_body_write_is_not_feedback(self):
        store.update_task(self.conn, self.task, {"summary": "组会在周三"}, actor="agent", origin="intake",
                          based_on_version=store.get_task(self.conn, self.task)["version"])
        self.push(self.task)
        self.pull()
        self.assertEqual(self.kinds(), [])

    def test_first_read_of_an_existing_page_is_a_baseline(self):
        self.conn.execute("UPDATE notion_pages SET notes_hash=NULL, notes_text=NULL, body_read_at=NULL")
        self.fake.markdown[self.page] += "\n\n很久以前写的笔记"
        self.fake._touch(self.fake.pages[self.page], "user-0001")
        self.pull()
        self.assertEqual(self.kinds(), [], "unknown history is not presented as a new request")
        self.fake.user_set_markdown(self.page, self.fake.markdown[self.page] + "\n新的要求：加一页总结")
        self.pull()
        self.assertEqual(self.kinds(), ["page_notes"])

    def test_body_reads_are_bounded_and_catch_up(self):
        self.cfg.data.setdefault("notion", {})["body_reads_per_run"] = 2
        tasks = [store.create_task(self.conn, f"任务 {n}", origin="intake") for n in range(5)]
        for t in tasks:
            self.push(t)
        clock.advance(minutes=1)
        for t in tasks:
            page = self.page_of(t)
            self.fake.user_set_markdown(page, self.fake.markdown[page] + f"\n笔记 {t}")
        reads = lambda: sum(1 for c in self.fake.calls if c[0] == "GET" and c[1].endswith("/markdown"))
        before = reads()
        self.pull()
        self.assertEqual(reads() - before, 2)
        for _ in range(3):
            clock.advance(minutes=2)
            self.pull()
        self.assertEqual(sorted(f["scope_id"] for f in feedback.pending(self.conn)), sorted(tasks))

    def test_new_page_body_is_part_of_the_new_task(self):
        self.fake.user_create_page(self.tasks_ds, "写基金申请", markdown="要求：周五前给导师看初稿，预算部分找 Carol")
        self.pull()
        fb = [f for f in feedback.pending(self.conn) if f["kind"] == "new_task"][0]
        self.assertIn("预算部分找 Carol", fb["payload"]["body"])
        self.assertEqual([f["kind"] for f in feedback.pending(self.conn)], ["new_task"], "no separate notes item")


class BodyFeedbackTests(GateCase):
    def test_requirement_added_to_notes_reaches_an_active_delegation(self):
        kb = FakeKanban()
        did = delegation.create(self.conn, self.cfg, task_id=self.task, title="起草", goal="draft")
        delegation.submit_pending(self.conn, self.cfg, kb)
        self.fake.user_set_markdown(self.page, self.fake.markdown[self.page] + "\n另外：回复里要提到截止日期是周五")
        _, woke, run_id = self.gate()
        self.assertTrue(woke)
        fb = feedback.claimed_by(self.conn, run_id)[0]
        self.assertEqual(fb["kind"], "page_notes")
        context = feedback_flow.render_context(self.conn, self.cfg, run_id, [fb])
        self.assertIn("截止日期是周五", context)
        self.assertIn("not a rule", context)
        self.decide(run_id, [{"feedback": fb["id"], "outcome": "applied", "ops": [
            {"op": "message", "delegation": did, "kind": "supplement", "body": "回复里要提到截止日期是周五"}]}])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM delegation_messages WHERE delegation_id=? AND"
                                           " kind='supplement'", (did,)).fetchone()[0], 1)

    def test_plain_note_can_be_ignored(self):
        self.fake.user_set_markdown(self.page, self.fake.markdown[self.page] + "\n（自己记一下：Bob 喜欢简短的邮件）")
        clock.advance(minutes=1)                       # the gate's read covers the edit's minute
        _, _, run_id = self.gate()
        fid = feedback.claimed_by(self.conn, run_id)[0]["id"]
        self.decide(run_id, [{"feedback": fid, "outcome": "ignored", "reason": "个人笔记"}])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0], 0)

    def test_bot_marker_is_not_user_content(self):
        self.assertEqual(body.outside(self.fake.markdown[self.page]).strip(), "")
