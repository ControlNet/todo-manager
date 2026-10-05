"""C02: an edit of the page body that our last read may not have seen keeps the older
interpretation from being applied, whatever the precision of the edit timestamps.

One predicate (``notion.body.edit_unread``) decides "the last edit may postdate our read" for
the body re-read, for claiming body_edit items and for applying them. No timestamp
precision is assumed: a stamp on a whole minute (Notion rounds edit times) may stand for any
moment of that minute, so only a read after the end of the minute covers it; a stamp with
seconds is taken as precise to the second. An item waits (unclaimed, or released at apply)
until a read covers the last edit; that read then supersedes or keeps it. (The user's own
notes outside the box are context, not feedback: tests/test_notion_body.py.)

Notion is ``FakeNotion`` (test double; minute-rounded stamps by default, ``precise_times``
for second-precision stamps), Kanban is ``FakeKanban``; model decisions are written by the test.
"""

from datetime import timedelta

from todo_manager import clock, feedback
from todo_manager.notion import body
from tests.test_feedback import GateCase


def pending(conn, kind="body_edit"):
    return [f for f in feedback.pending(conn, include_claimed=True) if f["kind"] == kind]


class Base(GateCase):
    def setUp(self):
        super().setUp()
        self.original = self.fake.markdown[self.page]
        self.cfg.data.setdefault("notion", {})["body_reads_per_run"] = 10

    def at(self, minutes, seconds=0):
        clock.set_now(self.t0 + timedelta(minutes=minutes, seconds=seconds))

    def box(self, text):
        """The user rewrites a line inside the managed box."""
        self.assertIn("Bob 直接问我", self.original)
        self.fake.user_set_markdown(self.page, self.original.replace("Bob 直接问我", text))

    def body_not_read(self):
        """The gate pulls the page (its new edit time is known) but does not get to its body."""
        self.cfg.data["notion"]["body_reads_per_run"] = 0
        self.pull()
        self.cfg.data["notion"]["body_reads_per_run"] = 10

    def ignore(self, run_id, fid):
        return self.decide(run_id, [{"feedback": fid, "outcome": "ignored", "reason": "已知"}])


class CoarseTimestampTests(Base):
    def setUp(self):
        super().setUp()
        self.t0 = clock.now()

    def test_a_box_edit_read_in_the_minute_of_its_edit_waits_for_a_covering_read(self):
        self.at(3, 5)
        self.box("要求：提到截止日期")
        self.at(3, 10)
        _, woke, _ = self.gate()
        self.assertFalse(woke, "the read may predate a later edit stamped with the same minute")
        [a] = pending(self.conn)
        self.at(3, 20)
        self.box("要求：不用提截止日期")                   # same stamp as the first edit
        self.at(4, 5)
        _, woke, run_id = self.gate()
        self.assertTrue(woke)
        self.assertEqual(feedback.get(self.conn, a["id"])["status"], "superseded")
        [b] = feedback.claimed_by(self.conn, run_id)
        self.assertIn("不用提", b["payload"]["region"])

    def test_an_edit_in_the_minute_of_the_read_defers_the_claimed_box_edit(self):
        self.at(3, 5)
        self.box("要求：提到截止日期")
        self.at(4, 5)
        _, _, run_id = self.gate()
        [a] = feedback.claimed_by(self.conn, run_id)
        self.at(4, 20)
        self.box("要求：不用提截止日期")                   # stamped 4:00, the minute of the read
        self.at(4, 30)
        self.body_not_read()
        report = self.ignore(run_id, a["id"])
        self.assertEqual(report["deferred"], [a["id"]], "the claimed box edit may be out of date")
        self.at(5, 5)
        self.gate()
        self.assertEqual(feedback.get(self.conn, a["id"])["status"], "superseded")


class PreciseTimestampTests(Base):
    def setUp(self):
        super().setUp()
        self.fake.precise_times = True
        self.t0 = clock.now()

    def test_a_read_after_the_edit_second_covers_it(self):
        self.at(3, 5)
        self.box("要求：提到截止日期")
        self.at(3, 10)
        _, woke, run_id = self.gate()
        self.assertTrue(woke, "no minute wait when the stamp has seconds")
        [a] = feedback.claimed_by(self.conn, run_id)
        self.at(3, 15)
        self.assertEqual(self.ignore(run_id, a["id"])["ignored"], [a["id"]])

    def test_a_later_edit_in_the_same_minute_defers_the_claimed_box_edit(self):
        self.at(3, 5)
        self.box("要求：提到截止日期")
        self.at(3, 10)
        _, _, run_id = self.gate()
        [a] = feedback.claimed_by(self.conn, run_id)
        self.at(3, 20)
        self.box("要求：不用提截止日期")
        self.at(3, 25)
        self.body_not_read()
        self.assertEqual(self.ignore(run_id, a["id"])["deferred"], [a["id"]])


class PredicateTests(GateCase):
    def test_no_precision_is_assumed(self):
        unread = lambda edited, read: body.edit_unread(edited, "user-0001", read, "bot")  # noqa: E731
        self.assertTrue(unread("2026-10-05T10:05:00.000Z", "2026-10-05T10:05:59.999Z"))
        self.assertFalse(unread("2026-10-05T10:05:00.000Z", "2026-10-05T10:06:00.000Z"))
        self.assertTrue(unread("2026-10-05T10:05:30.000Z", "2026-10-05T10:05:30.500Z"))
        self.assertFalse(unread("2026-10-05T10:05:30.000Z", "2026-10-05T10:05:31.000Z"))
        self.assertTrue(unread("2026-10-05T10:05:30.000Z", None), "never read")
        self.assertFalse(body.edit_unread("2026-10-05T10:05:00.000Z", "bot", "2026-10-05T10:05:01.000Z", "bot"),
                         "our own write")
