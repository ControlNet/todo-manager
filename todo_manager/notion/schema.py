"""Notion Project/Task schema and the mapping to local fields.

Properties are written and read by property ID once the data sources exist (IDs are
stored in ``meta`` after setup), so renaming a column in Notion does not break sync.
Property and option names come from the language catalogs (``notion.*`` and ``label.*``
keys) in the language that was active at setup; option names of every language are
accepted when reading.

Editable properties are imported back as human edits. Program-owned properties
(TM ID, agent work state, source) are rewritten from local state; manual edits to
them are reverted on the next push.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .. import i18n
from .client import plain_text, rich_text

# Option colours by local value (the option NAMES come from the language catalogs).
COLORS = {
    "task_status": {"todo": "default", "in_progress": "blue", "waiting": "yellow", "decision": "orange",
                    "review": "purple", "done": "green", "cancelled": "gray"},
    "next_actor": {"me": "blue", "agent": "purple", "external": "yellow"},
    "project_status": {"active": "blue", "paused": "yellow", "done": "green", "archived": "gray"},
    "delegation": {"queued": "gray", "running": "blue", "blocked": "orange", "review": "purple",
                   "paused": "yellow", "quarantined": "red"},
    "source": {"gmail": "red", "slack": "purple", "github": "default", "commit": "default", "notion": "default",
               "matrix": "green", "cli": "blue", "migration": "brown"},
}
SELECT_VALUES = {   # values that become Notion select options (delegation ended/cancelled are body-only)
    "delegation": ("queued", "running", "blocked", "review", "paused", "quarantined"),
}


def labels(group: str, lang: str | None = None) -> dict[str, str]:
    """local value -> option name in ``lang``, in the canonical option order (``COLORS``)."""
    allowed = SELECT_VALUES.get(group)
    names = i18n.prefixed(f"label.{group}", lang)
    order = [*COLORS.get(group, {}), *sorted(set(names) - set(COLORS.get(group, {})))]
    return {k: names[k] for k in order if k in names and (allowed is None or k in allowed)}


def label(group: str, value: str | None, lang: str | None = None) -> str | None:
    if not value:
        return None
    return i18n.prefixed(f"label.{group}", lang).get(value, value)


@dataclass(frozen=True)
class Prop:
    kind: str           # task | project
    field: str          # local field name
    type: str           # title | rich_text | select | relation | date | checkbox | last_edited_time
    editable: bool = True
    group: str | None = None   # option label group for selects with fixed options

    def name(self, lang: str | None = None) -> str:
        return i18n.t(f"notion.{self.kind}.{self.field}", lang)

    def names(self) -> set[str]:
        return i18n.variants(f"notion.{self.kind}.{self.field}")


TASK_PROPS: tuple[Prop, ...] = (
    Prop("task", "title", "title"),
    Prop("task", "status", "select", group="task_status"),
    Prop("task", "next_actor", "select", group="next_actor"),
    Prop("task", "project_id", "relation"),
    Prop("task", "next_step", "rich_text"),
    Prop("task", "waiting_on", "rich_text"),
    Prop("task", "due_date", "date"),
    Prop("task", "follow_up_date", "date"),
    Prop("task", "snooze_until", "date"),
    Prop("task", "focus", "checkbox"),
    Prop("task", "change_note", "rich_text"),
    Prop("task", "delegation", "select", editable=False, group="delegation"),
    Prop("task", "source", "select", editable=False, group="source"),
    Prop("task", "tm_id", "rich_text", editable=False),
    Prop("task", "updated", "last_edited_time", editable=False),
)

PROJECT_PROPS: tuple[Prop, ...] = (
    Prop("project", "title", "title"),
    Prop("project", "status", "select", group="project_status"),
    Prop("project", "domain", "select"),
    Prop("project", "goal", "rich_text"),
    Prop("project", "next_step", "rich_text"),
    Prop("project", "blockers", "rich_text"),
    Prop("project", "recent_activity", "rich_text"),
    Prop("project", "repos", "rich_text"),
    Prop("project", "recent_activity_at", "date", editable=False),
    Prop("project", "tm_id", "rich_text", editable=False),
    Prop("project", "updated", "last_edited_time", editable=False),
)

PROPS = {"task": TASK_PROPS, "project": PROJECT_PROPS}


def prop(kind: str, field: str) -> Prop:
    return next(p for p in PROPS[kind] if p.field == field)


def editable_fields(kind: str) -> list[str]:
    return [p.field for p in PROPS[kind] if p.editable and p.type != "last_edited_time"]


def written_props(kind: str) -> list[Prop]:
    return [p for p in PROPS[kind] if p.type != "last_edited_time"]


# --------------------------------------------------------------------------- schema creation

def _options(group: str, lang: str | None) -> list[dict[str, Any]]:
    seen: dict[str, str] = {}
    for value, name in labels(group, lang).items():
        seen.setdefault(name, COLORS.get(group, {}).get(value, "default"))
    return [{"name": name, "color": color} for name, color in seen.items()]


def data_source_properties(kind: str, projects_data_source_id: str | None = None,
                           lang: str | None = None) -> dict[str, Any]:
    """Property configuration for ``initial_data_source`` when creating a database."""
    result: dict[str, Any] = {}
    for p in PROPS[kind]:
        if p.type == "relation":
            if not projects_data_source_id:
                raise ValueError("the Task relation needs the Projects data source id")
            result[p.name(lang)] = {"relation": {
                "data_source_id": projects_data_source_id, "type": "dual_property",
                "dual_property": {"synced_property_name": i18n.t("notion.relation_back", lang)}}}
        elif p.type == "select":
            result[p.name(lang)] = {"select": {"options": _options(p.group, lang) if p.group else []}}
        else:
            result[p.name(lang)] = {p.type: {}}
    return result


# --------------------------------------------------------------------------- property ids

class PropertyMap:
    """field -> Notion property id, plus the language the Notion structure was created in."""

    def __init__(self, kind: str, ids: dict[str, str] | None = None, lang: str | None = None):
        self.kind = kind
        self.ids = dict(ids or {})
        self.lang = i18n.normalize(lang) or i18n.language()

    @classmethod
    def from_data_source(cls, kind: str, data_source: dict[str, Any], known: dict[str, str] | None = None,
                         lang: str | None = None) -> "PropertyMap":
        by_name = {name: prop["id"] for name, prop in (data_source.get("properties") or {}).items()}
        by_id = set(by_name.values())
        ids: dict[str, str] = {}
        for p in PROPS[kind]:
            if known and known.get(p.field) in by_id:
                ids[p.field] = known[p.field]
                continue
            for name in [p.name(lang), *sorted(p.names())]:
                if name in by_name:
                    ids[p.field] = by_name[name]
                    break
        return cls(kind, ids, lang)

    def key(self, prop: Prop) -> str:
        return self.ids.get(prop.field, prop.name(self.lang))

    def missing(self) -> list[str]:
        return [p.name(self.lang) for p in PROPS[self.kind] if p.field not in self.ids]


# --------------------------------------------------------------------------- values

def _date(value: Any) -> str | None:
    if not value:
        return None
    return str(value)[:10]


def render_value(prop: Prop, value: Any, lang: str | None = None) -> dict[str, Any]:
    if prop.type == "title":
        return {"title": rich_text(value or "")}
    if prop.type == "rich_text":
        return {"rich_text": rich_text(value or "")}
    if prop.type == "select":
        name = (labels(prop.group, lang).get(value, value) if prop.group else value) if value else None
        return {"select": {"name": name} if name else None}
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
        if not prop.group:
            return name, None
        for lang in i18n.LANGUAGES:    # accept option names of any language
            reverse = {v: k for k, v in labels(prop.group, lang).items()}
            if name in reverse:
                return reverse[name], None
        return None, f"unknown option {name!r} for {prop.name()}"
    if kind == "relation":
        rel = raw.get("relation") or []
        problem = f"{prop.name()} has {len(rel)} relations; using the first" if len(rel) > 1 else None
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
        raw = by_id.get(key) if key else props.get(p.name(pmap.lang))
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
        result[pmap.key(p)] = render_value(p, values.get(p.field), pmap.lang)
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
        elif p.type == "select" and not p.group:
            value = value or None
        out[p.field] = value
    return out
