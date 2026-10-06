"""Small business operations: what the agent (or the user) records, one command at a time.

Every write runs in ONE transaction together with what depends on it (a task and its evidence
links, the items it handles, the follow-up it settles). It acts in a context:

* ``run``      - a cron run (actor ``agent``); items and follow-ups claimed by that run are
                 handled by it;
* ``feedback`` - the user's Notion feedback item (actor ``human``); refused for feedback whose
                 author is not the verified user (it is information, never an instruction);
* ``request``  - the main agent acting on the user's chat request (actor ``human``); the user's
                 words are stored once per request id;
* ``cli``      - the user at the terminal (actor ``human``).

Idempotency: a command in a run/feedback/request context is identified by that context plus the
command and its canonical arguments (or an explicit ``key``); repeating it after a lost response
returns the first result (``op_log``). Validation covers data correctness only (unknown ids,
versions, enums, claims, authorship); what the agent decides is its own judgement.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable

from . import batching, clock, delegation, effects, feedback, i18n, prefs, runs, store
from .config import Config
from .db import dumps, get_meta, loads, tx
from .notion import body as notion_body

OUTCOMES = ("no_action", "informational", "automated", "not_mine", "duplicate", "spam", "handled_elsewhere")
FEEDBACK_OUTCOMES = ("applied", "clarify", "ignored")
MAX_REPLY_CHARS = 1900          # one Notion rich-text object holds 2000 characters
DEFER_DAYS = 7                  # a deferral without a date is offered again after this at the latest
# Lengths older versions cut stored Notion text at (an item stored before ``complete`` was kept).
LEGACY_CUTS = {"body": 4000}     # new-page bodies stored by versions that cut them


class OpError(ValueError):
    def __init__(self, code: str, details: list[str] | str):
        self.code = code
        self.details = [details] if isinstance(details, str) else list(details)
        super().__init__(f"{code}: " + "; ".join(self.details[:10]))


@dataclass
class Ctx:
    kind: str                         # run | feedback | request | cli
    ref: str | None = None
    actor: str = "human"
    origin: str = "cli"
    run: dict[str, Any] | None = None
    fb: dict[str, Any] | None = None
    key: str | None = None            # --key: explicit identity of this command
    notes: list[str] = field(default_factory=list)
    # Why a new command may not act in this context (the feedback item was resolved meanwhile...).
    # Checked after the op_log lookup, so a retried command still returns its first result.
    refusal: OpError | None = None
    cfg: Config | None = None

    @property
    def run_id(self) -> str | None:
        if self.run is not None:
            return self.run["id"]
        if self.fb is not None:
            return self.fb.get("claimed_run")
        return None

    @property
    def cause(self) -> str | None:
        """What history records as the cause: the feedback item (the user's words), else the run."""
        return self.fb["id"] if self.fb is not None else self.ref


# --------------------------------------------------------------------------- context

def _fb_origin(fb: dict[str, Any]) -> str:
    return {"notion_comment": "notion", "notion_property": "notion", "notion_page": "notion"}.get(fb["origin"], fb["origin"])


def incomplete_reason(fb: dict[str, Any]) -> str | None:
    """Why the stored text of an item may not be all the user wrote (None: complete)."""
    p = fb.get("payload") or {}
    if p.get("complete") is False:
        return "the page body has not been read completely"
    if p.get("truncated"):
        return "the Notion API returned the page body cut short"
    if fb["kind"] == "new_task" and not p.get("complete") and any(
            len(p.get(key) or "") >= limit for key, limit in LEGACY_CUTS.items()):
        return "stored by an older version that kept only the first characters of long text"
    return None


def feedback_input_reason(conn: sqlite3.Connection, fb: dict[str, Any]) -> str | None:
    reason = incomplete_reason(fb)
    if reason:
        return reason
    task_id = fb.get('scope_id') if fb.get('scope_type') == 'task' else None
    if fb.get('scope_type') == 'delegation':
        owner = delegation.get(conn, fb.get('scope_id'))
        task_id = owner['task_id'] if owner else None
    if task_id:
        for row in conn.execute("SELECT * FROM feedback WHERE scope_type='task' AND scope_id=? "
                                "AND kind='new_task' AND status='pending'", (task_id,)):
            reason = incomplete_reason({**dict(row), 'payload': loads(row['payload'], {})})
            if reason:
                return 'task creation context is incomplete: ' + reason
    return None


def page_body_unread(conn: sqlite3.Connection, cfg: Config, fb: dict[str, Any]) -> bool:
    """A box interpretation is built from our last read of the page body; while a later edit
    (pulled, not read yet) may have replaced or withdrawn its text, it is neither claimed nor
    applied: the next covering read supersedes it or keeps it."""
    if fb["kind"] not in ("body_edit", "new_task"):
        return False
    row = conn.execute("SELECT last_edited_time, last_edited_by, body_read_at FROM notion_pages WHERE page_id=?",
                       ((fb["payload"] or {}).get("page_id"),)).fetchone()
    if row is None:
        return False
    bot = cfg.get("notion.bot_user_id") or get_meta(conn, "notion.bot_user_id")
    return notion_body.edit_unread(row["last_edited_time"], row["last_edited_by"], row["body_read_at"], bot)


def not_ready(conn: sqlite3.Connection, cfg: Config, fb: dict[str, Any]) -> str | None:
    """Why a feedback item must not be claimed or acted on now (None: it may)."""
    if fb.get("hold_reason"):
        return f"held: {fb['hold_reason']}"
    if page_body_unread(conn, cfg, fb):
        return "the page body has an edit not read yet"
    return None


def context(conn: sqlite3.Connection, cfg: Config, *, run: str | None = None, feedback_id: str | None = None,
            request: str | None = None, said: str | None = None, origin: str = "matrix",
            scope: str | None = None, key: str | None = None,
            write: bool = True, human: bool = False) -> Ctx:
    """Resolve and check the context of a command (``write``: it changes Todo data)."""
    given = [x for x in (run, feedback_id, request, human) if x]
    if len(given) > 1:
        raise OpError("INVALID", "give one of --run, --feedback, --request, --human")
    if (said or "").strip() and not request:
        raise OpError("INVALID", "--said goes with --request <id>: the Matrix event id of the user's message, or"
                                 " one id you choose for this request")
    if run:
        row = runs.get(conn, run)
        if row is None or row["entry"] not in ("intake", "feedback"):
            raise OpError("NOT_FOUND", f"no intake or feedback run {run}")
        return Ctx("run", run, actor="agent", origin=row["entry"], run=row, key=key)
    if feedback_id:
        fb = feedback.get(conn, feedback_id)
        if fb is None:
            raise OpError("NOT_FOUND", f"no feedback item {feedback_id}")
        if write and not fb["author_is_user"]:
            raise OpError("NOT_THE_USER", f"{feedback_id} was written by someone who is not the verified user: it is"
                                          " information only; resolve it as ignored, or clarify with the user")
        refusal = None
        if write and fb["status"] == "superseded":
            refusal = OpError("SUPERSEDED", f"{feedback_id} was replaced by a later edit of the same text; the new"
                                            " version is offered in a later run")
        elif write and fb["status"] != "pending":
            refusal = OpError("ALREADY_RESOLVED", f"{feedback_id} is already {fb['status']}; a new request needs"
                                                  " new feedback (or --request in a chat)")
        elif write and not_ready(conn, cfg, fb):
            refusal = OpError("NOT_READY", f"{feedback_id}: {not_ready(conn, cfg, fb)}; it is offered again when"
                                           " ready")
        return Ctx("feedback", feedback_id, actor="human", origin=_fb_origin(fb), fb=fb, key=key,
                   refusal=refusal, cfg=cfg)
    if request:
        prior = conn.execute("SELECT text FROM feedback WHERE source_id IN (?,?) AND kind='direct' AND origin=? "
                             "ORDER BY rowid LIMIT 1", (request.strip(), f'record:{request.strip()}', origin)).fetchone()
        if prior and (said or '').strip() and (prior['text'] or '').strip() != said.strip():
            raise OpError('REQUEST_ID_REUSED', 'this request ID already names different words')
        if not (said or '').strip():
            if prior is None:
                raise OpError('INVALID', "a new --request needs --said with the user's words")
            said = prior['text']
        scope_type, _, scope_id = (scope or "global").partition(":")
        fid, created = feedback.record(conn, origin=origin if origin in ("matrix", "cli") else "matrix",
                                       source_id=request.strip(), text=said, scope_type=scope_type,
                                       scope_id=scope_id or None, kind="direct", author="user", author_is_user=True,
                                       status="applied", result={"recorded": "direct request"})
        fb = feedback.get(conn, fid)
        if not created and (fb["text"] or "").strip() != said.strip():
            raise OpError("REQUEST_ID_REUSED", f"{request} already identifies another request ({fid}, other words);"
                                               " use a new id for a new request")
        # The same request recorded earlier for the feedback job is handled here instead.
        recorded = [r["id"] for r in conn.execute(
            "SELECT id FROM feedback WHERE source_id=? AND status='pending'", (f"record:{request.strip()}",))]
        if recorded:
            with tx(conn):
                feedback.supersede(conn, recorded, "handled directly in the chat", by=fid)
        return Ctx("request", request.strip(), actor="human", origin=fb["origin"], fb=fb, key=key)
    if write and not human:
        raise OpError("CONTEXT_REQUIRED", "use --run, --feedback, --request, or --human for a manual write")
    return Ctx("cli", None, actor="human", origin="cli", key=key)


def _key(ctx: Ctx, command: str, args: dict[str, Any]) -> str | None:
    if ctx.key:
        return f"key:{ctx.key}"
    if ctx.kind == "cli":
        return None       # the user at the terminal: each command is meant
    digest = hashlib.sha256(dumps({"c": command, "a": args}).encode()).hexdigest()[:24]
    return f"{ctx.kind}:{ctx.ref}:{digest}"


def perform(conn: sqlite3.Connection, ctx: Ctx, command: str, args: dict[str, Any],
            fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run ``fn`` once per identity, in one transaction with its op_log record."""
    key = _key(ctx, command, args)
    with tx(conn):
        if key:
            row = conn.execute("SELECT result FROM op_log WHERE key=?", (key,)).fetchone()
            if row is not None:
                return {**loads(row["result"], {}), "duplicate": True}
        if ctx.refusal is not None:
            raise ctx.refusal
        if ctx.kind == 'feedback':
            current = feedback.get(conn, ctx.fb['id'])
            if current['status'] != 'pending':
                raise OpError('SUPERSEDED' if current['status'] == 'superseded' else 'ALREADY_RESOLVED', current['id'])
            if ctx.cfg and not_ready(conn, ctx.cfg, current):
                raise OpError('NOT_READY', not_ready(conn, ctx.cfg, current))
            reason = feedback_input_reason(conn, current)
            if reason:
                raise OpError('INCOMPLETE', reason)
        result = fn()
        if ctx.notes:
            result["notes"] = ctx.notes
        if key:
            conn.execute("INSERT INTO op_log(key, context, command, result, created_at) VALUES (?,?,?,?,?)",
                         (key, f"{ctx.kind}:{ctx.ref}", command, dumps(result), clock.iso()))
    return result


# --------------------------------------------------------------------------- evidence and items

def parse_evidence(values: list[str] | None, default_role: str) -> list[tuple[str, str]]:
    out = []
    for value in values or []:
        item, _, role = value.partition(":")
        role = role.strip() or default_role
        if role not in store.EVIDENCE_ROLES:
            raise OpError("INVALID", f"evidence role {role!r} for {item}: one of {', '.join(store.EVIDENCE_ROLES)}")
        out.append((item.strip(), role))
    return out


def _item(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT s.*, b.state AS batch_state, b.claimed_run FROM source_items s"
                       " LEFT JOIN batches b ON b.id=s.batch_id WHERE s.id=?", (item_id,)).fetchone()
    if row is None:
        raise OpError("NOT_FOUND", f"no source item {item_id}")
    return row


def _claimed_elsewhere(row: sqlite3.Row, ctx: Ctx) -> str | None:
    if row["state"] == "batched" and row["batch_state"] == "claimed" and row["claimed_run"] != ctx.run_id:
        return row["claimed_run"]
    return None


def _handle(conn: sqlite3.Connection, ctx: Ctx, item_ids: list[str], state: str) -> list[str]:
    """Mark items as handled (``processed``; the decision recorded with it says how). Handled
    items stay part of their conversation's history in later contexts."""
    handled, batches = [], set()
    now = clock.iso()
    for item_id in item_ids:
        row = _item(conn, item_id)
        if row["state"] not in ("batched", "pending"):
            continue
        if _claimed_elsewhere(row, ctx):
            continue
        conn.execute("UPDATE source_items SET state=?, processed_at=?, processed_run=? WHERE id=?",
                     (state, now, ctx.run_id or ctx.ref, item_id))
        handled.append(item_id)
        if row["batch_id"]:
            batches.add(row["batch_id"])
    settle_batches(conn, batches)
    return handled


def settle_batches(conn: sqlite3.Connection, batch_ids: set[str] | list[str]) -> None:
    """A batch with no unhandled item left is done."""
    now = clock.iso()
    for batch_id in batch_ids:
        left = conn.execute("SELECT COUNT(*) FROM source_items WHERE batch_id=? AND state='batched'",
                            (batch_id,)).fetchone()[0]
        if left == 0:
            conn.execute("UPDATE batches SET state='done', done_at=? WHERE id=? AND state != 'done'", (now, batch_id))


def _check_evidence(conn: sqlite3.Connection, ctx: Ctx, evidence: list[tuple[str, str]], creating: bool) -> None:
    for item_id, _role in evidence:
        row = _item(conn, item_id)
        other = _claimed_elsewhere(row, ctx)
        if other and creating:
            raise OpError("CLAIMED_ELSEWHERE", f"{item_id} is being handled by run {other}; do not create a second"
                                               " task from it")
        if other:
            ctx.notes.append(f"{item_id} is being handled by run {other}: linked, its handling is left to that run")


def _link(conn: sqlite3.Connection, ctx: Ctx, entity_type: str, entity_id: str, evidence: list[tuple[str, str]],
          decision_id: str | None) -> list[str]:
    for item_id, role in evidence:
        store.link_evidence(conn, entity_type, entity_id, item_id, role, None, decision_id)
    return _handle(conn, ctx, [i for i, _ in evidence], "processed")


def _settle_followup(conn: sqlite3.Connection, ctx: Ctx, task_id: str) -> bool:
    if ctx.run_id is None:
        return False
    cur = conn.execute("UPDATE followup_checks SET state='done', updated_at=? WHERE task_id=? AND run_id=?"
                       " AND state='claimed'", (clock.iso(), task_id, ctx.run_id))
    return cur.rowcount > 0


def _decision(conn: sqlite3.Connection, ctx: Ctx, op: str, target_type: str | None, target_id: str | None,
              items: list[str], reason: str | None = None, category: str | None = None, detail: Any = None) -> str:
    return store.record_decision(conn, run_id=ctx.run_id or ctx.ref, op=op, target_type=target_type,
                                 target_id=target_id, source_item_ids=items, category=category, rationale=reason,
                                 detail=detail)


def _closed(conn: sqlite3.Connection, task_id: str, before: dict[str, Any], applied: dict[str, Any],
            reason: str | None, feedback_id: str | None) -> list[str]:
    closed = applied.get("status")
    if closed in store.CLOSED_STATUSES and before["status"] not in store.CLOSED_STATUSES:
        return delegation.on_task_closed(conn, task_id, closed, reason or i18n.t("fb.user_closed"), feedback_id)
    return []


# --------------------------------------------------------------------------- tasks

def task_create(conn: sqlite3.Connection, cfg: Config, ctx: Ctx, *, title: str, project: str | None = None,
                fields: dict[str, Any] | None = None, evidence: list[tuple[str, str]] | None = None,
                follow_up_of: str | None = None, reason: str | None = None) -> dict[str, Any]:
    fields = dict(fields or {})
    evidence = evidence or []
    if not (title or "").strip():
        raise OpError("INVALID", "--title is required")
    if project and store.get_project(conn, project) is None:
        raise OpError("NOT_FOUND", f"no project {project}")
    if follow_up_of and store.get_task(conn, follow_up_of) is None:
        raise OpError("NOT_FOUND", f"no task {follow_up_of}")
    unknown = set(fields) - set(store.TASK_FIELDS)
    if unknown:
        raise OpError("INVALID", f"unknown task fields {sorted(unknown)}; fields: {', '.join(store.TASK_FIELDS)}")
    args = {"title": title, "project": project, "fields": fields, "evidence": evidence, "follow_up_of": follow_up_of}

    def apply() -> dict[str, Any]:
        _check_evidence(conn, ctx, evidence, creating=True)
        did = _decision(conn, ctx, "create_task", "task", None, [i for i, _ in evidence], reason)
        status = fields.pop("status", "todo")
        actor_field = fields.pop("next_actor", None)
        try:
            tid = store.create_task(conn, title, origin=ctx.origin, actor=ctx.actor, cause_ref=ctx.cause or did,
                                    project_id=project or None, status=status, next_actor=actor_field, **fields)
        except store.StoreError as exc:
            raise OpError("INVALID", str(exc)) from None
        conn.execute("UPDATE decisions SET target_id=? WHERE id=?", (tid, did))
        handled = _link(conn, ctx, "task", tid, evidence, did)
        if follow_up_of:
            store.link_tasks(conn, tid, follow_up_of, "follow_up_of")
            _settle_followup(conn, ctx, follow_up_of)
        effects.task_push(conn, tid)
        return {"task": tid, "version": 1, "handled": handled}

    return perform(conn, ctx, "task create", args, apply)


def task_update(conn: sqlite3.Connection, cfg: Config, ctx: Ctx, task_id: str, *, changes: dict[str, Any],
                based_on: int | None = None, evidence: list[tuple[str, str]] | None = None,
                override_human: bool = False, closed_reason: str | None = None,
                reason: str | None = None) -> dict[str, Any]:
    evidence = evidence or []
    if store.get_task(conn, task_id) is None:
        raise OpError("NOT_FOUND", f"no task {task_id}")
    unknown = set(changes) - set(store.TASK_FIELDS)
    if unknown:
        raise OpError("INVALID", f"unknown task fields {sorted(unknown)}; fields: {', '.join(store.TASK_FIELDS)}")
    if ctx.actor == "agent" and changes and based_on is None:
        raise OpError("INVALID", "--based-on <version> is required: the task version you read (task show/list)")
    if override_human and not evidence:
        raise OpError("INVALID", "--override-human needs --evidence for the change")
    args = {"task": task_id, "changes": changes, "based_on": based_on, "evidence": evidence,
            "override_human": override_human, "closed_reason": closed_reason}

    def apply() -> dict[str, Any]:
        before = store.get_task(conn, task_id)
        _check_evidence(conn, ctx, evidence, creating=False)
        did = _decision(conn, ctx, "update_task", "task", task_id, [i for i, _ in evidence], reason)
        report: dict[str, Any] = {"task": task_id}
        if changes:
            todo = dict(changes)
            stale: dict[str, str] = {}
            if ctx.actor == "human" and based_on is not None:
                # The user's request, written against version ``based_on``: a newer edit by the user wins.
                meta = loads(before["field_meta"], {})
                stale = {f: "stale: changed by the user after the version you read" for f in todo
                         if meta.get(f, {}).get("by") == "human" and int(meta[f].get("v", 0)) > based_on}
                for f in stale:
                    todo.pop(f)
            try:
                result = store.update_task(conn, task_id, todo, actor=ctx.actor, origin=ctx.origin,
                                           cause_ref=ctx.cause or did,
                                           based_on_version=based_on if ctx.actor != "human" else None,
                                           override_human=override_human)
            except store.StoreError as exc:
                raise OpError("INVALID", str(exc)) from None
            result.rejected.update(stale)
            if "change_note" in result.applied:
                conn.execute("UPDATE tasks SET change_note_at=? WHERE id=?", (clock.iso(), task_id))
            if result.applied.get("status") in store.CLOSED_STATUSES and closed_reason:
                store.set_closed_reason(conn, task_id, closed_reason)
            report.update(applied=sorted(result.applied), rejected=result.rejected, version=result.version)
            if result.rejected:
                conn.execute("UPDATE decisions SET outcome=?, detail=? WHERE id=?",
                             ("partial" if result.applied else "rejected", dumps(result.rejected), did))
            stopped = _closed(conn, task_id, before, result.applied, changes.get("change_note"),
                              ctx.fb["id"] if ctx.kind == "feedback" else None)
            if stopped:
                report["agent_work_stopped"] = stopped
        report["handled"] = _link(conn, ctx, "task", task_id, evidence, did)
        if _settle_followup(conn, ctx, task_id):
            report["followup"] = "checked"
        effects.task_push(conn, task_id)
        return report

    return perform(conn, ctx, "task update", args, apply)


def task_link(conn: sqlite3.Connection, cfg: Config, ctx: Ctx, task_id: str,
              evidence: list[tuple[str, str]], reason: str | None = None) -> dict[str, Any]:
    if not evidence:
        raise OpError("INVALID", "--evidence is required")
    return task_update(conn, cfg, ctx, task_id, changes={}, evidence=evidence, reason=reason)


# --------------------------------------------------------------------------- projects

def project_create(conn: sqlite3.Connection, cfg: Config, ctx: Ctx, *, title: str, fields: dict[str, Any],
                   repos: list[str], evidence: list[tuple[str, str]] | None = None,
                   reason: str | None = None) -> dict[str, Any]:
    evidence = evidence or []
    if not (title or "").strip():
        raise OpError("INVALID", "--title is required")
    unknown = set(fields) - {"domain", "goal", "next_step", "status"}
    if unknown:
        raise OpError("INVALID", f"unknown fields {sorted(unknown)} for a new project (domain, goal, next_step, status)")
    args = {"title": title, "fields": fields, "repos": repos, "evidence": evidence}

    def apply() -> dict[str, Any]:
        _check_evidence(conn, ctx, evidence, creating=False)
        did = _decision(conn, ctx, "create_project", "project", None, [i for i, _ in evidence], reason)
        pid = store.create_project(conn, title, origin=ctx.origin, actor=ctx.actor, cause_ref=ctx.cause or did,
                                   repos=repos, **fields)
        conn.execute("UPDATE decisions SET target_id=? WHERE id=?", (pid, did))
        handled = _link(conn, ctx, "project", pid, evidence, did)
        effects.project_push(conn, pid)
        return {"project": pid, "handled": handled}

    return perform(conn, ctx, "project create", args, apply)


def project_update(conn: sqlite3.Connection, cfg: Config, ctx: Ctx, project_id: str, *, changes: dict[str, Any],
                   based_on: int | None = None, repos_add: list[str] | None = None,
                   repos_remove: list[str] | None = None, evidence: list[tuple[str, str]] | None = None,
                   reason: str | None = None) -> dict[str, Any]:
    evidence = evidence or []
    if store.get_project(conn, project_id) is None:
        raise OpError("NOT_FOUND", f"no project {project_id}")
    unknown = set(changes) - set(store.PROJECT_FIELDS)
    if unknown:
        raise OpError("INVALID", f"unknown project fields {sorted(unknown)}; fields: {', '.join(store.PROJECT_FIELDS)}")
    args = {"project": project_id, "changes": changes, "based_on": based_on, "repos_add": repos_add or [],
            "repos_remove": repos_remove or [], "evidence": evidence}

    def apply() -> dict[str, Any]:
        _check_evidence(conn, ctx, evidence, creating=False)
        did = _decision(conn, ctx, "update_project", "project", project_id, [i for i, _ in evidence], reason)
        if "recent_activity" in changes:
            conn.execute("UPDATE projects SET recent_activity_at=? WHERE id=?", (clock.iso(), project_id))
        try:
            result = store.update_project(conn, project_id, changes, actor=ctx.actor, origin=ctx.origin,
                                          cause_ref=ctx.cause or did,
                                          based_on_version=based_on if ctx.actor != "human" else None,
                                          repos_add=repos_add or [], repos_remove=repos_remove or [])
        except store.StoreError as exc:
            raise OpError("INVALID", str(exc)) from None
        handled = _link(conn, ctx, "project", project_id, evidence, did)
        effects.project_push(conn, project_id)
        return {"project": project_id, "applied": sorted(result.applied), "rejected": result.rejected,
                "handled": handled}

    return perform(conn, ctx, "project update", args, apply)


# --------------------------------------------------------------------------- material without a task

def item_dismiss(conn: sqlite3.Connection, cfg: Config, ctx: Ctx, item_ids: list[str], *, outcome: str,
                 reason: str, label: str | None = None) -> dict[str, Any]:
    if outcome not in OUTCOMES:
        raise OpError("INVALID", f"--outcome must be one of {', '.join(OUTCOMES)} (add --label for your own name)")
    if not (reason or "").strip():
        raise OpError("INVALID", "--reason is required (one line: why no task)")
    if not item_ids:
        raise OpError("INVALID", "name at least one item")
    args = {"items": sorted(item_ids), "outcome": outcome, "label": label, "reason": reason}

    def apply() -> dict[str, Any]:
        skipped = {}
        for item_id in item_ids:
            row = _item(conn, item_id)
            other = _claimed_elsewhere(row, ctx)
            if other:
                raise OpError("CLAIMED_ELSEWHERE", f"{item_id} is being handled by run {other}")
            if row["state"] == "processed":
                linked = conn.execute("SELECT 1 FROM evidence_links WHERE source_item_id=?", (item_id,)).fetchone()
                skipped[item_id] = "already evidence of a task or project" if linked else "already handled"
            elif row["state"] in ("noise", "superseded"):
                skipped[item_id] = f"already {row['state']}"
        todo = [i for i in item_ids if i not in skipped]
        _decision(conn, ctx, "dismiss", None, None, todo, reason, category=outcome,
                  detail={"label": label} if label else None)
        handled = _handle(conn, ctx, todo, "processed")   # handled; the decision says how
        out: dict[str, Any] = {"dismissed": handled, "outcome": outcome}
        if label:
            out["label"] = label
        if skipped:
            out["skipped"] = skipped
        return out

    return perform(conn, ctx, "item dismiss", args, apply)


def item_defer(conn: sqlite3.Connection, cfg: Config, ctx: Ctx, item_ids: list[str], *, reason: str,
               until: str | None = None) -> dict[str, Any]:
    """Leave items unhandled on purpose: they move to a segment of their conversation that is
    offered again with the conversation's next new activity, or at ``until`` (default
    ``DEFER_DAYS`` days), whichever comes first."""
    if not (reason or "").strip():
        raise OpError("INVALID", "--reason is required")
    if not item_ids:
        raise OpError("INVALID", "name at least one item")
    if until is not None:
        try:
            when = clock.parse(until if "T" in until else f"{until}T00:00:00")
        except ValueError:
            raise OpError("INVALID", f"--until {until!r} is not a date (YYYY-MM-DD)") from None
    else:
        when = clock.now() + timedelta(days=DEFER_DAYS)
    args = {"items": sorted(item_ids), "reason": reason, "until": until}

    def apply() -> dict[str, Any]:
        groups: dict[tuple[str, str], list[str]] = {}
        for item_id in item_ids:
            row = _item(conn, item_id)
            other = _claimed_elsewhere(row, ctx)
            if other:
                raise OpError("CLAIMED_ELSEWHERE", f"{item_id} is being handled by run {other}")
            if row["state"] != "batched":
                raise OpError("INVALID", f"{item_id} is {row['state']}, not waiting for a decision")
            groups.setdefault((row["source"], row["conversation_key"]), []).append(item_id)
        _decision(conn, ctx, "defer", None, None, list(item_ids), reason, detail={"until": clock.iso(when)})
        batches = []
        for (source, conversation), members in groups.items():
            batches.append(batching.defer_items(conn, source, conversation, members, clock.iso(when), reason))
        return {"deferred": sorted(item_ids), "until": clock.iso(when), "batches": batches}

    return perform(conn, ctx, "item defer", args, apply)


def followup_checked(conn: sqlite3.Connection, cfg: Config, ctx: Ctx, task_id: str,
                     reason: str | None = None) -> dict[str, Any]:
    if store.get_task(conn, task_id) is None:
        raise OpError("NOT_FOUND", f"no task {task_id}")

    def apply() -> dict[str, Any]:
        _decision(conn, ctx, "followup_checked", "task", task_id, [], reason)
        return {"task": task_id, "followup": "checked" if _settle_followup(conn, ctx, task_id)
                else "nothing claimed by this run"}

    return perform(conn, ctx, "followup checked", {"task": task_id}, apply)


def digest_done(conn: sqlite3.Connection, cfg: Config, ctx: Ctx, reason: str | None = None) -> dict[str, Any]:
    if ctx.run is None:
        raise OpError("INVALID", "digest confirmation needs a run")
    manifest = runs.read_json(cfg.home, ctx.run["id"], "manifest.json") or {}
    digest = manifest.get("digest")
    if not digest:
        raise OpError("INVALID", f"run {ctx.run['id']} has no commit digest")

    def apply() -> dict[str, Any]:
        owned = conn.execute("SELECT 1 FROM daily_jobs WHERE job='commit_digest' AND day=? AND run_id=?"
                             " AND state='claimed'", (digest["day"], ctx.run["id"])).fetchone()
        if owned is None:
            raise OpError("STALE_RUN", "the digest is no longer claimed by this run")
        now = clock.iso()
        ids_ = digest["item_ids"]
        for start in range(0, len(ids_), 500):
            chunk = ids_[start:start + 500]
            conn.execute(f"UPDATE source_items SET state='processed', processed_at=?, processed_run=? WHERE id IN"
                         f" ({','.join('?' * len(chunk))}) AND state='pending'", (now, ctx.run["id"], *chunk))
        conn.execute("UPDATE daily_jobs SET state='done', updated_at=? WHERE job='commit_digest' AND day=?",
                     (now, digest["day"]))
        _decision(conn, ctx, "digest_done", None, None, ids_, reason)
        return {"digest": digest["day"], "items": len(ids_)}

    return perform(conn, ctx, "digest done", {"day": digest["day"]}, apply)


# --------------------------------------------------------------------------- feedback

def _queue_reply(conn: sqlite3.Connection, fb: dict[str, Any], text: str) -> None:
    """The reply goes to the comment thread it answers, else to the task's page; when there is
    no such page (chat/CLI feedback without a task, or the page is in the trash) to the Notion
    home page."""
    key = f"reply:{fb['id']}"
    comment = fb["origin"] == "notion_comment"
    thread = fb.get("reply_to") if comment else None
    task = fb["scope_id"] if fb["scope_type"] == "task" else None
    if task:
        trashed = conn.execute("SELECT 1 FROM notion_pages WHERE entity_type='task' AND entity_id=? AND trashed=1",
                               (task,)).fetchone()
        if not trashed:
            effects.task_comment(conn, task, key, text, discussion_id=thread)
            return
    elif comment and (fb["payload"] or {}).get("page_id"):
        effects.page_comment(conn, fb["payload"]["page_id"], key, text, discussion_id=thread)
        return
    home = get_meta(conn, "notion.home_page_id")
    if home:
        effects.page_comment(conn, home, key, text)


def release_body(conn: sqlite3.Connection, task_id: str) -> None:
    """Let the next push rewrite the managed box (after a user edit inside it was handled)."""
    conn.execute("UPDATE notion_pages SET managed_hash=NULL, rendered_hash=NULL WHERE entity_type='task'"
                 " AND entity_id=?", (task_id,))


def feedback_resolve(conn: sqlite3.Connection, cfg: Config, feedback_id: str, *, outcome: str,
                     reply: str | None = None, reason: str | None = None, settles: str | None = None) -> dict[str, Any]:
    with tx(conn):
        return _feedback_resolve(conn, cfg, feedback_id, outcome=outcome, reply=reply, reason=reason, settles=settles)


def _feedback_resolve(conn: sqlite3.Connection, cfg: Config, feedback_id: str, *, outcome: str,
                     reply: str | None = None, reason: str | None = None, settles: str | None = None) -> dict[str, Any]:
    fb = feedback.get(conn, feedback_id)
    if fb is None:
        raise OpError("NOT_FOUND", f"no feedback item {feedback_id}")
    if outcome not in FEEDBACK_OUTCOMES:
        raise OpError("INVALID", f"--outcome must be one of {', '.join(FEEDBACK_OUTCOMES)}")
    reply = (reply or "").strip()
    if fb["status"] != "pending":
        if fb["status"] == outcome and (fb.get("result") or {}).get("settles") == settles:
            return {"feedback": feedback_id, "outcome": outcome, "duplicate": True}
        if fb["status"] == "superseded":
            raise OpError("SUPERSEDED", f"{feedback_id} was replaced by a later edit of the same text; nothing to"
                                        " resolve (the new version is offered in a later run)")
        raise OpError("ALREADY_RESOLVED", f"{feedback_id} is already {fb['status']}")
    why = not_ready(conn, cfg, fb)
    if why:
        raise OpError("NOT_READY", f"{feedback_id}: {why}; it is offered again when ready")
    incomplete = feedback_input_reason(conn, fb)
    if incomplete and outcome != "clarify":
        raise OpError("INCOMPLETE", f"{feedback_id}: {incomplete}; only --outcome clarify (ask the user to restate it)")
    if outcome == "clarify" and not reply:
        raise OpError("INVALID", "clarify needs the one question in --reply")
    if outcome == "ignored" and not (reason or reply):
        raise OpError("INVALID", "ignored needs --reason")
    if len(reply) > MAX_REPLY_CHARS:
        raise OpError("INVALID", f"--reply has {len(reply)} characters; a Notion comment holds {MAX_REPLY_CHARS}")
    with tx(conn):
        result: dict[str, Any] = {"reason": reason, "settles": settles}
        if settles:
            original = feedback.get(conn, settles)
            if (not original or original['kind'] != 'body_edit' or original['status'] != 'clarify'
                    or original['scope_type'] != 'task' or fb['scope_type'] != 'task'
                    or original['scope_id'] != fb['scope_id'] or not fb['author_is_user']
                    or outcome not in ('applied', 'ignored')):
                raise OpError('INVALID_LINK', 'settles must identify the clarified body edit of this task')
            page = conn.execute('SELECT * FROM notion_pages WHERE page_id=?',
                                (original['payload'].get('page_id'),)).fetchone()
            if (not page or page['box_revision'] != original['payload'].get('revision')
                    or page_body_unread(conn, cfg, original)):
                raise OpError('STALE_BODY', 'a newer or unread body edit cannot be released by this answer')
            release_body(conn, original['scope_id'])
            effects.task_push(conn, original['scope_id'])
        if reply:
            _queue_reply(conn, fb, reply)
            result["reply"] = reply
        result["ops"] = [r["command"] for r in conn.execute(
            "SELECT command FROM op_log WHERE context=? ORDER BY created_at", (f"feedback:{feedback_id}",))]
        feedback.resolve(conn, feedback_id, outcome, result)
        if fb["kind"] == "body_edit" and outcome != "clarify" and fb["scope_id"]:
            release_body(conn, fb["scope_id"])
            effects.task_push(conn, fb["scope_id"])
    return {"feedback": feedback_id, "outcome": outcome, **({"reply": "queued"} if reply else {})}


def feedback_record(conn: sqlite3.Connection, *, request: str, said: str, scope: str | None,
                    origin: str = "matrix") -> dict[str, Any]:
    with tx(conn):
        return _feedback_record(conn, request=request, said=said, scope=scope, origin=origin)


def _feedback_record(conn: sqlite3.Connection, *, request: str, said: str, scope: str | None,
                    origin: str = "matrix") -> dict[str, Any]:
    """Store what the user said for the next feedback run to interpret."""
    if not (request or "").strip() or not (said or "").strip():
        raise OpError("INVALID", "--request and --said are required")
    prior = conn.execute("SELECT text FROM feedback WHERE source_id IN (?,?) AND kind='direct' AND origin=?",
                         (request.strip(), f'record:{request.strip()}', origin)).fetchone()
    if prior and (prior['text'] or '').strip() != said.strip():
        raise OpError('REQUEST_ID_REUSED', 'this request ID already names different words')
    direct = conn.execute("SELECT * FROM feedback WHERE source_id=? AND kind='direct' AND status='applied' AND origin=?",
                          (request.strip(), origin)).fetchone()
    if direct:
        if (direct['text'] or '').strip() != said.strip():
            raise OpError('REQUEST_ID_REUSED', 'this request was already handled with different words')
        return {'feedback': direct['id'], 'created': False, 'status': 'already_handled', 'queued': False}
    scope_type, _, scope_id = (scope or "global").partition(":")
    if scope_type not in ("task", "project", "delegation", "global"):
        raise OpError("INVALID", "--scope is task:<id>, project:<id> or global")
    fid, created = feedback.record(conn, origin=origin if origin in ("matrix", "cli") else "matrix",
                                   source_id=f"record:{request.strip()}", text=said, scope_type=scope_type,
                                   scope_id=scope_id or None, kind="direct", author="user", author_is_user=True)
    return {"feedback": fid, "created": created, "note": "the next feedback run interprets it"}


# --------------------------------------------------------------------------- delegations

def _delegation_ctx(ctx: Ctx) -> None:
    if ctx.kind == "run":
        raise OpError("NOT_AUTHORIZED", "agent work is started or changed only on the user's request: use --feedback"
                                        " <the user's feedback item> or --request in a chat")


def _submit_now(conn: sqlite3.Connection, cfg: Config, delegation_id: str) -> str:
    """Create the card right away (best effort); the feedback job's sync completes a create that
    failed or whose outcome is unknown."""
    from . import wiring
    try:
        kb = wiring.kanban(cfg)
        if kb is None:
            return "the Kanban board is not enabled: kept locally"
        delegation.submit_pending(conn, cfg, kb, only=delegation_id)
    except Exception as exc:  # noqa: BLE001 - stored; the next sync retries
        return f"card not created yet ({type(exc).__name__}); the feedback job retries"
    d = delegation.get(conn, delegation_id)
    if d and d.get("card_id"):
        return d["card_id"]
    return "card not created yet" + (f" ({d['last_error']})" if d and d.get("last_error") else "") + \
        "; the feedback job retries"


def delegate_create(conn: sqlite3.Connection, cfg: Config, ctx: Ctx, *, task_id: str, title: str, goal: str,
                    context_text: str | None = None, constraints: str | None = None, deliverable: str | None = None,
                    done_criteria: str | None = None, allowed: list[str] | None = None,
                    parallel: bool = False, submit: bool = True) -> dict[str, Any]:
    _delegation_ctx(ctx)
    if not (title or "").strip() or not (goal or "").strip():
        raise OpError("INVALID", "a task title and nonempty delegation brief are required")
    args = {"task": task_id, "title": title, "goal": goal, "context": context_text, "constraints": constraints,
            "deliverable": deliverable, "done": done_criteria, "allowed": allowed, "parallel": parallel}
    key = _key(ctx, "delegate create", args)

    def apply() -> dict[str, Any]:
        try:
            did = delegation.create(conn, cfg, task_id=task_id, title=title, goal=goal, context=context_text,
                                    constraints=constraints, deliverable=deliverable, done_criteria=done_criteria,
                                    allowed_actions=allowed, feedback_id=ctx.fb["id"] if ctx.fb else None,
                                    actor="human", op_key=key, parallel=parallel)
        except delegation.DelegationError as exc:
            raise OpError("INVALID", str(exc)) from None
        return {"delegation": did}

    result = perform(conn, ctx, "delegate create", args, apply)
    if submit and not result.get("duplicate"):
        result["card"] = _submit_now(conn, cfg, result["delegation"])
    return result


def _replied_delivery(fb: dict[str, Any] | None, delegation_id: str, kind: str, explicit: str | None) -> str | None:
    """The version an accept/revision is about: named explicitly, else the delivered version
    whose notice the user replied to; None means the latest version."""
    if kind not in ("accept", "revision"):
        return None
    if explicit:
        return explicit
    p = (fb or {}).get("payload") or {}
    if p.get("delegation_id") == delegation_id and p.get("delivery_kind") == "result":
        return p.get("delivery_id")
    return None


MESSAGE_KINDS = ("supplement", "revision", "pause", "resume", "cancel", "accept")


def delegate_message(conn: sqlite3.Connection, cfg: Config, ctx: Ctx, delegation_id: str, *, kind: str,
                     body: str | None, delivery: str | None = None) -> dict[str, Any]:
    _delegation_ctx(ctx)
    if kind not in MESSAGE_KINDS:
        raise OpError("INVALID", f"--kind is one of {', '.join(MESSAGE_KINDS)} (answers: `delegate answer`)")
    body = (body or "").strip() or ("accepted" if kind == "accept" else "")
    if not body:
        raise OpError("INVALID", "--body is required")
    d = delegation.get(conn, delegation_id)
    if d is None:
        raise OpError("NOT_FOUND", f"no delegation {delegation_id}")
    if kind == "revision" and d["status"] not in ("review", "queued", "running", "blocked"):
        raise OpError("INVALID", f"the agent work is {d['status']}: a revision needs a delivered version or work in"
                                 " progress")
    if delivery and kind not in ("accept", "revision"):
        raise OpError("INVALID", "--delivery names the version an accept or revision is about")
    args = {"delegation": delegation_id, "kind": kind, "body": body, "delivery": delivery}
    key = _key(ctx, "delegate message", args)

    def apply() -> dict[str, Any]:
        try:
            mid = delegation.add_message(conn, delegation_id, kind, body, feedback_id=ctx.fb["id"] if ctx.fb else None,
                                         delivery_id=_replied_delivery(ctx.fb, delegation_id, kind, delivery),
                                         op_key=key)
        except delegation.DelegationError as exc:
            raise OpError("INVALID", str(exc)) from None
        return {"message": mid, "kind": kind}

    return perform(conn, ctx, "delegate message", args, apply)


def parse_approvals(values: list[str] | None) -> list[tuple[str, str]]:
    """``action[:detail]`` -> (action, detail); unknown actions are refused."""
    result = []
    for value in values or []:
        action, _, detail = value.partition(":")
        action = action.strip()
        if action not in delegation.KNOWN_ACTIONS:
            raise OpError("INVALID", f"unknown action {action!r}; known: {sorted(delegation.KNOWN_ACTIONS)}")
        result.append((action, detail.strip()))
    return result


def delegate_answer(conn: sqlite3.Connection, cfg: Config, ctx: Ctx, delegation_id: str, *,
                    body: str, approve: list[str] | None = None, question: str | None = None) -> dict[str, Any]:
    """Answer the card's open question (bound to that question, however late the answer).
    ``approve``: external actions (``action[:detail]``) the user allows with this answer, e.g.
    when the worker asked whether it may send the email; each is used at most once."""
    _delegation_ctx(ctx)
    approvals = parse_approvals(approve)
    d = delegation.get(conn, delegation_id)
    if d is None:
        raise OpError("NOT_FOUND", f"no delegation {delegation_id}")
    if not (body or "").strip():
        raise OpError("INVALID", "--body is required")
    payload = (ctx.fb or {}).get('payload') or {}
    if question is None and payload.get('answer_binding_error'):
        raise OpError('INVALID', payload['answer_binding_error'])
    question = question or payload.get('delivery_id')
    if question is None:
        row = conn.execute("SELECT * FROM deliveries WHERE delegation_id=? AND card_id=? AND kind IN ('question','failure') "
                           "AND source_event_id=?", (delegation_id, d['card_id'], d['block_event_id'])).fetchone()
    else:
        row = conn.execute("SELECT * FROM deliveries WHERE id=? AND delegation_id=? AND kind IN ('question','failure')",
                           (question, delegation_id)).fetchone()
    if row is None:
        raise OpError('INVALID', 'no matching question in this delegation')
    question = row['id']
    if ctx.key:
        from dataclasses import replace
        ctx = replace(ctx, key=f'{ctx.key}:question:{question}')
    args = {'delegation': delegation_id, 'question': question, 'body': body,
            **({'approve': approve} if approve else {})}
    key = _key(ctx, 'delegate answer', args)

    def apply() -> dict[str, Any]:
        try:
            if row['card_id'] != d['card_id']:
                if approvals:
                    raise delegation.DelegationError('child external approvals are not supported; ask the root')
                from . import child_links
                return child_links.queue_answer(conn, d, dict(row), body, key, ctx.fb['id'] if ctx.fb else None)
            if (payload.get('delegation_id') == delegation_id and payload.get('delivery_kind') == 'question'
                    and payload.get('delivery_seq') not in (None, row['seq'])):
                raise delegation.DelegationError('reply names an earlier question')
            mid = delegation.add_message(conn, delegation_id, 'answer', body, delivery_id=question,
                                         feedback_id=ctx.fb['id'] if ctx.fb else None, op_key=key, approve=approvals)
        except delegation.DelegationError as exc:
            raise OpError('INVALID', str(exc)) from None
        result = {'message': mid, 'kind': 'answer', 'question': question, 'status': 'pending'}
        if approvals:
            result['approvals'] = [a['id'] for a in delegation.answer_approvals(conn, mid)]
        return result

    result = perform(conn, ctx, 'delegate answer', args, apply)
    message = conn.execute('SELECT status,error FROM delegation_messages WHERE id=?', (result['message'],)).fetchone()
    if message:
        result['status'] = message['status']
        if message['error']:
            result['error'] = message['error']
    return result


def delegate_approve(conn: sqlite3.Connection, cfg: Config, ctx: Ctx, delegation_id: str, *, delivery: str,
                     action: str, detail: str | None = None) -> dict[str, Any]:
    """Approve an external action for a delivered version waiting for review."""
    _delegation_ctx(ctx)
    names = delegation.action_names([action])
    if len(names) != 1 or not names <= delegation.KNOWN_ACTIONS:
        raise OpError("INVALID", f"unknown action {action!r}")
    args = {"delegation": delegation_id, "delivery": delivery, "action": action, "detail": detail}
    key = _key(ctx, "delegate approve", args)

    def apply() -> dict[str, Any]:
        try:
            aid = delegation.record_approval(conn, delegation_id, delivery, action, detail or "",
                                             ctx.fb["id"] if ctx.fb else None, op_key=key)
        except delegation.DelegationError as exc:
            raise OpError("INVALID", str(exc)) from None
        return {"approval": aid}

    return perform(conn, ctx, "delegate approve", args, apply)


# --------------------------------------------------------------------------- settings

# Settings that decide who counts as the user, which credentials are read, which programs run
# and whether agents work on the board. A change there widens what the automation may do, so it
# is taken only from the user directly (terminal or chat), never from a scheduled run, even one
# carrying out the user's Notion comment.
GUARDED_SETTINGS = re.compile(r"^(identity|hermes|kanban)\.|(^|\.)(token\w*|\w*_command|env_file|accounts|\w*bin)$")


def config_set(conn: sqlite3.Connection, cfg: Config, ctx: Ctx, key: str, value: Any) -> dict[str, Any]:
    """Change one ``config.toml`` value (effective from the next run), recorded with its context."""
    from . import config_file
    if ctx.kind == "run":
        raise OpError("NOT_AUTHORIZED", "a scheduled run does not change settings; a lasting change comes from the user"
                                        " (their chat request, or --feedback for their Notion comment)")
    if ctx.kind == "feedback" and GUARDED_SETTINGS.search(key):
        raise OpError("NOT_AUTHORIZED", f"{key} decides identities, credentials, programs or agent work: only the user"
                                        " changes it (terminal, or a chat request with --request/--said)")
    native_fields = {'model', 'provider', 'reasoning_effort'}
    args = {"key": key, "value": value}

    def apply() -> dict[str, Any]:
        installation = cfg.home / 'state/hermes/install.json'
        installed_jobs = loads(installation.read_text(), {}).get('jobs') if installation.exists() else {}
        if key.startswith('hermes.') and key.split('.', 1)[1] in native_fields and installed_jobs:
            raise OpError('NATIVE_SETTING', 'installed cron settings belong to Hermes; use hermes cron edit on the owned job ID')
        previous = cfg.get(key)
        try:
            changed = config_file.set_values(cfg.home / "config.toml", {key: value})
        except config_file.ConfigEditError as exc:
            raise OpError("INVALID", str(exc)) from None
        return {"set": {key: value}, "previous": previous, "changed": bool(changed),
                "file": str(cfg.home / "config.toml"),
                "effective": ("new installations only" if key.startswith("hermes.") and key.split(".", 1)[1] in native_fields
                              else "from the next run of the jobs")}

    return perform(conn, ctx, "config set", args, apply)


# --------------------------------------------------------------------------- runs

def run_done(conn: sqlite3.Connection, cfg: Config, run_id: str, note: str | None = None, *,
             checked: list[str] | None = None, digest_reviewed: bool = False) -> dict[str, Any]:
    with tx(conn):
        return _run_done(conn, cfg, run_id, note, checked=checked, digest_reviewed=digest_reviewed)


def _run_done(conn: sqlite3.Connection, cfg: Config, run_id: str, note: str | None = None, *,
             checked: list[str] | None = None, digest_reviewed: bool = False) -> dict[str, Any]:
    """The agent finished a run: what it left unhandled is released for a later run now
    (bounded attempts, then failed and reported) instead of waiting for the lease to expire."""
    run = runs.get(conn, run_id)
    if run is None or run["entry"] not in ("intake", "feedback"):
        raise OpError("NOT_FOUND", f"no intake or feedback run {run_id}")
    confirmations = {"checked": sorted(set(checked or [])), "digest_reviewed": digest_reviewed}
    if run.get("released_at"):
        if run['summary'].get('confirmations', {"checked": [], "digest_reviewed": False}) != confirmations:
            raise OpError('SETTLEMENT_CHANGED', 'this run was completed with different confirmations')
        return {"run": run_id, "duplicate": True, **(run["summary"].get("done") or {})}
    report: dict[str, Any] = {}
    with tx(conn):
        ctx = context(conn, cfg, run=run_id)
        for task_id in confirmations['checked']:
            claim = conn.execute('SELECT state, due FROM followup_checks WHERE run_id=? AND task_id=?',
                                 (run_id, task_id)).fetchone()
            if claim is None or claim['state'] not in ('claimed', 'done'):
                raise OpError('STALE_RUN', f'{task_id} is not a follow-up owned by this run')
            task = store.get_task(conn, task_id)
            if claim['state'] == 'claimed' and (task.get('follow_up_date') or '')[:10] != claim['due']:
                raise OpError('STALE_RUN', f'{task_id} follow-up date changed after this claim')
            followup_checked(conn, cfg, ctx, task_id)
        if digest_reviewed:
            digest_done(conn, cfg, ctx)
        now = clock.iso()
        if run["entry"] == "intake":
            left = [r["id"] for r in conn.execute(
                "SELECT s.id FROM source_items s JOIN batches b ON b.id=s.batch_id WHERE b.claimed_run=?"
                " AND b.state='claimed' AND s.state='batched'", (run_id,))]
            report["batches"] = batching.release_run(conn, run_id, int(cfg.get("wake.max_attempts", 3)))
            if left:
                report["left_without_outcome"] = left
            followups = [r["task_id"] for r in conn.execute(
                "SELECT task_id FROM followup_checks WHERE run_id=? AND state='claimed'", (run_id,))]
            # Due again at the next gate (bounded by the attempts of the check).
            conn.execute("UPDATE followup_checks SET claimed_at='' WHERE run_id=? AND state='claimed'", (run_id,))
            if followups:
                report["followups_not_checked"] = followups
            if conn.execute("UPDATE daily_jobs SET updated_at='' WHERE job='commit_digest' AND run_id=?"
                            " AND state='claimed'", (run_id,)).rowcount:
                report["digest"] = "not done: offered again"
        else:
            left = [r["id"] for r in conn.execute(
                "SELECT id FROM feedback WHERE claimed_run=? AND status='pending'", (run_id,))]
            if left:
                report["feedback_not_resolved"] = left
                report["feedback"] = feedback.release(conn, left, int(cfg.get("feedback.max_attempts", 3)))
        summary = {**run["summary"], "done": report, "confirmations": confirmations, **({"note": note} if note else {})}
        conn.execute("UPDATE runs SET released_at=?, agent_completed_at=COALESCE(agent_completed_at, ?), summary=?"
                     " WHERE id=?", (now, now, dumps(summary), run_id))
    return {"run": run_id, **report}
