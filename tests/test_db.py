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

    def test_fresh_database_takes_no_backup(self):
        db.connect(self.path).close()
        self.assertFalse((self.home / "backups").exists())


if __name__ == "__main__":
    unittest.main()
