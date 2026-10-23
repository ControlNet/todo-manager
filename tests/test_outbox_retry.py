"""``outbox retry``: a given-up Notion state push is queued again after its cause was fixed.

Synthetic workspace, tasks and errors only; no Notion client."""
import contextlib
import io
import json

from tests.helpers import WorkspaceCase
from todo_manager import cli, effects, health, outbox, runs, store


def invoke(home, *args):
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            code = cli.main(['--home', str(home), *args])
        except SystemExit as exc:
            code = exc.code
    return code, output.getvalue()


def reject(item, reconcile):
    raise outbox.Permanent('Synthetic validation error')


class OutboxRetryTests(WorkspaceCase):
    def row(self, oid):
        return dict(self.conn.execute('SELECT * FROM outbox WHERE id=?', (oid,)).fetchone())

    def rows(self, op='push_task'):
        return [dict(r) for r in self.conn.execute('SELECT * FROM outbox WHERE op=? ORDER BY rowid', (op,))]

    def dead_push(self, kind='task'):
        if kind == 'task':
            entity = store.create_task(self.conn, 'Pushed', actor='human', origin='cli')
            oid = effects.task_push(self.conn, entity)
        else:
            entity = store.create_project(self.conn, 'Synthetic project', origin='cli', actor='human')
            oid = effects.project_push(self.conn, entity)
        outbox.process(self.conn, {('notion', 'push_' + kind): reject})
        self.assertEqual(self.row(oid)['status'], 'dead')
        return entity, oid

    def test_a_dead_push_is_queued_again_with_the_current_projection(self):
        for kind in ('task', 'project'):
            entity, dead = self.dead_push(kind)
            self.assertIsNone(effects._push(self.conn, kind, entity), 'unchanged: the dirty scan never heals it')
            code, text = invoke(self.tmp, 'outbox', 'retry', dead, '--human')
            self.assertEqual(code, 0, text)
            result = json.loads(text)
            queued = result['retried'][0]['queued']
            self.assertEqual(result['retried'][0]['op'], f'notion/push_{kind}')
            self.assertEqual(self.row(dead)['status'], 'superseded', 'the given-up row keeps its attempts and error')
            self.assertEqual(self.row(dead)['last_error'], 'Synthetic validation error')
            fresh = self.row(queued)
            self.assertEqual((fresh['status'], fresh['attempts'], fresh['entity_id']), ('pending', 0, entity))
            self.assertIsNone(effects._push(self.conn, kind, entity), 'the scan reuses the new row')
            stats = outbox.process(self.conn, {('notion', 'push_' + kind): lambda item, reconcile: {'ok': True}})
            self.assertEqual(stats['done'], 1)
        self.assertEqual(health.report(self.conn, self.cfg)['outbox_dead'], [])

    def test_only_given_up_effects_are_retried(self):
        task = store.create_task(self.conn, 'Pushed', actor='human', origin='cli')
        oid = effects.task_push(self.conn, task)

        def flaky(item, reconcile):
            raise outbox.Retryable('Synthetic rate limit', retry_after=600)
        outbox.process(self.conn, {('notion', 'push_task'): flaky})
        before = self.row(oid)
        code, text = invoke(self.tmp, 'outbox', 'retry', oid, '--human')
        self.assertEqual(code, 2)
        self.assertIn('NOT_DEAD', text)
        self.assertIn('retries by itself', text)
        self.assertEqual(self.row(oid), before, 'the retry deadline is not bypassed')
        code, text = invoke(self.tmp, 'outbox', 'retry', 'outbox_missing', '--human')
        self.assertEqual(code, 2)
        self.assertIn('NOT_FOUND', text)

    def test_effects_that_may_already_have_happened_are_refused(self):
        task = store.create_task(self.conn, 'Commented', actor='human', origin='cli')
        effects.task_comment(self.conn, task, 'synthetic-comment', 'Synthetic note')
        effects.delivery_push(self.conn, 'delivery_synthetic', task)
        outbox.enqueue(self.conn, target='notion', op='project_view', entity_type='project', entity_id='project_x',
                       dedupe_key='project_view:project_x', payload={'project_id': 'project_x'})
        handlers = {('notion', op): reject for op in ('comment', 'push_delivery', 'project_view')}
        outbox.process(self.conn, handlers)
        for op, reason in (('comment', 'may already have been posted'), ('push_delivery', 'creates'),
                           ('project_view', 'creates')):
            oid = self.rows(op)[0]['id']
            code, text = invoke(self.tmp, 'outbox', 'retry', oid, '--human')
            self.assertEqual(code, 2, op)
            self.assertIn('NOT_RETRYABLE', text)
            self.assertIn(reason, text)
            self.assertEqual(self.row(oid)['status'], 'dead')

    def test_a_refused_id_leaves_the_others_unchanged(self):
        task, dead = self.dead_push()
        effects.task_comment(self.conn, task, 'synthetic-comment', 'Synthetic note')
        outbox.process(self.conn, {('notion', 'comment'): reject})
        comment = self.rows('comment')[0]['id']
        code, text = invoke(self.tmp, 'outbox', 'retry', dead, comment, '--human')
        self.assertEqual(code, 2)
        self.assertEqual(self.row(dead)['status'], 'dead')
        self.assertEqual(len(self.rows()), 1)

    def test_a_superseded_push_says_so_and_queues_nothing(self):
        task, dead = self.dead_push()
        store.update_task(self.conn, task, {'summary': 'Changed'}, actor='human', origin='cli')
        newer = effects.task_push(self.conn, task)
        code, text = invoke(self.tmp, 'outbox', 'retry', dead, '--human')
        self.assertEqual(code, 0, text)
        self.assertEqual(json.loads(text), {'retried': [], 'superseded': [
            {'id': dead, 'op': 'notion/push_task', 'entity': task, 'by': newer}]})
        self.assertEqual(len(self.rows()), 2)

    def test_a_dead_push_behind_a_newer_one_is_settled_by_it(self):
        task = store.create_task(self.conn, 'Pushed', actor='human', origin='cli')
        first = effects.task_push(self.conn, task)
        self.conn.execute("UPDATE outbox SET status='uncertain' WHERE id=?", (first,))
        store.update_task(self.conn, task, {'summary': 'Changed'}, actor='human', origin='cli')
        newer = effects.task_push(self.conn, task)
        self.conn.execute("UPDATE outbox SET status='dead' WHERE id=?", (first,))
        code, text = invoke(self.tmp, 'outbox', 'retry', first, '--human')
        self.assertEqual(code, 0, text)
        self.assertEqual(json.loads(text)['superseded'][0]['by'], newer)
        self.assertEqual(self.row(first)['status'], 'superseded')
        self.assertEqual(len(self.rows()), 2, 'the newer push carries the current projection')

    def test_a_push_for_a_removed_task_is_refused(self):
        task, dead = self.dead_push()
        self.conn.execute('PRAGMA foreign_keys = OFF')
        self.conn.execute('DELETE FROM tasks WHERE id=?', (task,))
        code, text = invoke(self.tmp, 'outbox', 'retry', dead, '--human')
        self.assertEqual(code, 2)
        self.assertIn('no longer exists', text)
        self.assertEqual(self.row(dead)['status'], 'dead')

    def test_provenance_and_idempotency_follow_the_other_writes(self):
        task, dead = self.dead_push()
        code, text = invoke(self.tmp, 'outbox', 'retry', dead)
        self.assertEqual(code, 2)
        self.assertIn('CONTEXT_REQUIRED', text)
        run = runs.start(self.conn, 'feedback')
        code, text = invoke(self.tmp, 'outbox', 'retry', dead, '--run', run)
        self.assertEqual(code, 2)
        self.assertIn('NOT_AUTHORIZED', text)
        self.assertEqual(self.row(dead)['status'], 'dead')
        code, first = invoke(self.tmp, 'outbox', 'retry', dead, '--human', '--key', 'synthetic-retry-1')
        self.assertEqual(code, 0, first)
        code, again = invoke(self.tmp, 'outbox', 'retry', dead, '--human', '--key', 'synthetic-retry-1')
        self.assertEqual(code, 0, again)
        self.assertTrue(json.loads(again)['duplicate'])
        self.assertEqual(json.loads(again)['retried'], json.loads(first)['retried'])
        self.assertEqual(len(self.rows()), 2)

    def test_status_points_at_the_command(self):
        self.dead_push()
        problems = health.problems(health.report(self.conn, self.cfg), self.cfg)
        self.assertTrue(any('1 outbox effects gave up' in p and 'todo-manager outbox retry' in p for p in problems),
                        problems)
