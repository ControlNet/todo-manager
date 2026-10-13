"""The real-time sync daemon (docs/realtime-sync.md): webhook events, local changes and board
changes turned into the gate's own sync steps, the hand-over with the feedback gate, liveness.

Notion is ``FakeNotion`` and the board ``FakeKanban`` (test doubles); webhook events are the
ones ``FakeNotion`` records for its changes. No process, systemd or port is used here.
"""

import json
from pathlib import Path

from todo_manager import clock, daemon, db, delegation, feedback, gate_feedback, health, store, webhook
from todo_manager.locks import file_lock, lock_path
from todo_manager.net import HttpError
from todo_manager.notion.sync import NotionSync, load_state
from tests.fake_notion import USER_ID
from tests.test_feedback import GateCase


class DaemonCase(GateCase):
    def setUp(self):
        super().setUp()
        self.cfg.data["daemon"] = {"enabled": True}
        webhook.save_token(self.cfg.home, "tok")
        self.wakes = []
        self.board = self.tmp / "board.db"
        self.board.write_text("")
        self.d = daemon.Daemon(self.cfg, notion_factory=lambda conn, cfg: NotionSync(conn, cfg, self.client,
                                                                                     load_state(conn, cfg)),
                               kanban_factory=lambda cfg: self.kb, waker=self.wake, board_db=self.board)
        self.addCleanup(self.d.close)
        self.d.cycle()                 # start-up: every timer is due once
        self.settle()

    def wake(self, cfg):
        self.wakes.append(clock.iso())
        return True

    def settle(self):
        """Let start-up work finish and forget what it did."""
        clock.advance(seconds=5)
        self.d.cycle()
        self.fake.events.clear()
        self.fake.calls.clear()
        self.wakes.clear()

    def deliver(self):
        """Hand the events Notion recorded to the daemon, as the receiver would."""
        for event in self.fake.events:
            parsed = webhook.parse(event)
            if parsed:
                self.d.events.put(parsed)
        self.fake.events.clear()

    def loop(self, seconds=3):
        """One loop: the daemon notices (events, local and board changes), then the debounce passes."""
        self.d.cycle()
        clock.advance(seconds=seconds)
        return self.d.cycle()

    def comment_reads(self):
        return [path for method, path in self.fake.calls if method == "GET" and path.startswith("/comments")]


class EventTests(DaemonCase):
    def test_a_user_comment_is_read_from_its_page_only_and_wakes_the_feedback_job(self):
        other = store.create_task(self.conn, "Other", origin="intake")
        self.push(other)
        self.settle()
        self.fake.user_comment(self.page, "Bob 说推迟到下周一")
        self.deliver()
        self.assertIsNone(self.d.cycle(), "debounced: not read at once")
        clock.advance(seconds=3)
        self.d.cycle()
        self.assertEqual(len(self.comment_reads()), 1, "only the page the event named")
        self.assertEqual([f["kind"] for f in feedback.pending(self.conn)], ["comment"])
        self.assertTrue(self.d.maybe_wake())
        self.assertEqual(len(self.wakes), 1)

    def test_a_burst_of_comments_on_one_page_is_one_read(self):
        for text in ("一", "二", "三"):
            self.fake.user_comment(self.page, text)
        self.deliver()
        self.loop()
        self.assertEqual(len(self.comment_reads()), 1)
        self.assertEqual(len(feedback.pending(self.conn)), 3)

    def test_our_own_writes_are_not_work(self):
        store.update_task(self.conn, self.task, {"next_step": "x"}, actor="program", origin="intake",
                          based_on_version=store.get_task(self.conn, self.task)["version"])
        self.loop(1)                      # pushes the change: Notion records a bot edit
        self.fake.calls.clear()
        self.deliver()
        self.assertIsNone(self.loop(5))
        self.assertEqual(self.fake.calls, [])

    def test_a_user_property_edit_is_pulled_after_its_event(self):
        self.fake.user_edit(self.page, **{"截止日期": "2026-10-20"})
        self.deliver()
        self.loop()
        self.assertEqual(store.get_task(self.conn, self.task)["due_date"], "2026-10-20")

    def test_a_page_the_user_created_in_tasks_is_pulled(self):
        self.fake.user_create_page(self.tasks_ds, "我自己建的任务")
        self.deliver()
        self.loop()
        self.assertEqual([f["kind"] for f in feedback.pending(self.conn)], ["new_task"])

    def test_events_about_pages_we_do_not_know_are_ignored(self):
        stranger = self.fake.user_top_level_page("Somebody's notes")
        self.fake.events.clear()          # a page created anywhere is looked for in our data sources
        self.fake.user_set_markdown(stranger, "x")
        self.fake.user_comment(stranger, "x")
        self.deliver()
        self.assertIsNone(self.loop(5))


    def test_a_full_pass_due_with_a_comment_event_still_reads_the_event_page(self):
        self.fake.user_comment(self.page, "改成周五")
        self.deliver()
        self.d.cycle()                                   # the event's read is debounced
        self.d.due(("notion_pass",))                     # the reconcile pass falls due at the same time
        clock.advance(seconds=3)
        self.d.cycle()
        self.assertEqual([f["kind"] for f in feedback.pending(self.conn)], ["comment"],
                         "the page is not in the pass's selection, yet the event is not dropped")


    def test_a_page_deleted_in_notion_is_read_directly(self):
        self.fake.user_trash(self.page)   # a data source query no longer returns it
        self.deliver()
        self.loop()
        row = self.conn.execute("SELECT trashed FROM notion_pages WHERE page_id=?", (self.page,)).fetchone()
        self.assertEqual(row["trashed"], 1)

    def test_a_failed_event_read_is_retried_without_another_event(self):
        self.fake.fail_next[("GET", "/comments")] = HttpError(400, "boom", body={"code": "validation_error"})
        self.fake.user_comment(self.page, "周五前给我")
        self.deliver()
        self.loop()
        self.assertEqual(feedback.pending(self.conn), [])
        clock.advance(seconds=daemon.RETRY_SECONDS)
        self.d.cycle()
        self.assertEqual([f["kind"] for f in feedback.pending(self.conn)], ["comment"])

    def test_a_deletion_read_that_keeps_failing_is_tried_again_slowly(self):
        self.fake.user_trash(self.page)
        self.deliver()
        self.d.cycle()
        for _ in range(daemon.MAX_RETRIES + 1):
            self.fake.fail_next[("GET", "/pages/")] = HttpError(400, "boom", body={"code": "validation_error"})
            clock.advance(seconds=daemon.RETRY_SECONDS)
            self.d.cycle()
        self.assertIn(("page", self.page), self.d.pending, "not forgotten: a full pass cannot see a trashed page")
        clock.advance(minutes=30)
        self.d.cycle()
        row = self.conn.execute("SELECT trashed FROM notion_pages WHERE page_id=?", (self.page,)).fetchone()
        self.assertEqual(row["trashed"], 1)

    def test_a_page_that_is_gone_is_reported_until_it_reads_again_and_not_retried(self):
        self.fake.user_trash(self.page)
        self.deliver()
        self.fake.fail_next[("GET", "/pages/")] = HttpError(404, "gone", body={"code": "object_not_found"})
        self.loop()
        self.assertNotIn(("page", self.page), self.d.pending)
        self.d.due(("home",))                         # a home refresh does not look at the page
        self.d.cycle()
        self.d.heartbeat(force=True)
        self.assertIn(self.page, daemon.read_status(self.cfg.home)["page_problems"])
        self.assertIn(self.page, daemon.health_of(self.cfg)["page_problems"])
        self.fake.user_trash(self.page, trashed=False)   # shared again / restored: it reads
        self.deliver()
        self.loop()
        self.d.heartbeat(force=True)
        self.assertNotIn(self.page, daemon.read_status(self.cfg.home)["page_problems"])

    def test_a_gone_page_stays_reported_across_a_restart(self):
        self.d.page_problems[self.page] = "no longer accessible"
        self.d.heartbeat(force=True)
        restarted = daemon.Daemon(self.cfg, notion_factory=lambda conn, cfg: None, kanban_factory=lambda cfg: None,
                                  waker=self.wake, board_db=None)
        restarted.heartbeat(force=True)
        self.assertIn(self.page, daemon.read_status(self.cfg.home)["page_problems"])

    def test_every_gone_page_is_kept(self):
        pages = [f"page-{i}" for i in range(80)]
        self.d.read_pages(type("N", (), {"refresh_page": staticmethod(lambda p: "missing")})(), pages)
        self.assertEqual(sorted(self.d.page_problems), sorted(pages))

    def test_a_pull_that_imports_the_page_clears_its_problem(self):
        self.d.page_problems[self.page] = "no longer accessible"
        self.fake.user_edit(self.page, **{"截止日期": "2026-10-21"})
        self.deliver()
        self.loop()
        self.assertEqual(store.get_task(self.conn, self.task)["due_date"], "2026-10-21")
        self.assertNotIn(self.page, self.d.page_problems)

    def test_only_the_failed_event_read_is_retried(self):
        other = store.create_task(self.conn, "Other", origin="intake")
        self.push(other)
        self.settle()
        other_page = self.page_of(other)
        self.fake.user_trash(self.page)
        self.fake.user_comment(other_page, "x")
        self.deliver()
        self.fake.fail_next[("GET", "/comments")] = HttpError(400, "boom", body={"code": "validation_error"})
        self.loop()
        self.assertIn(("comments", other_page), self.d.pending)
        self.assertNotIn(("page", self.page), self.d.pending, "its read succeeded")

    def test_timer_work_is_not_retried_its_timer_runs_it_again(self):
        self.fake.fail_next[("POST", "/data_sources/")] = HttpError(400, "boom", body={"code": "validation_error"})
        self.d.due(("notion_pass",))
        self.d.cycle()
        self.assertNotIn(("notion_pass",), self.d.pending)

    def test_a_deleted_project_page_is_left_to_the_pull_as_before(self):
        project = store.create_project(self.conn, "合作项目", origin="cli")
        self.loop(1)
        page = self.conn.execute("SELECT page_id FROM notion_pages WHERE entity_type='project' AND entity_id=?",
                                 (project,)).fetchone()["page_id"]
        self.fake.user_trash(page)
        self.deliver()
        self.d.cycle()
        self.assertNotIn(("page", page), self.d.pending)

    def test_an_event_taken_while_the_schema_is_wrong_is_kept(self):
        self.fake.user_trash(self.page)
        self.deliver()
        first = self.d.events.get_nowait()             # as the main loop takes it
        other = db.open_existing(self.cfg.db_path)
        self.addCleanup(other.close)
        other.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(db.SCHEMA_VERSION + 1),))
        self.assertIsNone(self.d.cycle(first))
        other.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(db.SCHEMA_VERSION),))
        self.loop()
        row = self.conn.execute("SELECT trashed FROM notion_pages WHERE page_id=?", (self.page,)).fetchone()
        self.assertEqual(row["trashed"], 1)


class MaintenanceTests(DaemonCase):
    def test_while_install_or_restore_holds_the_workspace_the_daemon_touches_no_database(self):
        self.d.lock_timeout = 0.1
        self.fake.user_comment(self.page, "x")
        self.deliver()
        queries = []
        self.d.conn.set_trace_callback(queries.append)
        with file_lock(lock_path(self.cfg.home, "workspace")):          # exclusive, as install takes it
            self.assertIsNone(self.d.cycle())
            clock.advance(seconds=5)
            self.assertIsNone(self.d.cycle())
            self.assertFalse(self.d.maybe_wake())
        self.d.conn.set_trace_callback(None)
        self.assertEqual(queries, [])
        self.loop()
        self.assertEqual([f["kind"] for f in feedback.pending(self.conn)], ["comment"], "the event was kept")

    def test_the_database_is_not_even_opened_while_install_holds_the_workspace(self):
        self.d.close()
        self.d.lock_timeout = 0.1
        with file_lock(lock_path(self.cfg.home, "workspace")):
            self.assertIsNone(self.d.cycle())
            self.assertIsNone(self.d.conn)
        self.d.cycle()
        self.assertIsNotNone(self.d.conn)

    def test_a_schema_changed_under_the_daemon_makes_it_wait_for_install(self):
        other = db.open_existing(self.cfg.db_path)
        self.addCleanup(other.close)
        other.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(db.SCHEMA_VERSION + 1),))
        self.assertIsNone(self.d.cycle())
        self.assertFalse(self.d.maybe_wake())
        self.assertEqual(self.d.state, "waiting_install")
        other.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(db.SCHEMA_VERSION),))
        self.d.cycle()
        self.assertEqual(self.d.state, "running")


class LocalChangeTests(DaemonCase):
    def test_a_local_write_reaches_notion_without_waiting_for_the_gate(self):
        store.update_task(self.conn, self.task, {"due_date": "2026-10-30"}, actor="agent", origin="feedback",
                          based_on_version=store.get_task(self.conn, self.task)["version"])
        self.loop(1)
        self.assertEqual(self.fake.prop(self.page, "截止日期"), "2026-10-30")

    def test_nothing_changed_nothing_done(self):
        clock.advance(seconds=20)
        self.assertIsNone(self.d.cycle())
        self.assertEqual(self.fake.calls, [])

    def test_a_new_delegation_gets_its_card_at_once(self):
        delegation.create(self.conn, self.cfg, task_id=self.task, title="起草", goal="draft")
        self.loop(1)
        self.assertEqual(len(self.kb.cards), 1)

    def test_a_commit_by_another_process_during_the_work_is_not_swallowed(self):
        other = db.open_existing(self.cfg.db_path)
        self.addCleanup(other.close)
        work = self.d.work

        def work_then_someone_else_writes(keys):
            out = work(keys)
            store.update_task(other, self.task, {"next_step": "from the CLI"}, actor="agent", origin="cli",
                              based_on_version=store.get_task(other, self.task)["version"])
            return out
        self.d.work = work_then_someone_else_writes
        self.d.due(("claims",))
        self.d.cycle()
        self.d.work = work
        self.loop(1)
        self.assertIn("from the CLI", self.fake.prop(self.page, "下一步") or "",
                      "the next loop sees the commit and pushes it")


class BoardTests(DaemonCase):
    def touch_board(self):
        stamp = self.board.stat().st_mtime + 1
        import os
        os.utime(self.board, (stamp, stamp))

    def test_a_board_change_reads_the_cards_at_most_every_interval(self):
        reads = []
        original = gate_feedback.kanban_read_steps

        def counting(step, conn, cfg, kanban):
            reads.append(clock.iso())
            return original(step, conn, cfg, kanban)
        gate_feedback.kanban_read_steps = counting
        self.addCleanup(setattr, gate_feedback, "kanban_read_steps", original)
        clock.advance(seconds=15)        # the start-up read is past the interval
        self.touch_board()
        self.d.cycle()
        self.assertEqual(len(reads), 1)
        self.touch_board()
        clock.advance(seconds=5)
        self.d.cycle()
        self.assertEqual(len(reads), 1, "within kanban_min_interval_seconds")
        clock.advance(seconds=10)
        self.d.cycle()
        self.assertEqual(len(reads), 2)


class WakeTests(DaemonCase):
    def test_no_feedback_no_wake(self):
        self.assertFalse(self.d.maybe_wake())
        self.assertEqual(self.wakes, [])

    def record(self, source_id):
        feedback.record(self.conn, kind="direct", origin="cli", source_id=source_id, text="把截止日期改到周五",
                        author_is_user=True, scope_type="task", scope_id=self.task)

    def test_at_most_one_wake_a_minute(self):
        self.record("c1")
        self.assertTrue(self.d.maybe_wake())
        clock.advance(seconds=30)
        self.record("c2")
        self.assertFalse(self.d.maybe_wake())
        clock.advance(seconds=31)
        self.assertTrue(self.d.maybe_wake(), "new feedback since the last wake")
        self.assertEqual(len(self.wakes), 2)

    def test_feedback_that_stays_unclaimed_is_not_woken_for_every_minute(self):
        self.record("c1")
        self.assertTrue(self.d.maybe_wake())
        clock.advance(minutes=2)
        self.assertFalse(self.d.maybe_wake(), "the gate did not claim it: wait for the job's own schedule")
        clock.advance(minutes=3)
        self.assertTrue(self.d.maybe_wake())


class HermesWakeTests(DaemonCase):
    def jobs(self, **feedback_job):
        path = self.tmp / "hermes" / "cron" / "jobs.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"jobs": [{"id": "j1", "name": "todo-manager-feedback", "enabled": True,
                                              "state": "scheduled", **feedback_job}]}))

    def wake_with(self):
        from unittest import mock
        from todo_manager import hermes_install
        with mock.patch.object(hermes_install.Installer, "hermes", autospec=True, return_value="") as run:
            woke = daemon.hermes_wake(self.cfg)
        return woke, [c.args[1:] for c in run.call_args_list]

    def test_the_feedback_job_is_run_by_id(self):
        self.jobs()
        self.assertEqual(self.wake_with(), (True, [("cron", "run", "j1")]))

    def test_a_paused_job_is_not_resumed_by_a_wake(self):
        self.jobs(enabled=False, state="paused", paused_at="2026-10-13T00:00:00Z")
        self.assertEqual(self.wake_with(), (False, []))

    def test_no_job_no_wake(self):
        self.assertEqual(self.wake_with(), (False, []))


class WebhookSilenceTests(DaemonCase):
    def write(self):
        store.update_task(self.conn, self.task, {"next_step": f"step {clock.iso()}"}, actor="agent",
                          origin="feedback", based_on_version=store.get_task(self.conn, self.task)["version"])
        self.loop(1)

    def test_no_event_after_our_write_means_the_webhook_is_broken_and_the_daemon_polls(self):
        self.write()
        self.assertEqual(self.d.mode, "webhook")
        clock.advance(minutes=11)
        self.d.cycle()
        self.assertEqual(self.d.mode, "polling")
        row = self.conn.execute("SELECT last_error FROM cursors WHERE source='daemon' AND scope='webhook'").fetchone()
        self.assertIn("no Notion events", row["last_error"])
        self.d.stats.last_event_at = clock.iso()      # deliveries arrive again
        self.d.cycle()
        self.assertEqual(self.d.mode, "webhook")

    def test_events_after_our_write_keep_webhook_mode(self):
        self.write()
        clock.advance(minutes=1)
        self.d.stats.last_event_at = clock.iso()
        clock.advance(minutes=11)
        self.d.cycle()
        self.assertEqual(self.d.mode, "webhook")

    def test_steady_writes_do_not_postpone_the_silence_check(self):
        self.write()
        for _ in range(3):                # a write every 5 minutes, no delivery at all
            clock.advance(minutes=5)
            self.write()
        self.assertEqual(self.d.mode, "polling", "15 minutes since the first write still unanswered")

    def test_a_new_write_does_not_end_a_detected_silence_only_a_delivery_does(self):
        self.write()
        clock.advance(minutes=11)
        self.d.cycle()
        self.assertEqual(self.d.mode, "polling")
        self.write()
        self.d.cycle()
        self.assertEqual(self.d.mode, "polling")
        self.d.stats.last_event_at = clock.iso()
        self.d.cycle()
        self.assertEqual(self.d.mode, "webhook")

    def test_a_delivery_during_the_work_does_not_answer_a_write_made_after_it(self):
        self.write()                                     # unanswered since t0
        work = self.d.work

        def delivery_then_write(keys):
            self.d.stats.last_event_at = clock.iso()     # an event about an older change arrives
            clock.advance(seconds=1)
            return work(keys)                            # then this cycle writes again
        self.d.work = delivery_then_write
        store.update_task(self.conn, self.task, {"next_step": "again"}, actor="agent", origin="feedback",
                          based_on_version=store.get_task(self.conn, self.task)["version"])
        self.loop(1)
        self.d.work = work
        clock.advance(minutes=11)
        self.d.cycle()
        self.assertEqual(self.d.mode, "polling", "the second write was never answered")

    def test_with_the_receiver_turned_off_a_stored_token_does_not_mean_webhook_mode(self):
        self.cfg.data["daemon"]["webhook"] = False
        self.assertEqual(self.d.mode, "polling")

    def test_with_webhooks_notion_is_still_read_as_often_as_the_gate_did(self):
        self.assertEqual(self.d.mode, "webhook")
        clock.advance(minutes=5)
        self.d.cycle()
        self.assertTrue([c for c in self.fake.calls if c[1].startswith("/data_sources/")],
                        "a full pass every 5 minutes: what the events miss waits no longer than before")

    def test_without_a_verified_subscription_the_daemon_polls_notion_like_the_gate(self):
        webhook.reset_token(self.cfg.home)
        self.assertEqual(self.d.mode, "polling")
        clock.advance(minutes=5)
        self.d.cycle()
        self.assertTrue(self.comment_reads(), "a full Notion pass every notion.active_poll_minutes")


class WaitingForInstallTests(DaemonCase):
    def test_a_newer_schema_expected_than_installed_waits_and_does_nothing(self):
        self.d.close()
        with db.tx(self.conn):
            db.set_meta(self.conn, "schema_version", "1")
        self.assertIsNone(self.d.cycle())
        self.assertEqual(self.d.state, "waiting_install")
        self.assertIn("todo-manager install", self.d.last_error)


class HandOverTests(DaemonCase):
    def gate_calls(self):
        self.fake.calls.clear()
        self.sync = self.new_sync()
        out, woke = gate_feedback.run_gate(self.conn, self.cfg, notion=self.sync, kanban=self.kb)
        return out, woke, list(self.fake.calls)

    def test_a_healthy_daemon_leaves_the_gate_only_the_claims(self):
        self.d.heartbeat(force=True)
        self.fake.user_comment(self.page, "看一下")
        self.deliver()
        self.loop()
        out, woke, calls = self.gate_calls()
        self.assertTrue(woke, "the comment the daemon read is claimed")
        self.assertEqual(calls, [], "no Notion request from the gate")

    def test_a_stale_daemon_is_reported_and_the_gate_syncs(self):
        self.d.heartbeat(force=True)
        clock.advance(seconds=daemon.STALE_SECONDS + 1)
        _, _, calls = self.gate_calls()
        self.assertTrue(calls, "the gate syncs itself")
        row = self.conn.execute("SELECT last_error FROM cursors WHERE source='daemon' AND scope='alive'").fetchone()
        self.assertIn("not running", row["last_error"])

    def test_a_daemon_on_other_code_is_not_trusted(self):
        self.d.code = "old"
        self.d.heartbeat(force=True)
        self.assertEqual(daemon.health_of(self.cfg)["reason"], "running other code or settings (restarting)")

    def test_a_stale_installed_unit_is_restarted_at_most_every_ten_minutes(self):
        unit_dir = self.tmp / "units"
        unit_dir.mkdir()
        self.cfg.data["daemon"]["unit_dir"] = str(unit_dir)
        daemon.unit_path(self.cfg).write_text("x")
        calls = []
        runner = lambda cmd, **kw: calls.append(cmd)      # noqa: E731
        for _ in range(2):
            gate_feedback.sync_or_leave(self.conn, self.cfg, notion=None, kanban=None, runner=runner)
        self.assertEqual(calls, [["systemctl", "--user", "restart", daemon.UNIT_NAME]])
        clock.advance(minutes=11)
        gate_feedback.sync_or_leave(self.conn, self.cfg, notion=None, kanban=None, runner=runner)
        self.assertEqual(len(calls), 2)

    def test_a_disabled_daemon_changes_nothing_for_the_gate(self):
        self.cfg.data["daemon"]["enabled"] = False
        self.d.heartbeat(force=True)
        _, _, calls = self.gate_calls()
        self.assertTrue(calls)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM cursors WHERE source='daemon' AND scope='alive'").fetchone())


class StatusTests(DaemonCase):
    def test_status_file_reports_mode_webhook_and_errors(self):
        self.d.heartbeat(force=True)
        st = daemon.read_status(self.cfg.home)
        self.assertEqual((st["state"], st["mode"], st["webhook"]["token"]), ("running", "webhook", True))
        self.assertTrue(daemon.health_of(self.cfg)["healthy"])

    def test_the_fingerprint_follows_source_edits(self):
        root = self.tmp / "pkg"
        root.mkdir()
        (root / "a.py").write_text("x = 1\n")
        before = daemon.code_fingerprint(root)
        (root / "a.py").write_text("x = 22\n")
        self.assertNotEqual(daemon.code_fingerprint(root), before)

    def test_the_fingerprint_follows_config_edits(self):
        before = daemon.code_fingerprint(config_path=self.cfg.home / "config.toml")
        with (self.cfg.home / "config.toml").open("a") as fh:
            fh.write("\n# edited\n")
        self.assertNotEqual(daemon.code_fingerprint(config_path=self.cfg.home / "config.toml"), before)

    def test_after_a_config_edit_the_gate_syncs_until_the_daemon_restarts(self):
        self.d.heartbeat(force=True)
        self.assertTrue(daemon.health_of(self.cfg)["healthy"])
        with (self.cfg.home / "config.toml").open("a") as fh:
            fh.write("\n# edited\n")
        self.assertFalse(daemon.health_of(self.cfg)["healthy"], "the running daemon still has the old settings")
        restarted = daemon.Daemon(self.cfg, notion_factory=lambda conn, cfg: None, kanban_factory=lambda cfg: None,
                                  waker=self.wake, board_db=None)
        self.addCleanup(restarted.close)
        restarted.open()
        restarted.heartbeat(force=True)
        self.assertTrue(daemon.health_of(self.cfg)["healthy"])

    def test_the_unit_runs_the_launcher_and_restarts(self):
        text = daemon.unit_text(Path("/home/u/.local/bin/todo-manager"), Path("/home/u/ws"))
        self.assertIn('ExecStart="/home/u/.local/bin/todo-manager" daemon run', text)
        self.assertIn("Restart=always", text)
        self.assertIn(f"RestartPreventExitStatus={daemon.EXIT_DISABLED}", text,
                      "turned off in config.toml: it exits and stays down until install removes the unit")
        self.assertIn("Type=notify", text)
        self.assertTrue(text.startswith(daemon.UNIT_MARK))

    def test_unit_paths_are_quoted_for_systemd(self):
        text = daemon.unit_text(Path("/home/u/My Tools/todo-manager"), Path("/home/u/50% ws"))
        self.assertIn('ExecStart="/home/u/My Tools/todo-manager" daemon run\n', text)
        self.assertIn('Environment="TODO_HOME=/home/u/50%% ws"\n', text)


class LivenessTests(DaemonCase):
    class Client:
        requests = 0

    class Notion:
        def __init__(self, client):
            self.client = client

    def test_a_long_cycle_that_makes_progress_stays_healthy(self):
        client = self.Client()
        self.d.notion_now = self.Notion(client)
        self.assertTrue(self.d.pulse(mono=1000.0))
        for t in range(1, 11):                       # 10 x 30 s inside one cycle, requests going out
            client.requests += 5
            clock.advance(seconds=30)
            self.assertTrue(self.d.pulse(mono=1000.0 + 30 * t))
        self.assertTrue(daemon.health_of(self.cfg)["healthy"], "the gate leaves the sync to the busy daemon")

    def test_the_status_can_be_read_while_the_main_thread_changes_retries(self):
        import threading
        d, writers = self.d, []

        class Interleaved(dict):
            """Halfway through the pulse thread's read, the main thread records a retry."""
            def items(self):
                for n, item in enumerate(dict.items(self)):
                    yield item
                    if n == 0:
                        writer = threading.Thread(target=d.done, args=([("comments", "new")],),
                                                  kwargs={"failed": {("comments", "new")}})
                        writer.start()
                        writer.join(0.2)          # blocked by the lock (fixed) or done (race)
                        writers.append(writer)
        d.retries = Interleaved({("comments", "a"): 9, ("comments", "b"): 9})
        status = d.status()                        # RuntimeError without the lock
        for writer in writers:
            writer.join()
        self.assertEqual(status["retrying"], ["comments/a", "comments/b"])
        self.assertIn(("comments", "new"), d.retries)

    def test_an_error_in_one_pulse_does_not_end_the_pulses(self):
        calls = []

        def broken(mono=None):
            calls.append(1)
            raise RuntimeError("x")
        self.d.pulse = broken
        self.d.pulse_once()
        self.d.pulse_once()
        self.assertEqual(len(calls), 2)

    def test_a_stuck_cycle_stops_the_heartbeat(self):
        self.d.notion_now = self.Notion(self.Client())
        self.assertTrue(self.d.pulse(mono=1000.0))
        clock.advance(seconds=daemon.STALL_SECONDS + 1)
        self.assertFalse(self.d.pulse(mono=1000.0 + daemon.STALL_SECONDS + 1),
                         "no watchdog ping: systemd kills and restarts it")
        self.assertFalse(daemon.health_of(self.cfg)["healthy"], "the gate syncs itself")


class CodeChangeTests(DaemonCase):
    def test_the_daemon_exits_once_a_source_change_has_settled(self):
        import threading
        versions = iter(["v1"] + ["v2"] * 3 + ["v3"] * 1000)   # a pull touches files over a few checks
        d = daemon.Daemon(self.cfg, notion_factory=lambda conn, cfg: None, kanban_factory=lambda cfg: None,
                          waker=self.wake, board_db=None, fingerprint=lambda: next(versions))
        d.code_check_seconds, d.code_settle_seconds = 0, 0.5
        result = []
        thread = threading.Thread(target=lambda: result.append(d.run_forever()), daemon=True)
        thread.start()
        thread.join(timeout=15)
        self.assertEqual(result, [0])
        self.assertEqual(daemon.read_status(self.cfg.home)["state"], "restarting")

    def test_the_daemon_exits_after_a_config_edit(self):
        import threading
        d = daemon.Daemon(self.cfg, notion_factory=lambda conn, cfg: None, kanban_factory=lambda cfg: None,
                          waker=self.wake, board_db=None)
        d.code_check_seconds, d.code_settle_seconds = 0, 0.5
        result = []
        thread = threading.Thread(target=lambda: result.append(d.run_forever()), daemon=True)
        thread.start()
        with (self.cfg.home / "config.toml").open("a") as fh:
            fh.write("\n# edited\n")
        thread.join(timeout=15)
        self.assertEqual(result, [0], "systemd starts it again with the new settings")


del GateCase   # imported only as a base

if __name__ == "__main__":
    import unittest
    unittest.main()
