"""C03: what the user wrote in Notion is stored in full; only the display is budgeted, and every
cut on display says so and how to read the rest.

* page_notes (added/removed lines, the notes baseline), body_edit regions and a new page's body
  are stored without any length cut (``payload.complete``); the supersede/carry-over logic of
  notes works on the full lines.
* The context shows each item's own text up to ``wake.max_item_chars``; longer text, and
  background (task fields, deliveries, earlier feedback), is cut with a
  "[... N more characters: `todo-manager feedback item <id>`]" mark naming the command that
  prints the stored item in full. Nothing is cut silently.
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
        self.assertIn(f"more characters: `todo-manager feedback item {fb['id']}`", context)

    def shown_in_full(self, fid):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--home", str(self.cfg.home), "feedback", "item", fid]), 0)
        return json.loads(out.getvalue())


class StoredInFullTests(Base):
    def test_a_long_note_is_stored_in_full_and_its_display_says_where_the_rest_is(self):
        self.notes(LONG, TAIL)
        run_id, [fb] = self.run_items()
        self.assertEqual(fb["payload"]["added"], LONG + "\n" + TAIL)
        self.assertTrue(fb["payload"]["complete"])
        self.assertEqual(fb["text"], LONG + "\n" + TAIL)
        self.assert_clipped_with_pointer(self.context(run_id, [fb]), fb, LONG[:3000])
        self.assertEqual(self.shown_in_full(fb["id"])["text"], LONG + "\n" + TAIL)

    def test_long_removed_lines_are_kept_in_full(self):
        self.notes(LONG, "保留这一行")
        run_id, [fb] = self.run_items()
        self.decide(run_id, [{"feedback": fb["id"], "outcome": "ignored", "reason": "笔记"}])
        self.notes("保留这一行")
        run_id, [fb] = self.run_items()
        self.assertEqual(fb["payload"]["removed"], LONG)
        self.assert_clipped_with_pointer(self.context(run_id, [fb]), fb, LONG[:3000])

    def test_the_baseline_of_long_notes_is_kept_in_full(self):
        self.cfg.data.setdefault("feedback", {})["max_context_chars"] = 200000
        big = ["第%d行：" % n + "内容" * 400 for n in range(80)]          # ~65k chars of notes
        self.notes(*big)
        run_id, [fb] = self.run_items()
        self.decide(run_id, [{"feedback": fb["id"], "outcome": "ignored", "reason": "笔记"}])
        self.notes(*big, TAIL)
        run_id, [fb] = self.run_items()
        self.assertEqual(fb["payload"]["added"], TAIL, "only the appended line, no tail of the old notes")
        self.assertEqual(fb["payload"]["removed"], "")

    def test_an_append_keeps_a_long_pending_note_valid(self):
        self.notes(LONG)
        _, [a] = self.run_items()
        self.notes(LONG, TAIL)
        self.gate()
        self.assertEqual(feedback.get(self.conn, a["id"])["status"], "pending", "still exactly on the page")

    def test_changing_the_end_of_a_long_line_supersedes_it(self):
        self.notes(LONG)
        _, [a] = self.run_items()
        self.notes(LONG[:-1] + "改")
        self.gate()
        self.assertEqual(feedback.get(self.conn, a["id"])["status"], "superseded")

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
        self.notes(LONG)
        out, woke, run_id = self.gate()
        self.assertTrue(woke)
        [fb] = feedback.claimed_by(self.conn, run_id)
        self.assertEqual(fb["payload"]["added"], LONG)
        self.assert_clipped_with_pointer(out, fb, LONG[:1000])


class DisplayCutTests(Base):
    def test_background_cuts_are_marked_and_earlier_items_can_be_read_in_full(self):
        store.update_task(self.conn, self.task, {"summary": "摘要" * 600}, actor="agent", origin="intake",
                          based_on_version=store.get_task(self.conn, self.task)["version"])
        self.gate()                                   # pushes the new summary into the box
        self.original = self.fake.markdown[self.page]
        clock.advance(minutes=1)
        self.notes(LONG)
        run_id, [first] = self.run_items()
        self.decide(run_id, [{"feedback": first["id"], "outcome": "ignored", "reason": "笔记"}])
        self.notes(LONG, TAIL)
        run_id, [fb] = self.run_items()
        context = self.context(run_id, [fb])
        self.assertIn("more characters: `todo-manager task show", context, "the long summary is cut, with a pointer")
        self.assertIn(first["id"], context, "the earlier item is named, so it can be read in full")
        self.assertIn(f"todo-manager feedback item {first['id']}", context)
        shown = self.shown_in_full(first["id"])
        self.assertEqual(shown["payload"]["added"], LONG)
        self.assertEqual(shown["text"], LONG)

    def test_an_unknown_item_is_not_found(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertNotEqual(cli.main(["--home", str(self.cfg.home), "feedback", "item", "fbk_nope"]), 0)


class IncompleteTextTests(Base):
    def test_a_body_cut_by_the_api_can_only_be_clarified(self):
        self.fake.truncate_markdown_at = len(self.original) + 200
        self.notes(LONG, TAIL)
        run_id, [fb] = self.run_items()
        self.assertTrue(fb["payload"]["truncated"])
        context = self.context(run_id, [fb])
        self.assertIn("INCOMPLETE", context)
        with self.assertRaises(OpError) as ctx:
            self.decide(run_id, [{"feedback": fb["id"], "outcome": "ignored", "reason": "笔记"}])
        self.assertEqual(ctx.exception.code, "INCOMPLETE")
        self.assertIn("clarify", str(ctx.exception))
        report = self.decide(run_id, [{"feedback": fb["id"], "outcome": "clarify",
                                       "reply": "笔记太长被截断了，能把关键要求单独写一条评论吗？"}])
        self.assertEqual(report["clarify"], [fb["id"]])

    def test_an_item_stored_cut_by_an_older_version_can_only_be_clarified(self):
        self.notes("旧版本记录的笔记")
        run_id, [fb] = self.run_items()
        legacy = {"page_id": self.page, "added": "x" * 3000, "removed": "", "notes": "x" * 3000,
                  "truncated": False, "revision": 1}
        self.conn.execute("UPDATE feedback SET payload=?, text=? WHERE id=?", (json.dumps(legacy), "x" * 3000, fb["id"]))
        [fb] = feedback.claimed_by(self.conn, run_id)
        self.assertIn("INCOMPLETE", self.context(run_id, [fb]))
        with self.assertRaises(OpError):
            self.decide(run_id, [{"feedback": fb["id"], "outcome": "ignored", "reason": "笔记"}])
