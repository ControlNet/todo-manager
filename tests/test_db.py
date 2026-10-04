"""Schema migrations on an existing workspace (no wipe; automatic rollback point)."""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from todo_manager import db


def make_v1(path: Path) -> None:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    db.schema_version(conn)
    conn.execute("BEGIN")
    for statement in db._split_sql(db.MIGRATIONS[1]):
        conn.execute(statement)
    conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '1')")
    conn.execute("INSERT INTO tasks(id, title, status, next_actor, origin, created_at, updated_at)"
                 " VALUES ('tsk_1', 'Old task', 'todo', 'me', 'cli', 'x', 'x')")
    conn.execute("COMMIT")
    conn.close()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="tm-db-"))
        (self.home / "state").mkdir()
        self.path = self.home / "state" / "todo.sqlite"

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_v1_workspace_upgrades_in_place_with_a_backup(self):
        make_v1(self.path)
        conn = db.connect(self.path)
        try:
            self.assertEqual(db.schema_version(conn), db.SCHEMA_VERSION)
            self.assertEqual(conn.execute("SELECT title FROM tasks WHERE id='tsk_1'").fetchone()[0], "Old task")
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(delegation_messages)")}
            self.assertTrue({"target_event_id", "approval_id", "cause"} <= cols)
        finally:
            conn.close()
        backups = list((self.home / "backups").glob(f"pre-schema-v{db.SCHEMA_VERSION}-from-v1-*.sqlite"))
        self.assertEqual(len(backups), 1)
        old = sqlite3.connect(str(backups[0]))
        try:
            self.assertEqual(old.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "1")
            self.assertEqual(old.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
        finally:
            old.close()
        # Re-opening an up-to-date database takes no further backup.
        db.connect(self.path).close()
        self.assertEqual(len(list((self.home / "backups").glob("pre-schema-*.sqlite"))), 1)

    def test_surfaced_follow_ups_become_done_checks(self):
        make_v1(self.path)
        conn = sqlite3.connect(str(self.path), isolation_level=None)
        conn.execute("UPDATE tasks SET follow_up_date='2026-10-01' WHERE id='tsk_1'")
        conn.execute("INSERT INTO tasks(id, title, status, next_actor, origin, follow_up_date, created_at, updated_at)"
                     " VALUES ('tsk_2', 'Moved on', 'waiting', 'external', 'cli', '2026-10-09', 'x', 'x')")
        for tid in ("tsk_1", "tsk_2"):
            conn.execute("INSERT INTO decisions(id, run_id, op, target_type, target_id, created_at)"
                         " VALUES (?, 'run_1', 'followup_surfaced', 'task', ?, '2026-10-01T09:00:00Z')", (f"d_{tid}", tid))
        conn.close()
        conn = db.connect(self.path)
        try:
            rows = [tuple(r) for r in conn.execute("SELECT task_id, due, state FROM followup_checks")]
        finally:
            conn.close()
        # tsk_2's follow-up date moved after it was surfaced: that is a new, open check.
        self.assertEqual(rows, [("tsk_1", "2026-10-01", "done")])

    def test_fresh_database_takes_no_backup(self):
        db.connect(self.path).close()
        self.assertFalse((self.home / "backups").exists())


if __name__ == "__main__":
    unittest.main()
