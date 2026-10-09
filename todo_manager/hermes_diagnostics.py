"""Read-only native diagnostics, executed under the configured Hermes interpreter.

Hermes 0.21.6+187: cron.jobs ticker readers and cron.store_health.free_bytes.
Never call load_jobs/probe_report here: they may repair or write the native store.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def board_status(kb, board: str) -> dict:
    """Metadata identity without connecting, initializing or reviving the board."""
    path = kb.board_metadata_path(board)
    db_path = kb.kanban_db_path(board=board)
    exists = kb.board_exists(board)
    state, reason = 'available', None
    try:
        raw = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(raw, dict) or not isinstance(raw.get('archived', False), bool):
            raise ValueError('board metadata must be an object with a boolean archived flag')
        if raw.get('archived'):
            state, reason = 'archived', 'board is archived; inspect it in Hermes; no automatic recreation'
    except FileNotFoundError:
        state = 'orphaned' if db_path.exists() else 'missing'
        reason = 'board metadata is missing' + (' but its database remains; inspect native board recovery' if state == 'orphaned' else '')
    except (OSError, ValueError) as exc:
        state, reason = 'invalid', f'cannot validate board metadata: {type(exc).__name__}'
    return {'board_exists': exists, 'board_state': state, 'board_available': state == 'available',
            'reason': reason, 'db_path': str(db_path)}


def cron_health(home: str, active: bool) -> dict:
    # Importing cron executes package initialization. Confine its incidental files to a
    # scratch home; use_cron_store below routes ONLY the read APIs to the inspected home.
    previous = os.environ.get('HERMES_HOME')
    with tempfile.TemporaryDirectory(prefix='tm-native-inspection-') as scratch:
        try:
            os.environ['HERMES_HOME'] = scratch
            from cron.jobs import (use_cron_store, get_ticker_heartbeat_age, get_ticker_success_age,
                                   get_ticker_last_error, TICKER_INTERVAL_SECONDS)
            from cron.store_health import free_bytes, LOW_FREE_BYTES
        finally:
            if previous is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = previous
    directory = Path(home).expanduser() / 'cron'
    jobs = directory / 'jobs.json'
    problems, warnings = [], []
    out = {'store': str(directory), 'write_probe': False, 'writable': None, 'skipped_runs': None,
           'problems': problems, 'warnings': warnings,
           'diagnostic': 'Run hermes cron status in this Hermes profile for a native write probe; '
                         'cross-process skipped-run totals are not available here.'}
    if not directory.is_dir():
        out['state'] = 'not_installed'
        return out
    targets = {directory.resolve(), jobs.resolve().parent}
    out['free_bytes'] = {}
    for path in sorted(targets):
        available = free_bytes(path)
        out['free_bytes'][str(path)] = available
        if available is not None and available <= 0:
            problems.append(f'cron store has no free space: {path}')
        elif available is not None and available < LOW_FREE_BYTES:
            warnings.append(f'cron store has less than {LOW_FREE_BYTES} bytes free: {path}')
        if not os.access(path, os.W_OK | os.X_OK):
            problems.append(f'cron store directory is not writable/searchable: {path}')
        try:
            if hasattr(os, 'statvfs') and os.statvfs(path).f_flag & getattr(os, 'ST_RDONLY', 1):
                problems.append(f'cron store filesystem is read-only: {path}')
        except OSError as exc:
            warnings.append(f'cannot inspect cron filesystem {path}: {type(exc).__name__}')
    for path in (jobs.resolve(), directory / '.tick.lock'):
        if path.exists() and not os.access(path, os.W_OK):
            problems.append(f'cron store file is not writable: {path}')
    with use_cron_store(home):
        heartbeat = get_ticker_heartbeat_age()
        success = get_ticker_success_age()
        error = get_ticker_last_error()
    out['ticker'] = {'heartbeat_age_seconds': heartbeat, 'success_age_seconds': success, 'error': error}
    threshold = TICKER_INTERVAL_SECONDS * 3 + 20
    if active:
        if error:
            problems.append(f'native cron ticker error: {error}')
        if heartbeat is not None and heartbeat > threshold:
            problems.append(f'native cron ticker heartbeat is stale ({int(heartbeat)} seconds)')
        if success is not None and success > threshold:
            problems.append(f'native cron ticker last successful tick is stale ({int(success)} seconds)')
        if heartbeat is None:
            warnings.append('native cron ticker heartbeat is unavailable; scheduler liveness is unknown')
    if problems:
        out['state'] = 'attention'
    else:
        out['state'] = 'observed' if heartbeat is not None else 'unknown'
    warnings.append('No write probe performed; permissions, quota and the scheduler user may differ from this process.')
    return out
