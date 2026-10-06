"""Notion/child-card association only. Hermes owns execution and dependencies."""
from __future__ import annotations

import sqlite3
from typing import Any

from .config import Config
from . import clock, effects, ids, i18n
from .db import dumps, loads, tx
from .kanban import Kanban, KanbanError, transition_verdict


def sync(conn: sqlite3.Connection, cfg: Config, kb: Kanban, d: dict[str, Any], quiet: bool = False) -> None:
    from . import delegation as dg
    failures = []
    closed_note = '\n' + i18n.t('dlg.child_closed') if d['status'] == 'ended' else ''
    for card in kb.owned(d['card_id']):
        row = conn.execute('SELECT * FROM delegation_children WHERE delegation_id=? AND card_id=?',
                           (d['id'], card)).fetchone()
        try:
            snap = kb.snapshot(card, after_event=row['event_cursor'] if row else 0)
        except KanbanError as exc:
            failures.append(f'{card}: {exc}')
            continue
        dg.repair_notification(conn, cfg, kb, d, card)
        cursor = row['event_cursor'] if row else 0
        with tx(conn):
            for ev in snap['events']:
                cursor = max(cursor, ev['id'])
                payload = ev.get('payload') or {}
                if ev['kind'] in ('completed', 'review_requested'):
                    run = dg._event_run(snap, ev)
                    payload = {**payload, 'run': run, 'attachments': snap.get('attachments', [])}
                inserted = conn.execute('INSERT OR IGNORE INTO delegation_child_events VALUES (?,?,?,?,?)',
                                        (d['id'], card, ev['id'], ev['kind'], dumps(payload))).rowcount
                if not inserted:
                    continue
                if ev['kind'] in ('blocked', 'block_loop_detected'):
                    reason = payload.get('reason') or 'Child needs input; inspect the card.'
                    if reason.startswith(dg.PAUSE_MARK) or payload.get('kind') in ('dependency', 'transient'):
                        continue
                    failed = payload.get('kind') == 'capability'
                    dlv = dg._record_delivery(conn, {**d, 'card_id': card}, kind='failure' if failed else 'question', summary=reason,
                                              body=reason, seq=ev['id'], event_id=ev['id'],
                                              run_id=str(ev.get('run_id') or ''), after_cancel=quiet)
                    if dlv and not quiet:
                        effects.task_comment(conn, d['task_id'], f'dlv:{dlv}',
                                             i18n.t("dlg.child_failure" if failed else "dlg.child_question", card=card, reason=reason, delivery=dlv) + closed_note)
                elif ev['kind'] == 'gave_up':
                    reason = payload.get('error') or 'gave_up'
                    dlv = dg._record_delivery(conn, {**d, 'card_id': card}, kind='failure', summary=reason,
                                              body=reason, seq=ev['id'], event_id=ev['id'],
                                              run_id=str(ev.get('run_id') or ''), after_cancel=quiet)
                    if dlv and not quiet:
                        effects.task_comment(conn, d['task_id'], f'dlv:{dlv}',
                                             i18n.t('dlg.child_failure', card=card, reason=reason, delivery=dlv) + closed_note)
                elif ev['kind'] in ('completed', 'review_requested') and not quiet:
                    effects.task_comment(conn, d['task_id'], f"child:{card}:{ev['id']}",
                                         i18n.t("dlg.child_result", card=card, summary=payload.get("summary") or ""))
            attention = None
            if snap['task']['status'] in ('blocked', 'triage'):
                latest = conn.execute("SELECT kind,payload FROM delegation_child_events WHERE delegation_id=? AND card_id=? "
                                      "AND kind IN ('blocked','block_loop_detected','gave_up','dependency_wait') "
                                      "ORDER BY event_id DESC LIMIT 1", (d['id'], card)).fetchone()
                data = loads(latest['payload'], {}) if latest else {}
                reason = data.get('reason') or ''
                block = snap['task'].get('block_kind')
                if latest and (latest['kind'] == 'gave_up' or block == 'capability'):
                    attention = 'failure'
                elif (latest and latest['kind'] != 'dependency_wait' and block in (None, 'needs_input')
                      and reason != 'initial_status' and not reason.startswith(dg.PAUSE_MARK)):
                    attention = 'question'
            conn.execute('INSERT INTO delegation_children (delegation_id,card_id,event_cursor,status,attention) '
                         'VALUES (?,?,?,?,?) ON CONFLICT(delegation_id,card_id) '
                         'DO UPDATE SET event_cursor=excluded.event_cursor,status=excluded.status,attention=excluded.attention',
                         (d['id'], card, cursor, snap['task']['status'], attention))
            if not quiet and (not row or row['attention'] != attention):
                effects.task_push(conn, d['task_id'])

    refresh_attention(conn, d, quiet)
    if failures:
        raise KanbanError('; '.join(failures))
    with tx(conn):
        dg._set(conn, d['id'], children_sync_pending=0)
        scope = f'child_sync:{d["id"]}'
        if conn.execute("SELECT 1 FROM cursors WHERE source='kanban' AND scope=?", (scope,)).fetchone():
            from . import health
            health.record_channel(conn, 'kanban', scope, None)


def refresh_attention(conn: sqlite3.Connection, d: dict[str, Any], quiet: bool = False) -> None:
    from . import delegation as dg
    needs_input = conn.execute("SELECT 1 FROM delegation_children c JOIN delegations owner ON owner.id=c.delegation_id "
                               "WHERE owner.task_id=? AND owner.status NOT IN ('cancelled','quarantined') "
                               "AND c.attention IS NOT NULL", (d['task_id'],)).fetchone()
    if needs_input and not quiet:
        with tx(conn):
            dg._next_actor(conn, d['id'], 'me', i18n.t("dlg.child_needs_input"))
    elif (not quiet and dg.get(conn, d['id'])['status'] in dg.EXECUTING and not d['pause_requested']
          and dg.task_label(conn, d['task_id']) not in dg.NEEDS_USER_LABELS):
        has_children = conn.execute('SELECT 1 FROM delegation_children WHERE delegation_id=?', (d['id'],)).fetchone()
        if has_children:
            with tx(conn):
                dg._next_actor(conn, d['id'], 'agent', None)


def queue_answer(conn: sqlite3.Connection, d: dict[str, Any], question: dict[str, Any], body: str,
                 key: str | None, feedback_id: str | None) -> dict[str, Any]:
    from . import delegation as dg
    d = dg.get(conn, d['id'])
    if d['status'] in ('cancelled', 'ended') or dg.cancel_pending(conn, d):
        raise dg.DelegationError('delegation is closed or cancelling; cancel remaining owned work before creating a follow-up')
    existing = conn.execute('SELECT id FROM delegation_messages WHERE op_key=?', (key,)).fetchone()
    if existing:
        return {'message': existing['id'], 'question': question['id']}
    row = conn.execute("SELECT * FROM delegation_children WHERE delegation_id=? AND card_id=?",
                       (d['id'], question['card_id'])).fetchone()
    latest = conn.execute("SELECT MAX(event_id) FROM delegation_child_events WHERE delegation_id=? AND card_id=? "
                          "AND kind IN ('blocked','block_loop_detected','gave_up')", (d['id'], question['card_id'])).fetchone()[0]
    if (not row or row['status'] not in ('blocked', 'triage') or row['attention'] not in ('question', 'failure')
            or latest != question['source_event_id']):
        raise dg.DelegationError('question is stale or no longer open; no answer queued')
    mid = ids.new_id('event')
    conn.execute("INSERT INTO delegation_messages (id,delegation_id,kind,body,feedback_id,requirement_version,"
                 "target_event_id,target_card_id,status,created_at,op_key) VALUES (?,?,'answer',?,?,?,?,?,'pending',?,?)",
                 (mid, d['id'], body, feedback_id, d['requirement_version'], question['source_event_id'],
                  question['card_id'], clock.iso(), key))
    return {'message': mid, 'question': question['id'], 'card': question['card_id'], 'status': 'pending'}


def deliver_answer(conn: sqlite3.Connection, cfg: Config, kb: Kanban, d: dict[str, Any], msg: dict[str, Any]) -> str:
    from . import delegation as dg
    card = msg['target_card_id']
    if dg.cancel_pending(conn, d) or d['status'] in ('cancelled', 'ended'):
        dg._finish(conn, msg['id'], 'superseded', 'delegation closed or cancelling')
        return 'superseded'
    if d['pause_requested']:
        return 'waiting'
    if card not in kb.owned(d['card_id']):
        raise KanbanError('child ownership cannot be verified; no message sent')
    snap = kb.snapshot(card)
    anchor = msg['target_event_id']
    verdict = transition_verdict(snap['task']['status'], snap['events'], 'unblock', anchor)
    child = {**d, 'card_id': card}
    if verdict == 'mismatch' or (verdict == 'already' and not dg._has_marker(snap, msg['id'])):
        dg._finish(conn, msg['id'], 'superseded', 'question already answered or replaced')
        return 'superseded'
    dg._sending(conn, msg['id'])
    dg._post_comment(conn, kb, child, msg, dg._message_text(conn, msg), snap)
    if verdict == 'apply':
        result = kb.transition(card, 'unblock', anchor)
        if result['outcome'] in ('mismatch', 'raced'):
            dg._finish(conn, msg['id'], 'failed', 'native transition raced with a newer question')
            effects.task_comment(conn, d['task_id'], f"child-race:{msg['id']}",
                                 i18n.t("dlg.child_answer_race", card=card))
            return 'failed'
    dg._finish(conn, msg['id'], 'sent')
    try:
        # Refresh from native events instead of clearing a potentially newer question.
        sync(conn, cfg, kb, d)
    except KanbanError as exc:
        from . import health
        with tx(conn):
            dg._set(conn, d['id'], children_sync_pending=1)
        health.record_channel(conn, 'kanban', f'child_sync:{d["id"]}', str(exc))
    return 'sent'


def cancel_owned(conn: sqlite3.Connection, kb: Kanban, d: dict[str, Any]) -> bool:
    """Root is archived first. Only proven descendants are archived, never graph dependencies.

    A bounded second observation catches children committed while workers were terminating;
    continued creation is an unconfirmed cancellation, never a new resident coordinator.
    """
    confirmed = True
    seen = set()
    for _ in range(2):
        owned = set(kb.owned(d['card_id']))
        for card in sorted(owned - seen):
            result = kb.transition(card, 'archive')
            term = result.get('termination')
            confirmed &= result['status'] == 'archived' and (term is None or bool(term.get('terminated')))
            seen.add(card)
    return confirmed and set(kb.owned(d['card_id'])) <= seen
