"""Behavior regressions with synthetic Notion, Kanban, source and installer fixtures only."""
import json
from unittest.mock import patch
from tests.helpers import WorkspaceCase
from tests import test_notion as notion_cases, test_hermes_install as install_cases
from tests.test_sources import FakeHttp, gmail_message
from todo_manager import clock, db, delegation as dg, ops, store, outbox, effects, gate_intake, config, hermes_install
from todo_manager.kanban import FakeKanban, KanbanError
from todo_manager.sources.base import Item, FetchResult, persist_fetch, conversation_root
from todo_manager.sources.gmail import GmailSource
from todo_manager.net import HttpError
from todo_manager.notion import body


class DelegationAudit(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.kb = FakeKanban()
        self.task = store.create_task(self.conn, 'Synthetic task', actor='human', origin='cli')
        self.did = dg.create(self.conn, self.cfg, task_id=self.task, title='Draft', goal='Draft only')
        dg.submit_pending(self.conn, self.cfg, self.kb)
        self.root = dg.get(self.conn, self.did)['card_id']

    def sync(self):
        return dg.sync_cards(self.conn, self.cfg, self.kb)

    def question(self, card):
        self.kb.worker_start(card)
        self.kb.worker_block(card, 'Format?')
        self.sync()
        return dg.deliveries(self.conn, self.did)[-1]

    def answer(self, delivery, approve=None):
        ctx = ops.context(self.conn, self.cfg, request=delivery['id'], said='Markdown')
        return ops.delegate_answer(self.conn, self.cfg, ctx, self.did, question=delivery['id'], body='Markdown', approve=approve)

    def test_r01_supplement_does_not_close_the_same_question(self):
        question = self.question(self.root)
        dg.add_message(self.conn, self.did, 'supplement', 'Use the updated style')
        dg.send_messages(self.conn, self.cfg, self.kb)
        self.sync()
        self.answer(question)
        dg.send_messages(self.conn, self.cfg, self.kb)
        self.assertEqual(self.kb.cards[self.root]['status'], 'ready')

    def test_r02_explicit_failure_reply_can_retry(self):
        self.kb.worker_start(self.root)
        self.kb.worker_crash(self.root)
        self.sync()
        failure = dg.deliveries(self.conn, self.did)[-1]
        self.assertEqual(failure['kind'], 'failure')
        self.answer(failure)
        dg.send_messages(self.conn, self.cfg, self.kb)
        self.assertEqual(self.kb.cards[self.root]['status'], 'ready')

    def test_r03_terminal_notification_registration_retries(self):
        self.cfg.data['kanban'].update(notify_platform='matrix', notify_chat_id='synthetic', notify_profile='default')
        self.conn.execute('UPDATE delegations SET notification_route=NULL WHERE id=?', (self.did,))
        self.kb.worker_start(self.root)
        self.kb.worker_complete(self.root, 'Done')
        with patch.object(self.kb, 'notify', side_effect=KanbanError('synthetic outage')):
            self.sync()
        self.conn.close()
        self.conn = db.connect(self.cfg.db_path)
        self.sync()
        self.assertIn(self.root, self.kb.subscriptions)

    def test_r08_cancel_ended_root_stops_owned_running_child(self):
        child = self.kb.create(title='Child', body='Synthetic', idem_key='child', assignee='default', tenant='test')
        self.kb.ownership[child] = self.root
        self.kb.worker_start(child)
        self.kb.worker_start(self.root)
        self.kb.worker_complete(self.root, 'Root done')
        self.sync()
        store.update_task(self.conn, self.task, {'status': 'cancelled'}, actor='human', origin='cli')
        dg.enforce_task_lifecycle(self.conn)
        dg.send_messages(self.conn, self.cfg, self.kb)
        self.assertEqual(self.kb.cards[child]['status'], 'archived')

    def child(self):
        card = self.kb.create(title='Child', body='Synthetic', idem_key='child-' + str(len(self.kb.cards)),
                              assignee='default', tenant='test')
        self.kb.ownership[card] = self.root
        return card

    def restart(self):
        self.conn.close()
        self.conn = db.connect(self.cfg.db_path)

    def test_r01_root_answer_and_supplement_keep_child_question_open(self):
        root_question = self.question(self.root)
        child = self.child()
        child_question = self.question(child)
        self.answer(root_question)
        dg.add_message(self.conn, self.did, 'supplement', 'Keep both questions')
        self.answer(child_question)
        dg.send_messages(self.conn, self.cfg, self.kb)
        self.assertEqual(self.kb.cards[child]['status'], 'ready')
        self.assertEqual(self.kb.cards[self.root]['status'], 'ready')

    def test_r01_changed_requirements_do_not_reuse_question_authorization(self):
        question = self.question(self.root)
        dg.add_message(self.conn, self.did, 'supplement', 'Different recipient')
        with self.assertRaises(ops.OpError):
            self.answer(question, approve=['send_email'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM approvals').fetchone()[0], 0)
        self.answer(question)

    def test_r02_child_failure_has_exact_recoverable_target(self):
        child = self.child()
        self.kb.cards[child]['max_retries'] = 1
        self.kb.worker_start(child)
        self.kb.worker_crash(child)
        self.sync()
        failure = next(x for x in dg.deliveries(self.conn, self.did) if x['card_id'] == child)
        self.assertEqual(failure['kind'], 'failure')
        self.answer(failure)
        original = self.kb.transition
        def lose(*a, **kw):
            original(*a, **kw)
            raise KanbanError('Synthetic response loss', uncertain=True)
        with patch.object(self.kb, 'transition', lose):
            dg.send_messages(self.conn, self.cfg, self.kb)
        self.restart()
        dg.send_messages(self.conn, self.cfg, self.kb)
        self.assertEqual(len(self.kb.snapshot(child)['comments']), 1)
        self.kb.worker_start(child)
        self.kb.worker_crash(child)
        self.sync()
        ctx = ops.context(self.conn, self.cfg, request='late-failure', said='Try again')
        with self.assertRaises(ops.OpError):
            ops.delegate_answer(self.conn, self.cfg, ctx, self.did, question=failure['id'], body='Try again')

    def test_c_cancel_ended_root_retries_discovery_and_archive_after_restart(self):
        child = self.child()
        external = self.kb.create(title='External', body='Synthetic', idem_key='external', assignee='default', tenant='test')
        for card in (self.root, child, external):
            self.kb.worker_start(card)
        self.kb.worker_complete(self.root, 'Done')
        with patch.object(self.kb, 'owned', side_effect=KanbanError('Synthetic discovery outage')):
            self.sync()
        with self.assertRaises(dg.DelegationError):
            dg.create(self.conn, self.cfg, task_id=self.task, title='Duplicate', goal='No')
        store.update_task(self.conn, self.task, {'status': 'cancelled'}, actor='human', origin='cli')
        with patch.object(self.kb, 'owned', side_effect=KanbanError('Synthetic discovery outage')):
            dg.send_messages(self.conn, self.cfg, self.kb)
        self.assertNotEqual(dg.get(self.conn, self.did)['cancel_state'], 'confirmed')
        self.restart()
        original = self.kb.transition
        def lose(card, *a, **kw):
            result = original(card, *a, **kw)
            if card == child:
                raise KanbanError('Synthetic lost archive', uncertain=True)
            return result
        with patch.object(self.kb, 'transition', lose):
            dg.send_messages(self.conn, self.cfg, self.kb)
        self.restart()
        dg.send_messages(self.conn, self.cfg, self.kb)
        self.assertEqual(self.kb.cards[child]['status'], 'archived')
        self.assertEqual(self.kb.cards[external]['status'], 'running')
        self.assertEqual(dg.get(self.conn, self.did)['cancel_state'], 'confirmed')

    def test_g_last_child_notification_repair_survives_terminal_sync(self):
        self.cfg.data['kanban'].update(notify_platform='matrix', notify_chat_id='synthetic', notify_profile='default')
        self.conn.execute('UPDATE delegations SET notification_route=NULL WHERE id=?', (self.did,))
        child = self.child()
        for card in (self.root, child):
            self.kb.worker_start(card)
            self.kb.worker_complete(card, 'Done')
        with patch.object(self.kb, 'notify', side_effect=KanbanError('Synthetic outage')):
            self.sync()
        count = self.conn.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]
        self.restart()
        self.sync()
        self.assertIn(child, self.kb.subscriptions)
        self.assertEqual(count, self.conn.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
        self.assertEqual(self.sync()['checked'], 0)

    def test_r03_terminal_registration_has_finite_failure_conclusion(self):
        self.cfg.data['kanban'].update(notify_platform='matrix', notify_chat_id='synthetic', notify_profile='default')
        self.conn.execute('UPDATE delegations SET notification_route=NULL WHERE id=?', (self.did,))
        self.kb.worker_start(self.root)
        self.kb.worker_complete(self.root, 'Done')
        with patch.object(self.kb, 'notify', side_effect=KanbanError('Synthetic persistent outage')):
            for _ in range(12):
                self.sync()
            self.assertEqual(self.sync()['checked'], 0)
        row = self.conn.execute("SELECT last_error FROM cursors WHERE scope=?", ('notifications:' + self.root,)).fetchone()
        self.assertIn('exhausted; manual repair required', row['last_error'])


class NotionAudit(notion_cases.NotionCase):
    def local_task(self):
        task = store.create_task(self.conn, 'Synthetic', actor='human', origin='cli')
        self.push(task)
        return task

    def test_r05a_unread_new_task_is_not_complete_input(self):
        page = self.fake.user_create_page(self.tasks_ds, 'Draft only', markdown='Never send it')
        from todo_manager.notion.client import NotionError
        with patch.object(self.client, 'page_markdown', side_effect=NotionError('synthetic outage', status=503)):
            self.sync.pull('task')
        fb = self.conn.execute("SELECT id FROM feedback WHERE kind='new_task'").fetchone()[0]
        from todo_manager import feedback
        self.assertIsNotNone(ops.incomplete_reason(feedback.get(self.conn, fb)))

    def test_r05b_incomplete_feedback_cannot_commit_mutation(self):
        from todo_manager import feedback
        task = self.local_task()
        fid, _ = feedback.record(self.conn, origin='notion_page', source_id='synthetic', text='Partial',
                                 scope_type='task', scope_id=task, kind='new_task', author_is_user=True,
                                 payload={'truncated': True, 'complete': True})
        ctx = ops.context(self.conn, self.cfg, feedback_id=fid)
        with self.assertRaises(ops.OpError):
            ops.task_update(self.conn, self.cfg, ctx, task, changes={'title': 'Unsafe'})
        self.assertEqual(store.get_task(self.conn, task)['title'], 'Synthetic')

    def test_r06_managed_box_race_preserves_human_text(self):
        task = self.local_task()
        page = self.page_of(task)
        row = self.sync.entity_row('task', task)
        self.fake.user_set_markdown(page, self.fake.markdown[page].replace('</callout>', 'Human constraint\n</callout>'))
        store.update_task(self.conn, task, {'summary': 'Local changed'}, actor='program', origin='cli')
        result = self.sync._sync_body(page, store.get_task(self.conn, task))
        self.assertIn('held', result)
        self.assertIn('Human constraint', self.fake.markdown[page])
        fb = self.conn.execute("SELECT author_is_user,payload FROM feedback WHERE kind='body_edit'").fetchone()
        self.assertTrue(fb['author_is_user'])
        self.assertFalse(json.loads(fb['payload'])['truncated'])

    def test_r07_confirmation_keeps_discussion_delivery_anchor(self):
        task = self.local_task()
        page = self.page_of(task)
        first = self.client.create_comment(markdown='Question', page_id=page)
        self.sync._record_bot_comment(first, page, 'dlv:original-question')
        clock.advance(minutes=1)
        ack = self.client.create_comment(markdown='Answer queued', discussion_id=first['discussion_id'])
        self.sync._record_bot_comment(ack, page, 'reply:ack')
        cid = self.fake.user_comment(page, 'More about the old question', discussion_id=first['discussion_id'])
        self.sync.poll_comments()
        row = self.conn.execute('SELECT payload FROM feedback WHERE source_id=?', (cid,)).fetchone()
        payload = db.loads(row[0])
        self.assertEqual(payload.get('in_reply_to'), 'dlv:original-question')

    def test_r09_dirty_scan_preserves_retry_after(self):
        task = self.local_task()
        store.update_task(self.conn, task, {'summary': 'Changed'}, actor='program', origin='cli')
        calls = []
        def retry(item, reconcile):
            calls.append(item['id'])
            raise outbox.Retryable('synthetic rate limit', retry_after=3600)
        for _ in range(3):
            self.sync.enqueue_dirty()
            outbox.process(self.conn, {('notion', 'push_task'): retry})
            clock.advance(minutes=5)
        self.assertEqual(len(calls), 1)

    def test_r10_duplicate_project_does_not_abort_pull(self):
        pid = store.create_project(self.conn, 'Project', actor='human', origin='cli')
        effects.project_push(self.conn, pid)
        self.drain()
        original = store.get_project(self.conn, pid)['notion_page_id']
        self.fake.user_create_page(self.projects_ds, 'Copy', **{'TM ID': pid})
        self.fake.user_create_page(self.projects_ds, 'Independent')
        result = self.sync.pull('project')
        self.assertEqual(len(store.list_projects(self.conn)), 2, result)
        self.assertEqual(store.get_project(self.conn, pid)['notion_page_id'], original)

    def test_r11_unknown_project_relation_survives_push(self):
        task = self.local_task()
        page = self.page_of(task)
        project_page = self.fake.user_create_page(self.projects_ds, 'New remote project')
        self.fake.user_edit(page, **{'项目': [project_page]})
        self.sync.pull('task')
        self.sync.enqueue_dirty()
        self.drain()
        self.assertEqual(self.fake.pages[page]['properties']['项目']['relation'], [{'id': project_page}])
        self.sync.pull('project')
        self.sync.pull('task')
        self.assertIsNotNone(store.get_task(self.conn, task)['project_id'])

    def test_r12_equal_text_in_other_discussion_is_not_delivery_proof(self):
        task = self.local_task()
        page = self.page_of(task)
        one = self.client.create_comment(markdown='Question one', page_id=page)
        two = self.client.create_comment(markdown='Question two', page_id=page)
        self.client.create_comment(markdown='Answer queued', discussion_id=one['discussion_id'])
        item = {'id': 'synthetic-effect', 'payload': {'task_id': task, 'text': 'Answer queued',
                'discussion_id': two['discussion_id'], 'key': 'reply:second'}}
        with self.assertRaises(outbox.Uncertain):
            self.sync.post_comment(item, reconcile=True)
        self.assertEqual(len(self.fake.comments), 3)

    def test_a_old_notion_discussion_never_answers_new_root_or_child_question(self):
        from todo_manager import feedback
        from tests.fake_notion import BOT_ID
        kb = FakeKanban()
        for is_child in (False, True):
            with self.subTest(child=is_child):
                task = self.local_task()
                did = dg.create(self.conn, self.cfg, task_id=task, title='Draft', goal='Draft')
                dg.submit_pending(self.conn, self.cfg, kb)
                root = dg.get(self.conn, did)['card_id']
                card = root
                if is_child:
                    card = kb.create(title='Child', body='Synthetic', idem_key='child', assignee='default', tenant='test')
                    kb.ownership[card] = root
                kb.worker_start(card)
                kb.worker_block(card, 'Question one')
                dg.sync_cards(self.conn, self.cfg, kb)
                q1 = next(x for x in dg.deliveries(self.conn, did) if x['card_id'] == card)
                self.drain()
                anchor = self.conn.execute('SELECT discussion_id FROM notion_comments WHERE ref=?', ('dlv:' + q1['id'],)).fetchone()[0]
                dg.add_message(self.conn, did, 'supplement', 'More background')
                dg.send_messages(self.conn, self.cfg, kb)
                self.fake.user_comment(self.page_of(task), 'Markdown', discussion_id=anchor)
                self.sync.poll_comments()
                fb = next(x for x in feedback.pending(self.conn) if x['scope_id'] == task and x['kind'] == 'comment')
                ctx = ops.context(self.conn, self.cfg, feedback_id=fb['id'])
                ops.delegate_answer(self.conn, self.cfg, ctx, did, body='Markdown')
                ops.feedback_resolve(self.conn, self.cfg, fb['id'], outcome='applied', reply='Answer queued')
                self.drain()
                dg.send_messages(self.conn, self.cfg, kb)
                kb.worker_start(card)
                kb.worker_block(card, 'Question two')
                dg.sync_cards(self.conn, self.cfg, kb)
                self.fake.user_comment(self.page_of(task), 'Old discussion amendment', discussion_id=anchor)
                self.sync.poll_comments()
                late = next(x for x in feedback.pending(self.conn) if x['scope_id'] == task and x['text'] == 'Old discussion amendment')
                self.assertEqual(late['payload']['delivery_id'], q1['id'])
                ctx = ops.context(self.conn, self.cfg, feedback_id=late['id'])
                for approve in (None, ['send_email']):
                    with self.assertRaises(ops.OpError):
                        ops.delegate_answer(self.conn, self.cfg, ctx, did, body='Old amendment', approve=approve)
                self.assertIn(kb.cards[card]['status'], ('blocked', 'triage'))

    def test_b_unread_body_recovers_before_delegation_without_duplicates(self):
        from todo_manager import feedback
        from todo_manager.notion.client import NotionError
        page = self.fake.user_create_page(self.tasks_ds, 'Draft', markdown='Only draft, never send')
        with patch.object(self.client, 'page_markdown', side_effect=NotionError('Synthetic outage', status=503)):
            self.sync.pull('task')
        fb = next(x for x in feedback.pending(self.conn) if x['kind'] == 'new_task')
        ctx = ops.context(self.conn, self.cfg, feedback_id=fb['id'])
        with self.assertRaises(ops.OpError):
            ops.delegate_create(self.conn, self.cfg, ctx, task_id=fb['scope_id'], title='Draft', goal='Draft', submit=False)
        self.sync.pull('task')
        self.assertEqual(feedback.get(self.conn, fb['id'])['status'], 'superseded')
        updated = [f for f in feedback.pending(self.conn) if f['kind'] == 'new_task'][0]
        self.assertIn('never send', updated['payload']['body'])
        clock.advance(minutes=2)
        self.sync.pull('task')
        ctx = ops.context(self.conn, self.cfg, feedback_id=updated['id'])
        first = ops.delegate_create(self.conn, self.cfg, ctx, task_id=fb['scope_id'], title='Draft', goal='Draft', submit=False)
        again = ops.delegate_create(self.conn, self.cfg, ctx, task_id=fb['scope_id'], title='Draft', goal='Draft', submit=False)
        self.assertTrue(again['duplicate'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM delegations').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)

    def test_b_another_feedback_cannot_bypass_unread_creation_context(self):
        from todo_manager import feedback
        from todo_manager.notion.client import NotionError
        page = self.fake.user_create_page(self.tasks_ds, 'Draft', markdown='Never send')
        with patch.object(self.client, 'page_markdown', side_effect=NotionError('Synthetic outage', status=503)):
            self.sync.pull('task')
        task = self.sync.page_row(page)['entity_id']
        fid, _ = feedback.record(self.conn, origin='notion_property', source_id=page, source_version='assigned',
                                 text='Agent', scope_type='task', scope_id=task, kind='delegate_request',
                                 author_is_user=True)
        ctx = ops.context(self.conn, self.cfg, feedback_id=fid)
        with self.assertRaises(ops.OpError):
            ops.delegate_create(self.conn, self.cfg, ctx, task_id=task, title='Draft', goal='Draft', submit=False)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM delegations').fetchone()[0], 0)

    def test_b_empty_body_is_complete_but_api_truncation_retries(self):
        from todo_manager import feedback
        empty = self.fake.user_create_page(self.tasks_ds, 'Empty', markdown='')
        long = self.fake.user_create_page(self.tasks_ds, 'Long', markdown='Only draft, never send')
        self.fake.truncate_markdown_at = 5
        self.sync.pull('task')
        fbs = {x['payload']['page_id']: x for x in feedback.pending(self.conn)}
        self.assertIsNone(ops.incomplete_reason(fbs[empty]))
        self.assertIsNotNone(ops.incomplete_reason(fbs[long]))
        self.fake.truncate_markdown_at = None
        self.sync.pull('task')
        self.assertEqual(feedback.get(self.conn, fbs[long]['id'])['status'], 'superseded')
        current = [f for f in feedback.pending(self.conn) if f['source_id'] == long][0]
        self.assertIsNone(ops.incomplete_reason(current))

    def test_d_dirty_scans_preserve_uncertain_write_and_human_change(self):
        task = self.local_task()
        store.update_task(self.conn, task, {'summary': 'Version one'}, actor='human', origin='cli')
        effects.task_push(self.conn, task)
        self.fake.lose_response.add(('PATCH', '/pages/'))
        self.drain()
        uncertain = self.conn.execute("SELECT * FROM outbox WHERE status='uncertain'").fetchone()
        self.assertIsNotNone(uncertain)
        for _ in range(3):
            self.sync.enqueue_dirty()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outbox WHERE status='uncertain'").fetchone()[0], 1)
        store.update_task(self.conn, task, {'summary': 'Human version two'}, actor='human', origin='cli')
        self.sync.enqueue_dirty()
        clock.advance(minutes=2)
        self.drain()
        self.drain()
        self.assertIn('Human version two', self.fake.markdown[self.page_of(task)])

    def test_r09_project_permanent_error_is_not_reset_by_dirty_scan(self):
        pid = store.create_project(self.conn, 'Project', actor='human', origin='cli')
        effects.project_push(self.conn, pid)
        calls = []
        def reject(item, reconcile):
            calls.append(item['id'])
            raise outbox.Permanent('Synthetic permission denial')
        for _ in range(3):
            self.sync.enqueue_dirty()
            outbox.process(self.conn, {('notion', 'push_project'): reject})
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outbox WHERE status='dead'").fetchone()[0], 1)

    def test_r12_same_discussion_equal_text_distinct_effects_and_lost_response(self):
        task = self.local_task()
        page = self.page_of(task)
        discussion = self.client.create_comment(markdown='Question', page_id=page)['discussion_id']
        effects.task_comment(self.conn, task, 'first', 'Answer queued', discussion)
        self.drain()
        effects.task_comment(self.conn, task, 'second', 'Answer queued', discussion)
        self.fake.lose_response.add(('POST', '/comments'))
        self.drain()
        clock.advance(minutes=2)
        self.drain()
        self.assertEqual(len(self.fake.comments), 3)
        results = [json.loads(r['result'])['comment_id'] for r in self.conn.execute("SELECT result FROM outbox WHERE op='comment' AND status='done'")]
        self.assertEqual(len(set(results)), 2)

    def test_e_project_outage_duplicate_and_relation_recovery_are_independent(self):
        from todo_manager.notion.client import NotionError
        pid = store.create_project(self.conn, 'Existing project', actor='human', origin='cli')
        effects.project_push(self.conn, pid)
        self.drain()
        linked = store.get_project(self.conn, pid)['notion_page_id']
        duplicate = self.fake.user_create_page(self.projects_ds, 'Copy', **{'TM ID': pid})
        target = self.fake.user_create_page(self.projects_ds, 'New project')
        task = self.local_task()
        self.fake.user_edit(self.page_of(task), **{'项目': [target]})
        original = self.client.query_data_source
        def unavailable(ds, **kw):
            if ds == self.projects_ds:
                raise NotionError('Synthetic project outage', status=503)
            return original(ds, **kw)
        with patch.object(self.client, 'query_data_source', unavailable):
            with self.assertRaises(NotionError):
                self.sync.pull('project')
            self.sync.pull('task')
        self.sync.enqueue_dirty()
        self.drain()
        self.assertEqual(self.fake.prop(self.page_of(task), '项目'), [target])
        self.conn.close()
        self.conn = db.connect(self.cfg.db_path)
        self.sync = self.new_sync()
        self.sync.pull('project')
        self.sync.pull('task')
        self.assertEqual(store.get_project(self.conn, pid)['notion_page_id'], linked)
        self.assertIsNone(self.sync.page_row(duplicate))
        resolved = store.get_task(self.conn, task)['project_id']
        self.assertEqual(store.get_project(self.conn, resolved)['notion_page_id'], target)
        self.fake.user_edit(self.page_of(task), **{'项目': []})
        self.sync.pull('task')
        self.assertIsNone(store.get_task(self.conn, task)['project_id'])

    def test_r11_later_local_project_choice_wins_over_unresolved_relation(self):
        task = self.local_task()
        target = self.fake.user_create_page(self.projects_ds, 'Remote')
        self.fake.user_edit(self.page_of(task), **{'项目': [target]})
        self.sync.pull('task')
        local = store.create_project(self.conn, 'Local choice', actor='human', origin='cli')
        store.update_task(self.conn, task, {'project_id': local}, actor='human', origin='cli')
        self.sync.pull('project')
        self.assertEqual(store.get_task(self.conn, task)['project_id'], local)

    def test_r12_fallback_target_is_durable_when_response_is_lost(self):
        from todo_manager.notion.client import NotionError, NotionUncertain
        task = self.local_task()
        effects.task_comment(self.conn, task, 'fallback', 'Answer queued', 'deleted-discussion')
        original = self.client.create_comment
        def fail_then_lose(**kw):
            if kw.get('discussion_id'):
                raise NotionError('Synthetic deleted discussion', status=404)
            original(**kw)
            raise NotionUncertain('Synthetic lost fallback response')
        with patch.object(self.client, 'create_comment', fail_then_lose):
            self.drain()
        row = self.conn.execute("SELECT status,payload FROM outbox WHERE op='comment'").fetchone()
        self.assertEqual(row['status'], 'uncertain')
        self.assertTrue(json.loads(row['payload'])['fallback_page'])
        self.conn.close()
        self.conn = db.connect(self.cfg.db_path)
        clock.advance(minutes=2)
        self.drain()
        self.assertEqual(len(self.fake.comments), 1)
        self.assertEqual(self.conn.execute("SELECT status FROM outbox WHERE op='comment'").fetchone()[0], 'done')

    def test_r13_notion_repo_edit_has_history_and_blocks_stale_agent(self):
        pid = store.create_project(self.conn, 'Project', actor='agent', origin='cli')
        effects.project_push(self.conn, pid)
        self.drain()
        page = store.get_project(self.conn, pid)['notion_page_id']
        self.fake.user_edit(page, **{'仓库': 'owner/repo'})
        self.sync.pull('project')
        result = store.update_project(self.conn, pid, {}, actor='agent', origin='intake', based_on_version=1,
                                      repos_remove=['owner/repo'])
        self.assertIn('repos', result.rejected)
        history = [json.loads(r['changes']) for r in self.conn.execute("SELECT changes FROM history WHERE entity_id=?", (pid,))]
        self.assertTrue(any('repos' in entry for entry in history))

    def test_r09_new_projection_keeps_server_delay_after_restart(self):
        task = self.local_task()
        store.update_task(self.conn, task, {'summary': 'First'}, actor='human', origin='cli')
        calls = []
        def limited(item, reconcile):
            calls.append(item['id'])
            raise outbox.Retryable('Synthetic rate limit', retry_after=3600)
        self.sync.enqueue_dirty()
        outbox.process(self.conn, {('notion', 'push_task'): limited})
        clock.advance(minutes=5)
        store.update_task(self.conn, task, {'summary': 'Second'}, actor='human', origin='cli')
        self.sync.enqueue_dirty()
        self.conn.close()
        self.conn = db.connect(self.cfg.db_path)
        self.sync = self.new_sync()
        self.sync.enqueue_dirty()
        outbox.process(self.conn, {('notion', 'push_task'): limited})
        self.assertEqual(len(calls), 1)
        clock.advance(minutes=55)
        outbox.process(self.conn, {('notion', 'push_task'): limited})
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1])


class SourceAndStoreAudit(WorkspaceCase):
    def test_r04_parent_keeps_identity_when_promoted_to_thread(self):
        parent = Item(source='slack', account='T', external_id='C:100', conversation_key='slack:C',
                      thread_key='slack:C', occurred_at=clock.iso(), body='Prepare camera-ready PDF')
        persist_fetch(self.conn, 'slack', FetchResult(scope='C', items=[parent], cursor='1'))
        old = dict(self.conn.execute('SELECT * FROM source_items').fetchone())
        self.conn.execute("UPDATE source_items SET state='processed' WHERE id=?", (old['id'],))
        parent.version = 'edited-version'
        parent.conversation_key = parent.thread_key = 'slack:C:100'
        parent.route = 'context'
        reply = Item(source='slack', account='T', external_id='C:200', conversation_key='slack:C:100',
                     thread_key='slack:C:100', occurred_at=clock.iso(), body='I will do it')
        persist_fetch(self.conn, 'slack', FetchResult(scope='C', items=[reply, parent], cursor='2'))
        root = conversation_root(self.conn, 'slack', 'slack:C:100')
        self.assertEqual(root['id'], old['id'])

    def test_r13_repo_edits_are_versioned_and_human_protected(self):
        pid = store.create_project(self.conn, 'Project', actor='agent', origin='cli')
        result = store.update_project(self.conn, pid, {}, actor='human', origin='cli', repos_add=['owner/repo'])
        self.assertEqual(result.version, 2)
        result = store.update_project(self.conn, pid, {}, actor='agent', origin='intake', based_on_version=1,
                                      repos_remove=['owner/repo'])
        self.assertIn('repos', result.rejected)
        self.assertEqual(store.get_project(self.conn, pid)['repos'], ['owner/repo'])

    def test_r15_history_honors_server_query(self):
        http = FakeHttp([
            ('/history', {'historyId': '2', 'history': [{'messagesAdded': [{'message': {'id': 'ad', 'threadId': 'ad'}}]}]}),
            ('/messages', {'messages': []}),
            ('/threads/ad', {'messages': [gmail_message('ad', 'ad', sender='advertiser@outside.org')]}),
        ])
        source = GmailSource(account='test', address='me@example.edu', token=lambda: 'fake', my_addresses=[],
                             query='from:trusted@example.edu', http=http)
        result = source.fetch(json.dumps({'history_id': '1', 'since': clock.iso()}), clock.iso())
        self.assertFalse(result.items)
        self.assertTrue(any('from:trusted@example.edu' in c[2].get('q', '') for c in http.calls))

    def test_r16_partial_digest_does_not_consume_daily_completion(self):
        self.cfg.data['commits']['digest_time'] = '00:00'
        class Repo:
            name = 'commit'
            def __init__(self, scope): self.scope = scope
        adapters = [Repo('first'), Repo('second')]
        with patch('todo_manager.gate_intake.fetch_all', return_value={'commit:first': {'inserted': 0},
                                                                      'commit:second': {'skipped': 'time budget'}}):
            manifest, _ = gate_intake.commit_digest_check(self.conn, self.cfg, adapters)
        self.assertIsNone(manifest)
        row = self.conn.execute("SELECT state FROM daily_jobs WHERE job='commit_digest'").fetchone()
        self.assertTrue(row is None or row[0] not in ('done', 'skipped', 'exhausted'))

    def test_h_digest_finishes_remaining_repos_next_tick_without_model_attempt(self):
        self.cfg.data['commits']['digest_time'] = '00:00'
        calls = []
        class Repo:
            name = 'commit'
            def __init__(self, scope): self.scope = scope
            def fetch(self, cursor, last_success_at):
                calls.append(self.scope)
                return FetchResult(scope=self.scope, items=[], cursor='complete')
        repos = [Repo('first'), Repo('second')]
        with patch('todo_manager.gate_intake.time.monotonic', side_effect=[0, 0, 121]):
            manifest, report = gate_intake.commit_digest_check(self.conn, self.cfg, repos)
        self.assertIsNone(manifest)
        self.assertEqual(calls, ['first'])
        self.conn.close()
        self.conn = db.connect(self.cfg.db_path)
        gate_intake.commit_digest_check(self.conn, self.cfg, repos)
        self.assertEqual(calls, ['first', 'second'])
        row = self.conn.execute("SELECT state,attempts FROM daily_jobs WHERE job='commit_digest'").fetchone()
        self.assertEqual(tuple(row), ('skipped', 0))

    def test_r15_query_selection_is_paginated_and_backlog_hydrates_full_thread(self):
        calls = []
        def listing(params):
            calls.append(params['q'])
            return ({'messages': [{'threadId': 'selected'}], 'nextPageToken': 'p2'} if not params.get('pageToken')
                    else {'messages': [{'threadId': 'other'}]})
        http = FakeHttp([
            ('/history', {'historyId': '2', 'history': [{'messagesAdded': [
                {'message': {'id': 'one', 'threadId': 'selected'}},
                {'message': {'id': 'two', 'threadId': 'other'}},
                {'message': {'id': 'three', 'threadId': 'excluded'}}]}]}),
            ('/messages', listing),
            ('/threads/selected', {'messages': [gmail_message('one', 'selected', sender='trusted@example.edu'),
                                                gmail_message('reply', 'selected', sender='other@example.org')]}),
            ('/threads/other', {'messages': [gmail_message('two', 'other', sender='trusted@example.edu')]}),
            ('/threads/excluded', {'messages': [gmail_message('three', 'excluded')]}),
        ])
        source = GmailSource(account='test', address='me@example.edu', token=lambda: 'fake', my_addresses=[],
                             query='from:trusted@example.edu', http=http, max_threads_per_run=1)
        first = source.fetch(json.dumps({'history_id': '1'}))
        self.assertFalse(first.complete)
        self.assertEqual(len(first.items), 2, 'a nonmatching reply remains full thread context')
        second = source.fetch(first.cursor)
        self.assertEqual(len(second.items), 1)
        third = source.fetch(second.cursor)
        self.assertTrue(third.complete)
        self.assertFalse(third.items)
        self.assertEqual(sum('rfc822msgid:one@x.org' in q for q in calls), 1,
                         'selected thread is not searched again while backlog continues')
        self.assertTrue(all('rfc822msgid:' in q for q in calls))

    def test_r04_slack_old_parent_promotion_preserves_processed_batch_and_evidence(self):
        from todo_manager.sources.slack import SlackSource
        parent_ts = str(clock.now().timestamp() - 86400)
        parent = {'ts': parent_ts, 'text': 'Prepare camera-ready PDF'}
        phase = [0]
        def history(params):
            if phase[0] == 0:
                return {'ok': True, 'messages': [parent]}
            found = [{**parent, 'reply_count': 1, 'thread_ts': parent_ts,
                      'latest_reply': str(clock.now().timestamp())}]
            return {'ok': True, 'messages': found if float(params['oldest']) <= float(parent_ts) else []}
        http = FakeHttp([('conversations.history', history),
                         ('conversations.replies', lambda p: {'ok': True, 'messages': [parent,
                          {'ts': str(clock.now().timestamp()), 'thread_ts': parent_ts, 'text': 'I will do it'}]})])
        source = SlackSource(token=lambda: 'fake', team='T', conversation='C', my_user_ids=[], http=http)
        first = source.fetch(None)
        persist_fetch(self.conn, 'slack', first)
        original = dict(self.conn.execute('SELECT * FROM source_items').fetchone())
        task = store.create_task(self.conn, 'PDF', actor='human', origin='cli')
        store.link_evidence(self.conn, 'task', task, original['id'], 'origin')
        self.conn.execute("UPDATE source_items SET state='processed' WHERE id=?", (original['id'],))
        phase[0] = 1
        clock.advance(days=2)
        second = source.fetch(first.cursor)
        persist_fetch(self.conn, 'slack', second)
        root = conversation_root(self.conn, 'slack', 'slack:C:' + parent_ts)
        self.assertEqual(root['id'], original['id'])
        self.assertEqual(root['batch_id'], original['batch_id'])
        self.assertEqual(root['state'], 'processed')
        self.assertEqual(len(store.evidence_for(self.conn, 'task', task)), 1)


class OwnershipAudit(install_cases.InstallCase):
    def test_r14_other_workspace_never_adopts_or_removes_owned_resources(self):
        self.install()
        before = self.paths.jobs_file.read_text()
        other = self.tmp / 'other-workspace'
        other.mkdir()
        cfg = config.load(other, overrides={'hermes': {'home': str(self.hermes_home), 'launcher': str(self.paths.launcher)}})
        paths = hermes_install.Paths.from_config(cfg)
        installer = hermes_install.Installer(cfg, paths, runner=self.hermes)
        installer.apply(installer.plan())
        removals = installer.uninstall_plan()
        self.assertFalse([a for a in removals if a.changes], [a.summary for a in removals])
        installer.uninstall(removals)
        self.assertEqual(before, self.paths.jobs_file.read_text())
        self.assertTrue(self.paths.install_skill_path.exists())
