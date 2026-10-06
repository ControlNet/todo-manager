"""Tasks, projects, evidence links, history and decisions.

Write rules that protect human corrections:

* Every task/project write bumps ``version`` and records field-level history.
* ``field_meta`` remembers who last set each field and at which version.
* Agent/program writes carry ``based_on_version`` (the version the agent read). A field
  last set by a human *after* that version is never overwritten (stale judgement).
* A field set by a human that the agent did see may only be changed with an explicit
  ``override_human`` flag plus evidence; the change is surfaced as a change note.
* A task closed (done/cancelled) by a human cannot be reopened by agent/program writes;
  genuinely new responsibility becomes a linked follow-up task instead.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import clock, ids
from .db import dumps, loads, row_dict, tx, get_meta, set_meta

TASK_STATUSES = ("todo", "in_progress", "waiting", "decision", "review", "done", "cancelled")
OPEN_STATUSES = ("todo", "in_progress", "waiting", "decision", "review")
CLOSED_STATUSES = ("done", "cancelled")
NEXT_ACTORS = ("me", "agent", "external")
TASK_FIELDS = (
    "title", "project_id", "status", "next_actor", "waiting_on", "responsibility", "next_step",
    "done_criteria", "summary", "due_date", "due_basis", "follow_up_date", "snooze_until",
    "focus", "focus_reason", "change_note",
)
PROJECT_FIELDS = (
    "title", "domain", "status", "goal", "next_step", "blockers", "recent_activity",
)
ACTORS = ("human", "agent", "program")


class StoreError(ValueError):
    pass


def normalize_state(status: str, next_actor: str | None) -> tuple[str, str]:
    """Return a consistent (status, next_actor) pair.

    Mapping (documented in docs/notion-schema.md):
      waiting   -> next_actor external (we wait for someone else)
      decision  -> me (a real choice only the user can make)
      review    -> me (agent delivered; user accepts or revises)
      done/cancelled -> next_actor kept as 'me' (irrelevant once closed)
      todo / in_progress -> me or agent
    """
    if status not in TASK_STATUSES:
        raise StoreError(f"invalid status {status!r}")
    actor = next_actor or "me"
    if actor not in NEXT_ACTORS:
        raise StoreError(f"invalid next_actor {actor!r}")
    if status == "waiting":
        return status, "external"
    if status in ("decision", "review", "done", "cancelled"):
        return status, "me"
    if actor == "external":
        # "someone else acts next" is the definition of waiting.
        return "waiting", "external"
    return status, actor


@dataclass
class WriteResult:
    entity_id: str
    version: int
    applied: dict[str, Any] = field(default_factory=dict)
    rejected: dict[str, str] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.applied)


def _now() -> str:
    return clock.iso()


def add_history(conn: sqlite3.Connection, entity_type: str, entity_id: str, version: int,
                actor: str, origin: str, cause_ref: str | None, changes: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO history(entity_type, entity_id, version, actor, origin, cause_ref, changes, at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (entity_type, entity_id, version, actor, origin, cause_ref, dumps(changes), _now()))


# --------------------------------------------------------------------------- projects

def create_project(conn: sqlite3.Connection, title: str, *, origin: str, actor: str = "agent",
                   cause_ref: str | None = None, domain: str | None = None,
                   goal: str | None = None, next_step: str | None = None,
                   repos: Iterable[str] = (), project_id: str | None = None,
                   notion_page_id: str | None = None, status: str = "active") -> str:
    pid = project_id or ids.new_id("project")
    now = _now()
    repos = sorted(set(repos))
    meta = {f: {"by": actor, "v": 1} for f in ("title", "domain", "goal", "next_step", "status", "repos")}
    with tx(conn):
        conn.execute(
            "INSERT INTO projects(id, title, domain, status, goal, next_step, origin, notion_page_id,"
            " field_meta, version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,1,?,?)",
            (pid, title.strip(), domain, status, goal, next_step, origin, notion_page_id,
             dumps(meta), now, now))
        for repo in repos:
            conn.execute("INSERT OR IGNORE INTO project_repos(project_id, repo) VALUES (?,?)", (pid, repo))
        add_history(conn, "project", pid, 1, actor, origin, cause_ref,
                    {"created": [None, {"title": title, "domain": domain, "goal": goal, "repos": repos}]})
    return pid


def get_project(conn: sqlite3.Connection, project_id: str) -> dict[str, Any] | None:
    row = row_dict(conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())
    if row:
        row["repos"] = [r["repo"] for r in conn.execute(
            "SELECT repo FROM project_repos WHERE project_id=? ORDER BY repo", (project_id,))]
    return row


def list_projects(conn: sqlite3.Connection, statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
    statuses = tuple(statuses or ("active", "paused"))
    marks = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"SELECT * FROM projects WHERE status IN ({marks}) ORDER BY title", statuses).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["repos"] = [r["repo"] for r in conn.execute(
            "SELECT repo FROM project_repos WHERE project_id=?", (row["id"],))]
        result.append(item)
    return result


def update_project(conn: sqlite3.Connection, project_id: str, changes: dict[str, Any], *,
                   actor: str, origin: str, cause_ref: str | None = None,
                   based_on_version: int | None = None, override_human: bool = False,
                   repos_add: Iterable[str] = (), repos_remove: Iterable[str] = ()) -> WriteResult:
    unknown = set(changes) - set(PROJECT_FIELDS)
    if unknown:
        raise StoreError(f"unknown project fields: {sorted(unknown)}")
    with tx(conn):
        row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise StoreError(f"project {project_id} not found")
        current = dict(row)
        current['repos'] = sorted(r['repo'] for r in conn.execute(
            'SELECT repo FROM project_repos WHERE project_id=?', (project_id,)))
        changes = dict(changes)
        desired = sorted((set(current['repos']) | set(repos_add)) - set(repos_remove))
        if desired != current['repos']:
            changes['repos'] = desired
        result = _apply_field_changes(conn, "projects", "project", current, changes, actor=actor,
                                      origin=origin, cause_ref=cause_ref,
                                      based_on_version=based_on_version, override_human=override_human)
    return result


# --------------------------------------------------------------------------- tasks

def create_task(conn: sqlite3.Connection, title: str, *, origin: str, actor: str = "agent",
                cause_ref: str | None = None, project_id: str | None = None,
                status: str = "todo", next_actor: str = "me", task_id: str | None = None,
                notion_page_id: str | None = None, **fields: Any) -> str:
    unknown = set(fields) - set(TASK_FIELDS)
    if unknown:
        raise StoreError(f"unknown task fields: {sorted(unknown)}")
    if not title or not title.strip():
        raise StoreError("task title is required")
    status, next_actor = normalize_state(status, next_actor)
    if project_id and conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
        raise StoreError(f"project {project_id} not found")
    tid = task_id or ids.new_id("task")
    now = _now()
    values = {"title": title.strip(), "project_id": project_id, "status": status,
              "next_actor": next_actor, **fields}
    if "focus" in values:
        values["focus"] = 1 if values["focus"] else 0
    meta = {f: {"by": actor, "v": 1} for f, v in values.items() if v is not None}
    columns = ["id", *values.keys(), "origin", "notion_page_id", "field_meta", "version",
               "created_at", "updated_at"]
    params = [tid, *values.values(), origin, notion_page_id, dumps(meta), 1, now, now]
    if status in CLOSED_STATUSES:
        columns.append("closed_at")
        params.append(now)
    with tx(conn):
        conn.execute(
            f"INSERT INTO tasks({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", params)
        add_history(conn, "task", tid, 1, actor, origin, cause_ref,
                    {"created": [None, {k: v for k, v in values.items() if v is not None}]})
    return tid


def get_task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    return row_dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())


def get_task_by_notion(conn: sqlite3.Connection, page_id: str) -> dict[str, Any] | None:
    return row_dict(conn.execute("SELECT * FROM tasks WHERE notion_page_id=?", (page_id,)).fetchone())


def list_tasks(conn: sqlite3.Connection, statuses: Iterable[str] | None = None,
               project_id: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    statuses = tuple(statuses or OPEN_STATUSES)
    marks = ",".join("?" for _ in statuses)
    sql = f"SELECT * FROM tasks WHERE status IN ({marks})"
    params: list[Any] = list(statuses)
    if project_id:
        sql += " AND project_id=?"
        params.append(project_id)
    sql += " ORDER BY COALESCE(due_date, '9999') , updated_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql, params)]


def update_task(conn: sqlite3.Connection, task_id: str, changes: dict[str, Any], *, actor: str,
                origin: str, cause_ref: str | None = None, based_on_version: int | None = None,
                override_human: bool = False) -> WriteResult:
    unknown = set(changes) - set(TASK_FIELDS)
    if unknown:
        raise StoreError(f"unknown task fields: {sorted(unknown)}")
    if actor not in ACTORS:
        raise StoreError(f"invalid actor {actor!r}")
    with tx(conn):
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise StoreError(f"task {task_id} not found")
        current = dict(row)
        changes = dict(changes)
        if "focus" in changes:
            changes["focus"] = 1 if changes["focus"] else 0
        if "project_id" in changes and changes["project_id"]:
            if conn.execute("SELECT 1 FROM projects WHERE id=?", (changes["project_id"],)).fetchone() is None:
                raise StoreError(f"project {changes['project_id']} not found")
        # Keep status/next_actor consistent.
        if "status" in changes or "next_actor" in changes:
            status = changes.get("status", current["status"])
            actor_field = changes.get("next_actor", current["next_actor"])
            if "status" in changes and "next_actor" not in changes and status in ("todo", "in_progress"):
                actor_field = current["next_actor"] if current["next_actor"] != "external" else "me"
            status, actor_field = normalize_state(status, actor_field)
            if "status" in changes or status != current["status"]:
                changes["status"] = status
            if "next_actor" in changes or actor_field != current["next_actor"]:
                changes["next_actor"] = actor_field
        result = WriteResult(task_id, current["version"])
        meta = loads(current["field_meta"], {})
        if actor != "human" and current["status"] in CLOSED_STATUSES and \
                meta.get("status", {}).get("by") == "human":
            reopening = "status" in changes and changes["status"] not in CLOSED_STATUSES
            if reopening:
                result.rejected["status"] = "human_closed: create a linked follow-up task instead"
                changes.pop("status", None)
                changes.pop("next_actor", None)
        _keep_state_pair(current, meta, changes, result, actor=actor, based_on_version=based_on_version,
                         override_human=override_human)
        pending_key = 'notion.project_pending:' + task_id
        pending_relation = conn.execute('SELECT value FROM meta WHERE key=?', (pending_key,)).fetchone()
        force_fields = set()
        if pending_relation and 'project_id' in changes:
            # An unresolved remote choice is still a human choice, even if the local
            # column is NULL. Apply the same protection before cancelling that intent.
            request = loads(pending_relation['value'], {})
            intent_meta = {**meta, 'project_id': {'by': request.get('actor', 'human'), 'v': request.get('version', current['version'])}}
            why = _protected(intent_meta, 'project_id', actor=actor, based_on_version=based_on_version,
                             override_human=override_human)
            if why:
                result.rejected['project_id'] = why
                changes.pop('project_id')
            else:
                force_fields.add('project_id')
        result2 = _apply_field_changes(conn, "tasks", "task", current, changes, actor=actor,
                                       origin=origin, cause_ref=cause_ref,
                                       based_on_version=based_on_version,
                                       override_human=override_human, force_fields=force_fields)
        result2.rejected.update(result.rejected)
        applied = result2.applied
        if 'project_id' in applied and pending_relation:
            conn.execute('DELETE FROM meta WHERE key=?', (pending_key,))
            add_history(conn, 'task', task_id, result2.version, actor, origin, cause_ref,
                        {'pending_project': [loads(pending_relation['value'], {}), None]})
        if "status" in applied:
            new_status = applied["status"]
            if new_status in CLOSED_STATUSES and current["status"] not in CLOSED_STATUSES:
                conn.execute("UPDATE tasks SET closed_at=? WHERE id=?", (_now(), task_id))
            elif new_status not in CLOSED_STATUSES and current["status"] in CLOSED_STATUSES:
                conn.execute("UPDATE tasks SET closed_at=NULL, closed_reason=NULL WHERE id=?", (task_id,))
    return result2


def defer_notion_project(conn: sqlite3.Connection, task_id: str, page_id: str, *, actor: str,
                        cause_ref: str) -> None:
    """Persist a verified remote relation whose Project has not been imported yet.

    It is an edit even when the local foreign key cannot change. Version it now so
    an agent that read the Task before the remote choice cannot revoke that choice.
    """
    key = 'notion.project_pending:' + task_id
    with tx(conn):
        previous = loads(get_meta(conn, key), {})
        if previous.get('page_id') == page_id:
            return
        current = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())
        result = _apply_field_changes(conn, 'tasks', 'task', current, {'project_id': current['project_id']},
                                      actor=actor, origin='notion', cause_ref=cause_ref, based_on_version=None,
                                      override_human=False, force_fields={'project_id'})
        request = {'page_id': page_id, 'actor': actor, 'version': result.version}
        set_meta(conn, key, dumps(request))
        add_history(conn, 'task', task_id, result.version, actor, 'notion', cause_ref,
                    {'pending_project': [previous or None, request]})


def set_closed_reason(conn: sqlite3.Connection, task_id: str, reason: str) -> None:
    conn.execute("UPDATE tasks SET closed_reason=? WHERE id=?", (reason, task_id))


def _protected(meta: dict[str, Any], name: str, *, actor: str, based_on_version: int | None,
               override_human: bool) -> str | None:
    """Why a non-human write may not change a field the user set (None: it may)."""
    info = meta.get(name, {})
    if actor == "human" or info.get("by") != "human":
        return None
    if based_on_version is None or int(info.get("v", 0)) > based_on_version:
        return "stale: human changed this field after the agent read it"
    if not override_human:
        return "human_set: requires override_human with evidence"
    return None


def _keep_state_pair(current: dict[str, Any], meta: dict[str, Any], changes: dict[str, Any], result: WriteResult,
                     *, actor: str, based_on_version: int | None, override_human: bool) -> None:
    """status and next_actor change together: when the protection of a user-set field would
    keep one of them, the other one is applied only if the resulting pair is still consistent."""
    pair = [f for f in ("status", "next_actor") if f in changes and changes[f] != current[f]]
    blocked = {f: why for f in pair
               if (why := _protected(meta, f, actor=actor, based_on_version=based_on_version,
                                     override_human=override_human))}
    if not blocked or len(blocked) == len(pair):
        return
    status = current["status"] if "status" in blocked else changes.get("status", current["status"])
    next_actor = current["next_actor"] if "next_actor" in blocked else changes.get("next_actor", current["next_actor"])
    if normalize_state(status, next_actor) == (status, next_actor):
        return
    for f in pair:
        if f not in blocked:
            result.rejected[f] = (f"pair: {', '.join(sorted(blocked))} is set by the user and would leave"
                                  f" {status}/{next_actor}; status and next_actor change together")
            changes.pop(f)


def _apply_field_changes(conn: sqlite3.Connection, table: str, entity: str, current: dict[str, Any],
                         changes: dict[str, Any], *, actor: str, origin: str, cause_ref: str | None,
                         based_on_version: int | None, override_human: bool,
                         force_fields: set[str] | None = None) -> WriteResult:
    meta = loads(current["field_meta"], {})
    result = WriteResult(current["id"], current["version"])
    to_apply: dict[str, Any] = {}
    for name, value in changes.items():
        if current.get(name) == value and name not in (force_fields or ()):
            continue
        why = _protected(meta, name, actor=actor, based_on_version=based_on_version, override_human=override_human)
        if why:
            result.rejected[name] = why
            continue
        to_apply[name] = value
    if not to_apply:
        return result
    new_version = current["version"] + 1
    now = _now()
    for name in to_apply:
        meta[name] = {"by": actor, "v": new_version, "at": now}
        if actor != "human" and override_human and changes.get(name) is not None:
            meta[name]["overrode_human"] = True
    columns = {k: v for k, v in to_apply.items() if not (entity == 'project' and k == 'repos')}
    assignments = ''.join(f"{name}=?, " for name in columns)
    conn.execute(
        f"UPDATE {table} SET {assignments}field_meta=?, version=?, updated_at=? WHERE id=? AND version=?",
        (*columns.values(), dumps(meta), new_version, now, current["id"], current["version"]))
    if conn.execute("SELECT changes()").fetchone()[0] != 1:
        raise StoreError(f"concurrent update of {entity} {current['id']}")
    if entity == 'project' and 'repos' in to_apply:
        conn.execute('DELETE FROM project_repos WHERE project_id=?', (current['id'],))
        conn.executemany('INSERT INTO project_repos(project_id,repo) VALUES (?,?)',
                         [(current['id'], repo) for repo in to_apply['repos']])
    add_history(conn, entity, current["id"], new_version, actor, origin, cause_ref,
                {name: [current.get(name), value] for name, value in to_apply.items()})
    result.applied = to_apply
    result.version = new_version
    return result


def link_tasks(conn: sqlite3.Connection, task_id: str, other_id: str, kind: str) -> None:
    conn.execute("INSERT OR IGNORE INTO task_links(task_id, other_task_id, kind, created_at) VALUES (?,?,?,?)",
                 (task_id, other_id, kind, _now()))


def task_links(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM task_links WHERE task_id=? OR other_task_id=?", (task_id, task_id))]


# --------------------------------------------------------------------------- evidence & decisions

EVIDENCE_ROLES = ("origin", "commitment", "follow_up", "completion", "context", "reminder", "cancellation")


def link_evidence(conn: sqlite3.Connection, entity_type: str, entity_id: str, source_item_id: str,
                  role: str, note: str | None = None, decision_id: str | None = None) -> bool:
    if role not in EVIDENCE_ROLES:
        raise StoreError(f"invalid evidence role {role!r}")
    cur = conn.execute(
        "INSERT OR IGNORE INTO evidence_links(entity_type, entity_id, source_item_id, role, note, decision_id,"
        " created_at) VALUES (?,?,?,?,?,?,?)",
        (entity_type, entity_id, source_item_id, role, note, decision_id, _now()))
    return cur.rowcount == 1


def evidence_for(conn: sqlite3.Connection, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT e.role, e.note, e.created_at AS linked_at, s.* FROM evidence_links e"
        " JOIN source_items s ON s.id=e.source_item_id"
        " WHERE e.entity_type=? AND e.entity_id=? ORDER BY s.occurred_at",
        (entity_type, entity_id))]


def tasks_for_threads(conn: sqlite3.Connection, thread_keys: Iterable[str]) -> list[dict[str, Any]]:
    keys = [k for k in set(thread_keys) if k]
    if not keys:
        return []
    marks = ",".join("?" for _ in keys)
    return [dict(r) for r in conn.execute(
        f"SELECT DISTINCT t.* FROM tasks t JOIN evidence_links e ON e.entity_type='task' AND e.entity_id=t.id"
        f" JOIN source_items s ON s.id=e.source_item_id WHERE s.thread_key IN ({marks})", keys)]


def record_decision(conn: sqlite3.Connection, *, run_id: str | None, op: str, target_type: str | None,
                    target_id: str | None, source_item_ids: Iterable[str] = (), category: str | None = None,
                    rationale: str | None = None, rules_version: int | None = None,
                    outcome: str = "applied", detail: Any = None) -> str:
    did = ids.new_id("decision")
    conn.execute(
        "INSERT INTO decisions(id, run_id, op, target_type, target_id, source_item_ids, category, rationale,"
        " rules_version, outcome, detail, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (did, run_id, op, target_type, target_id, dumps(list(source_item_ids)), category, rationale,
         rules_version, outcome, dumps(detail) if detail is not None else None, _now()))
    return did


def history(conn: sqlite3.Connection, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM history WHERE entity_type=? AND entity_id=? ORDER BY id",
                        (entity_type, entity_id)).fetchall()
    return [{**dict(r), "changes": loads(r["changes"], {})} for r in rows]
