"""TEST DOUBLE: an in-memory imitation of the Notion REST endpoints todo-manager uses.

It mirrors the behaviours the sync relies on (data-source queries filtered/sorted by
``last_edited_time``, which this double rounds to the minute so tests can force two edits to
share a timestamp; no precision of the real API is assumed), property ids, select options created on write,
markdown body search-and-replace, unresolved-comment listing, ``in_trash``) and can
inject faults (errors, lost write responses). It is NOT evidence that the real API
behaves the same; real behaviour is checked separately against a sandbox page.

Every change also records the webhook event Notion would send (``events``; ``deliveries``
signs them), shaped after the documented payloads: ``entity``, ``authors`` (person or bot),
``data.page_id`` for comments. Aggregation and delivery delays of the real service are not
imitated.
"""

from __future__ import annotations

import copy
import itertools
import json
import re
import uuid
from typing import Any

from todo_manager import clock, webhook
from todo_manager.net import HttpError, HttpTimeout

BOT_ID = "bot-0000"
USER_ID = "user-0001"


def _minute(ts: str) -> str:
    return ts[:16] + ":00.000Z"


# Seen on the production workspace (2026-10-11): Notion stores a bare URL as a link that ends
# only at white space (CJK text after it becomes part of the link) and a bare domain as a link
# to http://domain; the markdown write's response does not show it, later reads do.
_AUTOLINK = re.compile(r"(?P<link>(?<!\\)\[(?:\\.|[^\]\\])*\]\([^)\s]*\))"
                       r"|(?P<url>https?://[^\s<>\]]+)"
                       r"|(?<![\w./@-])(?P<domain>[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:edu|com|org|net|io|au))(?![\w-])")


def autolinked(markdown: str) -> str:
    def link(m: re.Match) -> str:
        if m.group("url"):
            return f"[{m.group('url')}]({m.group('url')})"
        if m.group("domain"):
            return f"[{m.group('domain')}](http://{m.group('domain')})"
        return m.group(0)
    return _AUTOLINK.sub(link, markdown)


class FakeNotion:
    def __init__(self) -> None:
        self.databases: dict[str, dict[str, Any]] = {}
        self.data_sources: dict[str, dict[str, Any]] = {}
        self.pages: dict[str, dict[str, Any]] = {}
        self.markdown: dict[str, str] = {}
        self.comments: list[dict[str, Any]] = []
        self.views: dict[str, dict[str, Any]] = {}
        self.blocks: dict[str, list[dict[str, Any]]] = {}   # page id -> top-level blocks, in order
        self.calls: list[tuple[str, str]] = []
        self.fail_next: dict[tuple[str, str], Exception] = {}
        self.precise_times = False   # True: page edit times keep their seconds (default: rounded to the minute)
        self.truncate_markdown_at: int | None = None   # imitate the API cutting a large body (``truncated``)
        self.autolink = False   # True: store bare URLs and domains as links, as Notion does (see autolinked)
        self.lose_response: set[tuple[str, str]] = set()
        # Imitate Notion creating a board with its own grouping (the Status select, empty groups
        # hidden) instead of the requested one, as it did on 2026-10-12; a PATCH applies it.
        self.default_grouping: dict[str, Any] | None = None
        self.uploads: dict[str, dict[str, Any]] = {}   # file upload id -> {filename, content_type, data, status}
        self.max_upload_bytes: int | None = None        # bot workspace limit (None: not reported)
        self.people: list[dict[str, Any]] = [{"object": "user", "id": USER_ID, "type": "person", "name": "Me"}]
        self.events: list[dict[str, Any]] = []   # webhook events, oldest first
        self._ids = itertools.count(1)

    # ------------------------------------------------------------------ helpers
    def _id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids):04d}-{uuid.uuid4().hex[:8]}"

    def block(self, kind: str, text: str = "", id: str | None = None) -> dict[str, Any]:
        """A top-level page block; ``child_database`` blocks share their database's id."""
        data = {"title": text} if kind == "child_database" else {
            "rich_text": [{"type": "text", "text": {"content": text}, "plain_text": text}]}
        return {"object": "block", "id": id or self._id("block"), "type": kind, kind: data}

    @staticmethod
    def text(block: dict[str, Any]) -> str:
        return "".join(t.get("plain_text") or t.get("text", {}).get("content", "")
                       for t in block[block["type"]].get("rich_text", []))

    def _insert_blocks(self, page_id: str, new: list[dict[str, Any]], after: str | None, kind: str = "end") -> None:
        blocks = self.blocks.setdefault(page_id, [])
        if kind == "start":
            at = 0
        elif kind == "after_block":
            at = next(i for i, b in enumerate(blocks) if b["id"] == after) + 1
        else:
            at = len(blocks)
        blocks[at:at] = new

    def _touch(self, page: dict[str, Any], who: str, event: str = "page.properties_updated") -> None:
        page["last_edited_time"] = clock.iso() if self.precise_times else _minute(clock.iso())
        page["last_edited_by"] = {"object": "user", "id": who}
        self._event(event, page["id"], "page", who, {"parent": copy.deepcopy(page["parent"])})

    def _event(self, kind: str, entity_id: str, entity_type: str, who: str, data: dict[str, Any]) -> None:
        self.events.append({"id": str(uuid.uuid4()), "timestamp": clock.iso(), "workspace_id": "ws-fake",
                            "subscription_id": "sub-fake", "integration_id": "int-fake", "type": kind,
                            "authors": [{"id": who, "type": "bot" if who == BOT_ID else "person"}],
                            "entity": {"id": entity_id, "type": entity_type}, "data": data, "attempt_number": 1})

    def deliveries(self, token: str, since: int = 0) -> list[tuple[bytes, dict[str, str]]]:
        """The webhook requests for ``events[since:]``: raw body and headers, signed with ``token``."""
        out = []
        for event in self.events[since:]:
            body = json.dumps(event).encode()
            out.append((body, {"Content-Type": "application/json", webhook.SIGNATURE_HEADER: webhook.sign(token, body)}))
        return out

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
    def user_set_icon(self, page_id: str, icon: dict[str, Any] | None, author: str = USER_ID) -> None:
        self.pages[page_id]["icon"] = copy.deepcopy(icon)
        self._touch(self.pages[page_id], author)

    def user_edit(self, page_id: str, *, author: str = USER_ID, **props_by_name: Any) -> None:
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
        self._touch(page, author)

    def user_create_page(self, data_source_id: str, title: str, *, author: str = USER_ID, markdown: str = "",
                         **props: Any) -> str:
        ds = self.data_sources[data_source_id]
        title_prop = next(n for n, s in ds["properties"].items() if s["type"] == "title")
        page = self._new_page({"type": "data_source_id", "data_source_id": data_source_id},
                              {title_prop: {"title": [{"text": {"content": title}}]}}, markdown, author)
        if props:
            self.user_edit(page["id"], author=author, **props)
        return page["id"]

    def user_comment(self, page_id: str, text: str, discussion_id: str | None = None, author: str = USER_ID) -> str:
        return self._add_comment(page_id, text, discussion_id, author)["id"]

    def user_edit_comment(self, comment_id: str, text: str) -> None:
        for c in self.comments:
            if c["id"] == comment_id:
                c["rich_text"] = [{"type": "text", "text": {"content": text}, "plain_text": text}]
                c["last_edited_time"] = _minute(clock.iso())
                self._event("comment.updated", c["id"], "comment", USER_ID, self._comment_data(c))

    def resolve_discussion(self, discussion_id: str, resolved: bool = True) -> None:
        for c in self.comments:
            if c["discussion_id"] == discussion_id:
                c["resolved"] = resolved

    def user_delete_comment(self, comment_id: str) -> None:
        """ASSUMPTION (not verified against the real API): a deleted comment disappears from the
        list and ``GET /v1/comments/{id}`` answers 404; a resolved one is unlisted but retrievable."""
        for c in self.comments:
            if c["id"] == comment_id:
                self._event("comment.deleted", c["id"], "comment", USER_ID, self._comment_data(c))
        self.comments = [c for c in self.comments if c["id"] != comment_id]

    def user_set_markdown(self, page_id: str, markdown: str, author: str = USER_ID) -> None:
        self.markdown[page_id] = markdown
        self._touch(self.pages[page_id], author, "page.content_updated")

    def user_trash(self, page_id: str, trashed: bool = True, author: str = USER_ID) -> None:
        self.pages[page_id]["in_trash"] = trashed
        self._touch(self.pages[page_id], author, "page.deleted" if trashed else "page.undeleted")

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

    def user_top_level_page(self, title: str, markdown: str = "", author: str = USER_ID) -> str:
        """A workspace-level page the user made and shared with the integration."""
        page = self._new_page({"type": "workspace", "workspace": True}, {}, markdown, author)
        page["properties"]["title"] = {"id": "title", "type": "title",
                                       "title": [{"type": "text", "text": {"content": title}, "plain_text": title}]}
        return page["id"]

    def pages_in(self, data_source_id: str) -> list[dict[str, Any]]:
        return [p for p in self.pages.values() if p["parent"].get("data_source_id") == data_source_id]

    # ------------------------------------------------------------------ objects
    def _new_page(self, parent: dict[str, Any], props: dict[str, Any], markdown: str, who: str) -> dict[str, Any]:
        pid = self._id("page")
        page = {"object": "page", "id": pid, "parent": parent, "properties": {}, "in_trash": False,
                "url": f"https://app.notion.com/p/{pid.replace('-', '')}", "created_time": clock.iso(),
                "created_by": {"object": "user", "id": who}}
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
        self.pages[pid] = page
        self._touch(page, who, "page.created")
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
        self._event("comment.created", comment["id"], "comment", author, self._comment_data(comment))
        return comment

    @staticmethod
    def _comment_data(comment: dict[str, Any]) -> dict[str, Any]:
        page_id = comment["parent"]["page_id"]
        return {"page_id": page_id, "parent": {"id": page_id, "type": "page"}}

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
            bot: dict[str, Any] = {"workspace_name": "Test"}
            if self.max_upload_bytes is not None:
                bot["workspace_limits"] = {"max_file_upload_size_in_bytes": self.max_upload_bytes}
            return {"object": "user", "id": BOT_ID, "type": "bot", "name": "Fake bot", "bot": bot}
        if parts == ["file_uploads"] and method == "POST":
            uid = self._id("upload")
            self.uploads[uid] = {"id": uid, "filename": body["filename"], "content_type": body["content_type"],
                                 "status": "pending"}
            return {"object": "file_upload", **copy.deepcopy(self.uploads[uid])}
        if parts[0] == "file_uploads" and len(parts) == 3 and parts[2] == "send" and method == "POST":
            upload = self.uploads[parts[1]]
            data = body.data
            if self.max_upload_bytes is not None and len(data) > self.max_upload_bytes + 1024:
                raise HttpError(400, "file too large", body={"code": "validation_error"})
            if b"Content-Type: " + upload["content_type"].encode() not in data:
                raise HttpError(400, "content type mismatch", body={"code": "validation_error"})
            upload.update(status="uploaded", data=data)
            return {"object": "file_upload", **{k: v for k, v in upload.items() if k != "data"}}
        if parts == ["users"]:
            return {"results": copy.deepcopy(self.people) + [{"object": "user", "id": BOT_ID, "type": "bot",
                                                              "name": "Fake bot"}],
                    "has_more": False, "next_cursor": None}
        if parts == ["search"] and method == "POST":
            kinds = {(body.get("filter") or {}).get("value")} - {None}
            pages = [self._page_view(p) for p in self.pages.values() if not kinds or "page" in kinds]
            return {"results": pages, "has_more": False, "next_cursor": None}
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
                    existing = ds["properties"].get(name) or ds["by_id"].get(name)
                    if existing and "select" in conf:
                        self._set_options(ds, existing, conf["select"].get("options", []))
                    else:
                        self._add_ds_property(ds, name, conf)
            return {"object": "data_source", "id": ds["id"], "properties": copy.deepcopy(ds["properties"])}
        if parts == ["pages"] and method == "POST":
            page = self._new_page(body["parent"], body.get("properties", {}), body.get("markdown", ""), BOT_ID)
            if body["parent"].get("type") == "page_id":
                title = "".join(t["text"]["content"] for t in (body.get("properties", {}).get("title") or {}).get("title", []))
                self.blocks.setdefault(body["parent"]["page_id"], []).append(
                    {"object": "block", "id": page["id"], "type": "child_page", "child_page": {"title": title}})
                # A child page's top-level blocks, roughly as Notion parses its Markdown.
                self.blocks[page["id"]] = [
                    self.block(f"heading_{len(m.group(1))}", m.group(2)) if m else self.block("paragraph", line)
                    for line in (body.get("markdown") or "").splitlines() if line.strip()
                    for m in [re.match(r"(#{1,3}) (.*)", line)]]
            if body.get("icon"):
                page["icon"] = copy.deepcopy(body["icon"])
            if self.autolink:
                self.markdown[page["id"]] = autolinked(self.markdown[page["id"]])
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
                if "icon" in body:
                    page["icon"] = copy.deepcopy(body["icon"])
                self._touch(page, BOT_ID)
            return self._page_view(page)
        if parts == ["comments"] and method == "GET":
            block = params["block_id"]
            if block not in self.pages:
                raise HttpError(404, "not found", body={"code": "object_not_found", "message": "not found"})
            items = [copy.deepcopy(c) for c in self.comments
                     if c["parent"].get("page_id") == block and not c.get("resolved")]
            return self._paged(items, params)
        if len(parts) == 2 and parts[0] == "comments" and method == "GET":
            found = [c for c in self.comments if c["id"] == parts[1]]
            if not found:
                raise HttpError(404, "not found", body={"code": "object_not_found", "message": "not found"})
            return {k: v for k, v in copy.deepcopy(found[0]).items() if k != "resolved"}
        if parts == ["comments"] and method == "POST":
            page_id = (body.get("parent") or {}).get("page_id")
            if body.get("discussion_id"):
                page_id = next(c["parent"]["page_id"] for c in self.comments if c["discussion_id"] == body["discussion_id"])
            return copy.deepcopy(self._add_comment(page_id, body["markdown"], body.get("discussion_id"), BOT_ID))
        if parts[0] == "blocks" and len(parts) == 3 and parts[2] == "children" and method == "PATCH":
            new = [self.block(c["type"], self.text(c) if "rich_text" in c[c["type"]] else "")
                   for c in body["children"]]
            for block, child in zip(new, body["children"]):
                upload = (child[child["type"]].get("file_upload") or {}).get("id")
                if upload:
                    if (self.uploads.get(upload) or {}).get("status") != "uploaded":
                        raise HttpError(400, "file upload not uploaded", body={"code": "validation_error"})
                    self.uploads[upload]["status"] = "attached"
                    block[child["type"]] = copy.deepcopy(child[child["type"]])
            position = body.get("position") or {"type": "end"}
            if position["type"] == "after_block" and "id" not in (position.get("after_block") or {}):
                # The real API: append-children positions are {type, after_block: {id}}, unlike views.
                raise HttpError(400, "body.position.after_block should be defined", body={"code": "validation_error"})
            self._insert_blocks(parts[1], new, (position.get("after_block") or {}).get("id"), position["type"])
            return {"results": copy.deepcopy(new), "has_more": False, "next_cursor": None}
        if parts[0] == "blocks" and len(parts) == 3 and parts[2] == "children":
            return self._paged(copy.deepcopy(self.blocks.get(parts[1], [])), params)
        if parts[0] == "blocks" and len(parts) == 2 and method in ("PATCH", "DELETE"):
            for page_id, blocks in self.blocks.items():
                found = next((b for b in blocks if b["id"] == parts[1]), None)
                if found is None:
                    continue
                if method == "DELETE":     # to the trash: no longer among the page's children
                    blocks.remove(found)
                    return {**copy.deepcopy(found), "in_trash": True}
                kind = found["type"]
                if kind in body and "rich_text" in body[kind]:
                    text = "".join(t.get("text", {}).get("content", "") for t in body[kind]["rich_text"])
                    found[kind]["rich_text"] = [{"type": "text", "text": {"content": text}, "plain_text": text}]
                return copy.deepcopy(found)
            raise HttpError(404, "not found", body={"code": "object_not_found", "message": "not found"})
        if parts == ["views"] and method == "GET":
            views = [v for v in self.views.values() if
                     (not params.get('data_source_id') or v.get('data_source_id') == params['data_source_id']) and
                     (not params.get('database_id') or (v.get('parent') or {}).get('database_id') == params['database_id'])]
            return self._paged([{'object':'view', 'id':v['id']} for v in views], params)
        if parts == ["views"] and method == "POST":
            position = (body.get('create_database') or {}).get('position')
            if position and position.get('type') != 'after_block':
                raise HttpError(400, 'linked database position must be after_block', body={'code':'validation_error'})
            vid = self._id("view")
            self.views[vid] = {"object": "view", "id": vid, **copy.deepcopy(body),
                               'parent': {'type': 'database_id', 'database_id': body.get('database_id') or self._id('linked-db')}}
            if self.default_grouping is not None and body.get('type') == 'board':
                self.views[vid]['configuration']['group_by'] = copy.deepcopy(self.default_grouping)
            if body.get('create_database') and body['create_database']['parent']['page_id'] in self.blocks:
                self._insert_blocks(body['create_database']['parent']['page_id'],
                                    [self.block('child_database', id=self.views[vid]['parent']['database_id'])],
                                    (position or {}).get('block_id'), (position or {}).get('type', 'end'))
            return copy.deepcopy(self.views[vid])
        if parts[0] == "views" and len(parts) == 2 and method in ("GET", "PATCH"):
            if parts[1] not in self.views:
                raise HttpError(404, "not found", body={"code": "object_not_found", "message": "not found"})
            if method == "PATCH":
                self.views[parts[1]].update(copy.deepcopy(body))
            return copy.deepcopy(self.views[parts[1]])
        raise HttpError(400, f"fake: unsupported {method} {path}", body={"code": "validation_error"})

    def _paged(self, items: list[Any], params: dict[str, Any]) -> dict[str, Any]:
        size = int(params.get("page_size", 100))
        start = int(params.get("start_cursor") or 0)
        chunk = items[start:start + size]
        more = start + size < len(items)
        return {"results": chunk, "has_more": more, "next_cursor": str(start + size) if more else None}

    def _set_options(self, ds: dict[str, Any], prop: dict[str, Any], options: list[dict[str, Any]]) -> None:
        """Notion keeps the options listed (by name; an existing one keeps its colour), adds new
        ones and removes the rest; pages lose a removed option."""
        old = {o["name"]: o for o in prop["select"]["options"]}
        prop["select"]["options"] = [copy.deepcopy(old.get(o["name"], o)) for o in options]
        kept = {o["name"] for o in options}
        for page in self.pages.values():
            if page["parent"].get("data_source_id") != ds["id"]:
                continue
            value = page["properties"].get(prop["name"], {}).get("select")
            if value and value.get("name") not in kept:
                page["properties"][prop["name"]]["select"] = None

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
            self.markdown[pid] = autolinked(current) if self.autolink else current
            self._touch(page, BOT_ID, "page.content_updated")
            text = current                    # the response shows the content as sent
        else:
            text = self.markdown[pid]
        cut = self.truncate_markdown_at
        return {"object": "page_markdown", "id": pid, "markdown": text if cut is None else text[:cut],
                "truncated": cut is not None and len(text) > cut, "unknown_block_ids": []}
