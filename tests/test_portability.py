"""R16: a workspace moves to a fresh directory from its export (config without secrets, rules,
history, run context, delegations and delivery files with relative paths and hashes); after an
import or a backup restore nothing is replayed and no worker is started until the user releases
the hold. Kanban is ``FakeKanban`` (test double)."""

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from todo_manager import clock, config, db, delegation, effects, export, gate_feedback, health, rules, store
from todo_manager.kanban import FakeKanban
from todo_manager.notify import MatrixNotifier
from tests.helpers import WorkspaceCase
from tests.test_feedback import FakeRunner


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PortabilityBase(WorkspaceCase):
    overrides = {"notify": {"matrix": True, "matrix_target": "matrix:test-room"}}

    def setUp(self):
        super().setUp()
        self.kb = FakeKanban()
        self.scratch = Path(tempfile.mkdtemp(prefix="tm-worker-"))   # stands in for a worker's scratch dir
        self.target = Path(tempfile.mkdtemp(prefix="tm-target-")) / "home"
        (self.tmp / "config.toml").write_text(
            'language = "zh-CN"\n[notion]\ntoken_env = "NOTION_API_KEY"\napi_key = "secret-value-123"\n'
            '[slack]\ntoken_file = "~/.config/slack_token"\n', encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.scratch, ignore_errors=True)
        shutil.rmtree(self.target.parent, ignore_errors=True)
        super().tearDown()

    def tick(self, conn=None, cfg=None):
        delegation.send_messages(conn or self.conn, cfg or self.cfg, self.kb)
        delegation.submit_pending(conn or self.conn, cfg or self.cfg, self.kb)
        delegation.sync_cards(conn or self.conn, cfg or self.cfg, self.kb)

    def delivered(self):
        """A task with history, a rule, and a delegation whose result has a file."""
        self.task = store.create_task(self.conn, "回复 Alice", origin="cli", actor="human")
        store.update_task(self.conn, self.task, {"next_step": "看草稿"}, actor="human", origin="cli")
        rules.add_rule(self.conn, title="周报", body="周五前发周报")
        self.did = delegation.create(self.conn, self.cfg, task_id=self.task, title="起草", goal="Draft only")
        self.tick()
        card = delegation.get(self.conn, self.did)["card_id"]
        self.kb.worker_start(card)
        draft = self.scratch / "draft.md"
        draft.write_text("Dear Alice, ...", encoding="utf-8")
        self.kb.worker_review(card, "草稿完成", {"deliverable_markdown": "Dear Alice", "artifacts": [str(draft)]})
        self.tick()
        return draft


class DeliveryFileTests(PortabilityBase):
    def test_delivery_files_are_kept_in_the_workspace_with_hashes(self):
        draft = self.delivered()
        original = sha(draft)
        [dlv] = delegation.deliveries(self.conn, self.did)
        [art] = dlv["artifacts"]
        self.assertFalse(Path(art["path"]).is_absolute())
        self.assertEqual(art["sha256"], original)
        self.assertEqual(art["source"], str(draft))
        shutil.rmtree(self.scratch)              # the card ended: its scratch space is gone
        self.assertEqual(sha(self.tmp / art["path"]), original)

    def test_a_missing_file_is_recorded_not_dropped(self):
        self.task = store.create_task(self.conn, "x", origin="cli")
        did = delegation.create(self.conn, self.cfg, task_id=self.task, title="t", goal="g")
        self.tick()
        card = delegation.get(self.conn, did)["card_id"]
        self.kb.worker_start(card)
        self.kb.worker_review(card, "done", {"artifacts": [str(self.scratch / "gone.pdf")]})
        self.tick()
        [dlv] = delegation.deliveries(self.conn, did)
        self.assertEqual(dlv["artifacts"], [{"source": str(self.scratch / "gone.pdf"), "missing": True}])


class ExportImportTests(PortabilityBase):
    def export_bundle(self):
        (self.tmp / "runs" / "run_x").mkdir(parents=True)
        (self.tmp / "runs" / "run_x" / "decisions.json").write_text('{"items": []}', encoding="utf-8")
        (self.tmp / "runs" / "run_x" / "manifest.json").write_text('{"run_id": "run_x"}', encoding="utf-8")
        return export.export_snapshot(self.conn, self.cfg)

    def test_round_trip_into_a_fresh_directory(self):
        self.delivered()
        bundle = self.export_bundle()
        exported = (bundle / "workspace" / "config.toml").read_text(encoding="utf-8")
        self.assertNotIn("secret-value-123", exported, "secrets never leave the workspace")
        self.assertIn('token_env = "NOTION_API_KEY"', exported)
        shutil.rmtree(self.scratch)
        report = export.import_bundle(bundle, self.target)
        self.assertTrue(report["ok"], report)
        cfg2 = config.load(self.target)
        conn2 = db.connect(cfg2.db_path)
        try:
            self.assertEqual(store.get_task(conn2, self.task)["next_step"], "看草稿")
            self.assertEqual(len(store.history(conn2, "task", self.task)), len(store.history(self.conn, "task", self.task)))
            self.assertEqual([r["title"] for r in rules.list_rules(conn2, ("active",))], ["周报"])
            [dlv] = delegation.deliveries(conn2, self.did)
            [art] = dlv["artifacts"]
            self.assertEqual(sha(self.target / art["path"]), art["sha256"])
        finally:
            conn2.close()
        self.assertTrue((self.target / "runs" / "run_x" / "decisions.json").exists(), "run context moves along")
        self.assertTrue((self.target / "runs" / "run_x" / "manifest.json").exists())

    def test_a_tampered_or_incomplete_bundle_is_refused(self):
        self.delivered()
        bundle = self.export_bundle()
        [art_file] = (bundle / "workspace" / "deliveries").rglob("draft.md")
        art_file.write_text("changed", encoding="utf-8")
        with self.assertRaises(export.ImportError_) as ctx:
            export.import_bundle(bundle, self.target)
        self.assertIn("draft.md", str(ctx.exception))
        self.assertFalse((self.target / "state" / "todo.sqlite").exists(), "nothing half-imported")

    def test_an_existing_workspace_is_never_overwritten(self):
        bundle = self.export_bundle()
        with self.assertRaises(export.ImportError_):
            export.import_bundle(bundle, self.tmp)


class NoReplayTests(PortabilityBase):
    def in_flight_state(self):
        """Work that was in flight when the export/backup was taken."""
        self.task = store.create_task(self.conn, "回复 Alice", origin="cli", actor="human")
        self.waiting = delegation.create(self.conn, self.cfg, task_id=self.task, title="起草", goal="Draft only")
        effects.notify(self.conn, self.cfg, "dlg:x", "草稿完成，请查看")       # a reminder not yet sent
        effects.task_push(self.conn, self.task)                               # an idempotent Notion push

    def held(self, conn):
        """Statuses per effect kind (a later push supersedes an earlier one of the same task)."""
        found: dict[str, set[str]] = {}
        for r in conn.execute("SELECT target, op, status FROM outbox"):
            found.setdefault(f"{r['target']}/{r['op']}", set()).add(r["status"])
        return found

    def check_hold(self, conn, cfg):
        self.tick(conn, cfg)
        self.assertEqual(self.kb.cards, {}, "no worker is started after a restore")
        self.assertEqual(delegation.get(conn, self.waiting)["status"], "pending_submit")
        outbox = self.held(conn)
        self.assertEqual(outbox["matrix/notify"], {"dead"}, "a reminder is not replayed")
        self.assertIn("pending", outbox["notion/push_task"], "state pushes converge and stay")
        self.assertNotIn("dead", outbox["notion/push_task"])
        problems = health.report(conn, cfg)["problems"]
        self.assertTrue(any("held" in p for p in problems), problems)
        delegation.release_hold(conn)
        self.tick(conn, cfg)
        self.assertEqual(len(self.kb.cards), 1, "released by the user: work continues")

    def test_import_holds_workers_and_non_idempotent_effects(self):
        self.in_flight_state()
        bundle = export.export_snapshot(self.conn, self.cfg)
        export.import_bundle(bundle, self.target)
        cfg2 = config.load(self.target, overrides={"notify": {"matrix": True, "matrix_target": "matrix:test-room"}})
        conn2 = db.connect(cfg2.db_path)
        try:
            self.check_hold(conn2, cfg2)
        finally:
            conn2.close()

    def test_backup_restore_holds_workers_and_non_idempotent_effects(self):
        self.in_flight_state()
        path = export.backup(self.conn, self.cfg)
        export.restore(self.conn, self.cfg, path)
        self.check_hold(self.conn, self.cfg)

    def test_the_feedback_gate_does_not_send_held_reminders(self):
        self.in_flight_state()
        export.restore(self.conn, self.cfg, export.backup(self.conn, self.cfg))
        runner = FakeRunner()
        gate_feedback.run_gate(self.conn, self.cfg, notion=None, kanban=self.kb,
                               notifier=MatrixNotifier(self.conn, self.cfg, runner))
        sent = [text for _, text in runner.calls]
        self.assertFalse(any("草稿完成" in t for t in sent), "the held reminder is not replayed")
        self.assertTrue(any("todo-manager health" in t for t in sent), "the user is told something needs a look")
