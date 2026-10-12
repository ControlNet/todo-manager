"""Regression cases use synthetic cards, profiles and Notion effects; no live services."""
from unittest.mock import patch
from tests.helpers import WorkspaceCase
from todo_manager import delegation as dg, ops, store, export, db
from todo_manager.kanban import FakeKanban, KanbanError


class ReviewReliability(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.cfg.data['kanban']['assignee'] = 'default'
        self.kb = FakeKanban()
        self.task = store.create_task(self.conn, 'Synthetic task', origin='cli', actor='human')
        self.did = dg.create(self.conn, self.cfg, task_id=self.task, title='Draft', goal='Draft only')

    def d(self):
        return dg.get(self.conn, self.did)

    def submit(self):
        dg.submit_pending(self.conn, self.cfg, self.kb)
        return self.d()['card_id']

    def sync(self):
        return dg.sync_cards(self.conn, self.cfg, self.kb)

    def child(self):
        cid = self.kb.create(title='Child', body='Synthetic', idem_key=str(len(self.kb.cards)), assignee='default', tenant='test', max_retries=1)
        self.kb.ownership[cid] = self.d()['card_id']
        return cid

    def question(self, card):
        self.kb.worker_start(card)
        self.kb.worker_block(card, 'Which format?')
        self.sync()
        return next(x for x in reversed(dg.deliveries(self.conn, self.did)) if x['card_id'] == card)

    def restart(self):
        self.conn.close()
        self.conn = db.connect(self.cfg.db_path)

    def test_hold_cannot_release_staged_card_after_pause_resume(self):
        self.cfg.data['kanban'].update(notify_platform='matrix', notify_chat_id='synthetic', notify_profile='default')
        self.conn.execute('UPDATE delegations SET notification_route=NULL WHERE id=?', (self.did,))
        self.kb.fail_next['notify'] = KanbanError('synthetic unavailable')
        self.submit()
        dg.add_message(self.conn, self.did, 'pause', 'Wait')
        card = self.submit()
        dg.add_message(self.conn, self.did, 'resume', 'Continue')
        export.reconcile_after_restore(self.conn, 'test restore')
        self.restart()
        for _ in range(2):
            dg.send_messages(self.conn, self.cfg, self.kb)
            self.sync()
            self.assertEqual(self.kb.cards[card]['status'], 'blocked')
            self.assertEqual(self.kb.dispatch_tick(), [])
        dg.release_hold(self.conn)
        dg.send_messages(self.conn, self.cfg, self.kb)
        self.sync()
        self.assertEqual(len(self.kb.cards), 1)
        self.assertEqual(self.kb.cards[card]['status'], 'ready')

    def test_explicit_old_root_question_cannot_answer_new_question_or_approve(self):
        card = self.submit()
        old = self.question(card)
        self.kb.unblock(card)
        self.question(card)
        before = self.kb.cards[card]['status']
        ctx = ops.context(self.conn, self.cfg, request='old-answer', said='Use Markdown')
        with self.assertRaises(ops.OpError):
            ops.delegate_answer(self.conn, self.cfg, ctx, self.did, body='Markdown', question=old['id'], approve=['send_email'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM approvals').fetchone()[0], 0)
        dg.send_messages(self.conn, self.cfg, self.kb)
        self.assertEqual(self.kb.cards[card]['status'], before)
        self.assertEqual(self.kb.comments, [])

    def test_done_root_retries_first_child_discovery_after_restart(self):
        card = self.submit()
        child = self.child()
        self.kb.worker_start(child)
        self.kb.worker_block(child, 'Child question')
        self.kb.worker_start(card)
        self.kb.worker_complete(card, 'Root result')
        with patch.object(self.kb, 'owned', side_effect=KanbanError('synthetic temporary read failure')):
            self.sync()
        self.assertEqual(self.d()['status'], 'ended')
        self.restart()
        self.assertEqual(self.sync()['checked'], 1)
        self.assertEqual(len([x for x in dg.deliveries(self.conn, self.did) if x['card_id'] == child]), 1)
        self.sync()
        self.assertEqual(len([x for x in dg.deliveries(self.conn, self.did) if x['card_id'] == child]), 1)

    def test_notify_failure_does_not_hide_root_or_child_results(self):
        card = self.submit()
        child = self.child()
        self.cfg.data['kanban'].update(notify_platform='matrix', notify_chat_id='synthetic', notify_profile='default')
        self.conn.execute('UPDATE delegations SET notification_route=NULL WHERE id=?', (self.did,))
        self.kb.worker_start(card)
        self.kb.worker_complete(card, 'Root result')
        self.kb.worker_start(child)
        self.kb.worker_block(child, 'Child question')
        with patch.object(self.kb, 'notify', side_effect=KanbanError('synthetic notification failure')):
            self.sync()
        self.assertEqual(self.d()['status'], 'ended')
        self.assertEqual(len(dg.deliveries(self.conn, self.did)), 2)

    def test_missing_profile_repaired_only_after_remote_lookup(self):
        self.conn.execute('UPDATE delegations SET assignee=NULL WHERE id=?', (self.did,))
        self.cfg.data['kanban']['assignee'] = ''
        self.submit()
        self.assertIsNone(self.d()['card_id'])
        self.cfg.data['kanban']['assignee'] = 'default'
        self.submit()
        self.assertEqual(self.d()['assignee'], 'default')
        self.assertEqual(len(self.kb.cards), 1)

    def test_child_question_surfaces_in_labels_and_views(self):
        self.submit()
        child = self.child()
        self.question(child)
        self.assertEqual(dg.task_label(self.conn, self.task), 'blocked')
        views = export.view_rows(self.conn, self.cfg)
        self.assertIn(self.task, [x['id'] for x in views['agent']])
        self.assertEqual(dg.task_progress(self.conn, self.task), 'answer', 'Needs my answer on the Agent board')
        self.assertEqual(self.d()['status'], 'queued')

    def answer(self, question, request='answer', approve=None):
        ctx = ops.context(self.conn, self.cfg, request=request, said='Use Markdown')
        return ops.delegate_answer(self.conn, self.cfg, ctx, self.did, body='Markdown',
                                   question=question['id'], approve=approve)

    def test_root_and_child_answer_retries_after_comment_and_unblock_loss(self):
        root = self.submit()
        for child in (False, True):
            for failure in ('comment', 'unblock'):
                with self.subTest(child=child, failure=failure):
                    card = self.child() if child else root
                    question = self.question(card)
                    result = self.answer(question, f'{child}-{failure}', None if child else ['send_email'])
                    self.kb.lose_response.add(failure)
                    dg.send_messages(self.conn, self.cfg, self.kb)
                    self.restart()
                    dg.send_messages(self.conn, self.cfg, self.kb)
                    self.assertEqual(self.kb.cards[card]['status'], 'ready')
                    self.assertEqual(sum(result['message'] in x['body'] for x in self.kb.comments), 1)
                    again = self.answer(question, f'{child}-{failure}', None if child else ['send_email'])
                    self.assertEqual(again['message'], result['message'])
                    self.sync()

    def test_old_child_question_and_cross_delegation_are_rejected(self):
        self.submit()
        child = self.child()
        old = self.question(child)
        self.kb.unblock(child)
        current = self.question(child)
        with self.assertRaises(ops.OpError):
            self.answer(old)
        with self.assertRaises(ops.OpError):
            self.answer(current, approve=['send_email'])
        other = store.create_task(self.conn, 'Other', origin='cli', actor='human')
        other_d = dg.create(self.conn, self.cfg, task_id=other, title='Other', goal='Other')
        ctx = ops.context(self.conn, self.cfg, request='cross', said='Markdown')
        with self.assertRaises(ops.OpError):
            ops.delegate_answer(self.conn, self.cfg, ctx, other_d, body='Markdown', question=current['id'])
        self.assertFalse(self.kb.comments)

    def test_same_text_targets_distinct_root_questions(self):
        root = self.submit()
        first = self.question(root)
        a = self.answer(first, 'same-request')
        dg.send_messages(self.conn, self.cfg, self.kb)
        second = self.question(root)
        b = self.answer(second, 'same-request')
        self.assertNotEqual(a['message'], b['message'])
        dg.send_messages(self.conn, self.cfg, self.kb)
        self.assertEqual(self.kb.cards[root]['status'], 'ready')

    def test_implicit_root_answer_and_stale_notion_discussion(self):
        root = self.submit()
        old = self.question(root)
        ctx = ops.context(self.conn, self.cfg, request='implicit', said='Markdown')
        result = ops.delegate_answer(self.conn, self.cfg, ctx, self.did, body='Markdown')
        self.assertEqual(result['question'], old['id'])
        dg.send_messages(self.conn, self.cfg, self.kb)
        self.question(root)
        ctx = ops.context(self.conn, self.cfg, request='discussion', said='Markdown')
        # Synthetic authenticated discussion payload, same shape as Notion importer.
        ctx.fb = {'id': 'synthetic', 'payload': {'delivery_id': old['id']}}
        with self.assertRaises(ops.OpError):
            ops.delegate_answer(self.conn, self.cfg, ctx, self.did, body='Markdown')

    def test_partial_child_read_failure_keeps_retry_and_other_child_progress(self):
        root = self.submit()
        first, second = self.child(), self.child()
        for card in (first, second):
            self.kb.worker_start(card)
            self.kb.worker_block(card, 'Question')
        self.kb.worker_start(root)
        self.kb.worker_complete(root, 'Root done')
        read = self.kb.snapshot
        def fail_first(card, **kwargs):
            if card == first:
                raise KanbanError('synthetic failure')
            return read(card, **kwargs)
        with patch.object(self.kb, 'snapshot', side_effect=fail_first):
            self.sync()
        self.assertTrue(self.d()['children_sync_pending'])
        self.assertEqual(len([x for x in dg.deliveries(self.conn, self.did) if x['card_id'] == second]), 1)
        self.restart()
        self.sync()
        self.assertFalse(self.d()['children_sync_pending'])
        questions = [x for x in dg.deliveries(self.conn, self.did) if x['kind'] == 'question']
        self.assertEqual(len(questions), 2)
        with self.assertRaises(ops.OpError):
            self.answer(questions[0])  # Data recovery never reopens an ended delegation.

    def test_existing_finished_child_does_not_hide_new_child_when_discovery_fails(self):
        root = self.submit()
        first = self.child()
        self.kb.worker_start(first)
        self.kb.worker_complete(first, 'Partial')
        self.sync()
        second = self.child()
        self.kb.worker_start(second)
        self.kb.worker_block(second, 'New child question')
        self.kb.worker_start(root)
        self.kb.worker_complete(root, 'Root done')
        with patch.object(self.kb, 'owned', side_effect=KanbanError('synthetic failure')):
            self.sync()
        self.restart()
        self.sync()
        self.assertTrue(any(x['card_id'] == second for x in dg.deliveries(self.conn, self.did)))
        self.kb.complete(second, 'Resolved')
        self.sync()
        self.assertEqual(self.sync()['checked'], 0, 'settled history is not scanned forever')

    def test_notify_failure_does_not_duplicate_business_outbox(self):
        root = self.submit()
        first, second = self.child(), self.child()
        self.cfg.data['kanban'].update(notify_platform='matrix', notify_chat_id='synthetic', notify_profile='default')
        self.conn.execute('UPDATE delegations SET notification_route=NULL WHERE id=?', (self.did,))
        for card in (root, first, second):
            self.kb.worker_start(card)
            self.kb.worker_block(card, 'Question')
        with patch.object(self.kb, 'notify', side_effect=KanbanError('synthetic failure')):
            self.sync()
        before = self.conn.execute("SELECT COUNT(*) FROM outbox WHERE op='comment'").fetchone()[0]
        self.assertEqual(len(dg.deliveries(self.conn, self.did)), 3)
        failures = self.conn.execute("SELECT scope FROM cursors WHERE source='kanban' AND consecutive_failures>0").fetchall()
        self.assertEqual(len(failures), 3)
        self.sync()
        self.sync()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outbox WHERE op='comment'").fetchone()[0], before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM cursors WHERE source='kanban' AND consecutive_failures>0").fetchone()[0], 0)

    def test_profile_repair_never_reassigns_uncertain_remote_create(self):
        self.kb.lose_response.add('create')
        self.submit()
        self.cfg.data['kanban']['assignee'] = 'different'
        self.kb.valid_profiles = {'different'}
        self.submit()
        self.assertEqual(len(self.kb.cards), 1)
        self.assertEqual(self.d()['assignee'], 'default')
        self.assertEqual(self.kb.cards[self.d()['card_id']]['assignee'], 'default')

    def test_invalid_profile_and_failed_lookup_do_not_create_or_repair(self):
        self.cfg.data['kanban']['assignee'] = 'missing'
        self.conn.execute("UPDATE delegations SET assignee='missing' WHERE id=?", (self.did,))
        self.submit()
        self.assertFalse(self.kb.cards)
        self.cfg.data['kanban']['assignee'] = 'default'
        self.kb.fail_next['find'] = KanbanError('synthetic read failure')
        self.submit()
        self.assertEqual(self.d()['assignee'], 'missing')
        self.submit()
        self.assertEqual(self.d()['assignee'], 'default')
        self.assertEqual(len(self.kb.cards), 1)

    def test_multiple_questions_clear_individually_and_preserve_human_next_actor(self):
        self.submit()
        first, second = self.child(), self.child()
        q1, q2 = self.question(first), self.question(second)
        self.answer(q1, 'first')
        dg.send_messages(self.conn, self.cfg, self.kb)
        self.assertEqual(dg.task_label(self.conn, self.task), 'blocked')
        self.assertEqual(store.get_task(self.conn, self.task)['next_actor'], 'me')
        store.update_task(self.conn, self.task, {'next_actor': 'agent'}, actor='human', origin='cli')
        store.update_task(self.conn, self.task, {'next_actor': 'me'}, actor='human', origin='cli')
        self.answer(q2, 'second')
        dg.send_messages(self.conn, self.cfg, self.kb)
        self.sync()
        self.assertEqual(dg.task_label(self.conn, self.task), 'queued')
        self.assertEqual(store.get_task(self.conn, self.task)['next_actor'], 'me')
        self.assertEqual(dg.task_progress(self.conn, self.task), 'working')

    def test_dependency_wait_is_not_question_but_failure_needs_attention(self):
        self.submit()
        child = self.child()
        self.kb.worker_start(child)
        self.kb.worker_block(child, 'Wait for sibling', kind='dependency')
        self.sync()
        self.assertEqual(dg.task_label(self.conn, self.task), 'queued')
        self.assertFalse(dg.deliveries(self.conn, self.did))
        self.kb.unblock(child)
        self.kb.worker_start(child)
        self.kb.worker_crash(child)
        self.sync()
        row = self.conn.execute('SELECT attention FROM delegation_children WHERE card_id=?', (child,)).fetchone()
        self.assertEqual(row['attention'], 'failure')
        self.assertEqual(dg.task_label(self.conn, self.task), 'blocked')

    def test_hold_with_cancel_never_releases_before_archive(self):
        self.cfg.data['kanban'].update(notify_platform='matrix', notify_chat_id='synthetic', notify_profile='default')
        self.conn.execute('UPDATE delegations SET notification_route=NULL WHERE id=?', (self.did,))
        self.kb.fail_next['notify'] = KanbanError('synthetic registration failure')
        self.submit()
        dg.add_message(self.conn, self.did, 'pause', 'Wait')
        card = self.submit()
        dg.add_message(self.conn, self.did, 'cancel', 'Stop')
        dg.hold(self.conn, 'synthetic restore')
        self.restart()
        self.sync()
        self.assertEqual(self.kb.cards[card]['status'], 'blocked')
        dg.release_hold(self.conn)
        self.sync()
        self.assertEqual(self.kb.cards[card]['status'], 'blocked', 'pending cancel bars notification release')
        dg.send_messages(self.conn, self.cfg, self.kb)
        self.assertEqual(self.kb.cards[card]['status'], 'archived')
        self.assertEqual(len(self.kb.cards), 1)

    def test_health_reports_missing_invalid_and_valid_profiles(self):
        from todo_manager import health
        self.cfg.data['kanban']['enabled'] = True
        with patch('todo_manager.kanban.BridgeKanban.from_config', return_value=self.kb):
            for profile in ('', 'missing', 'default'):
                self.cfg.data['kanban']['assignee'] = profile
                problems = health.report(self.conn, self.cfg)['problems']
                self.assertEqual(any('kanban.assignee' in p for p in problems), profile != 'default')

    def test_child_answer_lost_response_does_not_clear_newer_question(self):
        self.submit()
        child = self.child()
        old = self.question(child)
        result = self.answer(old)
        self.kb.lose_response.add('unblock')
        dg.send_messages(self.conn, self.cfg, self.kb)
        new = self.question(child)
        dg.send_messages(self.conn, self.cfg, self.kb)
        self.assertIn(self.kb.cards[child]['status'], ('blocked', 'triage'))
        self.assertEqual(dg.task_label(self.conn, self.task), 'blocked')
        self.assertEqual(sum(result['message'] in c['body'] for c in self.kb.comments), 1)
        self.assertNotEqual(old['id'], new['id'])

    def test_notion_and_export_put_a_child_question_on_the_agent_board_only(self):
        from todo_manager.notion import setup, schema
        self.submit()
        self.question(self.child())
        p = {x.field: x.name('en') for x in schema.TASK_PROPS}
        q = {x.field: x.name('en') for x in schema.PROJECT_PROPS}
        spec = next(x for x in setup.view_specs(p, q, 'en') if x['key'] == 'agent')
        self.assertEqual(spec['configuration']['group_by']['property_id'], p['agent_progress'])
        self.assertEqual(dg.task_progress(self.conn, self.task), 'answer')
        rows = export.view_rows(self.conn, self.cfg)
        self.assertIn(self.task, [x['id'] for x in rows['agent']])
        self.assertNotIn(self.task, [x['id'] for x in rows['focus']])

    def test_pending_child_answer_does_not_change_other_question_version(self):
        self.submit()
        first, second = self.child(), self.child()
        q1 = self.question(first)
        self.answer(q1, 'first-pending')
        q2 = self.question(second)
        self.assertEqual(q2['requirement_version'], self.d()['requirement_version'])
        self.answer(q2, 'second-pending')
        dg.send_messages(self.conn, self.cfg, self.kb)
        self.assertEqual(self.kb.cards[first]['status'], 'ready')
        self.assertEqual(self.kb.cards[second]['status'], 'ready')

    def test_explicit_operation_key_is_scoped_to_question_and_reports_live_status(self):
        root = self.submit()
        first = self.question(root)
        ctx = ops.context(self.conn, self.cfg, request='keyed', said='Markdown', key='explicit-answer')
        a = ops.delegate_answer(self.conn, self.cfg, ctx, self.did, question=first['id'], body='Markdown')
        self.assertEqual(a['status'], 'pending')
        dg.send_messages(self.conn, self.cfg, self.kb)
        repeated = ops.delegate_answer(self.conn, self.cfg, ctx, self.did, question=first['id'], body='Markdown')
        self.assertEqual(repeated['status'], 'sent')
        second = self.question(root)
        b = ops.delegate_answer(self.conn, self.cfg, ctx, self.did, question=second['id'], body='Markdown')
        self.assertNotEqual(a['message'], b['message'])

    def test_uncertain_create_can_link_completed_card_while_notification_fails(self):
        self.cfg.data['kanban'].update(notify_platform='matrix', notify_chat_id='synthetic', notify_profile='default')
        self.conn.execute('UPDATE delegations SET notification_route=NULL WHERE id=?', (self.did,))
        self.kb.lose_response.add('create')
        self.submit()
        card = next(iter(self.kb.cards))
        self.kb.worker_start(card)
        self.kb.worker_complete(card, 'Completed before relink')
        with patch.object(self.kb, 'notify', side_effect=KanbanError('synthetic notification failure')):
            self.submit()
            self.sync()
        self.assertEqual(self.d()['card_id'], card)
        self.assertTrue(any(x['kind'] == 'result' for x in dg.deliveries(self.conn, self.did)))
        self.assertEqual(len(self.kb.cards), 1)

    def test_invalid_legacy_notification_route_does_not_hide_completion(self):
        root = self.submit()
        self.conn.execute('UPDATE delegations SET notification_route=NULL WHERE id=?', (self.did,))
        self.cfg.data['kanban']['notify_platform'] = 'matrix'
        self.kb.worker_start(root)
        self.kb.worker_complete(root, 'Complete')
        self.sync()
        self.assertEqual(self.d()['status'], 'ended')
        self.assertEqual(len(dg.deliveries(self.conn, self.did)), 1)

    def test_repeated_child_sync_does_not_repeat_notion_business_effects(self):
        self.submit()
        self.question(self.child())
        before = self.conn.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]
        for _ in range(3):
            self.sync()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], before)

    def test_child_attention_never_reopens_human_closed_task(self):
        self.submit()
        child = self.child()
        store.update_task(self.conn, self.task, {'status': 'done'}, actor='human', origin='cli')
        self.question(child)
        self.assertEqual(store.get_task(self.conn, self.task)['status'], 'done')
        self.assertIsNone(dg.task_label(self.conn, self.task))
        self.assertNotIn(self.task, [x['id'] for x in export.view_rows(self.conn, self.cfg)['agent']])

    def test_hold_arriving_during_preflight_blocks_initial_create(self):
        original = self.kb.profile_valid
        def hold_during_validation(name):
            dg.hold(self.conn, 'synthetic concurrent restore')
            return original(name)
        with patch.object(self.kb, 'profile_valid', side_effect=hold_during_validation):
            self.submit()
        self.assertFalse(self.kb.cards)
        dg.release_hold(self.conn)
        self.submit()
        self.assertEqual(len(self.kb.cards), 1)

    def test_answer_does_not_hide_another_delegations_child_question(self):
        self.submit()
        first = self.question(self.child())
        other = dg.create(self.conn, self.cfg, task_id=self.task, title='Parallel', goal='Separate scope', parallel=True)
        dg.submit_pending(self.conn, self.cfg, self.kb)
        child = self.kb.create(title='Other child', body='Synthetic', idem_key='parallel-child', assignee='default', tenant='test')
        self.kb.ownership[child] = dg.get(self.conn, other)['card_id']
        self.kb.worker_start(child)
        self.kb.worker_block(child, 'Another question')
        self.sync()
        self.answer(first)
        dg.send_messages(self.conn, self.cfg, self.kb)
        self.assertEqual(store.get_task(self.conn, self.task)['next_actor'], 'me')
        self.assertEqual(dg.task_label(self.conn, self.task), 'blocked')
