"""R14: one intake run stays within the rendered context budget. An oversize conversation is
processed in segments (the unread part stays pending in its own batch), without blocking other
sources. Model decisions are written by the test (every new item ignored or linked)."""

from dataclasses import replace

from todo_manager import clock, gate_intake, health, intake, store
from todo_manager.db import rules_version
from tests.helpers import WorkspaceCase, mail, slack
from tests.test_intake import FakeSource, run_id_of, wake_of

TOTAL = 20000
EVIDENCE = 12000


class Base(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.gmail = FakeSource("gmail", "mailbox:primary")
        self.slack = FakeSource("slack", "conversation:C1")
        self.cfg.data["wake"].update({"max_context_chars": TOTAL, "max_evidence_chars": EVIDENCE,
                                      "max_items_per_run": 60})
        self.cfg.data["email"]["min_wake_interval_minutes"] = 0

    def gate(self):
        out, woke = gate_intake.run_gate(self.conn, self.cfg, [self.gmail, self.slack], [])
        self.assertEqual(woke, wake_of(out))
        return out

    def context(self, out):
        run_id = run_id_of(out)
        return intake.render_context(self.conn, self.cfg, run_id, gate_intake.manifest_for(self.cfg, run_id))

    def finish(self, out):
        """Model stand-in: ignores every new item of the run (all covered)."""
        run_id = run_id_of(out)
        manifest = gate_intake.manifest_for(self.cfg, run_id)
        ids = [i["id"] for i in intake.run_new_items(self.conn, run_id)]
        return intake.apply_decisions(self.conn, self.cfg, run_id, {
            "run_id": run_id, "rules_version": rules_version(self.conn),
            "items": [{"op": "ignore", "items": ids, "category": "fyi_no_action", "rationale": "test"}]}, manifest)

    def states(self, conversation_key):
        return [r[0] for r in self.conn.execute(
            "SELECT state FROM source_items WHERE conversation_key=? ORDER BY occurred_at", (conversation_key,))]

    def huge_thread(self, n=120, size=500):
        self.slack.push(*[slack(f"{i}.0", "C1", f"message {i:03d} " + "y" * size, thread="T1",
                                minutes=(i - n) / 100)
                          for i in range(n)])
        self.gate()
        clock.advance(minutes=31)


class BudgetTests(Base):
    def test_a_long_email_is_clipped_within_budget(self):
        self.gmail.push(mail("m1", "t1", "Please review. " + "z" * 200000))
        self.gate()
        clock.advance(minutes=16)
        out = self.gate()
        self.assertTrue(wake_of(out))
        context = self.context(out)
        self.assertLessEqual(len(context), TOTAL)
        self.assertIn("more chars; `todo-manager source show", context)

    def test_a_huge_thread_is_processed_in_segments_and_nothing_unread_is_marked_processed(self):
        self.huge_thread()
        key = "slack:C1:T1"
        seen_new: list[str] = []
        runs = 0
        while "batched" in self.states(key):
            out = self.gate()
            self.assertTrue(wake_of(out), "a pending segment keeps waking runs")
            context = self.context(out)
            self.assertLessEqual(len(context), TOTAL)
            run_items = [i["id"] for i in intake.run_new_items(self.conn, run_id_of(out))]
            self.assertTrue(run_items)
            self.assertFalse(set(run_items) & set(seen_new), "no message is given as new twice")
            later = self.conn.execute(
                "SELECT id FROM source_items WHERE conversation_key=? AND occurred_at > (SELECT MAX(occurred_at)"
                " FROM source_items WHERE id IN (%s))" % ",".join("?" * len(run_items)), (key, *run_items)).fetchall()
            for (later_id,) in later:
                self.assertNotIn(later_id, context, "later messages are not shown as earlier context")
            if runs:
                self.assertIn("[earlier]", context, "a continuation sees the preceding messages")
            self.finish(out)
            states = self.states(key)
            self.assertEqual(states.count("processed"), len(seen_new) + len(run_items),
                             "only the rendered segment is marked processed")
            seen_new.extend(run_items)
            runs += 1
            clock.advance(minutes=5)
            self.assertLess(runs, 30)
        self.assertGreater(runs, 1)
        self.assertEqual(len(seen_new), 120)

    def test_context_only_messages_inside_a_conversation_are_still_shown(self):
        bot = replace(slack("2.0", "C1", "CI: build 42 failed", thread="T2", minutes=-2), route="context",
                      author="ci-bot")
        self.slack.push(slack("1.0", "C1", "can you check the results?", thread="T2", minutes=-3), bot,
                        slack("3.0", "C1", "see the bot message above", thread="T2", minutes=-1))
        self.gate()
        clock.advance(minutes=31)
        self.assertIn("CI: build 42 failed", self.context(self.gate()))

    def test_item_count_limit_segments_a_thread(self):
        self.cfg.data["wake"]["max_items_per_run"] = 10
        self.huge_thread(n=25, size=20)
        out = self.gate()
        self.assertEqual(len(intake.run_new_items(self.conn, run_id_of(out))), 10)

    def test_a_segment_waits_while_the_previous_one_is_in_flight_but_other_sources_do_not(self):
        self.huge_thread()
        out1 = self.gate()
        self.gmail.push(mail("m1", "t1", "Can you send the report?"))
        self.gate()
        clock.advance(minutes=16)
        out2 = self.gate()
        self.assertTrue(wake_of(out2))
        sources = {i["source"] for i in intake.run_new_items(self.conn, run_id_of(out2))}
        self.assertEqual(sources, {"gmail"}, "the next segment of the in-flight thread waits; email does not")
        self.finish(out1)
        clock.advance(minutes=5)
        out3 = self.gate()
        self.assertEqual({i["source"] for i in intake.run_new_items(self.conn, run_id_of(out3))}, {"slack"})

    def test_multiple_threads_share_runs_round_robin_within_budget(self):
        self.gmail.push(*[mail(f"m{i}", f"t{i}", f"thread {i} " + "x" * 3500) for i in range(5)])
        self.huge_thread()
        sources_per_run = []
        for _ in range(30):
            out = self.gate()
            if not wake_of(out):
                break
            self.assertLessEqual(len(self.context(out)), TOTAL)
            sources_per_run.append({i["source"] for i in intake.run_new_items(self.conn, run_id_of(out))})
            self.finish(out)
            clock.advance(minutes=5)
        self.assertEqual(sources_per_run[0], {"gmail", "slack"}, "neither source crowds the other out")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM source_items WHERE state='batched'").fetchone()[0], 0)

    def test_open_tasks_and_rules_shrink_the_evidence_budget(self):
        for i in range(60):
            store.create_task(self.conn, f"Task {i:02d} " + "t" * 80, origin="cli", actor="human")
        self.gmail.push(*[mail(f"m{i}", f"t{i}", f"thread {i} " + "x" * 3000) for i in range(4)])
        self.gate()
        clock.advance(minutes=16)
        out = self.gate()
        context = self.context(out)
        self.assertLessEqual(len(context), TOTAL)
        claimed = len(intake.run_batches(self.conn, run_id_of(out)))
        self.assertLess(claimed, 4, "fixed context leaves room for fewer conversations")
        self.assertIn(f"Context: {len(context)} chars", out)

    def test_an_oversize_fixed_context_is_reported_not_silent(self):
        self.cfg.data["wake"]["max_context_chars"] = 3000   # smaller than the decisions format alone
        self.gmail.push(mail("m1", "t1", "Can you send the report?"))
        self.gate()
        clock.advance(minutes=16)
        out = self.gate()
        self.assertTrue(wake_of(out), "still makes progress, one conversation at a time")
        self.assertIn("over budget", out)
        self.assertTrue(any("context" in p and "budget" in p for p in health.report(self.conn, self.cfg)["problems"]))

    def test_a_failed_segment_is_retried_alone_and_does_not_block_the_rest(self):
        self.huge_thread()
        out = self.gate()
        first = sorted(i["id"] for i in intake.run_new_items(self.conn, run_id_of(out)))
        for attempt in range(2):          # the run never applies; its claim goes stale and is retried
            clock.advance(minutes=100)
            out = self.gate()
            self.assertTrue(wake_of(out))
            self.assertEqual(sorted(i["id"] for i in intake.run_new_items(self.conn, run_id_of(out))), first,
                             "the same segment is retried, not merged with the rest")
            self.assertLessEqual(len(self.context(out)), TOTAL)
        clock.advance(minutes=100)
        out = self.gate()                 # attempts exhausted: that segment fails visibly
        failed = self.conn.execute("SELECT COUNT(*) FROM batches WHERE state='failed'").fetchone()[0]
        self.assertEqual(failed, 1)
        self.assertTrue(wake_of(out), "the rest of the thread continues")
        rest = {i["id"] for i in intake.run_new_items(self.conn, run_id_of(out))}
        self.assertFalse(rest & set(first))
        self.assertEqual(self.states("slack:C1:T1").count("processed"), 0, "a failed segment is not processed")
