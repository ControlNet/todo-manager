"""Boundary 1: a model run never starts over its context budget unless the user opted in.

* Intake: when even one conversation does not fit (the fixed part, rules and open tasks, is too
  large) the run is deferred by default: nothing is claimed or woken, the batch keeps its
  attempts, and health reports and alerts it. ``wake.over_budget = "proceed"`` opts into the
  old behaviour (wake anyway, reported as over budget).
* Feedback: ``feedback.max_context_chars`` bounds the rendered feedback context; a run claims
  the oldest items that fit and leaves the rest pending for the next run; an item that cannot
  fit even alone defers the run the same way.

Sources are ``FakeSource`` (test double); feedback rows are recorded directly.
"""

from todo_manager import clock, feedback, feedback_flow, gate_feedback, gate_intake, health
from tests.helpers import WorkspaceCase, mail
from tests.test_intake import FakeSource, wake_of

ALERTS = "SELECT payload FROM outbox WHERE dedupe_key LIKE 'notify:health:%'"


class IntakeOverBudgetTests(WorkspaceCase):
    overrides = {"notify": {"matrix": True, "matrix_target": "matrix:test-room"}}

    def setUp(self):
        super().setUp()
        self.gmail = FakeSource("gmail", "mailbox:primary")
        self.cfg.data["email"]["min_wake_interval_minutes"] = 0
        self.cfg.data["wake"]["max_context_chars"] = 3000   # smaller than the decisions format alone
        self.gmail.push(mail("m1", "t1", "Can you send the report?"))
        gate_intake.run_gate(self.conn, self.cfg, [self.gmail], [])
        clock.advance(minutes=16)

    def gate(self):
        out, woke = gate_intake.run_gate(self.conn, self.cfg, [self.gmail], [])
        self.assertEqual(woke, wake_of(out))
        return out, woke

    def test_an_oversize_context_defers_the_run_and_alerts(self):
        out, woke = self.gate()
        self.assertFalse(woke, "no model run over budget by default")
        batch = self.conn.execute("SELECT state, attempts, claimed_run FROM batches").fetchone()
        self.assertEqual((batch["state"], batch["attempts"], batch["claimed_run"]), ("mature", 0, None))
        problems = health.report(self.conn, self.cfg)["problems"]
        self.assertTrue(any("deferred" in p and "over budget" in p for p in problems), problems)
        health.raise_alerts(self.conn, self.cfg)
        self.assertTrue(any("over budget" in r["payload"] for r in self.conn.execute(ALERTS)))
        clock.advance(minutes=5)
        _, woke = self.gate()
        self.assertFalse(woke, "still deferred while the cause is there")
        self.cfg.data["wake"]["max_context_chars"] = 60000          # the user fixes the cause
        clock.advance(minutes=5)
        _, woke = self.gate()
        self.assertTrue(woke, "nothing was lost while deferred")
        self.assertFalse(any("deferred" in p for p in health.report(self.conn, self.cfg)["problems"]))

    def test_proceeding_over_budget_is_an_explicit_choice(self):
        self.cfg.data["wake"]["over_budget"] = "proceed"
        out, woke = self.gate()
        self.assertTrue(woke)
        self.assertIn("over budget", out)
        self.assertTrue(any("context" in p and "budget" in p for p in health.report(self.conn, self.cfg)["problems"]))


class FeedbackBudgetTests(WorkspaceCase):
    overrides = {"notify": {"matrix": True, "matrix_target": "matrix:test-room"}}

    def record(self, n, size=2000):
        for i in range(n):
            feedback.record(self.conn, origin="matrix", source_id=f"req_{i}", text=f"请求 {i} " + "x" * size,
                            scope_type="global", kind="direct", author="user", author_is_user=True)
            clock.advance(seconds=1)

    def gate(self):
        out, woke = gate_feedback.run_gate(self.conn, self.cfg, notion=None, kanban=None, notifier=None)
        run_id = out.splitlines()[0].split(": ")[1] if woke else None
        return out, woke, run_id

    def fixed(self):
        return len(feedback_flow.render_context(self.conn, self.cfg, "run_00000000000000000000", []))

    def test_a_run_claims_only_the_oldest_items_that_fit(self):
        self.record(5)
        two = len(feedback_flow.render_context(self.conn, self.cfg, "run_00000000000000000000",
                                               feedback.pending(self.conn)[:2]))
        self.cfg.data["feedback"]["max_context_chars"] = two + 100      # room for two, not three
        _, woke, run_id = self.gate()
        self.assertTrue(woke)
        claimed = feedback.claimed_by(self.conn, run_id)
        self.assertEqual([f["source_id"] for f in claimed], ["req_0", "req_1"])
        context = feedback_flow.render_context(self.conn, self.cfg, run_id, claimed)
        self.assertLessEqual(len(context), self.cfg.get("feedback.max_context_chars"))
        unclaimed = [f for f in feedback.pending(self.conn) if f["claimed_run"] is None]
        self.assertEqual(len(unclaimed), 3, "the rest waits for the next run, untouched")
        self.assertTrue(all(f["attempts"] == 0 for f in unclaimed))

    def test_an_item_that_cannot_fit_alone_defers_and_alerts(self):
        self.record(1, size=8000)
        self.cfg.data["feedback"]["max_context_chars"] = self.fixed() + 1000
        _, woke, _ = self.gate()
        self.assertFalse(woke)
        [fb] = feedback.pending(self.conn)
        self.assertEqual((fb["claimed_run"], fb["attempts"]), (None, 0))
        problems = health.report(self.conn, self.cfg)["problems"]
        self.assertTrue(any(p.startswith("feedback run deferred") for p in problems), problems)
        self.cfg.data["feedback"]["over_budget"] = "proceed"
        _, woke, run_id = self.gate()
        self.assertTrue(woke, "opted in: the item is handled over budget")
        self.assertEqual(len(feedback.claimed_by(self.conn, run_id)), 1)
