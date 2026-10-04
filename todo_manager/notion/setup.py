"""Create the Notion workspace structure (home page, Projects/Tasks databases, home views).

``plan()`` describes every write without performing it (for review); ``apply()`` performs
the plan and records ids in ``meta``. Re-running ``apply()`` creates only what is missing.
This is a production write: run it only after the user approved the plan.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .. import clock
from ..db import get_meta, loads
from . import body, schema
from .client import NotionClient
from .sync import HOME_MARKER, save_state

HOME_TITLE = "Todo Manager"
OPEN_WORK = ["待办", "进行中"]


def _not_snoozed(p: dict[str, str]) -> dict[str, Any]:
    return {"or": [{"property": p["snooze_until"], "date": {"is_empty": True}},
                   {"property": p["snooze_until"], "date": {"on_or_before": "today"}}]}


def view_specs(p: dict[str, str], q: dict[str, str]) -> list[dict[str, Any]]:
    """Home sections. ``p``/``q`` map task/project fields to property ids."""

    def cols(m: dict[str, str], fields: list[str]) -> list[dict[str, Any]]:
        return [{"property_id": m[f], "visible": True} for f in fields]

    return [
        {"key": "focus", "heading": "⭐ 精选待办", "source": "task", "name": "精选待办", "type": "table",
         "note": "现在值得推进的事：精选，或一周内到期／已过期；暂缓中的不显示",
         "filter": {"and": [{"property": p["status"], "select": {"equals": OPEN_WORK}},
                            {"property": p["next_actor"], "select": {"equals": "我"}},
                            _not_snoozed(p),
                            {"or": [{"property": p["focus"], "checkbox": {"equals": True}},
                                    {"property": p["due_date"], "date": {"on_or_before": "one_week_from_now"}}]}]},
         "sorts": [{"property": p["due_date"], "direction": "ascending"},
                   {"property": p["updated"], "direction": "descending"}],
         "configuration": {"type": "table", "properties": cols(p, ["title", "next_step", "due_date", "project_id",
                                                                   "status", "change_note"])}},
        {"key": "projects", "heading": "📁 活跃项目", "source": "project", "name": "活跃项目", "type": "table",
         "filter": {"property": q["status"], "select": {"equals": "进行中"}},
         "sorts": [{"property": q["updated"], "direction": "descending"}],
         "configuration": {"type": "table", "properties": cols(q, ["title", "goal", "next_step", "blockers",
                                                                   "recent_activity"])}},
        {"key": "waiting", "heading": "⏳ 等待别人", "source": "task", "name": "等待别人", "type": "table",
         "filter": {"and": [{"property": p["status"], "select": {"equals": "等待"}}, _not_snoozed(p)]},
         "sorts": [{"property": p["follow_up_date"], "direction": "ascending"}],
         "configuration": {"type": "table", "properties": cols(p, ["title", "waiting_on", "follow_up_date",
                                                                   "next_step", "project_id"])}},
        {"key": "decision", "heading": "🤔 需要决定", "source": "task", "name": "需要决定", "type": "list",
         "filter": {"property": p["status"], "select": {"equals": "需要决定"}},
         "sorts": [{"property": p["due_date"], "direction": "ascending"}],
         "configuration": {"type": "list", "properties": cols(p, ["next_step", "due_date", "project_id"])}},
        {"key": "agent", "heading": "🤖 Agent 排队／处理中", "source": "task", "name": "Agent 处理中", "type": "board",
         "filter": {"property": p["delegation"], "select": {"equals": ["排队中", "执行中", "已暂停", "异常"]}},
         "configuration": {"type": "board", "group_by": {"type": "select", "property_id": p["delegation"],
                                                         "sort": {"type": "manual"}, "hide_empty_groups": True},
                           "properties": cols(p, ["next_step", "project_id"])}},
        {"key": "review", "heading": "✅ 待我验收／待我补充", "source": "task", "name": "待我验收", "type": "table",
         "filter": {"property": p["status"], "select": {"equals": "待我验收"}},
         "sorts": [{"property": p["updated"], "direction": "descending"}],
         "configuration": {"type": "table", "properties": cols(p, ["title", "delegation", "change_note",
                                                                   "project_id"])}},
        {"key": "recent", "heading": "🕘 最近完成与重要变化", "source": "task", "name": "最近变化", "type": "table",
         "filter": {"and": [{"property": p["updated"], "last_edited_time": {"on_or_after": "one_week_ago"}},
                            {"or": [{"property": p["status"], "select": {"equals": ["完成", "取消"]}},
                                    {"property": p["change_note"], "rich_text": {"is_not_empty": True}}]}]},
         "sorts": [{"property": p["updated"], "direction": "descending"}],
         "configuration": {"type": "table", "properties": cols(p, ["title", "status", "change_note", "project_id"])}},
    ]


def home_markdown() -> str:
    fresh = body.render({"extra": [("状态", "尚未运行同步")]}, marker=HOME_MARKER)
    return fresh + "\n" + "\n".join(
        f"## {spec_heading}" for spec_heading in ("⭐ 精选待办", "📁 活跃项目", "⏳ 等待别人", "🤔 需要决定",
                                                 "🤖 Agent 排队／处理中", "✅ 待我验收／待我补充", "🕘 最近完成与重要变化"))


def plan(parent_page_id: str | None = None, home_page_id: str | None = None) -> list[str]:
    """Human-readable list of the writes ``apply`` would perform on a fresh setup."""
    default_ids = {p.field: p.name for p in schema.TASK_PROPS}
    project_ids = {p.field: p.name for p in schema.PROJECT_PROPS}
    first = (f"把现有页面 {home_page_id} 用作首页：在页面顶部插入数据新鲜度提示框 + 7 个分区标题（页面原有内容保留在下方）"
             if home_page_id else f"在父页面 {parent_page_id} 下新建页面「{HOME_TITLE}」：顶部数据新鲜度提示框 + 7 个分区标题")
    steps = [first,
             "在首页下新建数据库「项目」：" + "、".join(f"{p.name}({p.type})" for p in schema.PROJECT_PROPS),
             "在首页下新建数据库「任务」：" + "、".join(f"{p.name}({p.type})" for p in schema.TASK_PROPS)
             + f"；「项目」为双向关联，在项目库生成「{schema.TASKS_RELATION_NAME}」列"]
    for spec in view_specs(default_ids, project_ids):
        steps.append(f"在分区「{spec['heading']}」下插入关联视图「{spec['name']}」({spec['type']})")
    steps.append("把数据源、属性 ID、bot 用户 ID 记入本地状态（state/todo.sqlite 的 meta 表）")
    return steps


def apply(conn: sqlite3.Connection, client: NotionClient, parent_page_id: str | None = None,
          home_page_id: str | None = None) -> dict[str, Any]:
    """Create what is missing. ``home_page_id`` adopts an existing (e.g. new, empty top-level)
    page as the home page; otherwise a new home page is created under ``parent_page_id``."""
    if not (parent_page_id or home_page_id):
        raise ValueError("either a parent page or an existing home page is required")
    done: dict[str, Any] = {}
    me = client.me()
    save_state(conn, bot_user_id=me["id"])
    home = get_meta(conn, "notion.home_page_id")
    if not home and home_page_id:
        page = client.retrieve_page(home_page_id)
        if page.get("in_trash"):
            raise ValueError(f"page {home_page_id} is in the trash")
        current = client.page_markdown(page["id"]).get("markdown") or ""
        if body.extract(current, HOME_MARKER) is None:
            client.update_markdown(page["id"], {"type": "insert_content", "insert_content": {
                "content": home_markdown(), "position": {"type": "start"}}})
        home = page["id"]
        save_state(conn, home_page_id=home)
        done["home_page"] = home
    if not home:
        page = client.create_page({"type": "page_id", "page_id": parent_page_id},
                                  {"title": {"title": [{"type": "text", "text": {"content": HOME_TITLE}}]}},
                                  markdown=home_markdown(), icon={"type": "emoji", "emoji": "✅"})
        home = page["id"]
        save_state(conn, home_page_id=home)
        done["home_page"] = home
    projects_ds = get_meta(conn, "notion.projects_data_source_id")
    if not projects_ds:
        db = client.create_database(home, "项目", schema.data_source_properties("project"), icon="📁")
        projects_ds = db["data_sources"][0]["id"]
        save_state(conn, projects_database_id=db["id"], projects_data_source_id=projects_ds)
        done["projects_data_source"] = projects_ds
    tasks_ds = get_meta(conn, "notion.tasks_data_source_id")
    if not tasks_ds:
        db = client.create_database(home, "任务", schema.data_source_properties("task", projects_ds), icon="✅")
        tasks_ds = db["data_sources"][0]["id"]
        save_state(conn, tasks_database_id=db["id"], tasks_data_source_id=tasks_ds)
        done["tasks_data_source"] = tasks_ds
    tmap = schema.PropertyMap.from_data_source("task", client.retrieve_data_source(tasks_ds),
                                               loads(get_meta(conn, "notion.props.task"), {}))
    pmap = schema.PropertyMap.from_data_source("project", client.retrieve_data_source(projects_ds),
                                               loads(get_meta(conn, "notion.props.project"), {}))
    missing = tmap.missing() + pmap.missing()
    if missing:
        raise RuntimeError(f"data sources lack properties {missing}; fix the schema before continuing")
    save_state(conn, **{"props.task": tmap.ids, "props.project": pmap.ids})
    views = loads(get_meta(conn, "notion.views"), {}) or {}
    headings = _headings(client, home)
    for spec in view_specs(tmap.ids, pmap.ids):
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
        view = client.create_view(req)
        views[spec["key"]] = view["id"]
        save_state(conn, views=views)
    done["views"] = views
    if not get_meta(conn, "notion.setup_at"):
        save_state(conn, setup_at=clock.iso())
    return done


def _headings(client: NotionClient, page_id: str) -> dict[str, str]:
    result = {}
    for block in client.block_children(page_id):
        kind = block.get("type", "")
        if kind.startswith("heading_"):
            text = "".join(t.get("plain_text", "") for t in block.get(kind, {}).get("rich_text", []))
            result[text.strip()] = block["id"]
    return result
