"""Test helpers that act like a woken agent: they read the gate's context and record outcomes
through the same operations the ``todo-manager`` commands call (``ops``).

``apply_feedback_ops`` / ``apply_direct`` take the operation lists older tests were written with
(``{"op": "update_task", ...}``) and carry each one out as the command it corresponds to, in the
context of a feedback item or a chat request. They exist to keep those reliability scenarios;
new tests call ``ops`` directly.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from todo_manager import context, feedback, gate_intake, ops, runs
from todo_manager.config import Config


def wake_of(output: str) -> bool:
    """Hermes' rule: the run is skipped only when the last line is ``{"wakeAgent": false}``."""
    lines = [ln for ln in output.splitlines() if ln.strip()]
    try:
        gate = json.loads(lines[-1])
    except (ValueError, IndexError):
        return True
    return not isinstance(gate, dict) or gate.get("wakeAgent", True) is not False


def run_id_of(output: str) -> str:
    return output.splitlines()[0].rsplit(" ", 1)[1]


def run_ctx(conn: sqlite3.Connection, cfg: Config, run_id: str, **kw: Any) -> ops.Ctx:
    return ops.context(conn, cfg, run=run_id, **kw)


def intake_context(conn: sqlite3.Connection, cfg: Config, run_id: str) -> str:
    """The context of an intake run as its gate printed it (rendered again from its claims)."""
    manifest = gate_intake.manifest_for(cfg, run_id)
    return context.intake(conn, cfg, run_id, followups=manifest.get("followups") or [], digest=manifest.get("digest"),
                          problems=[], reasons=manifest.get("reasons") or [])


def feedback_context(conn: sqlite3.Connection, cfg: Config, run_id: str, items: list[dict[str, Any]]) -> str:
    return context.feedback(conn, cfg, run_id, items, {f["id"]: ops.incomplete_reason(f) for f in items}, [])


def evidence(op: dict[str, Any], default: str) -> list[tuple[str, str]]:
    return [(e["item"], e.get("role", default)) for e in op.get("evidence") or []]


TASK_FIELD_KEYS = ("status", "next_actor", "waiting_on", "responsibility", "next_step", "done_criteria", "summary",
                   "due_date", "due_basis", "follow_up_date", "snooze_until", "focus", "focus_reason")


def apply_op(conn: sqlite3.Connection, cfg: Config, ctx: ops.Ctx, op: dict[str, Any]) -> dict[str, Any]:
    """One operation of the older list format, as the command it corresponds to."""
    kind = op["op"]
    if kind == "update_task":
        changes = dict(op.get("set") or {})
        if op.get("note"):
            changes["change_note"] = op["note"]
        return ops.task_update(conn, cfg, ctx, op["task"], changes=changes, based_on=op.get("based_on_version"),
                               evidence=evidence(op, "context"), override_human=bool(op.get("override_human")),
                               closed_reason=op.get("closed_reason"), reason=op.get("rationale"))
    if kind == "link":
        return ops.task_link(conn, cfg, ctx, op["task"], evidence(op, "context"), op.get("rationale"))
    if kind == "update_project":
        return ops.project_update(conn, cfg, ctx, op["project"], changes=dict(op.get("set") or {}),
                                  based_on=op.get("based_on_version"), repos_add=op.get("repos_add"),
                                  repos_remove=op.get("repos_remove"), evidence=evidence(op, "context"))
    if kind == "create_project":
        return ops.project_create(conn, cfg, ctx, title=op["title"],
                                  fields={k: op[k] for k in ("domain", "goal", "next_step") if op.get(k)},
                                  repos=op.get("repos") or [], evidence=evidence(op, "context"))
    if kind == "create_task":
        fields = {k: op[k] for k in TASK_FIELD_KEYS if op.get(k) is not None}
        return ops.task_create(conn, cfg, ctx, title=op["title"], project=op.get("project"), fields=fields,
                               evidence=evidence(op, "origin"), follow_up_of=op.get("follow_up_of"),
                               reason=op.get("rationale"))
    if kind == "delegate":
        return ops.delegate_create(conn, cfg, ctx, task_id=op["task"], title=op["title"], goal=op["goal"],
                                   context_text=op.get("context"), constraints=op.get("constraints"),
                                   deliverable=op.get("deliverable"), done_criteria=op.get("done_criteria"),
                                   allowed=op.get("allowed_actions"), parallel=bool(op.get("parallel")), submit=False)
    if kind == "message":
        if op["kind"] == "answer":
            return ops.delegate_answer(conn, cfg, ctx, op["delegation"], body=op["body"])
        return ops.delegate_message(conn, cfg, ctx, op["delegation"], kind=op["kind"], body=op.get("body"),
                                    delivery=op.get("delivery"))
    if kind == "approve":
        return ops.delegate_approve(conn, cfg, ctx, op["delegation"], delivery=op["delivery"], action=op["action"],
                                    detail=op.get("detail"))
    raise ValueError(f"no command for op {kind!r}")


def apply_feedback_ops(conn: sqlite3.Connection, cfg: Config, fb: dict[str, Any] | str,
                       op_list: list[dict[str, Any]], run: str | None = None) -> list[dict[str, Any]]:
    """Carry out ``op_list`` for the user's feedback item ``fb`` (``--feedback <id>``, with
    ``--run <run>`` when a feedback run that claimed it does so)."""
    if not op_list:
        return []
    fid = fb if isinstance(fb, str) else fb["id"]
    ctx = ops.context(conn, cfg, feedback_id=fid, run=run)
    return [apply_op(conn, cfg, ctx, op) for op in op_list]


def user_feedback(conn: sqlite3.Connection, source_id: str, *, text: str = "(test feedback)",
                  origin: str = "matrix", payload: dict[str, Any] | None = None, scope_type: str = "global",
                  scope_id: str | None = None) -> str:
    """A pending feedback item written by the verified user (what a gate would have stored)."""
    fid, _ = feedback.record(conn, origin=origin, source_id=source_id, text=text, scope_type=scope_type,
                             scope_id=scope_id, kind="comment" if origin == "notion_comment" else "direct",
                             author="user", author_is_user=True, payload=payload)
    return fid


def resolve_feedback(conn: sqlite3.Connection, cfg: Config, run_id: str, items: list[dict[str, Any]],
                     done: bool = True) -> dict[str, Any]:
    """A feedback run: each item's ops, then ``feedback resolve``; finally ``run done``.
    ``items``: ``{"feedback": id, "outcome": ..., "ops": [...], "reply": ..., "reason": ...}``.
    Like the agent told so by a refusal, it moves on from an item that was superseded
    meanwhile (``report["superseded"]``) or is not ready (``report["deferred"]``)."""
    report: dict[str, Any] = {"applied": [], "clarify": [], "ignored": [], "superseded": [], "deferred": [],
                              "results": {}}
    for item in items:
        try:
            results = apply_feedback_ops(conn, cfg, item["feedback"], item.get("ops") or [], run=run_id)
            ops.feedback_resolve(conn, cfg, item["feedback"], outcome=item["outcome"], reply=item.get("reply"),
                                 reason=item.get("reason"), run=run_id)
        except ops.OpError as exc:
            if exc.code not in ("SUPERSEDED", "NOT_READY"):
                raise
            report["superseded" if exc.code == "SUPERSEDED" else "deferred"].append(item["feedback"])
            continue
        report[item["outcome"]].append(item["feedback"])
        report["results"][item["feedback"]] = results
    if done:
        report["done"] = ops.run_done(conn, cfg, run_id)
    return report


def apply_direct(conn: sqlite3.Connection, cfg: Config, *, text: str, op_list: list[dict[str, Any]],
                 source_id: str, origin: str = "matrix", scope_type: str = "global",
                 scope_id: str | None = None) -> list[dict[str, Any]]:
    """The main agent acting on the user's chat request (``--request <id> --said <text>``)."""
    scope = f"{scope_type}:{scope_id}" if scope_id else scope_type
    ctx = ops.context(conn, cfg, request=source_id, said=text, origin=origin, scope=scope)
    return [apply_op(conn, cfg, ctx, op) for op in op_list]


def claimed_feedback(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    return feedback.claimed_by(conn, run_id)


def run_summary(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    return (runs.get(conn, run_id) or {}).get("summary") or {}
