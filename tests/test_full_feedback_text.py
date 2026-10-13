"""C03: what the user wrote in Notion is stored in full; only the display is budgeted, and every
cut on display says so and how to read the rest.

* Comments, body_edit regions and a new page's body are stored without any length cut
  (``payload.complete``); the user's own page notes (context, not feedback) are kept in full in
  ``notion_pages.notes_text``.
* The context shows each item's own text up to ``wake.max_item_chars``; longer text, and
  background (task fields, page notes, deliveries, earlier feedback), is cut with a
  "[... N more characters: `<command>`]" mark naming the command that prints it in full
  (``todo-manager feedback show <id>`` / ``todo-manager task show <id>``). Nothing is cut silently.
* An item whose text may be incomplete (the Notion API cut the page body, or an older version
  stored it cut) is marked as such and can only be answered with ``clarify``.
* An item larger than the context budget is clipped and handled; it never waits for room.

Notion is ``FakeNotion`` (test double; ``truncate_markdown_at`` imitates the API's truncated
flag), Kanban is ``FakeKanban``; what the agent records is written by the test.
"""

import contextlib
import io
import json

from todo_manager import cli, clock, feedback, store
from todo_manager.ops import OpError
from tests.flow import feedback_context
from tests.test_feedback import GateCase

LONG = "背景：" + "很长的说明" * 900                    # 4,503 chars on one line
TAIL = "最后一条要求：附上原始数据"


class Base(GateCase):
    def setUp(self):
        super().setUp()
        self.original = self.fake.markdown[self.page]

    def notes(self, *lines):
        self.fake.user_set_markdown(self.page, self.original + ("\n\n" + "\n".join(lines) if lines else ""))
        clock.advance(minutes=1)

    def run_items(self):
        _, woke, run_id = self.gate()
        self.assertTrue(woke)
        return run_id, feedback.claimed_by(self.conn, run_id)

    def context(self, run_id, items):
        return feedback_context(self.conn, self.cfg, run_id, items)

    def assert_clipped_with_pointer(self, context, fb, start):
        self.assertIn(start, context)
        self.assertIn(f"more characters: `todo-manager feedback show {fb['id']}`", context)

    def shown_in_full(self, fid):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--home", str(self.cfg.home), "feedback", "show", fid]), 0)
        return json.loads(out.getvalue())


class StoredInFullTests(Base):
    def test_a_long_comment_is_stored_in_full_and_its_display_says_where_the_rest_is(self):
        self.fake.user_comment(self.page, LONG + "\n" + TAIL)
        run_id, [fb] = self.run_items()
        self.assertEqual(fb["text"], LONG + "\n" + TAIL)
        self.assert_clipped_with_pointer(self.context(run_id, [fb]), fb, LONG[:3000])
        self.assertEqual(self.shown_in_full(fb["id"])["text"], LONG + "\n" + TAIL)

    def test_long_page_notes_are_kept_in_full_and_shown_with_a_pointer(self):
        big = ["第%d行：" % n + "内容" * 400 for n in range(80)]          # ~65k chars of notes
        self.notes(*big, TAIL)
        self.gate()
        notes = "\n".join(big + [TAIL])
        self.assertEqual(self.conn.execute("SELECT notes_text FROM notion_pages WHERE page_id=?",
                                           (self.page,)).fetchone()[0], notes)
        self.fake.user_comment(self.page, "按笔记准备")
        run_id, [fb] = self.run_items()
        context = self.context(run_id, [fb])
        self.assertIn(big[0][:200], context)
        self.assertIn(f"more characters: `todo-manager task show {self.task}`", context)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--home", str(self.cfg.home), "task", "show", self.task]), 0)
        self.assertEqual(json.loads(out.getvalue())["page_notes"], notes)

    def test_a_new_page_body_is_stored_in_full(self):
        self.fake.user_create_page(self.tasks_ds, "写基金申请", markdown=LONG + "\n" + TAIL)
        clock.advance(minutes=1)
        run_id, items = self.run_items()
        [fb] = [f for f in items if f["kind"] == "new_task"]
        self.assertEqual(fb["payload"]["body"], LONG + "\n" + TAIL)
        self.assert_clipped_with_pointer(self.context(run_id, [fb]), fb, LONG[:3000])

    def test_a_long_box_edit_is_shown_with_the_differing_lines_and_a_pointer(self):
        edited = self.original.replace("Bob 直接问我", "其实是导师转给我的；" + LONG)
        self.fake.user_set_markdown(self.page, edited)
        clock.advance(minutes=1)
        run_id, [fb] = self.run_items()
        self.assert_clipped_with_pointer(self.context(run_id, [fb]), fb, "其实是导师转给我的；" + LONG[:3000])
        self.assertIn(LONG, json.dumps(self.shown_in_full(fb["id"])["payload"], ensure_ascii=False))

    def test_over_the_context_budget_it_is_clipped_and_handled_never_waits(self):
        self.cfg.data.setdefault("feedback", {})["max_context_chars"] = 3000
        self.fake.user_comment(self.page, LONG)
        out, woke, run_id = self.gate()
        self.assertTrue(woke)
        [fb] = feedback.claimed_by(self.conn, run_id)
        self.assertEqual(fb["text"], LONG)
        self.assert_clipped_with_pointer(out, fb, LONG[:1000])


class DisplayCutTests(Base):
    def test_background_cuts_are_marked_and_earlier_items_can_be_read_in_full(self):
        store.update_task(self.conn, self.task, {"summary": "摘要" * 600}, actor="agent", origin="intake",
                          based_on_version=store.get_task(self.conn, self.task)["version"])
        self.gate()                                   # pushes the new summary into the box
        self.fake.user_comment(self.page, LONG)
        clock.advance(minutes=31)   # a new task without agent work: its comments are read every warm interval
        run_id, [first] = self.run_items()
        self.decide(run_id, [{"feedback": first["id"], "outcome": "ignored", "reason": "背景"}])
        self.fake.user_comment(self.page, TAIL)
        run_id, [fb] = self.run_items()
        context = self.context(run_id, [fb])
        self.assertIn("more characters: `todo-manager task show", context, "the long summary is cut, with a pointer")
        self.assertIn(first["id"], context, "the earlier item is named, so it can be read in full")
        self.assertIn(f"todo-manager feedback show {first['id']}", context)
        self.assertEqual(self.shown_in_full(first["id"])["text"], LONG)

    def test_an_unknown_item_is_not_found(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertNotEqual(cli.main(["--home", str(self.cfg.home), "feedback", "show", "fbk_nope"]), 0)


class IncompleteTextTests(Base):
    def test_a_body_cut_by_the_api_can_only_be_clarified(self):
        self.fake.user_create_page(self.tasks_ds, "写基金申请", markdown=LONG + "\n" + TAIL)
        self.fake.truncate_markdown_at = 1000
        clock.advance(minutes=1)
        run_id, items = self.run_items()
        [fb] = [f for f in items if f["kind"] == "new_task"]
        self.assertTrue(fb["payload"]["truncated"])
        context = self.context(run_id, [fb])
        self.assertIn("INCOMPLETE", context)
        with self.assertRaises(OpError) as ctx:
            self.decide(run_id, [{"feedback": fb["id"], "outcome": "ignored", "reason": "说明"}])
        self.assertEqual(ctx.exception.code, "INCOMPLETE")
        self.assertIn("clarify", str(ctx.exception))
        report = self.decide(run_id, [{"feedback": fb["id"], "outcome": "clarify",
                                       "reply": "页面内容太长被截断了，能把关键要求单独写一条评论吗？"}])
        self.assertEqual(report["clarify"], [fb["id"]])

    def test_an_item_stored_cut_by_an_older_version_can_only_be_clarified(self):
        self.fake.user_create_page(self.tasks_ds, "写基金申请", markdown="旧版本记录的页面")
        clock.advance(minutes=1)
        run_id, items = self.run_items()
        [fb] = [f for f in items if f["kind"] == "new_task"]
        legacy = {**fb["payload"], "body": "x" * 4000}
        legacy.pop("complete", None)
        self.conn.execute("UPDATE feedback SET payload=? WHERE id=?", (json.dumps(legacy), fb["id"]))
        [fb] = [f for f in feedback.claimed_by(self.conn, run_id) if f["kind"] == "new_task"]
        self.assertIn("INCOMPLETE", self.context(run_id, [fb]))
        with self.assertRaises(OpError):
            self.decide(run_id, [{"feedback": fb["id"], "outcome": "ignored", "reason": "说明"}])
