"""Public CLI reliability regressions using synthetic data and isolated workspaces."""
import json
from pathlib import Path
from unittest.mock import patch

from tests.helpers import WorkspaceCase
from tests.test_cli_redesign import invoke
from tests.test_hermes_install import InstallCase
from todo_manager import clock, db, export, feedback, ops, runs, store


class RequestTests(WorkspaceCase):
    def test_failed_request_can_queue_and_reuse_words(self):
        args = ('task', 'update', 'tsk_missing', '--set', 'summary=Draft', '--request', 'failed')
        self.assertEqual(invoke(self.tmp, *args, '--said', 'Draft only')[0], 2)
        self.assertEqual(invoke(self.tmp, *args)[0], 2)
        code, text = invoke(self.tmp, 'feedback', 'record', '--request', 'failed', '--said', 'Draft only')
        self.assertEqual(code, 0, text)
        self.assertEqual(len(feedback.pending(self.conn)), 1)
        self.assertNotEqual(json.loads(text).get('status'), 'already_handled')
        self.assertEqual(invoke(self.tmp, *args, '--said', 'Send it')[0], 2)

    def test_queued_failure_and_partial_update_keep_pending(self):
        tid = store.create_task(self.conn, 'Draft', origin='cli')
        store.update_task(self.conn, tid, {'summary': 'Human'}, actor='human', origin='cli')
        queued = ops.feedback_record(self.conn, request='queued', said='Draft only', scope=None)
        invoke(self.tmp, 'task', 'update', 'missing', '--request', 'queued', '--set', 'summary=Draft')
        self.assertEqual(feedback.get(self.conn, queued['feedback'])['status'], 'pending')
        code, text = invoke(self.tmp, 'task', 'update', tid, '--request', 'queued', '--based-on', '1',
                            '--set', 'summary=Draft', '--set', 'title=Updated')
        self.assertEqual(code, 0, text)
        self.assertTrue(json.loads(text)['rejected'])
        self.assertEqual(feedback.get(self.conn, queued['feedback'])['status'], 'pending')
        args = ('task', 'update', tid, '--request', 'queued', '--set', 'summary=Draft')
        self.assertEqual(invoke(self.tmp, *args)[0], 0)
        self.assertTrue(json.loads(invoke(self.tmp, *args)[1])['duplicate'])
        self.assertEqual(feedback.get(self.conn, queued['feedback'])['status'], 'superseded')

    def test_failed_legacy_registration_is_not_success_evidence(self):
        feedback.record(self.conn, origin='matrix', source_id='legacy-fail', text='Draft only',
                        scope_type='global', kind='direct', author_is_user=True, status='applied',
                        result={'recorded': 'direct request'})
        result = ops.feedback_record(self.conn, request='legacy-fail', said='Draft only', scope=None)
        self.assertNotEqual(result.get('status'), 'already_handled')
        self.assertEqual(len(feedback.pending(self.conn)), 1)

    def test_success_bookkeeping_rolls_back_with_business_transaction(self):
        queued = ops.feedback_record(self.conn, request='atomic', said='Draft only', scope=None)
        ctx = ops.context(self.conn, self.cfg, request='atomic')
        self.conn.execute("CREATE TEMP TRIGGER fail_log BEFORE INSERT ON op_log BEGIN "
                          "SELECT RAISE(ABORT, 'synthetic local failure'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            ops.task_create(self.conn, self.cfg, ctx, title='Draft', fields={})
        self.assertEqual(store.list_tasks(self.conn), [])
        self.assertEqual(feedback.get(self.conn, queued['feedback'])['status'], 'pending')
        self.assertFalse(feedback.get(self.conn, ctx.fb['id'])['result']['handled'])
        self.conn.execute('DROP TRIGGER fail_log')
        ops.task_create(self.conn, self.cfg, ctx, title='Draft', fields={})
        self.assertEqual(feedback.get(self.conn, queued['feedback'])['status'], 'superseded')


class RunTests(WorkspaceCase):
    def test_restore_rejects_new_writes_but_keeps_duplicate(self):
        run = runs.start(self.conn, 'intake')
        runs.finish(self.conn, run, status='success', wake=True)
        args = ('task', 'create', '--title', 'Before', '--run', run)
        self.assertEqual(invoke(self.tmp, *args)[0], 0)
        saved = export.backup(self.conn, self.cfg)
        self.assertEqual(invoke(self.tmp, 'restore', str(saved), '--yes', '--json')[0], 0)
        self.assertTrue(json.loads(invoke(self.tmp, *args)[1])['duplicate'])
        before = {t: self.conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                  for t in ('tasks', 'evidence_links', 'outbox', 'op_log')}
        code, text = invoke(self.tmp, 'task', 'create', '--title', 'Stale', '--run', run)
        self.assertEqual(code, 2, text)
        self.assertEqual(json.loads(text)['error'], 'STALE_RUN')
        for table, count in before.items():
            self.assertEqual(self.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], count)
        new = runs.start(self.conn, 'intake')
        self.assertEqual(invoke(self.tmp, 'task', 'create', '--title', 'Fresh', '--run', new)[0], 0)

    def test_captured_context_rechecked_after_done_and_timeout(self):
        for mode in ('done', 'timeout'):
            run = runs.start(self.conn, 'intake')
            ctx = ops.context(self.conn, self.cfg, run=run)
            if mode == 'done':
                ops.run_done(self.conn, self.cfg, run)
            else:
                clock.advance(minutes=91)
            with self.assertRaises(ops.OpError) as caught:
                ops.task_create(self.conn, self.cfg, ctx, title='Stale', fields={})
            self.assertEqual(caught.exception.code, 'STALE_RUN')


class ConfigTests(WorkspaceCase):
    def test_syntax_and_type_errors_are_structured(self):
        for text in ('[slack\nquiet_minutes = 60', 'slack = 3', '[slack]\nquiet_minutes = "wrong"'):
            (self.tmp / 'config.toml').write_text(text)
            code, result = invoke(self.tmp, 'status', '--json')
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(result)['error'], 'CONFIG')
            self.assertIn('config.toml', result)
            self.assertNotIn('Traceback', result)

    def test_corrupt_config_restore_sqlite_and_bundle_preserves_raw_safety(self):
        cfgfile = self.tmp / 'config.toml'
        for kind in ('sqlite', 'bundle'):
            cfgfile.write_text('language = "en"\n')
            saved = export.backup(self.conn, self.cfg) if kind == 'sqlite' else export.export_snapshot(self.conn, self.cfg)
            corrupt = '[slack\nquiet_minutes = 60\n'
            cfgfile.write_text(corrupt)
            self.assertEqual(invoke(self.tmp, 'restore', str(saved), '--json')[0], 2)
            self.assertEqual(cfgfile.read_text(), corrupt)
            self.assertEqual(invoke(self.tmp, 'restore', str(self.tmp / 'missing'), '--yes', '--json')[0], 2)
            self.assertEqual(cfgfile.read_text(), corrupt)
            code, text = invoke(self.tmp, 'restore', str(saved), '--yes', '--json')
            self.assertEqual(code, 0, text)
            safety = Path(json.loads(text)['safety_backup'])
            self.assertEqual((export.files_of(safety) / 'config.toml').read_text(), corrupt)
            self.assertEqual(cfgfile.read_text(), 'language = "en"\n')
            self.assertIsNotNone(db.get_meta(self.conn, 'delegations.hold'))


class InstallationTests(InstallCase):
    def status(self):
        # Native context availability is tested separately; isolate installation diagnostics here.
        with patch('todo_manager.hermes_install.context_problems', return_value=[]):
            code, text = invoke(self.home, 'status', '--json')
        return code, json.loads(text)

    def test_uninstalled_partial_paused_and_missing_assets(self):
        code, result = self.status()
        self.assertEqual(code, 1)
        self.assertEqual(result['installation']['state'], 'not_installed')
        self.assertTrue(result['problems'])
        self.install()
        code, result = self.status()
        self.assertEqual(code, 0, result)
        self.assertEqual(result['installation']['state'], 'paused')
        self.hermes.cron(['cron', 'remove', self.job('intake')['id']])
        code, result = self.status()
        self.assertEqual(code, 1, result)
        self.assertEqual(result['installation']['state'], 'partial')
        self.assertIn('todo-manager-intake', ' '.join(result['problems']))
        self.paths.script('feedback').unlink()
        self.paths.launcher.unlink()
        code, result = self.status()
        self.assertEqual(code, 1)
        self.assertIn(str(self.paths.launcher), ' '.join(result['problems']))
        self.hermes.cron(['cron', 'remove', self.job('feedback')['id']])
        before = {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        code, result = self.status()
        self.assertEqual(code, 1)
        self.assertEqual(len(result['installation']['missing_jobs']), 2)
        self.assertEqual(result['installation']['state'], 'partial')
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()})


    def test_paused_jobs_do_not_raise_gate_idle_faults(self):
        self.install()
        conn = db.open_existing(self.cfg.db_path)
        try:
            for entry in ('intake', 'feedback'):
                run = runs.start(conn, entry)
                runs.finish(conn, run, status='idle')
        finally:
            conn.close()
        clock.advance(minutes=120)
        code, result = self.status()
        self.assertEqual(code, 0, result)
        self.assertEqual(result['installation']['state'], 'paused')


class AdditionalBoundaries(WorkspaceCase):
    def test_restore_invalid_and_preview_do_not_create_lock_files(self):
        saved = export.backup(self.conn, self.cfg)
        export.check_backup(saved)  # SQLite may open WAL sidecars on the source snapshot.
        (self.tmp / 'config.toml').write_text('[broken')
        before = sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob('*'))
        for source, flags in ((saved, ()), (self.tmp / 'absent', ('--yes',))):
            self.assertEqual(invoke(self.tmp, 'restore', str(source), '--json', *flags)[0], 2)
            self.assertEqual(sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob('*')), before)
        self.assertEqual(invoke(self.tmp, 'task', 'create', '--title', 'No fallback', '--human')[0], 2)
        self.assertEqual(store.list_tasks(self.conn), [])

    def test_restore_releases_running_gate_before_wake_and_rejects_updates(self):
        task = store.create_task(self.conn, 'Existing', origin='intake')
        run = runs.start(self.conn, 'intake')
        saved = export.backup(self.conn, self.cfg)
        self.assertEqual(invoke(self.tmp, 'restore', str(saved), '--yes', '--json')[0], 0)
        code, text = invoke(self.tmp, 'task', 'update', task, '--run', run, '--based-on', '1', '--set', 'summary=Old')
        self.assertEqual(code, 2, text)
        self.assertEqual(json.loads(text)['error'], 'STALE_RUN')
        self.assertFalse(store.get_task(self.conn, task)['summary'])

    def test_idle_failed_and_feedback_expired_runs_cannot_write(self):
        for mode in ('idle', 'error', 'expired'):
            run = runs.start(self.conn, 'feedback')
            if mode == 'expired':
                clock.advance(minutes=61)
            else:
                runs.finish(self.conn, run, status='success' if mode == 'idle' else 'error')
            ctx = ops.context(self.conn, self.cfg, run=run)
            with self.assertRaises(ops.OpError) as caught:
                ops.project_create(self.conn, self.cfg, ctx, title='No write', fields={}, repos=[])
            self.assertEqual(caught.exception.code, 'STALE_RUN')

    def test_restored_material_can_be_claimed_by_a_new_run(self):
        from tests.helpers import mail
        from tests.flow import run_id_of
        from todo_manager import gate_intake
        self.add_items(mail('synthetic', 'thread', 'Review the draft'))
        clock.advance(minutes=61)
        old = run_id_of(gate_intake.run_gate(self.conn, self.cfg, [], [])[0])
        saved = export.backup(self.conn, self.cfg)
        self.assertEqual(invoke(self.tmp, 'restore', str(saved), '--yes', '--json')[0], 0)
        fresh = run_id_of(gate_intake.run_gate(self.conn, self.cfg, [], [])[0])
        self.assertNotEqual(old, fresh)
        evidence = [(self.item_id('synthetic'), 'origin')]
        with self.assertRaises(ops.OpError):
            ops.task_create(self.conn, self.cfg, ops.context(self.conn, self.cfg, run=old),
                            title='Old', fields={}, evidence=evidence)
        result = ops.task_create(self.conn, self.cfg, ops.context(self.conn, self.cfg, run=fresh),
                                 title='New', fields={}, evidence=evidence)
        self.assertTrue(result['task'])
        self.assertEqual(len(store.list_tasks(self.conn)), 1)
