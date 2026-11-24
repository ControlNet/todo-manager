"""Cards of agent work that ended a while ago leave the board (synthetic cards, no live services)."""
from tests.helpers import WorkspaceCase
from todo_manager import clock, delegation as dg, store
from todo_manager.kanban import FakeKanban, KanbanError


class CardArchiveTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.cfg.data['kanban']['assignee'] = 'default'
        self.kb = FakeKanban()
        self.task = store.create_task(self.conn, 'Synthetic task', origin='cli', actor='human')
        self.did = dg.create(self.conn, self.cfg, task_id=self.task, title='Draft', goal='Draft only')
        dg.submit_pending(self.conn, self.cfg, self.kb)
        self.card = dg.get(self.conn, self.did)['card_id']

    def child(self):
        cid = self.kb.create(title='Child', body='Synthetic', idem_key='child', assignee='default', tenant='test',
                             max_retries=1)
        self.kb.ownership[cid] = self.card
        return cid

    def finish(self, card):
        if self.kb.cards[card]['status'] == 'ready':
            self.kb.worker_start(card)
        self.kb.worker_complete(card, 'Done', {'deliverable_markdown': 'Draft'})

    def archive(self):
        return dg.archive_settled(self.conn, self.cfg, self.kb)

    def test_ended_work_leaves_the_board_after_a_week_with_its_sub_cards(self):
        child = self.child()
        self.finish(child)
        self.finish(self.card)
        dg.sync_cards(self.conn, self.cfg, self.kb)
        self.assertEqual(dg.get(self.conn, self.did)['status'], 'ended')
        clock.advance(days=6)
        self.assertEqual(self.archive()['archived'], 0)
        clock.advance(days=1, minutes=1)
        self.assertEqual(self.archive()['archived'], 1)
        self.assertEqual((self.kb.cards[self.card]['status'], self.kb.cards[child]['status']), ('archived', 'archived'))
        d = dg.get(self.conn, self.did)
        self.assertEqual((d['status'], d['card_status']), ('ended', 'archived'), 'archiving is not a cancel')
        self.assertEqual(len(dg.deliveries(self.conn, self.did)), 1)
        self.assertEqual(self.archive()['archived'], 0)
        dg.sync_cards(self.conn, self.cfg, self.kb)
        self.assertEqual(dg.get(self.conn, self.did)['status'], 'ended')

    def test_work_still_going_or_with_a_message_on_its_way_stays(self):
        self.kb.worker_start(self.card)
        dg.sync_cards(self.conn, self.cfg, self.kb)
        clock.advance(days=30)
        self.assertEqual(self.archive()['archived'], 0)
        self.finish(self.card)
        dg.sync_cards(self.conn, self.cfg, self.kb)
        dg.add_message(self.conn, self.did, 'supplement', 'One more thing')
        clock.advance(days=30)
        self.assertEqual(self.archive()['archived'], 0)
        self.assertEqual(self.kb.cards[self.card]['status'], 'done')

    def test_an_unfinished_sub_card_keeps_the_work_on_the_board(self):
        child = self.child()
        self.kb.worker_start(child)
        self.finish(self.card)
        dg.sync_cards(self.conn, self.cfg, self.kb)
        clock.advance(days=30)
        self.assertEqual(self.archive()['archived'], 0)

    def test_zero_days_turns_it_off_and_a_failure_is_retried_next_time(self):
        self.finish(self.card)
        dg.sync_cards(self.conn, self.cfg, self.kb)
        clock.advance(days=30)
        self.cfg.data['kanban']['archive_after_days'] = 0
        self.assertEqual(self.archive()['archived'], 0)
        self.cfg.data['kanban']['archive_after_days'] = 7
        self.kb.fail_next['archive'] = KanbanError('db locked')
        self.assertEqual(self.archive()['errors'], 1)
        self.assertEqual(self.archive()['archived'], 1)
