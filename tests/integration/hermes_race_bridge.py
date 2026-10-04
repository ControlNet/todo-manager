"""Runs the real ``todo_manager/kanban_bridge.py`` under the Hermes interpreter with one Hermes
state function wrapped: right before it runs, the request's ``race`` steps (real Hermes calls:
what a dashboard user and a worker would do) are applied on the same board. This puts a human
action exactly between the bridge's anchor check and its state function, the interleaving
``transition_raced`` reports. Integration tests only; never installed or used in production.

Request: the bridge request plus ``"race": {"before": "<kanban_db function>", "steps": [...]}``,
each step ``{"op": "reopen_review" | "unblock" | "claim" | "review" | "block", ...}``.
"""

import importlib.util
import io
import json
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != here]
BRIDGE = os.path.join(here, "..", "..", "todo_manager", "kanban_bridge.py")


def main():
    req = json.loads(sys.stdin.read())
    race = req.pop("race")
    sys.path.insert(0, req["hermes_repo"])
    from hermes_cli import kanban_db as kb

    real = {name: getattr(kb, name) for name in ("reopen_review_task", "unblock_task", "claim_task", "request_review",
                                                  "block_task", "get_task", race["before"])}

    def interleaved(conn, task_id, *args, **kwargs):
        for step in race["steps"]:
            op = step["op"]
            if op == "reopen_review":
                assert real["reopen_review_task"](conn, task_id)
            elif op == "unblock":
                assert real["unblock_task"](conn, task_id)
            elif op == "claim":
                assert real["claim_task"](conn, task_id, claimer="sim-worker") is not None
            elif op == "review":
                run = real["get_task"](conn, task_id).current_run_id
                assert real["request_review"](conn, task_id, summary=step["summary"], metadata=step.get("metadata"),
                                              expected_run_id=run)
            elif op == "block":
                run = real["get_task"](conn, task_id).current_run_id
                assert real["block_task"](conn, task_id, reason=step["reason"], kind=step.get("kind"),
                                          expected_run_id=run)
            else:
                raise ValueError(op)
        return real[race["before"]](conn, task_id, *args, **kwargs)

    setattr(kb, race["before"], interleaved)
    spec = importlib.util.spec_from_file_location("kanban_bridge", os.path.abspath(BRIDGE))
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    sys.stdin = io.StringIO(json.dumps(req))
    try:
        bridge.main()
    except Exception as exc:  # noqa: BLE001 - reported like the bridge does
        bridge._out({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    main()
