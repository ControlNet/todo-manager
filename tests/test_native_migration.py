"""Schema-8 synthetic data survives upgrade; backups remain usable by schema-8 code."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from todo_manager import config, db, delegation, export, store
from todo_manager.kanban import FakeKanban
from tests.helpers import WorkspaceCase


class MigrationTests(unittest.TestCase):
    def test_schema8_rows_and_unknown_actions_survive_and_backup_stays_v8(self):
        with tempfile.TemporaryDirectory(prefix='tm-migration9-') as temp:
            path = Path(temp) / 'state/todo.sqlite'
            path.parent.mkdir()
            old = sqlite3.connect(path, isolation_level=None)
            old.row_factory = sqlite3.Row
            db.migrate(old, target=8)
            old.execute("INSERT INTO tasks(id,title,status,origin,created_at,updated_at,version,next_actor) "
                        "VALUES('tsk_old','Preserved task','todo','cli','x','x',1,'me')")
            old.close()
            current = db.connect(path)
            self.assertEqual(db.schema_version(current), db.SCHEMA_VERSION)
            self.assertEqual(current.execute('SELECT title FROM tasks').fetchone()[0], 'Preserved task')
            self.assertEqual(current.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertFalse(current.execute('PRAGMA foreign_key_check').fetchall())
            current.close()
            backup = next((Path(temp) / 'backups').glob(f'pre-schema-v{db.SCHEMA_VERSION}-from-v8-*.sqlite'))
            old = sqlite3.connect(f'file:{backup}?mode=ro', uri=True)
            self.assertEqual(old.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], '8')
            self.assertEqual(old.execute('SELECT title FROM tasks').fetchone()[0], 'Preserved task')
            old.close()


class SuccessorAndExportTests(WorkspaceCase):
    def test_done_followup_stays_on_task_and_pins_previous_delivery(self):
        task = store.create_task(self.conn, 'Synthetic task', origin='cli', actor='human')
        first = delegation.create(self.conn, self.cfg, task_id=task, title='First', goal='Draft')
        kb = FakeKanban()
        delegation.submit_pending(self.conn, self.cfg, kb)
        card = delegation.get(self.conn, first)['card_id']
        kb.worker_start(card)
        kb.worker_complete(card, 'First draft')
        delegation.sync_cards(self.conn, self.cfg, kb)
        second = delegation.create(self.conn, self.cfg, task_id=task, title='Revise', goal='Revise the first draft')
        successor = delegation.get(self.conn, second)
        self.assertEqual(successor['previous_delegation_id'], first)
        self.assertEqual(successor['previous_delivery_id'], delegation.deliveries(self.conn, first)[0]['id'])
        self.assertEqual(kb.cards[card]['status'], 'done')
        self.assertEqual(successor['task_id'], task)
        bundle = export.export_snapshot(self.conn, self.cfg)
        manifest = json.loads((bundle / 'manifest.json').read_text())
        self.assertTrue((bundle / 'framework/cron.json').exists())
        self.assertTrue((bundle / 'framework/feedback.md').exists())
        self.assertIn('delegation_children', manifest['tables'])
        self.assertTrue(any('native memory' in x for x in manifest['not_exported']))
        export.check_bundle(bundle)


class ReviewMigrationTests(unittest.TestCase):
    def test_schema9_backup_retry_marker_and_idempotent_upgrade(self):
        with tempfile.TemporaryDirectory(prefix='tm-migration10-') as temp:
            path = Path(temp) / 'state/todo.sqlite'
            path.parent.mkdir()
            old = sqlite3.connect(path, isolation_level=None)
            old.row_factory = sqlite3.Row
            db.migrate(old, target=9)
            cfg = config.load(Path(temp))
            task = store.create_task(old, 'Preserved task', actor='human', origin='cli')
            did = delegation.create(old, cfg, task_id=task, title='Old', goal='Preserved')
            old.execute("UPDATE delegations SET status='ended',card_id='old-card',event_cursor=42 WHERE id=?", (did,))
            old.execute("INSERT INTO delegation_children VALUES (?, 'old-child', 41, 'blocked')", (did,))
            old.close()
            current = db.connect(path)
            before = dict(current.execute('SELECT * FROM delegations WHERE id=?', (did,)).fetchone())
            self.assertEqual(before['children_sync_pending'], 1)
            self.assertEqual(before['event_cursor'], 42)
            self.assertIsNone(current.execute('SELECT attention FROM delegation_children').fetchone()[0])
            db.migrate(current)
            self.assertEqual(dict(current.execute('SELECT * FROM delegations WHERE id=?', (did,)).fetchone()), before)
            self.assertEqual(current.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertFalse(current.execute('PRAGMA foreign_key_check').fetchall())
            current.close()
            backups = list((Path(temp) / 'backups').glob('pre-schema-v10-from-v9-*.sqlite'))
            self.assertEqual(len(backups), 1)
            backup = sqlite3.connect(f'file:{backups[0]}?mode=ro', uri=True)
            rollback = sqlite3.connect(Path(temp) / 'rollback.sqlite')
            backup.backup(rollback)
            self.assertEqual(rollback.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], '9')
            self.assertEqual(rollback.execute('SELECT card_id,event_cursor FROM delegations').fetchone(), ('old-card', 42))
            self.assertEqual(rollback.execute('SELECT card_id,event_cursor FROM delegation_children').fetchone(), ('old-child', 41))
            rollback.close()
            backup.close()
