"""Two-way Notion sync: import human edits, push local state, collect comments.

Import (gate B, every run)
  * Tasks/Projects data sources are queried by ``last_edited_time`` (minute precision,
    so with an overlap window). Each page's editable properties are compared with the
    values last seen/written (``notion_pages.last_seen_props``); every difference is a
    human edit, applied with ``actor=human`` and recorded as feedback. Our own writes
    never show up as edits because ``last_seen_props`` is updated when we write.
  * Pages created by the user become tasks/projects (``origin=notion``).
  * Edits with workflow meaning are not guessed: "下一步由谁=Agent" without an active
    delegation becomes a pending feedback item for the model; closing a task stops its
    active delegations deterministically (accept a delivered review, cancel otherwise).

Push (outbox handlers)
  * Read-before-write: the page is fetched first; unseen human edits are imported before
    anything is written, then only the properties that differ are patched.
  * Page creation looks the task up by ``TM ID`` first; an uncertain create (timeout,
    503) is reconciled the same way before any retry, never blindly repeated.
  * Body: only the managed callout is replaced (see ``body``). An edit the user made
    inside it is handed to the feedback flow instead of being overwritten.

Comments
  * Active pages (open delegation, recent edits/comments) every run; other open tasks
    round-robin at a lower frequency; recently closed tasks and projects even less.
  * Comments by our own bot are never feedback. Edits create a new feedback version.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .. import clock, delegation, effects, feedback, ids, outbox, store
from ..config import Config
from ..db import dumps, get_meta, loads, set_meta, tx
from . import body, schema
from .client import NotionClient, NotionError, NotionUncertain, plain_text

TRASH_REASON = "页面已在 Notion 中删除"
HOME_MARKER = "数据新鲜度（由 Todo Manager 维护）"


@dataclass
class NotionState:
    tasks_data_source_id: str
    projects_data_source_id: str
    home_page_id: str | None
    bot_user_id: str | None
    task_props: schema.PropertyMap
    project_props: schema.PropertyMap

    @property
    def ready(self) -> bool:
        return bool(self.tasks_data_source_id and self.projects_data_source_id)


def _meta_json(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    return loads(get_meta(conn, key), {}) or {}


def load_state(conn: sqlite3.Connection, cfg: Config) -> NotionState:
    n = cfg.section("notion")

    def pick(key: str) -> str:
        return n.get(key) or get_meta(conn, f"notion.{key}") or ""

    return NotionState(
        tasks_data_source_id=pick("tasks_data_source_id"),
        projects_data_source_id=pick("projects_data_source_id"),
        home_page_id=pick("home_page_id") or None,
        bot_user_id=pick("bot_user_id") or None,
        task_props=schema.PropertyMap("task", _meta_json(conn, "notion.props.task")),
        project_props=schema.PropertyMap("project", _meta_json(conn, "notion.props.project")),
    )


def save_state(conn: sqlite3.Connection, **values: Any) -> None:
    with tx(conn):
        for key, value in values.items():
            set_meta(conn, f"notion.{key}", value if isinstance(value, str) else dumps(value))


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


class NotionSync:
    def __init__(self, conn: sqlite3.Connection, cfg: Config, client: NotionClient, state: NotionState | None = None):
        self.conn = conn
        self.cfg = cfg
        self.client = client
        self.state = state or load_state(conn, cfg)
        self.problems: list[str] = []

    # ================================================================== bookkeeping

    def page_row(self, page_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM notion_pages WHERE page_id=?", (page_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["last_seen_props"] = loads(row["last_seen_props"], {})
        return item

    def entity_row(self, entity_type: str, entity_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT page_id FROM notion_pages WHERE entity_type=? AND entity_id=?",
                                (entity_type, entity_id)).fetchone()
        return self.page_row(row["page_id"]) if row else None

    def _link(self, entity_type: str, entity_id: str, page: dict[str, Any], seen: dict[str, Any]) -> None:
        with tx(self.conn):
            self.conn.execute(
                "INSERT INTO notion_pages(page_id, entity_type, entity_id, url, last_edited_time, last_edited_by,"
                " last_seen_props) VALUES (?,?,?,?,?,?,?) ON CONFLICT(page_id) DO UPDATE SET"
                " entity_type=excluded.entity_type, entity_id=excluded.entity_id, url=excluded.url,"
                " last_edited_time=excluded.last_edited_time, last_edited_by=excluded.last_edited_by,"
                " last_seen_props=excluded.last_seen_props",
                (page["id"], entity_type, entity_id, page.get("url"), page.get("last_edited_time"),
                 (page.get("last_edited_by") or {}).get("id"), dumps(seen)))
            table = "tasks" if entity_type == "task" else "projects"
            self.conn.execute(f"UPDATE {table} SET notion_page_id=? WHERE id=?", (page["id"], entity_id))

    def _seen(self, page_id: str, seen: dict[str, Any], page: dict[str, Any] | None = None) -> None:
        params: list[Any] = [dumps(seen)]
        sql = "UPDATE notion_pages SET last_seen_props=?"
        if page is not None:
            sql += ", last_edited_time=?, last_edited_by=?"
            params += [page.get("last_edited_time"), (page.get("last_edited_by") or {}).get("id")]
        self.conn.execute(sql + " WHERE page_id=?", (*params, page_id))

    def pmap(self, kind: str) -> schema.PropertyMap:
        return self.state.task_props if kind == "task" else self.state.project_props

    def data_source(self, kind: str) -> str:
        return self.state.tasks_data_source_id if kind == "task" else self.state.projects_data_source_id

    # ================================================================== local -> values

    def project_page_id(self, project_id: str | None) -> str | None:
        if not project_id:
            return None
        row = self.entity_row("project", project_id)
        return row["page_id"] if row else None

    def delegation_label(self, task_id: str) -> str | None:
        order = ["quarantined", "blocked", "review", "running", "queued", "paused"]
        statuses = [d["status"] for d in delegation.for_task(self.conn, task_id, active_only=True)]
        statuses = ["queued" if s in ("pending_submit", "reconcile") else s for s in statuses]
        for status in order:
            if status in statuses:
                return status
        return None

    def source_label(self, task: dict[str, Any]) -> str:
        row = self.conn.execute(
            "SELECT s.source FROM evidence_links e JOIN source_items s ON s.id=e.source_item_id"
            " WHERE e.entity_type='task' AND e.entity_id=? ORDER BY CASE e.role WHEN 'origin' THEN 0 ELSE 1 END,"
            " s.occurred_at LIMIT 1", (task["id"],)).fetchone()
        if row:
            return row["source"]
        return {"intake": "cli", "notion": "notion", "matrix": "matrix", "migration": "migration"}.get(task["origin"], "cli")

    def task_values(self, task: dict[str, Any]) -> dict[str, Any]:
        values = {f: task.get(f) for f in ("title", "status", "next_actor", "next_step", "waiting_on", "due_date",
                                           "follow_up_date", "snooze_until", "change_note")}
        values["focus"] = bool(task.get("focus"))
        values["project_id"] = self.project_page_id(task.get("project_id"))
        values["delegation"] = self.delegation_label(task["id"])
        values["source"] = self.source_label(task)
        values["tm_id"] = task["id"]
        return values

    def project_values(self, project: dict[str, Any]) -> dict[str, Any]:
        values = {f: project.get(f) for f in ("title", "status", "domain", "goal", "next_step", "blockers",
                                              "recent_activity")}
        values["repos"] = ", ".join(project.get("repos") or []) or None
        values["recent_activity_at"] = project.get("recent_activity_at")
        values["tm_id"] = project["id"]
        return values

    def task_sections(self, task: dict[str, Any]) -> dict[str, Any]:
        tz = self.cfg.tz
        sections: dict[str, Any] = {k: task.get(k) for k in ("responsibility", "done_criteria", "summary")}
        if task.get("due_date") and task.get("due_basis"):
            sections["due_basis"] = task["due_basis"]
        if task.get("focus") and task.get("focus_reason"):
            sections["focus_reason"] = task["focus_reason"]
        sources = []
        for ev in store.evidence_for(self.conn, "task", task["id"]):
            if ev["role"] == "context":
                continue
            when = ev.get("occurred_at")
            day = clock.local_dt(tz, clock.parse(when)).strftime("%Y-%m-%d") if when else ""
            sources.append({"title": ev.get("title") or (ev.get("excerpt") or "")[:80], "url": ev.get("url"),
                            "when": day, "source": schema.SOURCE_LABELS.get(ev["source"], ev["source"])})
        sections["sources"] = sources[-12:] if len(sources) > 12 else sources
        dels = delegation.for_task(self.conn, task["id"])
        status_names = {**schema.DELEGATION_LABELS, "pending_submit": "排队中", "reconcile": "排队中",
                        "ended": "已结束", "cancelled": "已取消"}
        shown = [d for d in dels if d["status"] in delegation.ACTIVE] + \
                [d for d in dels if d["status"] not in delegation.ACTIVE][-3:]
        sections["delegations"] = [
            {"title": d["title"], "status": status_names.get(d["status"], d["status"]),
             "versions": self.conn.execute("SELECT COUNT(*) FROM deliveries WHERE delegation_id=? AND kind='result'",
                                           (d["id"],)).fetchone()[0]} for d in shown]
        latest = self.conn.execute(
            "SELECT v.*, d.card_id AS card FROM deliveries v JOIN delegations d ON d.id=v.delegation_id"
            " WHERE d.task_id=? AND v.kind='result' ORDER BY v.received_at DESC LIMIT 1", (task["id"],)).fetchone()
        if latest:
            sections["delivery"] = {"seq": latest["seq"], "summary": latest["summary"], "body": latest["body"],
                                    "artifacts": loads(latest["artifacts"], []), "card": latest["card"],
                                    "when": clock.local_dt(tz, clock.parse(latest["received_at"])).strftime("%Y-%m-%d %H:%M")}
        return sections

    # ================================================================== import (pull)

    def pull(self, kind: str, overlap_minutes: int = 3) -> dict[str, int]:
        stats = {"pages": 0, "edits": 0, "created": 0, "problems": 0}
        ds = self.data_source(kind)
        scope = f"{kind}s"
        row = self.conn.execute("SELECT cursor FROM cursors WHERE source='notion' AND scope=?", (scope,)).fetchone()
        since = row["cursor"] if row and row["cursor"] else get_meta(self.conn, "notion.setup_at")
        flt = None
        if since:
            start = clock.iso(clock.parse(since) - timedelta(minutes=overlap_minutes))
            flt = {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": start}}
        try:
            pages = self.client.query_data_source(ds, filter=flt, sorts=[{"timestamp": "last_edited_time",
                                                                          "direction": "ascending"}])
        except NotionError as exc:
            _cursor_error(self.conn, scope, str(exc))
            raise
        newest = since
        for page in pages:
            stats["pages"] += 1
            outcome = self.import_page(kind, page)
            stats["edits"] += 1 if outcome == "edited" else 0
            stats["created"] += 1 if outcome == "created" else 0
            if page.get("last_edited_time") and (newest is None or page["last_edited_time"] > newest):
                newest = page["last_edited_time"]
        with tx(self.conn):
            self.conn.execute(
                "INSERT INTO cursors(source, scope, cursor, last_attempt_at, last_success_at, consecutive_failures)"
                " VALUES ('notion',?,?,?,?,0) ON CONFLICT(source, scope) DO UPDATE SET cursor=excluded.cursor,"
                " last_attempt_at=excluded.last_attempt_at, last_success_at=excluded.last_success_at,"
                " last_error=NULL, consecutive_failures=0", (scope, newest, clock.iso(), clock.iso()))
        stats["problems"] = len(self.problems)
        return stats

    def import_page(self, kind: str, page: dict[str, Any]) -> str:
        """Apply unseen human edits of one page. Returns created|edited|unchanged|ignored."""
        values, problems = schema.parse_page(kind, page, self.pmap(kind))
        for problem in problems:
            self.problems.append(f"{page['id']}: {problem}")
        row = self.page_row(page["id"])
        if row is None:
            return self._import_new(kind, page, values)
        if page.get("in_trash"):
            if kind == "task" and not row["trashed"]:
                self.page_trashed(row)
            return "edited"
        if row["trashed"]:
            self._page_restored(kind, row, page)
        seen = row["last_seen_props"]
        current = schema.comparable(kind, values)
        changed = {f: current.get(f) for f in schema.editable_fields(kind) if current.get(f) != seen.get(f)}
        # Unmappable values (e.g. an unknown status option) are reported, not imported.
        for f in list(changed):
            if current.get(f) is None and any(p.field == f for p in schema.PROPS[kind] if p.type == "select") \
                    and _select_raw_present(page, self.pmap(kind), f):
                changed.pop(f)
        if not changed:
            with tx(self.conn):
                self._seen(page["id"], {**seen, **{k: v for k, v in current.items() if k not in schema.editable_fields(kind)}},
                           page)
            return "unchanged"
        if kind == "task":
            self._apply_task_edit(row, page, changed, seen, current)
        else:
            self._apply_project_edit(row, page, changed, seen, current)
        return "edited"

    def _import_new(self, kind: str, page: dict[str, Any], values: dict[str, Any]) -> str:
        if page.get("in_trash"):
            return "ignored"
        tm_id = values.get("tm_id")
        current = schema.comparable(kind, values)
        if kind == "task":
            if tm_id and store.get_task(self.conn, tm_id):
                existing = self.entity_row("task", tm_id)
                if existing and existing["page_id"] != page["id"]:
                    self.problems.append(f"duplicate Notion page {page['id']} for task {tm_id} "
                                         f"(linked page {existing['page_id']})")
                    return "ignored"
                self._link("task", tm_id, page, current)   # recovery: page exists, link was lost
                return "unchanged"
            title = values.get("title") or "(未命名)"
            project_id = self._local_project(values.get("project_id"))
            fields = {k: values.get(k) for k in ("next_step", "waiting_on", "due_date", "follow_up_date",
                                                 "snooze_until") if values.get(k)}
            with tx(self.conn):
                tid = store.create_task(self.conn, title, origin="notion", actor="human", cause_ref=page["id"],
                                        project_id=project_id, status=values.get("status") or "todo",
                                        next_actor=values.get("next_actor") or "me",
                                        focus=bool(values.get("focus")), notion_page_id=None, **fields)
                self._link("task", tid, page, {**current, "tm_id": None})
                feedback.record(self.conn, origin="notion_page", source_id=page["id"], source_version="created",
                                text=title, scope_type="task", scope_id=tid, kind="new_task",
                                author=(page.get("last_edited_by") or {}).get("id"),
                                payload={"page_id": page["id"], "values": current})
                effects.task_push(self.conn, tid)
            return "created"
        if tm_id and store.get_project(self.conn, tm_id):
            self._link("project", tm_id, page, current)
            return "unchanged"
        title = values.get("title") or "(未命名项目)"
        with tx(self.conn):
            pid = store.create_project(self.conn, title, origin="notion", actor="human", cause_ref=page["id"],
                                       domain=values.get("domain"), goal=values.get("goal"),
                                       next_step=values.get("next_step"), status=values.get("status") or "active",
                                       repos=_split_repos(values.get("repos")))
            self._link("project", pid, page, {**current, "tm_id": None})
            feedback.record(self.conn, origin="notion_page", source_id=page["id"], source_version="created",
                            text=title, scope_type="project", scope_id=pid, kind="new_project", status="applied",
                            payload={"page_id": page["id"]})
            effects.project_push(self.conn, pid)
        return "created"

    def _local_project(self, page_id: str | None) -> str | None:
        if not page_id:
            return None
        row = self.page_row(page_id)
        if row and row["entity_type"] == "project":
            return row["entity_id"]
        self.problems.append(f"task relation points to unknown project page {page_id}")
        return None

    def _apply_task_edit(self, row: dict[str, Any], page: dict[str, Any], changed: dict[str, Any],
                         seen: dict[str, Any], current: dict[str, Any]) -> None:
        task_id = row["entity_id"]
        local = dict(changed)
        if "project_id" in local:
            local["project_id"] = self._local_project(local["project_id"])
        author = (page.get("last_edited_by") or {}).get("id")
        with tx(self.conn):
            before = store.get_task(self.conn, task_id)
            fid, created = feedback.record(
                self.conn, origin="notion_property", source_id=page["id"],
                source_version=f"{page.get('last_edited_time')}:{_h(dumps(changed))}", text=None,
                scope_type="task", scope_id=task_id, kind="property_edit", author=author,
                payload={"changes": changed, "before": {k: seen.get(k) for k in changed}}, status="applied")
            result = store.update_task(self.conn, task_id, local, actor="human", origin="notion", cause_ref=fid)
            after = store.get_task(self.conn, task_id)
            followups = self._edit_semantics(before, after, changed, fid)
            feedback.resolve(self.conn, fid, "applied", {"applied": result.applied, "rejected": result.rejected,
                                                         "followups": followups})
            self._seen(page["id"], current, page)
            if after["status"] != current.get("status") or after["next_actor"] != current.get("next_actor"):
                effects.task_push(self.conn, task_id)   # normalised pair differs from what the user typed

    def _edit_semantics(self, before: dict[str, Any], after: dict[str, Any], changed: dict[str, Any],
                        fid: str) -> list[str]:
        """Deterministic consequences of a human property edit; ambiguous ones go to the model."""
        notes: list[str] = []
        active = delegation.for_task(self.conn, after["id"], active_only=True)
        if "status" in changed and after["status"] in store.CLOSED_STATUSES:
            return delegation.on_task_closed(self.conn, after["id"], after["status"],
                                             f"用户在 Notion 将事项标为{schema.TASK_STATUS_LABELS[after['status']]}", fid)
        if "next_actor" in changed and after["next_actor"] == "agent" and before["next_actor"] != "agent" and not active:
            feedback.record(self.conn, origin="notion_property", source_id=after["id"],
                            source_version=f"delegate:{fid}", text="用户把“下一步由谁”设为 Agent",
                            scope_type="task", scope_id=after["id"], kind="delegate_request",
                            payload={"from_feedback": fid})
            notes.append("delegate_request")
        elif "next_actor" in changed and before["next_actor"] == "agent" and after["next_actor"] != "agent" and active:
            feedback.record(self.conn, origin="notion_property", source_id=after["id"],
                            source_version=f"unassign:{fid}", text="用户把“下一步由谁”从 Agent 改走，但委派仍在进行",
                            scope_type="task", scope_id=after["id"], kind="delegate_unassigned",
                            payload={"from_feedback": fid, "delegations": [d["id"] for d in active]})
            notes.append("delegate_unassigned")
        return notes

    def _apply_project_edit(self, row: dict[str, Any], page: dict[str, Any], changed: dict[str, Any],
                            seen: dict[str, Any], current: dict[str, Any]) -> None:
        pid = row["entity_id"]
        local = {k: v for k, v in changed.items() if k != "repos"}
        project = store.get_project(self.conn, pid)
        with tx(self.conn):
            fid, _ = feedback.record(
                self.conn, origin="notion_property", source_id=page["id"],
                source_version=f"{page.get('last_edited_time')}:{_h(dumps(changed))}", text=None,
                scope_type="project", scope_id=pid, kind="property_edit",
                author=(page.get("last_edited_by") or {}).get("id"),
                payload={"changes": changed, "before": {k: seen.get(k) for k in changed}}, status="applied")
            repos_add: list[str] = []
            repos_remove: list[str] = []
            if "repos" in changed:
                new = set(_split_repos(changed["repos"]))
                old = set(project["repos"])
                repos_add, repos_remove = sorted(new - old), sorted(old - new)
            result = store.update_project(self.conn, pid, local, actor="human", origin="notion", cause_ref=fid,
                                          repos_add=repos_add, repos_remove=repos_remove)
            feedback.resolve(self.conn, fid, "applied", {"applied": result.applied, "rejected": result.rejected,
                                                         "repos_add": repos_add, "repos_remove": repos_remove})
            self._seen(page["id"], current, page)

    def page_trashed(self, row: dict[str, Any]) -> None:
        task = store.get_task(self.conn, row["entity_id"])
        with tx(self.conn):
            self.conn.execute("UPDATE notion_pages SET trashed=1 WHERE page_id=?", (row["page_id"],))
            if task and task["status"] not in store.CLOSED_STATUSES:
                fid, _ = feedback.record(self.conn, origin="notion_page", source_id=row["page_id"],
                                         source_version=f"trashed:{clock.iso()}", text=TRASH_REASON, scope_type="task",
                                         scope_id=task["id"], kind="page_trashed", status="applied")
                store.update_task(self.conn, task["id"], {"status": "cancelled"}, actor="human", origin="notion",
                                  cause_ref=fid)
                store.set_closed_reason(self.conn, task["id"], TRASH_REASON)
                delegation.on_task_closed(self.conn, task["id"], "cancelled", TRASH_REASON, fid)

    def _page_restored(self, kind: str, row: dict[str, Any], page: dict[str, Any]) -> None:
        with tx(self.conn):
            self.conn.execute("UPDATE notion_pages SET trashed=0 WHERE page_id=?", (row["page_id"],))
            if kind != "task":
                return
            task = store.get_task(self.conn, row["entity_id"])
            if task and task["status"] == "cancelled" and task.get("closed_reason") == TRASH_REASON:
                previous = row["last_seen_props"].get("status") or "todo"
                fid, _ = feedback.record(self.conn, origin="notion_page", source_id=row["page_id"],
                                         source_version=f"restored:{page.get('last_edited_time')}",
                                         text="页面已从 Notion 回收站恢复", scope_type="task", scope_id=task["id"],
                                         kind="page_restored", status="applied")
                store.update_task(self.conn, task["id"], {"status": previous}, actor="human", origin="notion",
                                  cause_ref=fid)

    # ================================================================== push (outbox handlers)

    def push_task(self, item: dict[str, Any], reconcile: bool) -> Any:
        task_id = item["payload"]["task_id"]
        task = store.get_task(self.conn, task_id)
        if task is None:
            raise outbox.Permanent(f"task {task_id} no longer exists")
        if task.get("project_id") and not self.project_page_id(task["project_id"]):
            self._push_project(task["project_id"], reconcile=False)
        row = self.entity_row("task", task_id)
        if row and row["trashed"]:
            return {"skipped": "page trashed"}
        if row is None:
            row = self._create_page("task", task, reconcile or self._uncertain_create("task", task_id, item["id"]))
            if row is None:
                return {"created": True}
        return self._update_page("task", task_id, row)

    def push_project(self, item: dict[str, Any], reconcile: bool) -> Any:
        return self._push_project(item["payload"]["project_id"], reconcile, own_id=item["id"])

    def _push_project(self, project_id: str, reconcile: bool, own_id: str | None = None) -> Any:
        project = store.get_project(self.conn, project_id)
        if project is None:
            raise outbox.Permanent(f"project {project_id} no longer exists")
        row = self.entity_row("project", project_id)
        if row and row["trashed"]:
            return {"skipped": "page trashed"}
        if row is None:
            row = self._create_page("project", project, reconcile or
                                    self._uncertain_create("project", project_id, own_id))
            if row is None:
                return {"created": True}
        return self._update_page("project", project_id, row)

    def _uncertain_create(self, kind: str, entity_id: str, own_id: str | None) -> bool:
        """Another push for the same entity is in an unknown state: look up before creating."""
        return self.conn.execute(
            "SELECT 1 FROM outbox WHERE op=? AND entity_id=? AND status IN ('uncertain','in_flight') AND id != ?",
            (f"push_{kind}", entity_id, own_id or "")).fetchone() is not None

    def find_by_tm_id(self, kind: str, entity_id: str) -> list[dict[str, Any]]:
        prop = next(p for p in schema.PROPS[kind] if p.field == "tm_id")
        return self.client.query_data_source(
            self.data_source(kind), filter={"property": self.pmap(kind).key(prop), "rich_text": {"equals": entity_id}})

    def _create_page(self, kind: str, entity: dict[str, Any], reconcile: bool) -> dict[str, Any] | None:
        matches = self.find_by_tm_id(kind, entity["id"])
        if matches:
            if len(matches) > 1:
                self.problems.append(f"{len(matches)} Notion pages carry TM ID {entity['id']}; linked the oldest")
                matches.sort(key=lambda p: p.get("created_time") or "")
            page = matches[0]
            values, _ = schema.parse_page(kind, page, self.pmap(kind))
            self._link(kind, entity["id"], page, schema.comparable(kind, values))
            return self.entity_row(kind, entity["id"])
        if reconcile and self.conn.execute(
                "SELECT 1 FROM outbox WHERE op=? AND entity_id=? AND status='uncertain'"
                " AND updated_at > ?", (f"push_{kind}", entity["id"],
                                        clock.iso(clock.now() - timedelta(seconds=50)))).fetchone():
            raise outbox.Retryable("waiting for an uncertain create to become visible", retry_after=60)
        values = self.task_values(entity) if kind == "task" else self.project_values(entity)
        props = schema.render_properties(kind, values, self.pmap(kind))
        markdown = body.render(self.task_sections(entity)) if kind == "task" else None
        parent = {"type": "data_source_id", "data_source_id": self.data_source(kind)}
        try:
            page = self.client.create_page(parent, props, markdown=markdown)
        except NotionUncertain as exc:
            if exc.committed_id:
                page = self.client.retrieve_page(exc.committed_id)
            else:
                raise outbox.Uncertain(str(exc)) from None
        self._link(kind, entity["id"], page, schema.comparable(kind, values))
        if markdown:
            # The visible-text fingerprint of what we sent identifies our region until the next read.
            self._store_region(page["id"], markdown, markdown)
        return None

    def _update_page(self, kind: str, entity_id: str, row: dict[str, Any]) -> Any:
        try:
            page = self.client.retrieve_page(row["page_id"])
        except NotionError as exc:
            if exc.not_found:
                raise outbox.Permanent(f"page {row['page_id']} is not accessible: {exc}") from None
            raise outbox.Retryable(str(exc), retry_after=exc.retry_after) from None
        if page.get("in_trash"):
            if kind == "task":
                self.page_trashed(row)
            return {"skipped": "page trashed"}
        self.import_page(kind, page)          # unseen human edits first
        row = self.entity_row(kind, entity_id)
        entity = store.get_task(self.conn, entity_id) if kind == "task" else store.get_project(self.conn, entity_id)
        values = self.task_values(entity) if kind == "task" else self.project_values(entity)
        target = schema.comparable(kind, values)
        on_page = schema.comparable(kind, schema.parse_page(kind, page, self.pmap(kind))[0])
        diff = {f for f in target if target[f] != on_page.get(f)}
        result: dict[str, Any] = {"props": sorted(diff)}
        if diff:
            props = schema.render_properties(kind, values, self.pmap(kind), only=diff)
            try:
                page = self.client.update_page(row["page_id"], props)
            except NotionUncertain as exc:
                raise outbox.Uncertain(str(exc)) from None   # a retry re-reads and re-diffs: idempotent
        with tx(self.conn):
            self._seen(row["page_id"], target, page)
        if kind == "task":
            result["body"] = self._sync_body(row["page_id"], entity)
        return result

    def _sync_body(self, page_id: str, task: dict[str, Any]) -> str:
        row = self.page_row(page_id)
        rendered = body.render(self.task_sections(task))
        if row["rendered_hash"] == _h(rendered) and row["managed_hash"]:
            return "unchanged"
        if self.conn.execute("SELECT 1 FROM feedback WHERE kind='body_edit' AND status='pending' AND scope_id=?",
                             (task["id"],)).fetchone():
            return "held: user edit inside the managed region awaits interpretation"
        doc = self.client.page_markdown(page_id)
        region = body.extract(doc.get("markdown") or "")
        if region is not None:
            fp = body.content_fingerprint(region)
            if fp == body.content_fingerprint(rendered):
                self._store_region(page_id, rendered, region)
                return "already current"
            if row["managed_hash"] and fp != row["managed_hash"]:
                feedback.record(self.conn, origin="notion_page", source_id=page_id, source_version=f"body:{fp}",
                                text=region, scope_type="task", scope_id=task["id"], kind="body_edit",
                                payload={"page_id": page_id, "region": region, "would_render": rendered})
                return "held: user edited the managed region"
            command = {"type": "update_content",
                       "update_content": {"content_updates": [{"old_str": region, "new_str": rendered}]}}
        else:
            command = {"type": "insert_content", "insert_content": {"content": rendered, "position": {"type": "start"}}}
        try:
            result = self.client.update_markdown(page_id, command)
        except NotionUncertain as exc:
            raise outbox.Uncertain(str(exc)) from None
        new_region = body.extract(result.get("markdown") or "") or rendered
        self._store_region(page_id, rendered, new_region)
        return "updated" if region is not None else "inserted"

    def _store_region(self, page_id: str, rendered: str, region: str) -> None:
        with tx(self.conn):
            self.conn.execute("UPDATE notion_pages SET rendered_hash=?, managed_hash=? WHERE page_id=?",
                              (_h(rendered), body.content_fingerprint(region), page_id))

    def accept_body_edit(self, page_id: str) -> None:
        """The user's edit inside the managed region was handled: the next push may overwrite it."""
        with tx(self.conn):
            self.conn.execute("UPDATE notion_pages SET managed_hash=NULL, rendered_hash=NULL WHERE page_id=?", (page_id,))

    def post_comment(self, item: dict[str, Any], reconcile: bool) -> Any:
        p = item["payload"]
        if p.get("page_id"):
            page_id = p["page_id"]
        elif p.get("task_id"):
            row = self.entity_row("task", p["task_id"])
            if row is None:
                raise outbox.Retryable("task page not created yet", retry_after=60)
            if row["trashed"]:
                return {"skipped": "page trashed"}
            page_id = row["page_id"]
        else:
            raise outbox.Permanent("comment without target")
        text = p["text"]
        if reconcile:
            for c in self.client.list_comments(page_id):
                if (c.get("created_by") or {}).get("id") == self.state.bot_user_id and \
                        plain_text(c.get("rich_text")).strip() == text.strip():
                    self._record_bot_comment(c, page_id, p.get("key"))
                    return {"comment_id": c["id"], "reconciled": True}
        try:
            c = self.client.create_comment(markdown=body.escape_inline(text), discussion_id=p.get("discussion_id"),
                                           page_id=None if p.get("discussion_id") else page_id)
        except NotionUncertain as exc:
            raise outbox.Uncertain(str(exc)) from None
        except NotionError as exc:
            if exc.not_found and p.get("discussion_id"):
                # The thread was resolved/deleted: post on the page instead.
                c = self.client.create_comment(markdown=body.escape_inline(text), page_id=page_id)
            else:
                raise
        self._record_bot_comment(c, page_id, p.get("key"))
        return {"comment_id": c["id"]}

    def _record_bot_comment(self, c: dict[str, Any], page_id: str, key: str | None) -> None:
        with tx(self.conn):
            self.conn.execute(
                "INSERT OR IGNORE INTO notion_comments(comment_id, page_id, discussion_id, author_id, is_bot, ref,"
                " last_edited_time, content_hash, seen_at) VALUES (?,?,?,?,1,?,?,?,?)",
                (c["id"], page_id, c.get("discussion_id"), (c.get("created_by") or {}).get("id"), key,
                 c.get("last_edited_time"), _h(plain_text(c.get("rich_text"))), clock.iso()))

    # ================================================================== comments (poll)

    def comment_targets(self, cold_limit: int = 30) -> list[dict[str, Any]]:
        now = clock.now()
        hot_since = clock.iso(now - timedelta(hours=48))
        talk_since = clock.iso(now - timedelta(days=7))
        cold_before = clock.iso(now - timedelta(minutes=int(self.cfg.get("notion.cold_comment_poll_minutes", 60))))
        closed_before = clock.iso(now - timedelta(hours=int(self.cfg.get("notion.history_poll_hours", 24))))
        closed_since = clock.iso(now - timedelta(days=30))
        active = ",".join(repr(s) for s in delegation.ACTIVE)
        open_ = ",".join(repr(s) for s in store.OPEN_STATUSES)
        targets: dict[str, dict[str, Any]] = {}

        def add(rows: list[sqlite3.Row], reason: str) -> None:
            for r in rows:
                targets.setdefault(r["page_id"], {**dict(r), "reason": reason})

        add(self.conn.execute(
            f"SELECT p.page_id, p.entity_type, p.entity_id FROM notion_pages p JOIN tasks t ON t.id=p.entity_id"
            f" WHERE p.entity_type='task' AND p.trashed=0 AND (t.id IN (SELECT task_id FROM delegations WHERE status IN"
            f" ({active})) OR t.updated_at>=? OR p.last_edited_time>=? OR p.last_comment_at>=?)",
            (hot_since, hot_since, talk_since)).fetchall(), "hot")
        if self.state.home_page_id:
            targets.setdefault(self.state.home_page_id, {"page_id": self.state.home_page_id, "entity_type": "home",
                                                         "entity_id": None, "reason": "home"})
        add(self.conn.execute(
            f"SELECT p.page_id, p.entity_type, p.entity_id FROM notion_pages p JOIN tasks t ON t.id=p.entity_id"
            f" WHERE p.entity_type='task' AND p.trashed=0 AND t.status IN ({open_})"
            f" AND (p.comments_checked_at IS NULL OR p.comments_checked_at<?) ORDER BY p.comments_checked_at LIMIT ?",
            (cold_before, cold_limit)).fetchall(), "open")
        add(self.conn.execute(
            "SELECT p.page_id, p.entity_type, p.entity_id FROM notion_pages p JOIN projects j ON j.id=p.entity_id"
            " WHERE p.entity_type='project' AND p.trashed=0 AND j.status IN ('active','paused')"
            " AND (p.comments_checked_at IS NULL OR p.comments_checked_at<?) ORDER BY p.comments_checked_at LIMIT ?",
            (cold_before, cold_limit)).fetchall(), "project")
        add(self.conn.execute(
            f"SELECT p.page_id, p.entity_type, p.entity_id FROM notion_pages p JOIN tasks t ON t.id=p.entity_id"
            f" WHERE p.entity_type='task' AND p.trashed=0 AND t.status NOT IN ({open_}) AND t.closed_at>=?"
            f" AND (p.comments_checked_at IS NULL OR p.comments_checked_at<?) ORDER BY p.comments_checked_at LIMIT ?",
            (closed_since, closed_before, cold_limit)).fetchall(), "closed")
        return list(targets.values())

    def poll_comments(self, cold_limit: int = 30) -> dict[str, int]:
        stats = {"pages": 0, "new": 0, "edited": 0, "errors": 0}
        user_ids = set(self.cfg.get("identity.notion_user_ids") or [])
        for target in self.comment_targets(cold_limit):
            page_id = target["page_id"]
            try:
                comments = self.client.list_comments(page_id)
            except NotionError as exc:
                stats["errors"] += 1
                if exc.not_found and target["entity_type"] == "task":
                    self._check_missing(page_id)
                else:
                    self.problems.append(f"comments of {page_id}: {exc}")
                continue
            stats["pages"] += 1
            for c in comments:
                outcome = self._comment(c, target, user_ids)
                if outcome in ("new", "edited"):
                    stats[outcome] += 1
            if target["entity_type"] != "home":
                with tx(self.conn):
                    self.conn.execute("UPDATE notion_pages SET comments_checked_at=? WHERE page_id=?",
                                      (clock.iso(), page_id))
        return stats

    def _check_missing(self, page_id: str) -> None:
        try:
            page = self.client.retrieve_page(page_id)
        except NotionError as exc:
            if exc.not_found:
                self.problems.append(f"page {page_id} is no longer accessible (deleted or unshared)")
            return
        if page.get("in_trash"):
            row = self.page_row(page_id)
            if row and not row["trashed"]:
                self.page_trashed(row)

    def _comment(self, c: dict[str, Any], target: dict[str, Any], user_ids: set[str]) -> str:
        author = (c.get("created_by") or {}).get("id")
        text = plain_text(c.get("rich_text")).strip()
        attachments = [a.get("category") for a in c.get("attachments") or []]
        digest = _h(text + "|" + ",".join(str(a) for a in attachments))
        prev = self.conn.execute("SELECT * FROM notion_comments WHERE comment_id=?", (c["id"],)).fetchone()
        if author and author == self.state.bot_user_id:
            if prev is None:
                self._record_bot_comment(c, target["page_id"], None)
            return "bot"
        if prev is not None and prev["is_bot"]:
            return "bot"
        if prev is not None and prev["content_hash"] == digest:
            return "seen"
        scope_type = {"task": "task", "project": "project"}.get(target["entity_type"], "global")
        bot = self.conn.execute("SELECT ref FROM notion_comments WHERE discussion_id=? AND is_bot=1 AND ref IS NOT NULL"
                                " ORDER BY seen_at DESC LIMIT 1", (c.get("discussion_id"),)).fetchone()
        payload: dict[str, Any] = {"page_id": target["page_id"], "comment_id": c["id"],
                                   "discussion_id": c.get("discussion_id"), "attachments": attachments}
        if bot and bot["ref"]:
            payload["in_reply_to"] = bot["ref"]
            if bot["ref"].startswith("dlv:"):
                dlv = self.conn.execute("SELECT id, delegation_id, kind, seq FROM deliveries WHERE id=?",
                                        (bot["ref"][4:],)).fetchone()
                if dlv:
                    payload.update(delegation_id=dlv["delegation_id"], delivery_id=dlv["id"],
                                   delivery_kind=dlv["kind"], delivery_seq=dlv["seq"])
        if prev is not None:
            payload["previous_text_hash"] = prev["content_hash"]
        is_user = (author in user_ids) if user_ids else True
        if not user_ids:
            payload["identity_unverified"] = True
        with tx(self.conn):
            fid, created = feedback.record(
                self.conn, origin="notion_comment", source_id=c["id"],
                source_version=c.get("last_edited_time") or digest, text=text, scope_type=scope_type,
                scope_id=target.get("entity_id"), author=author, author_is_user=is_user,
                kind="comment" if prev is None else "comment_edit", payload=payload,
                reply_to=c.get("discussion_id"))
            if created and prev is not None:
                feedback.supersede_older(self.conn, "notion_comment", c["id"], fid)
            self.conn.execute(
                "INSERT INTO notion_comments(comment_id, page_id, discussion_id, author_id, is_bot, last_edited_time,"
                " content_hash, feedback_id, seen_at) VALUES (?,?,?,?,0,?,?,?,?) ON CONFLICT(comment_id) DO UPDATE SET"
                " last_edited_time=excluded.last_edited_time, content_hash=excluded.content_hash,"
                " feedback_id=excluded.feedback_id, seen_at=excluded.seen_at",
                (c["id"], target["page_id"], c.get("discussion_id"), author, c.get("last_edited_time"), digest, fid,
                 clock.iso()))
            if target["entity_type"] == "task":
                self.conn.execute("UPDATE notion_pages SET last_comment_at=? WHERE page_id=?",
                                  (clock.iso(), target["page_id"]))
        return "edited" if prev is not None else "new"

    # ================================================================== home page

    def sync_home(self, sections: dict[str, Any]) -> str:
        """Rewrite the freshness callout on the home page when its content changed."""
        home = self.state.home_page_id
        if not home:
            return "no home page"
        rendered = body.render(sections, marker=HOME_MARKER)
        if get_meta(self.conn, "notion.home_hash") == _h(rendered):
            return "unchanged"
        doc = self.client.page_markdown(home)
        region = body.extract(doc.get("markdown") or "", HOME_MARKER)
        if region is not None and body.content_fingerprint(region) == body.content_fingerprint(rendered):
            command = None
        elif region is not None:
            command = {"type": "update_content",
                       "update_content": {"content_updates": [{"old_str": region, "new_str": rendered}]}}
        else:
            command = {"type": "insert_content", "insert_content": {"content": rendered, "position": {"type": "start"}}}
        if command:
            self.client.update_markdown(home, command)
        with tx(self.conn):
            set_meta(self.conn, "notion.home_hash", _h(rendered))
        return "updated" if command else "already current"

    # ================================================================== dirty scan

    def enqueue_dirty(self, closed_days: int = 30) -> int:
        """Queue pushes for entities whose Notion values differ from what was last written."""
        count = 0
        since = clock.iso(clock.now() - timedelta(days=closed_days))
        open_ = ",".join(repr(s) for s in store.OPEN_STATUSES)
        for t in self.conn.execute(f"SELECT * FROM tasks WHERE status IN ({open_}) OR closed_at>=?", (since,)).fetchall():
            row = self.entity_row("task", t["id"])
            if row and row["trashed"]:
                continue
            task = dict(t)
            target = schema.comparable("task", self.task_values(task))
            stale_body = row is not None and row["rendered_hash"] != _h(body.render(self.task_sections(task)))
            if row is None or row["last_seen_props"] != {**row["last_seen_props"], **target} or stale_body:
                if effects.task_push(self.conn, t["id"]):
                    count += 1
        for p in store.list_projects(self.conn, ("active", "paused", "done", "archived")):
            row = self.entity_row("project", p["id"])
            if row and row["trashed"]:
                continue
            target = schema.comparable("project", self.project_values(p))
            if row is None or row["last_seen_props"] != {**row["last_seen_props"], **target}:
                if effects.project_push(self.conn, p["id"]):
                    count += 1
        return count

    def handlers(self) -> dict[tuple[str, str], Any]:
        return {("notion", "push_task"): self.push_task, ("notion", "push_project"): self.push_project,
                ("notion", "comment"): self.post_comment}


def _select_raw_present(page: dict[str, Any], pmap: schema.PropertyMap, field: str) -> bool:
    prop = next(p for p in schema.PROPS[pmap.kind] if p.field == field)
    key = pmap.ids.get(field)
    props = page.get("properties") or {}
    raw = next((v for v in props.values() if isinstance(v, dict) and v.get("id") == key), None) if key \
        else props.get(prop.name)
    return bool(raw and raw.get("select"))


def _split_repos(text: str | None) -> list[str]:
    return [r.strip() for r in (text or "").replace("\n", ",").split(",") if r.strip()]


def _cursor_error(conn: sqlite3.Connection, scope: str, error: str) -> None:
    with tx(conn):
        conn.execute(
            "INSERT INTO cursors(source, scope, last_attempt_at, last_error, consecutive_failures)"
            " VALUES ('notion',?,?,?,1) ON CONFLICT(source, scope) DO UPDATE SET"
            " last_attempt_at=excluded.last_attempt_at, last_error=excluded.last_error,"
            " consecutive_failures=cursors.consecutive_failures+1", (scope, clock.iso(), error[:1000]))


def new_setup_marker() -> str:
    return ids.new_id("run")
