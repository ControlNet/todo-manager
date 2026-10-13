"""Two-way Notion sync: import human edits, push local state, collect comments.

Import (gate B, every run)
  * Tasks/Projects data sources are queried by ``last_edited_time`` with an overlap window
    (the timestamp may be coarse, so it is never used to tell edits apart). Each page's
    editable properties are compared with the values last seen/written
    (``notion_pages.last_seen_props``). A difference that equals a write of ours whose
    response was lost (``pending_write``) is our own echo; any other difference is an edit
    by the page's last editor: applied as a human edit when that editor is a configured
    user (``identity.notion_user_ids``), otherwise recorded as evidence only.
  * Pages created by the user become tasks/projects (``origin=notion``).
  * Edits with workflow meaning are not guessed: "next by = Agent" without an active
    delegation becomes a pending feedback item for the model; closing a task stops its
    active delegations deterministically (cancelled; closing never accepts a delivery).

Page bodies (gate B, after the property import)
  * A page last edited by someone other than our bot gets its body read until a read covers
    the edit's timestamp (``body.edit_unread``: a whole-minute stamp may stand for any moment
    of that minute, so a read in the same minute may predate a later edit with the same stamp).
    At most ``notion.body_reads_per_run`` reads per run, least recently read first. A change
    inside the managed box is ``body_edit`` feedback (the model decides what it means; the box is
    not overwritten meanwhile). The user's own notes outside it are kept as written
    (``notion_pages.notes_text``) and shown with the Task as context; they are not feedback.
    The first read of a page is a baseline only.
  * The body of a page created by the user is part of its ``new_task`` feedback.

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
  * Comments by our own bot are never feedback. An edit creates a new feedback version
    (the version includes the content, so edits within the same timestamp are not lost).
  * Authorship: missing identity configuration never defaults to "the user"; content by
    anyone else is evidence for the model, never an instruction.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .. import clock, delegation, effects, feedback, health, i18n, ids, outbox, project_icons, store
from ..config import Config
from ..db import dumps, get_meta, loads, set_meta, tx
from . import body, delivery_pages, schema
from .client import NotionClient, NotionError, NotionUncertain, plain_text

TRASH_REASON = "sync.trash_reason"    # catalog keys
BOARD_STATUS = {"cancel": "cancelled", "done": "done", "todo": "todo"}   # decision-board choice -> status
HOME_MARKER = "home.marker"


@dataclass
class NotionState:
    tasks_data_source_id: str
    projects_data_source_id: str
    home_page_id: str | None
    bot_user_id: str | None
    task_props: schema.PropertyMap
    project_props: schema.PropertyMap
    language: str = i18n.DEFAULT    # language of the Notion structure and managed text (fixed at setup)

    @property
    def ready(self) -> bool:
        return bool(self.tasks_data_source_id and self.projects_data_source_id)


def _meta_json(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    return loads(get_meta(conn, key), {}) or {}


def load_state(conn: sqlite3.Connection, cfg: Config) -> NotionState:
    n = cfg.section("notion")

    def pick(key: str) -> str:
        return n.get(key) or get_meta(conn, f"notion.{key}") or ""

    lang = i18n.normalize(get_meta(conn, "notion.language")) or i18n.language()
    return NotionState(
        tasks_data_source_id=pick("tasks_data_source_id"),
        projects_data_source_id=pick("projects_data_source_id"),
        home_page_id=pick("home_page_id") or None,
        bot_user_id=pick("bot_user_id") or None,
        task_props=schema.PropertyMap("task", _meta_json(conn, "notion.props.task"), lang),
        project_props=schema.PropertyMap("project", _meta_json(conn, "notion.props.project"), lang),
        language=lang,
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
        item["pending_write"] = loads(row["pending_write"], {}) or {}
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

    def _set_pending(self, page_id: str, props: dict[str, Any] | None, version: int | None = None) -> None:
        value = dumps({"props": props, "at": clock.iso(), "version": version}) if props else None
        self.conn.execute("UPDATE notion_pages SET pending_write=? WHERE page_id=?", (value, page_id))

    def user_ids(self) -> set[str]:
        return set(self.cfg.get("identity.notion_user_ids") or [])

    def is_user(self, who: str | None) -> bool:
        """Only a configured Notion user id is the user; an empty configuration means nobody is."""
        return bool(who) and who in self.user_ids()

    def pmap(self, kind: str) -> schema.PropertyMap:
        return self.state.task_props if kind == "task" else self.state.project_props

    def data_source(self, kind: str) -> str:
        return self.state.tasks_data_source_id if kind == "task" else self.state.projects_data_source_id

    def t(self, key: str, **values: Any) -> str:
        """Text written into Notion, in the language of the Notion structure."""
        return i18n.t(key, self.state.language, **values)

    def render_body(self, task: dict[str, Any]) -> str:
        return body.render(self.task_sections(task), lang=self.state.language)

    # ================================================================== local -> values

    def project_page_id(self, project_id: str | None) -> str | None:
        if not project_id:
            return None
        row = self.entity_row("project", project_id)
        return row["page_id"] if row else None

    def delegation_label(self, task_id: str) -> str | None:
        return delegation.task_label(self.conn, task_id)

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
        values["decision"] = task.get("decision") or ("pending" if task["status"] == "decision" else None)
        values["focus"] = bool(task.get("focus"))
        values["project_id"] = self.project_page_id(task.get("project_id"))
        pending = loads(get_meta(self.conn, 'notion.project_pending:' + task['id']), {})
        if pending:
            values['project_id'] = pending['page_id']
        values["delegation"] = self.delegation_label(task["id"])
        # Not written before the install created the property (``render_properties`` skips it).
        values["agent_progress"] = (delegation.task_progress(self.conn, task["id"])
                                    if "agent_progress" in self.pmap("task").ids else None)
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
        # One line per link (a mail thread's messages share one), with its latest message.
        latest: dict[Any, dict[str, Any]] = {}
        for ev in store.evidence_for(self.conn, "task", task["id"]):
            if ev["role"] == "context":
                continue
            when = ev.get("occurred_at")
            day = clock.local_dt(tz, clock.parse(when)).strftime("%Y-%m-%d") if when else ""
            title = ev.get("title") or (ev.get("excerpt") or "")[:80]
            key = ev.get("url") or (ev["source"], title)
            latest.pop(key, None)
            latest[key] = {"title": title, "url": ev.get("url"), "when": day,
                           "source": schema.label("source", ev["source"], self.state.language)}
        sources = list(latest.values())
        sections["sources"] = sources[-12:] if len(sources) > 12 else sources
        dels = delegation.for_task(self.conn, task["id"])
        status_names = i18n.prefixed("label.delegation", self.state.language)
        status_names.update(pending_submit=status_names["queued"], reconcile=status_names["queued"])
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
                                    "page_url": delivery_pages.page_url(self.conn, latest["id"]),
                                    "when": clock.local_dt(tz, clock.parse(latest["received_at"])).strftime("%Y-%m-%d %H:%M")}
        return sections

    # ================================================================== import (pull)

    def pull(self, kind: str, overlap_minutes: int = 3) -> dict[str, int]:
        """Import edits of ``kind`` pages since the cursor, then (tasks) read edited bodies. Any
        failure of the import is kept on the cursor (``notion``/``{kind}s``); body reads keep
        their own channel (``notion``/``bodies``)."""
        scope = f"{kind}s"
        try:
            stats = self._pull(kind, scope, overlap_minutes)
        except Exception as exc:
            _cursor_error(self.conn, scope, _describe(exc))
            raise
        self.resolve_projects()
        if kind == "task":
            stats["bodies"] = self.check_bodies(int(self.cfg.get("notion.body_reads_per_run", 10)))
        stats["problems"] = len(self.problems)
        return stats

    def _pull(self, kind: str, scope: str, overlap_minutes: int) -> dict[str, int]:
        stats = {"pages": 0, "edits": 0, "created": 0, "problems": 0}
        ds = self.data_source(kind)
        row = self.conn.execute("SELECT cursor FROM cursors WHERE source='notion' AND scope=?", (scope,)).fetchone()
        since = row["cursor"] if row and row["cursor"] else get_meta(self.conn, "notion.setup_at")
        flt = None
        if since:
            start = clock.iso(clock.parse(since) - timedelta(minutes=overlap_minutes))
            flt = {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": start}}
        pages = self.client.query_data_source(ds, filter=flt, sorts=[{"timestamp": "last_edited_time",
                                                                      "direction": "ascending"}])
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
        return stats

    def body_unread(self, row: dict[str, Any]) -> bool:
        """The page's last edit (not ours) may not be reflected in our last body read."""
        incomplete = self.conn.execute("SELECT 1 FROM feedback WHERE source_id=? AND kind IN ('new_task','body_edit') "
                                       "AND status='pending' AND (json_extract(payload,'$.complete')=0 "
                                       "OR json_extract(payload,'$.truncated')=1)", (row['page_id'],)).fetchone()
        return bool(incomplete) or body.edit_unread(row["last_edited_time"], row["last_edited_by"], row["body_read_at"],
                                self.state.bot_user_id)

    def check_bodies(self, limit: int) -> int:
        """Read the bodies of pages edited by someone other than our bot since the last read.
        A body that cannot be read stays unread (retried next run) and marks the ``bodies``
        channel failing until a run reads every due body."""
        failure: str | None = None
        done = 0
        try:
            rows = [self.page_row(r["page_id"]) for r in self.conn.execute(
                "SELECT page_id FROM notion_pages WHERE entity_type='task' AND trashed=0 AND last_edited_time IS NOT NULL"
                " AND (last_edited_by IS NOT ? OR EXISTS (SELECT 1 FROM feedback f WHERE f.source_id=notion_pages.page_id "
                "AND f.kind IN ('new_task','body_edit') AND f.status='pending' AND (json_extract(f.payload,'$.complete')=0 "
                "OR json_extract(f.payload,'$.truncated')=1)))", (self.state.bot_user_id,))]
            due = sorted((r for r in rows if self.body_unread(r)), key=lambda r: r["body_read_at"] or "")
            for row in due[:max(0, limit)]:
                task = store.get_task(self.conn, row["entity_id"])
                if task is None:
                    continue
                try:
                    self.check_body(row, task)
                except NotionError as exc:
                    failure = failure or f"body of {row['page_id']}: {exc}"
                    continue
                done += 1
        except Exception as exc:
            health.record_channel(self.conn, "notion", "bodies", _describe(exc))
            raise
        health.record_channel(self.conn, "notion", "bodies", failure)
        return done

    def check_body(self, row: dict[str, Any], task: dict[str, Any]) -> list[str]:
        doc = self.client.page_markdown(row["page_id"])
        markdown = doc.get("markdown") or ""
        region = body.extract(markdown)
        if region is not None and not doc.get('truncated'):
            self.reconcile_body(row['page_id'], region)
            row = self.page_row(row['page_id'])
        notes = body.outside(markdown).strip()
        editor = row["last_edited_by"]
        trust = {"author": editor, "author_is_user": self.is_user(editor)}
        found: list[str] = []
        with tx(self.conn):
            # Replace the identity, not only the payload: a claimed old interpretation
            # must fail at the transactional command boundary, including after restart.
            for pending in self.conn.execute("SELECT * FROM feedback WHERE kind='new_task' "
                                             "AND source_id=? AND status='pending'", (row['page_id'],)).fetchall():
                payload = loads(pending['payload'], {})
                updated = {**payload, 'body': notes if row['managed_hash'] else markdown.strip(),
                           'complete': not bool(doc.get('truncated')), 'truncated': bool(doc.get('truncated'))}
                if updated != payload:
                    fid, _ = feedback.record(
                        self.conn, origin='notion_page', source_id=row['page_id'],
                        source_version=f"created:after:{pending['id']}", text=pending['text'],
                        scope_type='task', scope_id=task['id'], kind='new_task', payload=updated,
                        author=editor if editor != self.state.bot_user_id else pending['author'],
                        author_is_user=trust['author_is_user'] if editor != self.state.bot_user_id
                        else bool(pending['author_is_user']))
                    feedback.supersede(self.conn, [pending['id']], 'creation body reread', by=fid)
                    found.append('new_task')
            if region is not None and row["managed_hash"]:
                found += self._box_read(row, task, region, self.render_body(task), bool(doc.get("truncated")), trust)
            # The user's own notes are context (shown with the Task to the agent and in the card),
            # kept exactly as written; a change is not a request by itself and wakes nothing.
            notes_fp = body.content_fingerprint(notes) if notes else ""
            revision = row["notes_revision"] + (1 if row["notes_hash"] is not None and notes_fp != row["notes_hash"]
                                                and not body.same_content(notes, row["notes_hash"]) else 0)
            self.conn.execute("UPDATE notion_pages SET body_read_at=?, notes_hash=?, notes_text=?, notes_revision=?"
                              " WHERE page_id=?", (None if doc.get("truncated") else clock.iso(), notes_fp, notes, revision, row["page_id"]))
        return found

    def _box_read(self, row: dict[str, Any], task: dict[str, Any], region: str, rendered: str, truncated: bool,
                  trust: dict[str, Any]) -> list[str]:
        """One read of the managed box. A changed box is a new revision: an edit replaces the
        pending interpretation of the previous one; a box back to what we wrote withdraws it."""
        fp = body.content_fingerprint(region)
        pending = self.conn.execute("SELECT payload FROM feedback WHERE source_id=? AND kind='body_edit' "
                                    "AND status='pending' ORDER BY rowid DESC LIMIT 1", (row['page_id'],)).fetchone()
        previous = loads(pending['payload'], {}) if pending else {}
        incomplete = previous.get('complete') is False or bool(previous.get('truncated'))
        completeness_changed = pending is not None and incomplete != truncated
        if body.same_content(region, row["box_seen_hash"] or row["managed_hash"]) and not completeness_changed:
            return []                                    # unchanged since the last read
        revision = row["box_revision"] + 1
        self.conn.execute("UPDATE notion_pages SET box_revision=?, box_seen_hash=? WHERE page_id=?",
                          (revision, fp, row["page_id"]))
        if body.same_content(region, row["managed_hash"]):
            older = [r["id"] for r in self.conn.execute(
                "SELECT id FROM feedback WHERE origin='notion_page' AND source_id=? AND kind='body_edit'"
                " AND status='pending'", (row["page_id"],))]
            feedback.supersede(self.conn, older, "withdrawn: the box is back to what we wrote")
            return []
        fid, _ = feedback.record(self.conn, origin="notion_page", source_id=row["page_id"],
                                 source_version=f"body:r{revision}:{fp}", text=region, scope_type="task",
                                 scope_id=task["id"], kind="body_edit", **trust,
                                 payload={"page_id": row["page_id"], "region": region, "would_render": rendered,
                                          "revision": revision, "truncated": truncated, "complete": not truncated})
        feedback.supersede_older(self.conn, "notion_page", row["page_id"], fid, kind="body_edit",
                                 reason="replaced by a later edit of the box")
        return ["body_edit"]

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
                self.page_trashed(row, page)
            return "edited"
        if row["trashed"]:
            self._page_restored(kind, row, page)
        seen = row["last_seen_props"]
        current = {**schema.comparable(kind, values), **_page_icon(kind, page)}
        if kind == "project" and "icon" not in seen:
            seen = self._adopt_icon(row, current["icon"], seen)
        editable = _editable(kind)
        changed = {f: current.get(f) for f in editable if current.get(f) != seen.get(f)}
        # Unmappable values (e.g. an unknown status option) are reported, not imported.
        for f in list(changed):
            if current.get(f) is None and any(p.field == f for p in schema.PROPS[kind] if p.type == "select") \
                    and _select_raw_present(page, self.pmap(kind), f):
                changed.pop(f)
        # Our own write whose response was lost shows up with exactly the value we sent.
        pending = row["pending_write"].get("props") or {}
        echo = {f for f in changed if f in pending and pending[f] == changed[f]}
        for f in echo:
            changed.pop(f)
        seen = {**seen, **{f: current.get(f) for f in echo}}
        still_pending = {f: v for f, v in pending.items() if f not in echo and current.get(f) == seen.get(f)}
        if not changed:
            with tx(self.conn):
                self._seen(page["id"], {**seen, **{k: v for k, v in current.items() if k not in editable}}, page)
                self._set_pending(page["id"], still_pending, row["pending_write"].get("version"))
            return "unchanged"
        editor = (page.get("last_edited_by") or {}).get("id")
        if not self.is_user(editor):
            self._unverified_edit(kind, row, page, changed, seen, current, editor)
        elif kind == "task":
            self._apply_task_edit(row, page, changed, seen, current)
        else:
            self._apply_project_edit(row, page, changed, seen, current)
        with tx(self.conn):
            self._set_pending(page["id"], still_pending, row["pending_write"].get("version"))
        return "edited" if self.is_user(editor) else "evidence"

    def _adopt_icon(self, row: dict[str, Any], icon: str | None, seen: dict[str, Any]) -> dict[str, Any]:
        """First read since project icons are synced: an icon already on the page is the user's."""
        with tx(self.conn):
            if icon and not store.get_project(self.conn, row["entity_id"]).get("icon"):
                store.update_project(self.conn, row["entity_id"], {"icon": icon}, actor="human", origin="notion")
            seen = {**seen, "icon": icon}
            self._seen(row["page_id"], seen)
        return seen

    def _unverified_edit(self, kind: str, row: dict[str, Any], page: dict[str, Any], changed: dict[str, Any],
                         seen: dict[str, Any], current: dict[str, Any], editor: str | None) -> None:
        """Properties changed by someone who is not a verified user (or not attributable): kept
        as evidence for the model; the local values stay authoritative and are written back."""
        payload: dict[str, Any] = {"changes": changed, "before": {k: seen.get(k) for k in changed}, "applied": False}
        if not self.user_ids():
            payload["identity_unverified"] = True
        with tx(self.conn):
            feedback.record(self.conn, origin="notion_property", source_id=page["id"],
                            source_version=f"{page.get('last_edited_time')}:{_h(dumps(changed))}", text=None,
                            scope_type=kind, scope_id=row["entity_id"], kind="property_edit", author=editor,
                            author_is_user=False, payload=payload)
            self._seen(page["id"], current, page)
            (effects.task_push if kind == "task" else effects.project_push)(self.conn, row["entity_id"])

    def _import_new(self, kind: str, page: dict[str, Any], values: dict[str, Any]) -> str:
        if page.get("in_trash"):
            return "ignored"
        tm_id = values.get("tm_id")
        current = schema.comparable(kind, values)
        creator = (page.get("created_by") or {}).get("id") or (page.get("last_edited_by") or {}).get("id")
        by_user = self.is_user(creator)
        actor = "human" if by_user else "program"   # someone else's page is a task for the user to judge
        if kind == "task":
            if tm_id and store.get_task(self.conn, tm_id):
                existing = self.entity_row("task", tm_id)
                if existing and existing["page_id"] != page["id"]:
                    self.problems.append(f"duplicate Notion page {page['id']} for task {tm_id} "
                                         f"(linked page {existing['page_id']})")
                    return "ignored"
                self._link("task", tm_id, page, current)   # recovery: page exists, link was lost
                return "unchanged"
            title = values.get("title") or self.t("sync.untitled_task")
            project_id = self._local_project(values.get("project_id"))
            truncated = False
            try:
                doc = self.client.page_markdown(page["id"])
                page_text, truncated = (doc.get("markdown") or "").strip(), bool(doc.get("truncated"))
            except NotionError as exc:
                self.problems.append(f"body of new page {page['id']}: {exc}")
                page_text = None
            fields = {k: values.get(k) for k in ("next_step", "waiting_on", "due_date", "follow_up_date",
                                                 "snooze_until", "decision") if values.get(k)}
            with tx(self.conn):
                tid = store.create_task(self.conn, title, origin="notion", actor=actor, cause_ref=page["id"],
                                        project_id=project_id, status=values.get("status") or "todo",
                                        next_actor=values.get("next_actor") or "me",
                                        focus=bool(values.get("focus")), notion_page_id=None, **fields)
                self._link("task", tid, page, {**current, "tm_id": None})
                if values.get('project_id') and project_id is None:
                    store.defer_notion_project(self.conn, tid, values['project_id'], actor=actor, cause_ref=page['id'])
                if page_text is not None:   # the body is in this feedback; later edits are diffed against it
                    self.conn.execute("UPDATE notion_pages SET body_read_at=?, notes_hash=?, notes_text=?"
                                      " WHERE page_id=?",
                                      (clock.iso(), body.content_fingerprint(page_text) if page_text else "",
                                       page_text, page["id"]))
                feedback.record(self.conn, origin="notion_page", source_id=page["id"], source_version="created",
                                text=title, scope_type="task", scope_id=tid, kind="new_task", author=creator,
                                author_is_user=by_user, payload={"page_id": page["id"], "values": current,
                                                                 "body": page_text or "", "truncated": truncated,
                                                                 "complete": page_text is not None and not truncated})
                effects.task_push(self.conn, tid)
            return "created"
        if tm_id and store.get_project(self.conn, tm_id):
            existing = self.entity_row('project', tm_id)
            if existing and existing['page_id'] != page['id']:
                self.problems.append(f"duplicate Notion page {page['id']} for project {tm_id} (linked page {existing['page_id']})")
                return 'ignored'
            self._link("project", tm_id, page, current)
            return "unchanged"
        title = values.get("title") or self.t("sync.untitled_project")
        with tx(self.conn):
            pid = store.create_project(self.conn, title, origin="notion", actor=actor, cause_ref=page["id"],
                                       domain=values.get("domain"), goal=values.get("goal"),
                                       next_step=values.get("next_step"), status=values.get("status") or "active",
                                       repos=_split_repos(values.get("repos")))
            self._link("project", pid, page, {**current, "tm_id": None})
            feedback.record(self.conn, origin="notion_page", source_id=page["id"], source_version="created",
                            text=title, scope_type="project", scope_id=pid, kind="new_project", status="applied",
                            author=creator, author_is_user=by_user, payload={"page_id": page["id"]})
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
        choice = local.get('decision') if 'decision' in local else None
        if choice == 'agent':
            existing = store.get_task(self.conn, task_id)
            desired_status = local.get('status', existing['status'])
            if existing['status'] not in store.CLOSED_STATUSES and desired_status not in store.CLOSED_STATUSES:
                local.update(status=desired_status if desired_status in ('todo', 'in_progress') else 'todo', next_actor='agent')
            local['decision'] = None
        board_closed = None
        # The Agent progress board: Done / My to-do / Cancel act like the decision board's
        # choices and also settle the agent work (``delegation.board_choice``); the program's
        # own columns are not choices. The page gets the program's column back either way.
        progress = local.pop('agent_progress', None) if 'agent_progress' in local else None
        progress = progress if progress in BOARD_STATUS else None
        if progress and 'status' not in local:
            local['status'] = BOARD_STATUS[progress]
            if progress == 'todo':
                local['next_actor'] = 'me'
            elif local['status'] in store.CLOSED_STATUSES:
                board_closed = self.t('sync.agent_board_closed',
                                      choice=schema.label('agent_progress', progress, self.state.language))
        if choice in BOARD_STATUS:
            # The board's own choices take effect here, without a model run; a status set in the
            # same edit wins. The choice is cleared, so the handled card leaves the board.
            if 'status' not in local:
                local['status'] = BOARD_STATUS[choice]
                if choice == 'todo':
                    local['next_actor'] = 'me'
                elif local['status'] in store.CLOSED_STATUSES:
                    board_closed = self.t('sync.board_closed', choice=schema.label('decision', choice, self.state.language))
            local['decision'] = None
        relation_changed = 'project_id' in local
        pending_relation = None
        if relation_changed:
            remote = local['project_id']
            resolved = self._local_project(remote)
            if remote and resolved is None:
                pending_relation = remote
                del local['project_id']
            else:
                local['project_id'] = resolved
        author = (page.get("last_edited_by") or {}).get("id")
        with tx(self.conn):
            before = store.get_task(self.conn, task_id)
            fid, created = feedback.record(
                self.conn, origin="notion_property", source_id=page["id"],
                source_version=f"{page.get('last_edited_time')}:{_h(dumps(changed))}:v{before['version']}", text=None,
                scope_type="task", scope_id=task_id, kind="property_edit", author=author, author_is_user=True,
                payload={"changes": changed, "before": {k: seen.get(k) for k in changed}}, status="applied")
            if pending_relation:
                store.defer_notion_project(self.conn, task_id, pending_relation, actor='human', cause_ref=fid)
            board_notes = delegation.board_choice(self.conn, task_id, progress, fid) if progress else []
            result = store.update_task(self.conn, task_id, local, actor="human", origin="notion", cause_ref=fid)
            if board_closed and store.get_task(self.conn, task_id)['status'] in store.CLOSED_STATUSES:
                store.set_closed_reason(self.conn, task_id, board_closed)
            after = store.get_task(self.conn, task_id)
            if 'decision' in changed:
                stale = [r['id'] for r in self.conn.execute(
                    "SELECT id,payload FROM feedback WHERE scope_type='task' AND scope_id=? AND status='pending'",
                    (task_id,)) if loads(r['payload'], {}).get('decision_event')]
                feedback.supersede(self.conn, stale, 'replaced by a newer decision', by=fid)
                # Preserve creation-body completeness while replacing the entire
                # interpretation identity when its original choice is withdrawn.
                creations = self.conn.execute(
                    "SELECT * FROM feedback WHERE scope_type='task' AND scope_id=? "
                    "AND kind='new_task' AND status='pending'", (task_id,)).fetchall()
                for creation in creations:
                    payload = loads(creation['payload'], {})
                    payload['values'] = {**payload.get('values', {}), **local,
                                         **({'decision': choice} if choice else {})}
                    replacement, _ = feedback.record(
                        self.conn, origin='notion_page', source_id=creation['source_id'],
                        source_version='creation-choice:' + fid, text=creation['text'],
                        scope_type='task', scope_id=task_id, kind='new_task', payload=payload,
                        author=author, author_is_user=True)
                    feedback.supersede(self.conn, [creation['id']], 'creation choice changed', by=replacement)
                if not creations and after['status'] not in store.CLOSED_STATUSES and choice in ('agree', 'decline', 'agent'):
                    feedback.record(self.conn, origin='notion_property', source_id=page['id'],
                                    source_version='decision:' + fid, scope_type='task', scope_id=task_id,
                                    kind='delegate_request' if choice == 'agent' else 'decision_choice',
                                    author=author, author_is_user=True,
                                    text=self.t('sync.decision_choice', choice=schema.label('decision', choice, self.state.language)),
                                    payload={'decision_event': fid, 'decision': choice, 'task_version': after['version']})
            # A board drag already queues the same durable delegation request.
            semantic_changes = dict(changed)
            if choice == 'agent':
                semantic_changes.pop('next_actor', None)
            if progress:
                semantic_changes.update({k: local[k] for k in ('status', 'next_actor') if k in local})
                semantic_changes.pop('next_actor', None)   # settled by board_choice, not a delegate request
            followups = board_notes + self._edit_semantics(before, after, semantic_changes, fid, author)
            feedback.resolve(self.conn, fid, "applied", {"applied": result.applied, "rejected": result.rejected,
                                                         "followups": followups})
            self._seen(page["id"], current, page)
            if after["status"] != current.get("status") or after["next_actor"] != current.get("next_actor") \
                    or 'agent_progress' in changed:
                effects.task_push(self.conn, task_id)   # normalised pair differs from what the user typed

    def _edit_semantics(self, before: dict[str, Any], after: dict[str, Any], changed: dict[str, Any],
                        fid: str, author: str | None) -> list[str]:
        """Deterministic consequences of a verified user's property edit; ambiguous ones go to
        the model as derived feedback that keeps the original author."""
        notes: list[str] = []
        active = delegation.for_task(self.conn, after["id"], active_only=True)
        if "status" in changed and after["status"] in store.CLOSED_STATUSES:
            status = schema.label("task_status", after["status"], self.state.language)
            return delegation.on_task_closed(self.conn, after["id"], after["status"],
                                             self.t("sync.closed_in_notion", status=status), fid)
        field = schema.prop("task", "next_actor").name(self.state.language)
        if "next_actor" in changed and after["next_actor"] == "agent" and before["next_actor"] != "agent" and not active:
            feedback.record(self.conn, origin="notion_property", source_id=after["id"],
                            source_version=f"delegate:{fid}", text=self.t("sync.set_to_agent", field=field),
                            scope_type="task", scope_id=after["id"], kind="delegate_request", author=author,
                            author_is_user=True, payload={"from_feedback": fid})
            notes.append("delegate_request")
        elif "next_actor" in changed and before["next_actor"] == "agent" and after["next_actor"] != "agent" and active:
            feedback.record(self.conn, origin="notion_property", source_id=after["id"],
                            source_version=f"unassign:{fid}", text=self.t("sync.unassigned", field=field),
                            scope_type="task", scope_id=after["id"], kind="delegate_unassigned", author=author,
                            author_is_user=True, payload={"from_feedback": fid, "delegations": [d["id"] for d in active]})
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
                author=(page.get("last_edited_by") or {}).get("id"), author_is_user=True,
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

    def page_trashed(self, row: dict[str, Any], page: dict[str, Any] | None = None) -> None:
        task = store.get_task(self.conn, row["entity_id"])
        reason = self.t(TRASH_REASON)
        editor = ((page or {}).get("last_edited_by") or {}).get("id")
        with tx(self.conn):
            self.conn.execute("UPDATE notion_pages SET trashed=1 WHERE page_id=?", (row["page_id"],))
            if task and task["status"] not in store.CLOSED_STATUSES and not self.is_user(editor):
                # Not the verified user: the task stays open; the model asks the user (on the home page).
                feedback.record(self.conn, origin="notion_page", source_id=row["page_id"],
                                source_version=f"trashed:{(page or {}).get('last_edited_time') or clock.iso()}",
                                text=reason, scope_type="task", scope_id=task["id"], kind="page_trashed",
                                author=editor, author_is_user=False, payload={"page_id": row["page_id"]})
                return
            if task and task["status"] not in store.CLOSED_STATUSES:
                fid, _ = feedback.record(self.conn, origin="notion_page", source_id=row["page_id"],
                                         source_version=f"trashed:{clock.iso()}", text=reason, scope_type="task",
                                         scope_id=task["id"], kind="page_trashed", status="applied", author=editor,
                                         author_is_user=True)
                store.update_task(self.conn, task["id"], {"status": "cancelled"}, actor="human", origin="notion",
                                  cause_ref=fid)
                store.set_closed_reason(self.conn, task["id"], reason)
                delegation.on_task_closed(self.conn, task["id"], "cancelled", reason, fid)

    def _page_restored(self, kind: str, row: dict[str, Any], page: dict[str, Any]) -> None:
        with tx(self.conn):
            self.conn.execute("UPDATE notion_pages SET trashed=0 WHERE page_id=?", (row["page_id"],))
            if kind != "task":
                return
            task = store.get_task(self.conn, row["entity_id"])
            editor = (page.get("last_edited_by") or {}).get("id")
            if task and task["status"] == "cancelled" and task.get("closed_reason") in i18n.variants(TRASH_REASON) \
                    and self.is_user(editor):
                previous = row["last_seen_props"].get("status") or "todo"
                fid, _ = feedback.record(self.conn, origin="notion_page", source_id=row["page_id"],
                                         source_version=f"restored:{page.get('last_edited_time')}",
                                         text=self.t("sync.restored"), scope_type="task", scope_id=task["id"],
                                         kind="page_restored", status="applied", author=editor, author_is_user=True)
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
            self._link(kind, entity["id"], page, {**schema.comparable(kind, values), **_page_icon(kind, page)})
            return self.entity_row(kind, entity["id"])
        if reconcile and self.conn.execute(
                "SELECT 1 FROM outbox WHERE op=? AND entity_id=? AND status='uncertain'"
                " AND updated_at > ?", (f"push_{kind}", entity["id"],
                                        clock.iso(clock.now() - timedelta(seconds=50)))).fetchone():
            raise outbox.Retryable("waiting for an uncertain create to become visible", retry_after=60)
        values = self.task_values(entity) if kind == "task" else self.project_values(entity)
        props = schema.render_properties(kind, values, self.pmap(kind))
        markdown = self.render_body(entity) if kind == "task" else None
        parent = {"type": "data_source_id", "data_source_id": self.data_source(kind)}
        icon = project_icons.render(entity.get("icon")) if kind == "project" else None
        try:
            page = self.client.create_page(parent, props, markdown=markdown, icon=icon)
        except NotionUncertain as exc:
            if exc.committed_id:
                page = self.client.retrieve_page(exc.committed_id)
            else:
                raise outbox.Uncertain(str(exc)) from None
        self._link(kind, entity["id"], page, {**schema.comparable(kind, values), **_page_icon(kind, page)})
        if markdown:
            # The visible-text fingerprint of what we sent identifies our region until the next read.
            self._store_region(page["id"], markdown, markdown)
            with tx(self.conn):
                self.conn.execute("UPDATE notion_pages SET notes_hash='', notes_text='', body_read_at=? WHERE page_id=?",
                                  (clock.iso(), page["id"]))
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
                self.page_trashed(row, page)
            return {"skipped": "page trashed"}
        self.import_page(kind, page)          # unseen human edits first
        row = self.entity_row(kind, entity_id)
        if kind == "task" and self.body_unread(row):
            # Our write below would make the bot the page's last editor and hide this edit.
            self.check_body(row, store.get_task(self.conn, entity_id))
            row = self.entity_row(kind, entity_id)
        entity = store.get_task(self.conn, entity_id) if kind == "task" else store.get_project(self.conn, entity_id)
        values = self.task_values(entity) if kind == "task" else self.project_values(entity)
        target = schema.comparable(kind, values)
        on_page = {**schema.comparable(kind, schema.parse_page(kind, page, self.pmap(kind))[0]), **_page_icon(kind, page)}
        if kind == "project":
            target["icon"] = project_icons.normalize(entity.get("icon"))
            if not project_icons.writable(target["icon"]):
                target["icon"] = on_page["icon"]     # an uploaded image: what the page has
        diff = {f for f in target if target[f] != on_page.get(f)}
        result: dict[str, Any] = {"props": sorted(diff)}
        if diff:
            props = schema.render_properties(kind, values, self.pmap(kind), only=diff - {"icon"})
            extra = {"icon": project_icons.render(target["icon"])} if "icon" in diff else {}
            with tx(self.conn):
                # Expected values, kept until the write is confirmed (response or a later read).
                self._set_pending(row["page_id"], {f: target[f] for f in diff}, entity["version"])
            try:
                page = self.client.update_page(row["page_id"], props or None, **extra)
            except NotionUncertain as exc:
                raise outbox.Uncertain(str(exc)) from None   # a retry re-reads and re-diffs: idempotent
            except NotionError:
                with tx(self.conn):
                    self._set_pending(row["page_id"], None)  # definitely not written
                raise
        with tx(self.conn):
            self._seen(row["page_id"], target, page)
            self._set_pending(row["page_id"], None)
        if kind == "task":
            result["body"] = self._sync_body(row["page_id"], entity)
        return result

    def _sync_body(self, page_id: str, task: dict[str, Any]) -> str:
        row = self.page_row(page_id)
        rendered = self.render_body(task)
        if row["rendered_hash"] == _h(rendered) and row["managed_hash"]:
            return "unchanged"
        if self.conn.execute("SELECT 1 FROM feedback WHERE kind='body_edit' AND status='pending' AND scope_id=?",
                             (task["id"],)).fetchone():
            return "held: user edit inside the managed region awaits interpretation"
        doc = self.client.page_markdown(page_id)
        region = body.extract(doc.get("markdown") or "")
        if doc.get('truncated'):
            if region is not None and row['managed_hash']:
                editor = (self.client.retrieve_page(page_id).get('last_edited_by') or {}).get('id')
                with tx(self.conn):
                    self._box_read(row, task, region, rendered, True,
                                   {'author': editor, 'author_is_user': self.is_user(editor)})
            return 'held: API body is truncated'
        if region is not None:
            self.reconcile_body(page_id, region)
            row = self.page_row(page_id)
            fp = body.content_fingerprint(region)
            if fp == body.content_fingerprint(rendered):
                self._store_region(page_id, rendered, region)
                return "already current"
            if row["managed_hash"] and not body.same_content(region, row["managed_hash"]):
                editor = (self.client.retrieve_page(page_id).get('last_edited_by') or {}).get('id')
                with tx(self.conn):
                    self._box_read(row, task, region, rendered, bool(doc.get("truncated")),
                                   {"author": editor, "author_is_user": self.is_user(editor)})
                return "held: user edited the managed region"
            command = {"type": "update_content",
                       "update_content": {"content_updates": [{"old_str": region, "new_str": rendered}]}}
        else:
            command = {"type": "insert_content", "insert_content": {"content": rendered, "position": {"type": "start"}}}
        with tx(self.conn):
            set_meta(self.conn, 'notion.body_pending:' + page_id, rendered)
        try:
            result = self.client.update_markdown(page_id, command)
        except NotionUncertain as exc:
            raise outbox.Uncertain(str(exc)) from None
        new_region = body.extract(result.get("markdown") or "") or rendered
        self._store_region(page_id, rendered, new_region)
        return "updated" if region is not None else "inserted"

    def reconcile_body(self, page_id: str, region: str) -> None:
        expected = get_meta(self.conn, 'notion.body_pending:' + page_id)
        if expected and body.content_fingerprint(expected) == body.content_fingerprint(region):
            self._store_region(page_id, expected, region)

    def _store_region(self, page_id: str, rendered: str, region: str) -> None:
        fp = body.content_fingerprint(region)
        with tx(self.conn):
            self.conn.execute('DELETE FROM meta WHERE key=?', ('notion.body_pending:' + page_id,))
            self.conn.execute("UPDATE notion_pages SET rendered_hash=?, managed_hash=?, box_seen_hash=? WHERE page_id=?",
                              (_h(rendered), fp, fp, page_id))

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
        marker = 'TM effect ' + _h(p.get('key') or item['id'])[:24]
        text = p['text'] + '\n\n' + marker
        if reconcile:
            if not p.get('comment_protocol'):
                raise outbox.Uncertain('legacy comment has no readable effect identity; manual reconciliation required')
            for c in self.client.list_comments(page_id):
                if (c.get("created_by") or {}).get("id") == self.state.bot_user_id and \
                        marker in plain_text(c.get("rich_text")) and (
                            not p.get('discussion_id') or p.get('fallback_page') or
                            c.get('discussion_id') == p['discussion_id']):
                    self._record_bot_comment(c, page_id, p.get("key"))
                    return {"comment_id": c["id"], "reconciled": True}
            raise outbox.Uncertain('comment identity is not visible; delivery remains unknown, no automatic replay')
        # Older queued-but-unsent effects acquire the protocol before the first request.
        if not p.get('comment_protocol'):
            p['comment_protocol'] = 1
            with tx(self.conn):
                self.conn.execute('UPDATE outbox SET payload=? WHERE id=?', (dumps(p), item['id']))
        try:
            c = self.client.create_comment(markdown=body.escape_inline(text), discussion_id=None if p.get("fallback_page") else p.get("discussion_id"),
                                           page_id=page_id if p.get("fallback_page") or not p.get("discussion_id") else None)
        except NotionUncertain as exc:
            raise outbox.Uncertain(str(exc)) from None
        except NotionError as exc:
            if exc.not_found and p.get("discussion_id"):
                # Persist the effective target before the fallback can lose its response.
                p['fallback_page'] = True
                with tx(self.conn):
                    self.conn.execute('UPDATE outbox SET payload=? WHERE id=?', (dumps(p), item['id']))
                try:
                    c = self.client.create_comment(markdown=body.escape_inline(text), page_id=page_id)
                except NotionUncertain as uncertain:
                    raise outbox.Uncertain(str(uncertain)) from None
            else:
                raise
        self._record_bot_comment(c, page_id, p.get("key"))
        return {"comment_id": c["id"]}

    def _record_bot_comment(self, c: dict[str, Any], page_id: str, key: str | None) -> None:
        with tx(self.conn):
            self.conn.execute(
                "INSERT OR IGNORE INTO notion_comments(comment_id, page_id, discussion_id, author_id, is_bot, ref,"
                " last_edited_time, content_hash, seen_at) VALUES (?,?,?,?,1,?,?,?,?) "
                "ON CONFLICT(comment_id) DO UPDATE SET ref=COALESCE(excluded.ref,notion_comments.ref)",
                (c["id"], page_id, c.get("discussion_id"), (c.get("created_by") or {}).get("id"), key,
                 c.get("last_edited_time"), _h(plain_text(c.get("rich_text"))), clock.iso()))

    # ================================================================== comments (poll)

    def comment_targets(self, cold_limit: int = 30) -> list[dict[str, Any]]:
        now = clock.now()
        recent_since = clock.iso(now - timedelta(hours=48))
        edit_hot_since = clock.iso(now - timedelta(hours=float(self.cfg.get("notion.user_edit_hot_hours", 2))))
        talk_since = clock.iso(now - timedelta(days=7))
        warm_before = clock.iso(now - timedelta(minutes=int(self.cfg.get("notion.warm_comment_poll_minutes", 30))))
        cold_before = clock.iso(now - timedelta(minutes=int(self.cfg.get("notion.cold_comment_poll_minutes", 60))))
        closed_before = clock.iso(now - timedelta(hours=int(self.cfg.get("notion.history_poll_hours", 24))))
        closed_since = clock.iso(now - timedelta(days=30))
        active = ",".join(repr(s) for s in delegation.ACTIVE)
        open_ = ",".join(repr(s) for s in store.OPEN_STATUSES)
        targets: dict[str, dict[str, Any]] = {}

        def add(rows: list[sqlite3.Row], reason: str) -> None:
            for r in rows:
                targets.setdefault(r["page_id"], {**dict(r), "reason": reason})

        def user_edited(since: str) -> str:
            # The user edited the page since the named parameter, also when our write-back came
            # after it: their edit is feedback on the task, which outlives our write as last editor.
            return (f"((p.last_edited_time>=:{since} AND p.last_edited_by IS NOT :bot) OR t.id IN (SELECT scope_id"
                    f" FROM feedback WHERE scope_type='task' AND author_is_user=1 AND received_at>=:{since}))")

        tasks = "SELECT p.page_id, p.entity_type, p.entity_id FROM notion_pages p JOIN tasks t ON t.id=p.entity_id" \
                " WHERE p.entity_type='task' AND p.trashed=0"
        # Read every run where a conversation is going on: agent work (its questions included), a
        # recent user comment, a page the user has just edited. Our own writes are not activity: a
        # bulk push would otherwise make every page hot for two days (one request per page per run).
        add(self.conn.execute(
            f"{tasks} AND (t.id IN (SELECT task_id FROM delegations WHERE status IN ({active}))"
            f" OR p.last_comment_at>=:talk OR {user_edited('edit_hot')})",
            {"talk": talk_since, "edit_hot": edit_hot_since, "bot": self.state.bot_user_id}).fetchall(), "hot")
        # Merely recent (a new open task, a change the user was told about, an older user edit):
        # comments there are rarer, so these pages wait for the warm interval.
        add(self.conn.execute(
            f"{tasks} AND (p.comments_checked_at IS NULL OR p.comments_checked_at<:warm) AND ({user_edited('recent')}"
            f" OR (t.status IN ({open_}) AND (t.created_at>=:recent OR t.change_note_at>=:recent)))"
            " ORDER BY p.comments_checked_at LIMIT :limit",
            {"warm": warm_before, "recent": recent_since, "bot": self.state.bot_user_id, "limit": cold_limit}
        ).fetchall(), "warm")
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

    def comment_target(self, page_id: str) -> dict[str, Any] | None:
        """The comment-poll target of one page we know (a task, a project or the home page)."""
        if page_id == self.state.home_page_id:
            return {"page_id": page_id, "entity_type": "home", "entity_id": None, "reason": "event"}
        row = self.conn.execute("SELECT page_id, entity_type, entity_id FROM notion_pages WHERE page_id=?"
                                " AND entity_type IN ('task','project') AND trashed=0", (page_id,)).fetchone()
        return {**dict(row), "reason": "event"} if row else None

    def poll_comments(self, cold_limit: int = 30, targets: list[dict[str, Any]] | None = None) -> dict[str, int]:
        """Read the comments of the pages due, or of ``targets`` (``comment_target``: pages a
        webhook named). A page that is gone is a problem of that page (``problems``); any other
        failure marks the ``comments`` channel failing until a run reads every due page (a run
        over ``targets`` only reports failures: it did not read every due page)."""
        stats = {"pages": 0, "new": 0, "edited": 0, "errors": 0, "missing": 0, "deleted": 0, "held": 0}
        user_ids = set(self.cfg.get("identity.notion_user_ids") or [])
        failure: str | None = None
        try:
            for target in (self.comment_targets(cold_limit) if targets is None else targets):
                page_id = target["page_id"]
                try:
                    comments = self.client.list_comments(page_id)
                except NotionError as exc:
                    stats["errors"] += 1
                    error = self._check_missing(page_id) if exc.not_found and target["entity_type"] == "task" \
                        else f"comments of {page_id}: {exc}"
                    if error is None:
                        stats["missing"] += 1
                    failure = failure or error
                    continue
                stats["pages"] += 1
                for c in comments:
                    outcome = self._comment(c, target, user_ids)
                    if outcome in ("new", "edited"):
                        stats[outcome] += 1
                # The list is complete here (paginate raises rather than return part of it).
                error = self._check_unlisted(target, {c["id"] for c in comments}, stats)
                failure = failure or error
                if target["entity_type"] != "home":
                    with tx(self.conn):
                        self.conn.execute("UPDATE notion_pages SET comments_checked_at=? WHERE page_id=?",
                                          (clock.iso(), page_id))
        except Exception as exc:
            health.record_channel(self.conn, "notion", "comments", _describe(exc))
            raise
        if targets is None or failure is not None:
            health.record_channel(self.conn, "notion", "comments", failure)
        return stats

    def _check_missing(self, page_id: str) -> str | None:
        """A task page whose comments are not found: trashed (imported as such), or gone
        (a problem of that page). Returns the error when that could not be told."""
        try:
            page = self.client.retrieve_page(page_id)
        except NotionError as exc:
            if exc.not_found:
                self.problems.append(f"page {page_id} is no longer accessible (deleted or unshared)")
                return None
            return f"page {page_id}: {exc}"
        if page.get("in_trash"):
            row = self.page_row(page_id)
            if row and not row["trashed"]:
                self.page_trashed(row, page)
        return None

    def _check_unlisted(self, target: dict[str, Any], listed: set[str], stats: dict[str, int]) -> str | None:
        """Comments whose feedback is still pending but that a complete list of their page no
        longer has. The list only returns unresolved comments; retrieved by id, a 404 is taken
        as deleted (the page itself was just listed, so it is not a loss of access): its pending
        feedback is withdrawn. Found but unlisted is taken as a resolved discussion, and anything
        else cannot be told: the feedback is held (not claimed, not applied, shown by health)
        and checked again next run. A comment listed again releases the hold. Feedback already
        handled is history either way. (The real API's answers for deleted/resolved comments
        are not documented; see docs/limitations.md.) Returns the error of a failed check."""
        page_id = target["page_id"]
        failure: str | None = None
        rows = self.conn.execute(
            "SELECT c.comment_id, c.presence FROM notion_comments c WHERE c.page_id=? AND c.is_bot=0 AND"
            " (c.presence != 'listed' OR EXISTS (SELECT 1 FROM feedback f WHERE f.origin='notion_comment'"
            " AND f.source_id=c.comment_id AND f.status='pending'))", (page_id,)).fetchall()
        for r in rows:
            cid = r["comment_id"]
            if cid in listed:
                if r["presence"] != "listed":
                    self._comment_presence(cid, "listed", None)
                continue
            if r["presence"] == "deleted":
                continue
            if not self.conn.execute("SELECT 1 FROM feedback WHERE origin='notion_comment' AND source_id=?"
                                     " AND status='pending'", (cid,)).fetchone():
                continue
            try:
                self.client.retrieve_comment(cid)
            except NotionError as exc:
                if exc.status == 404 or exc.not_found:
                    self._comment_presence(cid, "deleted", None)
                    stats["deleted"] += 1
                    continue
                self._comment_presence(cid, "missing", f"not listed any more and could not be checked: {exc}")
                stats["held"] += 1
                failure = failure or f"comment {cid}: {exc}"
                continue
            if r["presence"] != "resolved":
                self._comment_presence(cid, "resolved", "its discussion was resolved before it was handled;"
                                                        " reopen the discussion to have it handled", target)
            stats["held"] += 1
        return failure

    def _comment_presence(self, cid: str, presence: str, hold: str | None,
                          target: dict[str, Any] | None = None) -> None:
        with tx(self.conn):
            self.conn.execute("UPDATE notion_comments SET presence=?, presence_at=? WHERE comment_id=?",
                              (presence, clock.iso(), cid))
            pending = [r["id"] for r in self.conn.execute(
                "SELECT id FROM feedback WHERE origin='notion_comment' AND source_id=? AND status='pending'", (cid,))]
            if presence == "deleted":
                feedback.supersede(self.conn, pending, "withdrawn: the comment was deleted before it was handled")
                return
            for fid in pending:
                self.conn.execute("UPDATE feedback SET hold_reason=? WHERE id=?", (hold, fid))
            if presence == "resolved" and pending:
                fb = feedback.get(self.conn, pending[-1])
                task = store.get_task(self.conn, fb["scope_id"]) if fb["scope_type"] == "task" else None
                text = i18n.t("sync.comment_held_resolved", title=task["title"] if task else "-",
                              text=(fb["text"] or "")[:200])
                key = f"comment-held:{cid}:{fb['id']}"
                if task:         # a new discussion on the page, not in the resolved one
                    effects.task_comment(self.conn, task["id"], key, text)
                elif (fb["payload"] or {}).get("page_id"):
                    effects.page_comment(self.conn, fb["payload"]["page_id"], key, text)

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
        bot = self.conn.execute("SELECT ref FROM notion_comments WHERE discussion_id=? AND page_id=? AND is_bot=1 AND ref IS NOT NULL"
                                " ORDER BY (ref LIKE 'dlv:%') DESC, seen_at ASC LIMIT 1", (c.get("discussion_id"), target["page_id"])).fetchone()
        payload: dict[str, Any] = {"page_id": target["page_id"], "comment_id": c["id"],
                                   "discussion_id": c.get("discussion_id"), "attachments": attachments}
        anchor_count = self.conn.execute("SELECT COUNT(DISTINCT ref) FROM notion_comments WHERE page_id=? "
                                         "AND discussion_id=? AND is_bot=1 AND ref LIKE 'dlv:%'",
                                         (target['page_id'], c.get('discussion_id'))).fetchone()[0]
        if anchor_count > 1:
            bot = None
            payload['answer_binding_error'] = 'discussion names multiple deliveries; specify the exact question'
        if bot and bot['ref'] and not bot['ref'].startswith('dlv:'):
            payload['answer_binding_error'] = 'discussion has no verified delivery anchor; specify a question explicitly'
        if bot and bot["ref"]:
            payload["in_reply_to"] = bot["ref"]
            if bot["ref"].startswith("dlv:"):
                dlv = self.conn.execute("SELECT id, delegation_id, kind, seq FROM deliveries WHERE id=?",
                                        (bot["ref"][4:],)).fetchone()
                if dlv is None:
                    payload['answer_binding_error'] = 'discussion delivery is unavailable'
                if dlv:
                    payload.update(delegation_id=dlv["delegation_id"], delivery_id=dlv["id"],
                                   delivery_kind=dlv["kind"], delivery_seq=dlv["seq"])
        if prev is not None:
            payload["previous_text_hash"] = prev["content_hash"]
        is_user = bool(author) and author in user_ids   # no configured identity: nobody is the user
        if not user_ids:
            payload["identity_unverified"] = True
        # Each content change is a new revision of the comment, so A->B->A (even within one
        # timestamp) yields a new row for the restored A instead of reusing A's old one.
        revision = 1 if prev is None else int(prev["revision"] or 1) + 1
        payload["revision"] = revision
        with tx(self.conn):
            fid, created = feedback.record(
                self.conn, origin="notion_comment", source_id=c["id"],
                source_version=f"r{revision}|{c.get('last_edited_time') or ''}|{digest}", text=text,
                scope_type=scope_type, scope_id=target.get("entity_id"), author=author, author_is_user=is_user,
                kind="comment" if prev is None else "comment_edit", payload=payload,
                reply_to=c.get("discussion_id"))
            if prev is not None:
                feedback.supersede_older(self.conn, "notion_comment", c["id"], fid)
            self.conn.execute(
                "INSERT INTO notion_comments(comment_id, page_id, discussion_id, author_id, is_bot, last_edited_time,"
                " content_hash, feedback_id, seen_at, revision) VALUES (?,?,?,?,0,?,?,?,?,?) ON CONFLICT(comment_id)"
                " DO UPDATE SET last_edited_time=excluded.last_edited_time, content_hash=excluded.content_hash,"
                " feedback_id=excluded.feedback_id, seen_at=excluded.seen_at, revision=excluded.revision",
                (c["id"], target["page_id"], c.get("discussion_id"), author, c.get("last_edited_time"), digest, fid,
                 clock.iso(), revision))
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
        rendered = body.render(sections, marker=HOME_MARKER, lang=self.state.language)
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

    def resolve_projects(self) -> None:
        for pending in self.conn.execute("SELECT key,value FROM meta WHERE key LIKE 'notion.project_pending:%'").fetchall():
            request = loads(pending['value'], {})
            remote = request['page_id']
            project = self._local_project(remote)
            if project:
                with tx(self.conn):
                    task_id = pending['key'].split(':', 1)[1]
                    store.update_task(self.conn, task_id, {'project_id': project}, actor=request.get('actor', 'human'),
                                      origin='notion', cause_ref=remote)
                    self.conn.execute('DELETE FROM meta WHERE key=?', (pending['key'],))

    def enqueue_dirty(self, closed_days: int = 30) -> int:
        """Queue pushes for entities whose Notion values differ from what was last written."""
        self.resolve_projects()
        count = 0
        since = clock.iso(clock.now() - timedelta(days=closed_days))
        open_ = ",".join(repr(s) for s in store.OPEN_STATUSES)
        for t in self.conn.execute(f"SELECT * FROM tasks WHERE status IN ({open_}) OR closed_at>=?", (since,)).fetchall():
            row = self.entity_row("task", t["id"])
            if row and row["trashed"]:
                continue
            if row is None and t["origin"] == "migration" and t["status"] in store.CLOSED_STATUSES:
                continue  # migrated history: kept locally only, never shown in Notion
            task = dict(t)
            target = schema.comparable("task", self.task_values(task))
            stale_body = row is not None and row["rendered_hash"] != _h(self.render_body(task))
            if row is None or row["last_seen_props"] != {**row["last_seen_props"], **target} or stale_body:
                if effects.task_push(self.conn, t["id"]):
                    count += 1
        # Each delivered version gets its page once the task page exists.
        for d in self.conn.execute(
                "SELECT v.id, g.task_id FROM deliveries v JOIN delegations g ON g.id=v.delegation_id"
                " JOIN notion_pages n ON n.entity_type='task' AND n.entity_id=g.task_id AND n.trashed=0"
                " WHERE v.kind='result' AND NOT EXISTS (SELECT 1 FROM delivery_pages p WHERE p.delivery_id=v.id"
                " AND p.done_at IS NOT NULL) ORDER BY v.received_at").fetchall():
            if effects.delivery_push(self.conn, d["id"], d["task_id"]):
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
                ("notion", "comment"): self.post_comment, ("notion", "push_delivery"): self.push_delivery}

    def push_delivery(self, item: dict[str, Any], reconcile: bool) -> Any:
        return delivery_pages.push(self, item["payload"]["delivery_id"])

    def max_upload_bytes(self) -> int:
        if not hasattr(self, "_max_upload"):
            self._max_upload = self.client.max_upload_bytes()
        return self._max_upload


def _select_raw_present(page: dict[str, Any], pmap: schema.PropertyMap, field: str) -> bool:
    prop = next(p for p in schema.PROPS[pmap.kind] if p.field == field)
    key = pmap.ids.get(field)
    props = page.get("properties") or {}
    raw = next((v for v in props.values() if isinstance(v, dict) and v.get("id") == key), None) if key \
        else props.get(prop.name(pmap.lang))
    return bool(raw and raw.get("select"))


def _split_repos(text: str | None) -> list[str]:
    return [r.strip() for r in (text or "").replace("\n", ",").split(",") if r.strip()]


def _describe(exc: BaseException) -> str:
    return str(exc) if isinstance(exc, NotionError) else f"{type(exc).__name__}: {exc}"


def _cursor_error(conn: sqlite3.Connection, scope: str, error: str) -> None:
    with tx(conn):
        conn.execute(
            "INSERT INTO cursors(source, scope, last_attempt_at, last_error, consecutive_failures)"
            " VALUES ('notion',?,?,?,1) ON CONFLICT(source, scope) DO UPDATE SET"
            " last_attempt_at=excluded.last_attempt_at, last_error=excluded.last_error,"
            " consecutive_failures=cursors.consecutive_failures+1", (scope, clock.iso(), error[:1000]))


def new_setup_marker() -> str:
    return ids.new_id("run")


def _editable(kind: str) -> list[str]:
    """Fields the user may change in Notion; a project's page icon is one."""
    return schema.editable_fields(kind) + (["icon"] if kind == "project" else [])


def _page_icon(kind: str, page: dict[str, Any]) -> dict[str, Any]:
    return {"icon": project_icons.from_page(page.get("icon"))} if kind == "project" else {}
