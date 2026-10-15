"""Real Hermes kernel + simulated worker, synthetic data and no network/model calls."""
import inspect
import json
import os
from pathlib import Path
import sys

repo, project, home = map(Path, sys.argv[1:4])
sys.path.insert(0, str(repo))
sys.path.insert(0, str(project))
os.environ['HERMES_HOME'] = str(home)
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.kanban_db_notify import add_notify_sub, list_notify_subs, unseen_events_for_sub
from hermes_cli.kanban_db_graph import decompose_triage_task
from todo_manager.kanban_bridge import ensure_notify, owned_cards
from todo_manager import delegation, templates
from cron.scheduler_prompt import _build_job_prompt, _parse_wake_gate
from agent.prompt_builder import build_context_files_prompt
from tools.memory_tool import MemoryStore

board = 'native-contract'
kb.create_board(board, name='Synthetic contract test')
route = {'platform': 'matrix', 'chat_id': 'synthetic-no-client', 'notifier_profile': 'default'}
report = {}
with connect_closing(board=board) as conn:
    tid = kb.create_task(conn, title='Synthetic root', body='root', assignee='default', initial_status='blocked', created_by='todo-manager', board=board)
    assert not dispatch.dispatch_once(conn, dry_run=True, board=board).spawned
    assert ensure_notify(kb, conn, tid, route, release=True)['status'] == 'ready'
    spawned = []
    def simulated_worker(task, workspace, **kwargs):
        spawned.append((task.id, task.assignee, workspace, task.skills))
        assert str(home) in workspace
        assert task.id == tid
        assert kb.get_task(conn, tid).status == 'running'
        return None
    result = dispatch.dispatch_once(conn, spawn_fn=simulated_worker, board=board)
    assert result.spawned and spawned[0][1] == 'default'
    assert not spawned[0][3], 'Todo skill is not inherited by workers'
    run_id = kb.get_task(conn, tid).current_run_id
    kb.block_task(conn, tid, kind='needs_input', reason='Synthetic question', expected_run_id=run_id)
    events = unseen_events_for_sub(conn, task_id=tid, platform=route['platform'], chat_id=route['chat_id'])[1]
    assert any(e.kind == 'blocked' for e in events)
    before = list_notify_subs(conn, tid)[0]['last_event_id']
    ensure_notify(kb, conn, tid, route)
    assert list_notify_subs(conn, tid)[0]['last_event_id'] == before
    assert kb.get_task(conn, tid).status == 'blocked', 'repair must not unblock user questions'
    report['staged_subscription_dispatch_and_blocked_event'] = True
    late = kb.create_task(conn, title='Late registration', assignee='default', board=board)
    kb.block_task(conn, late, kind='needs_input', reason='before subscription')
    assert ensure_notify(kb, conn, late, route)['history_gap']
    assert unseen_events_for_sub(conn, task_id=late, platform=route['platform'], chat_id=route['chat_id'])[1] == []
    report['late_subscription_does_not_replay'] = True

    root = kb.create_task(conn, title='Native decomposition', body='ROOT_ONLY', assignee='default', triage=True, board=board)
    add_notify_sub(conn, task_id=root, delivery_mode='notify', **route)
    children = decompose_triage_task(conn, root, root_assignee='default', children=[
        {'title': 'Child', 'body': 'CHILD_ONLY', 'assignee': 'default'}])
    assert owned_cards(kb, conn, root) == children
    assert list_notify_subs(conn, children[0])[0]['delivery_mode'] == 'notify'
    context = kb.build_worker_context(conn, children[0])
    assert 'CHILD_ONLY' in context and 'ROOT_ONLY' not in context
    outsider = kb.create_task(conn, title='Dependent outsider', parents=[root], assignee='default', board=board)
    assert outsider not in owned_cards(kb, conn, root)
    kb.archive_task(conn, root)
    assert kb.get_task(conn, children[0]).status != 'archived'
    kb.recompute_ready(conn)
    assert kb.get_task(conn, outsider).status == 'ready'
    report['lineage_inheritance_and_nonrecursive_archive'] = True

    d = {'id': 'dlg_synthetic', 'requirement_version': 1, 'allowed_actions': ['read', 'draft'],
         'goal': 'LONG_MATERIAL ' * 2500, 'context': 'kept in full'}
    body = delegation.card_body(d, {'id': 'tsk_synthetic', 'title': 'Long task'}, None, [], 'N' * 9000)
    long_id = kb.create_task(conn, title='Long context', body=body, assignee='default', board=board)
    worker_context = kb.build_worker_context(conn, long_id)
    assert 'must NOT be repeated' in worker_context
    assert 'todo-manager delegate show dlg_synthetic' in worker_context
    assert len(worker_context) < len(body)
    assert kb.get_task(conn, long_id).body == body
    from hermes_cli.kanban_specify import _task_prompt_fields
    assert len(_task_prompt_fields(kb.get_task(conn, long_id))['body']) < len(worker_context)
    argv = dispatch._worker_argv(kb.get_task(conn, long_id), 'default', str(home))
    if argv[-1] == 'hermes_cli.kanban_worker_client':
        # Since 2026-10 (ca069e1) the profile owner (gateway) runs the card: it refuses any profile
        # but the assignee's and preloads only the card's own skills (gateway.session_kanban).
        from gateway.session_kanban import build_kanban_policy
        assert kb.get_task(conn, long_id).assignee == 'default' and not kb.get_task(conn, long_id).skills
        assert 'resolve_profile_env(task.assignee)' in inspect.getsource(build_kanban_policy)
        assert 'skills = list(task.skills or ())' in inspect.getsource(build_kanban_policy)
    else:
        assert '-p' in argv and '--skills' not in argv
    report['worker_protocol_survives_clipping_and_decomposer_is_shorter'] = True

workspace = home / 'workspace'
workspace.mkdir()
(workspace / 'AGENTS.md').write_text('MANAGED_PROJECT_CONTEXT')
assert 'MANAGED_PROJECT_CONTEXT' in build_context_files_prompt(str(workspace), skip_soul=True)
(workspace / 'AGENTS.override.md').write_text('OVERRIDE_CONTEXT')
ctx = build_context_files_prompt(str(workspace), skip_soul=True)
assert 'OVERRIDE_CONTEXT' in ctx and 'MANAGED_PROJECT_CONTEXT' not in ctx
(workspace / 'HERMES.md').write_text('HERMES_CONTEXT')
ctx = build_context_files_prompt(str(workspace), skip_soul=True)
assert 'HERMES_CONTEXT' in ctx and 'OVERRIDE_CONTEXT' not in ctx
(home / 'SOUL.md').write_text('SYNTHETIC_SOUL')
assert 'SYNTHETIC_SOUL' in build_context_files_prompt(str(workspace), home_override=home)
(home / 'memories').mkdir(exist_ok=True)
(home / 'memories/USER.md').write_text('Synthetic preference before')
memory = MemoryStore()
memory.load_from_disk()
before = memory.format_for_system_prompt('user')
(home / 'memories/USER.md').write_text('Synthetic preference after')
assert memory.format_for_system_prompt('user') == before
new_memory = MemoryStore()
new_memory.load_from_disk()
assert 'Synthetic preference after' in new_memory.format_for_system_prompt('user')
report['native_project_priority_soul_and_memory_snapshot'] = True

skill = home / 'skills/productivity/todo-manager/SKILL.md'
skill.parent.mkdir(parents=True)
skill.write_text(templates.asset('skill/SKILL.md'))
job = {'id': 'synthetic-cron', 'prompt': templates.cron_prompt('feedback'), 'skills': ['todo-manager'],
       'script': 'synthetic.py', 'workdir': str(workspace)}
assembled = _build_job_prompt(job, prerun_script=(True, 'SYNTHETIC_MATERIAL'))
assert 'SYNTHETIC_MATERIAL' in assembled and '## Command reference' in assembled and 'Script Output' in assembled
failed = _build_job_prompt(job, prerun_script=(False, 'synthetic failure'))
assert 'Script Error' in failed and '[CRON_FAILURE]' in failed
assert not _parse_wake_gate('{"wakeAgent": false}')
assert _build_job_prompt(job, prerun_script=(True, '')) is None
report['native_cron_skills_script_output_error_and_idle'] = True

# Complete connector path: synthetic Notion property edit -> recorded request ->
# explicit simulated feedback decision -> real card -> native dispatcher callback.
sys.path.insert(0, str(project))
sys.modules.pop("tests", None)
from tests.test_feedback import GateCase
from todo_manager import clock, feedback, ops
from todo_manager.kanban import BridgeKanban
case = GateCase()
case.setUp()
try:
    e2e_board = 'native-notion-entry'
    kb.create_board(e2e_board, name='Synthetic Notion entry')
    case.cfg.data['kanban'].update(assignee='default', board=e2e_board, notify_platform='matrix',
                                    notify_chat_id='synthetic-no-client', notify_profile='default')
    case.kb = BridgeKanban(e2e_board, sys.executable, str(repo), str(home))
    clock.advance(minutes=1)
    case.fake.user_edit(case.page, **{'下一步由谁': 'Agent'})
    output, woke, run = case.gate()
    assert woke and case.task in output
    item = next(f for f in feedback.claimed_by(case.conn, run) if f['kind'] == 'delegate_request')
    ctx = ops.context(case.conn, case.cfg, feedback_id=item['id'])
    args = dict(task_id=case.task, title='Synthetic delegated draft', goal='Draft only', submit=False)
    decision = ops.delegate_create(case.conn, case.cfg, ctx, **args)
    again = ops.delegate_create(case.conn, case.cfg, ctx, **args)
    assert decision['delegation'] == again['delegation']
    ops.feedback_resolve(case.conn, case.cfg, item['id'], outcome='applied', reply='Synthetic draft delegated')
    from unittest.mock import patch
    from todo_manager.kanban import KanbanError
    original_create = case.kb.create
    def lost_create_response(**kwargs):
        original_create(**kwargs)
        raise KanbanError('Synthetic lost create response', uncertain=True)
    with patch.object(case.kb, 'create', lost_create_response):
        case.gate()
    case.gate()
    did = decision['delegation']
    linked = delegation.get(case.conn, did)
    root_card = linked['card_id']
    assert not delegation.deliveries(case.conn, did), 'subscription staging is not a user question'
    assert root_card and linked['from_feedback_id'] == item['id']
    assert len(case.kb.find(linked['idem_key'], f'todo-manager-delegation: {did}')) == 1
    with connect_closing(board=e2e_board) as native:
        called = []
        def root_worker(task, workspace, **kwargs):
            called.append(task.id)
            kb.block_task(native, task.id, reason='Root waits for integration', kind='needs_input',
                          expected_run_id=task.current_run_id)
        dispatch.dispatch_once(native, spawn_fn=root_worker, board=e2e_board)
        assert called == [root_card]
        child = kb.create_task(native, title='Synthetic child question', body='Read and draft only',
                               assignee='default', creator_task_id=root_card, board=e2e_board)
        claimed = kb.claim_task(native, child, claimer='synthetic-child')
        kb.block_task(native, child, reason='Which output format?', kind='needs_input', expected_run_id=claimed.current_run_id)
    case.gate()
    notice = next(c for c in case.bot_comments() if 'Which output format?' in str(c))
    comment_id = case.fake.user_comment(case.page, 'Use Markdown', discussion_id=notice['discussion_id'])
    _, woke, run = case.gate()
    assert woke
    answer = next(f for f in feedback.claimed_by(case.conn, run) if f['source_id'] == comment_id)
    assert answer['payload']['delivery_kind'] == 'question'
    answer_ctx = ops.context(case.conn, case.cfg, feedback_id=answer['id'])
    ops.delegate_answer(case.conn, case.cfg, answer_ctx, did, body='Use Markdown')
    ops.feedback_resolve(case.conn, case.cfg, answer['id'], outcome='applied', reply='Sent to the requesting child')
    case.gate()
    assert case.kb.snapshot(child)['task']['status'] == 'ready'
    assert case.kb.snapshot(root_card)['task']['status'] == 'blocked'
    case.gate()
    assert sum('Use Markdown' in c['body'] for c in case.kb.snapshot(child)['comments']) == 1
    assert case.conn.execute('SELECT notion_page_id FROM tasks WHERE id=?', (case.task,)).fetchone()[0] == case.page
    report['notion_assign_to_dispatch_and_child_question_round_trip'] = True
finally:
    case.tearDown()

# Real CLI create/edit/remove; every job stays paused and delivers locally.
from todo_manager import config, hermes_install, prefs
install_home = home / 'installer-profile'
control = home / 'installer-workspace'
control.mkdir()
cfg = config.load(control)
cfg.data['hermes'].update(home=str(install_home), bin=str(Path(sys.executable).with_name('hermes')),
                          launcher=str(home / 'bin/todo-manager'))
paths = hermes_install.Paths(install_home, control, home / 'bin/todo-manager', source_root=project, python=sys.executable)
installer = hermes_install.Installer(cfg, paths)
installer.apply(installer.plan())
jobs = hermes_install.load_jobs(paths)
assert len(jobs) == 2 and all(j.get('state') == 'paused' for j in jobs.values())
intake = jobs['todo-manager-intake']
custom = intake['prompt'] + '\nSynthetic old customization'
installer.hermes('cron', 'edit', intake['id'], '--prompt', custom, '--schedule', '*/17 * * * *',
                 '--deliver', 'local', '--failure-deliver', 'local', '--model', 'synthetic-model',
                 '--provider', 'openai', '--reasoning-effort', 'low')
before = hermes_install.load_jobs(paths)['todo-manager-intake']
installer.apply(installer.plan())
after = hermes_install.load_jobs(paths)['todo-manager-intake']
assert after['prompt'] == templates.cron_prompt('intake')
for field in ('schedule', 'deliver', 'failure_deliver', 'model', 'provider', 'reasoning_effort', 'state', 'workdir', 'skills'):
    assert after.get(field) == before.get(field), field
assert any(custom == json.loads(p.read_text())['todo-manager-intake']['prompt']
           for p in (control / 'backups/framework').glob('*/cron.json') if 'todo-manager-intake' in json.loads(p.read_text()))
installer.uninstall(installer.uninstall_plan())
assert not hermes_install.load_jobs(paths)
assert (control / 'AGENTS.md').exists()
report['native_cli_install_upgrade_preserves_runtime_settings_and_uninstall'] = True
print('@@PROBE@@' + json.dumps(report))
