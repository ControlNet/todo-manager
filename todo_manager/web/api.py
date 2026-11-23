"""The API's operations: one table read by the HTTP routes (``/api/*``, docs/api.md) and by the
MCP tools (``/mcp``), where a few tools each stand for several operations (``MERGED``). An operation only turns its arguments into a call of what already answers
it: the page's own ``views`` and ``actions``, or ``queries`` and ``ops`` as the command line
calls them. A write that is not one of the page's buttons is the user's own edit (``origin`` api).
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import unquote

from .. import delegation, ids, ops, queries, store
from ..config import Config
from . import actions, views

Run = Callable[[sqlite3.Connection, Config, dict[str, Any]], Any]


@dataclass(frozen=True)
class Op:
    name: str                     # the MCP tool
    method: str
    path: str                     # "/api/tasks/{id}": each {name} is an argument
    doc: str
    run: Run
    params: dict[str, dict[str, Any]] = field(default_factory=dict)   # JSON Schema of the other arguments
    required: tuple[str, ...] = ()
    tool: bool = True             # offered as an MCP tool

    @property
    def path_args(self) -> list[str]:
        return re.findall(r"\{(\w+)\}", self.path)

    def schema(self) -> dict[str, Any]:
        props = {name: {"type": "string"} for name in self.path_args} | self.params
        return {"type": "object", "properties": props, "required": [*self.path_args, *self.required]}


def error_status(code: str) -> int:
    if code == "NOT_FOUND":
        return 404
    if code in ("CONTEXT_REQUIRED", "NOT_AUTHORIZED", "NOT_THE_USER"):
        return 403
    if code in ("CLAIMED_ELSEWHERE", "CLAIMED_BY_RUN", "REQUEST_ID_REUSED", "SUPERSEDED", "ALREADY_RESOLVED",
                "STALE_RUN", "STALE_BODY", "SETTLEMENT_CHANGED"):
        return 409
    return 400


def _found(value: Any, what: str) -> Any:
    if value is None:
        raise ops.OpError("NOT_FOUND", what)
    return value


def _flag(value: Any) -> bool:
    return value is True or str(value).lower() in ("1", "true", "yes")


def _list(value: Any) -> list[str]:
    """A list argument, also given as one comma-separated string (a URL parameter)."""
    if value is None:
        return []
    return [str(v) for v in value] if isinstance(value, list) else [v for v in str(value).split(",") if v]


def _rid(a: dict[str, Any]) -> str:
    """The page names each write; an agent may leave it out (then a retry applies again)."""
    return a.get("rid") or f"api-{secrets.token_hex(8)}"


def _ctx(a: dict[str, Any]) -> ops.Ctx:
    return ops.manual(origin="api", key=a.get("key"))


def _delivery_file(c: sqlite3.Connection, cfg: Config, a: dict[str, Any]) -> Any:
    if not re.fullmatch(r"[0-9]{1,6}", a["index"]):
        raise ops.OpError("NOT_FOUND", a["index"])
    return _found(views.delivery_file(c, cfg, a["id"], int(a["index"])), a["index"])


STRING = {"type": "string"}
INTEGER = {"type": "integer"}
BOOLEAN = {"type": "boolean"}
STRINGS = {"type": "array", "items": {"type": "string"}}
FIELDS = {"type": "object", "description": "field: value; null clears it (fields as in `todo-manager task update --set`)"}
EVIDENCE = {"type": "array", "items": {"type": "string"}, "description": "source item ids, each optionally item:role"}
KEY = {"type": "string", "description": "names this write: a retry with the same key applies once"}

OPERATIONS: list[Op] = [
    # ---------------------------------------------------------------- the page's reads
    Op("me", "GET", "/api/me", "Who is signed in and the workspace language.",
       lambda c, cfg, a: views.me(c, cfg), tool=False),
    Op("stamp", "GET", "/api/stamp", "Changes whenever the data changes.",
       lambda c, cfg, a: {"stamp": views.stamp(c)}, tool=False),
    Op("overview", "GET", "/api/home",
       "Today's overview: tasks grouped by what each needs (to decide, my to-do, agent needs me, agent working,"
       " waiting on others), counts and projects.",
       lambda c, cfg, a: views.home(c, cfg)),
    Op("list_tasks", "GET", "/api/tasks", "Open tasks as cards (closed ones too with closed=true).",
       lambda c, cfg, a: views.task_list(c, cfg, project=a.get("project") or None, closed=_flag(a.get("closed"))),
       {"project": STRING, "closed": BOOLEAN}),
    Op("show_task", "GET", "/api/tasks/{id}",
       "One task: its fields, sources with their words, the conversation, agent work and history.",
       lambda c, cfg, a: _found(views.task_detail(c, cfg, a["id"]), a["id"]), tool=False),
    Op("list_projects", "GET", "/api/projects", "Projects with their open task counts.",
       lambda c, cfg, a: views.project_list(c, cfg), tool=False),
    Op("show_project", "GET", "/api/projects/{id}", "One project and its tasks.",
       lambda c, cfg, a: _found(views.project_detail(c, cfg, a["id"]), a["id"]), tool=False),
    Op("search", "GET", "/api/search", "Tasks, projects and source material containing the words.",
       lambda c, cfg, a: views.search(c, cfg, str(a.get("q") or "")[:200]), {"q": STRING}, ("q",), tool=False),
    Op("show_delivery", "GET", "/api/deliveries/{id}", "One version an agent delivered, with its notes and files.",
       lambda c, cfg, a: _found(views.delivery(c, a["id"]), a["id"]), tool=False),
    Op("delivery_file", "GET", "/api/deliveries/{id}/files/{index}", "A delivered file.", _delivery_file, tool=False),
    # ---------------------------------------------------------------- the page's writes
    Op("act_on_task", "POST", "/api/tasks/{id}/act",
       "What a button on the page does: done, todo (mine), cancel, reopen, agent (hand it to agent work),"
       " wait (params.on, params.follow_up), snooze (params.until), due (params.date), follow_up (params.date),"
       " focus (params.on), project (params.project), pause, resume (agent work).",
       lambda c, cfg, a: actions.act(c, cfg, a["id"], str(a.get("action") or ""),
                                     a["params"] if isinstance(a.get("params"), dict) else {}, _rid(a)),
       {"action": STRING, "params": {"type": "object"}, "rid": STRING}, ("action",)),
    Op("comment_on_task", "POST", "/api/tasks/{id}/comment",
       "Write on a task, as in its Notion discussion: to Hermes (default) or to the agent working on it.",
       lambda c, cfg, a: actions.comment(c, cfg, a["id"], a.get("text"), a.get("to") or "hermes", _rid(a)),
       {"text": STRING, "to": {"type": "string", "enum": ["hermes", "agent"]}, "rid": STRING}, ("text",)),
    # ---------------------------------------------------------------- the command line's writes
    Op("create_task", "POST", "/api/tasks", "Create a task.",
       lambda c, cfg, a: ops.task_create(c, cfg, _ctx(a), title=a.get("title") or "", project=a.get("project"),
                                         fields=a.get("set") or {},
                                         evidence=ops.parse_evidence(_list(a.get("evidence")), "origin"),
                                         follow_up_of=a.get("follow_up_of"), reason=a.get("reason")),
       {"title": STRING, "project": STRING, "set": FIELDS, "evidence": EVIDENCE, "follow_up_of": STRING,
        "reason": STRING, "key": KEY}, ("title",)),
    Op("update_task", "PATCH", "/api/tasks/{id}",
       "Change a task's fields (title, summary, next_step, status, due_date, follow_up_date, ...).",
       lambda c, cfg, a: ops.task_update(c, cfg, _ctx(a), a["id"],
                                         changes={**(a.get("set") or {}),
                                                  **({"change_note": a["note"]} if a.get("note") else {})},
                                         evidence=ops.parse_evidence(_list(a.get("evidence")), "context"),
                                         closed_reason=a.get("closed_reason"), reason=a.get("reason")),
       {"set": FIELDS, "note": STRING, "evidence": EVIDENCE, "closed_reason": STRING, "reason": STRING, "key": KEY}),
    Op("create_project", "POST", "/api/projects", "Create a project.",
       lambda c, cfg, a: ops.project_create(c, cfg, _ctx(a), title=a.get("title") or "", fields=a.get("set") or {},
                                            repos=_list(a.get("repos")),
                                            evidence=ops.parse_evidence(_list(a.get("evidence")), "context"),
                                            reason=a.get("reason")),
       {"title": STRING, "set": FIELDS, "repos": STRINGS, "evidence": EVIDENCE, "reason": STRING, "key": KEY},
       ("title",)),
    Op("update_project", "PATCH", "/api/projects/{id}", "Change a project's fields or its repositories.",
       lambda c, cfg, a: ops.project_update(c, cfg, _ctx(a), a["id"], changes=a.get("set") or {},
                                            repos_add=_list(a.get("repos_add")) or None,
                                            repos_remove=_list(a.get("repos_remove")) or None,
                                            evidence=ops.parse_evidence(_list(a.get("evidence")), "context"),
                                            reason=a.get("reason")),
       {"set": FIELDS, "repos_add": STRINGS, "repos_remove": STRINGS, "evidence": EVIDENCE,
        "reason": STRING, "key": KEY}),
    Op("record_feedback", "POST", "/api/feedback",
       "Hand what the user said to the next feedback run, which interprets it as a Notion comment.",
       lambda c, cfg, a: ops.feedback_record(c, request=a.get("request") or f"api-{secrets.token_hex(8)}",
                                             said=a.get("said") or "", scope=a.get("scope"), origin="web"),
       {"said": STRING, "scope": {**STRING, "description": "task:<id> | project:<id> | global"},
        "request": {**STRING, "description": "names this request: a retry with the same id records it once"}},
       ("said",)),
    # ---------------------------------------------------------------- agent work
    Op("list_delegations", "GET", "/api/delegations", "Agent work in progress.",
       lambda c, cfg, a: queries.delegate_list(c)),
    Op("show_delegation", "GET", "/api/delegations/{id}",
       "One piece of agent work: messages, sub-cards, deliveries and approvals.",
       lambda c, cfg, a: _found(queries.delegate_show(c, a["id"]), a["id"]), tool=False),
    Op("create_delegation", "POST", "/api/delegations",
       "Hand a task to an agent on the Hermes Kanban board. allow names what it may do (read, analyze, draft;"
       " an external action such as send_email only when the user asked for it).",
       lambda c, cfg, a: ops.delegate_task(c, cfg, _ctx(a), a.get("task") or "", brief=a.get("brief") or "",
                                           allowed=_list(a.get("allow")), title=a.get("title"),
                                           parallel=_flag(a.get("parallel")), start_after=a.get("start_after")),
       {"task": STRING, "brief": {**STRING, "description": "goal, background, constraints and what to deliver"},
        "allow": STRINGS, "title": STRING, "parallel": BOOLEAN,
        "start_after": {**STRING, "description": "YYYY-MM-DD HH:MM local time"}, "key": KEY}, ("task", "brief", "allow")),
    Op("message_delegation", "POST", "/api/delegations/{id}/messages",
       "Send agent work a message: supplement, revision, pause, resume, cancel or accept.",
       lambda c, cfg, a: ops.delegate_message(c, cfg, _ctx(a), a["id"], kind=a.get("kind") or "",
                                              body=a.get("body"), delivery=a.get("delivery")),
       {"kind": {"type": "string", "enum": list(ops.MESSAGE_KINDS)}, "body": STRING, "delivery": STRING, "key": KEY},
       ("kind",), tool=False),
    Op("answer_delegation", "POST", "/api/delegations/{id}/answer",
       "Answer the agent's open question; approve lists external actions (action[:detail]) the user allows.",
       lambda c, cfg, a: ops.delegate_answer(c, cfg, _ctx(a), a["id"], body=a.get("body") or "",
                                             approve=_list(a.get("approve")) or None, question=a.get("question")),
       {"body": STRING, "approve": STRINGS, "question": STRING, "key": KEY}, ("body",), tool=False),
    Op("approve_delivery", "POST", "/api/delegations/{id}/approve",
       "Approve an external action for a delivered version.",
       lambda c, cfg, a: ops.delegate_approve(c, cfg, _ctx(a), a["id"], delivery=a.get("delivery") or "",
                                              action=a.get("action") or "", detail=a.get("detail")),
       {"delivery": STRING, "action": STRING, "detail": STRING, "key": KEY}, ("delivery", "action"), tool=False),
    # ---------------------------------------------------------------- material and status
    Op("search_sources", "GET", "/api/sources",
       "Stored mail, Slack and GitHub material containing the words (mine=true: what the user wrote).",
       lambda c, cfg, a: queries.source_search(c, str(a.get("q") or ""), mine=_flag(a.get("mine")),
                                               since=a.get("since"), limit=int(a.get("limit") or 20)),
       {"q": STRING, "mine": BOOLEAN, "since": STRING, "limit": INTEGER}, ("q",), tool=False),
    Op("show_source", "GET", "/api/sources/{id}", "One item of source material with its full text.",
       lambda c, cfg, a: _found(queries.source_show(c, a["id"]), a["id"]), tool=False),
    Op("status", "GET", "/api/status", "Task counts, waiting material, agent work, last runs and problems.",
       lambda c, cfg, a: queries.status(c, cfg, details=_flag(a.get("details")))),
]

OPS = {op.name: op for op in OPERATIONS}
SHOWN = {ids.PREFIXES["task"]: "show_task", ids.PREFIXES["project"]: "show_project",
         ids.PREFIXES["delegation"]: "show_delegation", ids.PREFIXES["delivery"]: "show_delivery",
         ids.PREFIXES["source"]: "show_source"}
ANSWERS = {"answer": "answer_delegation", "approve": "approve_delivery"}


def _show(c: sqlite3.Connection, cfg: Config, a: dict[str, Any]) -> Any:
    name = SHOWN.get(str(a["id"]).partition("_")[0])
    if name is None:
        raise ops.OpError("NOT_FOUND", a["id"])
    return OPS[name].run(c, cfg, a)


def _search(c: sqlite3.Connection, cfg: Config, a: dict[str, Any]) -> Any:
    found = OPS["search_sources"].run(c, cfg, a)
    return {**OPS["search"].run(c, cfg, a), "sources": found["items"], "content_role": found["content_role"]}


def _message(c: sqlite3.Connection, cfg: Config, a: dict[str, Any]) -> Any:
    return OPS[ANSWERS.get(a.get("kind"), "message_delegation")].run(c, cfg, a)


# A client chooses among fewer tools: some are several operations, told apart by the id or the kind.
MERGED: list[Op] = [
    Op("show", "", "",
       "Read anything by its id: a task (tsk_) with its sources, conversation, agent work and history; a project"
       " (prj_) and its tasks; agent work (dlg_) with messages, sub-cards, deliveries and approvals; a delivered"
       " version (dlv_); source material (src_) with its full text.",
       _show, {"id": STRING}, ("id",)),
    Op("search", "", "",
       "Tasks, projects and stored mail, Slack and GitHub material containing the words (mine=true: material the"
       " user wrote; since: YYYY-MM-DD).",
       _search, {"q": STRING, "mine": BOOLEAN, "since": STRING, "limit": INTEGER}, ("q",)),
    Op("message_delegation", "", "",
       "Tell agent work something: supplement, revision, pause, resume, cancel or accept (body, delivery); answer"
       " its open question (body; approve lists external actions action[:detail] the user allows; question);"
       " approve an external action for a delivered version (delivery, action, detail).",
       _message, {"id": STRING, "kind": {"type": "string", "enum": [*ops.MESSAGE_KINDS, *ANSWERS]}, "body": STRING,
                  "delivery": STRING, "approve": STRINGS, "question": STRING, "action": STRING, "detail": STRING,
                  "key": KEY}, ("id", "kind")),
]

TOOLS = {op.name: op for op in [*MERGED, *OPERATIONS] if op.tool}


def route(method: str, path: str) -> tuple[Op, dict[str, str]] | None:
    """The operation at this method and path, with the arguments the path holds."""
    given = [unquote(p) for p in path.strip("/").split("/")]
    for op in OPERATIONS:
        pattern = op.path.strip("/").split("/")
        if op.method != method or len(pattern) != len(given):
            continue
        args: dict[str, str] = {}
        for want, got in zip(pattern, given):
            if want.startswith("{"):
                args[want[1:-1]] = got
            elif want != got:
                break
        else:
            return op, args
    return None


class ApiFailure(Exception):
    """An operation that did not succeed, as the HTTP status and body (and the MCP tool error) say it."""

    def __init__(self, status: int, body: dict[str, Any]):
        super().__init__(status)
        self.status, self.body = status, body


def failure(exc: Exception) -> ApiFailure | None:
    """A refused operation as the command line prints it (``{error, details}``); the page's own
    writes keep their ``{error, message}``. None: not a refusal."""
    if isinstance(exc, ops.OpError):
        return ApiFailure(error_status(exc.code), {"error": exc.code, "details": exc.details})
    if isinstance(exc, (store.StoreError, delegation.DelegationError)):
        return ApiFailure(400, {"error": "INVALID", "details": [str(exc)]})
    if isinstance(exc, actions.ActionError):
        return ApiFailure(exc.status, {"error": exc.code, "message": str(exc)})
    return None
