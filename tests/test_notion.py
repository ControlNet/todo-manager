from datetime import timedelta

from todo_manager import clock, delegation, feedback, outbox, store
from todo_manager.kanban import FakeKanban
from todo_manager.net import HttpError
from todo_manager.notion import body, schema, setup
from todo_manager.notion.client import NotionClient
from todo_manager.notion.sync import NotionSync, load_state
from tests.fake_notion import BOT_ID, USER_ID, FakeNotion
from tests.helpers import WorkspaceCase, mail


class NotionCase(WorkspaceCase):
    overrides = {"identity": {"notion_user_ids": [USER_ID]}}

    def setUp(self):
        super().setUp()
        self.fake = FakeNotion()
        self.client = NotionClient(transport=self.fake, min_interval=0)
        self.parent = self.fake._new_page({"type": "workspace"}, {}, "", USER_ID)["id"]
        setup.apply(self.conn, self.client, self.parent)
        self.sync = self.new_sync()
        self.tasks_ds = self.sync.state.tasks_data_source_id
        self.projects_ds = self.sync.state.projects_data_source_id

    def new_sync(self):
        return NotionSync(self.conn, self.cfg, self.client, load_state(self.conn, self.cfg))

    def drain(self):
        self.sync = self.new_sync()
        return outbox.process(self.conn, self.sync.handlers())

    def push(self, task_id):
        from todo_manager import effects
        effects.task_push(self.conn, task_id)
        return self.drain()

    def page_of(self, task_id):
        return store.get_task(self.conn, task_id)["notion_page_id"]

    def pull(self):
        self.sync = self.new_sync()
        return self.sync.pull("project"), self.sync.pull("task")

    def writes(self):
        return [c for c in self.fake.calls if c[0] in ("POST", "PATCH") and not c[1].endswith("/query")]


class SetupTests(NotionCase):
    def test_setup_creates_structure_once(self):
        self.assertTrue(self.sync.state.ready)
        self.assertEqual(len(self.fake.databases), 2)
        self.assertEqual(len(self.fake.views), 7)
        names = set(self.fake.data_sources[self.tasks_ds]["properties"])
        self.assertTrue({"任务", "状态", "下一步由谁", "项目", "TM ID", "委派"} <= names)
        self.assertIn("任务", self.fake.data_sources[self.projects_ds]["properties"], "dual relation on projects")
        before = len(self.writes())
        setup.apply(self.conn, self.client, self.parent)
        self.assertEqual(len(self.writes()), before, "re-running setup writes nothing")
        self.assertTrue(any("关联视图" in s for s in setup.plan("parent")))

    def test_setup_can_adopt_an_existing_page_as_home(self):
        from todo_manager import db
        conn = db.connect(self.tmp / "other.sqlite")
        try:
            home = self.fake._new_page({"type": "workspace"}, {}, "我自己写的说明", USER_ID)["id"]
            setup.apply(conn, self.client, home_page_id=home)
            setup.apply(conn, self.client, home_page_id=home)
            md = self.fake.markdown[home]
            self.assertEqual(md.count("数据新鲜度"), 1)
            self.assertIn("我自己写的说明", md)
            self.assertEqual(load_state(conn, self.cfg).home_page_id, home)
            self.assertEqual(len(self.fake.views), 14, "7 more views on the adopted page")
        finally:
            conn.close()


class PushPullTests(NotionCase):
    def setUp(self):
        super().setUp()
        self.project = store.create_project(self.conn, "Dataset paper", origin="intake")
        self.task = store.create_task(self.conn, "回复 Alice 的数据集邮件", origin="intake", project_id=self.project,
                                      responsibility="Alice 直接问我", summary="她需要拆分文件",
                                      next_step="确认拆分方式", due_date="2026-10-09")

    def test_push_creates_page_with_properties_body_and_project(self):
        stats = self.push(self.task)
        self.assertEqual(stats["done"], 1)
        page = self.page_of(self.task)
        self.assertEqual(self.fake.prop(page, "状态"), "待办")
        self.assertEqual(self.fake.prop(page, "TM ID"), self.task)
        self.assertEqual(self.fake.prop(page, "截止日期"), "2026-10-09")
        project_page = store.get_project(self.conn, self.project)["notion_page_id"]
        self.assertEqual(self.fake.prop(page, "项目"), [project_page])
        self.assertIn("Alice 直接问我", self.fake.markdown[page])
        calls = len(self.writes())
        self.push(self.task)
        self.assertEqual(len(self.writes()), calls, "nothing changed -> no write")

    def test_human_edit_is_imported_once_and_not_echoed(self):
        self.push(self.task)
        page = self.page_of(self.task)
        clock.advance(minutes=3)
        self.fake.user_edit(page, 状态="等待", 等待对象="Alice 的回复", 跟进日期="2026-10-12")
        self.pull()
        t = store.get_task(self.conn, self.task)
        self.assertEqual((t["status"], t["next_actor"], t["waiting_on"]), ("waiting", "external", "Alice 的回复"))
        meta = __import__("json").loads(t["field_meta"])
        self.assertEqual(meta["status"]["by"], "human")
        fb = self.conn.execute("SELECT * FROM feedback WHERE kind='property_edit'").fetchall()
        self.assertEqual(len(fb), 1)
        self.assertEqual(fb[0]["status"], "applied")
        self.drain()  # normalised next_actor pushed back
        self.assertEqual(self.fake.prop(page, "下一步由谁"), "外部")
        clock.advance(minutes=3)
        self.pull()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM feedback WHERE kind='property_edit'").fetchone()[0], 1,
                         "our own write is not a human edit")

    def test_push_reads_before_write_and_keeps_concurrent_human_edit(self):
        self.push(self.task)
        page = self.page_of(self.task)
        clock.advance(minutes=1)
        self.fake.user_edit(page, 截止日期="2026-10-20")          # user edits in Notion ...
        store.update_task(self.conn, self.task, {"next_step": "起草回复"}, actor="agent", origin="intake",
                          based_on_version=store.get_task(self.conn, self.task)["version"])
        self.push(self.task)                                      # ... before we pulled it
        t = store.get_task(self.conn, self.task)
        self.assertEqual(t["due_date"], "2026-10-20", "human edit imported, not overwritten")
        self.assertEqual(self.fake.prop(page, "截止日期"), "2026-10-20")
        self.assertEqual(self.fake.prop(page, "下一步"), "起草回复")
        # An agent write based on the old version cannot revert the human date.
        result = store.update_task(self.conn, self.task, {"due_date": "2026-10-09"}, actor="agent", origin="intake",
                                   based_on_version=1)
        self.assertIn("due_date", result.rejected)

    def test_lost_create_response_does_not_duplicate_page(self):
        self.push(self.task)
        self.task = store.create_task(self.conn, "另一个事项", origin="intake")
        self.fake.lose_response.add(("POST", "/pages"))
        stats = self.push(self.task)
        self.assertEqual(stats["uncertain"], 1)
        self.assertEqual(len(self.fake.pages_in(self.tasks_ds)), 2, "the page was created remotely")
        self.assertIsNone(self.page_of(self.task))
        clock.advance(minutes=2)
        self.drain()
        self.assertEqual(len(self.fake.pages_in(self.tasks_ds)), 2, "reconciled by TM ID, not created twice")
        self.assertIsNotNone(self.page_of(self.task))

    def test_rate_limit_is_retried(self):
        self.fake.fail_next[("POST", "/pages")] = HttpError(429, "slow down", retry_after=0,
                                                             body={"code": "rate_limited"})
        self.assertEqual(self.push(self.task)["done"], 1)

    def test_unknown_status_option_is_reported_not_imported(self):
        self.push(self.task)
        page = self.page_of(self.task)
        clock.advance(minutes=1)
        self.fake.user_edit(page, 状态="以后再说")
        self.pull()
        self.assertEqual(store.get_task(self.conn, self.task)["status"], "todo")
        self.assertTrue(any("以后再说" in p for p in self.sync.problems))

    def test_page_created_in_notion_becomes_task_and_needs_triage(self):
        self.push(self.task)
        clock.advance(minutes=1)
        pid = self.fake.user_create_page(self.tasks_ds, "给学生写推荐信", 截止日期="2026-10-30")
        self.pull()
        row = self.conn.execute("SELECT * FROM tasks WHERE notion_page_id=?", (pid,)).fetchone()
        self.assertEqual((row["title"], row["origin"], row["due_date"]), ("给学生写推荐信", "notion", "2026-10-30"))
        pending = feedback.pending(self.conn)
        self.assertEqual([f["kind"] for f in pending], ["new_task"])
        self.drain()
        self.assertEqual(self.fake.prop(pid, "TM ID"), row["id"])
        self.assertEqual(len(self.fake.pages_in(self.tasks_ds)), 2)

    def test_trash_cancels_and_restore_reopens(self):
        self.push(self.task)
        page = self.page_of(self.task)
        clock.advance(minutes=1)
        self.fake.user_trash(page)
        self.push(self.task)
        t = store.get_task(self.conn, self.task)
        self.assertEqual((t["status"], t["closed_reason"]), ("cancelled", "页面已在 Notion 中删除"))
        clock.advance(minutes=1)
        self.fake.user_trash(page, False)
        self.pull()
        self.assertEqual(store.get_task(self.conn, self.task)["status"], "todo")


class WorkflowEditTests(NotionCase):
    def setUp(self):
        super().setUp()
        self.kb = FakeKanban()
        self.task = store.create_task(self.conn, "起草回复", origin="intake")
        self.push(self.task)
        self.page = self.page_of(self.task)

    def test_assigning_agent_asks_the_model_once(self):
        clock.advance(minutes=1)
        self.fake.user_edit(self.page, 下一步由谁="Agent")
        self.pull()
        clock.advance(minutes=5)
        self.pull()
        kinds = [f["kind"] for f in feedback.pending(self.conn)]
        self.assertEqual(kinds, ["delegate_request"], "a persistent property state is not a new request each run")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM delegations").fetchone()[0], 0, "no card guessed")

    def test_marking_done_accepts_a_delivered_review(self):
        did = delegation.create(self.conn, self.cfg, task_id=self.task, title="起草", goal="draft")
        for _ in range(2):
            delegation.submit_pending(self.conn, self.cfg, self.kb)
        card = delegation.get(self.conn, did)["card_id"]
        self.kb.worker_start(card)
        self.kb.worker_review(card, "草稿", {"deliverable_markdown": "Dear Alice"})
        delegation.sync_cards(self.conn, self.cfg, self.kb)
        self.drain()
        clock.advance(minutes=1)
        self.fake.user_edit(self.page, 状态="完成")
        self.pull()
        delegation.send_messages(self.conn, self.cfg, self.kb)
        self.assertEqual(self.kb.cards[card]["status"], "done")
        self.assertEqual(delegation.get(self.conn, did)["status"], "ended")
        self.assertEqual(store.get_task(self.conn, self.task)["status"], "done", "the human closed it")


class CommentTests(NotionCase):
    def setUp(self):
        super().setUp()
        self.task = store.create_task(self.conn, "回复 Bob", origin="intake")
        self.push(self.task)
        self.page = self.page_of(self.task)

    def poll(self):
        self.sync = self.new_sync()
        return self.sync.poll_comments()

    def test_user_comment_becomes_one_feedback_and_bot_reply_is_ignored(self):
        cid = self.fake.user_comment(self.page, "对方已同意延期到周五")
        self.poll()
        self.poll()
        rows = feedback.pending(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["origin"], rows[0]["scope_id"], rows[0]["text"]),
                         ("notion_comment", self.task, "对方已同意延期到周五"))
        from todo_manager import effects
        effects.task_comment(self.conn, self.task, "reply:1", "好的，已把截止日期改为周五。",
                             discussion_id=rows[0]["payload"]["discussion_id"])
        self.drain()
        self.poll()
        self.assertEqual(len(feedback.pending(self.conn)), 1, "our reply is not a new command")
        clock.advance(minutes=2)
        self.fake.user_edit_comment(cid, "对方已同意延期到下周一")
        self.poll()
        rows = self.conn.execute("SELECT status, text FROM feedback WHERE origin='notion_comment' ORDER BY received_at"
                                 ).fetchall()
        self.assertEqual([(r["status"], r["text"]) for r in rows],
                         [("superseded", "对方已同意延期到周五"), ("pending", "对方已同意延期到下周一")])

    def test_reply_to_a_delivery_comment_is_tied_to_the_delegation(self):
        kb = FakeKanban()
        did = delegation.create(self.conn, self.cfg, task_id=self.task, title="起草", goal="draft")
        delegation.submit_pending(self.conn, self.cfg, kb)
        card = delegation.get(self.conn, did)["card_id"]
        kb.worker_start(card)
        kb.worker_block(card, "收件人是谁？")
        delegation.sync_cards(self.conn, self.cfg, kb)
        self.drain()
        bot = [c for c in self.fake.comments if c["created_by"]["id"] == BOT_ID]
        self.assertEqual(len(bot), 1)
        self.fake.user_comment(self.page, "发给 Bob 本人", discussion_id=bot[0]["discussion_id"])
        self.poll()
        fb = feedback.pending(self.conn)[0]
        self.assertEqual(fb["payload"]["delegation_id"], did)
        self.assertEqual(fb["payload"]["delivery_kind"], "question")

    def test_lost_comment_response_is_not_posted_twice(self):
        from todo_manager import effects
        self.fake.lose_response.add(("POST", "/comments"))
        effects.task_comment(self.conn, self.task, "k1", "交付已更新")
        self.drain()
        clock.advance(minutes=2)
        self.drain()
        self.assertEqual(len([c for c in self.fake.comments if c["created_by"]["id"] == BOT_ID]), 1)

    def test_comment_from_someone_else_is_marked_not_user(self):
        self.fake.user_comment(self.page, "请直接发出去", author="guest-9")
        self.poll()
        self.assertEqual(feedback.pending(self.conn)[0]["author_is_user"], 0)


class BodyTests(NotionCase):
    def setUp(self):
        super().setUp()
        self.task = store.create_task(self.conn, "准备组会材料", origin="intake", responsibility="导师安排给我")
        self.push(self.task)
        self.page = self.page_of(self.task)

    def test_managed_region_updates_and_user_notes_survive(self):
        self.fake.user_set_markdown(self.page, self.fake.markdown[self.page] + "\n我的笔记：先看上周反馈")
        store.update_task(self.conn, self.task, {"summary": "组会在周三"}, actor="agent", origin="intake",
                          based_on_version=1)
        self.push(self.task)
        md = self.fake.markdown[self.page]
        self.assertIn("组会在周三", md)
        self.assertIn("我的笔记：先看上周反馈", md)
        self.assertEqual(md.count(body.MARKER), 1)

    def test_user_edit_inside_region_is_held_for_interpretation(self):
        self.push(self.task)  # records the region fingerprint
        region = body.extract(self.fake.markdown[self.page])
        self.fake.user_set_markdown(self.page, self.fake.markdown[self.page].replace("导师安排给我", "其实是同学请我帮忙"))
        store.update_task(self.conn, self.task, {"summary": "新的背景"}, actor="agent", origin="intake",
                          based_on_version=1)
        self.push(self.task)
        self.assertIn("其实是同学请我帮忙", self.fake.markdown[self.page], "not overwritten")
        self.assertNotIn("新的背景", self.fake.markdown[self.page])
        self.assertEqual([f["kind"] for f in feedback.pending(self.conn)], ["body_edit"])
        self.assertNotEqual(region, body.extract(self.fake.markdown[self.page]))

    def test_deleted_region_is_reinserted_at_top(self):
        self.fake.user_set_markdown(self.page, "只剩我的笔记")
        store.update_task(self.conn, self.task, {"summary": "更新"}, actor="agent", origin="intake", based_on_version=1)
        self.push(self.task)
        md = self.fake.markdown[self.page]
        self.assertTrue(md.startswith("<callout"))
        self.assertIn("只剩我的笔记", md)


class EvidenceSourcesTests(NotionCase):
    def test_sources_and_label(self):
        self.add_items(mail("m1", "t1", "Can you send the split files?", subject="Dataset split"))
        item = self.item_id("m1")
        task = store.create_task(self.conn, "发送拆分文件", origin="intake")
        store.link_evidence(self.conn, "task", task, item, "origin")
        self.push(task)
        page = self.page_of(task)
        self.assertEqual(self.fake.prop(page, "来源"), "邮件")
        self.assertIn("Dataset split", self.fake.markdown[page])


class DirtyScanTests(NotionCase):
    def test_dirty_scan_pushes_only_changed(self):
        t1 = store.create_task(self.conn, "A", origin="cli")
        t2 = store.create_task(self.conn, "B", origin="cli")
        self.sync = self.new_sync()
        self.assertEqual(self.sync.enqueue_dirty(), 2)
        self.drain()
        self.assertEqual(self.new_sync().enqueue_dirty(), 0)
        store.update_task(self.conn, t2, {"next_step": "x"}, actor="human", origin="cli")
        self.assertEqual(self.new_sync().enqueue_dirty(), 1)
        self.drain()
        self.assertEqual(self.fake.prop(self.page_of(t2), "下一步"), "x")
        self.assertIsNotNone(self.page_of(t1))
