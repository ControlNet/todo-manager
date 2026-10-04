"""R04 (decision apply is exactly-once per run) and R06 (a follow-up is handled only when
the model's decisions are applied). Model decisions are written by the test."""

import threading

from todo_manager import clock, db, gate_intake, health, intake, store
from todo_manager.db import rules_version
from todo_manager.sources.base import Item
from tests.helpers import WorkspaceCase, mail
from tests.test_intake import FakeSource, run_id_of, wake_of


class Base(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.commits = FakeSource("commit", "commits:o/r")

    def gate(self, commits=False):
        out, woke = gate_intake.run_gate(self.conn, self.cfg, [], [self.commits] if commits else [])
        self.assertEqual(woke, wake_of(out))
        return out

    def woken(self, out):
        run_id = run_id_of(out)
        return run_id, gate_intake.manifest_for(self.cfg, run_id)

    def apply(self, run_id, manifest, items, conn=None):
        conn = conn or self.conn
        return intake.apply_decisions(conn, self.cfg, run_id, {
            "run_id": run_id, "rules_version": rules_version(conn), "items": items}, manifest)

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class ApplyOnceTests(Base):
    def digest_run(self):
        clock.advance(hours=10, minutes=5)  # 21:05 local: digest time
        self.commits.push(Item(source="commit", account="o/r", external_id="abc", conversation_key="repo:o/r",
                               occurred_at=clock.iso(), author="me-gh", title="new loader", body="new loader",
                               route="digest"))
        out = self.gate(commits=True)
        self.assertIn("commit_digest:1", out)
        return self.woken(out)

    def project_items(self):
        return [{"op": "create_project", "ref": "p1", "title": "Data loader", "evidence": [
            {"item": self.conn.execute("SELECT id FROM source_items WHERE external_id='abc'").fetchone()[0]}]}]

    def test_reapplying_a_digest_run_does_nothing_twice(self):
        run_id, manifest = self.digest_run()
        first = self.apply(run_id, manifest, self.project_items())
        again = self.apply(run_id, manifest, self.project_items())   # e.g. the response was lost
        self.assertTrue(again.get("duplicate"))
        self.assertEqual(again["created_projects"], first["created_projects"])
        self.assertEqual(self.count("projects"), 1)

    def test_reapplying_different_decisions_is_refused(self):
        run_id, manifest = self.digest_run()
        self.apply(run_id, manifest, self.project_items())
        with self.assertRaises(intake.DecisionError) as ctx:
            self.apply(run_id, manifest, [])
        self.assertEqual(ctx.exception.code, "ALREADY_APPLIED")
        self.assertEqual(self.count("projects"), 1)

    def test_concurrent_applies_apply_once(self):
        run_id, manifest = self.digest_run()
        items = self.project_items()
        results, errors = [], []

        def worker():
            conn = db.connect(self.cfg.db_path)
            try:
                results.append(self.apply(run_id, manifest, items, conn=conn))
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors)
        self.assertEqual(self.count("projects"), 1)
        self.assertEqual(sum(1 for r in results if not r.get("duplicate")), 1)

    def test_late_result_of_a_stale_claim_is_not_applied(self):
        self.add_items(mail("m1", "t1", "Could you review my draft by Friday?"))
        clock.advance(minutes=61)
        run1, manifest1 = self.woken(self.gate())
        clock.advance(minutes=100)          # run1 never applied: its claim goes stale
        run2, manifest2 = self.woken(self.gate())
        self.assertNotEqual(run1, run2)
        item = self.item_id("m1")
        create = [{"op": "create_task", "title": "审阅草稿", "responsibility": "asked directly",
                   "evidence": [{"item": item, "role": "origin"}]}]
        with self.assertRaises(intake.DecisionError) as ctx:
            self.apply(run1, manifest1, create)
        self.assertEqual(ctx.exception.code, "STALE_RUN")
        self.apply(run2, manifest2, create)
        self.assertEqual(self.count("tasks"), 1)

    def test_same_feedback_item_cannot_be_created_twice_in_one_file(self):
        self.add_items(mail("m1", "t1", "Please send the form"))
        clock.advance(minutes=61)
        run_id, manifest = self.woken(self.gate())
        item = self.item_id("m1")
        op = {"op": "create_task", "title": "寄表格", "responsibility": "asked",
              "evidence": [{"item": item, "role": "origin"}]}
        with self.assertRaises(intake.DecisionError) as ctx:
            self.apply(run_id, manifest, [op, dict(op)])
        self.assertIn("identical", str(ctx.exception))
        self.assertEqual(self.count("tasks"), 0)


class FollowupTests(Base):
    def setUp(self):
        super().setUp()
        self.task = store.create_task(self.conn, "等 Bob 回复合同", origin="cli", actor="human", status="waiting",
                                      waiting_on="Bob", follow_up_date=clock.local_date(self.cfg.tz).isoformat())

    def test_followup_is_not_handled_before_the_decisions_are_applied(self):
        out = self.gate()
        self.assertIn("followups_due:1", out)
        clock.advance(minutes=10)
        self.assertNotIn("followups_due", self.gate(), "claimed by a run in flight")
        clock.advance(minutes=100)       # that run never applied: the follow-up is offered again
        self.assertIn("followups_due:1", self.gate())
        clock.advance(minutes=100)
        out = self.gate()                # attempts exhausted: reported, not silently dropped
        self.assertNotIn("followups_due", out)
        row = self.conn.execute("SELECT state FROM followup_checks WHERE task_id=?", (self.task,)).fetchone()
        self.assertEqual(row["state"], "failed")
        self.assertTrue(any(self.task in p and "follow-up" in p for p in health.report(self.conn, self.cfg)["problems"]))

    def test_review_must_be_explicit(self):
        run_id, manifest = self.woken(self.gate())
        with self.assertRaises(intake.DecisionError) as ctx:
            self.apply(run_id, manifest, [])
        self.assertIn("follow-up", str(ctx.exception))
        self.apply(run_id, manifest, [{"op": "followup_reviewed", "task": self.task, "rationale": "Bob 说下周回复"}])
        clock.advance(minutes=100)
        self.assertNotIn("followups_due", self.gate())

    def test_a_new_follow_up_date_is_a_new_check(self):
        run_id, manifest = self.woken(self.gate())
        self.apply(run_id, manifest, [{"op": "followup_reviewed", "task": self.task, "rationale": "还在等"}])
        tomorrow = clock.local_date(self.cfg.tz).toordinal() + 1
        from datetime import date
        store.update_task(self.conn, self.task, {"follow_up_date": date.fromordinal(tomorrow).isoformat()},
                          actor="human", origin="notion")
        clock.advance(days=1)
        self.assertIn("followups_due:1", self.gate())


class AmbiguityTests(Base):
    """Gap A: noise is ignored; a few high-impact ambiguities become one question each."""

    def woke_with_mail(self, n):
        self.add_items(*[mail(f"m{i}", f"t{i}", f"Are you able to chair session {i}?") for i in range(n)])
        clock.advance(minutes=61)
        return self.woken(self.gate())

    def ask(self, i):
        return {"op": "create_task", "title": f"是否主持第 {i} 场？", "responsibility": "对方直接询问我",
                "status": "decision", "next_actor": "me", "ask": f"要答应主持第 {i} 场吗？",
                "evidence": [{"item": self.item_id(f"m{i}"), "role": "origin"}], "rationale": "deadline unclear"}

    def test_a_question_becomes_a_decision_for_the_user(self):
        run_id, manifest = self.woke_with_mail(1)
        report = self.apply(run_id, manifest, [self.ask(0)])
        [tid] = report["questions"]
        task = store.get_task(self.conn, tid)
        self.assertEqual((task["status"], task["next_actor"], task["next_step"]),
                         ("decision", "me", "要答应主持第 0 场吗？"))

    def test_questions_are_bounded_per_run(self):
        run_id, manifest = self.woke_with_mail(4)
        with self.assertRaises(intake.DecisionError) as ctx:
            self.apply(run_id, manifest, [self.ask(i) for i in range(4)])
        self.assertIn("at most 3 questions", str(ctx.exception))

    def test_a_question_is_a_decision_for_the_user_only(self):
        run_id, manifest = self.woke_with_mail(1)
        op = {**self.ask(0), "status": "todo"}
        with self.assertRaises(intake.DecisionError) as ctx:
            self.apply(run_id, manifest, [op])
        self.assertIn("ask", str(ctx.exception))

    def test_the_prompt_bounds_questions_and_ignores_noise(self):
        self.cfg.data["wake"]["max_questions_per_run"] = 2
        run_id, manifest = self.woke_with_mail(1)
        context = intake.render_context(self.conn, self.cfg, run_id, manifest)
        self.assertIn("At most 2 per run", context)
        self.assertNotIn("{max_questions}", context)
        self.assertIn("not a review queue", context)
