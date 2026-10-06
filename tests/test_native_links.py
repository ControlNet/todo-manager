"""Synthetic cards and questions; no live Notion or channel clients."""
from todo_manager import child_links, delegation, ops, store
from todo_manager.kanban import FakeKanban, KanbanError
from tests.helpers import WorkspaceCase


class NativeLinksTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.kb = FakeKanban()
        self.task = store.create_task(self.conn, 'Synthetic task', origin='cli', actor='human')
        self.did = delegation.create(self.conn, self.cfg, task_id=self.task, title='Draft', goal='Draft only')

    def d(self):
        return delegation.get(self.conn, self.did)

    def submit(self):
        return delegation.submit_pending(self.conn, self.cfg, self.kb)

    def child(self):
        cid = self.kb.create(title='Child', body='Synthetic child', idem_key='child', assignee='default', tenant='test')
        self.kb.ownership[cid] = self.d()['card_id']
        return cid

    def test_subscription_failure_recovers_without_duplicate_or_early_dispatch(self):
        self.cfg.data['kanban'].update(notify_platform='matrix', notify_chat_id='synthetic-room', notify_profile='default')
        self.conn.execute('UPDATE delegations SET notification_route=NULL WHERE id=?', (self.did,))
        self.kb.fail_next['notify'] = KanbanError('Synthetic registration failure')
        self.submit()
        self.assertEqual(self.kb.dispatch_tick(), [])
        self.assertEqual(len(self.kb.cards), 1)
        self.submit()
        self.assertEqual(len(self.kb.cards), 1)
        self.assertEqual(self.kb.dispatch_tick(), [self.d()['card_id']])
        self.assertEqual(len(self.kb.subscriptions), 1)

    def test_no_channel_means_no_subscription(self):
        self.submit()
        self.assertEqual(self.kb.subscriptions, {})

    def test_child_question_reply_lost_response_is_bound_to_child(self):
        self.submit()
        cid = self.child()
        self.kb.worker_start(cid)
        self.kb.worker_block(cid, 'Which format?')
        delegation.sync_cards(self.conn, self.cfg, self.kb)
        question = next(x for x in delegation.deliveries(self.conn, self.did) if x['card_id'] == cid)
        ctx = ops.context(self.conn, self.cfg, request='synthetic-answer', said='Use Markdown')
        result = ops.delegate_answer(self.conn, self.cfg, ctx, self.did, question=question['id'], body='Markdown')
        self.kb.lose_response.add('comment')
        delegation.send_messages(self.conn, self.cfg, self.kb)
        delegation.send_messages(self.conn, self.cfg, self.kb)
        self.assertEqual(self.kb.cards[cid]['status'], 'ready')
        self.assertEqual(self.kb.cards[self.d()['card_id']]['status'], 'ready')
        self.assertEqual(sum(result['message'] in c['body'] for c in self.kb.comments), 1)
        self.assertTrue(all(c['task_id'] == cid for c in self.kb.comments))

    def test_stale_child_answer_does_not_unblock_new_question(self):
        self.submit()
        cid = self.child()
        self.kb.worker_start(cid)
        self.kb.worker_block(cid, 'First question')
        child_links.sync(self.conn, self.cfg, self.kb, self.d())
        question = delegation.deliveries(self.conn, self.did)[0]
        child_links.queue_answer(self.conn, self.d(), question, 'Old reply', 'old-answer', None)
        self.kb.unblock(cid)
        self.kb.worker_start(cid)
        self.kb.worker_block(cid, 'New question', kind='decision')
        delegation.send_messages(self.conn, self.cfg, self.kb)
        self.assertEqual(self.kb.cards[cid]['status'], 'blocked')
        self.assertEqual(self.kb.comments, [])

    def test_cancel_archives_proven_children_and_not_unrelated_cards(self):
        self.submit()
        cid = self.child()
        other = self.kb.create(title='Unrelated', body='', idem_key='other', assignee='default', tenant='test')
        delegation.add_message(self.conn, self.did, 'cancel', 'Stop owned work')
        delegation.send_messages(self.conn, self.cfg, self.kb)
        self.assertEqual(self.kb.cards[cid]['status'], 'archived')
        self.assertEqual(self.kb.cards[other]['status'], 'ready')
        self.assertEqual(self.d()['cancel_state'], 'confirmed')

    def test_child_result_is_not_aggregate_delivery_or_task_completion(self):
        self.submit()
        cid = self.child()
        self.kb.worker_start(cid)
        self.kb.worker_complete(cid, 'Partial draft')
        delegation.sync_cards(self.conn, self.cfg, self.kb)
        self.assertEqual(delegation.deliveries(self.conn, self.did), [])
        self.assertEqual(self.d()['status'], 'queued')
        self.assertEqual(store.get_task(self.conn, self.task)['status'], 'todo')
        self.assertGreater(self.conn.execute('SELECT COUNT(*) FROM delegation_child_events').fetchone()[0], 0)

    def test_optional_worker_skills_and_workspace_pass_through(self):
        self.cfg.data['kanban'].update(skills=['specific-worker-skill'], workspace='worktree:/synthetic/repo')
        self.submit()
        card = self.kb.cards[self.d()['card_id']]
        self.assertEqual(card['skills'], ['specific-worker-skill'])
        self.assertNotIn('todo-manager', card['skills'])
        self.assertEqual(card['workspace'], 'worktree:/synthetic/repo')
