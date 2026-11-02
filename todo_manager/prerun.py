"""Fixed Hermes pre-run interface. No model, dispatch loop or CLI command registry."""
import sys
from typing import Any
from . import config, db
from .locks import file_lock, lock_path, LockBusy


def run(entry: str, home=None) -> int:
    if entry not in ('intake', 'feedback'):
        raise ValueError(entry)
    conn = None
    try:
        cfg = config.load(home)
        with file_lock(lock_path(cfg.home, 'workspace'), blocking=True, shared=True), file_lock(lock_path(cfg.home, entry)):
            conn = db.open_existing(cfg.db_path)
            output = prepare(entry, conn, cfg)
    except LockBusy:
        output = '{"wakeAgent": false}'
    except Exception as exc:
        print(f'Todo pre-run {entry} failed: {type(exc).__name__}: {str(exc)[:500]}', file=sys.stderr)
        return 1
    finally:
        if conn:
            conn.close()
    print(output)
    return 0


def intake(home=None):
    return run('intake', home)


def feedback(home=None):
    return run('feedback', home)

def prepare(entry: str, conn: Any, cfg: config.Config) -> str:
    from . import wiring
    if entry == "intake":
        from .gate_intake import run_gate
        from .sources.registry import commit_sources, conversation_sources
        return run_gate(conn, cfg, conversation_sources(cfg, conn), commit_sources(cfg))[0]
    from .gate_feedback import run_gate as run_feedback
    return run_feedback(conn, cfg, notion=wiring.notion_sync(conn, cfg), kanban=wiring.kanban(cfg))[0]

