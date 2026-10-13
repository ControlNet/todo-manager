"""The real-time sync daemon (docs/realtime-sync.md): webhook events, local changes and board
changes turned into the gate's own sync steps, the hand-over with the feedback gate, liveness.

Notion is ``FakeNotion`` and the board ``FakeKanban`` (test doubles); webhook events are the
ones ``FakeNotion`` records for its changes. No process, systemd or port is used here.
"""

import json
from pathlib import Path

from todo_manager import clock, daemon, db, delegation, feedback, gate_feedback, health, store, webhook
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

    def test_at_most_one_wake_a_minute(self):
        feedback.record(self.conn, kind="direct", origin="cli", source_id="c1", text="把截止日期改到周五",
                        author_is_user=True, scope_type="task", scope_id=self.task)
        self.assertTrue(self.d.maybe_wake())
        clock.advance(seconds=30)
        self.assertFalse(self.d.maybe_wake())
        clock.advance(seconds=31)
        self.assertTrue(self.d.maybe_wake())
        self.assertEqual(len(self.wakes), 2)


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
        self.assertEqual(daemon.health_of(self.cfg)["reason"], "running other code (restarting)")

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

    def test_the_unit_runs_the_launcher_and_restarts(self):
        text = daemon.unit_text(Path("/home/u/.local/bin/todo-manager"), Path("/home/u/ws"))
        self.assertIn("ExecStart=/home/u/.local/bin/todo-manager daemon run", text)
        self.assertIn("Restart=always", text)
        self.assertIn("Type=notify", text)
        self.assertTrue(text.startswith(daemon.UNIT_MARK))


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


del GateCase   # imported only as a base

if __name__ == "__main__":
    import unittest
    unittest.main()
