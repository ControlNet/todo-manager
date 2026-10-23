"""A feedback run acts on a feedback item only while it holds the item's claim with a live lease.

Run A claims an item and stalls past ``feedback.stale_claim_minutes``; the gate lets run B take
the item over. A's later commands (and its resolution) must not change anything B handles.
Synthetic data in an isolated workspace."""

import json

from tests.flow import run_id_of, user_feedback
from tests.helpers import WorkspaceCase
from tests.test_cli_redesign import invoke
from todo_manager import clock, feedback, gate_feedback, ops, runs, store


class ClaimBindingTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.task = store.create_task(self.conn, "Reply to the partner", origin="cli", actor="human")
        self.fid = user_feedback(self.conn, "fb-1", text="Change the summary", scope_type="task",
                                 scope_id=self.task)

    def gate(self):
        out, woke = gate_feedback.run_gate(self.conn, self.cfg, notion=None, kanban=None)
        self.assertTrue(woke, out)
        return run_id_of(out)

    def taken_over(self):
        """Run A claims the item, stalls past the lease; run B claims it again."""
        a = self.gate()
        clock.advance(minutes=61)
        b = self.gate()
        self.assertEqual(feedback.get(self.conn, self.fid)["claimed_run"], b)
        return a, b

    def update(self, summary, **kw):
        ctx = ops.context(self.conn, self.cfg, feedback_id=self.fid, **kw)
        return ops.task_update(self.conn, self.cfg, ctx, self.task, changes={"summary": summary})

    def summary(self):
        return store.get_task(self.conn, self.task).get("summary")

    def test_a_stale_run_cannot_write_or_resolve_what_a_later_run_took_over(self):
        a, b = self.taken_over()
        for kw in ({"run": a}, {}):    # the run-bound form, and the older form without the run
            with self.assertRaises(ops.OpError) as caught:
                self.update("From the stale run", **kw)
            self.assertEqual(caught.exception.code, "STALE_RUN" if kw else "CLAIMED_BY_RUN")
            with self.assertRaises(ops.OpError) as caught:
                ops.feedback_resolve(self.conn, self.cfg, self.fid, outcome="applied", **kw)
            self.assertEqual(caught.exception.code, "STALE_RUN" if kw else "CLAIMED_BY_RUN")
        self.assertIsNone(self.summary())
        self.assertEqual(feedback.get(self.conn, self.fid)["status"], "pending")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM op_log").fetchone()[0], 0)
        self.update("From the current run", run=b)
        self.assertEqual(self.summary(), "From the current run")
        self.assertEqual(ops.feedback_resolve(self.conn, self.cfg, self.fid, outcome="applied", run=b)["outcome"],
                         "applied")

    def test_the_command_without_a_run_names_the_fix(self):
        self.gate()
        with self.assertRaises(ops.OpError) as caught:
            self.update("Old form")
        self.assertIn("--run", str(caught.exception))
        self.assertIsNone(self.summary())

    def test_a_run_cannot_act_on_an_item_it_does_not_hold(self):
        a = self.gate()
        other = runs.start(self.conn, "feedback")
        runs.finish(self.conn, other, status="woke", wake=True)
        with self.assertRaises(ops.OpError) as caught:
            self.update("Not ours", run=other)
        self.assertEqual(caught.exception.code, "STALE_RUN")
        self.update("Ours", run=a)
        self.assertEqual(self.summary(), "Ours")

    def test_an_exact_retry_keeps_its_first_result_after_the_lease(self):
        a = self.gate()
        first = self.update("Applied once", run=a)
        clock.advance(minutes=61)
        self.gate()
        again = self.update("Applied once", run=a)
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["task"], first["task"])

    def test_manual_handling_waits_for_the_run_then_works(self):
        a = self.gate()
        with self.assertRaises(ops.OpError):
            self.update("By hand")
        ops.run_done(self.conn, self.cfg, a)      # leaves the item unresolved: released
        self.assertIsNone(feedback.get(self.conn, self.fid)["claimed_run"])
        self.update("By hand")
        self.assertEqual(ops.feedback_resolve(self.conn, self.cfg, self.fid, outcome="applied")["outcome"], "applied")

    def test_an_intake_run_does_not_carry_feedback(self):
        intake = runs.start(self.conn, "intake")
        runs.finish(self.conn, intake, status="woke", wake=True)
        with self.assertRaises(ops.OpError) as caught:
            self.update("Wrong run", run=intake)
        self.assertEqual(caught.exception.code, "NOT_FOUND")

    def test_the_cli_takes_the_run_with_the_feedback(self):
        a, b = self.taken_over()
        code, text = invoke(self.tmp, "task", "update", self.task, "--feedback", self.fid, "--run", a,
                            "--set", "summary=Stale")
        self.assertEqual((code, json.loads(text)["error"]), (2, "STALE_RUN"), text)
        code, text = invoke(self.tmp, "feedback", "resolve", self.fid, "--outcome", "applied", "--run", a)
        self.assertEqual((code, json.loads(text)["error"]), (2, "STALE_RUN"), text)
        code, text = invoke(self.tmp, "task", "update", self.task, "--feedback", self.fid, "--run", b,
                            "--set", "summary=Current")
        self.assertEqual(code, 0, text)
        code, text = invoke(self.tmp, "feedback", "resolve", self.fid, "--outcome", "applied", "--run", b)
        self.assertEqual(code, 0, text)
        code, text = invoke(self.tmp, "task", "update", self.task, "--run", b, "--human", "--set", "summary=X")
        self.assertEqual(code, 2, text)

    def test_the_run_context_shows_the_run_bound_commands(self):
        out, _ = gate_feedback.run_gate(self.conn, self.cfg, notion=None, kanban=None)
        run = run_id_of(out)
        self.assertIn(f"--feedback <id> --run {run}", out)
        self.assertIn(f"feedback resolve <feedback id> --run {run}", out)
