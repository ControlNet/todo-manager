"""F01-F05 behaviour regressions. All remote responses are synthetic test doubles."""
import json
from unittest.mock import patch

from todo_manager import clock, db, effects, feedback, ops, outbox, store
from todo_manager.notion.client import NotionError
from todo_manager.sources.gmail import GmailSource
from tests.helpers import WorkspaceCase
from tests.test_notion import NotionCase
from tests.test_sources import FakeHttp, gmail_message


class BodyHelpers(NotionCase):
    def pending(self, kind):
        return [f for f in feedback.pending(self.conn, include_claimed=True) if f['kind'] == kind]

    def restart(self):
        self.conn.close()
        self.conn = db.connect(self.cfg.db_path)
        self.sync = self.new_sync()


class BodyFreshness(BodyHelpers):
    def test_creation_changed_body_invalidates_claimed_context_and_retries(self):
        page = self.fake.user_create_page(self.tasks_ds, 'Request', markdown='Set in_progress')
        self.sync.pull('task')
        clock.advance(minutes=2)
        self.sync.pull('task')
        first = self.pending('new_task')[0]
        old_ctx = ops.context(self.conn, self.cfg, feedback_id=first['id'])
        feedback.claim(self.conn, 'synthetic-run', [first['id']])
        self.fake.user_set_markdown(page, 'Keep todo; do not start')
        clock.advance(minutes=2)
        self.sync.pull('task')
        self.restart()
        with self.assertRaises(ops.OpError):
            ops.task_update(self.conn, self.cfg, old_ctx, first['scope_id'], changes={'status': 'in_progress'})
        current = self.pending('new_task')[0]
        self.assertNotEqual(first['id'], current['id'])
        self.assertEqual(current['payload']['body'], 'Keep todo; do not start')
        ctx = ops.context(self.conn, self.cfg, feedback_id=current['id'])
        ops.task_update(self.conn, self.cfg, ctx, first['scope_id'], changes={'summary': 'Keep todo'})
        self.assertTrue(ops.task_update(self.conn, self.cfg, ctx, first['scope_id'],
                                       changes={'summary': 'Keep todo'})['duplicate'])
        self.assertEqual(store.get_task(self.conn, first['scope_id'])['status'], 'todo')

    def test_complete_reread_recovers_identical_region_automatically(self):
        tid = store.create_task(self.conn, 'Task', actor='human', origin='cli')
        self.push(tid)
        page = self.page_of(tid)
        edited = self.fake.markdown[page].replace('</callout>', 'Only draft\n</callout>')
        self.fake.user_set_markdown(page, edited + '\nExtra notes' * 20)
        self.fake.truncate_markdown_at = len(edited) + 5
        clock.advance(minutes=2)
        self.sync.pull('task')
        first = self.pending('body_edit')[0]
        self.assertTrue(first['payload']['truncated'])
        old_ctx = ops.context(self.conn, self.cfg, feedback_id=first['id'])
        self.restart()
        self.fake.truncate_markdown_at = None
        self.sync.pull('task')
        current = self.pending('body_edit')[0]
        self.assertIsNone(ops.incomplete_reason(current))
        self.assertNotEqual(first['id'], current['id'])
        with self.assertRaises(ops.OpError):
            ops.task_update(self.conn, self.cfg, old_ctx, tid, changes={'summary': 'Old partial interpretation'})
        ctx = ops.context(self.conn, self.cfg, feedback_id=current['id'])
        ops.task_update(self.conn, self.cfg, ctx, tid, changes={'summary': 'Only draft'})

    def test_creation_read_failure_and_same_minute_edit_do_not_allow_mutation(self):
        page = self.fake.user_create_page(self.tasks_ds, 'Request', markdown='Start')
        self.sync.pull('task')
        first = self.pending('new_task')[0]
        old_ctx = ops.context(self.conn, self.cfg, feedback_id=first['id'])
        self.fake.user_set_markdown(page, 'Withdrawn')
        with patch.object(self.client, 'page_markdown', side_effect=NotionError('Synthetic outage', status=503)):
            self.sync.pull('task')
        with self.assertRaises(ops.OpError):
            ops.task_update(self.conn, self.cfg, old_ctx, first['scope_id'], changes={'status': 'in_progress'})


class ProjectionRecovery(WorkspaceCase):
    def test_three_replacements_inherit_deadline_for_tasks_and_projects(self):
        for kind in ('task', 'project'):
            with self.subTest(kind=kind):
                eid = getattr(store, 'create_' + kind)(self.conn, 'First', actor='human', origin='cli')
                push = getattr(effects, kind + '_push')
                update = getattr(store, 'update_' + kind)
                calls = []
                def limited(item, reconcile):
                    calls.append(clock.iso())
                    raise outbox.Retryable('Synthetic limit', retry_after=3600)
                handlers = {('notion', 'push_' + kind): limited}
                push(self.conn, eid)
                outbox.process(self.conn, handlers)
                for title in ('Second', 'Third', 'Fourth'):
                    clock.advance(minutes=5)
                    update(self.conn, eid, {'title': title}, actor='human', origin='cli')
                    push(self.conn, eid)
                    self.conn.close()
                    self.conn = db.connect(self.cfg.db_path)
                    outbox.process(self.conn, handlers)
                self.assertEqual(len(calls), 1)
                clock.advance(minutes=45)
                outbox.process(self.conn, handlers)
                self.assertEqual(len(calls), 2)


class PendingRelation(NotionCase):
    def test_explicit_clear_cancels_unresolved_relation_and_survives_restart(self):
        tid = store.create_task(self.conn, 'Task', actor='human', origin='cli')
        self.push(tid)
        remote = self.fake.user_create_page(self.projects_ds, 'Unread project')
        self.fake.user_edit(self.page_of(tid), **{'项目': [remote]})
        self.sync.pull('task')
        before = store.get_task(self.conn, tid)['version']
        ctx = ops.context(self.conn, self.cfg, request='clear', said='Clear project', scope='task:' + tid)
        ops.task_update(self.conn, self.cfg, ctx, tid, changes={'project_id': None})
        self.assertIsNone(db.get_meta(self.conn, 'notion.project_pending:' + tid))
        self.assertGreater(store.get_task(self.conn, tid)['version'], before)
        self.conn.close()
        self.conn = db.connect(self.cfg.db_path)
        self.sync = self.new_sync()
        self.sync.pull('project')
        self.sync.enqueue_dirty()
        self.drain()
        self.assertIsNone(store.get_task(self.conn, tid)['project_id'])
        self.assertEqual(self.fake.prop(self.page_of(tid), '项目'), [])


class IncrementalQuery(WorkspaceCase):
    def test_one_changed_thread_does_not_scan_hundred_mailbox_pages(self):
        def listing(p):
            # A server evaluates the added Message-ID restriction; unrestricted search
            # models a large mailbox. No local query-language implementation is tested.
            if 'rfc822msgid:' in p['q']:
                return {'messages': [{'id': 'm', 'threadId': 'changed'}]}
            page = int(p.get('pageToken') or 0)
            result = {'messages': [{'id': 'm', 'threadId': 'changed'}] if page == 0 else []}
            if page < 99:
                result['nextPageToken'] = str(page + 1)
            return result
        http = FakeHttp([
            ('/history', {'historyId': '2', 'history': [{'messagesAdded': [{'message': {'id': 'm', 'threadId': 'changed'}}]}]}),
            ('/threads/changed', {'messages': [gmail_message('m', 'changed')]}),
            ('/messages', listing),
        ])
        source = GmailSource(account='test', address='me@example.edu', token=lambda: 'synthetic',
                             my_addresses=[], http=http)
        result = source.fetch(json.dumps({'history_id': '1'}))
        self.assertEqual(len(result.items), 1)
        self.assertEqual(json.loads(result.cursor)['history_id'], '2')
        self.assertLessEqual(sum(c[1].endswith('/messages') for c in http.calls), 1)

    def test_large_mailbox_last_page_match_and_no_match_are_incremental(self):
        for matches in (True, False):
            with self.subTest(matches=matches):
                def listing(p):
                    if 'rfc822msgid:' in p['q']:
                        return {'messages': [{'threadId': 'changed'}] if matches else []}
                    page = int(p.get('pageToken') or 0)
                    return {'messages': [{'threadId': 'changed'}] if matches and page == 99 else [],
                            **({'nextPageToken': str(page + 1)} if page < 99 else {})}
                http = FakeHttp([
                    ('/history', {'historyId': '2', 'history': [{'messagesAdded': [
                        {'message': {'id': 'm', 'threadId': 'changed'}}]}]}),
                    ('/threads/changed', {'messages': [gmail_message('m', 'changed')]}),
                    ('/messages', listing),
                ])
                source = GmailSource(account='test', address='me@example.edu', token=lambda: 'synthetic',
                                     my_addresses=[], http=http)
                result = source.fetch(json.dumps({'history_id': '1'}))
                self.assertEqual(len(result.items), int(matches))
                self.assertTrue(result.complete)
                self.assertEqual(sum(c[1].endswith('/messages') for c in http.calls), 1)


class BodyCrossChecks(BodyHelpers):
    def test_incomplete_then_full_then_retraction_rejects_all_old_contexts(self):
        page = self.fake.user_create_page(self.tasks_ds, 'Request', markdown='Draft only; never send')
        self.fake.truncate_markdown_at = 5
        self.sync.pull('task')
        partial = self.pending('new_task')[0]
        ctx_partial = ops.context(self.conn, self.cfg, feedback_id=partial['id'])
        self.fake.truncate_markdown_at = None
        clock.advance(minutes=2)
        self.sync.pull('task')
        full = self.pending('new_task')[0]
        self.assertNotEqual(partial['id'], full['id'])
        ctx_full = ops.context(self.conn, self.cfg, feedback_id=full['id'])
        self.fake.user_set_markdown(page, '')
        self.sync.pull('task')
        current = self.pending('new_task')[0]
        self.assertEqual(current['payload']['body'], '')
        self.assertIsNone(ops.incomplete_reason(current))
        for ctx in (ctx_partial, ctx_full):
            with self.assertRaises(ops.OpError):
                ops.delegate_create(self.conn, self.cfg, ctx, task_id=full['scope_id'], title='Draft', goal='Send', submit=False)
        # Cover the coarse timestamp before interpreting the newest request.
        clock.advance(minutes=2)
        self.sync.pull('task')
        ctx = ops.context(self.conn, self.cfg, feedback_id=current['id'])
        first = ops.task_update(self.conn, self.cfg, ctx, full['scope_id'], changes={'summary': 'No action requested'})
        self.restart()
        second = ops.task_update(self.conn, self.cfg, ctx, full['scope_id'], changes={'summary': 'No action requested'})
        self.assertTrue(second['duplicate'])
        self.assertEqual(second['task'], first['task'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM delegations').fetchone()[0], 0)
        feedback.resolve(self.conn, current['id'], 'applied')
        self.fake.user_set_markdown(page, 'Ordinary notes after completion')
        clock.advance(minutes=2)
        self.sync.pull('task')
        self.assertFalse(self.pending('new_task'))

    def test_truncation_inside_box_then_changed_full_read_and_claim(self):
        tid = store.create_task(self.conn, 'Task', actor='human', origin='cli')
        self.push(tid)
        page = self.page_of(tid)
        original = self.fake.markdown[page]
        self.fake.user_set_markdown(page, original.replace('</callout>', 'Draft v1\n</callout>'))
        self.fake.truncate_markdown_at = len(original) // 2
        clock.advance(minutes=2)
        self.sync.pull('task')
        self.assertFalse(self.pending('body_edit'), 'an unfinished region cannot be interpreted')
        self.fake.truncate_markdown_at = None
        self.fake.user_set_markdown(page, original.replace('</callout>', 'Draft v2\n</callout>'))
        self.sync.pull('task')
        current = self.pending('body_edit')[0]
        ctx = ops.context(self.conn, self.cfg, feedback_id=current['id'])
        with self.assertRaises(ops.OpError):
            ops.task_update(self.conn, self.cfg, ctx, tid, changes={'summary': 'Draft v2'})
        feedback.claim(self.conn, 'synthetic-run', [current['id']])
        clock.advance(minutes=2)
        self.sync.pull('task')
        self.restart()
        self.sync.pull('task')
        self.assertEqual([f['id'] for f in self.pending('body_edit')], [current['id']])
        ctx = ops.context(self.conn, self.cfg, feedback_id=current['id'])
        ops.task_update(self.conn, self.cfg, ctx, tid, changes={'summary': 'Draft v2'})


class RelationCrossChecks(NotionCase):
    def unresolved(self):
        tid = store.create_task(self.conn, 'Task', actor='human', origin='cli')
        self.push(tid)
        remote = self.fake.user_create_page(self.projects_ds, 'Unread project')
        self.fake.user_edit(self.page_of(tid), **{'项目': [remote]})
        self.sync.pull('task')
        return tid, remote

    def test_rejected_agent_cannot_clear_pending_but_human_can_idempotently(self):
        tid, _ = self.unresolved()
        result = store.update_task(self.conn, tid, {'project_id': None}, actor='agent', origin='intake', based_on_version=1)
        self.assertIn('project_id', result.rejected)
        self.assertIsNotNone(db.get_meta(self.conn, 'notion.project_pending:' + tid))
        result = store.update_task(self.conn, tid, {'project_id': None}, actor='human', origin='cli')
        self.assertIn('project_id', result.applied)
        version = result.version
        result = store.update_task(self.conn, tid, {'project_id': None}, actor='human', origin='cli')
        self.assertEqual(result.version, version)
        self.assertFalse(result.applied)
        history = [json.loads(r[0]) for r in self.conn.execute('SELECT changes FROM history WHERE entity_id=?', (tid,))]
        self.assertTrue(any('pending_project' in h for h in history))

    def test_pending_remote_choice_advances_version_before_agent_override(self):
        tid = store.create_task(self.conn, 'Task', actor='human', origin='cli')
        self.push(tid)
        before = store.get_task(self.conn, tid)['version']
        remote = self.fake.user_create_page(self.projects_ds, 'Unread project')
        self.fake.user_edit(self.page_of(tid), **{'项目': [remote]})
        self.sync.pull('task')
        result = store.update_task(self.conn, tid, {'project_id': None}, actor='agent', origin='intake',
                                   based_on_version=before, override_human=True)
        self.assertIn('project_id', result.rejected)
        self.assertIsNotNone(db.get_meta(self.conn, 'notion.project_pending:' + tid))

    def test_clear_rate_limit_replacements_restart_and_remote_recovery(self):
        tid, remote = self.unresolved()
        calls = []
        def limited(item, reconcile):
            calls.append(clock.iso())
            raise outbox.Retryable('Synthetic limit', retry_after=3600)
        store.update_task(self.conn, tid, {'summary': 'Before limit'}, actor='human', origin='cli')
        self.sync.enqueue_dirty()
        outbox.process(self.conn, {('notion', 'push_task'): limited})
        clock.advance(minutes=5)
        store.update_task(self.conn, tid, {'project_id': None}, actor='human', origin='cli')
        self.sync.enqueue_dirty()
        for text in ('First note', 'Second note'):
            clock.advance(minutes=5)
            store.update_task(self.conn, tid, {'summary': text}, actor='human', origin='cli')
            self.sync.enqueue_dirty()
            outbox.process(self.conn, {('notion', 'push_task'): limited})
        self.conn.close()
        self.conn = db.connect(self.cfg.db_path)
        self.sync = self.new_sync()
        self.sync.pull('project')
        self.sync.enqueue_dirty()
        outbox.process(self.conn, {('notion', 'push_task'): limited})
        self.assertEqual(len(calls), 1)
        self.assertIsNone(store.get_task(self.conn, tid)['project_id'])
        clock.advance(minutes=45)
        self.drain()
        self.assertEqual(self.fake.prop(self.page_of(tid), '项目'), [])
        self.assertIn('Second note', self.fake.markdown[self.page_of(tid)])

    def test_notion_explicit_empty_relation_cancels_pending(self):
        tid, _ = self.unresolved()
        self.fake.user_edit(self.page_of(tid), **{'项目': []})
        self.sync.pull('task')
        self.assertIsNone(db.get_meta(self.conn, 'notion.project_pending:' + tid))
        self.sync.pull('project')
        self.assertIsNone(store.get_task(self.conn, tid)['project_id'])


class ProjectionCrossChecks(WorkspaceCase):
    def test_uncertain_blocks_new_projections_without_delaying_other_entity(self):
        one = store.create_task(self.conn, 'One', actor='human', origin='cli')
        two = store.create_task(self.conn, 'Two', actor='human', origin='cli')
        calls = []
        def unknown(item, reconcile):
            calls.append((item['entity_id'], reconcile))
            if item['entity_id'] == one:
                raise outbox.Uncertain('Synthetic response loss')
            return {}
        effects.task_push(self.conn, one)
        outbox.process(self.conn, {('notion', 'push_task'): unknown})
        for name in ('v2', 'v3'):
            store.update_task(self.conn, one, {'title': name}, actor='human', origin='cli')
            effects.task_push(self.conn, one)
        effects.task_push(self.conn, two)
        outbox.process(self.conn, {('notion', 'push_task'): unknown})
        self.assertEqual(calls, [(one, False), (two, False)])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outbox WHERE status='uncertain'").fetchone()[0], 1)
        clock.advance(minutes=5)
        recovered = []
        def success(item, reconcile):
            recovered.append((item['entity_id'], reconcile))
            return {}
        for _ in range(2):
            outbox.process(self.conn, {('notion', 'push_task'): success})
        self.assertEqual(recovered, [(one, True), (one, False)])

class GmailBudgetChecks(WorkspaceCase):
    query = '(from:trusted@example.edu OR subject:"camera ready") -label:ignored'

    def source(self, http, **kw):
        return GmailSource(account='test', address='me@example.edu', token=lambda: 'synthetic',
                           my_addresses=[], query=self.query, http=http, **kw)

    def transport(self, listing, messages=None, history=None):
        return FakeHttp([
            ('/history', history or {'historyId': '2', 'history': [{'messagesAdded': [
                {'message': {'id': 'reply', 'threadId': 'changed'}}]}]}),
            ('/profile', {'historyId': '3'}),
            ('/threads/changed', {'messages': messages or [gmail_message('parent', 'changed'), gmail_message('reply', 'changed')]}),
            ('/messages', listing),
        ])

    def test_bounded_candidates_matching_later_page_and_not_matching(self):
        for match_page in (0, 2, None):
            with self.subTest(match_page=match_page):
                def listing(p):
                    self.assertIn(self.query, p['q'])
                    self.assertIn('rfc822msgid:parent@x.org', p['q'])
                    self.assertIn('rfc822msgid:reply@x.org', p['q'])
                    page = int(p.get('pageToken') or 0)
                    if page == match_page:
                        return {'messages': [{'threadId': 'changed'}]}
                    # Model duplicate RFC IDs outside this candidate; membership must
                    # still be checked and pagination cannot be treated as no-match.
                    return {'messages': [{'threadId': 'other'}], **({'nextPageToken': str(page + 1)} if page < 2 else {})}
                http = self.transport(listing)
                cursor = json.dumps({'history_id': '1'})
                found = []
                for _ in range(8):
                    result = self.source(http, max_requests_per_run=2).fetch(cursor)
                    cursor = result.cursor
                    found.extend(result.items)
                    if result.complete:
                        break
                    self.assertEqual(json.loads(cursor)['history_id'], '1')
                self.assertTrue(result.complete)
                self.assertEqual(len(found), 0 if match_page is None else 2)
                searches = [c for c in http.calls if c[1].endswith('/messages')]
                self.assertEqual(len(searches), 3 if match_page is None else match_page + 1)
                self.assertEqual(json.loads(cursor)['history_id'], '2')

    def test_missing_identifier_fallback_resumes_without_false_negative(self):
        msg = gmail_message('no-header', 'changed')
        msg['payload']['headers'] = []
        def listing(p):
            self.assertEqual(p['q'], self.query)
            page = int(p.get('pageToken') or 0)
            return {'messages': [{'threadId': 'changed'}] if page == 99 else [],
                    **({'nextPageToken': str(page + 1)} if page < 99 else {})}
        http = self.transport(listing, [msg])
        cursor = json.dumps({'history_id': '1'})
        ticks = 0
        found = []
        while ticks < 40:
            before = len(http.calls)
            result = self.source(http, max_requests_per_run=4).fetch(cursor)
            ticks += 1
            self.assertLessEqual(len(http.calls) - before, 4)
            found.extend(result.items)
            cursor = result.cursor
            if result.complete:
                break
            self.assertEqual(json.loads(cursor)['history_id'], '1')
        self.assertTrue(result.complete)
        self.assertGreater(ticks, 20)
        self.assertEqual(len(found), 1)
        self.assertEqual(sum(c[1].endswith('/messages') for c in http.calls), 100)

    def test_pagination_failure_checkpoint_is_persisted_and_health_reports_error(self):
        from todo_manager import gate_intake
        from todo_manager.net import HttpError
        from todo_manager.sources.base import get_cursor
        fail = [True]
        def listing(p):
            if not p.get('pageToken'):
                return {'messages': [], 'nextPageToken': 'next'}
            if fail[0]:
                return HttpError(503, 'Synthetic outage')
            return {'messages': [{'threadId': 'changed'}]}
        http = self.transport(listing)
        self.conn.execute("INSERT INTO cursors(source,scope,cursor) VALUES ('gmail','mailbox:test',?)",
                          (json.dumps({'history_id': '1'}),))
        report = gate_intake.fetch_all(self.conn, [self.source(http)])
        self.assertIn('error', report['gmail:mailbox:test'])
        saved = get_cursor(self.conn, 'gmail', 'mailbox:test')
        self.assertIn('Synthetic outage', saved['last_error'])
        self.assertEqual(json.loads(saved['cursor'])['history_id'], '1')
        gate_intake.fetch_all(self.conn, [self.source(http)])
        self.assertEqual(get_cursor(self.conn, 'gmail', 'mailbox:test')['consecutive_failures'], 2)
        fail[0] = False
        self.conn.close()
        self.conn = db.connect(self.cfg.db_path)
        report = gate_intake.fetch_all(self.conn, [self.source(http)])
        self.assertNotIn('error', report['gmail:mailbox:test'])
        saved = get_cursor(self.conn, 'gmail', 'mailbox:test')
        self.assertIsNone(saved['last_error'])
        self.assertEqual(json.loads(saved['cursor'])['history_id'], '2')
        self.assertEqual(sum(c[1].endswith('/messages') and not c[2].get('pageToken') for c in http.calls), 1)

    def test_time_budget_between_requests_and_gate_idle_until_selection_finishes(self):
        from todo_manager import gate_intake
        http = self.transport({'messages': [{'threadId': 'changed'}]})
        source = self.source(http)
        # Start, history request, metadata request exceeds wall-clock budget.
        with patch('todo_manager.sources.gmail.time.monotonic', side_effect=[0, 0, 181]):
            result = source.fetch(json.dumps({'history_id': '1'}))
        self.assertFalse(result.complete)
        self.assertFalse(result.items)
        self.assertEqual(len(http.calls), 1)
        self.conn.execute("INSERT INTO cursors(source,scope,cursor) VALUES ('gmail','mailbox:test',?)", (result.cursor,))
        out, _ = gate_intake.run_gate(self.conn, self.cfg, [self.source(http, max_requests_per_run=2)], [])
        self.assertEqual(out.strip(), '{"wakeAgent": false}')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM source_items').fetchone()[0], 0)
        gate_intake.fetch_all(self.conn, [self.source(http)])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM source_items').fetchone()[0], 2)

    def test_history_expiry_and_initial_backfill_persist_pagination(self):
        from todo_manager.net import HttpError
        for initial in (True, False):
            with self.subTest(initial=initial):
                def listing(p):
                    self.assertIn('after:', p['q'])
                    self.assertIn(self.query, p['q'])
                    return {'messages': [{'threadId': 'changed'}],
                            **({'nextPageToken': 'p2'} if not p.get('pageToken') else {})}
                http = self.transport(listing, history=HttpError(404, 'expired'))
                cursor = None if initial else json.dumps({'history_id': '1'})
                result = self.source(http, max_requests_per_run=2).fetch(cursor)
                for _ in range(5):
                    if result.complete:
                        break
                    result = self.source(http, max_requests_per_run=2).fetch(result.cursor)
                self.assertTrue(result.complete)
                self.assertEqual(json.loads(result.cursor)['history_id'], '3')
                self.assertEqual(sum(c[1].endswith('/messages') for c in http.calls), 2)

    def test_old_matching_parent_selects_new_reply_and_hydration_backlog(self):
        parent = gmail_message('parent', 'changed')
        parent['internalDate'] = '1000000'
        reply = gmail_message('reply', 'changed', sender='nonmatching@example.org')
        http = self.transport({'messages': [{'id': 'parent', 'threadId': 'changed'}]}, [parent, reply])
        result = self.source(http, max_requests_per_run=3).fetch(json.dumps({'history_id': '1'}))
        self.assertFalse(result.complete)
        self.assertFalse(result.items)
        result = self.source(http).fetch(result.cursor)
        self.assertEqual([i.route for i in result.items], ['context', 'batch'])
        self.assertEqual(sum(c[1].endswith('/messages') for c in http.calls), 1)

    def test_large_multipart_body_makes_progress_across_small_budgets(self):
        from tests.test_gmail_body import by_reference
        from tests.test_sources import b64
        msg = by_reference('reply', 'changed', plain='Full plain text', html='<p>Full HTML</p>')
        http = self.transport({'messages': [{'threadId': 'changed'}]}, [msg])
        http.routes.insert(0, ('/attachments/', {'data': b64('Full body')}))
        cursor = json.dumps({'history_id': '1'})
        items = []
        for _ in range(8):
            result = self.source(http, max_requests_per_run=2).fetch(cursor)
            items.extend(result.items)
            cursor = result.cursor
            if result.complete:
                break
        self.assertTrue(result.complete, 'multipart hydration must not restart its first part every tick')
        self.assertEqual(len(items), 1)
        self.assertEqual(sum('/attachments/' in c[1] for c in http.calls), 2)

    def test_history_pages_resume_and_old_selected_queue_remains_compatible(self):
        def history(p):
            return {'historyId': '2', 'history': [], **({'nextPageToken': 'next'} if not p.get('pageToken') else {})}
        http = self.transport({'messages': []}, history=history)
        source = self.source(http, max_requests_per_run=2)
        with patch('todo_manager.sources.gmail.time.monotonic', side_effect=[0, 0, 181]):
            first = source.fetch(json.dumps({'history_id': '1'}))
        self.assertFalse(first.complete)
        second = self.source(http).fetch(first.cursor)
        self.assertTrue(second.complete)
        self.assertEqual([c[2].get('pageToken') for c in http.calls], [None, 'next'])
        http.calls.clear()
        legacy = json.dumps({'history_id': '1', 'queue_history_id': '2', 'queue': ['changed']})
        result = self.source(http).fetch(legacy)
        self.assertTrue(result.complete)
        self.assertEqual(json.loads(result.cursor)['history_id'], '2')
        self.assertEqual(len(http.calls), 1)
        self.assertTrue(http.calls[0][1].endswith('/threads/changed'))

    def test_many_message_ids_are_bounded_chunks_and_resume_until_match(self):
        messages = [gmail_message(str(i), 'changed') for i in range(25)]
        def listing(p):
            return {'messages': [{'threadId': 'changed'}] if 'rfc822msgid:24@x.org' in p['q'] else []}
        http = self.transport(listing, messages)
        result = self.source(http, max_requests_per_run=2).fetch(json.dumps({'history_id': '1'}))
        for _ in range(5):
            if result.complete:
                break
            result = self.source(http, max_requests_per_run=2).fetch(result.cursor)
        self.assertTrue(result.complete)
        self.assertEqual(len(result.items), 25)
        queries = [c[2]['q'] for c in http.calls if c[1].endswith('/messages')]
        self.assertEqual([q.count('rfc822msgid:') for q in queries], [10, 10, 5])
