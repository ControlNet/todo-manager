"""Projects maintain themselves (2026-10-19): "Next step", "Blockers" and "Recent progress" of a
project are written by Todo Manager from the project's tasks, and every project page shows a
linked view of its own open tasks. The user keeps the goal, title, status, area and repositories.
Notion is ``FakeNotion`` (test double).
"""

from todo_manager import clock, effects, ops, outbox, store
from todo_manager.db import get_meta, loads
from tests.test_notion import NotionCase


class ProjectCase(NotionCase):
    def ctx(self):
        return ops.context(self.conn, self.cfg, human=True)

    def project(self, title="Atlas", **fields):
        pid = ops.project_create(self.conn, self.cfg, self.ctx(), title=title, fields=fields, repos=[])["project"]
        self.drain()
        return pid

    def task(self, pid, title, **fields):
        return store.create_task(self.conn, title, origin="cli", actor="human", project_id=pid, **fields)

    def close(self, task_id, status="done"):
        store.update_task(self.conn, task_id, {"status": status}, actor="human", origin="cli")

    def sync_now(self):
        self.sync = self.new_sync()
        self.sync.enqueue_dirty()
        return self.drain()

    def page(self, pid):
        return self.fake.pages[store.get_project(self.conn, pid)["notion_page_id"]]

    def prop(self, pid, name):
        value = self.page(pid)["properties"][name]
        return "".join(t["plain_text"] for t in value.get("rich_text", [])) if "rich_text" in value \
            else (value.get("date") or {}).get("start")


class NextStepTests(ProjectCase):
    def test_the_most_urgent_task_for_me_is_the_next_step(self):
        pid = self.project()
        self.task(pid, "Write the rebuttal", due_date="2026-10-20")
        self.task(pid, "Send the camera-ready", due_date="2026-10-09", next_step="Ask Bob for the figures")
        self.task(pid, "Collect reviews", status="waiting", waiting_on="the chairs")
        self.task(pid, "Run baselines", next_actor="agent")
        self.sync_now()
        self.assertEqual(self.prop(pid, "下一步"),
                         "Send the camera-ready：Ask Bob for the figures（截止 10-09）；另有 1 项待我处理")

    def test_a_long_next_step_is_clipped_and_a_passed_due_date_is_marked(self):
        pid = self.project()
        self.task(pid, "Fix the server", due_date="2026-10-01", next_step="Take it to the shop " + "x" * 100)
        self.sync_now()
        self.assertEqual(self.prop(pid, "下一步"), "Fix the server：Take it to the shop " + "x" * 40 + "…（已过截止 10-01）")

    def test_without_a_task_for_me_it_says_who_has_the_work(self):
        pid = self.project()
        self.task(pid, "Collect reviews", status="waiting", waiting_on="the chairs")
        self.task(pid, "Run baselines", next_actor="agent")
        self.task(pid, "Read later", snooze_until="2026-10-30")
        self.sync_now()
        self.assertEqual(self.prop(pid, "下一步"), "Agent 处理中 1 项；等待别人 1 项；暂缓中 1 项")

    def test_a_project_without_open_tasks_says_so(self):
        pid = self.project()
        self.close(self.task(pid, "Old work"))
        self.sync_now()
        self.assertEqual(self.prop(pid, "下一步"), "没有未完成任务")


class BlockerTests(ProjectCase):
    def test_questions_decisions_and_waiting_are_blockers(self):
        pid = self.project()
        self.task(pid, "Pick a venue", status="decision")
        self.task(pid, "Collect reviews", status="waiting", waiting_on="the chairs", follow_up_date="2026-10-03")
        self.task(pid, "Get the GPU quota", status="waiting", follow_up_date="2026-10-12")
        self.task(pid, "Write the rebuttal")
        self.sync_now()
        self.assertEqual(self.prop(pid, "阻碍").splitlines(), [
            "待我决定：Pick a venue",
            "等 the chairs：Collect reviews（跟进已过期：10-03）",
            "等待中：Get the GPU quota（10-12 跟进）",
        ])

    def test_tasks_waiting_on_the_same_people_share_a_line(self):
        pid = self.project()
        for n, follow_up in ((1, "2026-10-23"), (2, "2026-10-04"), (3, None)):
            self.task(pid, f"PR #{n}", status="waiting", waiting_on="the maintainers", follow_up_date=follow_up)
        self.sync_now()
        self.assertEqual(self.prop(pid, "阻碍"), "等 the maintainers：3 项（最早跟进已过期：10-04）")

    def test_no_blockers_leaves_the_column_empty(self):
        pid = self.project()
        self.task(pid, "Write the rebuttal")
        self.sync_now()
        self.assertEqual(self.prop(pid, "阻碍"), "")


class RecentProgressTests(ProjectCase):
    def test_tasks_done_in_the_last_two_weeks_newest_first(self):
        pid = self.project()
        old = self.task(pid, "Old milestone")
        self.close(old)
        clock.advance(days=20)                      # now 2026-10-25
        first = self.task(pid, "Draft the paper")
        dropped = self.task(pid, "Try another venue")
        self.close(first)
        self.close(dropped, "cancelled")
        clock.advance(days=2)                       # now 2026-10-27
        self.close(self.task(pid, "Submit the paper"))
        self.sync_now()
        self.assertEqual(self.prop(pid, "近期进展").splitlines(),
                         ["10-27 完成：Submit the paper", "10-25 完成：Draft the paper"])
        self.assertEqual(self.prop(pid, "进展时间"), "2026-10-27")

    def test_one_line_per_task(self):
        pid = self.project()
        tid = self.task(pid, "Draft the poster", next_actor="agent")
        self.conn.execute("INSERT INTO delegations(id, task_id, title, goal, status, board, idem_key, base_task_version,"
                          " created_at, updated_at) VALUES ('d1', ?, 'Draft', 'A poster', 'review', 'test', 'k1', 1, ?, ?)",
                          (tid, clock.iso(), clock.iso()))
        self.conn.execute("INSERT INTO deliveries(id, delegation_id, card_id, run_id, kind, seq, received_at)"
                          " VALUES ('v1', 'd1', 'c1', 'r1', 'result', 1, ?)", (clock.iso(),))
        self.sync_now()
        self.assertEqual(self.prop(pid, "近期进展"), "10-05 Agent 交付：Draft the poster")
        clock.advance(days=1)
        self.close(tid)
        self.sync_now()
        self.assertEqual(self.prop(pid, "近期进展"), "10-06 完成：Draft the poster")

    def test_the_commit_digest_still_counts(self):
        pid = self.project()
        ops.project_update(self.conn, self.cfg, self.ctx(), pid, changes={"recent_activity": "3 commits on the parser"})
        self.sync_now()
        self.assertEqual(self.prop(pid, "近期进展"), "10-05 提交：3 commits on the parser")
        clock.advance(days=15)
        self.sync_now()
        self.assertEqual(self.prop(pid, "近期进展"), "", "an old digest is no longer recent")
        self.assertEqual(self.prop(pid, "进展时间"), "2026-10-05")

    def test_closing_a_task_updates_the_project_on_the_next_sync(self):
        pid = self.project()
        tid = self.task(pid, "Draft the paper")
        self.sync_now()
        self.assertEqual(self.prop(pid, "下一步"), "Draft the paper")
        self.close(tid)
        self.sync_now()
        self.assertEqual(self.prop(pid, "下一步"), "没有未完成任务")
        self.assertEqual(self.prop(pid, "近期进展"), "10-05 完成：Draft the paper")


class OwnershipTests(ProjectCase):
    def test_an_edit_to_a_derived_column_is_put_back(self):
        pid = self.project()
        self.task(pid, "Draft the paper")
        self.sync_now()
        self.fake.user_edit(self.page(pid)["id"], **{"下一步": "my own words", "目标": "Ship v1"})
        self.pull()
        self.sync_now()
        self.assertEqual(self.prop(pid, "下一步"), "Draft the paper")
        self.assertEqual(store.get_project(self.conn, pid)["goal"], "Ship v1", "the goal is still the user's")
        self.assertEqual(self.prop(pid, "目标"), "Ship v1")

    def test_next_step_and_blockers_cannot_be_set_by_hand(self):
        pid = self.project()
        for field in ("next_step", "blockers"):
            with self.assertRaises(ops.OpError, msg=field):
                ops.project_update(self.conn, self.cfg, self.ctx(), pid, changes={field: "x"})
        with self.assertRaises(ops.OpError):
            ops.project_create(self.conn, self.cfg, self.ctx(), title="B", fields={"next_step": "x"}, repos=[])


class ProjectTaskViewTests(ProjectCase):
    def views_on(self, page_id):
        return [v for v in self.fake.views.values()
                if (v.get("create_database") or {}).get("parent", {}).get("page_id") == page_id]

    def test_a_project_page_shows_its_open_tasks(self):
        pid = self.project()
        self.sync_now()
        page_id = self.page(pid)["id"]
        [view] = self.views_on(page_id)
        self.assertEqual(view["name"], "本项目任务")
        self.assertEqual(view["type"], "table")
        self.assertEqual(view["data_source_id"], self.tasks_ds)
        relation, *statuses = view["filter"]["and"]
        self.assertEqual(relation["relation"], {"contains": page_id})
        self.assertEqual(sorted(s["select"]["does_not_equal"] for s in statuses), ["取消", "完成"])
        self.assertEqual(loads(get_meta(self.conn, "notion.project_views"), {}), {pid: view["id"]})

    def test_created_once_and_not_again_after_the_user_deletes_it(self):
        pid = self.project()
        self.sync_now()
        self.sync_now()
        page_id = self.page(pid)["id"]
        self.assertEqual(len(self.views_on(page_id)), 1)
        self.fake.views.clear()
        self.sync_now()
        self.assertEqual(self.views_on(page_id), [])

    def test_paused_projects_get_one_but_archived_and_trashed_do_not(self):
        paused = self.project("Paused", status="paused")
        archived = self.project("Old", status="archived")
        trashed = self.project("Gone")
        self.fake.user_trash(self.page(trashed)["id"])
        self.pull()
        self.sync_now()
        self.assertEqual(len(self.views_on(self.page(paused)["id"])), 1)
        self.assertEqual(self.views_on(self.page(archived)["id"]), [])
        self.assertEqual(self.views_on(self.page(trashed)["id"]), [])

    def test_a_lost_response_does_not_make_a_second_view(self):
        pid = self.project()
        self.fake.lose_response.add(("POST", "/views"))
        self.sync_now()
        self.assertEqual(len(self.views_on(self.page(pid)["id"])), 1)
        self.assertEqual(self.conn.execute("SELECT status FROM outbox WHERE op='project_view'").fetchone()[0],
                         "uncertain")
        clock.advance(minutes=10)
        self.sync_now()
        [view] = self.views_on(self.page(pid)["id"])
        self.assertEqual(loads(get_meta(self.conn, "notion.project_views"), {}), {pid: view["id"]})
        self.assertFalse(self.conn.execute(
            "SELECT 1 FROM outbox WHERE op='project_view' AND status!='done'").fetchone())


class TrashedProjectPageTests(ProjectCase):
    def test_a_project_page_in_the_trash_is_not_pushed_again_and_again(self):
        pid = self.project()
        self.sync_now()
        self.fake.user_trash(self.page(pid)["id"])
        self.pull()                                 # a query does not return pages in the trash
        self.task(pid, "New work")
        self.sync_now()                             # the push finds the page in the trash
        pushes = self.conn.execute("SELECT count(*) FROM outbox WHERE op='push_project' AND entity_id=?",
                                   (pid,)).fetchone()[0]
        self.task(pid, "More work")
        self.sync_now()
        self.sync_now()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM outbox WHERE op='push_project' AND entity_id=?",
                                           (pid,)).fetchone()[0], pushes)

    def test_a_restored_project_page_is_updated_again(self):
        pid = self.project()
        self.sync_now()
        self.fake.user_trash(self.page(pid)["id"])
        self.pull()
        self.task(pid, "New work")
        self.sync_now()
        self.fake.user_trash(self.page(pid)["id"], False)
        self.pull()
        self.sync_now()
        self.assertEqual(self.prop(pid, "下一步"), "New work")
