"""Decision-board behavior with synthetic Notion pages; no live API writes."""
import json
from todo_manager import clock, db, feedback, ops, store
from todo_manager.notion import setup, schema
from tests.test_notion import NotionCase


class DecisionBoardTests(NotionCase):
    def test_new_install_has_board_and_all_open_tasks(self):
        specs = {s['key']: s for s in setup.view_specs(self.sync.state.task_props.ids,
                                                     self.sync.state.project_props.ids, 'zh-CN')}
        self.assertEqual(specs['decision']['type'], 'board')
        self.assertEqual(specs['decision']['configuration']['group_by']['property_id'],
                         self.sync.state.task_props.ids['decision'])
        self.assertIn('all', specs)
        self.assertFalse(specs['decision']['configuration']['group_by']['hide_empty_groups'])

    def test_home_order_is_the_users(self):
        headings = [line[3:] for line in setup.home_markdown('zh-CN').splitlines() if line.startswith('## ')]
        self.assertEqual(headings, ['🤔 需要决定', '✅ 待我验收／待我补充', '🤖 Agent 排队／处理中', '⭐ 精选待办',
                                    '📁 活跃项目', '⏳ 等待别人', '📋 全部未完成任务', '🕘 最近完成与重要变化'])
        self.assertEqual([s['key'] for s in setup.view_specs(self.sync.state.task_props.ids,
                                                             self.sync.state.project_props.ids, 'zh-CN')],
                         list(setup.SECTIONS))

    def test_drag_records_feedback_and_reversal_invalidates_old_context(self):
        tid = store.create_task(self.conn, 'Attend a meeting?', origin='cli', status='decision')
        self.push(tid)
        page = self.page_of(tid)
        self.fake.user_edit(page, **{'决定': '同意'})
        self.pull()
        pending = [f for f in feedback.pending(self.conn) if f['kind'] == 'decision_choice']
        self.assertEqual(len(pending), 1)
        old = pending[0]
        ctx = ops.context(self.conn, self.cfg, feedback_id=old['id'])
        self.assertEqual(store.get_task(self.conn, tid)['decision'], 'agree')
        self.assertEqual(store.get_task(self.conn, tid)['status'], 'decision')
        self.pull()
        self.assertEqual(len([f for f in feedback.pending(self.conn) if f['kind']=='decision_choice']), 1)
        self.fake.user_edit(page, **{'决定': '不同意'})
        self.pull()
        with self.assertRaises(ops.OpError):
            ops.perform(self.conn, ctx, 'synthetic stale mutation', {}, lambda: {'should_not_run': True})
        self.assertEqual(feedback.get(self.conn, old['id'])['status'], 'superseded')
        self.assertEqual(store.get_task(self.conn, tid)['decision'], 'decline')

    def test_agent_drag_queues_request_without_completing_task(self):
        tid = store.create_task(self.conn, 'Draft only', origin='cli', status='decision')
        self.push(tid)
        self.fake.user_edit(self.page_of(tid), **{'决定': '交给 Agent'})
        self.pull()
        task = store.get_task(self.conn, tid)
        self.assertEqual(task['next_actor'], 'agent')
        self.assertEqual(task['status'], 'todo')
        requests = [f for f in feedback.pending(self.conn) if f['kind']=='delegate_request']
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['payload']['decision'], 'agent')
        self.pull()
        self.assertEqual(len([f for f in feedback.pending(self.conn) if f['kind']=='delegate_request']), 1)

    def test_creation_choice_change_replaces_claimed_context(self):
        page = self.fake.user_create_page(self.tasks_ds, 'New choice', **{'决定': '同意'})
        self.pull()
        original = next(f for f in feedback.pending(self.conn) if f['kind']=='new_task')
        ctx = ops.context(self.conn, self.cfg, feedback_id=original['id'])
        self.fake.user_edit(page, **{'决定': '交给 Agent'})
        self.pull()
        with self.assertRaises(ops.OpError):
            ops.perform(self.conn, ctx, 'old creation interpretation', {}, lambda: self.fail('stale'))
        pending = feedback.pending(self.conn)
        replacement = next(f for f in pending if f['kind']=='new_task')
        self.assertEqual(replacement['payload']['values']['decision'], 'agent')
        self.assertEqual(replacement['payload']['complete'], original['payload']['complete'])
        self.assertFalse([f for f in pending if f['kind']=='delegate_request'])
        clock.advance(minutes=2)
        self.sync.check_bodies(10)
        replacement = next(f for f in feedback.pending(self.conn) if f['kind']=='new_task')
        fresh = ops.context(self.conn, self.cfg, feedback_id=replacement['id'])
        result = ops.perform(self.conn, fresh, 'new creation interpretation', {}, lambda: {'ok': True})
        self.assertTrue(result['ok'])

    def test_cancel_in_same_edit_wins_over_agent_column(self):
        tid = store.create_task(self.conn, 'Cancel this', origin='cli', status='decision')
        self.push(tid)
        self.fake.user_edit(self.page_of(tid), **{'决定': '交给 Agent', '状态': '取消'})
        self.pull()
        self.assertEqual(store.get_task(self.conn, tid)['status'], 'cancelled')
        self.assertEqual(store.get_task(self.conn, tid)['next_actor'], 'me')

    def test_same_minute_round_trip_is_a_new_decision(self):
        tid = store.create_task(self.conn, 'Choice', origin='cli', status='decision')
        self.push(tid)
        page = self.page_of(tid)
        for choice in ('同意', '不同意', '同意'):
            self.fake.user_edit(page, **{'决定': choice})
            self.pull()
        pending = [f for f in feedback.pending(self.conn) if f['kind']=='decision_choice']
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['payload']['decision'], 'agree')
        self.assertEqual(self.conn.execute("SELECT count(*) FROM feedback WHERE kind='decision_choice'").fetchone()[0], 3)

    def test_board_choice_cannot_approve_external_actions(self):
        tid = store.create_task(self.conn, 'Draft response', origin='cli', status='decision')
        self.push(tid)
        self.fake.user_edit(self.page_of(tid), **{'决定': '交给 Agent'})
        self.pull()
        fb = next(f for f in feedback.pending(self.conn) if f['kind']=='delegate_request')
        ctx = ops.context(self.conn, self.cfg, feedback_id=fb['id'])
        with self.assertRaisesRegex(ops.OpError, 'external'):
            ops.delegate_create(self.conn, self.cfg, ctx, task_id=tid, title='Draft', goal='Send',
                                allowed=['send_email'], submit=False)
        with self.assertRaisesRegex(ops.OpError, 'approval'):
            ops.delegate_approve(self.conn, self.cfg, ctx, 'not-needed', delivery='not-needed', action='send_email')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM delegations').fetchone()[0], 0)

    def test_pending_withdraws_choice_and_unverified_drag_cannot_delegate(self):
        tid = store.create_task(self.conn, 'Choice', origin='cli', status='decision')
        self.push(tid)
        page = self.page_of(tid)
        self.fake.user_edit(page, **{'决定': '同意'})
        self.pull()
        self.fake.user_edit(page, **{'决定': '待决定'})
        self.pull()
        self.assertFalse([f for f in feedback.pending(self.conn) if f['kind']=='decision_choice'])
        self.fake.user_edit(page, author='unverified-user', **{'决定': '交给 Agent'})
        self.pull()
        self.assertEqual(store.get_task(self.conn, tid)['next_actor'], 'me')
        self.assertFalse([f for f in feedback.pending(self.conn) if f['kind']=='delegate_request'])

    def test_complete_operation_retry_survives_later_drag(self):
        tid = store.create_task(self.conn, 'Choice', origin='cli', status='decision')
        self.push(tid)
        page = self.page_of(tid)
        self.fake.user_edit(page, **{'决定': '同意'})
        self.pull()
        fb = next(f for f in feedback.pending(self.conn) if f['kind']=='decision_choice')
        ctx = ops.context(self.conn, self.cfg, feedback_id=fb['id'])
        first = ops.perform(self.conn, ctx, 'synthetic operation', {}, lambda: {'ok': True})
        self.fake.user_edit(page, **{'决定': '不同意'})
        self.pull()
        result = ops.perform(self.conn, ctx, 'synthetic operation', {}, lambda: self.fail('must not replay'))
        self.assertTrue(result['duplicate'])


class BoardChoiceTests(NotionCase):
    """The board's four choices (user's design, 2026-10-12) take effect at the next pull without a
    model run, and a handled card leaves the board (its Decision is cleared)."""

    def drag(self, label, **more):
        tid = store.create_task(self.conn, 'Offered work', origin='cli', status='decision',
                                next_step='Agent 可以：起草回复')
        self.push(tid)
        self.fake.user_edit(self.page_of(tid), **{'决定': label, **more})
        self.pull()
        return tid

    def test_new_board_offers_cancel_done_my_todo_and_agent(self):
        self.assertEqual(list(schema.labels('decision', 'zh-CN').values()),
                         ['待决定', '取消', '已完成', '设为待办', '交给 Agent'])
        options = [o['name'] for o in schema.data_source_properties('task', self.projects_ds, 'zh-CN')['决定']['select']['options']]
        self.assertEqual(options, ['待决定', '取消', '已完成', '设为待办', '交给 Agent'])

    def test_each_choice_sets_the_status_and_leaves_the_board(self):
        for label, status in (('取消', 'cancelled'), ('已完成', 'done'), ('设为待办', 'todo')):
            with self.subTest(label):
                tid = self.drag(label)
                task = store.get_task(self.conn, tid)
                self.assertEqual((task['status'], task['next_actor'], task['decision']), (status, 'me', None))
                self.assertEqual([f for f in feedback.pending(self.conn) if f['scope_id'] == tid], [],
                                 'nothing for a model to interpret')
                self.push(tid)
                props = self.fake.pages[self.page_of(tid)]['properties']
                self.assertIsNone(props['决定']['select'])
                self.assertEqual(props['状态']['select']['name'], schema.label('task_status', status, 'zh-CN'))
                self.pull()
                self.assertEqual(store.get_task(self.conn, tid)['version'], task['version'], 'no echo')
                if status != 'todo':
                    self.assertIn('需要决定', task['closed_reason'] or '')

    def test_assign_to_agent_queues_the_request_and_clears_the_choice(self):
        tid = self.drag('交给 Agent')
        task = store.get_task(self.conn, tid)
        self.assertEqual((task['status'], task['next_actor'], task['decision']), ('todo', 'agent', None))
        self.assertEqual([f['kind'] for f in feedback.pending(self.conn)], ['delegate_request'])

    def test_a_status_set_in_the_same_edit_wins(self):
        tid = self.drag('设为待办', **{'状态': '等待'})
        self.assertEqual(store.get_task(self.conn, tid)['status'], 'waiting')


class DecisionMigrationTests(NotionCase):
    def old_ui(self):
        from todo_manager.db import get_meta, loads, set_meta
        views = loads(get_meta(self.conn, 'notion.views'), {})
        del self.fake.views[views.pop('all')]
        t = self.sync.state.task_props.ids
        self.fake.views[views['decision']].update(type='list',
            filter={'property':t['status'], 'select':{'equals':'需要决定'}},
            sorts=[{'property':t['due_date'], 'direction':'ascending'}],
            configuration={'type':'list','properties':[{'property_id':t[k],'visible':True}
                           for k in ('next_step','due_date','project_id')]})
        self.fake.data_sources[self.tasks_ds]['properties'].pop('决定')
        t = dict(t); t.pop('decision')
        set_meta(self.conn, 'notion.props.task', json.dumps(t))
        set_meta(self.conn, 'notion.views', json.dumps(views))
        self.conn.execute("DELETE FROM meta WHERE key='notion.decision_ui_version'")
        return views

    def test_install_migration_preview_backup_retry_and_preservation(self):
        from todo_manager.notion import decision_migration
        from todo_manager.db import get_meta, loads
        from todo_manager.install import Install, AnswerPrompter
        old = self.old_ui()
        tid = store.create_task(self.conn, 'Keep task and notes', origin='cli', status='decision')
        # A pre-upgrade page with no Decision property.
        page = self.fake.user_create_page(self.tasks_ds, 'Keep task and notes', **{'状态':'需要决定'})
        before = len(self.writes())
        plan = decision_migration.plan(self.conn, self.client)
        self.assertTrue(plan['changes'])
        self.assertEqual(len(self.writes()), before)
        inst = Install(self.tmp, AnswerPrompter({}, {'all'}, output=lambda _:None))
        inst._notion_views(self.conn, self.client)
        self.assertTrue((self.tmp/'backups/notion-decision-ui-v1.json').exists())
        views = loads(get_meta(self.conn, 'notion.views'), {})
        self.assertIn(old['decision'], self.fake.views)
        self.assertNotEqual(old['decision'], views['decision'])
        self.assertEqual(self.fake.views[views['decision']]['position'], {'type':'start'})
        self.assertIn('all', views)
        self.assertIsNotNone(store.get_task(self.conn, tid))
        self.assertEqual(self.fake.pages[page]['properties']['决定']['select']['name'], '待决定')
        before = len(self.writes())
        inst._notion_views(self.conn, self.client)
        self.assertEqual(len(self.writes()), before)
        self.assertEqual(feedback.pending(self.conn), [])

    def test_customized_view_is_not_overwritten(self):
        from todo_manager.notion import decision_migration
        views = self.old_ui()
        self.fake.views[views['decision']]['filter'] = {'custom': True}
        before = len(self.writes())
        with self.assertRaisesRegex(RuntimeError, 'customized'):
            decision_migration.plan(self.conn, self.client)
        self.assertEqual(len(self.writes()), before)

    def test_native_decoded_property_ids_are_recognized(self):
        from urllib.parse import quote
        from todo_manager.notion import decision_migration
        from todo_manager.db import get_meta, loads, set_meta
        views = self.old_ui()
        props = loads(get_meta(self.conn, 'notion.props.task'), {})
        # Model the native API: encoded IDs in the data source, decoded IDs in views.
        view = self.fake.views[views['decision']]
        props['status'] = 'a%7Cb'; view['filter']['property'] = 'a|b'
        props['due_date'] = 'd%3A'; view['sorts'][0]['property'] = 'd:'
        view['configuration']['properties'][1]['property_id'] = 'd:'
        set_meta(self.conn, 'notion.props.task', json.dumps(props))
        self.assertIsNotNone(decision_migration.plan(self.conn, self.client))

    def test_lost_create_response_recovers_same_board(self):
        from todo_manager.notion import decision_migration
        from todo_manager.notion.client import NotionUncertain
        from todo_manager.db import get_meta, loads
        self.old_ui()
        self.fake.lose_response.add(('POST', '/views'))
        with self.assertRaises(NotionUncertain):
            decision_migration.apply(self.conn, self.client, self.tmp)
        count = len(self.fake.views)
        decision_migration.apply(self.conn, self.client, self.tmp)
        self.assertEqual(len(self.fake.views), count + 1, 'only the all-open view remains to create')
        self.assertEqual(get_meta(self.conn, 'notion.decision_ui_version'), decision_migration.VERSION)
        self.assertEqual(len([v for v in self.fake.views.values() if v['name']=='需要决定' and v['type']=='board']), 1)


    def test_all_open_tasks_get_their_heading_after_the_decision_board_once(self):
        from todo_manager.notion import decision_migration
        from todo_manager.notion.client import NotionUncertain
        from todo_manager.db import get_meta, loads
        views = self.old_ui()
        home = get_meta(self.conn, 'notion.home_page_id')
        container = lambda key: self.fake.views[views[key]]['parent']['database_id']
        b = self.fake.block
        self.fake.blocks[home] = [b('callout', 'freshness'), b('heading_2', '⭐ 精选待办'),
                                  b('child_database', id=container('focus')), b('heading_2', '🤔 需要决定'),
                                  b('child_database', id=container('decision')),
                                  b('heading_2', '🤖 Agent 排队／处理中'), b('child_database', id=container('agent'))]
        self.fake.lose_response.add(('PATCH', f'/blocks/{home}/children'))
        with self.assertRaises(NotionUncertain):
            decision_migration.apply(self.conn, self.client, self.tmp)
        decision_migration.apply(self.conn, self.client, self.tmp)
        all_open = self.fake.views[loads(get_meta(self.conn, 'notion.views'), {})['all']]['parent']['database_id']
        order = [x['id'] if x['type'] == 'child_database' else self.fake.text(x) for x in self.fake.blocks[home]]
        self.assertEqual(order[3:8], ['🤔 需要决定', container('decision'), '📋 全部未完成任务', all_open,
                                      '🤖 Agent 排队／处理中'])
        self.assertEqual(order.count('📋 全部未完成任务'), 1, 'a lost response is reconciled, not repeated')


class DecisionChoicesMigrationTests(NotionCase):
    """A board set up with Agree / Decline gets the four choices at install; an old option still on a
    page is kept until it is gone."""

    def old_options(self, *used):
        from todo_manager.db import set_meta
        prop = self.fake.data_sources[self.tasks_ds]['properties']['决定']
        prop['select']['options'] = [{'name': n, 'color': c} for n, c in
                                     (('待决定', 'gray'), ('同意', 'green'), ('不同意', 'red'), ('交给 Agent', 'purple'))]
        for label in used:
            self.fake.user_create_page(self.tasks_ds, 'Old choice', **{'决定': label})
        set_meta(self.conn, 'notion.decision_ui_version', '1')
        return prop

    def names(self):
        return [o['name'] for o in self.fake.data_sources[self.tasks_ds]['properties']['决定']['select']['options']]

    def test_install_replaces_agree_and_decline_once(self):
        from todo_manager.notion import decision_migration
        from todo_manager.db import get_meta
        prop_id = self.old_options()['id']
        plan = decision_migration.plan(self.conn, self.client)
        self.assertTrue(any('取消' in c or 'Cancel' in c for c in plan['changes']))
        decision_migration.apply(self.conn, self.client, self.tmp)
        self.assertEqual(self.names(), ['待决定', '取消', '已完成', '设为待办', '交给 Agent'])
        self.assertEqual(self.fake.data_sources[self.tasks_ds]['properties']['决定']['id'], prop_id)
        self.assertEqual(get_meta(self.conn, 'notion.decision_ui_version'), '2')
        before = len(self.writes())
        self.assertIsNone(decision_migration.plan(self.conn, self.client))
        self.assertEqual(len(self.writes()), before)

    def test_an_old_option_in_use_is_kept(self):
        from todo_manager.notion import decision_migration
        self.old_options('同意')
        decision_migration.apply(self.conn, self.client, self.tmp)
        self.assertEqual(self.names(), ['待决定', '取消', '已完成', '设为待办', '交给 Agent', '同意'])


class DecisionSchemaMigrationTests(__import__('unittest').TestCase):
    def test_upgrade_preserves_data_backups_and_reruns(self):
        import sqlite3, tempfile
        from pathlib import Path
        from todo_manager import export
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'state/todo.sqlite'; path.parent.mkdir()
            old = sqlite3.connect(path, isolation_level=None); old.row_factory=sqlite3.Row
            db.migrate(old, target=10)
            task = store.create_task(old, 'Old task', origin='cli', status='decision')
            old.close()
            conn = db.connect(path)
            self.assertEqual(store.get_task(conn, task)['title'], 'Old task')
            self.assertIsNone(store.get_task(conn, task)['decision'])
            db.migrate(conn)
            self.assertEqual(export.integrity_problems(conn), [])
            conn.close()
            copies = list((Path(tmp)/'backups').glob('pre-schema-v11-from-v10-*.sqlite'))
            self.assertEqual(len(copies), 1)
            restored = sqlite3.connect(copies[0]);
            self.assertEqual(restored.execute("select value from meta where key='schema_version'").fetchone()[0], '10')
            self.assertEqual(restored.execute('select title from tasks').fetchone()[0], 'Old task')
            restored.close()
