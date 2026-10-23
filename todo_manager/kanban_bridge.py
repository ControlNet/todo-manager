"""Bridge to the Hermes Kanban Python API. Runs under the Hermes interpreter, e.g.

    ~/.hermes/hermes-agent/venv/bin/python kanban_bridge.py < request.json

Request: {"op": ..., "board": ..., ...}. Response (last stdout line, prefixed with
``@@TM@@``): {"ok": true, "result": ...} or {"ok": false, "error": ...}; ``"uncertain": true``
(code PARTIAL) when it failed after a write, e.g. a create whose card exists.

Only formal state functions are used (create_task, block_task, unblock_task,
specify_triage_task, reopen_review_task, complete_task, archive_task, add_comment), never
direct status UPDATEs, so Kanban events and sticky-block semantics stay intact. Lookups by
idempotency key read the tasks table directly so archived cards are found too.

This file must stay import-light and stdlib + Hermes only.
"""

from __future__ import annotations

import json
import os
import sys
import traceback

MARK = "@@TM@@"

# Mirror of todo_manager.kanban.TRANSITIONS / transition_verdict / transition_raced (this file cannot import
# the package); tests/integration checks both against the installed Hermes code.
TRANSITIONS = {
    "unblock": {"anchors": ("blocked", "block_loop_detected", "gave_up"), "from": ("blocked", "triage"),
                "done_by": ("unblocked", "specified"), "final": ()},
    "reopen_review": {"anchors": ("review_requested",), "from": ("review",), "done_by": ("review_reopened",),
                      "final": ()},
    "complete": {"anchors": ("review_requested",), "from": ("review",), "done_by": ("completed",),
                 "final": ("done",)},
    "archive": {"anchors": (), "from": None, "done_by": ("archived",), "final": ("archived",)},
}


def transition_verdict(status, events, action, anchor):
    spec = TRANSITIONS[action]
    if status in spec["final"]:
        return "already"
    if anchor is not None:
        if any(e.kind in spec["done_by"] for e in events if e.id > int(anchor)):
            return "already"
        latest = max((e.id for e in events if e.kind in spec["anchors"]), default=None)
        if latest != int(anchor):
            return "mismatch"
    if spec["from"] is not None and status not in spec["from"]:
        return "mismatch"
    return "apply"


def transition_raced(events, action, anchor):
    if anchor is None:
        return False
    spec = TRANSITIONS[action]
    done = [e.id for e in events if e.kind in spec["done_by"] and e.id > int(anchor)]
    if not done:
        return False
    ours = max(done)
    return any(e.kind in spec["anchors"] and int(anchor) < e.id < ours for e in events)


def _out(payload: dict) -> None:
    sys.stdout.write(MARK + json.dumps(payload, default=str) + "\n")
    sys.stdout.flush()


def _task_dict(task) -> dict:
    keys = ("id", "title", "status", "assignee", "tenant", "created_by", "created_at", "started_at", "completed_at",
            "workspace_kind", "workspace_path", "idempotency_key", "current_run_id", "block_kind",
            "block_recurrences", "consecutive_failures", "max_retries", "last_failure_error", "result",
            "worker_pid")
    return {k: getattr(task, k, None) for k in keys}


def owned_cards(kb, conn, root):
    """Lineage from immutable creation events, never dependency edges."""
    parents = {}
    for task in kb.list_tasks(conn, include_archived=True):
        for event in kb.list_events(conn, task.id):
            if event.kind == "created":
                payload = event.payload or {}
                parent = payload.get("creator_task_id") or payload.get("from_decompose_of")
                if parent:
                    parents[task.id] = parent
                break
    owned = {root}
    while True:
        more = {child for child, parent in parents.items() if parent in owned}
        if more <= owned:
            return sorted(owned - {root})
        owned |= more


def is_staged(kb, conn, tid):
    """Our card that never ran: still in the creation staging block (``initial_status``)."""
    task = kb.get_task(conn, tid)
    return (task.status == "blocked" and task.created_by == "todo-manager"
            and all(e.kind == "created" or (e.kind == "blocked" and (e.payload or {}).get("reason") == "initial_status")
                    for e in kb.list_events(conn, tid)))


def park(kb, conn, tid, until):
    """``scheduled`` until ``until`` (Hermes' own convention for the reason); only a staged card."""
    if is_staged(kb, conn, tid):
        kb.schedule_task(conn, tid, reason=f"SCHEDULED_UNTIL={until} (todo-manager starts it then)")
    return kb.get_task(conn, tid).status


def ensure_notify(kb, conn, tid, route, release=False):
    from hermes_cli.kanban_db_notify import add_notify_sub, list_notify_subs
    existing = list_notify_subs(conn, tid)
    found = any(s["platform"] == route["platform"] and s["chat_id"] == route["chat_id"]
                and (s.get("thread_id") or "") == (route.get("thread_id") or "") for s in existing)
    events = kb.list_events(conn, tid)
    # Only release our initial staging block; never lift a worker/human block on recovery.
    staged = is_staged(kb, conn, tid)
    if not found:
        add_notify_sub(conn, task_id=tid, delivery_mode="notify", **route)
    if release and staged:
        kb.unblock_task(conn, tid)
    gap = not found and not staged and any(e.kind in ("completed", "blocked", "review_requested", "gave_up") for e in events)
    return {"registered": True, "history_gap": gap, "status": kb.get_task(conn, tid).status}


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != here]
    req = json.loads(sys.stdin.read() or "{}")
    repo = req.get("hermes_repo")
    if repo and repo not in sys.path:
        sys.path.insert(0, repo)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_ATTACHMENTS_ROOT"):
        os.environ.pop(var, None)
    # Load the sibling explicitly: the bridge removes its directory to avoid shadowing Hermes modules.
    import importlib.util
    spec = importlib.util.spec_from_file_location('tm_hermes_diagnostics', os.path.join(here, 'hermes_diagnostics.py'))
    diagnostics = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diagnostics)
    if req['op'] == 'cron_health':
        from hermes_constants import get_hermes_home
        return _out({'ok': True, 'result': diagnostics.cron_health(str(get_hermes_home()), bool(req.get('active')))})
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing

    op = req["op"]
    board = req.get("board")
    if not board or board == "default":
        raise ValueError("an explicit non-default board is required")

    if op == 'profile_valid':
        from hermes_cli.kanban_db_dispatch import _profile_exists_fn
        check = _profile_exists_fn()
        name = req.get('name')
        try:
            valid = bool(name and check and check(name))
        except ValueError:
            valid = False
        return _out({'ok': True, 'result': {'valid': valid}})
    if op == "ping":
        names = ["create_task", "get_task", "block_task", "unblock_task", "specify_triage_task", "reopen_review_task",
                 "complete_task", "archive_task", "add_comment", "list_events", "list_runs", "list_comments_after"]
        return _out({"ok": True, "result": {"missing": [n for n in names if not hasattr(kb, n)],
                                            **diagnostics.board_status(kb, board)}})
    if op == "context_status":
        from pathlib import Path
        from agent.prompt_builder import discover_context_files
        from hermes_cli.config import load_config
        from tools.memory_tool import get_builtin_memory_store_flags
        from hermes_constants import get_hermes_home
        config = load_config() or {}
        flags = get_builtin_memory_store_flags(config)
        from tools.skills_tool import skill_view
        from agent.skill_utils import normalize_skill_lookup_name
        from agent.skill_bundles import resolve_bundle_command_key
        missing_skills = [name for name in req.get("skills", [])
                          if not resolve_bundle_command_key(name.lstrip("/"))
                          and not json.loads(skill_view(normalize_skill_lookup_name(name))).get("success")]
        candidates = discover_context_files(Path(req["workdir"]).resolve())
        winning = next((kind for kind, label, path, content in candidates if content.strip()), None)
        files = [str(path) for kind, label, path, content in candidates if kind == winning and content.strip()]
        return _out({"ok": True, "result": {"project_kind": winning, "project_files": files,
            "memory_enabled": flags[0], "user_profile_enabled": flags[1], "missing_skills": missing_skills,
            "profile_home": str(get_hermes_home()),
            "memory_tool_disabled": "memory" in config.get("agent", {}).get("disabled_toolsets", [])}})
    if op == "settings":
        # Read-only: the global switches the board protocol depends on, resolved the way the
        # installed dispatcher/gateway resolve them (defaults included).
        from hermes_cli.config import load_config
        from hermes_cli.kanban_db_dispatch import review_dispatch_enabled
        from tools.memory_tool import get_builtin_memory_store_flags
        try:
            from gateway.kanban_watchers_common import _resolve_auto_decompose_settings
            auto_decompose = _resolve_auto_decompose_settings(load_config)[0]
        except ImportError:
            auto_decompose = bool(((load_config() or {}).get("kanban") or {}).get("auto_decompose", True))
        return _out({"ok": True, "result": {"review_dispatch": bool(review_dispatch_enabled()),
                                            "auto_decompose": bool(auto_decompose),
                                            "memory_flags": list(get_builtin_memory_store_flags(load_config() or {}))}})
    board_info = diagnostics.board_status(kb, board)
    if board_info['board_state'] not in ('available', 'missing'):
        return _out({'ok': False, 'error': board_info['reason'],
                     'code': 'BOARD_' + board_info['board_state'].upper()})
    if op == "create_board":
        meta = kb.create_board(board, name=req.get("name"), description=req.get("description"))
        return _out({"ok": True, "result": meta})
    if not kb.board_exists(board):
        return _out({"ok": False, "error": f"board {board!r} does not exist", "code": "NO_BOARD"})

    with connect_closing(board=board) as conn:
        if op == "find":
            rows = conn.execute(
                "SELECT id, status, title, idempotency_key, created_at FROM tasks"
                " WHERE idempotency_key = ? OR (body IS NOT NULL AND instr(body, ?) > 0) ORDER BY created_at",
                (req["idem_key"], req["marker"])).fetchall()
            return _out({"ok": True, "result": [dict(r) for r in rows]})
        if op == "owned":
            return _out({"ok": True, "result": owned_cards(kb, conn, req["task_id"])})
        if op == "notify":
            return _out({"ok": True, "result": ensure_notify(kb, conn, req["task_id"], req["route"], req.get("release", False))})
        if op == "create":
            from hermes_cli.kanban_db_dispatch import _profile_exists_fn
            check = _profile_exists_fn()
            if not req.get("assignee") or check is None or not check(req["assignee"]):
                return _out({"ok": False, "error": "assignee is not a dispatchable Hermes profile", "code": "NO_PROFILE"})
            tid = kb.create_task(
                conn, title=req["title"], body=req["body"], assignee=req.get("assignee") or None,
                created_by=req.get("created_by", "todo-manager"), tenant=req.get("tenant"),
                idempotency_key=req["idem_key"], workspace_kind=req.get("workspace_kind"),
                workspace_path=req.get("workspace_path"), skills=req.get("skills"),
                max_runtime_seconds=req.get("max_runtime_seconds"), max_retries=req.get("max_retries"),
                initial_status="blocked" if req.get("notify") or req.get("start_after") else "running", board=board)
            try:
                if req.get("notify"):
                    ensure_notify(kb, conn, tid, req["notify"], release=not req.get("start_after"))
                if req.get("start_after"):
                    park(kb, conn, tid, req["start_after"])
            except Exception as exc:  # noqa: BLE001 - the card is written: the caller must look it up
                return _out({"ok": False, "error": f"card {tid} created, then {type(exc).__name__}: {exc}",
                             "code": "PARTIAL", "uncertain": True})
            return _out({"ok": True, "result": tid})
        if op == "schedule":
            return _out({"ok": True, "result": park(kb, conn, req["task_id"], req["until"])})
        if op == "release_schedule":
            tid = req["task_id"]
            if kb.get_task(conn, tid).status == "scheduled" or is_staged(kb, conn, tid):
                kb.unblock_task(conn, tid)
            return _out({"ok": True, "result": kb.get_task(conn, tid).status})
        if op == "snapshot":
            task = kb.get_task(conn, req["task_id"])
            if task is None:
                return _out({"ok": False, "error": "card not found", "code": "NO_CARD"})
            after_event = int(req.get("after_event") or 0)
            events = [e for e in kb.list_events(conn, req["task_id"]) if e.id > after_event]
            comments = kb.list_comments_after(conn, req["task_id"], after_id=int(req.get("after_comment") or 0))
            run_rows = kb.list_runs(conn, req["task_id"])
            atts = kb.list_attachments(conn, req["task_id"]) if hasattr(kb, "list_attachments") else []
            return _out({"ok": True, "result": {
                "task": _task_dict(task),
                "events": [{"id": e.id, "kind": e.kind, "payload": e.payload, "created_at": e.created_at,
                            "run_id": e.run_id} for e in events],
                "comments": [{"id": c.id, "author": c.author, "body": c.body, "created_at": c.created_at}
                             for c in comments],
                "runs": [{"id": r.id, "status": r.status, "outcome": r.outcome, "summary": r.summary,
                          "metadata": r.metadata, "error": r.error, "started_at": r.started_at,
                          "ended_at": r.ended_at, "profile": r.profile} for r in run_rows],
                "attachments": [{"id": a.id, "filename": a.filename, "path": a.stored_path, "size": a.size}
                                for a in atts]}})
        if op == "comment":
            author = req.get("author", "todo-manager")
            cid = kb.add_comment(conn, req["task_id"], author, req["body"])
            # add_comment writes the comment and its "commented" event in one transaction; our
            # comments are posted one at a time (kanban-submit lock), so the newest commented
            # event by this author is the one just written.
            event_id = None
            for row in conn.execute("SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'commented'"
                                    " ORDER BY id DESC LIMIT 20", (req["task_id"],)):
                payload = json.loads(row["payload"] or "{}") if isinstance(row["payload"], str) else (row["payload"] or {})
                if payload.get("author") == author:
                    event_id = row["id"]
                    break
            return _out({"ok": True, "result": {"comment_id": cid, "event_id": event_id}})
        if op == "block":
            ok = kb.block_task(conn, req["task_id"], reason=req.get("reason"), kind=req.get("kind"))
            return _out({"ok": True, "result": bool(ok)})
        if op == "unblock":
            return _out({"ok": True, "result": bool(kb.unblock_task(conn, req["task_id"]))})
        if op == "respecify":
            ok = kb.specify_triage_task(conn, req["task_id"], author=req.get("author", "todo-manager"))
            return _out({"ok": True, "result": bool(ok)})
        if op == "reopen_review":
            return _out({"ok": True, "result": bool(kb.reopen_review_task(conn, req["task_id"]))})
        if op == "complete":
            ok = kb.complete_task(conn, req["task_id"], result=req.get("result"), summary=req.get("summary"),
                                  force=bool(req.get("force")))
            return _out({"ok": True, "result": bool(ok)})
        if op == "archive":
            return _out({"ok": True, "result": bool(kb.archive_task(conn, req["task_id"]))})
        if op == "transition":
            # Check and apply in one process, right before the Hermes state function (which
            # re-checks the status in its own transaction). They cannot share one transaction:
            # write_txn refuses nesting and complete_task & co. must not run under an outer
            # transaction. A blocked/review card has no worker, so only a human dashboard action
            # can interleave between the check and the call; that is detected from the events
            # afterwards and reported as "raced" (applied to a newer question/version).
            tid, action, anchor = req["task_id"], req["action"], req.get("anchor")
            task = kb.get_task(conn, tid)
            if task is None:
                return _out({"ok": False, "error": "card not found", "code": "NO_CARD"})
            verdict = transition_verdict(task.status, kb.list_events(conn, tid), action, anchor)
            outcome = verdict
            if verdict == "apply":
                if action == "unblock":
                    ok = (kb.specify_triage_task(conn, tid, author="todo-manager") if task.status == "triage"
                          else kb.unblock_task(conn, tid))
                elif action == "reopen_review":
                    ok = kb.reopen_review_task(conn, tid)
                elif action == "complete":
                    ok = kb.complete_task(conn, tid, result=req.get("summary"), summary=req.get("summary"))
                else:
                    ok = kb.archive_task(conn, tid)
                after = kb.get_task(conn, tid)
                events = kb.list_events(conn, tid)
                outcome = ("raced" if transition_raced(events, action, anchor) else "done") if ok else (
                    "already" if transition_verdict(after.status, events, action, anchor) == "already"
                    else "mismatch")
            task = kb.get_task(conn, tid)
            termination = None
            if action == "archive":
                term = [e for e in kb.list_events(conn, tid) if e.kind == "archive_worker_termination"]
                termination = term[-1].payload if term else None
            return _out({"ok": True, "result": {"outcome": outcome, "status": task.status,
                                                "termination": termination}})
        if op == "counts":
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
            return _out({"ok": True, "result": {r["status"]: r["n"] for r in rows}})
    return _out({"ok": False, "error": f"unknown op {op}"})


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - reported to the caller as data
        _out({"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-2000:]})
