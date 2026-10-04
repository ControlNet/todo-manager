"""Bridge to the Hermes Kanban Python API. Runs under the Hermes interpreter, e.g.

    ~/.hermes/hermes-agent/venv/bin/python kanban_bridge.py < request.json

Request: {"op": ..., "board": ..., ...}. Response (last stdout line, prefixed with
``@@TM@@``): {"ok": true, "result": ...} or {"ok": false, "error": ...}.

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
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing

    op = req["op"]
    board = req.get("board")
    if not board or board == "default":
        raise ValueError("an explicit non-default board is required")

    if op == "ping":
        names = ["create_task", "get_task", "block_task", "unblock_task", "specify_triage_task", "reopen_review_task",
                 "complete_task", "archive_task", "add_comment", "list_events", "list_runs", "list_comments_after"]
        return _out({"ok": True, "result": {"missing": [n for n in names if not hasattr(kb, n)],
                                            "board_exists": kb.board_exists(board),
                                            "db_path": str(kb.kanban_db_path(board=board))}})
    if op == "settings":
        # Read-only: the global switches the board protocol depends on, resolved the way the
        # installed dispatcher/gateway resolve them (defaults included).
        from hermes_cli.config import load_config
        from hermes_cli.kanban_db_dispatch import review_dispatch_enabled
        try:
            from gateway.kanban_watchers_common import _resolve_auto_decompose_settings
            auto_decompose = _resolve_auto_decompose_settings(load_config)[0]
        except ImportError:
            auto_decompose = bool(((load_config() or {}).get("kanban") or {}).get("auto_decompose", True))
        return _out({"ok": True, "result": {"review_dispatch": bool(review_dispatch_enabled()),
                                            "auto_decompose": bool(auto_decompose)}})
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
        if op == "create":
            tid = kb.create_task(
                conn, title=req["title"], body=req["body"], assignee=req.get("assignee") or None,
                created_by=req.get("created_by", "todo-manager"), tenant=req.get("tenant"),
                idempotency_key=req["idem_key"], workspace_kind=req.get("workspace_kind"),
                workspace_path=req.get("workspace_path"), skills=req.get("skills"),
                max_runtime_seconds=req.get("max_runtime_seconds"), max_retries=req.get("max_retries"),
                board=board)
            return _out({"ok": True, "result": tid})
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
            cid = kb.add_comment(conn, req["task_id"], req.get("author", "todo-manager"), req["body"])
            return _out({"ok": True, "result": cid})
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
