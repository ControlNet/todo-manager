"""Business rules: responsibility recognition and personal preferences.

Rules are data, not code. They change only through explicit user feedback (or are
stored as ``suggested`` when the agent generalises from a single correction). Every
change bumps the global ``rules_version`` so that stale semantic judgements can be
detected before they are written.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import clock, ids
from .db import get_meta, rules_version, set_meta, tx

SCOPES = ("global", "source", "project", "task")
STATUSES = ("active", "suggested", "retired")


class RuleError(ValueError):
    pass


def _bump(conn: sqlite3.Connection) -> int:
    version = rules_version(conn) + 1
    set_meta(conn, "rules_version", str(version))
    return version


def add_rule(conn: sqlite3.Connection, *, title: str, body: str, scope_type: str = "global",
             scope_id: str | None = None, status: str = "active", actor: str = "human",
             feedback_id: str | None = None, rule_id: str | None = None) -> str:
    if scope_type not in SCOPES:
        raise RuleError(f"invalid scope {scope_type!r}")
    if status not in STATUSES:
        raise RuleError(f"invalid status {status!r}")
    if status == "active" and actor == "agent" and not feedback_id:
        raise RuleError("an agent may only activate a rule from explicit user feedback")
    rid = rule_id or ids.new_id("rule")
    now = clock.iso()
    with tx(conn):
        version = _bump(conn)
        conn.execute(
            "INSERT INTO rules(id, scope_type, scope_id, title, body, status, revision, from_feedback_id,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,1,?,?,?)",
            (rid, scope_type, scope_id, title.strip(), body.strip(), status, feedback_id, now, now))
        conn.execute(
            "INSERT INTO rule_revisions(rule_id, revision, title, body, status, actor, feedback_id, rules_version, at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, 1, title.strip(), body.strip(), status, actor, feedback_id, version, now))
    return rid


def update_rule(conn: sqlite3.Connection, rule_id: str, *, title: str | None = None, body: str | None = None,
                status: str | None = None, actor: str = "human", feedback_id: str | None = None) -> int:
    if status is not None and status not in STATUSES:
        raise RuleError(f"invalid status {status!r}")
    with tx(conn):
        row = conn.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
        if row is None:
            raise RuleError(f"rule {rule_id} not found")
        new_status = status or row["status"]
        if new_status == "active" and row["status"] != "active" and actor == "agent" and not feedback_id:
            raise RuleError("an agent may only activate a rule from explicit user feedback")
        revision = row["revision"] + 1
        new_title = (title or row["title"]).strip()
        new_body = (body or row["body"]).strip()
        version = _bump(conn)
        now = clock.iso()
        conn.execute("UPDATE rules SET title=?, body=?, status=?, revision=?, updated_at=? WHERE id=?",
                     (new_title, new_body, new_status, revision, now, rule_id))
        conn.execute(
            "INSERT INTO rule_revisions(rule_id, revision, title, body, status, actor, feedback_id, rules_version, at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (rule_id, revision, new_title, new_body, new_status, actor, feedback_id, version, now))
    return version


def list_rules(conn: sqlite3.Connection, statuses: tuple[str, ...] = ("active",),
               scope_type: str | None = None, scope_id: str | None = None) -> list[dict[str, Any]]:
    marks = ",".join("?" for _ in statuses)
    sql = f"SELECT * FROM rules WHERE status IN ({marks})"
    params: list[Any] = list(statuses)
    if scope_type:
        sql += " AND scope_type=?"
        params.append(scope_type)
    if scope_id:
        sql += " AND scope_id=?"
        params.append(scope_id)
    sql += " ORDER BY scope_type, created_at"
    return [dict(r) for r in conn.execute(sql, params)]


def relevant_rules(conn: sqlite3.Connection, *, sources: set[str] | None = None,
                   project_ids: set[str] | None = None, task_ids: set[str] | None = None) -> list[dict[str, Any]]:
    """Active rules that apply to the given context (global + matching scopes)."""
    result = []
    for rule in list_rules(conn):
        scope = rule["scope_type"]
        if scope == "global":
            result.append(rule)
        elif scope == "source" and (sources is None or rule["scope_id"] in sources):
            result.append(rule)
        elif scope == "project" and project_ids and rule["scope_id"] in project_ids:
            result.append(rule)
        elif scope == "task" and task_ids and rule["scope_id"] in task_ids:
            result.append(rule)
    return result


def render_rules(rules: list[dict[str, Any]]) -> str:
    lines = []
    for rule in rules:
        scope = rule["scope_type"] + (f":{rule['scope_id']}" if rule.get("scope_id") else "")
        lines.append(f"- [{rule['id']} r{rule['revision']} {scope}] {rule['title']}: {rule['body']}")
    return "\n".join(lines) if lines else "(no active rules)"


def rules_meta(conn: sqlite3.Connection) -> dict[str, Any]:
    return {"rules_version": rules_version(conn), "updated": get_meta(conn, "rules_updated_at")}
