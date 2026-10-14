"""The Agent progress board (2026-10-12): one board for agent work, grouped by its own select.

Columns: Needs my answer / To review (program) | Done / My to-do / Cancel (the user's drags,
applied without a model run) | In progress (program). Answers and revisions are page comments.
Kanban is ``FakeKanban`` and Notion is ``FakeNotion`` (test doubles).
"""

import json

from todo_manager import clock, delegation, feedback, ops, store
from todo_manager.db import get_meta, loads, set_meta
from todo_manager.kanban import FakeKanban
from todo_manager.notion import setup
from tests.test_notion import NotionCase

PROP = "Agent 进展"


class Board(NotionCase):
    def setUp(self):
        super().setUp()
        self.kb = FakeKanban()
        self.task = store.create_task(self.conn, "起草给 Bob 的回复", origin="intake")
        self.push(self.task)
        self.page = self.page_of(self.task)

    def tick(self):
        self.pull()
        delegation.send_messages(self.conn, self.cfg, self.kb)
        delegation.submit_pending(self.conn, self.cfg, self.kb)
        delegation.sync_cards(self.conn, self.cfg, self.kb)
        self.sync = self.new_sync()
        self.sync.enqueue_dirty()
        self.drain()

    def delegated(self):
        did = delegation.create(self.conn, self.cfg, task_id=self.task, title="起草", goal="Draft only")
        self.tick()
        return did, delegation.get(self.conn, did)["card_id"]

    def in_review(self):
        did, card = self.delegated()
        self.kb.worker_start(card)
        self.kb.worker_review(card, "草稿在附件")
        self.tick()
        return did, card

    def column(self):
        return self.fake.prop(self.page, PROP)

    def drag(self, label):
        self.fake.user_edit(self.page, **{PROP: label})
        self.tick()
        self.tick()


class ColumnTests(Board):
    def test_no_agent_work_is_not_on_the_board(self):
        self.assertIsNone(self.column())

    def test_each_state_lands_in_its_column(self):
        did, card = self.delegated()
        self.assertEqual(self.column(), "处理中")
        self.kb.worker_start(card)
        self.kb.worker_block(card, "发给谁？")
        self.tick()
        self.assertEqual(self.column(), "等我回答")
        delegation.add_message(self.conn, did, "answer", "发给 Bob")
        self.tick()
        self.kb.worker_start(card)
        self.kb.worker_review(card, "草稿")
        self.tick()
        self.assertEqual(self.column(), "待我验收")

    def test_a_delivered_draft_waits_for_review_until_handled(self):
        did, card = self.delegated()
        self.kb.worker_start(card)
        self.kb.worker_complete(card, "草稿")
        self.tick()
        self.assertEqual(self.column(), "待我验收")


class DragTests(Board):
    def test_done_accepts_the_version_and_closes_the_task(self):
        did, card = self.in_review()
        self.drag("已完成")
        task = store.get_task(self.conn, self.task)
        self.assertEqual(task["status"], "done")
        self.assertIn("Agent 进展", task["closed_reason"])
        self.assertEqual(self.kb.cards[card]["status"], "done", "accepted, not cancelled")
        self.assertIsNone(self.column())

    def test_my_todo_accepts_and_hands_the_task_to_me(self):
        did, card = self.in_review()
        self.drag("设为待办")
        task = store.get_task(self.conn, self.task)
        self.assertEqual((task["status"], task["next_actor"]), ("todo", "me"))
        self.assertEqual(self.kb.cards[card]["status"], "done")
        self.assertIsNone(self.column(), "handled: off the board")
        self.assertIsNone(delegation.task_label(self.conn, self.task))

    def test_my_todo_on_a_delivered_draft_takes_it_off_the_board(self):
        did, card = self.delegated()
        self.kb.worker_start(card)
        self.kb.worker_complete(card, "草稿")
        self.tick()
        self.drag("设为待办")
        self.assertIsNone(self.column())
        self.assertEqual(store.get_task(self.conn, self.task)["status"], "todo")

    def test_my_todo_on_running_work_stops_the_agent(self):
        did, card = self.delegated()
        self.kb.worker_start(card)
        self.tick()
        self.drag("设为待办")
        self.assertEqual(self.kb.cards[card]["status"], "archived")
        task = store.get_task(self.conn, self.task)
        self.assertEqual((task["status"], task["next_actor"]), ("todo", "me"))
        self.assertIsNone(self.column())

    def test_cancel_cancels_the_task_and_its_agent_work(self):
        did, card = self.in_review()
        self.drag("取消")
        self.assertEqual(store.get_task(self.conn, self.task)["status"], "cancelled")
        self.assertEqual(self.kb.cards[card]["status"], "archived")
        self.assertIsNone(self.column())

    def test_dragging_to_a_state_column_changes_nothing(self):
        did, card = self.in_review()
        self.drag("处理中")
        self.assertEqual(self.kb.cards[card]["status"], "review")
        self.assertEqual(self.column(), "待我验收", "written back")

    def test_an_accept_given_in_a_comment_also_takes_it_off_the_board(self):
        did, card = self.in_review()
        delegation.add_message(self.conn, did, "accept", "可以")
        self.tick()
        self.assertIsNone(self.column())

    def test_new_agent_work_on_the_task_is_on_the_board_again(self):
        did, card = self.in_review()
        self.drag("设为待办")
        delegation.create(self.conn, self.cfg, task_id=self.task, title="再改一版", goal="Revise")
        self.tick()
        self.assertEqual(self.column(), "处理中")


class HandedOverTests(Board):
    """Assign to Agent on the To decide board: the task is on the Agent board at once, before a
    run has turned the request into agent work, and stays there if the run asks instead."""

    def setUp(self):
        super().setUp()
        self.task = store.create_task(self.conn, "确定组会时间", origin="intake", status="decision")
        self.push(self.task)
        self.page = self.page_of(self.task)

    def hand_over(self):
        self.fake.user_edit(self.page, **{"决定": "交给 Agent"})
        self.tick()
        return next(f for f in feedback.pending(self.conn) if f["kind"] == "delegate_request")

    def test_handed_over_task_is_in_progress_before_any_agent_work(self):
        self.hand_over()
        self.assertEqual(self.column(), "处理中")

    def test_a_question_instead_of_agent_work_waits_for_my_answer(self):
        request = self.hand_over()
        ops.feedback_resolve(self.conn, self.cfg, request["id"], outcome="clarify", reply="要 Agent 做什么？")
        self.tick()
        self.assertEqual(self.column(), "等我回答")
        self.fake.user_comment(self.page, "整理一份选项清单")
        clock.advance(minutes=31)   # comments of a task without agent work are read every warm interval
        self.new_sync().poll_comments()
        self.tick()
        self.assertEqual(self.column(), "处理中", "my answer waits for a run")
        answer = next(f for f in feedback.pending(self.conn) if f["kind"] == "comment")
        delegation.create(self.conn, self.cfg, task_id=self.task, title="整理清单", goal="List the options")
        ops.feedback_resolve(self.conn, self.cfg, answer["id"], outcome="applied")
        self.tick()
        self.assertEqual(self.column(), "处理中")

    def test_a_question_then_the_task_taken_back_leaves_the_board(self):
        request = self.hand_over()
        ops.feedback_resolve(self.conn, self.cfg, request["id"], outcome="clarify", reply="要 Agent 做什么？")
        self.tick()
        self.drag("设为待办")
        self.assertEqual(store.get_task(self.conn, self.task)["next_actor"], "me")
        self.assertIsNone(self.column())

    def test_a_request_set_aside_is_not_on_the_board(self):
        request = self.hand_over()
        store.update_task(self.conn, self.task, {"status": "waiting"}, actor="agent", origin="intake",
                          based_on_version=store.get_task(self.conn, self.task)["version"])
        ops.feedback_resolve(self.conn, self.cfg, request["id"], outcome="ignored", reason="the user said to wait")
        self.tick()
        self.assertIsNone(self.column())


class ViewTests(NotionCase):
    def specs(self):
        return {s["key"]: s for s in setup.view_specs(self.sync.state.task_props.ids,
                                                       self.sync.state.project_props.ids, "zh-CN")}

    def test_one_agent_board_grouped_by_its_own_select_with_every_column_shown(self):
        specs = self.specs()
        self.assertNotIn("review", specs)
        agent = specs["agent"]
        prop = self.sync.state.task_props.ids["agent_progress"]
        self.assertEqual(agent["type"], "board")
        self.assertEqual(agent["configuration"]["group_by"]["property_id"], prop)
        self.assertFalse(agent["configuration"]["group_by"]["hide_empty_groups"], "drop targets stay visible")
        self.assertEqual(agent["filter"], {"property": prop, "select": {"is_not_empty": True}})
        self.assertEqual([o["name"] for o in self.fake.data_sources[self.tasks_ds]["properties"][PROP]["select"]["options"]],
                         ["等我回答", "待我验收", "已完成", "设为待办", "取消", "处理中"])

    def test_a_board_notion_grouped_its_own_way_is_regrouped(self):
        from tests.fake_notion import FakeNotion
        from todo_manager.notion.client import NotionClient
        fake = FakeNotion()
        fake.default_grouping = {"type": "select", "property_id": "status", "hide_empty_groups": True}
        client = NotionClient(transport=fake, min_interval=0)
        self.conn.execute("DELETE FROM meta WHERE key LIKE 'notion.%'")
        setup.apply(self.conn, client, fake._new_page({"type": "workspace"}, {}, "", "u")["id"])
        views = loads(get_meta(self.conn, "notion.views"), {})
        props = loads(get_meta(self.conn, "notion.props.task"), {})
        for key, field in (("agent", "agent_progress"), ("decision", "decision")):
            group = fake.views[views[key]]["configuration"]["group_by"]
            self.assertEqual(group["property_id"], props[field], key)
            self.assertFalse(group["hide_empty_groups"], key)

    def test_focus_is_the_users_own_work_only(self):
        self.assertNotIn(self.sync.state.task_props.ids["delegation"], json.dumps(self.specs()["focus"]["filter"]))

    def test_home_has_seven_sections(self):
        self.assertEqual(setup.SECTIONS, ("decision", "agent", "focus", "projects", "waiting", "all", "recent"))
        self.assertEqual(get_meta(self.conn, "notion.agent_ui_version"), "1", "a fresh setup is current")


class MigrationTests(Board):
    """An install set up before the board: Agent work grouped by the read-only Delegation select,
    a separate To review table, agent work also in Focus."""

    def old_ui(self):
        from todo_manager import i18n
        from todo_manager.notion.agent_board_migration import OLD_FOCUS, OLD_AGENT
        t = dict(self.sync.state.task_props.ids)
        views = loads(get_meta(self.conn, "notion.views"), {})
        rid = self.fake._id("view")
        self.fake.views[rid] = {"object": "view", "id": rid, "type": "table", "name": "待我验收",
                                "parent": {"type": "database_id", "database_id": self.fake._id("linked-db")}}
        views["review"] = rid
        old_headings = {"review": "✅ 待我验收／待我补充", "agent": "🤖 Agent 排队／处理中"}
        blocks = []
        for key in ("decision", "review", "agent", "focus", "projects", "waiting", "all", "recent"):
            blocks.append(self.fake.block("heading_2", old_headings.get(key) or i18n.t(f"home.section.{key}", "zh-CN")))
            blocks.append(self.fake.block("child_database", id=self.fake.views[views[key]]["parent"]["database_id"]))
        self.fake.blocks[self.sync.state.home_page_id] = blocks
        self.fake.views[views["agent"]].update(OLD_AGENT(t, "zh-CN"))
        self.fake.views[views["focus"]]["filter"] = OLD_FOCUS(t, "zh-CN")
        self.fake.data_sources[self.tasks_ds]["properties"].pop(PROP)
        for p in self.fake.pages.values():
            p["properties"].pop(PROP, None)
        t.pop("agent_progress")
        set_meta(self.conn, "notion.props.task", json.dumps(t))
        set_meta(self.conn, "notion.views", json.dumps(views))
        self.conn.execute("DELETE FROM meta WHERE key='notion.agent_ui_version'")
        for r in self.conn.execute("SELECT page_id, last_seen_props FROM notion_pages").fetchall():
            seen = loads(r["last_seen_props"], {})
            seen.pop("agent_progress", None)
            self.conn.execute("UPDATE notion_pages SET last_seen_props=? WHERE page_id=?", (json.dumps(seen), r["page_id"]))
        self.sync = self.new_sync()
        return views

    def headings(self):
        return [self.fake.text(b) for b in self.fake.blocks[self.sync.state.home_page_id] if b["type"] == "heading_2"]

    def test_install_builds_the_board_and_retires_the_review_table(self):
        from todo_manager.notion import agent_board_migration
        old = self.old_ui()
        self.sync.enqueue_dirty()
        self.assertEqual(self.drain()["failed"], 0, "before the install: the missing property is not written")
        did, card = self.in_review()
        plan = agent_board_migration.plan(self.conn, self.client)
        self.assertTrue(plan["changes"])
        agent_board_migration.apply(self.conn, self.client, self.tmp)
        self.assertTrue((self.tmp / "backups/notion-agent-board-v1.json").exists())
        views = loads(get_meta(self.conn, "notion.views"), {})
        self.assertNotIn("review", views)
        self.assertNotEqual(views["agent"], old["agent"])
        heads = self.headings()
        self.assertIn("🤖 Agent 工作", heads)
        self.assertNotIn("✅ 待我验收／待我补充", heads)
        self.assertNotIn("🤖 Agent 排队／处理中", heads)
        home = self.fake.blocks[self.sync.state.home_page_id]
        new_db = self.fake.views[views["agent"]]["parent"]["database_id"]
        at = next(i for i, b in enumerate(home) if b["id"] == new_db)
        self.assertEqual(self.fake.text(home[at - 1]), "🤖 Agent 工作", "the board sits under its heading")
        ids = {b["id"] for b in home}
        self.assertNotIn(self.fake.views[old["agent"]]["parent"]["database_id"], ids)
        self.assertNotIn(self.fake.views[old["review"]]["parent"]["database_id"], ids)
        self.assertNotIn(self.sync.state.task_props.ids["delegation"],
                         json.dumps(self.fake.views[views["focus"]]["filter"]))
        self.tick()
        self.assertEqual(self.column(), "待我验收", "the next sync fills the new column")
        self.assertEqual(get_meta(self.conn, "notion.agent_ui_version"), "1")
        before = len(self.writes())
        self.assertIsNone(agent_board_migration.plan(self.conn, self.client))
        self.assertEqual(len(self.writes()), before)

    def test_the_new_board_is_regrouped_if_notion_ignored_the_grouping(self):
        from todo_manager.notion import agent_board_migration
        self.old_ui()
        self.fake.default_grouping = {"type": "select", "property_id": "status", "hide_empty_groups": True}
        agent_board_migration.apply(self.conn, self.client, self.tmp)
        views = loads(get_meta(self.conn, "notion.views"), {})
        group = self.fake.views[views["agent"]]["configuration"]["group_by"]
        self.assertEqual(group["property_id"], loads(get_meta(self.conn, "notion.props.task"), {})["agent_progress"])
        self.assertFalse(group["hide_empty_groups"])

    def test_install_asks_once_before_migrating(self):
        from todo_manager.install import Install, AnswerPrompter
        views = self.old_ui()
        declined = Install(self.tmp, AnswerPrompter({}, set(), output=lambda _: None))
        try:
            declined._notion_views(self.conn, self.client)
        except Exception as exc:   # NeedsConfirmation: nothing written before the answer
            self.assertIn("notion_agent_board", str(getattr(exc, "step", exc)))
        self.assertEqual(loads(get_meta(self.conn, "notion.views"), {})["agent"], views["agent"])
        Install(self.tmp, AnswerPrompter({}, {"all"}, output=lambda _: None))._notion_views(self.conn, self.client)
        self.assertEqual(get_meta(self.conn, "notion.agent_ui_version"), "1")

    def test_a_customized_focus_view_is_kept_and_reported(self):
        from todo_manager.notion import agent_board_migration
        views = self.old_ui()
        self.fake.views[views["focus"]]["filter"] = {"property": "x", "checkbox": {"equals": True}}
        plan = agent_board_migration.plan(self.conn, self.client)
        self.assertTrue(any("Focus" in c for c in plan["changes"]))
        agent_board_migration.apply(self.conn, self.client, self.tmp)
        self.assertEqual(self.fake.views[views["focus"]]["filter"], {"property": "x", "checkbox": {"equals": True}})
