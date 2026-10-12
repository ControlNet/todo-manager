"""Install-owned, resumable Notion decision UI upgrade. No background schema writes."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote

from .. import clock, i18n
from ..db import get_meta, loads, dumps
from . import schema, setup
from .client import NotionUncertain
from .sync import save_state


VERSION = '2'   # 1: the decision board; 2: its choices are Cancel / Done / My to-do / Assign to Agent


def plan(conn, client):
    version = get_meta(conn, 'notion.decision_ui_version')
    if version == VERSION:
        return None
    if version == '1':
        return _choices_plan(conn, client)
    home = get_meta(conn, 'notion.home_page_id')
    ds_id = get_meta(conn, 'notion.tasks_data_source_id')
    if not home or not ds_id:
        return None
    lang = get_meta(conn, 'notion.language') or 'en'
    ds = client.retrieve_data_source(ds_id)
    views = loads(get_meta(conn, 'notion.views'), {})
    decision = client.retrieve_view(views['decision'])
    props = loads(get_meta(conn, 'notion.props.task'), {})
    expected_filter = {'property': props['status'], 'select': {'equals': schema.label('task_status', 'decision', lang)}}
    visible = [p['property_id'] for p in decision.get('configuration', {}).get('properties', []) if p.get('visible')]
    owned = (decision.get('type') == 'list' and _ids(decision.get('filter')) == _ids(expected_filter)
             and [unquote(x) for x in visible] == [unquote(props[k]) for k in ('next_step', 'due_date', 'project_id')]
             and _ids(decision.get('sorts', [])) == _ids([{'property': props['due_date'], 'direction': 'ascending'}]))
    # Resume after creating the board and checkpointing its identity.
    resumed = (decision.get('type') == 'board' and props.get('decision') and
               unquote(decision.get('configuration', {}).get('group_by', {}).get('property_id') or '') == unquote(props['decision']))
    if not owned and not resumed:
        raise RuntimeError('Decision view was customized; preserve it and review its migration before installing')
    name = schema.prop('task', 'decision').name(lang)
    known = props.get('decision')
    matching = next((v for k,v in ds['properties'].items() if v.get('id') == known or k == name), None)
    if matching and matching.get('type') != 'select':
        raise RuntimeError(f'{name}: existing property is not a select; cannot migrate safely')
    if matching and not known:
        options = {v['name'] for v in matching.get('select', {}).get('options', [])}
        if options != set(schema.labels('decision', lang).values()):
            raise RuntimeError(f'{name}: existing property has unknown ownership/options')
    return {'home': home, 'data_source': ds, 'decision_view': decision, 'views': views,
            'lang': lang, 'home_blocks': client.block_children(home), 'changes': [
                'Add the Decision select: Pending / Cancel / Done / My to-do / Assign to Agent.',
                'Add a draggable Decisions board as the first tab in the same database; retain the original list as a fallback.',
                'Add All open tasks, with its heading, after the Decisions board on the home page; retain existing views, comments and custom content.',
                'Initialize empty decisions on existing decision tasks as Pending; do not submit decisions or delegations.']}


def _choices_plan(conn, client):
    """Replace Agree / Decline with the board's four choices; an old option still on a page is
    kept (removing it would clear that page's choice)."""
    ds_id = get_meta(conn, 'notion.tasks_data_source_id')
    prop_id = loads(get_meta(conn, 'notion.props.task'), {}).get('decision')
    if not ds_id or not prop_id:
        return None
    lang = get_meta(conn, 'notion.language') or 'en'
    ds = client.retrieve_data_source(ds_id)
    prop = next((v for v in ds['properties'].values() if unquote(v.get('id') or '') == unquote(prop_id)), None)
    if not prop or prop.get('type') != 'select':
        raise RuntimeError('Decision property is missing or not a select; cannot update its choices safely')
    current = [o['name'] for o in prop['select'].get('options', [])]
    wanted = list(schema.labels('decision', lang).values())
    if current[:len(wanted)] == wanted:
        return None
    used = set()
    for page in client.query_data_source(ds_id):
        option = ((page.get('properties') or {}).get(prop['name']) or {}).get('select')
        if option:
            used.add(option.get('name'))
    keep = [n for n in current if n not in wanted and n in used]
    names = lambda values: ' / '.join(schema.label('decision', v, lang) for v in values)
    return {'kind': 'choices', 'data_source': ds_id, 'property': prop['id'], 'old_options': current,
            'options': [{'name': n, 'color': schema.COLORS['decision'][v]}
                        for v, n in schema.labels('decision', lang).items()] + [{'name': n} for n in keep],
            'changes': [f"Decision board choices: {names(('cancel', 'done', 'todo', 'agent'))}"
                        f" (replacing {names(('agree', 'decline'))}); a handled card leaves the board."]
                       + ([f"Keep {', '.join(keep)} while a task still has it."] if keep else [])}


def _apply_choices(conn, client, home: Path, proposed):
    backup = home / 'backups' / 'notion-decision-ui-v2.json'
    if not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(json.dumps({'at': clock.iso(), **proposed}, ensure_ascii=False, indent=2), encoding='utf-8')
    client.update_data_source(proposed['data_source'], {proposed['property']: {'select': {'options': proposed['options']}}})
    save_state(conn, decision_ui_version=VERSION)
    return str(backup)


def apply(conn, client, home: Path):
    proposed = plan(conn, client)
    if not proposed:
        return None
    if proposed.get('kind') == 'choices':
        return _apply_choices(conn, client, home, proposed)
    # Snapshot precedes all remote writes; reruns never replace the original snapshot.
    backup = home / 'backups' / 'notion-decision-ui-v1.json'
    if not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        temporary = backup.with_suffix('.json.tmp')
        with temporary.open('w', encoding='utf-8') as f:
            json.dump({'at': clock.iso(), **proposed}, f, ensure_ascii=False, indent=2)
        temporary.replace(backup)
    else:
        json.loads(backup.read_text(encoding='utf-8'))  # Refuse a damaged rollback snapshot.
    lang = proposed['lang']
    ds_id = proposed['data_source']['id']
    props = loads(get_meta(conn, 'notion.props.task'), {})
    name = schema.prop('task', 'decision').name(lang)
    mapping = schema.PropertyMap.from_data_source('task', proposed['data_source'], props, lang)
    if 'decision' not in mapping.ids:
        client.update_data_source(ds_id, {name: schema.data_source_properties('task',
            get_meta(conn, 'notion.projects_data_source_id'), lang)[name]})
    mapping = schema.PropertyMap.from_data_source('task', client.retrieve_data_source(ds_id), props, lang)
    save_state(conn, **{'props.task': mapping.ids})
    specs = {s['key']: s for s in setup.view_specs(mapping.ids,
                loads(get_meta(conn, 'notion.props.project'), {}), lang)}
    decision = specs['decision']
    desired = {k: decision[k] for k in ('type', 'filter', 'sorts', 'configuration')}
    old = proposed['decision_view']
    if old.get('type') != 'board':
        database_id = (old.get('parent') or {}).get('database_id')
        if not database_id:
            raise RuntimeError('Decision view has no parent database identity')
        view = _create_once(conn, client, 'board', {'database_id': database_id}, {
            'database_id': database_id, 'data_source_id': ds_id, 'position': {'type': 'start'},
            'name': decision['name'], **desired})
        save_state(conn, decision_legacy_view=old['id'])
        proposed['views']['decision'] = view['id']
        save_state(conn, views=proposed['views'])
    # Only initialize blank selections on current remote decision tasks; never overwrite a drag.
    for page in client.query_data_source(ds_id):
        vals, _ = schema.parse_page('task', page, mapping)
        if vals.get('status') == 'decision' and not vals.get('decision'):
            client.update_page(page['id'], {mapping.ids['decision']:
                schema.render_value(schema.prop('task', 'decision'), 'pending', lang)})
            linked = conn.execute('SELECT last_seen_props FROM notion_pages WHERE page_id=?', (page['id'],)).fetchone()
            if linked:
                seen = {**loads(linked['last_seen_props'], {}), 'decision': 'pending'}
                conn.execute('UPDATE notion_pages SET last_seen_props=? WHERE page_id=?', (dumps(seen), page['id']))
    views = proposed['views']
    if 'all' not in views:
        spec = specs['all']
        placement = {'parent': {'type': 'page_id', 'page_id': proposed['home']}}
        heading = _all_heading(client, proposed, old, lang)
        if heading:
            placement['position'] = {'type': 'after_block', 'block_id': heading}
        view = _create_once(conn, client, 'all', {'data_source_id': ds_id}, {
            'create_database': placement,
            'data_source_id': ds_id, **{k:spec[k] for k in ('name','type','filter','sorts','configuration')}})
        views['all'] = view['id']
        save_state(conn, views=views)
    save_state(conn, decision_ui_version='1')
    later = _choices_plan(conn, client)
    if later:
        _apply_choices(conn, client, home, later)
    else:
        save_state(conn, decision_ui_version=VERSION)
    return str(backup)


def _all_heading(client, proposed, decision, lang):
    """The All open tasks heading after the decision board's block (else the first block); an
    existing heading of that text is reused, so a lost response is never repeated."""
    text = i18n.t('home.section.all', lang)
    blocks = proposed['home_blocks']
    found = [b for b in blocks if _heading_text(b) == text]
    if found or not blocks:
        return found[0]['id'] if found else None
    board = _bare((decision.get('parent') or {}).get('database_id'))
    after = next((b['id'] for b in blocks if _bare(b['id']) == board), blocks[0]['id'])
    created = client.append_children(proposed['home'], [{'object': 'block', 'type': 'heading_2', 'heading_2': {
        'rich_text': [{'type': 'text', 'text': {'content': text}}]}}], {'type': 'after_block', 'after_block': {'id': after}})
    return next(b['id'] for b in created if _heading_text(b) == text)


def _heading_text(block):
    kind = block.get('type', '')
    if not kind.startswith('heading_'):
        return None
    return ''.join(t.get('plain_text') or t.get('text', {}).get('content', '') for t in block[kind].get('rich_text', [])).strip()


def _bare(block_id):
    return (block_id or '').replace('-', '')


def _create_once(conn, client, key, query, payload):
    """Small installation checkpoint; uncertain native creates are never blindly replayed."""
    marker = 'decision_ui_create_' + key
    prior = loads(get_meta(conn, 'notion.' + marker), None)
    live = client.list_views(**query)
    if prior is None:
        prior = {'before': [v['id'] for v in live], 'uncertain': False}
        save_state(conn, **{marker: prior})
    new_views = [client.retrieve_view(v['id']) for v in live if v['id'] not in prior['before']]
    candidates = [v for v in new_views if
                  v.get('name') == payload['name'] and v.get('type') == payload['type'] and
                  _ids(v.get('filter')) == _ids(payload.get('filter'))]
    if len(candidates) > 1:
        raise RuntimeError('Multiple migration views found; preserve them and inspect before retrying')
    if candidates:
        return client.retrieve_view(candidates[0]['id'])
    if prior['uncertain']:
        raise RuntimeError('Notion view creation outcome is unknown; retry installation for read-only reconciliation')
    try:
        return client.create_view(payload)
    except NotionUncertain as exc:
        save_state(conn, **{marker: {**prior, 'uncertain': True}})
        if exc.committed_id:
            return client.retrieve_view(exc.committed_id)
        raise


def _ids(value):
    """Notion view responses decode property IDs, while data source responses encode them."""
    if isinstance(value, list):
        return [_ids(v) for v in value]
    if isinstance(value, dict):
        return {k: unquote(v) if k in ('property', 'property_id') and isinstance(v, str)
                else _ids(v) for k,v in value.items()}
    return value
