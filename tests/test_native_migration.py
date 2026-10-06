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
            self.assertEqual(db.schema_version(current), 9)
            self.assertEqual(current.execute('SELECT title FROM tasks').fetchone()[0], 'Preserved task')
            self.assertEqual(current.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertFalse(current.execute('PRAGMA foreign_key_check').fetchall())
            current.close()
            backup = next((Path(temp) / 'backups').glob('pre-schema-v9-from-v8-*.sqlite'))
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
