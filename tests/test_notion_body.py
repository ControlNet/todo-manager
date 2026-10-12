"""R09: an edit of only the remote page body is noticed (FakeNotion is a test double).

The managed box and the user's own notes are told apart. An edit inside the box is
``body_edit`` feedback: the model decides what it means and the box is not overwritten
meanwhile. The user's own notes are context: kept exactly as written
(``notion_pages.notes_text``), shown with the Task (feedback context, ``task show``) and in a
new agent card; a change of the notes wakes nothing and is never an instruction by itself.
"""

import hashlib
import re
import unittest

from todo_manager import clock, delegation, feedback, store
from todo_manager.kanban import FakeKanban
from todo_manager.notion import body
from tests.fake_notion import autolinked
from tests.helpers import mail
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

    def notes(self, task=None):
        row = self.conn.execute("SELECT notes_text FROM notion_pages WHERE entity_type='task' AND entity_id=?",
                                (task or self.task,)).fetchone()
        return row["notes_text"] if row else None

    def test_a_notes_edit_is_kept_as_written_and_is_not_feedback(self):
        self.fake.user_set_markdown(self.page, self.fake.markdown[self.page] + "\n\n补充：材料里要放上周的实验结果")
        self.pull()
        self.pull()
        self.assertEqual(self.kinds(), [])
        self.assertEqual(self.notes(), "补充：材料里要放上周的实验结果")

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
        self.assertEqual(self.notes(), "很久以前写的笔记", "it is context from the first read")
        self.fake.user_set_markdown(self.page, self.fake.markdown[self.page] + "\n新的要求：加一页总结")
        self.pull()
        self.assertEqual(self.kinds(), [])
        self.assertIn("新的要求：加一页总结", self.notes())

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
        self.assertEqual(sum(1 for t in tasks if self.notes(t)), 2)
        for _ in range(3):
            clock.advance(minutes=2)
            self.pull()
        self.assertEqual([self.notes(t) for t in tasks], [f"笔记 {t}" for t in tasks], "every page is read in the end")

    def test_new_page_body_is_part_of_the_new_task(self):
        self.fake.user_create_page(self.tasks_ds, "写基金申请", markdown="要求：周五前给导师看初稿，预算部分找 Carol")
        self.pull()
        fb = [f for f in feedback.pending(self.conn) if f["kind"] == "new_task"][0]
        self.assertIn("预算部分找 Carol", fb["payload"]["body"])
        self.assertEqual([f["kind"] for f in feedback.pending(self.conn)], ["new_task"], "no separate notes item")


class NotesAsContextTests(GateCase):
    NOTE = "（自己记一下：Bob 喜欢简短的邮件，回复里要提到截止日期是周五）"

    def write_note(self):
        self.fake.user_set_markdown(self.page, self.fake.markdown[self.page] + "\n" + self.NOTE)
        clock.advance(minutes=1)                       # the gate's read covers the edit's minute

    def test_notes_wake_nothing_and_come_with_the_task_in_the_next_feedback_run(self):
        self.write_note()
        _, woke, _ = self.gate()
        self.assertFalse(woke, "a note is not a request")
        self.assertEqual(feedback.pending(self.conn), [])
        self.fake.user_comment(self.page, "帮我起草回复")
        out, woke, run_id = self.gate()
        self.assertTrue(woke)
        self.assertIn("the user's notes on the page (context", out)
        self.assertIn(self.NOTE, out)

    def test_notes_go_into_a_new_agent_card(self):
        self.write_note()
        self.gate()
        kb = FakeKanban()
        delegation.create(self.conn, self.cfg, task_id=self.task, title="起草", goal="draft")
        delegation.submit_pending(self.conn, self.cfg, kb)
        [card] = kb.cards.values()
        self.assertIn("The user's notes on the task page (context", card["body"])
        self.assertIn(self.NOTE, card["body"])

    def test_bot_marker_is_not_user_content(self):
        self.assertEqual(body.outside(self.fake.markdown[self.page]).strip(), "")


def old_fingerprint(region: str) -> str:
    """The box fingerprint before 2026-10-12, still stored by existing workspaces."""
    text = re.sub(r"\\(.)", r"\1", re.sub(r"<[^>]+>|\{[^}]*\}|[*_`~#>\-]", " ", region))
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()[:24]


LINKY = "原帖 https://forum.example.edu/courses/1/discussion/2。通知正文已截断\n证书来自 certs.example.edu 的通知"


class NotionLinkingTests(NotionCase):
    """Notion stores bare URLs and domains as links; only later reads show it (FakeNotion.autolink).
    Seen on the production workspace: each later push took our own box for a user's edit."""

    def setUp(self):
        super().setUp()
        self.fake.autolink = True
        self.task = store.create_task(self.conn, "核对证书", origin="intake", summary=LINKY)
        self.push(self.task)
        self.page = self.page_of(self.task)

    def kinds(self):
        return [f["kind"] for f in feedback.pending(self.conn)]

    def update_summary(self, task, summary):
        store.update_task(self.conn, task, {"summary": summary}, actor="agent", origin="intake",
                          based_on_version=store.get_task(self.conn, task)["version"])
        self.push(task)

    def remember_old_fingerprint(self, page, region):
        self.conn.execute("UPDATE notion_pages SET managed_hash=?, box_seen_hash=? WHERE page_id=?",
                          (old_fingerprint(region), old_fingerprint(region), page))

    def test_our_box_read_back_with_links_is_not_a_box_edit(self):
        self.update_summary(self.task, LINKY + "\n周三前部署")
        self.assertEqual(self.kinds(), [])
        self.assertIn("周三前部署", self.fake.markdown[self.page], "the box is updated, not held")
        self.fake.user_set_markdown(self.page, self.fake.markdown[self.page] + "\n\n我的笔记")
        self.pull()
        self.assertEqual(self.kinds(), [], "a notes edit makes the box be read again")

    def test_a_real_edit_of_linked_text_is_still_a_box_edit(self):
        self.fake.user_set_markdown(self.page, self.fake.markdown[self.page].replace("通知正文已截断", "通知正文完整"))
        self.pull()
        self.assertEqual(self.kinds(), ["body_edit"])

    def test_a_url_before_chinese_text_is_written_as_a_link_of_its_own(self):
        url = "https://forum.example.edu/courses/1/discussion/2"
        self.assertIn(f"原帖 [{url}]({url})。通知正文已截断", self.fake.markdown[self.page])

    def link_mail(self, task):
        self.add_items(mail("m1", "t1", "请核对", subject="Certificate renewal"))
        self.conn.execute("UPDATE source_items SET url='https://mail.google.com/mail/#all/t1' WHERE external_id='m1'")
        store.link_evidence(self.conn, "task", task, self.item_id("m1"), "origin")
        self.push(task)

    def test_a_linked_box_fingerprinted_by_the_earlier_code_is_not_a_box_edit(self):
        # The earlier code wrote bare URLs (Notion linked them) next to the source links, and
        # kept the old fingerprint of what it sent.
        self.link_mail(self.task)
        sent = self.sync.render_body(store.get_task(self.conn, self.task))
        self.assertIn("](https://mail.google.com/", sent)
        earlier = re.sub(r"\[(https?://[^\]]+)\]\(\1\)", r"\1", sent)
        self.assertNotEqual(earlier, sent)
        md = self.fake.markdown[self.page]
        self.fake.markdown[self.page] = md.replace(body.extract(md), autolinked(earlier))
        self.remember_old_fingerprint(self.page, earlier)
        self.update_summary(self.task, LINKY + "\n周三前部署")
        self.assertEqual(self.kinds(), [])
        self.assertIn("周三前部署", self.fake.markdown[self.page])

    def test_a_box_with_source_links_fingerprinted_by_the_earlier_code_is_not_a_box_edit(self):
        task = store.create_task(self.conn, "更新证书", origin="intake", summary="按邮件步骤部署")
        self.link_mail(task)
        page = self.page_of(task)
        region = body.extract(self.fake.markdown[page])
        self.assertIn("](https://mail.google.com/", region)
        self.remember_old_fingerprint(page, region)
        self.update_summary(task, "按邮件步骤部署，周三前")
        self.assertEqual(self.kinds(), [])
        self.assertIn("周三前", self.fake.markdown[page])


class LinkRenderingTests(unittest.TestCase):
    def test_bare_urls_in_text_become_links_that_end_where_the_url_ends(self):
        out = body.render({"summary": "见 https://a.org/x_y?q=1&r=(2)。然后\n(参考 https://c.org/z)",
                           "done_criteria": "打开 https://b.org/~me/p."}, lang="zh-CN")
        self.assertIn("见 [https://a.org/x_y?q=1&r=(2)](https://a.org/x_y?q=1&r=%282%29)。然后", out)
        self.assertIn("(参考 [https://c.org/z](https://c.org/z))", out)
        self.assertIn("打开 [https://b.org/\\~me/p](https://b.org/~me/p).", out)

    def test_a_source_titled_by_its_url_is_linked_once(self):
        out = body.render({"sources": [{"title": "", "url": "https://d.org/1"}]}, lang="en")
        self.assertIn("- [https://d.org/1](https://d.org/1)", out)
        self.assertNotIn("[[", out)

    def test_links_count_by_their_visible_text(self):
        self.assertEqual(body.content_fingerprint("见 [https://a.org/x](https://a.org/x)。然后"),
                         body.content_fingerprint("见 https://a.org/x。然后"))
        self.assertEqual(body.content_fingerprint("证书 [certs.example.edu](http://certs.example.edu) 的"),
                         body.content_fingerprint("证书 certs.example.edu 的"))
        self.assertNotEqual(body.content_fingerprint("见 https://a.org/x。然后"),
                            body.content_fingerprint("见 https://a.org/y。然后"))
