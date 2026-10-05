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

    def test_v5_box_edits_and_comments_get_revisions(self):
        """B01/B02 (v6): a pending box edit seeds the box's last seen fingerprint, so re-reading
        the same box after the upgrade does not present it as a new edit."""
        make_v1(self.path)
        conn = sqlite3.connect(str(self.path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        for version in range(2, 6):
            for statement in db._split_sql(db.MIGRATIONS[version]):
                conn.execute(statement)
        conn.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
        conn.execute("INSERT INTO notion_pages(page_id, entity_type, entity_id, managed_hash) VALUES"
                     " ('pg_1', 'task', 'tsk_1', 'ours'), ('pg_2', 'task', 'tsk_2', 'ours2')")
        conn.execute("INSERT INTO feedback(id, origin, source_id, source_version, received_at, scope_type, kind, status)"
                     " VALUES ('fb_1', 'notion_page', 'pg_1', 'body:theirs', 'x', 'task', 'body_edit', 'pending')")
        conn.execute("INSERT INTO notion_comments(comment_id, page_id, content_hash, seen_at) VALUES ('c1', 'pg_1', 'h', 'x')")
        conn.close()
        conn = db.connect(self.path)
        try:
            rows = {r["page_id"]: (r["box_revision"], r["box_seen_hash"], r["notes_revision"])
                    for r in conn.execute("SELECT * FROM notion_pages")}
            self.assertEqual(rows, {"pg_1": (0, "theirs", 0), "pg_2": (0, "ours2", 0)})
            self.assertEqual(conn.execute("SELECT revision FROM notion_comments").fetchone()[0], 1)
        finally:
            conn.close()
        self.assertEqual(len(list((self.home / "backups").glob(f"pre-schema-v{db.SCHEMA_VERSION}-from-v5-*.sqlite"))),
                         1, "the rollback point is the pre-migration copy")

    def test_v5_messages_survive_the_rebuild_in_order(self):
        """B04 (v6): delegation_messages is rebuilt without its table-level UNIQUE."""
        make_v1(self.path)
        conn = sqlite3.connect(str(self.path), isolation_level=None)
        for version in range(2, 6):
            for statement in db._split_sql(db.MIGRATIONS[version]):
                conn.execute(statement)
        conn.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
        conn.execute("INSERT INTO delegations(id, task_id, title, goal, allowed_actions, requirement_version, status,"
                     " board, idem_key, created_at, updated_at) VALUES ('dlg_1', 'tsk_1', 't', 'g', '[]', 2, 'blocked',"
                     " 'b', 'k', 'x', 'x')")
        for n, (kind, fb) in enumerate([("requirement", None), ("answer", "fbk_1"), ("supplement", "fbk_1")]):
            conn.execute("INSERT INTO delegation_messages(id, delegation_id, kind, feedback_id, body, requirement_version,"
                         " status, created_at, target_event_id, approval_id, cause) VALUES (?, 'dlg_1', ?, ?, ?, 1,"
                         " 'pending', 'same', ?, NULL, 'c')", (f"m{n}", kind, fb, f"body {n}", 40 + n))
        conn.close()
        conn = db.connect(self.path)
        try:
            rows = [(r["id"], r["kind"], r["target_event_id"], r["cause"], r["op_key"])
                    for r in conn.execute("SELECT * FROM delegation_messages ORDER BY created_at, rowid")]
            self.assertEqual(rows, [("m0", "requirement", 40, "c", None), ("m1", "answer", 41, "c", None),
                                    ("m2", "supplement", 42, "c", None)])
            conn.execute("INSERT INTO delegation_messages(id, delegation_id, kind, feedback_id, body, requirement_version,"
                         " created_at, op_key) VALUES ('m3', 'dlg_1', 'supplement', 'fbk_1', 'b', 3, 'x', 'fbk_1#1')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO delegation_messages(id, delegation_id, kind, feedback_id, body,"
                             " requirement_version, created_at) VALUES ('m4', 'dlg_1', 'supplement', 'fbk_1', 'b', 3, 'x')")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            conn.close()

    def test_v6_workspace_keeps_its_messages_and_feedback(self):
        """Round 3 (v7): message card positions, feedback holds and comment presence are added
        columns; rows written before them read as before (positions unknown, nothing held)."""
        make_v1(self.path)
        conn = sqlite3.connect(str(self.path), isolation_level=None)
        for version in range(2, 7):
            for statement in db._split_sql(db.MIGRATIONS[version]):
                conn.execute(statement)
        conn.execute("UPDATE meta SET value='6' WHERE key='schema_version'")
        conn.execute("INSERT INTO delegations(id, task_id, title, goal, allowed_actions, requirement_version, status,"
                     " board, idem_key, created_at, updated_at) VALUES ('dlg_1', 'tsk_1', 't', 'g', '[]', 2, 'review',"
                     " 'b', 'k', 'x', 'x')")
        conn.execute("INSERT INTO delegation_messages(id, delegation_id, kind, body, requirement_version, status,"
                     " created_at, target_seq) VALUES ('m1', 'dlg_1', 'accept', 'ok', 2, 'pending', 'x', 0)")
        conn.execute("INSERT INTO feedback(id, origin, source_id, received_at, scope_type, kind, status)"
                     " VALUES ('fb_1', 'notion_comment', 'c1', 'x', 'task', 'comment', 'pending')")
        conn.execute("INSERT INTO notion_comments(comment_id, page_id, content_hash, seen_at, feedback_id)"
                     " VALUES ('c1', 'pg_1', 'h', 'x', 'fb_1')")
        conn.close()
        conn = db.connect(self.path)
        try:
            m = conn.execute("SELECT * FROM delegation_messages WHERE id='m1'").fetchone()
            self.assertEqual((m["status"], m["target_seq"], m["card_event_before"], m["card_event_id"]),
                             ("pending", 0, None, None))
            self.assertIsNone(conn.execute("SELECT hold_reason FROM feedback WHERE id='fb_1'").fetchone()[0])
            self.assertEqual(conn.execute("SELECT presence FROM notion_comments").fetchone()[0], "listed")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            conn.close()
        backups = list((self.home / "backups").glob(f"pre-schema-v{db.SCHEMA_VERSION}-from-v6-*.sqlite"))
        self.assertEqual(len(backups), 1, "the rollback point is the pre-migration copy")
        old = sqlite3.connect(str(backups[0]))
        try:
            self.assertEqual(old.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "6")
        finally:
            old.close()

    def test_v7_delegations_keep_working_without_the_capacity_hold(self):
        """Schema 8: Hermes schedules agent work, so messages held for local capacity go out;
        a hold of the user's pause stays; pending page-notes feedback is closed (notes are
        context now). Cards, bindings and approvals are untouched."""
        make_v1(self.path)
        conn = sqlite3.connect(str(self.path), isolation_level=None)
        for version in range(2, 8):
            for statement in db._split_sql(db.MIGRATIONS[version]):
                conn.execute(statement)
        conn.execute("UPDATE meta SET value='7' WHERE key='schema_version'")
        conn.execute("INSERT INTO delegations(id, task_id, title, goal, allowed_actions, requirement_version, status,"
                     " board, idem_key, card_id, created_at, updated_at) VALUES ('dlg_1', 'tsk_1', 't', 'g', '[]', 1,"
                     " 'blocked', 'b', 'k', 'card_1', 'x', 'x')")
        for mid, error in (("m_cap", "waiting for capacity"), ("m_pause", "paused by user")):
            conn.execute("INSERT INTO delegation_messages(id, delegation_id, kind, body, requirement_version, status,"
                         " error, created_at, target_seq) VALUES (?, 'dlg_1', 'answer', 'yes', 1, 'waiting_capacity',"
                         " ?, 'x', 0)", (mid, error))
        conn.execute("INSERT INTO feedback(id, origin, source_id, received_at, scope_type, kind, status)"
                     " VALUES ('fb_n', 'notion_page', 'pg_1:notes', 'x', 'task', 'page_notes', 'pending')")
        conn.close()
        conn = db.connect(self.path)
        try:
            status = dict(conn.execute("SELECT id, status FROM delegation_messages").fetchall())
            self.assertEqual(status, {"m_cap": "pending", "m_pause": "waiting_capacity"})
            d = conn.execute("SELECT status, card_id FROM delegations WHERE id='dlg_1'").fetchone()
            self.assertEqual(tuple(d), ("blocked", "card_1"))
            self.assertEqual(conn.execute("SELECT status FROM feedback WHERE id='fb_n'").fetchone()[0], "superseded")
            self.assertEqual(db.get_meta(conn, "prefs.migration_from"), "7", "the rules move on the next open")
        finally:
            conn.close()

    def test_fresh_database_takes_no_backup(self):
        db.connect(self.path).close()
        self.assertFalse((self.home / "backups").exists())


if __name__ == "__main__":
    unittest.main()
