"""Notion Project/Task schema and the mapping to local fields.

Properties are written and read by property ID once the data sources exist (IDs are
stored in ``meta`` after setup), so renaming a column in Notion does not break sync.
Default names below are used when creating the data sources and in tests.

Editable properties are imported back as human edits. Program-owned properties
(``TM ID``, ``委派``, ``来源``) are rewritten from local state; manual edits to
them are reverted on the next push.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .client import plain_text, rich_text

TASK_STATUS_LABELS = {"todo": "待办", "in_progress": "进行中", "waiting": "等待", "decision": "需要决定",
                      "review": "待我验收", "done": "完成", "cancelled": "取消"}
NEXT_ACTOR_LABELS = {"me": "我", "agent": "Agent", "external": "外部"}
PROJECT_STATUS_LABELS = {"active": "进行中", "paused": "暂停", "done": "完成", "archived": "归档"}
DELEGATION_LABELS = {"queued": "排队中", "running": "执行中", "blocked": "等我回答", "review": "待我验收",
                     "paused": "已暂停", "quarantined": "异常"}
SOURCE_LABELS = {"gmail": "邮件", "slack": "Slack", "github": "GitHub", "commit": "GitHub", "notion": "Notion",
                 "matrix": "Matrix", "cli": "手动", "migration": "迁移"}

COLORS = {"待办": "default", "进行中": "blue", "等待": "yellow", "需要决定": "orange", "待我验收": "purple",
          "完成": "green", "取消": "gray", "我": "blue", "Agent": "purple", "外部": "yellow", "暂停": "yellow",
          "归档": "gray", "排队中": "gray", "执行中": "blue", "等我回答": "orange", "已暂停": "yellow", "异常": "red",
          "邮件": "red", "Slack": "purple", "GitHub": "default", "Notion": "default", "Matrix": "green",
          "手动": "blue", "迁移": "brown"}


@dataclass(frozen=True)
class Prop:
    field: str          # local field name
    name: str           # default Notion property name
    type: str           # title | rich_text | select | relation | date | checkbox | last_edited_time
    editable: bool = True
    labels: dict[str, str] = field(default_factory=dict)   # local value -> Notion option name


TASK_PROPS: tuple[Prop, ...] = (
    Prop("title", "任务", "title"),
    Prop("status", "状态", "select", labels=TASK_STATUS_LABELS),
    Prop("next_actor", "下一步由谁", "select", labels=NEXT_ACTOR_LABELS),
    Prop("project_id", "项目", "relation"),
    Prop("next_step", "下一步", "rich_text"),
    Prop("waiting_on", "等待对象", "rich_text"),
    Prop("due_date", "截止日期", "date"),
    Prop("follow_up_date", "跟进日期", "date"),
    Prop("snooze_until", "暂缓至", "date"),
    Prop("focus", "精选", "checkbox"),
    Prop("change_note", "变化", "rich_text"),
    Prop("delegation", "委派", "select", editable=False, labels=DELEGATION_LABELS),
    Prop("source", "来源", "select", editable=False, labels={k: v for k, v in SOURCE_LABELS.items()}),
    Prop("tm_id", "TM ID", "rich_text", editable=False),
    Prop("updated", "更新于", "last_edited_time", editable=False),
)

PROJECT_PROPS: tuple[Prop, ...] = (
    Prop("title", "项目", "title"),
    Prop("status", "状态", "select", labels=PROJECT_STATUS_LABELS),
    Prop("domain", "领域", "select"),
    Prop("goal", "目标", "rich_text"),
    Prop("next_step", "下一步", "rich_text"),
    Prop("blockers", "阻碍", "rich_text"),
    Prop("recent_activity", "近期进展", "rich_text"),
    Prop("repos", "仓库", "rich_text"),
    Prop("recent_activity_at", "进展时间", "date", editable=False),
    Prop("tm_id", "TM ID", "rich_text", editable=False),
    Prop("updated", "更新于", "last_edited_time", editable=False),
)
TASKS_RELATION_NAME = "任务"   # dual relation created on Projects by the Task "项目" relation

PROPS = {"task": TASK_PROPS, "project": PROJECT_PROPS}


def editable_fields(kind: str) -> list[str]:
    return [p.field for p in PROPS[kind] if p.editable and p.type != "last_edited_time"]


def written_props(kind: str) -> list[Prop]:
    return [p for p in PROPS[kind] if p.type != "last_edited_time"]


# --------------------------------------------------------------------------- schema creation

def _options(labels: dict[str, str]) -> list[dict[str, Any]]:
    seen: list[str] = []
    for label in labels.values():
        if label not in seen:
            seen.append(label)
    return [{"name": name, "color": COLORS.get(name, "default")} for name in seen]


def data_source_properties(kind: str, projects_data_source_id: str | None = None) -> dict[str, Any]:
    """Property configuration for ``initial_data_source`` when creating a database."""
    result: dict[str, Any] = {}
    for p in PROPS[kind]:
        if p.type == "relation":
            if not projects_data_source_id:
                raise ValueError("the Task relation needs the Projects data source id")
            result[p.name] = {"relation": {"data_source_id": projects_data_source_id, "type": "dual_property",
                                           "dual_property": {"synced_property_name": TASKS_RELATION_NAME}}}
        elif p.type == "select":
            result[p.name] = {"select": {"options": _options(p.labels)}}
        else:
            result[p.name] = {p.type: {}}
    return result


# --------------------------------------------------------------------------- property ids

class PropertyMap:
    """field -> Notion property id (falls back to default names, e.g. before setup)."""

    def __init__(self, kind: str, ids: dict[str, str] | None = None):
        self.kind = kind
        self.ids = dict(ids or {})

    @classmethod
    def from_data_source(cls, kind: str, data_source: dict[str, Any], known: dict[str, str] | None = None) -> "PropertyMap":
        by_name = {name: prop["id"] for name, prop in (data_source.get("properties") or {}).items()}
        by_id = set(by_name.values())
        ids: dict[str, str] = {}
        for p in PROPS[kind]:
            if known and known.get(p.field) in by_id:
                ids[p.field] = known[p.field]
            elif p.name in by_name:
                ids[p.field] = by_name[p.name]
        return cls(kind, ids)

    def key(self, prop: Prop) -> str:
        return self.ids.get(prop.field, prop.name)

    def missing(self) -> list[str]:
        return [p.name for p in PROPS[self.kind] if p.field not in self.ids]


# --------------------------------------------------------------------------- values

def _date(value: Any) -> str | None:
    if not value:
        return None
    return str(value)[:10]


def render_value(prop: Prop, value: Any) -> dict[str, Any]:
    if prop.type == "title":
        return {"title": rich_text(value or "")}
    if prop.type == "rich_text":
        return {"rich_text": rich_text(value or "")}
    if prop.type == "select":
        label = prop.labels.get(value, value) if value else None
        return {"select": {"name": label} if label else None}
    if prop.type == "relation":
        return {"relation": [{"id": value}] if value else []}
    if prop.type == "date":
        day = _date(value)
        return {"date": {"start": day} if day else None}
    if prop.type == "checkbox":
        return {"checkbox": bool(value)}
    raise ValueError(f"cannot render {prop.type}")


def parse_value(prop: Prop, raw: dict[str, Any] | None) -> tuple[Any, str | None]:
    """Return (local value, problem). ``problem`` is set for unmappable values."""
    if raw is None:
        return None, "property missing"
    kind = raw.get("type", prop.type)
    if kind in ("title", "rich_text"):
        text = plain_text(raw.get(kind)).strip()
        return (text or None), None
    if kind == "select":
        option = raw.get("select")
        if not option:
            return None, None
        name = option.get("name")
        if not prop.labels:
            return name, None
        reverse = {v: k for k, v in prop.labels.items()}
        if name in reverse:
            return reverse[name], None
        return None, f"unknown option {name!r} for {prop.name}"
    if kind == "relation":
        rel = raw.get("relation") or []
        problem = f"{prop.name} has {len(rel)} relations; using the first" if len(rel) > 1 else None
        return (rel[0]["id"] if rel else None), problem
    if kind == "date":
        date = raw.get("date")
        return (_date(date.get("start")) if date else None), None
    if kind == "checkbox":
        return bool(raw.get("checkbox")), None
    if kind == "last_edited_time":
        return raw.get("last_edited_time"), None
    return None, f"unexpected property type {kind}"


def parse_page(kind: str, page: dict[str, Any], pmap: PropertyMap) -> tuple[dict[str, Any], list[str]]:
    """Normalised values of all known properties of a page, plus mapping problems."""
    props = page.get("properties") or {}
    by_id = {v.get("id"): v for v in props.values() if isinstance(v, dict)}
    values: dict[str, Any] = {}
    problems: list[str] = []
    for p in PROPS[kind]:
        key = pmap.ids.get(p.field)
        raw = by_id.get(key) if key else props.get(p.name)
        value, problem = parse_value(p, raw)
        values[p.field] = value
        if problem and problem != "property missing":
            problems.append(problem)
    return values, problems


def render_properties(kind: str, values: dict[str, Any], pmap: PropertyMap,
                      only: set[str] | None = None) -> dict[str, Any]:
    result = {}
    for p in written_props(kind):
        if only is not None and p.field not in only:
            continue
        result[pmap.key(p)] = render_value(p, values.get(p.field))
    return result


def comparable(kind: str, values: dict[str, Any]) -> dict[str, Any]:
    """Values normalised exactly as a Notion round trip would return them."""
    out: dict[str, Any] = {}
    for p in written_props(kind):
        value = values.get(p.field)
        if p.type in ("title", "rich_text"):
            value = (str(value).strip() or None) if value else None
        elif p.type == "date":
            value = _date(value)
        elif p.type == "checkbox":
            value = bool(value)
        elif p.type == "select" and not p.labels:
            value = value or None
        out[p.field] = value
    return out
