"""A model writing ``null`` for "no value": cleared, never stored as text or sent to Notion as a date.

Synthetic workspace and tasks only."""
import contextlib
import io

from tests.helpers import WorkspaceCase
from todo_manager import cli, effects, outbox, store


def invoke(home, *args):
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            code = cli.main(['--home', str(home), *args])
        except SystemExit as exc:
            code = exc.code
    return code, output.getvalue()


class NullValues(WorkspaceCase):
    def test_cli_null_clears_the_field(self):
        task = store.create_task(self.conn, 'Follow up', actor='agent', origin='intake', status='waiting',
                                 next_actor='external', follow_up_date='2026-10-15', waiting_on='Someone')
        code, text = invoke(self.tmp, 'task', 'update', task, '--human', '--set', 'status=done',
                            '--set', 'follow_up_date=null', '--set', 'waiting_on=null')
        self.assertEqual(code, 0, text)
        row = store.get_task(self.conn, task)
        self.assertIsNone(row['follow_up_date'])
        self.assertIsNone(row['waiting_on'])

    def test_store_rejects_a_date_that_is_not_a_date(self):
        task = store.create_task(self.conn, 'Dated', actor='agent', origin='intake')
        for field in ('due_date', 'follow_up_date', 'snooze_until'):
            with self.assertRaises(store.StoreError, msg=field):
                store.update_task(self.conn, task, {field: 'null'}, actor='human', origin='cli')
        with self.assertRaises(store.StoreError):
            store.create_task(self.conn, 'Bad', actor='agent', origin='intake', due_date='next Friday')
        store.update_task(self.conn, task, {'due_date': '2026-10-15'}, actor='human', origin='cli')
        self.assertEqual(store.get_task(self.conn, task)['due_date'], '2026-10-15')

    def test_cli_reports_a_bad_date(self):
        task = store.create_task(self.conn, 'Dated', actor='agent', origin='intake')
        code, text = invoke(self.tmp, 'task', 'update', task, '--human', '--set', 'due_date=soon')
        self.assertNotEqual(code, 0)
        self.assertIn('due_date', text)
        self.assertIsNone(store.get_task(self.conn, task)['due_date'])

    def test_a_newer_push_replaces_a_given_up_one(self):
        task = store.create_task(self.conn, 'Pushed', actor='human', origin='cli')
        effects.task_push(self.conn, task)

        def reject(item, reconcile):
            raise outbox.Permanent('Synthetic validation error')
        outbox.process(self.conn, {('notion', 'push_task'): reject})
        dead = self.conn.execute("SELECT id FROM outbox WHERE status='dead'").fetchall()
        self.assertEqual(len(dead), 1)
        self.assertIsNone(effects.task_push(self.conn, task), 'an unchanged projection stays given up')
        store.update_task(self.conn, task, {'summary': 'Fixed'}, actor='human', origin='cli')
        self.assertIsNotNone(effects.task_push(self.conn, task))
        status = self.conn.execute('SELECT status FROM outbox WHERE id=?', (dead[0]['id'],)).fetchone()['status']
        self.assertEqual(status, 'superseded')
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outbox WHERE status='dead'").fetchone()[0], 0)
