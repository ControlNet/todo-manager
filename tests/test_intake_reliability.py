"""R04 (an outcome is recorded exactly once, also when a command is retried or runs
concurrently) and R06 (a follow-up is handled only when the run records it). The agent's
commands are issued by the test."""

import threading

from todo_manager import clock, db, gate_intake, health, ops, store
from todo_manager.sources.base import Item
from tests.flow import run_ctx, run_id_of, wake_of
from tests.helpers import WorkspaceCase, mail
from tests.test_intake import FakeSource


class Base(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.commits = FakeSource("commit", "commits:o/r")

    def gate(self, commits=False):
        out, woke = gate_intake.run_gate(self.conn, self.cfg, [], [self.commits] if commits else [])
        self.assertEqual(woke, wake_of(out))
        return out

    def ctx(self, run_id, conn=None):
        return run_ctx(conn or self.conn, self.cfg, run_id)

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class ApplyOnceTests(Base):
    def digest_run(self):
        clock.advance(hours=8, minutes=5)  # 21:05 local: digest time
        self.commits.push(Item(source="commit", account="o/r", external_id="abc", conversation_key="repo:o/r",
                               occurred_at=clock.iso(), author="me-gh", title="new loader", body="new loader",
                               route="digest"))
        out = self.gate(commits=True)
        self.assertIn("commit_digest:1", out)
        return run_id_of(out)

    def create_project(self, run_id, conn=None):
        conn = conn or self.conn
        item = conn.execute("SELECT id FROM source_items WHERE external_id='abc'").fetchone()[0]
        return ops.project_create(conn, self.cfg, self.ctx(run_id, conn), title="Data loader", fields={}, repos=[],
                                  evidence=[(item, "context")])

    def test_repeating_a_digest_run_command_does_nothing_twice(self):
        run_id = self.digest_run()
        first = self.create_project(run_id)
        again = self.create_project(run_id)   # e.g. the response was lost
        self.assertTrue(again.get("duplicate"))
        self.assertEqual(again["project"], first["project"])
        self.assertEqual(self.count("projects"), 1)
        ops.digest_done(self.conn, self.cfg, self.ctx(run_id))
        self.assertTrue(ops.digest_done(self.conn, self.cfg, self.ctx(run_id))["duplicate"])

    def test_concurrent_retries_apply_once(self):
        run_id = self.digest_run()
        results, errors = [], []

        def worker():
            conn = db.connect(self.cfg.db_path)
            try:
                results.append(self.create_project(run_id, conn=conn))
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
        run1 = run_id_of(self.gate())
        clock.advance(minutes=100)          # run1 never reported back: its lease expired
        run2 = run_id_of(self.gate())
        self.assertNotEqual(run1, run2)
        item = self.item_id("m1")
        with self.assertRaises(ops.OpError) as caught:
            ops.task_create(self.conn, self.cfg, self.ctx(run1), title="审阅草稿", evidence=[(item, "origin")])
        self.assertEqual(caught.exception.code, "STALE_RUN")  # Expired run is rejected before evidence claims.
        ops.task_create(self.conn, self.cfg, self.ctx(run2), title="审阅草稿", evidence=[(item, "origin")])
        self.assertEqual(self.count("tasks"), 1)

    def test_a_repeated_identical_create_is_one_task(self):
        self.add_items(mail("m1", "t1", "Please send the form"))
        clock.advance(minutes=61)
        run_id = run_id_of(self.gate())
        args = dict(title="寄表格", fields={"responsibility": "asked"}, evidence=[(self.item_id("m1"), "origin")])
        ops.task_create(self.conn, self.cfg, self.ctx(run_id), **args)
        ops.task_create(self.conn, self.cfg, self.ctx(run_id), **args)
        self.assertEqual(self.count("tasks"), 1)

    def test_a_failed_command_changes_nothing(self):
        self.add_items(mail("m1", "t1", "Please send the form"))
        clock.advance(minutes=61)
        run_id = run_id_of(self.gate())
        with self.assertRaises(ops.OpError):
            ops.task_create(self.conn, self.cfg, self.ctx(run_id), title="寄表格", project="prj_missing",
                            evidence=[(self.item_id("m1"), "origin")])
        self.assertEqual(self.count("tasks"), 0)
        self.assertEqual(self.count("decisions"), 0)
        self.assertEqual(self.conn.execute("SELECT state FROM source_items").fetchone()[0], "batched")


class FollowupTests(Base):
    def setUp(self):
        super().setUp()
        self.task = store.create_task(self.conn, "等 Bob 回复合同", origin="cli", actor="human", status="waiting",
                                      waiting_on="Bob", follow_up_date=clock.local_date(self.cfg.tz).isoformat())

    def test_followup_is_not_handled_before_the_run_records_it(self):
        out = self.gate()
        self.assertIn("followups_due:1", out)
        clock.advance(minutes=10)
        self.assertFalse(wake_of(self.gate()), "claimed by a run in flight")
        clock.advance(minutes=100)       # that run never reported back: the follow-up is offered again
        self.assertIn("followups_due:1", self.gate())
        clock.advance(minutes=100)
        out = self.gate()                # attempts exhausted: reported, not silently dropped
        self.assertFalse(wake_of(out))
        row = self.conn.execute("SELECT state FROM followup_checks WHERE task_id=?", (self.task,)).fetchone()
        self.assertEqual(row["state"], "failed")
        self.assertTrue(any(self.task in p and "follow-up" in p for p in health.report(self.conn, self.cfg)["problems"]))

    def test_a_run_that_ends_without_checking_it_leaves_it_due(self):
        run_id = run_id_of(self.gate())
        self.assertEqual(ops.run_done(self.conn, self.cfg, run_id)["followups_not_checked"], [self.task])
        clock.advance(minutes=5)
        self.assertIn("followups_due:1", self.gate(), "offered again without waiting for a lease")

    def test_checked_or_updated_settles_it(self):
        run_id = run_id_of(self.gate())
        ops.followup_checked(self.conn, self.cfg, self.ctx(run_id), self.task, "Bob 说下周回复")
        ops.run_done(self.conn, self.cfg, run_id)
        clock.advance(minutes=100)
        self.assertFalse(wake_of(self.gate()))

    def test_updating_the_task_settles_the_check(self):
        run_id = run_id_of(self.gate())
        task = store.get_task(self.conn, self.task)
        report = ops.task_update(self.conn, self.cfg, self.ctx(run_id), self.task, based_on=task["version"],
                                 changes={"next_step": "周五再问 Bob"})
        self.assertEqual(report["followup"], "checked")
        ops.run_done(self.conn, self.cfg, run_id)
        clock.advance(minutes=100)
        self.assertFalse(wake_of(self.gate()))

    def test_a_new_follow_up_date_is_a_new_check(self):
        run_id = run_id_of(self.gate())
        ops.followup_checked(self.conn, self.cfg, self.ctx(run_id), self.task, "还在等")
        tomorrow = clock.local_date(self.cfg.tz).toordinal() + 1
        from datetime import date
        store.update_task(self.conn, self.task, {"follow_up_date": date.fromordinal(tomorrow).isoformat()},
                          actor="human", origin="notion")
        clock.advance(days=1)
        self.assertIn("followups_due:1", self.gate())


class DuePassedTests(Base):
    """A task done outside the sources (a meeting held, a form uploaded) is looked at once more when
    its due date has passed, with the stored material searchable for the evidence."""

    def setUp(self):
        super().setUp()
        from datetime import date
        today = clock.local_date(self.cfg.tz).toordinal()
        self.yesterday = date.fromordinal(today - 1).isoformat()
        self.task = store.create_task(self.conn, "提交 camera-ready", origin="cli", actor="human",
                                      due_date=self.yesterday)

    def test_a_passed_due_date_is_checked_once(self):
        out = self.gate()
        self.assertIn("due_passed:1", out)
        self.assertIn(self.task, out)
        self.assertIn("todo-manager source search", out)
        run_id = run_id_of(out)
        report = ops.run_done(self.conn, self.cfg, run_id, checked=[self.task])
        self.assertNotIn("followups_not_checked", report)
        clock.advance(minutes=100)
        self.assertFalse(wake_of(self.gate()))

    def test_closing_it_in_the_run_settles_the_check(self):
        run_id = run_id_of(self.gate())
        task = store.get_task(self.conn, self.task)
        report = ops.task_update(self.conn, self.cfg, self.ctx(run_id), self.task, based_on=task["version"],
                                 changes={"status": "done"})
        self.assertEqual(report["followup"], "checked")
        ops.run_done(self.conn, self.cfg, run_id)
        clock.advance(minutes=100)
        self.assertFalse(wake_of(self.gate()))

    def test_decisions_are_checked_and_a_due_date_today_is_not(self):
        from datetime import date
        store.update_task(self.conn, self.task, {"status": "decision"}, actor="human", origin="cli")
        today = clock.local_date(self.cfg.tz).isoformat()
        store.create_task(self.conn, "今天交", origin="cli", actor="human", due_date=today)
        self.assertIn("due_passed:1", self.gate())

    def test_a_task_with_a_follow_up_and_a_passed_due_date_is_offered_once_per_run(self):
        store.update_task(self.conn, self.task, {"follow_up_date": clock.local_date(self.cfg.tz).isoformat(),
                                                 "status": "waiting", "waiting_on": "Bob"},
                          actor="human", origin="cli")
        out = self.gate()
        self.assertIn("followups_due:1", out)
        self.assertNotIn("due_passed", out, "the follow-up first; the due date at a later run")
        ops.run_done(self.conn, self.cfg, run_id_of(out), checked=[self.task])
        clock.advance(minutes=10)
        self.assertIn("due_passed:1", self.gate())

    def test_the_check_says_an_event_that_has_ended_is_done_unless_cancelled_or_moved(self):
        out = self.gate()
        self.assertIn("An event the user takes part in", out)
        self.assertIn("is done once it has ended", out)
        self.assertIn("cancelled or moved", out)



class ProjectGroupingTests(Base):
    def test_the_run_is_told_how_many_open_tasks_have_no_project(self):
        pid = store.create_project(self.conn, "Example Workshop 2026", origin="cli", actor="human")
        store.create_task(self.conn, "上传海报", origin="intake", project_id=pid)
        store.create_task(self.conn, "录制五分钟报告", origin="intake")
        store.create_task(self.conn, "设置海报 topic", origin="intake", status="decision")
        store.create_task(self.conn, "已完成的事", origin="intake", status="done")
        self.add_items(mail("m1", "t1", "Please send the poster thumbnail"))
        clock.advance(minutes=61)
        out = self.gate()
        self.assertIn("Open tasks without a project: 2", out)
        self.assertIn("--set project_id=", out)


class AmbiguityTests(Base):
    """An unclear item that could cost the user becomes a decision with one question. How many
    questions a run may ask is the user's convention (AGENTS.md), not a check in code."""

    def test_a_question_becomes_a_decision_for_the_user(self):
        self.add_items(mail("m0", "t0", "Are you able to chair session 0?"))
        clock.advance(minutes=61)
        run_id = run_id_of(self.gate())
        tid = ops.task_create(self.conn, self.cfg, self.ctx(run_id), title="是否主持第 0 场？",
                              fields={"status": "decision", "next_step": "要答应主持第 0 场吗？",
                                      "responsibility": "对方直接询问我"},
                              evidence=[(self.item_id("m0"), "origin")])["task"]
        task = store.get_task(self.conn, tid)
        self.assertEqual((task["status"], task["next_actor"], task["next_step"]),
                         ("decision", "me", "要答应主持第 0 场吗？"))

    def test_any_number_of_questions_is_accepted(self):
        self.add_items(*[mail(f"m{i}", f"t{i}", f"Are you able to chair session {i}?") for i in range(4)])
        clock.advance(minutes=61)
        run_id = run_id_of(self.gate())
        for i in range(4):
            ops.task_create(self.conn, self.cfg, self.ctx(run_id), title=f"主持第 {i} 场？",
                            fields={"status": "decision", "next_step": f"要答应主持第 {i} 场吗？"},
                            evidence=[(self.item_id(f"m{i}"), "origin")])
        self.assertEqual(self.count("tasks"), 4)


class RecheckTests(Base):
    """A follow-up check that found nothing new left the past date on the task, and it never came
    due again (2026-10-30: 21 of 34 waiting tasks a week or more past their date). Settling a
    check now moves a passed date to the next look, as far out as the matter has been quiet."""

    def setUp(self):
        super().setUp()
        self.task = store.create_task(self.conn, "等 Bob 回复合同", origin="intake", actor="agent", status="waiting",
                                      waiting_on="Bob", follow_up_date=self.day(0))

    def day(self, offset):
        from datetime import timedelta
        return (clock.local_date(self.cfg.tz) + timedelta(days=offset)).isoformat()

    def fu(self):
        return store.get_task(self.conn, self.task)["follow_up_date"]

    def check(self, **kw):
        run_id = run_id_of(self.gate())
        ops.run_done(self.conn, self.cfg, run_id, checked=[self.task], **kw)
        return run_id

    def test_a_checked_follow_up_moves_to_the_next_look(self):
        self.check()
        self.assertEqual(self.fu(), self.day(7))
        last = self.conn.execute("SELECT actor FROM history WHERE entity_id=? ORDER BY version DESC LIMIT 1",
                                 (self.task,)).fetchone()
        self.assertEqual(last["actor"], "agent")
        clock.advance(days=7)
        self.assertIn("followups_due:1", self.gate(), "looked at again on the new date")

    def test_the_next_look_is_as_far_as_the_matter_has_been_quiet(self):
        store.update_task(self.conn, self.task, {"follow_up_date": self.day(20)}, actor="human", origin="cli")
        clock.advance(days=20)
        self.check()
        self.assertEqual(self.fu(), self.day(20))
        clock.advance(days=20)
        self.check()
        self.assertEqual(self.fu(), self.day(28), "at most recheck_max_days")

    def test_new_material_restarts_the_short_wait(self):
        store.update_task(self.conn, self.task, {"follow_up_date": self.day(20)}, actor="human", origin="cli")
        clock.advance(days=20)
        self.add_items(mail("m1", "t1", "Bob: still with legal, sorry"))
        clock.advance(minutes=61)
        run_id = run_id_of(self.gate())
        ops.task_update(self.conn, self.cfg, self.ctx(run_id), self.task, changes={},
                        evidence=[(self.item_id("m1"), "follow_up")])
        ops.run_done(self.conn, self.cfg, run_id)
        self.assertEqual(self.fu(), self.day(7))

    def test_a_date_the_run_sets_is_kept(self):
        run_id = run_id_of(self.gate())
        task = store.get_task(self.conn, self.task)
        ops.task_update(self.conn, self.cfg, self.ctx(run_id), self.task, based_on=task["version"],
                        changes={"follow_up_date": self.day(3)})
        ops.run_done(self.conn, self.cfg, run_id)
        self.assertEqual(self.fu(), self.day(3))

    def test_a_closed_task_gets_no_new_date(self):
        run_id = run_id_of(self.gate())
        task = store.get_task(self.conn, self.task)
        ops.task_update(self.conn, self.cfg, self.ctx(run_id), self.task, based_on=task["version"],
                        changes={"status": "done"})
        ops.run_done(self.conn, self.cfg, run_id)
        self.assertEqual(self.fu(), self.day(0))

    def test_a_passed_due_date_gets_a_follow_up_date_and_keeps_its_due_date(self):
        store.update_task(self.conn, self.task, {"status": "todo", "waiting_on": None, "follow_up_date": None,
                                                 "due_date": self.day(-1)}, actor="human", origin="cli")
        self.check()
        task = store.get_task(self.conn, self.task)
        self.assertEqual((task["due_date"], task["follow_up_date"]), (self.day(-1), self.day(7)))

    def test_the_gate_gives_a_next_look_to_a_check_settled_without_one(self):
        """Checks settled before the next look existed: the gate sets it, once."""
        today = self.day(0)
        self.conn.execute("INSERT INTO followup_checks(task_id, due, state, run_id, attempts, claimed_at, updated_at)"
                          " VALUES (?, ?, 'done', 'run_old', 1, ?, ?)", (self.task, today, clock.iso(), clock.iso()))
        clock.advance(days=3)
        out = self.gate()
        self.assertFalse(wake_of(out))
        self.assertEqual(self.fu(), self.day(7))
        last = self.conn.execute("SELECT actor, origin FROM history WHERE entity_id=? ORDER BY version DESC LIMIT 1",
                                 (self.task,)).fetchone()
        self.assertEqual((last["actor"], last["origin"]), ("program", "gate"))
        version = store.get_task(self.conn, self.task)["version"]
        clock.advance(minutes=10)
        self.gate()
        self.assertEqual(store.get_task(self.conn, self.task)["version"], version)

    def test_the_gate_leaves_unsettled_and_closed_tasks_alone(self):
        clock.advance(days=3)
        self.assertIn("followups_due:1", self.gate(), "not checked yet: offered, not moved")
        self.assertEqual(self.fu(), self.day(-3))
        closed = store.create_task(self.conn, "已完成", origin="intake", actor="agent", status="done",
                                   follow_up_date=self.day(-5))
        self.conn.execute("INSERT INTO followup_checks(task_id, due, state, run_id, attempts, claimed_at, updated_at)"
                          " VALUES (?, ?, 'done', 'run_old', 1, ?, ?)", (closed, self.day(-5), clock.iso(), clock.iso()))
        self.assertNotIn(closed, gate_intake.without_next_look(self.conn, self.cfg))
