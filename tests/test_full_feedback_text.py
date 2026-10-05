"""C03: what the user wrote in Notion is stored in full; only the display is budgeted, and every
cut on display says so and how to read the rest.

* page_notes (added/removed lines, the notes baseline), body_edit regions and a new page's body
  are stored without any length cut (``payload.complete``); the supersede/carry-over logic of
  notes works on the full lines.
* The context shows each item's own text in full (removed lines, the box as edited and the
  lines that differ from ours, a new page's body); background (task fields, deliveries, earlier
  feedback) may be cut, always with a "…[+N chars]" mark, and earlier feedback names its id for
  ``todo-manager feedback item <id>``, which prints a stored item in full.
* An item whose text may be incomplete (the Notion API cut the page body, or an older version
  stored it cut) is marked as such and can only be answered with ``clarify``.
* An item larger than the context budget is deferred by the existing budget rule, not cut.

Notion is ``FakeNotion`` (test double; ``truncate_markdown_at`` imitates the API's truncated
flag), Kanban is ``FakeKanban``; model decisions are written by the test.
"""

import contextlib
import io
import json

from todo_manager import cli, clock, feedback, feedback_flow, store
from todo_manager.feedback_flow import FeedbackDecisionError
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
        return feedback_flow.render_context(self.conn, self.cfg, run_id, items)


class StoredInFullTests(Base):
    def test_a_long_note_is_stored_and_shown_in_full(self):
        self.notes(LONG, TAIL)
        run_id, [fb] = self.run_items()
        self.assertEqual(fb["payload"]["added"], LONG + "\n" + TAIL)
        self.assertTrue(fb["payload"]["complete"])
        self.assertEqual(fb["text"], LONG + "\n" + TAIL)
        context = self.context(run_id, [fb])
        self.assertIn(TAIL, context)
        self.assertIn(LONG, context)

    def test_long_removed_lines_are_kept_in_full(self):
        self.notes(LONG, "保留这一行")
        run_id, [fb] = self.run_items()
        self.decide(run_id, [{"feedback": fb["id"], "outcome": "ignored", "reason": "笔记"}])
        self.notes("保留这一行")
        run_id, [fb] = self.run_items()
        self.assertEqual(fb["payload"]["removed"], LONG)
        self.assertIn(LONG, self.context(run_id, [fb]))

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

    def test_a_new_page_body_is_stored_and_shown_in_full(self):
        self.fake.user_create_page(self.tasks_ds, "写基金申请", markdown=LONG + "\n" + TAIL)
        clock.advance(minutes=1)
        run_id, items = self.run_items()
        [fb] = [f for f in items if f["kind"] == "new_task"]
        self.assertEqual(fb["payload"]["body"], LONG + "\n" + TAIL)
        self.assertIn(TAIL, self.context(run_id, [fb]))

    def test_a_long_box_edit_is_shown_in_full_with_the_differing_lines(self):
        edited = self.original.replace("Bob 直接问我", "其实是导师转给我的；" + LONG)
        self.fake.user_set_markdown(self.page, edited)
        clock.advance(minutes=1)
        run_id, [fb] = self.run_items()
        context = self.context(run_id, [fb])
        self.assertIn(LONG, context)
        self.assertNotIn("…[+", context.split("### ")[1].split("Task t")[0], "the item itself is never cut")

    def test_over_the_context_budget_it_waits_instead_of_being_cut(self):
        self.cfg.data.setdefault("feedback", {})["max_context_chars"] = 3000
        self.notes(LONG)
        _, woke, _ = self.gate()
        self.assertFalse(woke)
        [fb] = feedback.pending(self.conn)
        self.assertEqual(fb["payload"]["added"], LONG)


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
        self.assertIn("…[+", context)
        self.assertIn(first["id"], context, "the earlier item is named, so it can be read in full")
        self.assertIn("todo-manager feedback item", context)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--home", str(self.cfg.home), "feedback", "item", first["id"]]), 0)
        shown = json.loads(out.getvalue())
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
        with self.assertRaises(FeedbackDecisionError) as ctx:
            self.decide(run_id, [{"feedback": fb["id"], "outcome": "ignored", "reason": "笔记"}])
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
        with self.assertRaises(FeedbackDecisionError):
            self.decide(run_id, [{"feedback": fb["id"], "outcome": "ignored", "reason": "笔记"}])
