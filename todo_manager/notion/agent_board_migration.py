"""Install-owned, resumable Notion upgrade to the Agent progress board (2026-10-12).

Before: "Agent work" was a board grouped by the read-only Delegation select, "To review" a
separate table of the agent work waiting for the user, and Focus listed that work too. After:
one board grouped by the Agent progress select, under the renamed Agent heading; the To review
section and the old board's block go to the Notion trash (restorable there); Focus lists the
user's own work only. Each step checks what is already done, so a rerun continues an
interrupted install. The user hides the board's empty "No Agent progress" column once (the API
cannot hide a single column).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .. import clock, delegation, i18n
from ..db import dumps, get_meta, loads
from . import schema, setup
from .client import NotionError
from .decision_migration import _bare, _create_once, _heading_text, _ids
from .sync import save_state

VERSION = '1'


def OLD_AGENT(p: dict[str, str], lang: str | None) -> dict[str, Any]:
    """The Agent work board as set up before this upgrade."""
    return {'type': 'board', 'filter': {'property': p['delegation'], 'select': {'is_not_empty': True}},
            'configuration': {'type': 'board', 'group_by': {'type': 'select', 'property_id': p['delegation'],
                                                            'sort': {'type': 'manual'}, 'hide_empty_groups': True},
                              'properties': [{'property_id': p[f], 'visible': True} for f in ('next_step', 'project_id')]}}


def OLD_FOCUS(p: dict[str, str], lang: str | None) -> dict[str, Any]:
    """The Focus filter as set up before this upgrade (it also listed agent work waiting for the user)."""
    needs_user = [schema.label('delegation', v, lang) for v in delegation.NEEDS_USER_LABELS]
    return {'and': [
        {'property': p['status'], 'select': {'equals': [schema.label('task_status', v, lang) for v in ('todo', 'in_progress')]}},
        {'property': p['next_actor'], 'select': {'equals': schema.label('next_actor', 'me', lang)}},
        setup._not_snoozed(p),
        {'or': [{'property': p['delegation'], 'select': {'equals': needs_user}},
                {'property': p['focus'], 'checkbox': {'equals': True}},
                {'property': p['due_date'], 'date': {'on_or_before': 'one_week_from_now'}}]}]}


def _view(client, view_id: str | None) -> dict[str, Any] | None:
    if not view_id:
        return None
    try:
        return client.retrieve_view(view_id)
    except NotionError as exc:
        if exc.status == 404 or exc.not_found:
            return None
        raise


def plan(conn, client) -> dict[str, Any] | None:
    """What ``apply`` would do (read-only), or None when there is nothing to do."""
    if get_meta(conn, 'notion.agent_ui_version') == VERSION:
        return None
    home = get_meta(conn, 'notion.home_page_id')
    ds_id = get_meta(conn, 'notion.tasks_data_source_id')
    views = loads(get_meta(conn, 'notion.views'), {}) or {}
    if not home or not ds_id or 'agent' not in views:
        return None
    lang = get_meta(conn, 'notion.language') or 'en'
    props = loads(get_meta(conn, 'notion.props.task'), {})
    ds = client.retrieve_data_source(ds_id)
    name = schema.prop('task', 'agent_progress').name(lang)
    known = props.get('agent_progress')
    matching = next((v for k, v in ds['properties'].items()
                     if (known and unquote(v.get('id') or '') == unquote(known)) or k == name), None)
    if matching and matching.get('type') != 'select':
        raise RuntimeError(f'{name}: an existing property is not a select; cannot migrate safely')
    if matching and not known:
        options = {o['name'] for o in matching.get('select', {}).get('options', [])}
        if options != set(schema.labels('agent_progress', lang).values()):
            raise RuntimeError(f'{name}: an existing property has unknown ownership or options')
    agent = _view(client, views['agent'])
    if agent is None:
        raise RuntimeError('the Agent work view is gone; restore it from the Notion trash or remove it from'
                           ' notion.views before installing')
    group = unquote(((agent.get('configuration') or {}).get('group_by') or {}).get('property_id') or '')
    migrated = bool(known) and group == unquote(known)
    if not migrated and not (agent.get('type') == 'board' and group == unquote(props['delegation'])):
        raise RuntimeError('the Agent work view was customized; preserve it and review this migration before installing')
    focus = _view(client, views.get('focus'))
    focus_owned = focus is not None and _ids(focus.get('filter')) == _ids(OLD_FOCUS(props, lang))
    review = _view(client, views.get('review'))
    columns = ' / '.join(schema.labels('agent_progress', lang).values())
    heading = i18n.t('home.section.agent', lang)
    changes = [f'Add the {name} select ({columns}): Done / My to-do / Cancel are the user\'s drags, '
               'the other columns are written by Todo Manager.',
               f'Replace the Agent work board with one grouped by {name} under the heading "{heading}";'
               ' the old board block goes to the Notion trash.']
    if review is not None:
        changes.append('Move the To review section (heading and table) to the Notion trash: its tasks are in'
                       ' the new board\'s first two columns.')
    if focus_owned:
        changes.append('Focus: list your own work only (agent work waiting for you is on the board).')
    elif focus is not None:
        changes.append('Focus view was customized: kept as it is (it may still list agent work).')
    changes.append(f'Afterwards, in Notion: hide the board\'s empty "No {name}" column once (the API cannot).')
    return {'home': home, 'data_source': ds_id, 'lang': lang, 'views': views, 'agent_view': agent,
            'review_view': review, 'focus_view': focus, 'focus_owned': focus_owned, 'migrated': migrated,
            'home_blocks': client.block_children(home), 'properties': sorted(ds['properties']), 'changes': changes}


def apply(conn, client, home: Path) -> str | None:
    proposed = plan(conn, client)
    if not proposed:
        return None
    backup = home / 'backups' / 'notion-agent-board-v1.json'
    if not backup.exists():   # the first snapshot is the rollback point; reruns never replace it
        backup.parent.mkdir(parents=True, exist_ok=True)
        temporary = backup.with_suffix('.json.tmp')
        temporary.write_text(json.dumps({'at': clock.iso(), **proposed}, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(backup)
    lang, ds_id, views = proposed['lang'], proposed['data_source'], proposed['views']
    page_home = proposed['home']
    props = loads(get_meta(conn, 'notion.props.task'), {})
    name = schema.prop('task', 'agent_progress').name(lang)
    mapping = schema.PropertyMap.from_data_source('task', client.retrieve_data_source(ds_id), props, lang)
    if 'agent_progress' not in mapping.ids:
        client.update_data_source(ds_id, {name: schema.data_source_properties(
            'task', get_meta(conn, 'notion.projects_data_source_id'), lang)[name]})
        mapping = schema.PropertyMap.from_data_source('task', client.retrieve_data_source(ds_id), props, lang)
    save_state(conn, **{'props.task': mapping.ids})
    # The new property is empty on every page: what the next push compares against.
    for r in conn.execute("SELECT page_id, last_seen_props FROM notion_pages WHERE entity_type='task'").fetchall():
        seen = loads(r['last_seen_props'], {})
        if 'agent_progress' not in seen:
            conn.execute('UPDATE notion_pages SET last_seen_props=? WHERE page_id=?',
                         (dumps({**seen, 'agent_progress': None}), r['page_id']))
    # Blocks to retire, fixed before the first change (a rerun finishes the same list).
    retire = loads(get_meta(conn, 'notion.agent_ui_retire'), None)
    blocks = proposed['home_blocks']
    if retire is None:
        retire = []
        if not proposed['migrated']:
            retire.append(_bare(proposed['agent_view']['parent']['database_id']))
        if proposed['review_view'] is not None:
            review_db = _bare(proposed['review_view']['parent']['database_id'])
            retire += [_bare(b) for b in (_heading_before(blocks, review_db), review_db) if b]
        save_state(conn, agent_ui_retire=retire)
    specs = {s['key']: s for s in setup.view_specs(mapping.ids, loads(get_meta(conn, 'notion.props.project'), {}), lang)}
    if not proposed['migrated']:
        spec = specs['agent']
        old_db = _bare(proposed['agent_view']['parent']['database_id'])
        anchor = _heading_before(blocks, old_db) or next((b['id'] for b in blocks if _bare(b['id']) == old_db), None)
        placement: dict[str, Any] = {'parent': {'type': 'page_id', 'page_id': page_home}}
        if anchor:
            placement['position'] = {'type': 'after_block', 'block_id': anchor}
        view = _create_once(conn, client, 'agent_board', {'data_source_id': ds_id}, {
            'create_database': placement, 'data_source_id': ds_id,
            **{k: spec[k] for k in ('name', 'type', 'filter', 'sorts', 'configuration')}})
        views['agent'] = view['id']
        save_state(conn, views=views)
    blocks = client.block_children(page_home)
    for block in blocks:
        if _bare(block['id']) in retire:
            client.trash_block(block['id'])
    views.pop('review', None)
    save_state(conn, views=views)
    agent_db = _bare(client.retrieve_view(views['agent'])['parent']['database_id'])
    blocks = client.block_children(page_home)
    heading = _heading_before(blocks, agent_db)
    text = i18n.t('home.section.agent', lang)
    found = next((b for b in blocks if _bare(b['id']) == _bare(heading)), None) if heading else None
    if found is not None and _heading_text(found) != text:
        client.update_block(found['id'], {found['type']: {'rich_text': [{'type': 'text', 'text': {'content': text}}]}})
    if proposed['focus_owned']:
        client.update_view(views['focus'], {'filter': specs['focus']['filter']})
    save_state(conn, agent_ui_version=VERSION)
    return str(backup)


def _heading_before(blocks: list[dict[str, Any]], block_id: str) -> str | None:
    """The id of the section heading right above a block (given by its bare id), if it is one."""
    for before, block in zip(blocks, blocks[1:]):
        if _bare(block['id']) == block_id and _heading_text(before) is not None:
            return before['id']
    return None
