"""B14: an export is one consistent snapshot. The database is copied first with SQLite's online
backup (one step, so a gate or an apply committing meanwhile is either wholly in it or wholly
out); Markdown, data.json and the manifest are all made from that copy; the workspace part
carries exactly the run context and delivery files the snapshot refers to. A kept delivery
file is sealed (copied to a temporary name, hashed as copied, then renamed), so its recorded
hash is the hash of the bytes kept. Every export, rebuild, import and restore checks that each
task's and project's history matches its version.

"Concurrent" writes are made deterministically from a second connection at a fixed point of
the export (a hook on the Markdown writer), not by racing threads. Kanban is ``FakeKanban``
(test double).
"""

import json
import re
import shutil
from pathlib import Path
from unittest import mock

from todo_manager import db, export, files, runs, store
from tests.test_portability import PortabilityBase, sha


class ConcurrentWriteBase(PortabilityBase):
    def export_with(self, concurrent):
        """Export while ``concurrent(conn2)`` commits from another connection mid-export."""
        real = export._task_md
        done = []

        def hooked(*args, **kwargs):
            if not done:
                done.append(True)
                conn2 = db.connect(self.cfg.db_path)
                try:
                    concurrent(conn2)
                finally:
                    conn2.close()
            return real(*args, **kwargs)

        with mock.patch.object(export, "_task_md", hooked):
            bundle = export.export_snapshot(self.conn, self.cfg)
        self.assertTrue(done, "the concurrent write ran during the export")
        return bundle

    def data(self, bundle):
        return json.loads((bundle / "data.json").read_text(encoding="utf-8"))

    def md_version(self, bundle, task_id):
        text = (bundle / "tasks" / f"{task_id}.md").read_text(encoding="utf-8")
        return int(re.search(r"版本: (\d+)", text).group(1))


class ConsistentSnapshotTests(ConcurrentWriteBase):
    def test_an_apply_during_export_is_wholly_in_or_wholly_out(self):
        task = store.create_task(self.conn, "回复 Alice", origin="cli", actor="human")
        store.update_task(self.conn, task, {"next_step": "看草稿"}, actor="human", origin="cli")

        def apply(conn2):
            store.update_task(conn2, task, {"next_step": "并发写入"}, actor="agent", origin="feedback")
            store.create_task(conn2, "并发新任务", origin="intake")

        bundle = self.export_with(apply)
        data = self.data(bundle)
        [row] = [t for t in data["tasks"] if t["id"] == task]
        versions = sorted(h["version"] for h in data["history"] if h["entity_id"] == task)
        self.assertEqual(versions, list(range(1, row["version"] + 1)), "history matches the version")
        self.assertEqual(self.md_version(bundle, task), row["version"], "Markdown and data.json agree")
        late_in_data = any(t["title"] == "并发新任务" for t in data["tasks"])
        late_in_md = any("并发新任务" in p.read_text(encoding="utf-8") for p in (bundle / "tasks").glob("*.md"))
        self.assertEqual(late_in_data, late_in_md)
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["integrity"], [])
        self.assertTrue(export.verify_rebuild(bundle / "data.json")["ok"])
        self.assertFalse(list(bundle.parent.glob(".*snapshot*")), "the snapshot copy is removed")

    def test_files_written_during_export_that_the_snapshot_does_not_know_are_left_out(self):
        self.delivered()
        run_id = runs.start(self.conn, "feedback")
        runs.write_json(self.cfg.home, run_id, "manifest.json", {"run_id": run_id})

        def gate(conn2):
            late = runs.start(conn2, "feedback")
            runs.write_json(self.cfg.home, late, "manifest.json", {"run_id": late})
            (self.cfg.home / "deliveries" / "dlv_late").mkdir(parents=True)
            (self.cfg.home / "deliveries" / "dlv_late" / "half.md").write_text("half writ", encoding="utf-8")
            self.late = late

        bundle = self.export_with(gate)
        exported_runs = {p.name for p in (bundle / "workspace" / "runs").iterdir()}
        known = {r["id"] for r in self.data(bundle)["runs"]}
        self.assertIn(run_id, exported_runs)
        self.assertEqual(exported_runs - known, set(), "only runs the snapshot knows")
        self.assertFalse((bundle / "workspace" / "deliveries" / "dlv_late").exists())
        [dlv] = [d for d in self.data(bundle)["deliveries"] if d["artifacts"] != "[]"]
        [art] = json.loads(dlv["artifacts"])
        self.assertEqual(sha(bundle / "workspace" / art["path"]), art["sha256"])
        report = export.import_bundle(bundle, self.target)
        self.assertTrue(report["ok"])
        self.assertEqual(report["integrity"], [])

    def test_a_referenced_file_changed_after_it_was_kept_is_reported(self):
        self.delivered()
        [path] = (self.cfg.home / "deliveries").rglob("draft.md")
        path.write_text("edited by hand", encoding="utf-8")
        bundle = export.export_snapshot(self.conn, self.cfg)
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(any("draft.md" in p for p in manifest["artifact_problems"]), manifest["artifact_problems"])


class SealedArtifactTests(PortabilityBase):
    def test_the_recorded_hash_is_of_the_bytes_kept_while_the_source_changes(self):
        src = self.scratch / "report.md"
        src.write_text("v1", encoding="utf-8")
        real = files.sha256_file

        def worker_still_writing(path):
            digest = real(path)
            src.write_text("v2, the worker kept writing", encoding="utf-8")
            return digest

        with mock.patch.object(files, "sha256_file", worker_still_writing):
            [art] = files.keep_artifacts(self.tmp, "dlv_x", [str(src)])
        self.assertEqual(sha(self.tmp / art["path"]), art["sha256"])
        self.assertEqual(art["size"], (self.tmp / art["path"]).stat().st_size)
        self.assertEqual([p.name for p in (self.tmp / "deliveries" / "dlv_x").iterdir()], ["report.md"])

    def test_a_failed_copy_leaves_no_file_under_the_kept_name(self):
        src = self.scratch / "report.md"
        src.write_text("v1" * 1000, encoding="utf-8")

        def broken_copy(a, b, *args, **kwargs):
            Path(b).write_text("v1v1", encoding="utf-8")
            raise OSError("No space left on device")

        with mock.patch.object(files.shutil, "copyfile", broken_copy):
            with self.assertRaises(OSError):
                files.keep_artifacts(self.tmp, "dlv_x", [str(src)])
        self.assertEqual(list((self.tmp / "deliveries" / "dlv_x").iterdir()), [], "retried by the next sync")


class HistoryIntegrityTests(ConcurrentWriteBase):
    def test_history_is_whole_after_a_restore_and_continues_from_it(self):
        task = store.create_task(self.conn, "回复 Alice", origin="cli", actor="human")
        store.update_task(self.conn, task, {"next_step": "a"}, actor="human", origin="cli")
        path = export.backup(self.conn, self.cfg)
        self.assertEqual(export.check_backup(path)["integrity_problems"], [])
        store.update_task(self.conn, task, {"next_step": "b"}, actor="human", origin="cli")
        result = export.restore(self.conn, self.cfg, path)
        self.assertEqual(result["integrity_problems"], [])
        self.assertEqual(store.get_task(self.conn, task)["version"], 2)
        store.update_task(self.conn, task, {"next_step": "c"}, actor="human", origin="cli")
        self.assertEqual([h["version"] for h in store.history(self.conn, "task", task)], [1, 2, 3])
        self.assertEqual(export.integrity_problems(self.conn), [])

    def test_a_history_gap_is_reported_by_export_rebuild_backup_and_import(self):
        task = store.create_task(self.conn, "回复 Alice", origin="cli", actor="human")
        store.update_task(self.conn, task, {"next_step": "a"}, actor="human", origin="cli")
        store.update_task(self.conn, task, {"next_step": "b"}, actor="human", origin="cli")
        with db.tx(self.conn):
            self.conn.execute("DELETE FROM history WHERE entity_id=? AND version=2", (task,))
        bundle = export.export_snapshot(self.conn, self.cfg)
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(any(task in p for p in manifest["integrity"]), manifest["integrity"])
        rebuild = export.verify_rebuild(bundle / "data.json")
        self.assertFalse(rebuild["ok"])
        self.assertTrue(rebuild["integrity_problems"])
        self.assertTrue(export.check_backup(export.backup(self.conn, self.cfg))["integrity_problems"])
        with self.assertRaises(export.ImportError_) as ctx:
            export.import_bundle(bundle, self.target)
        self.assertIn("history", str(ctx.exception))
        self.assertFalse((self.target / "state" / "todo.sqlite").exists())
        shutil.rmtree(bundle)
