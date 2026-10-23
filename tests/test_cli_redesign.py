"""Redesigned command contracts; all workspaces and inputs are synthetic."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from todo_manager import cli, db, ops, store, runs
from tests.helpers import WorkspaceCase


def invoke(home, *args):
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            code = cli.main(['--home', str(home), *args])
        except SystemExit as exc:
            code = exc.code
    return code, output.getvalue()


class SurfaceTests(unittest.TestCase):
    def test_surface(self):
        parser = cli.build_parser()
        choices = next(a.choices for a in parser._actions if a.dest == 'command')
        self.assertEqual(set(choices), set('install status config pause resume sync export restore uninstall task project source feedback delegate run daemon gmail-labels outbox'.split()))

    def test_absent_read_does_not_initialize(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'absent'
            code, text = invoke(home, 'task', 'list')
            self.assertEqual(code, 2)
            self.assertIn('install', text)
            self.assertFalse(home.exists())


class BusinessContracts(WorkspaceCase):
    def test_explicit_provenance_and_request_reuse(self):
        code, text = invoke(self.tmp, 'task', 'create', '--title', 'Draft')
        self.assertEqual(code, 2)
        self.assertEqual(store.list_tasks(self.conn), [])
        code, text = invoke(self.tmp, 'task', 'create', '--title', 'Draft', '--request', 'event-1', '--said', 'Draft only')
        self.assertEqual(code, 0, text)
        task = json.loads(text)['task']
        code, text = invoke(self.tmp, 'task', 'update', task, '--request', 'event-1', '--set', 'summary=Draft')
        self.assertEqual(code, 0, text)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM feedback').fetchone()[0], 1)

    def test_json_lists_and_history(self):
        code, text = invoke(self.tmp, 'task', 'create', '--title', 'Manual', '--human')
        self.assertEqual(code, 0, text)
        task = json.loads(text)['task']
        code, text = invoke(self.tmp, 'task', 'list')
        self.assertEqual(json.loads(text)[0]['id'], task)
        code, text = invoke(self.tmp, 'task', 'show', task, '--history')
        self.assertTrue(json.loads(text)['history'])

    def test_completed_run_rejects_different_confirmations(self):
        run = runs.start(self.conn, 'intake')
        ops.run_done(self.conn, self.cfg, run)
        with self.assertRaises(ops.OpError):
            ops.run_done(self.conn, self.cfg, run, checked=['tsk_unknown'])

class SettlementContracts(WorkspaceCase):
    def claim(self, run, task, day='2026-10-05'):
        self.conn.execute("INSERT INTO followup_checks(task_id,due,state,run_id,attempts,claimed_at,updated_at) "
                          "VALUES (?,?,'claimed',?,1,'now','now')", (task, day, run))

    def test_confirmations_are_atomic_owned_and_idempotent(self):
        task = store.create_task(self.conn, 'Follow up', origin='cli', follow_up_date='2026-10-05')
        run = runs.start(self.conn, 'intake')
        self.claim(run, task)
        with self.assertRaises(ops.OpError):
            ops.run_done(self.conn, self.cfg, run, checked=[task, 'tsk_missing'])
        self.assertEqual(self.conn.execute('SELECT state FROM followup_checks').fetchone()[0], 'claimed')
        code, text = invoke(self.tmp, 'run', 'done', run, '--checked', task)
        self.assertEqual(code, 0, text)
        self.assertEqual(self.conn.execute('SELECT state FROM followup_checks').fetchone()[0], 'done')
        code, text = invoke(self.tmp, 'run', 'done', run, '--checked', task)
        self.assertTrue(json.loads(text)['duplicate'])
        self.assertEqual(invoke(self.tmp, 'run', 'done', run)[0], 2)

    def test_a_repeated_checked_option_adds_to_the_list(self):
        # 2026-10-19: an intake run wrote `--checked A --checked B --checked C`; only C was kept,
        # so A's follow-up was never confirmed and its retries ran out.
        tasks = [store.create_task(self.conn, f'Follow up {n}', origin='cli', follow_up_date='2026-10-05')
                 for n in range(3)]
        run = runs.start(self.conn, 'intake')
        for task in tasks:
            self.claim(run, task)
        code, text = invoke(self.tmp, 'run', 'done', run, '--checked', tasks[0], '--checked', tasks[1], tasks[2])
        self.assertEqual(code, 0, text)
        self.assertEqual([r[0] for r in self.conn.execute('SELECT state FROM followup_checks')], ['done'] * 3)

    def test_changed_followup_and_missing_digest_are_not_confirmed(self):
        task = store.create_task(self.conn, 'Changed follow up', origin='cli', follow_up_date='2026-10-06')
        run = runs.start(self.conn, 'intake')
        self.claim(run, task)
        for kwargs in ({'checked': [task]}, {'digest_reviewed': True}):
            with self.assertRaises(ops.OpError):
                ops.run_done(self.conn, self.cfg, run, **kwargs)
        self.assertIsNone(runs.get(self.conn, run)['released_at'])

    def test_digest_is_explicit_and_settled_once(self):
        run = runs.start(self.conn, 'intake')
        runs.write_json(self.tmp, run, 'manifest.json', {'digest': {'day': '2026-10-05', 'item_ids': []}})
        self.conn.execute("INSERT INTO daily_jobs(job,day,state,run_id,attempts,updated_at) "
                          "VALUES ('commit_digest','2026-10-05','claimed',?,1,'now')", (run,))
        ops.run_done(self.conn, self.cfg, run, digest_reviewed=True)
        self.assertEqual(self.conn.execute('SELECT state FROM daily_jobs').fetchone()[0], 'done')
        self.assertTrue(ops.run_done(self.conn, self.cfg, run, digest_reviewed=True)['duplicate'])

    def test_handled_chat_request_is_not_queued_later(self):
        ctx = ops.context(self.conn, self.cfg, request='chat-1', said='Draft a letter')
        ops.task_create(self.conn, self.cfg, ctx, title='Draft', fields={})
        result = ops.feedback_record(self.conn, request='chat-1', said='Draft a letter', scope=None)
        self.assertFalse(result['queued'])
        with self.assertRaises(ops.OpError):
            ops.feedback_record(self.conn, request='chat-1', said='Send a letter', scope=None)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM feedback WHERE status='pending'").fetchone()[0], 0)

    def test_evidence_only_update_does_not_fabricate_field_version(self):
        from tests.helpers import mail
        self.add_items(mail('evidence', 'thread', 'Synthetic evidence'))
        task = store.create_task(self.conn, 'Draft', origin='cli')
        code, text = invoke(self.tmp, 'task', 'update', task, '--human', '--evidence', self.item_id('evidence'))
        self.assertEqual(code, 0, text)
        self.assertEqual(store.get_task(self.conn, task)['version'], 1)
        self.assertEqual(len(store.evidence_for(self.conn, 'task', task)), 1)
        self.assertEqual(len(store.history(self.conn, 'task', task)), 1)

    def test_brief_file_and_explicit_authority_preserve_parallel(self):
        task = store.create_task(self.conn, 'Investigate', origin='cli')
        brief = self.tmp / 'brief.txt'
        brief.write_text('Investigate locally. Deliver a report. Do not publish.')
        args = ('delegate', 'create', '--task', task, '--brief-file', str(brief), '--allow', 'read,analyze', '--human', '--no-submit')
        code, text = invoke(self.tmp, *args)
        self.assertEqual(code, 0, text)
        self.assertNotEqual(invoke(self.tmp, *args)[0], 0)
        self.assertEqual(invoke(self.tmp, *args, '--parallel')[0], 0)


from tests.test_notion import NotionCase
from todo_manager import feedback, clock


class ClarificationContracts(NotionCase):
    def setUp(self):
        super().setUp()
        self.task = store.create_task(self.conn, 'Draft', origin='intake', responsibility='Original responsibility')
        self.push(self.task)
        self.page = self.page_of(self.task)

    def clarified(self):
        self.fake.user_set_markdown(self.page, self.fake.markdown[self.page].replace('Original responsibility', 'Ambiguous change'))
        self.pull()
        original = next(f for f in feedback.pending(self.conn) if f['kind'] == 'body_edit')
        clock.advance(minutes=2)
        self.pull()
        ops.feedback_resolve(self.conn, self.cfg, original['id'], outcome='clarify', reply='Which deadline?')
        fid, _ = feedback.record(self.conn, origin='notion_comment', source_id='synthetic-answer', text='Friday',
                                 kind='comment', scope_type='task', scope_id=self.task, author='user', author_is_user=True)
        return original['id'], fid

    def test_later_answer_releases_only_the_linked_current_body(self):
        original, answer = self.clarified()
        ops.feedback_resolve(self.conn, self.cfg, answer, outcome='applied', settles=original)
        page = self.conn.execute('SELECT managed_hash FROM notion_pages WHERE page_id=?', (self.page,)).fetchone()
        self.assertIsNone(page['managed_hash'])
        self.assertTrue(ops.feedback_resolve(self.conn, self.cfg, answer, outcome='applied', settles=original)['duplicate'])
        with self.assertRaises(ops.OpError):
            ops.feedback_resolve(self.conn, self.cfg, answer, outcome='applied', settles=None)

    def test_late_answer_cannot_release_a_newer_body(self):
        original, answer = self.clarified()
        self.fake.user_set_markdown(self.page, self.fake.markdown[self.page].replace('Ambiguous change', 'New user instruction'))
        self.pull()
        with self.assertRaises(ops.OpError) as caught:
            ops.feedback_resolve(self.conn, self.cfg, answer, outcome='applied', settles=original)
        self.assertEqual(caught.exception.code, 'STALE_BODY')
        self.assertEqual(feedback.get(self.conn, answer)['status'], 'pending')
        self.assertIsNotNone(self.conn.execute('SELECT managed_hash FROM notion_pages WHERE page_id=?', (self.page,)).fetchone()[0])

    def test_unrelated_feedback_does_not_release_protection(self):
        original, answer = self.clarified()
        ops.feedback_resolve(self.conn, self.cfg, answer, outcome='applied')
        self.assertIsNotNone(self.conn.execute('SELECT managed_hash FROM notion_pages WHERE page_id=?', (self.page,)).fetchone()[0])


class ReadContracts(unittest.TestCase):
    def test_old_workspace_read_keeps_schema_and_files(self):
        import sqlite3
        from tests.test_prefs import make_v7
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / 'state').mkdir()
            path = home / 'state' / 'todo.sqlite'
            make_v7(path)
            before = path.read_bytes()
            for args in [('task','list'), ('status','--json')]:
                code, text = invoke(home, *args)
                self.assertEqual(code, 2, text)
                self.assertIn('install', text)
            self.assertEqual(before, path.read_bytes())
            self.assertFalse((home / 'AGENTS.md').exists())
            self.assertFalse((home / 'state' / 'locks').exists())

    def test_restore_invalid_source_does_not_populate_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'target'
            code, text = invoke(home, 'restore', str(Path(tmp) / 'missing'), '--yes', '--json')
            self.assertEqual(code, 2, text)
            self.assertFalse(home.exists())

from tests.test_install import InstallCase
from todo_manager import install
from todo_manager.install_flow import complete


class InstallationContracts(InstallCase):
    def choices(self):
        return {'email.scope': 'work', 'slack.scope': 'direct_messages', 'github.enabled': 'yes',
                'notion.home_page': '1', 'kanban.assignee': 'default', 'automation.start': 'yes'}

    def install_disabled(self):
        self.page()
        answers = {**self.choices(), 'kanban.assignee': 'disabled'}
        return complete(self.home, install.AnswerPrompter(answers, {'all'}, output=lambda _: None),
                        self.world.env(), confirmed=True)

    def test_disabled_install_requires_explicit_choice_even_with_yes(self):
        self.install_disabled()
        before = (self.home / 'config.toml').read_bytes()
        with self.assertRaises(install.NeedsAnswer) as caught:
            complete(self.home, install.AnswerPrompter({}, {'all'}, output=lambda _: None),
                     self.world.env(), confirmed=True)
        self.assertEqual(caught.exception.key, 'kanban.enabled')
        self.assertEqual(before, (self.home / 'config.toml').read_bytes())
        self.assertFalse(self.world.kanban.board_exists)

    def test_disabled_install_can_enable_and_create_board(self):
        self.install_disabled()
        result = complete(self.home, install.AnswerPrompter(
            {'kanban.enabled': 'yes', 'kanban.assignee': 'default'}, {'all'}, output=lambda _: None),
            self.world.env(), confirmed=True)
        from todo_manager import config
        cfg = config.load(self.home)
        self.assertTrue(cfg.get('kanban.enabled'))
        self.assertEqual(cfg.get('kanban.assignee'), 'default')
        self.assertTrue(self.world.kanban.board_exists)
        self.assertIn('create the Hermes board', ' '.join(result['changes']))

    def test_enabling_disabled_install_still_needs_confirmation(self):
        self.install_disabled()
        before = (self.home / 'config.toml').read_bytes()
        user = install.AnswerPrompter({'kanban.enabled': 'yes', 'kanban.assignee': 'default'},
                                     declined={'install'}, output=lambda _: None)
        result = complete(self.home, user, self.world.env())
        self.assertEqual(result['status'], 'declined')
        self.assertEqual(before, (self.home / 'config.toml').read_bytes())
        self.assertFalse(self.world.kanban.board_exists)

    def test_enabling_disabled_install_rejects_invalid_profile_without_writes(self):
        self.install_disabled()
        self.world.kanban.valid_profiles = set()
        before = (self.home / 'config.toml').read_bytes()
        with self.assertRaises(install.NeedsAnswer) as caught:
            complete(self.home, install.AnswerPrompter(
                {'kanban.enabled': 'yes', 'kanban.assignee': 'default'}, {'all'}, output=lambda _: None),
                self.world.env(), confirmed=True)
        self.assertEqual(caught.exception.key, 'kanban.assignee')
        self.assertEqual(before, (self.home / 'config.toml').read_bytes())
        self.assertFalse(self.world.kanban.board_exists)

    def test_disabled_install_can_stay_disabled_and_explain_status(self):
        self.install_disabled()
        result = complete(self.home, install.AnswerPrompter(
            {'kanban.enabled': 'no'}, {'all'}, output=lambda _: None), self.world.env(), confirmed=True)
        self.assertFalse(self.world.kanban.board_exists)
        self.assertIn('Agent delegation is disabled', ' '.join(result['changes']))
        from unittest.mock import patch
        with patch('todo_manager.hermes_install.status', return_value={
                'paused_jobs': [], 'problems': [], 'state': 'ready'}), \
                patch('todo_manager.health.report', return_value={}), \
                patch('todo_manager.health.problem_items', return_value=[]):
            code, text = invoke(self.home, 'status', '--json')
            self.assertEqual(code, 0, text)
            summary = json.loads(text)['delegation_config']
            self.assertFalse(summary['enabled'])
            self.assertIn('todo-manager install', summary['message'])
            code, text = invoke(self.home, 'status')
            self.assertIn('Agent delegation is disabled', text)

    def test_confirmed_install_still_requires_choices_and_writes_nothing(self):
        self.page()
        with self.assertRaises(install.NeedsAnswer):
            complete(self.home, install.AnswerPrompter({}, {'all'}, output=lambda _: None), self.world.env(), confirmed=True)
        self.assertFalse(self.home.exists())
        self.assertEqual(self.world.notion_writes(), [])
        self.assertFalse(self.world.kanban.board_exists)
        self.assertEqual(json.loads(self.world.jobs_file.read_text())['jobs'], [])

    def test_invalid_choice_is_not_guessed_despite_confirmation(self):
        self.page()
        answers = {**self.choices(), 'github.enabled': 'maybe'}
        with self.assertRaises(install.NeedsAnswer) as caught:
            complete(self.home, install.AnswerPrompter(answers, {'all'}, output=lambda _: None), self.world.env(), confirmed=True)
        self.assertEqual(caught.exception.key, 'github.enabled')
        self.assertFalse(self.home.exists())
        self.assertEqual(self.world.notion_writes(), [])

    def test_upgrade_preserves_pause_and_runtime_settings(self):
        self.page()
        answers = install.AnswerPrompter(self.choices(), {'all'}, output=lambda _: None)
        complete(self.home, answers, self.world.env(), confirmed=True)
        jobs = json.loads(self.world.jobs_file.read_text())['jobs']
        for job in jobs:
            self.world.runner(['hermes','cron','pause',job['id']])
            self.world.runner(['hermes','cron','edit',job['id'],'--deliver','local','--workdir','/synthetic/user/workdir'])
        before = json.loads(self.world.jobs_file.read_text())['jobs']
        complete(self.home, install.AnswerPrompter({}, {'all'}, output=lambda _: None), self.world.env(), confirmed=True)
        after = json.loads(self.world.jobs_file.read_text())['jobs']
        self.assertEqual([(j['id'],j['state'],j['workdir'],j['deliver']) for j in before],
                         [(j['id'],j['state'],j['workdir'],j['deliver']) for j in after])
        self.assertTrue(list((self.home / 'backups').glob('pre-install*.sqlite')))

    def test_unsettled_old_run_blocks_protocol_switch(self):
        install.init_workspace(self.home, 'en')
        from todo_manager import config
        conn = db.open_existing(config.load(self.home).db_path)
        try:
            run = runs.start(conn, 'intake')
            runs.finish(conn, run, status='success', wake=True)
        finally:
            conn.close()
        with self.assertRaises(install.InstallError):
            complete(self.home, install.AnswerPrompter(self.choices(), {'all'}, output=lambda _: None), self.world.env(), confirmed=True)
        self.assertEqual(self.world.calls, [])
        self.assertEqual(self.world.notion_writes(), [])

class RecoveryContracts(WorkspaceCase):
    def test_bundle_existing_restore_and_hold_are_separate_from_cron_resume(self):
        from todo_manager import export, delegation
        from unittest.mock import patch
        task = store.create_task(self.conn, 'Kept', origin='cli')
        run = runs.start(self.conn, 'intake')
        runs.finish(self.conn, run, status='success', wake=True)
        bundle = export.export_snapshot(self.conn, self.cfg)
        later = store.create_task(self.conn, 'After saved state', origin='cli')
        code, text = invoke(self.tmp, 'restore', str(bundle), '--json')
        self.assertEqual(code, 2)
        self.assertIsNotNone(store.get_task(self.conn, later))
        code, text = invoke(self.tmp, 'restore', str(bundle), '--yes', '--json')
        self.assertEqual(code, 0, text)
        result = json.loads(text)
        self.assertTrue(Path(result['safety_backup']).exists())
        self.assertIsNotNone(store.get_task(self.conn, task))
        self.assertIsNone(store.get_task(self.conn, later))
        self.assertIsNotNone(runs.get(self.conn, run)['released_at'])
        self.assertEqual(runs.get(self.conn, run)['summary']['done']['coverage'], 'unconfirmed')
        self.assertIsNotNone(db.get_meta(self.conn, 'delegations.hold'))
        with patch('todo_manager.hermes_install.Installer.set_jobs_state', return_value=['resumed']) as state:
            self.assertEqual(invoke(self.tmp, 'resume', '--json')[0], 0)
            state.assert_called_once_with('resume')
            self.assertIsNotNone(db.get_meta(self.conn, 'delegations.hold'))
            self.assertEqual(invoke(self.tmp, 'resume', '--release-recovery-hold', '--json')[0], 2)
            code, text = invoke(self.tmp, 'resume', '--release-recovery-hold', '--yes', '--json')
            self.assertEqual(code, 0, text)
            self.assertEqual(state.call_count, 1, 'recovery release must not implicitly resume cron')

    def test_valid_bundle_rebuilds_an_existing_empty_directory_without_confirmation(self):
        from todo_manager import export
        task = store.create_task(self.conn, 'Portable', origin='cli')
        bundle = export.export_snapshot(self.conn, self.cfg)
        target = self.tmp / 'empty-target'
        target.mkdir()
        code, text = invoke(target, 'restore', str(bundle), '--json')
        self.assertEqual(code, 0, text)
        self.assertTrue(json.loads(text)['delegations_held'])
        code, text = invoke(target, 'task', 'show', task)
        self.assertEqual(json.loads(text)['title'], 'Portable')

    def test_writer_waits_for_exclusive_restore_lock(self):
        import subprocess
        import sys
        from todo_manager.locks import file_lock, lock_path
        with file_lock(lock_path(self.tmp, 'workspace')):
            proc = subprocess.Popen([sys.executable, '-B', '-m', 'todo_manager.cli', '--home', str(self.tmp),
                                     'task', 'create', '--title', 'After recovery', '--human'],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    proc.communicate(timeout=0.4)
                self.assertEqual(store.list_tasks(self.conn), [])
            except BaseException:
                proc.kill()
                proc.communicate()
                raise
        output, error = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, error)
        self.assertEqual(len(store.list_tasks(self.conn)), 1)

    def test_pre_run_failure_nonzero_and_idle_native_contract(self):
        from todo_manager import prerun
        from unittest.mock import patch
        output, errors = io.StringIO(), io.StringIO()
        with patch('todo_manager.prerun.prepare', return_value='{"wakeAgent": false}'):
            with contextlib.redirect_stdout(output):
                self.assertEqual(prerun.feedback(self.tmp), 0)
        self.assertEqual(json.loads(output.getvalue()), {'wakeAgent': False})
        with patch('todo_manager.prerun.prepare', side_effect=RuntimeError('synthetic failure')):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
                self.assertEqual(prerun.intake(self.tmp), 1)
        self.assertIn('synthetic failure', errors.getvalue())

class LegacyAssetContracts(InstallCase):
    def test_old_gate_marker_upgrades_but_cannot_be_adopted_by_other_workspace(self):
        from todo_manager import hermes_install
        self.page()
        self.install({'notion.home_page': '1'})
        inst = self.world.env().hermes_installer(self.cfg_for_home())
        for entry in hermes_install.JOBS:
            path = inst.paths.script(entry)
            old = path.read_text().replace(hermes_install.SCRIPT_MARK,
                    '# todo-manager pre-run gate (generated; overwritten by `todo-manager hermes install`)')
            path.write_text(old)
        actions = inst.plan()
        self.assertFalse(any(a.kind == 'conflict' for a in actions))
        from todo_manager import config
        other = self.world.env().hermes_installer(config.load(self.tmp / 'other'))
        self.assertTrue(other.ownership_conflict())
        inst.apply(actions)
        self.assertTrue(all('from todo_manager.prerun import' in inst.paths.script(e).read_text() for e in hermes_install.JOBS))

    def cfg_for_home(self):
        from todo_manager import config
        return config.load(self.home)

class OutputAndSettingsContracts(WorkspaceCase):
    def test_admin_json_and_rejected_native_runtime_setting(self):
        code, text = invoke(self.tmp, 'config', 'get', 'language', '--json')
        self.assertEqual(code, 0, text)
        self.assertIn('language', json.loads(text))
        state = self.tmp / 'state/hermes'
        state.mkdir(parents=True)
        (state / 'install.json').write_text('{"version":2,"jobs":{"intake":{"id":"synthetic-job"}}}')
        code, text = invoke(self.tmp, 'config', 'set', 'hermes.model', 'synthetic-model', '--human', '--json')
        self.assertEqual(code, 2, text)
        self.assertEqual(json.loads(text)['error'], 'NATIVE_SETTING')

    def test_queued_request_text_is_stable_when_main_chat_handles_it(self):
        record = ops.feedback_record(self.conn, request='deferred-1', said='Draft only', scope=None)
        with self.assertRaises(ops.OpError):
            ops.context(self.conn, self.cfg, request='deferred-1', said='Send now')
        ctx = ops.context(self.conn, self.cfg, request='deferred-1')
        result = ops.task_create(self.conn, self.cfg, ctx, title='Draft', fields={})
        self.assertTrue(result['task'])
        self.assertEqual(feedback.get(self.conn, record['feedback'])['status'], 'superseded')
