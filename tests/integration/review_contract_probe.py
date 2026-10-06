"""Synthetic reliability interleavings against installed Hermes, never a model or channel client."""
import io
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

repo, project, home = map(Path, sys.argv[1:4])
sys.path[:0] = [str(project), str(repo)]
os.environ['HERMES_HOME'] = str(home)
os.environ['HERMES_DISABLE_LAZY_INSTALLS'] = '1'
from hermes_cli import kanban_db as native
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.kanban_db_notify import list_notify_subs
from todo_manager import delegation as dg, store, db, export, ops, kanban_bridge
from todo_manager.kanban import BridgeKanban, KanbanError
from tests.helpers import WorkspaceCase


class InlineBridge(BridgeKanban):
    """Run the actual bridge entrypoint in this isolated process; only transport is in-process."""
    def call(self, op, mutating=True, **args):
        req = dict(op=op, board=self.board, hermes_repo=str(repo), **args)
        output = io.StringIO()
        with patch('sys.stdin', io.StringIO(json.dumps(req))), patch('sys.stdout', output):
            kanban_bridge.main()
        result = json.loads(next(line for line in output.getvalue().splitlines() if line.startswith('@@TM@@'))[6:])
        if not result['ok']:
            raise KanbanError(result.get('error', 'native error'), code=result.get('code'))
        return result.get('result')


case = WorkspaceCase()
case.setUp()
report = {}
try:
    board = 'review-reliability'
    native.create_board(board, name='Synthetic reliability board')
    kb = InlineBridge(board, sys.executable, str(repo), str(home))
    case.cfg.data['kanban'].update(board=board, assignee='default', notify_platform='matrix',
                                  notify_chat_id='synthetic-no-client', notify_profile='default')
    task = store.create_task(case.conn, 'Synthetic', origin='cli', actor='human')
    did = dg.create(case.conn, case.cfg, task_id=task, title='Draft', goal='Draft only')
    def d():
        return dg.get(case.conn, did)
    def sync():
        return dg.sync_cards(case.conn, case.cfg, kb)
    def restart():
        case.conn.close()
        case.conn = db.connect(case.cfg.db_path)
    # Native creation succeeded; notification registration failed before the caller learned the id.
    def staged_failure(**args):
        with connect_closing(board=board) as conn:
            native.create_task(conn, title=args['title'], body=args['body'], assignee=args['assignee'],
                               created_by='todo-manager', initial_status='blocked', idempotency_key=args['idem_key'], board=board)
        raise KanbanError('Synthetic lost registration/create response', uncertain=True)
    with patch.object(kb, 'create', staged_failure):
        dg.submit_pending(case.conn, case.cfg, kb)
    dg.add_message(case.conn, did, 'pause', 'Wait')
    dg.submit_pending(case.conn, case.cfg, kb)
    card = d()['card_id']
    dg.add_message(case.conn, did, 'resume', 'Continue')
    export.reconcile_after_restore(case.conn, 'synthetic restore')
    restart()
    for _ in range(2):
        dg.send_messages(case.conn, case.cfg, kb)
        sync()
        with connect_closing(board=board) as conn:
            assert native.get_task(conn, card).status == 'blocked'
            assert not dispatch.dispatch_once(conn, dry_run=True, board=board).spawned
    dg.release_hold(case.conn)
    dg.send_messages(case.conn, case.cfg, kb)
    sync()
    with connect_closing(board=board) as conn:
        calls = []
        def worker(task, workspace, **kwargs):
            calls.append(task.id)
            native.block_task(conn, task.id, reason='Question one', kind='needs_input', expected_run_id=task.current_run_id)
        dispatch.dispatch_once(conn, spawn_fn=worker, board=board)
        assert calls == [card]
    sync()
    old = dg.deliveries(case.conn, did)[0]
    with connect_closing(board=board) as conn:
        native.unblock_task(conn, card)
        claimed = native.claim_task(conn, card, claimer='synthetic')
        native.block_task(conn, card, reason='Question two', kind='needs_input', expected_run_id=claimed.current_run_id)
    sync()
    ctx = ops.context(case.conn, case.cfg, request='old-answer', said='Markdown')
    try:
        ops.delegate_answer(case.conn, case.cfg, ctx, did, question=old['id'], body='Markdown', approve=['send_email'])
        raise AssertionError('old question accepted')
    except ops.OpError:
        pass
    assert not kb.snapshot(card)['comments']
    report['hold_blocks_native_dispatch_and_old_root_question_rejected'] = True

    with connect_closing(board=board) as conn:
        child = native.create_task(conn, title='Child', body='Synthetic', assignee='default', creator_task_id=card, board=board)
        claim = native.claim_task(conn, child, claimer='synthetic')
        native.block_task(conn, child, reason='Child question', kind='needs_input', expected_run_id=claim.current_run_id)
    sync()
    question = next(x for x in dg.deliveries(case.conn, did) if x['card_id'] == child)
    assert dg.task_label(case.conn, task) == 'blocked'
    answer_ctx = ops.context(case.conn, case.cfg, request='child-answer', said='Markdown')
    answer = ops.delegate_answer(case.conn, case.cfg, answer_ctx, did, question=question['id'], body='Markdown')
    original_transition = kb.transition
    def lost_unblock(*args, **kwargs):
        original_transition(*args, **kwargs)
        raise KanbanError('Synthetic lost unblock response', uncertain=True)
    with patch.object(kb, 'transition', lost_unblock):
        dg.send_messages(case.conn, case.cfg, kb)
    restart()
    dg.send_messages(case.conn, case.cfg, kb)
    assert sum(answer['message'] in c['body'] for c in kb.snapshot(child)['comments']) == 1
    assert kb.snapshot(child)['task']['status'] == 'ready'
    report['native_child_reply_recovers_lost_unblock_once'] = True

    kb.transition(card, 'unblock', d()['block_event_id'])
    with connect_closing(board=board) as conn:
        new_child = native.create_task(conn, title='Unseen child', body='Synthetic', assignee='default', creator_task_id=card, board=board)
        claim = native.claim_task(conn, new_child, claimer='synthetic')
        native.block_task(conn, new_child, reason='Unseen question', kind='needs_input', expected_run_id=claim.current_run_id)
        assert native.complete_task(conn, card, summary='Root result')
        before_cursor = list_notify_subs(conn, card)[0]['last_event_id']
    with patch.object(kb, 'owned', side_effect=KanbanError('Synthetic discovery failure')):
        sync()
    assert d()['status'] == 'ended' and d()['children_sync_pending']
    restart()
    with patch.object(kb, 'notify', side_effect=KanbanError('Synthetic notification failure')):
        sync()
    assert any(x['card_id'] == new_child for x in dg.deliveries(case.conn, did))
    assert not d()['children_sync_pending']
    assert task in [x['id'] for x in export.view_rows(case.conn, case.cfg)['review']]
    sync()
    with connect_closing(board=board) as conn:
        assert list_notify_subs(conn, card)[0]['last_event_id'] == before_cursor
    report['ended_discovery_retries_independently_of_notifications_and_preserves_cursor'] = True

    assert kb.profile_valid('default') and not kb.profile_valid('missing-synthetic-profile')
    other_task = store.create_task(case.conn, 'Profile recovery', origin='cli', actor='human')
    case.cfg.data['kanban']['assignee'] = ''
    other = dg.create(case.conn, case.cfg, task_id=other_task, title='Profile', goal='Draft only')
    dg.submit_pending(case.conn, case.cfg, kb, only=other)
    assert dg.get(case.conn, other)['card_id'] is None
    case.cfg.data['kanban']['assignee'] = 'default'
    dg.submit_pending(case.conn, case.cfg, kb, only=other)
    fixed = dg.get(case.conn, other)
    assert fixed['assignee'] == 'default' and fixed['card_id']
    assert len(kb.find(fixed['idem_key'], f'todo-manager-delegation: {other}')) == 1
    report['native_profile_validation_and_pending_recovery'] = True
    # A still-open event remains answerable after a supplement; grants stay version-bound.
    qtask = store.create_task(case.conn, 'Open question', origin='cli', actor='human')
    qdid = dg.create(case.conn, case.cfg, task_id=qtask, title='Question', goal='Draft only')
    dg.submit_pending(case.conn, case.cfg, kb, only=qdid)
    qroot = dg.get(case.conn, qdid)['card_id']
    with connect_closing(board=board) as conn:
        claim = native.claim_task(conn, qroot, claimer='synthetic')
        native.block_task(conn, qroot, reason='Choose format', kind='needs_input', expected_run_id=claim.current_run_id)
    dg.sync_cards(case.conn, case.cfg, kb)
    question = dg.deliveries(case.conn, qdid)[0]
    dg.add_message(case.conn, qdid, 'supplement', 'Use the updated style guide')
    dg.send_messages(case.conn, case.cfg, kb)
    qctx = ops.context(case.conn, case.cfg, request='native-supplement-answer', said='Markdown')
    ops.delegate_answer(case.conn, case.cfg, qctx, qdid, question=question['id'], body='Markdown')
    dg.send_messages(case.conn, case.cfg, kb)
    assert kb.snapshot(qroot)['task']['status'] == 'ready'
    report['open_question_survives_supplement_on_native_card'] = True

    # Terminal notification repair continues after restart without cursor rewriting/replay.
    with connect_closing(board=board) as conn:
        owned = native.create_task(conn, title='Owned active child', body='Synthetic', assignee='default',
                                   creator_task_id=qroot, board=board)
        unrelated = native.create_task(conn, title='Unrelated dependency', body='Synthetic', assignee='default', parents=[qroot], board=board)
        native.complete_task(conn, qroot, summary='Root finished')
    # Remove only synthetic subscriptions through the native API to exercise late registration.
    from hermes_cli.kanban_db_notify import remove_notify_sub
    with connect_closing(board=board) as conn:
        remove_notify_sub(conn, task_id=qroot, platform='matrix', chat_id='synthetic-no-client')
    original_notify = kb.notify
    def fail_terminal_notify(card, *args, **kwargs):
        if card == qroot:
            raise KanbanError('Synthetic terminal registration failure')
        return original_notify(card, *args, **kwargs)
    with patch.object(kb, 'notify', fail_terminal_notify):
        dg.sync_cards(case.conn, case.cfg, kb)
    restart()
    dg.sync_cards(case.conn, case.cfg, kb)
    with connect_closing(board=board) as conn:
        subs = list_notify_subs(conn, qroot)
        assert len(subs) == 1
        assert subs[0]['last_event_id'] == max(e.id for e in native.list_events(conn, qroot))
    report['terminal_native_registration_recovers_without_historical_replay'] = True

    store.update_task(case.conn, qtask, {'status': 'cancelled'}, actor='human', origin='cli')
    with patch.object(kb, 'owned', side_effect=KanbanError('Synthetic cancellation discovery outage')):
        dg.send_messages(case.conn, case.cfg, kb)
    restart()
    dg.send_messages(case.conn, case.cfg, kb)
    assert kb.snapshot(qroot)['task']['status'] == 'archived'
    assert kb.snapshot(owned)['task']['status'] == 'archived'
    assert kb.snapshot(unrelated)['task']['status'] != 'archived'
    assert dg.get(case.conn, qdid)['cancel_state'] == 'confirmed'
    report['native_cancel_after_root_done_stops_only_owned_child_after_restart'] = True

finally:
    case.tearDown()
print('@@PROBE@@' + json.dumps(report))
