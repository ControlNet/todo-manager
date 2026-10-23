"""Create the Notion workspace structure (home page, Projects/Tasks databases, home views).

``plan()`` describes every write without performing it (for review); ``apply()`` performs
the plan and records ids in ``meta``. Re-running ``apply()`` creates only what is missing.
This is a production write: run it only after the user approved the plan.

Names, headings and option labels are created in the configured language, which is then
stored as ``notion.language`` so later runs keep writing the same labels.
"""

from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import unquote

from .. import clock, delegation, i18n
from ..db import get_meta, loads, tx
from . import body, schema
from .client import NotionClient, NotionError
from .sync import HOME_MARKER, save_state

SECTIONS = ("decision", "agent", "focus", "projects", "waiting", "all", "recent")   # the user's order
# 2: card events no longer set a Task to "review": "Agent work" shows every state and "To
# review" also lists agent work waiting for the user (question, delivery).
# 3: actionable agent work is visible in focus without a focus flag or near deadline.
# The Agent progress board (one board instead of Agent work + To review; Focus without agent
# work) is installed by ``agent_board_migration``, not by these filter upgrades.
VIEWS_VERSION = 3


def _not_snoozed(p: dict[str, str]) -> dict[str, Any]:
    return {"or": [{"property": p["snooze_until"], "date": {"is_empty": True}},
                   {"property": p["snooze_until"], "date": {"on_or_before": "today"}}]}


def view_specs(p: dict[str, str], q: dict[str, str], lang: str | None = None) -> list[dict[str, Any]]:
    """Home sections. ``p``/``q`` map task/project fields to property ids."""

    def cols(m: dict[str, str], fields: list[str]) -> list[dict[str, Any]]:
        return [{"property_id": m[f], "visible": True} for f in fields]

    def task(*values: str) -> str | list[str]:
        names = [schema.label("task_status", v, lang) for v in values]
        return names[0] if len(names) == 1 else names

    def spec(key: str, source: str, kind: str, **rest: Any) -> dict[str, Any]:
        return {"key": key, "heading": i18n.t(f"home.section.{key}", lang), "source": source,
                "name": i18n.t(f"view.{key}", lang), "type": kind, **rest}

    specs = [
        # Worth pushing now: focus, or due within a week / overdue; snoozed tasks are hidden.
        # Agent work waiting for the user is on the Agent board, not here.
        spec("focus", "task", "table",
             filter={"and": [{"property": p["status"], "select": {"equals": task("todo", "in_progress")}},
                             {"property": p["next_actor"], "select": {"equals": schema.label("next_actor", "me", lang)}},
                             _not_snoozed(p),
                             {"or": [{"property": p["focus"], "checkbox": {"equals": True}},
                                     {"property": p["due_date"], "date": {"on_or_before": "one_week_from_now"}}]}]},
             sorts=[{"property": p["due_date"], "direction": "ascending"},
                    {"property": p["updated"], "direction": "descending"}],
             configuration={"type": "table", "properties": cols(p, ["title", "next_step", "due_date", "project_id",
                                                                    "status", "change_note"])}),
        spec("decision", "task", "board",
             filter={"or": [{"property": p["status"], "select": {"equals": task("decision")}},
                            {"property": p["decision"], "select": {"is_not_empty": True}}]},
             sorts=[{"property": p["due_date"], "direction": "ascending"}],
             configuration={"type": "board", "group_by": {"type": "select", "property_id": p["decision"],
                                                          "sort": {"type": "manual"}, "hide_empty_groups": False},
                            "properties": cols(p, ["title", "next_step", "due_date", "project_id"])}),
        spec("all", "task", "table",
             filter={"and": [{"property": p["status"], "select": {"does_not_equal": task("done")}},
                             {"property": p["status"], "select": {"does_not_equal": task("cancelled")}}]},
             sorts=[{"property": p["due_date"], "direction": "ascending"}],
             configuration={"type": "table", "properties": cols(p, ["title", "status", "next_step", "due_date", "next_actor", "decision"])}),
        spec("projects", "project", "table",
             filter={"property": q["status"], "select": {"equals": schema.label("project_status", "active", lang)}},
             sorts=[{"property": q["updated"], "direction": "descending"}],
             configuration={"type": "table", "properties": cols(q, ["title", "goal", "next_step", "blockers",
                                                                    "recent_activity"])}),
        spec("waiting", "task", "table",
             filter={"and": [{"property": p["status"], "select": {"equals": task("waiting")}}, _not_snoozed(p)]},
             sorts=[{"property": p["follow_up_date"], "direction": "ascending"}],
             configuration={"type": "table", "properties": cols(p, ["title", "waiting_on", "follow_up_date",
                                                                    "next_step", "project_id"])}),
        # Every column shown: the user's choices (Done / My to-do / Cancel) are empty drop targets.
        spec("agent", "task", "board",
             filter={"property": p["agent_progress"], "select": {"is_not_empty": True}},
             sorts=[{"property": p["updated"], "direction": "descending"}],
             configuration={"type": "board", "group_by": {"type": "select", "property_id": p["agent_progress"],
                                                          "sort": {"type": "manual"}, "hide_empty_groups": False},
                            "properties": cols(p, ["next_step", "delegation", "change_note", "project_id"])}),
        spec("recent", "task", "table",
             filter={"and": [{"property": p["updated"], "last_edited_time": {"on_or_after": "one_week_ago"}},
                             {"or": [{"property": p["status"], "select": {"equals": task("done", "cancelled")}},
                                     {"property": p["change_note"], "rich_text": {"is_not_empty": True}}]}]},
             sorts=[{"property": p["updated"], "direction": "descending"}],
             configuration={"type": "table", "properties": cols(p, ["title", "status", "change_note", "project_id"])}),
    ]
    return sorted(specs, key=lambda s: SECTIONS.index(s["key"]))


def _earlier_filters(p: dict[str, str], lang: str | None) -> dict[str, dict[str, Any]]:
    """Earlier owned filters (versions 1 and 2), used to preserve customized views. The Agent work
    and To review views are replaced by ``agent_board_migration``."""
    # Version 2 focus required an explicit focus flag or a near deadline.
    focus = {"and": [
        {"property": p["status"], "select": {"equals": [schema.label('task_status', v, lang) for v in ('todo', 'in_progress')]}},
        {"property": p["next_actor"], "select": {"equals": schema.label('next_actor', 'me', lang)}},
        _not_snoozed(p),
        {"or": [{"property": p['focus'], "checkbox": {"equals": True}},
                {"property": p['due_date'], "date": {"on_or_before": 'one_week_from_now'}}]}]}
    return {"focus": focus}


def view_upgrades(conn: sqlite3.Connection, client: NotionClient) -> tuple[list[dict[str, Any]], list[str]]:
    """Views of an earlier version: (filter updates for views still as created, notes for
    views the user changed or removed - those are left alone). Read-only."""
    if int(get_meta(conn, "notion.views_version") or 1) >= VIEWS_VERSION:
        return [], []
    lang = i18n.normalize(get_meta(conn, "notion.language")) or i18n.language()
    views = loads(get_meta(conn, "notion.views"), {}) or {}
    tmap = loads(get_meta(conn, "notion.props.task"), {})
    pmap = loads(get_meta(conn, "notion.props.project"), {})
    earlier = _earlier_filters(tmap, lang)
    prop = next(p for p in schema.TASK_PROPS if p.field == "delegation").name(lang)
    states = i18n.t("list.sep", lang).join(schema.label("delegation", v, lang) for v in delegation.NEEDS_USER_LABELS)
    updates, notes = [], []
    for spec in view_specs(tmap, pmap, lang):
        if spec["key"] not in earlier or spec["key"] not in views:
            continue
        try:
            live = client.retrieve_view(views[spec["key"]]).get("filter")
        except NotionError as exc:
            if exc.status != 404:
                raise
            notes.append(i18n.t("setup.view_missing", lang, view=spec["name"]))
            continue
        if live == spec["filter"]:
            continue
        if live == earlier[spec["key"]]:
            what = i18n.t(f"setup.view_update.{spec['key']}", lang)
            updates.append({"key": spec["key"], "view_id": views[spec["key"]], "filter": spec["filter"],
                            "change": i18n.t("setup.view_update", lang, name=spec["name"], what=what)})
        else:
            notes.append(i18n.t(f"setup.view_kept.{spec['key']}", lang, view=spec["name"], prop=prop, states=states))
    return updates, notes


def apply_view_upgrades(conn: sqlite3.Connection, client: NotionClient, updates: list[dict[str, Any]]) -> None:
    """Write the approved ``view_upgrades`` and record that the views are current."""
    for u in updates:
        client.update_view(u["view_id"], {"filter": u["filter"]})
    save_state(conn, views_version=str(VIEWS_VERSION))


def home_markdown(lang: str | None = None) -> str:
    fresh = body.render({"extra": [(i18n.t("home.status", lang), i18n.t("home.not_run_yet", lang))]},
                        marker=HOME_MARKER, lang=lang)
    return fresh + "\n" + "\n".join(f"## {i18n.t(f'home.section.{key}', lang)}" for key in SECTIONS)


def plan(parent_page_id: str | None = None, home_page_id: str | None = None,
         lang: str | None = None) -> list[str]:
    """Human-readable list of the writes ``apply`` would perform on a fresh setup."""
    def t(key: str, **values: Any) -> str:
        return i18n.t(key, lang, **values)

    def props(kind: str) -> str:
        return t("list.sep").join(f"{p.name(lang)}({p.type})" for p in schema.PROPS[kind])

    task_names = {p.field: p.name(lang) for p in schema.TASK_PROPS}
    project_names = {p.field: p.name(lang) for p in schema.PROJECT_PROPS}
    first = (t("setup.adopt_home", page=home_page_id) if home_page_id
             else t("setup.create_home", page=parent_page_id, title=t("notion.home_title")))
    steps = [first,
             t("setup.create_db", name=t("notion.db.projects"), props=props("project")),
             t("setup.create_db", name=t("notion.db.tasks"), props=props("task"))
             + t("setup.relation", project=task_names["project_id"], back=t("notion.relation_back"))]
    for spec in view_specs(task_names, project_names, lang):
        steps.append(t("setup.view", heading=spec["heading"], name=spec["name"], type=spec["type"]))
    steps.append(t("setup.record"))
    return steps


def apply(conn: sqlite3.Connection, client: NotionClient, parent_page_id: str | None = None,
          home_page_id: str | None = None, lang: str | None = None) -> dict[str, Any]:
    """Create what is missing. ``home_page_id`` adopts an existing (e.g. new, empty top-level)
    page as the home page; otherwise a new home page is created under ``parent_page_id``.
    A re-run keeps the language of the first setup."""
    if not (parent_page_id or home_page_id):
        raise ValueError("either a parent page or an existing home page is required")
    lang = i18n.normalize(get_meta(conn, "notion.language")) or i18n.normalize(lang) or i18n.language()
    done: dict[str, Any] = {"language": lang}
    me = client.me()
    save_state(conn, bot_user_id=me["id"], language=lang)
    home = get_meta(conn, "notion.home_page_id")
    if not home and home_page_id:
        page = client.retrieve_page(home_page_id)
        if page.get("in_trash"):
            raise ValueError(f"page {home_page_id} is in the trash")
        current = client.page_markdown(page["id"]).get("markdown") or ""
        if body.extract(current, HOME_MARKER) is None:
            client.update_markdown(page["id"], {"type": "insert_content", "insert_content": {
                "content": home_markdown(lang), "position": {"type": "start"}}})
        home = page["id"]
        save_state(conn, home_page_id=home)
        done["home_page"] = home
    if not home:
        page = client.create_page({"type": "page_id", "page_id": parent_page_id},
                                  {"title": {"title": [{"type": "text",
                                                        "text": {"content": i18n.t("notion.home_title", lang)}}]}},
                                  markdown=home_markdown(lang), icon={"type": "emoji", "emoji": "✅"})
        home = page["id"]
        save_state(conn, home_page_id=home)
        done["home_page"] = home
    projects_ds = get_meta(conn, "notion.projects_data_source_id")
    if not projects_ds:
        db = client.create_database(home, i18n.t("notion.db.projects", lang),
                                    schema.data_source_properties("project", lang=lang), icon="📁")
        projects_ds = db["data_sources"][0]["id"]
        save_state(conn, projects_database_id=db["id"], projects_data_source_id=projects_ds)
        done["projects_data_source"] = projects_ds
    tasks_ds = get_meta(conn, "notion.tasks_data_source_id")
    if not tasks_ds:
        db = client.create_database(home, i18n.t("notion.db.tasks", lang),
                                    schema.data_source_properties("task", projects_ds, lang), icon="✅")
        tasks_ds = db["data_sources"][0]["id"]
        save_state(conn, tasks_database_id=db["id"], tasks_data_source_id=tasks_ds)
        done["tasks_data_source"] = tasks_ds
    tmap = schema.PropertyMap.from_data_source("task", client.retrieve_data_source(tasks_ds),
                                               loads(get_meta(conn, "notion.props.task"), {}), lang)
    pmap = schema.PropertyMap.from_data_source("project", client.retrieve_data_source(projects_ds),
                                               loads(get_meta(conn, "notion.props.project"), {}), lang)
    missing = tmap.missing() + pmap.missing()
    if missing:
        raise RuntimeError(f"data sources lack properties {missing}; fix the schema before continuing")
    for kind, mapping in (("task", tmap), ("project", pmap)):
        # A property recreated in Notion (a new id): what the pages showed in the old one is no
        # baseline for the new one, so an empty new property is not read as the user clearing it.
        old = loads(get_meta(conn, f"notion.props.{kind}"), {}) or {}
        for field in sorted(f for f, pid in mapping.ids.items() if old.get(f) and old[f] != pid):
            with tx(conn):
                conn.execute("UPDATE notion_pages SET last_seen_props=json_remove(last_seen_props, ?)"
                             " WHERE entity_type=? AND last_seen_props IS NOT NULL", (f"$.{field}", kind))
    save_state(conn, **{"props.task": tmap.ids, "props.project": pmap.ids})
    views = loads(get_meta(conn, "notion.views"), {}) or {}
    fresh = not views
    headings = _headings(client, home)
    for spec in view_specs(tmap.ids, pmap.ids, lang):
        if spec["key"] in views:
            continue
        create: dict[str, Any] = {"parent": {"type": "page_id", "page_id": home}}
        if spec["heading"] in headings:
            create["position"] = {"type": "after_block", "block_id": headings[spec["heading"]]}
        req = {"create_database": create, "data_source_id": tasks_ds if spec["source"] == "task" else projects_ds,
               "name": spec["name"], "type": spec["type"], "configuration": spec["configuration"]}
        if spec.get("filter"):
            req["filter"] = spec["filter"]
        if spec.get("sorts"):
            req["sorts"] = spec["sorts"]
        view = settle_grouping(client, client.create_view(req), spec["configuration"])
        views[spec["key"]] = view["id"]
        save_state(conn, views=views)
    done["views"] = views
    if fresh:
        save_state(conn, views_version=str(VIEWS_VERSION), decision_ui_version="2", agent_ui_version="1")
    if not get_meta(conn, "notion.setup_at"):
        save_state(conn, setup_at=clock.iso())
    return done


def settle_grouping(client: NotionClient, view: dict[str, Any], configuration: dict[str, Any]) -> dict[str, Any]:
    """Notion may create a board grouped its own way (by Status, empty groups hidden: the Agent
    board on 2026-10-12, showing one column); set the requested grouping when the view lacks it."""
    want = (configuration or {}).get("group_by")
    have = (view.get("configuration") or {}).get("group_by") or {}
    if not want or (unquote(have.get("property_id") or "") == unquote(want["property_id"])
                    and have.get("hide_empty_groups") == want.get("hide_empty_groups")):
        return view
    return client.update_view(view["id"], {"configuration": configuration})


def _headings(client: NotionClient, page_id: str) -> dict[str, str]:
    result = {}
    for block in client.block_children(page_id):
        kind = block.get("type", "")
        if kind.startswith("heading_"):
            text = "".join(t.get("plain_text", "") for t in block.get(kind, {}).get("rich_text", []))
            result[text.strip()] = block["id"]
    return result
