"""Simulates worker-side Kanban calls (what the worker's kanban_* tools do) using the
installed Hermes API, for integration tests in an isolated HERMES_HOME. Runs under the
Hermes interpreter. Request JSON on stdin, JSON result on the last stdout line."""

import json
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != here]


def main():
    req = json.loads(sys.stdin.read())
    sys.path.insert(0, req["hermes_repo"])
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing(board=req["board"]) as conn:
        op = req["op"]
        tid = req.get("task_id")
        if op == "claim":
            task = kb.claim_task(conn, tid, claimer="sim-worker")
            result = task is not None
        elif op == "block":
            run = kb.get_task(conn, tid).current_run_id
            result = kb.block_task(conn, tid, reason=req["reason"], kind=req.get("kind"), expected_run_id=run)
        elif op == "review":
            run = kb.get_task(conn, tid).current_run_id
            result = kb.request_review(conn, tid, summary=req["summary"], metadata=req.get("metadata"),
                                       expected_run_id=run)
        elif op == "status":
            task = kb.get_task(conn, tid)
            result = {"status": task.status, "block_kind": task.block_kind, "recurrences": task.block_recurrences}
        elif op == "dispatch_dry":
            res = kbd.dispatch_once(conn, dry_run=True, board=req["board"])
            result = {"spawned": [s[0] for s in res.spawned], "review_gate": kbd.review_dispatch_enabled()}
        elif op == "fail":
            # The dispatcher's failure funnel for a run that ended without a verdict
            # (crash / timeout / stale claim all count the same way); True = breaker tripped.
            result = kbd._record_task_failure(conn, tid, "sim: worker exited", outcome="crashed",
                                              release_claim=True, end_run=True)
        elif op == "recompute":
            result = kb.recompute_ready(conn)
        else:
            raise ValueError(op)
    print("@@SIM@@" + json.dumps(result, default=str))


main()
