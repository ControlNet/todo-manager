"""TEST DOUBLE: an in-memory imitation of the Notion REST endpoints todo-manager uses.

It mirrors the behaviours the sync relies on (data-source queries filtered/sorted by
minute-precision ``last_edited_time``, property ids, select options created on write,
markdown body search-and-replace, unresolved-comment listing, ``in_trash``) and can
inject faults (errors, lost write responses). It is NOT evidence that the real API
behaves the same; real behaviour is checked separately against a sandbox page.
"""

from __future__ import annotations

import copy
import itertools
import uuid
from typing import Any

from todo_manager import clock
from todo_manager.net import HttpError, HttpTimeout

BOT_ID = "bot-0000"
USER_ID = "user-0001"


def _minute(ts: str) -> str:
    return ts[:16] + ":00.000Z"


class FakeNotion:
    def __init__(self) -> None:
        self.databases: dict[str, dict[str, Any]] = {}
        self.data_sources: dict[str, dict[str, Any]] = {}
        self.pages: dict[str, dict[str, Any]] = {}
        self.markdown: dict[str, str] = {}
        self.comments: list[dict[str, Any]] = []
        self.views: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_next: dict[tuple[str, str], Exception] = {}
        self.lose_response: set[tuple[str, str]] = set()
        self._ids = itertools.count(1)

    # ------------------------------------------------------------------ helpers
    def _id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids):04d}-{uuid.uuid4().hex[:8]}"

    def _touch(self, page: dict[str, Any], who: str) -> None:
        page["last_edited_time"] = _minute(clock.iso())
        page["last_edited_by"] = {"object": "user", "id": who}

    def _prop_id(self, ds: dict[str, Any], key: str) -> str:
        if key in ds["by_id"]:
            return key
        if key in ds["properties"]:
            return ds["properties"][key]["id"]
        raise HttpError(400, f"property {key} does not exist", body={"code": "validation_error",
                                                                  "message": f"{key} is not a property"})

    def _set_props(self, page: dict[str, Any], ds: dict[str, Any], props: dict[str, Any]) -> None:
        for key, value in props.items():
            pid = self._prop_id(ds, key)
            schema = ds["by_id"][pid]
            ptype = schema["type"]
            if ptype == "select" and value.get("select"):
                names = [o["name"] for o in schema["select"]["options"]]
                if value["select"]["name"] not in names:
                    schema["select"]["options"].append({"name": value["select"]["name"], "color": "default"})
            stored = {"id": pid, "type": ptype}
            if ptype in ("title", "rich_text"):
                stored[ptype] = [{"type": "text", "text": {"content": t["text"]["content"]},
                                  "plain_text": t["text"]["content"]} for t in value.get(ptype, [])]
            else:
                stored[ptype] = copy.deepcopy(value.get(ptype))
            page["properties"][schema["name"]] = stored

    def _page_view(self, page: dict[str, Any]) -> dict[str, Any]:
        view = copy.deepcopy(page)
        ds = self.data_sources.get(page["parent"].get("data_source_id", ""))
        if ds:
            for name, schema in ds["properties"].items():
                if schema["type"] == "last_edited_time":
                    view["properties"][name] = {"id": schema["id"], "type": "last_edited_time",
                                                "last_edited_time": page["last_edited_time"]}
        return view

    # ------------------------------------------------------------------ test controls
    def user_edit(self, page_id: str, **props_by_name: Any) -> None:
        page = self.pages[page_id]
        ds = self.data_sources[page["parent"]["data_source_id"]]
        payload = {}
        for name, value in props_by_name.items():
            ptype = ds["properties"][name]["type"]
            if ptype in ("title", "rich_text"):
                payload[name] = {ptype: [{"text": {"content": value}}] if value else []}
            elif ptype == "select":
                payload[name] = {"select": {"name": value} if value else None}
            elif ptype == "date":
                payload[name] = {"date": {"start": value} if value else None}
            elif ptype == "checkbox":
                payload[name] = {"checkbox": bool(value)}
            elif ptype == "relation":
                payload[name] = {"relation": [{"id": v} for v in (value if isinstance(value, list) else [value])]
                                 if value else []}
        self._set_props(page, ds, payload)
        self._touch(page, USER_ID)

    def user_create_page(self, data_source_id: str, title: str, **props: Any) -> str:
        ds = self.data_sources[data_source_id]
        title_prop = next(n for n, s in ds["properties"].items() if s["type"] == "title")
        page = self._new_page({"type": "data_source_id", "data_source_id": data_source_id},
                              {title_prop: {"title": [{"text": {"content": title}}]}}, "", USER_ID)
        if props:
            self.user_edit(page["id"], **props)
        return page["id"]

    def user_comment(self, page_id: str, text: str, discussion_id: str | None = None, author: str = USER_ID) -> str:
        return self._add_comment(page_id, text, discussion_id, author)["id"]

    def user_edit_comment(self, comment_id: str, text: str) -> None:
        for c in self.comments:
            if c["id"] == comment_id:
                c["rich_text"] = [{"type": "text", "text": {"content": text}, "plain_text": text}]
                c["last_edited_time"] = _minute(clock.iso())

    def resolve_discussion(self, discussion_id: str) -> None:
        for c in self.comments:
            if c["discussion_id"] == discussion_id:
                c["resolved"] = True

    def user_set_markdown(self, page_id: str, markdown: str) -> None:
        self.markdown[page_id] = markdown
        self._touch(self.pages[page_id], USER_ID)

    def user_trash(self, page_id: str, trashed: bool = True) -> None:
        self.pages[page_id]["in_trash"] = trashed
        self._touch(self.pages[page_id], USER_ID)

    def prop(self, page_id: str, name: str) -> Any:
        raw = self.pages[page_id]["properties"].get(name)
        if raw is None:
            return None
        t = raw["type"]
        if t in ("title", "rich_text"):
            return "".join(x["plain_text"] for x in raw[t]) or None
        if t == "select":
            return raw["select"]["name"] if raw["select"] else None
        if t == "date":
            return raw["date"]["start"] if raw["date"] else None
        if t == "relation":
            return [r["id"] for r in raw["relation"]]
        return raw.get(t)

    def pages_in(self, data_source_id: str) -> list[dict[str, Any]]:
        return [p for p in self.pages.values() if p["parent"].get("data_source_id") == data_source_id]

    # ------------------------------------------------------------------ objects
    def _new_page(self, parent: dict[str, Any], props: dict[str, Any], markdown: str, who: str) -> dict[str, Any]:
        pid = self._id("page")
        page = {"object": "page", "id": pid, "parent": parent, "properties": {}, "in_trash": False,
                "url": f"https://app.notion.com/p/{pid.replace('-', '')}", "created_time": clock.iso()}
        if parent.get("type") == "data_source_id":
            ds = self.data_sources[parent["data_source_id"]]
            for name, schema in ds["properties"].items():
                if schema["type"] in ("title", "rich_text"):
                    page["properties"][name] = {"id": schema["id"], "type": schema["type"], schema["type"]: []}
                elif schema["type"] == "checkbox":
                    page["properties"][name] = {"id": schema["id"], "type": "checkbox", "checkbox": False}
                elif schema["type"] == "relation":
                    page["properties"][name] = {"id": schema["id"], "type": "relation", "relation": []}
                elif schema["type"] != "last_edited_time":
                    page["properties"][name] = {"id": schema["id"], "type": schema["type"], schema["type"]: None}
            self._set_props(page, ds, props)
        self._touch(page, who)
        self.pages[pid] = page
        self.markdown[pid] = markdown or ""
        return page

    def _add_comment(self, page_id: str, text: str, discussion_id: str | None, author: str) -> dict[str, Any]:
        if page_id not in self.pages:
            raise HttpError(404, "not found", body={"code": "object_not_found", "message": "not found"})
        comment = {"object": "comment", "id": self._id("comment"), "parent": {"type": "page_id", "page_id": page_id},
                   "discussion_id": discussion_id or self._id("disc"), "created_time": clock.iso(),
                   "last_edited_time": _minute(clock.iso()), "created_by": {"object": "user", "id": author},
                   "rich_text": [{"type": "text", "text": {"content": text}, "plain_text": text}], "resolved": False}
        self.comments.append(comment)
        return comment

    # ------------------------------------------------------------------ transport
    def __call__(self, method: str, path: str, params: dict[str, Any] | None, body: Any) -> Any:
        self.calls.append((method, path))
        for (m, prefix), exc in list(self.fail_next.items()):
            if m == method and path.startswith(prefix):
                del self.fail_next[(m, prefix)]
                raise exc
        result = self._dispatch(method, path, params or {}, copy.deepcopy(body))
        for (m, prefix) in list(self.lose_response):
            if m == method and path.startswith(prefix):
                self.lose_response.discard((m, prefix))
                raise HttpTimeout(f"timeout: {method} {path}")
        return result

    def _dispatch(self, method: str, path: str, params: dict[str, Any], body: Any) -> Any:
        parts = path.strip("/").split("/")
        if parts == ["users", "me"]:
            return {"object": "user", "id": BOT_ID, "type": "bot", "name": "Fake bot",
                    "bot": {"workspace_name": "Test"}}
        if parts == ["users"]:
            return {"results": [{"object": "user", "id": USER_ID, "type": "person", "name": "Me"},
                                {"object": "user", "id": BOT_ID, "type": "bot", "name": "Fake bot"}],
                    "has_more": False, "next_cursor": None}
        if parts == ["databases"] and method == "POST":
            return self._create_database(body)
        if parts[0] == "databases" and method == "GET":
            return copy.deepcopy(self.databases[parts[1]])
        if parts[0] == "data_sources":
            ds = self.data_sources.get(parts[1])
            if ds is None:
                raise HttpError(404, "not found", body={"code": "object_not_found", "message": "not found"})
            if len(parts) == 3 and parts[2] == "query":
                return self._query(ds, body)
            if method == "PATCH":
                for name, conf in body["properties"].items():
                    self._add_ds_property(ds, name, conf)
            return {"object": "data_source", "id": ds["id"], "properties": copy.deepcopy(ds["properties"])}
        if parts == ["pages"] and method == "POST":
            page = self._new_page(body["parent"], body.get("properties", {}), body.get("markdown", ""), BOT_ID)
            return self._page_view(page)
        if parts[0] == "pages":
            page = self.pages.get(parts[1])
            if page is None:
                raise HttpError(404, "not found", body={"code": "object_not_found", "message": "not found"})
            if len(parts) == 3 and parts[2] == "markdown":
                return self._markdown(page, method, body)
            if method == "PATCH":
                if page["in_trash"] and "in_trash" not in body:
                    raise HttpError(400, "archived", body={"code": "validation_error",
                                                           "message": "Can't edit block that is archived."})
                if body.get("properties"):
                    ds = self.data_sources[page["parent"]["data_source_id"]]
                    self._set_props(page, ds, body["properties"])
                if "in_trash" in body:
                    page["in_trash"] = body["in_trash"]
                self._touch(page, BOT_ID)
            return self._page_view(page)
        if parts == ["comments"] and method == "GET":
            block = params["block_id"]
            if block not in self.pages:
                raise HttpError(404, "not found", body={"code": "object_not_found", "message": "not found"})
            items = [copy.deepcopy(c) for c in self.comments
                     if c["parent"].get("page_id") == block and not c.get("resolved")]
            return self._paged(items, params)
        if parts == ["comments"] and method == "POST":
            page_id = (body.get("parent") or {}).get("page_id")
            if body.get("discussion_id"):
                page_id = next(c["parent"]["page_id"] for c in self.comments if c["discussion_id"] == body["discussion_id"])
            return copy.deepcopy(self._add_comment(page_id, body["markdown"], body.get("discussion_id"), BOT_ID))
        if parts[0] == "blocks" and parts[2] == "children":
            return {"results": [], "has_more": False, "next_cursor": None}
        if parts == ["views"] and method == "POST":
            vid = self._id("view")
            self.views[vid] = {"object": "view", "id": vid, **body}
            return copy.deepcopy(self.views[vid])
        raise HttpError(400, f"fake: unsupported {method} {path}", body={"code": "validation_error"})

    def _paged(self, items: list[Any], params: dict[str, Any]) -> dict[str, Any]:
        size = int(params.get("page_size", 100))
        start = int(params.get("start_cursor") or 0)
        chunk = items[start:start + size]
        more = start + size < len(items)
        return {"results": chunk, "has_more": more, "next_cursor": str(start + size) if more else None}

    def _add_ds_property(self, ds: dict[str, Any], name: str, conf: dict[str, Any]) -> None:
        ptype = next(k for k in conf if k not in ("type", "name"))
        pid = self._id("prop")[:12]
        schema = {"id": pid, "name": name, "type": ptype, ptype: copy.deepcopy(conf[ptype])}
        if ptype == "select":
            schema["select"] = {"options": copy.deepcopy(conf["select"].get("options", []))}
        ds["properties"][name] = schema
        ds["by_id"][pid] = schema
        if ptype == "relation" and conf["relation"].get("type") == "dual_property":
            target = self.data_sources[conf["relation"]["data_source_id"]]
            synced = conf["relation"]["dual_property"].get("synced_property_name", "Related")
            if synced not in target["properties"]:
                self._add_ds_property(target, synced, {"relation": {"data_source_id": ds["id"],
                                                                     "type": "single_property",
                                                                     "single_property": {}}})

    def _create_database(self, body: dict[str, Any]) -> dict[str, Any]:
        db_id = self._id("db")
        ds_id = self._id("ds")
        title = body["title"][0]["text"]["content"]
        ds = {"id": ds_id, "name": title, "properties": {}, "by_id": {}}
        self.data_sources[ds_id] = ds
        for name, conf in (body.get("initial_data_source") or {}).get("properties", {}).items():
            self._add_ds_property(ds, name, conf)
        self.databases[db_id] = {"object": "database", "id": db_id, "title": body["title"],
                                 "parent": body["parent"], "data_sources": [{"id": ds_id, "name": title}]}
        return copy.deepcopy(self.databases[db_id])

    def _query(self, ds: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        pages = [p for p in self.pages.values()
                 if p["parent"].get("data_source_id") == ds["id"] and not p["in_trash"]]
        flt = body.get("filter")
        if flt:
            pages = [p for p in pages if self._match(p, ds, flt)]
        for sort in body.get("sorts") or []:
            if sort.get("timestamp") == "last_edited_time":
                pages.sort(key=lambda p: p["last_edited_time"], reverse=sort.get("direction") == "descending")
        return self._paged([self._page_view(p) for p in pages], body)

    def _match(self, page: dict[str, Any], ds: dict[str, Any], flt: dict[str, Any]) -> bool:
        if "and" in flt:
            return all(self._match(page, ds, f) for f in flt["and"])
        if "or" in flt:
            return any(self._match(page, ds, f) for f in flt["or"])
        if flt.get("timestamp") == "last_edited_time":
            cond = flt["last_edited_time"]
            return page["last_edited_time"] >= _minute(cond["on_or_after"]) if "on_or_after" in cond else True
        pid = self._prop_id(ds, flt["property"])
        name = ds["by_id"][pid]["name"]
        if "rich_text" in flt:
            value = "".join(x["plain_text"] for x in page["properties"][name]["rich_text"])
            return value == flt["rich_text"].get("equals")
        raise HttpError(400, "fake: unsupported filter", body={"code": "validation_error"})

    def _markdown(self, page: dict[str, Any], method: str, body: dict[str, Any]) -> dict[str, Any]:
        pid = page["id"]
        if method == "PATCH":
            current = self.markdown[pid]
            kind = body["type"]
            if kind == "update_content":
                for op in body["update_content"]["content_updates"]:
                    count = current.count(op["old_str"])
                    if count != 1 and not op.get("replace_all_matches"):
                        raise HttpError(400, "old_str must match exactly once",
                                        body={"code": "validation_error", "message": f"matches={count}"})
                    current = current.replace(op["old_str"], op["new_str"])
            elif kind == "insert_content":
                content = body["insert_content"]["content"]
                if (body["insert_content"].get("position") or {}).get("type") == "start":
                    current = content + ("\n" + current if current else "")
                else:
                    current = (current + "\n" if current else "") + content
            elif kind == "replace_content":
                current = body["replace_content"]["new_str"]
            self.markdown[pid] = current
            self._touch(page, BOT_ID)
        return {"object": "page_markdown", "id": pid, "markdown": self.markdown[pid], "truncated": False,
                "unknown_block_ids": []}
