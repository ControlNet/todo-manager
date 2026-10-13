"""Which pages the comment poll reads in a gate run (one Notion request per page).

Pages where a conversation is going on are read every run (agent work, a user comment in the
last week, a user edit in the last ``notion.user_edit_hot_hours``); pages that are merely recent
(a new open task, a change note, an older user edit within 48 hours) every
``notion.warm_comment_poll_minutes``; the others round-robin (open tasks hourly, recently closed
ones daily). Our own writes are not activity: after a bulk push (a migration, a re-render) every
page was "recently edited" by the bot and every run read every page (137 requests, about a
minute per run, 2026-10-12). In production all 7 user comments of a day landed on pages with
agent work while about 55 pages were read every run, mostly new tasks (2026-10-13).
Notion is ``FakeNotion`` (test double).
"""

from datetime import timedelta

from todo_manager import clock, delegation, store
from tests.test_notion import NotionCase


class CommentPollingTests(NotionCase):
    def setUp(self):
        super().setUp()
        self.open = [store.create_task(self.conn, f"Open {i}", origin="intake") for i in range(4)]
        self.closed = [store.create_task(self.conn, f"Closed {i}", origin="intake") for i in range(3)]
        for t in self.closed:
            store.update_task(self.conn, t, {"status": "done"}, actor="program",
                              origin="intake", based_on_version=store.get_task(self.conn, t)["version"])
        for t in self.open + self.closed:
            self.push(t)
        self.poll()                       # first read of every page
        clock.advance(hours=49)           # no longer new
        self.poll()
        clock.advance(minutes=5)

    def poll(self):
        self.sync = self.new_sync()
        return self.sync.poll_comments()

    def targets(self):
        self.sync = self.new_sync()
        return {t["entity_id"]: t["reason"] for t in self.sync.comment_targets() if t["entity_type"] == "task"}

    def test_our_own_writes_do_not_make_every_page_hot(self):
        for t in self.open + self.closed:   # a bulk re-render: every page edited by the bot again
            store.update_task(self.conn, t, {"next_step": "x"}, actor="program", origin="intake",
                              based_on_version=store.get_task(self.conn, t)["version"], override_human=True)
            self.push(t)
        clock.advance(minutes=5)
        self.assertEqual(self.targets(), {})

    def test_a_new_open_task_is_read_every_warm_interval_not_every_run(self):
        t = store.create_task(self.conn, "New", origin="intake")
        self.push(t)
        self.poll()
        clock.advance(minutes=5)
        self.assertEqual(self.targets(), {})
        clock.advance(minutes=26)
        self.assertEqual(self.targets(), {t: "warm"})
        self.poll()
        clock.advance(minutes=5)
        self.assertEqual(self.targets(), {}, "read again only after another warm interval")

    def test_the_warm_interval_is_a_setting(self):
        t = store.create_task(self.conn, "New", origin="intake")
        self.push(t)
        self.poll()
        self.cfg.data["notion"]["warm_comment_poll_minutes"] = 10
        clock.advance(minutes=11)
        self.assertEqual(self.targets(), {t: "warm"})

    def test_open_pages_are_still_read_round_robin(self):
        clock.advance(minutes=61)
        self.assertEqual(set(self.targets()), set(self.open))

    def test_a_page_the_user_edited_is_read_every_run_for_a_while_then_warm(self):
        t = self.open[0]
        self.fake.user_edit(self.page_of(t), **{"下一步": "问一下 Bob"})
        self.pull()
        self.assertEqual(self.targets(), {t: "hot"})
        clock.advance(minutes=119)
        self.poll()
        self.assertEqual(self.targets(), {t: "hot"})
        clock.advance(minutes=2)
        self.assertEqual(self.targets(), {}, "past user_edit_hot_hours and read a minute ago")
        clock.advance(minutes=30)
        self.assertEqual(self.targets(), {t: "warm"})
        self.poll()
        clock.advance(hours=47)
        self.poll()
        clock.advance(minutes=31)
        self.assertNotIn(t, self.targets(), "48 hours after the edit it is an ordinary open page")

    def test_the_hot_window_after_a_user_edit_is_a_setting(self):
        t = self.open[0]
        self.fake.user_edit(self.page_of(t), **{"下一步": "问一下 Bob"})
        self.pull()
        self.cfg.data["notion"]["user_edit_hot_hours"] = 6
        clock.advance(hours=5)
        self.poll()
        clock.advance(minutes=5)
        self.assertEqual(self.targets(), {t: "hot"})

    def test_a_page_the_user_edited_stays_hot_after_we_write_it_back(self):
        t = self.open[0]
        self.fake.user_edit(self.page_of(t), **{"下一步": "问一下 Bob"})
        self.pull()
        store.update_task(self.conn, t, {"waiting_on": "Bob"}, actor="program", origin="intake",
                          based_on_version=store.get_task(self.conn, t)["version"])
        self.push(t)
        self.assertEqual(self.conn.execute("SELECT last_edited_by FROM notion_pages WHERE entity_id=?", (t,)
                                           ).fetchone()[0], self.sync.state.bot_user_id, "our write came last")
        self.assertEqual(self.targets(), {t: "hot"})

    def test_a_page_with_a_recent_comment_is_read_every_run(self):
        self.fake.user_comment(self.page_of(self.open[1]), "先别做")
        clock.advance(minutes=61)
        self.poll()
        clock.advance(minutes=5)
        self.assertEqual(self.targets(), {self.open[1]: "hot"})

    def test_a_task_with_agent_work_is_read_every_run(self):
        delegation.create(self.conn, self.cfg, task_id=self.open[2], title="Draft", goal="Draft only")
        self.assertEqual(self.targets().get(self.open[2]), "hot")

    def test_a_change_the_user_was_told_about_is_read_every_warm_interval_for_two_days(self):
        t = self.open[3]
        store.update_task(self.conn, t, {"change_note": "Agent 交付了草稿"}, actor="program", origin="kanban",
                          based_on_version=store.get_task(self.conn, t)["version"], override_human=True)
        self.conn.execute("UPDATE tasks SET change_note_at=? WHERE id=?", (clock.iso(), t))
        self.push(t)
        self.poll()
        clock.advance(minutes=5)
        self.assertNotIn(t, self.targets(), "not every run")
        clock.advance(minutes=26)
        self.assertEqual(self.targets(), {t: "warm"})
        clock.advance(hours=49)
        self.poll()
        clock.advance(minutes=5)
        self.assertNotIn(t, self.targets())

    def test_a_question_from_the_agent_keeps_the_page_hot(self):
        d = delegation.create(self.conn, self.cfg, task_id=self.open[3], title="Draft", goal="Draft only")
        self.conn.execute("UPDATE delegations SET status='blocked' WHERE id=?", (d,))
        clock.advance(hours=72)
        self.poll()
        clock.advance(minutes=5)
        self.assertEqual(self.targets().get(self.open[3]), "hot")
