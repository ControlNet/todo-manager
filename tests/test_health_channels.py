"""B12/B13: failures of the feedback gate's own channels (Notion comments and page bodies, the
Kanban steps, any step that raises) are kept as persistent health and cleared by the next clean
run; Notion problems found by a run (an inaccessible page, a duplicate page) are reported too.
None of this creates feedback, wakes the model or sends anything: problems are read in
``todo-manager health`` and the Notion home page.

A source read on its own schedule (the daily commit digest) is stale only once its next read
was due and did not happen, never in between; a problem is identified by what is wrong (the
source scope, the kind of problem), not by its changing text.

Notion is ``FakeNotion`` and Kanban is ``FakeKanban`` (test doubles); the commit adapter is
``FakeSource`` (test double).
"""

from datetime import datetime, timedelta, timezone
from unittest import mock

from todo_manager import clock, delegation, feedback, gate_intake, health
from todo_manager.kanban import KanbanError
from todo_manager.net import HttpError
from todo_manager.sources.base import Item, record_fetch_error
from tests.helpers import WorkspaceCase
from tests.test_feedback import GateCase
from tests.test_intake import FakeSource

RESTRICTED = HttpError(403, "Insufficient permissions for this endpoint.", body={"code": "restricted_resource"})


def channel(conn, source, scope):
    row = conn.execute("SELECT * FROM cursors WHERE source=? AND scope=?", (source, scope)).fetchone()
    return dict(row) if row else None


def sent_elsewhere(conn):
    """Effects queued for anything but Notion or the board (there is no other channel)."""
    return [dict(r) for r in conn.execute("SELECT target, op FROM outbox WHERE target NOT IN ('notion', 'kanban')")]


class ChannelHealthTests(GateCase):
    def problems(self):
        return health.report(self.conn, self.cfg)["problems"]

    def assert_no_wake(self, woke):
        self.assertFalse(woke, "an API failure never wakes the model")
        self.assertEqual(feedback.pending(self.conn, include_claimed=True), [])

    def test_a_failed_comment_poll_is_kept_and_cleared(self):
        self.fake.fail_next[("GET", "/comments")] = RESTRICTED
        _, woke, _ = self.gate()
        self.assert_no_wake(woke)
        row = channel(self.conn, "notion", "comments")
        self.assertEqual(row["consecutive_failures"], 1)
        self.assertIn("Insufficient permissions", row["last_error"])
        self.assertTrue(any(p.startswith("source notion:comments") for p in self.problems()), self.problems())
        self.assertEqual(sent_elsewhere(self.conn), [])
        clock.advance(minutes=5)
        self.gate()
        self.assertEqual(channel(self.conn, "notion", "comments")["consecutive_failures"], 0)
        self.assertFalse(any("notion:comments" in p for p in self.problems()))

    def test_a_failed_body_read_is_kept_and_the_edit_is_read_after_recovery(self):
        self.fake.user_set_markdown(self.page, self.fake.markdown[self.page].replace("Bob 直接问我", "其实是导师转给我的"))
        self.fake.fail_next[("GET", f"/pages/{self.page}/markdown")] = RESTRICTED
        _, woke, _ = self.gate()
        self.assert_no_wake(woke)
        self.assertEqual(channel(self.conn, "notion", "bodies")["consecutive_failures"], 1)
        self.assertTrue(any(p.startswith("source notion:bodies") for p in self.problems()))
        clock.advance(minutes=5)
        _, woke, _ = self.gate()
        self.assertTrue(woke, "the user's edit is read once the body can be read again")
        self.assertEqual(channel(self.conn, "notion", "bodies")["consecutive_failures"], 0)

    def test_failed_kanban_steps_are_kept_per_step_and_cleared(self):
        delegation.create(self.conn, self.cfg, task_id=self.task, title="起草回复", goal="draft")
        self.kb.fail_next["find"] = KanbanError("database is locked")
        _, woke, _ = self.gate()
        self.assert_no_wake(woke)
        submit = channel(self.conn, "kanban", "submit")
        self.assertEqual(submit["consecutive_failures"], 1)
        self.assertIn("database is locked", submit["last_error"])
        self.kb.fail_next["snapshot"] = KanbanError("bridge offline")
        clock.advance(minutes=5)
        self.gate()
        self.assertEqual(channel(self.conn, "kanban", "submit")["consecutive_failures"], 0, "the card was created")
        self.assertEqual(channel(self.conn, "kanban", "cards")["consecutive_failures"], 1)
        self.assertTrue(any(p.startswith("source kanban:cards failing") for p in self.problems()), self.problems())
        clock.advance(minutes=5)
        self.gate()
        self.assertEqual(channel(self.conn, "kanban", "cards")["consecutive_failures"], 0)

    def test_a_step_that_raises_is_kept_as_its_channel_failure(self):
        with mock.patch.object(delegation, "send_messages", side_effect=RuntimeError("bridge returned garbage")):
            _, woke, _ = self.gate()
        self.assert_no_wake(woke)
        row = channel(self.conn, "kanban", "messages")
        self.assertEqual(row["consecutive_failures"], 1)
        self.assertIn("RuntimeError", row["last_error"])
        self.assertEqual(sent_elsewhere(self.conn), [])

    def test_a_channel_is_not_stale_merely_because_its_step_had_nothing_to_do(self):
        self.gate()
        clock.advance(hours=6)
        self.assertFalse([p for p in self.problems() if p.startswith("source kanban") or p.startswith("source notion:")
                          and ("comments" in p or "bodies" in p)])

    def test_an_inaccessible_page_is_reported_not_counted_as_a_channel_failure(self):
        saved = self.fake.pages.pop(self.page)
        self.gate()
        clock.advance(minutes=5)
        self.gate()
        self.assertEqual(channel(self.conn, "notion", "comments")["consecutive_failures"], 0)
        lines = [p for p in self.problems() if "no longer accessible" in p]
        self.assertEqual(len(lines), 1, self.problems())
        self.fake.pages[self.page] = saved
        clock.advance(minutes=5)
        self.gate()
        self.assertFalse([p for p in self.problems() if "no longer accessible" in p], "cleared once the page is back")


class ProblemIdentityTests(WorkspaceCase):
    def keys(self):
        return health.report(self.conn, self.cfg)["problem_keys"]

    def test_the_same_failing_scope_with_changing_text_is_one_problem(self):
        record_fetch_error(self.conn, "gmail", "mailbox:x", "HTTP 503 backend error")
        before = self.keys()
        clock.advance(minutes=7)
        record_fetch_error(self.conn, "gmail", "mailbox:x", "timed out after 60s")
        self.assertEqual(self.keys(), before)
        self.assertEqual(len(before), 1)

    def test_scopes_that_differ_only_in_digits_are_separate_problems(self):
        record_fetch_error(self.conn, "slack", "conversation:C01", "not_in_channel")
        record_fetch_error(self.conn, "slack", "conversation:C02", "not_in_channel")
        self.assertEqual(len(self.keys()), 2)


def commit(sha):
    return Item(source="commit", account="org/repo", external_id=sha, kind="commit", conversation_key="repo:org/repo",
                thread_key=None, occurred_at=clock.iso(), author="me-gh", title=sha, route="digest")


class CommitCadenceTests(WorkspaceCase):
    """The daily digest reads commits once a day at ``commits.digest_time`` (21:00 Auckland here)."""

    def setUp(self):
        super().setUp()
        self.src = FakeSource("commit", "commits:org/repo")

    def at(self, year, month, day, hour, minute=0):
        clock.set_now(datetime(year, month, day, hour, minute, tzinfo=timezone.utc))

    def check(self):
        return gate_intake.commit_digest_check(self.conn, self.cfg, [self.src])

    def stale(self):
        return health.stale_sources(self.conn).get("commit", [])

    def test_a_daily_source_is_not_stale_between_its_reads(self):
        self.at(2026, 10, 5, 8, 0)             # 21:00 NZDT
        self.src.push(commit("a1"))
        self.check()
        clock.advance(minutes=31)
        self.assertEqual(self.stale(), [], "31 minutes after a daily read is not stale")
        clock.advance(hours=23)
        self.assertEqual(self.stale(), [], "the next read is not due yet")

    def test_a_due_read_that_failed_is_stale_at_once(self):
        self.at(2026, 10, 5, 8, 0)
        self.src.push(commit("a1"))
        self.check()
        self.at(2026, 10, 6, 8, 0)             # next day 21:00: due
        self.src.fail("HTTP 502")
        self.check()
        [row] = self.stale()
        self.assertEqual(row["consecutive_failures"], 1)

    def test_a_due_read_that_never_happened_is_stale(self):
        self.at(2026, 10, 5, 8, 0)
        self.src.push(commit("a1"))
        self.check()
        self.at(2026, 10, 6, 8, 31)            # 21:31 next day, no read (gate stopped, digest blocked)
        self.assertEqual(len(self.stale()), 1)

    def test_a_25_hour_day_is_not_reported_before_the_read_is_due(self):
        self.at(2027, 4, 3, 8, 0)              # 21:00 NZDT; the clocks go back the next night
        self.src.push(commit("a1"))
        self.check()
        clock.set_now(datetime(2027, 4, 3, 8, 0, tzinfo=timezone.utc) + timedelta(hours=24, minutes=40))
        self.assertEqual(clock.local_dt(self.cfg.tz).hour, 20, "20:40 NZST, before the 21:00 read")
        self.assertEqual(self.stale(), [])
