"""A linked view of a project's open tasks on its project page.

Each active or paused project page gets one table view of the Tasks database, filtered by that
project and by an open status. It is created once per project: ``notion.project_views`` maps the
project to its view, so a view the user deletes or changes is left alone. Archived and done
projects get none (one is added if the project becomes active again), and so does a page in the
trash.
"""

from __future__ import annotations

from typing import Any

from .. import outbox
from ..db import dumps, get_meta, loads, set_meta, tx
from . import schema
from .client import NotionUncertain

META = "notion.project_views"
STATUSES = ("active", "paused")


def created(conn) -> dict[str, str]:
    return loads(get_meta(conn, META), {}) or {}


def enqueue_missing(sync: Any) -> int:
    """Queue a view for every active or paused project page that never had one."""
    done = created(sync.conn)
    count = 0
    for row in sync.conn.execute(
            "SELECT p.id FROM projects p JOIN notion_pages n ON n.entity_type='project' AND n.entity_id=p.id"
            f" AND n.trashed=0 WHERE p.status IN ({','.join('?' * len(STATUSES))})", STATUSES).fetchall():
        if row["id"] not in done and outbox.enqueue(
                sync.conn, target="notion", op="project_view", entity_type="project", entity_id=row["id"],
                dedupe_key=f"project_view:{row['id']}", payload={"project_id": row["id"]}):
            count += 1
    return count


def request(sync: Any, page_id: str) -> dict[str, Any]:
    p = sync.pmap("task").ids
    lang = sync.state.language
    shown = [f for f in ("title", "status", "next_actor", "next_step", "due_date", "agent_progress") if f in p]
    return {"create_database": {"parent": {"type": "page_id", "page_id": page_id}},
            "data_source_id": sync.state.tasks_data_source_id, "name": sync.t("view.project_tasks"), "type": "table",
            "filter": {"and": [{"property": p["project_id"], "relation": {"contains": page_id}},
                               *({"property": p["status"], "select": {"does_not_equal": schema.label("task_status", v, lang)}}
                                 for v in ("done", "cancelled"))]},
            "sorts": [{"property": p["due_date"], "direction": "ascending"},
                      {"property": p["updated"], "direction": "descending"}],
            "configuration": {"type": "table", "properties": [{"property_id": p[f], "visible": True} for f in shown]}}


def push(sync: Any, project_id: str, reconcile: bool) -> Any:
    if project_id in created(sync.conn):
        return {"skipped": "view exists"}
    row = sync.entity_row("project", project_id)
    if row is None or row["trashed"] or sync.client.retrieve_page(row["page_id"]).get("in_trash"):
        return {"skipped": "no project page"}
    req = request(sync, row["page_id"])
    view = _find(sync, req) if reconcile else None
    if view is None:
        try:
            view = sync.client.create_view(req)
        except NotionUncertain as exc:
            if not exc.committed_id:
                raise outbox.Uncertain(str(exc)) from None
            view = {"id": exc.committed_id}
    with tx(sync.conn):
        set_meta(sync.conn, META, dumps({**created(sync.conn), project_id: view["id"]}))
    return {"view": view["id"]}


def _find(sync: Any, req: dict[str, Any]) -> dict[str, Any] | None:
    """The view an earlier attempt may have created: same name, filtered by the same page."""
    page_id = req["create_database"]["parent"]["page_id"]
    for item in sync.client.list_views(data_source_id=req["data_source_id"]):
        view = sync.client.retrieve_view(item["id"])
        if view.get("name") == req["name"] and page_id in dumps(view.get("filter")):
            return view
    return None
