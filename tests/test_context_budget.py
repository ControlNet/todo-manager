"""Boundary 1: the context budget limits how much one run takes; it never keeps material waiting.

* Intake: conversations that do not fit ``wake.max_context_chars`` wait for the next run; the
  first one is always taken, its items clipped (``wake.max_item_chars``) with a pointer to the
  full text. A run whose context is still over budget runs anyway and is reported by ``health``.
* Feedback: ``feedback.max_context_chars`` bounds the rendered feedback context; a run claims
  the oldest items that fit and leaves the rest pending, untouched, for the next run; an item
  that cannot fit even alone is clipped and handled, never deferred.

Sources are ``FakeSource`` (test double); feedback rows are recorded directly.
"""

from todo_manager import clock, feedback, gate_feedback, gate_intake, health
from tests.flow import feedback_context, run_id_of, run_summary, wake_of
from tests.helpers import WorkspaceCase, mail
from tests.test_intake import FakeSource


class IntakeOverBudgetTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.gmail = FakeSource("gmail", "mailbox:primary")
        self.cfg.data["email"]["min_wake_interval_minutes"] = 0
        self.cfg.data["wake"]["max_context_chars"] = 500   # smaller than the fixed part of the context
        self.gmail.push(mail("m1", "t1", "Can you send the report?"))
        gate_intake.run_gate(self.conn, self.cfg, [self.gmail], [])
        clock.advance(minutes=16)

    def test_an_oversize_context_still_runs_and_is_reported(self):
        out, woke = gate_intake.run_gate(self.conn, self.cfg, [self.gmail], [])
        self.assertEqual(woke, wake_of(out))
        self.assertTrue(woke, "material never waits on the budget")
        self.assertIn("Can you send the report?", out)
        batch = self.conn.execute("SELECT state, claimed_run FROM batches").fetchone()
        self.assertEqual((batch["state"], batch["claimed_run"]), ("claimed", run_id_of(out)))
        self.assertGreater(run_summary(self.conn, run_id_of(out))["context_over_budget"], 500)
        problems = health.report(self.conn, self.cfg)["problems"]
        self.assertTrue(any("over budget" in p and "wake.max_context_chars" in p for p in problems), problems)


class FeedbackBudgetTests(WorkspaceCase):
    def record(self, n, size=2000):
        for i in range(n):
            feedback.record(self.conn, origin="matrix", source_id=f"req_{i}", text=f"请求 {i} " + "x" * size,
                            scope_type="global", kind="direct", author="user", author_is_user=True)
            clock.advance(seconds=1)

    def gate(self):
        out, woke = gate_feedback.run_gate(self.conn, self.cfg, notion=None, kanban=None)
        self.assertEqual(woke, wake_of(out))
        return out, woke, run_id_of(out) if woke else None

    def fixed(self):
        return len(feedback_context(self.conn, self.cfg, "run_00000000000000000000", []))

    def test_a_run_claims_only_the_oldest_items_that_fit(self):
        self.record(5)
        two = len(feedback_context(self.conn, self.cfg, "run_00000000000000000000", feedback.pending(self.conn)[:2]))
        self.cfg.data["feedback"]["max_context_chars"] = two + 100      # room for two, not three
        out, woke, run_id = self.gate()
        self.assertTrue(woke)
        claimed = feedback.claimed_by(self.conn, run_id)
        self.assertEqual([f["source_id"] for f in claimed], ["req_0", "req_1"])
        self.assertLessEqual(len(out), self.cfg.get("feedback.max_context_chars"))
        unclaimed = [f for f in feedback.pending(self.conn) if f["claimed_run"] is None]
        self.assertEqual(len(unclaimed), 3, "the rest waits for the next run, untouched")
        self.assertTrue(all(f["attempts"] == 0 for f in unclaimed))

    def test_an_item_that_cannot_fit_alone_is_clipped_and_handled(self):
        self.record(1, size=8000)
        self.cfg.data["feedback"]["max_context_chars"] = self.fixed() + 1000
        out, woke, run_id = self.gate()
        self.assertTrue(woke, "never deferred")
        [fb] = feedback.claimed_by(self.conn, run_id)
        self.assertIn(f"todo-manager feedback item {fb['id']}", out, "a pointer to the full text")
        self.assertNotIn("x" * 5000, out)
        problems = health.report(self.conn, self.cfg)["problems"]
        self.assertTrue(any("over budget" in p and "feedback.max_context_chars" in p for p in problems), problems)
