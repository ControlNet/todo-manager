"""Synthetic native diagnostics; no real gateway, models or external delivery."""
import json
from unittest.mock import patch
from tests.test_hermes_install import InstallCase as HermesCase
from tests.test_install import InstallCase
from tests.test_cli_redesign import invoke
from todo_manager import config, db, health, hermes_install, install


class BoardInstallTests(InstallCase):
    def test_archived_board_is_not_accepted_or_recreated(self):
        self.page()
        self.world.kanban.board_exists = True
        with patch.object(self.world.kanban, 'ping', return_value={
                'board_exists': True, 'board_state': 'archived', 'board_available': False,
                'missing': [], 'reason': 'board is archived'}), patch.object(self.world.kanban, 'create_board') as create:
            self.install({'notion.home_page': '1'})
        create.assert_not_called()
        self.assertFalse(config.load(self.home).get('kanban.enabled'))


class CronStatusTests(HermesCase):
    def test_native_storage_failure_reaches_cli_problems_without_writing(self):
        self.install()
        diagnostic = {'state': 'attention', 'write_probe': False, 'skipped_runs': None,
                      'problems': ['cron store: synthetic ENOSPC'], 'warnings': []}
        before = {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        with patch('todo_manager.hermes_install.cron_diagnostics', return_value=diagnostic, create=True), \
                patch('todo_manager.hermes_install.context_problems', return_value=[]):
            code, text = invoke(self.home, 'status', '--json')
        out = json.loads(text)
        self.assertEqual(code, 1, out)
        self.assertIn('synthetic ENOSPC', ' '.join(out['problems']))
        self.assertFalse(out['installation']['cron_health']['write_probe'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()})

    def test_unreadable_job_store_is_reported_without_repair(self):
        self.install()
        path = self.paths.jobs_file
        path.write_text('{synthetic invalid JSON')
        with patch('todo_manager.hermes_install.context_problems', return_value=[]):
            code, text = invoke(self.home, 'status', '--json')
        result = json.loads(text)
        self.assertEqual(code, 1)
        self.assertIn('cannot read native cron jobs', ' '.join(result['problems']))
        self.assertEqual(result['installation']['missing_jobs'], [])
        self.assertEqual(path.read_text(), '{synthetic invalid JSON')


class ReadOnlyCronTests(HermesCase):
    """Native API test doubles for filesystem failures, never a production write probe."""
    def check(self, *, active=True, free=10**9, heartbeat=1, success=1, error=None, writable=True):
        import contextlib
        import sys
        from types import SimpleNamespace
        from todo_manager.hermes_diagnostics import cron_health
        jobs = SimpleNamespace(use_cron_store=lambda home: contextlib.nullcontext(),
            get_ticker_heartbeat_age=lambda: heartbeat, get_ticker_success_age=lambda: success,
            get_ticker_last_error=lambda: error, TICKER_INTERVAL_SECONDS=60)
        storage = SimpleNamespace(free_bytes=lambda path: free, LOW_FREE_BYTES=100 << 20)
        (self.hermes_home / 'cron').mkdir(parents=True, exist_ok=True)
        with patch.dict(sys.modules, {'cron.jobs': jobs, 'cron.store_health': storage}), \
                patch('todo_manager.hermes_diagnostics.os.access', return_value=writable):
            return cron_health(str(self.hermes_home), active)

    def test_full_disk_and_permissions_are_problems_even_when_paused(self):
        for kwargs in ({'free': 0}, {'writable': False}):
            result = self.check(active=False, **kwargs)
            self.assertEqual(result['state'], 'attention')
            self.assertTrue(result['problems'])
            self.assertIsNone(result['writable'])

    def test_missing_heartbeat_and_low_space_are_explicit_warnings(self):
        result = self.check(heartbeat=None, success=None, free=4096)
        self.assertEqual(result['state'], 'unknown')
        self.assertFalse(result['problems'])
        self.assertGreaterEqual(len(result['warnings']), 2)
        self.assertIsNone(result['skipped_runs'])

    def test_stale_ticker_is_problem_only_for_active_jobs(self):
        self.assertTrue(self.check(heartbeat=600, success=600)['problems'])
        self.assertFalse(self.check(active=False, heartbeat=600, success=600)['problems'])

    def test_symlink_store_target_and_tick_lock_are_inspected(self):
        target = self.tmp / 'storage'
        target.mkdir()
        cron = self.hermes_home / 'cron'
        cron.mkdir(parents=True, exist_ok=True)
        (cron / 'jobs.json').unlink(missing_ok=True)  # Replace only the synthetic fixture.
        (cron / 'jobs.json').symlink_to(target / 'jobs.json')
        result = self.check()
        self.assertIn(str(target), result['free_bytes'])
        self.assertIn(str(cron), result['free_bytes'])

from tests.helpers import WorkspaceCase
from todo_manager import delegation, ops, store
from todo_manager.kanban import FakeKanban, KanbanError


class BoardRecoveryTests(WorkspaceCase):
    overrides = {'kanban': {'enabled': True}}

    def test_health_reports_archived_board_without_changing_business_state(self):
        before = self.conn.total_changes
        with patch('todo_manager.health.hermes_install.context_problems', return_value=[]), \
                patch('todo_manager.kanban.BridgeKanban.from_config') as factory:
            factory.return_value.ping.return_value = {'board_exists': True, 'board_available': False,
                'board_state': 'archived', 'reason': 'board is archived'}
            factory.return_value.profile_valid.return_value = True
            report = health.report(self.conn, self.cfg)
        self.assertIn('archived', ' '.join(report['problems']))
        self.assertEqual(report['kanban_board']['board_state'], 'archived')
        self.assertEqual(self.conn.total_changes, before)

    def test_unavailable_board_keeps_request_pending_without_creating_a_card(self):
        task = store.create_task(self.conn, 'Synthetic draft', origin='cli')
        ctx = ops.context(self.conn, self.cfg, request='synthetic-request', said='Draft locally')
        # Use the business API without submitting, then fault the read-only native lookup.
        result = ops.delegate_create(self.conn, self.cfg, ctx, task_id=task, title='Draft',
                                     goal='Draft', context_text='Synthetic', deliverable='Draft',
                                     allowed=['read', 'analyze', 'draft'], submit=False)
        kb = FakeKanban()
        with patch.object(kb, 'find', side_effect=KanbanError('board is archived', code='BOARD_ARCHIVED')), \
                patch.object(kb, 'create') as create:
            for _ in range(2):
                stats = delegation.submit_pending(self.conn, self.cfg, kb)
                self.assertEqual(stats['errors'], 1)
            create.assert_not_called()
        row = delegation.get(self.conn, result['delegation'])
        self.assertEqual(row['status'], 'pending_submit')
        self.assertIn('archived', row['last_error'])
        self.assertIsNone(row['card_id'])
