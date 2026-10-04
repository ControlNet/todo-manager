"""Thin Notion REST client (API version 2026-03-11).

* One shared limiter per process: requests are spaced (default 0.4 s, well under the
  180 requests/minute connection budget) and 429/529 wait for ``Retry-After``.
* Reads (GET) are retried on 500/502/503/504. Writes are NOT retried here: a timeout or
  a 5xx after sending means the outcome is unknown (``NotionUncertain``). The caller
  reconciles by reading the object (a 503 may carry ``committed_resource_id``).
* Tokens are only placed in the Authorization header and never logged or returned.
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, Iterator

from .. import config
from ..net import HttpError, HttpTimeout, RateLimiter, request_json

API = "https://api.notion.com/v1"
DEFAULT_VERSION = "2026-03-11"

Transport = Callable[[str, str, dict[str, Any] | None, Any], Any]


class NotionError(Exception):
    def __init__(self, message: str, status: int = 0, code: str | None = None, body: Any = None,
                 retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body
        self.retry_after = retry_after

    @property
    def not_found(self) -> bool:
        return self.status == 404 or self.code == "object_not_found"


class NotionUncertain(NotionError):
    """A write may or may not have been applied. ``committed_id`` is set when Notion says so."""

    def __init__(self, message: str, committed_id: str | None = None, **kw: Any):
        super().__init__(message, **kw)
        self.committed_id = committed_id


class NotionClient:
    def __init__(self, token: str | None = None, *, version: str = DEFAULT_VERSION, min_interval: float = 0.4,
                 transport: Transport | None = None, timeout: float = 60.0, max_retries: int = 5):
        if transport is None and not token:
            raise NotionError("Notion token missing (set notion.token_env in the environment or env file)")
        self._token = token
        self.version = version
        self.limiter = RateLimiter(min_interval)
        self.timeout = timeout
        self.max_retries = max_retries
        self._transport = transport or self._http
        self.requests = 0

    @classmethod
    def from_config(cls, cfg: config.Config, transport: Transport | None = None) -> "NotionClient":
        n = cfg.section("notion")
        token = None if transport else config.read_env_secret(n.get("token_env", "NOTION_API_KEY"), n.get("env_file"))
        return cls(token, version=n.get("api_version", DEFAULT_VERSION),
                   min_interval=float(n.get("min_request_interval_seconds", 0.4)), transport=transport)

    # ------------------------------------------------------------------ transport

    def _http(self, method: str, path: str, params: dict[str, Any] | None, body: Any) -> Any:
        headers = {"Authorization": f"Bearer {self._token}", "Notion-Version": self.version}
        return request_json(method, API + path, headers=headers, params=params, body=body, timeout=self.timeout,
                            limiter=self.limiter, max_429_retries=0)

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None, body: Any = None) -> Any:
        idempotent = method in ("GET", "DELETE")
        attempt = 0
        while True:
            self.requests += 1
            try:
                return self._transport(method, path, params, body)
            except HttpTimeout as exc:
                if idempotent and attempt < self.max_retries:
                    attempt += 1
                    time.sleep(min(30, 2 ** attempt))
                    continue
                if idempotent:
                    raise NotionError(str(exc)) from None
                raise NotionUncertain(f"{method} {path}: {exc}") from None
            except HttpError as exc:
                err = _to_error(exc)
                reason = ((exc.body or {}).get("additional_data") or {}).get("rate_limit_reason") \
                    if isinstance(exc.body, dict) else None
                if exc.status in (429, 529) and reason != "public_api_request_blocked" and attempt < self.max_retries:
                    attempt += 1
                    wait = exc.retry_after if exc.retry_after is not None else min(30, 2 ** attempt)
                    if wait > 120:
                        raise err from None
                    time.sleep(wait + random.random() * 0.25)
                    continue
                if exc.status in (500, 502, 503, 504) or exc.status == 0:
                    if idempotent and attempt < self.max_retries:
                        attempt += 1
                        time.sleep(min(30, 2 ** attempt))
                        continue
                    if not idempotent:
                        data = (exc.body.get("additional_data") or {}) if isinstance(exc.body, dict) else {}
                        raise NotionUncertain(f"{method} {path}: {err}", committed_id=data.get("committed_resource_id"),
                                              status=exc.status, code=err.code, body=exc.body) from None
                raise err from None

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params or None)

    def post(self, path: str, body: Any) -> Any:
        return self.request("POST", path, body=body)

    def patch(self, path: str, body: Any) -> Any:
        return self.request("PATCH", path, body=body)

    def paginate(self, method: str, path: str, *, body: dict[str, Any] | None = None,
                 params: dict[str, Any] | None = None, max_pages: int = 200) -> Iterator[dict[str, Any]]:
        """Yield every result. Raises instead of silently truncating at ``max_pages``."""
        cursor = None
        for _ in range(max_pages):
            if method == "GET":
                p = dict(params or {}, page_size=100)
                if cursor:
                    p["start_cursor"] = cursor
                data = self.request("GET", path, params=p)
            else:
                b = dict(body or {}, page_size=100)
                if cursor:
                    b["start_cursor"] = cursor
                data = self.request(method, path, body=b)
            yield from data.get("results", [])
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")
        raise NotionError(f"pagination exceeded {max_pages} pages for {path}")

    # ------------------------------------------------------------------ endpoints

    def me(self) -> dict[str, Any]:
        return self.get("/users/me")

    def users(self) -> list[dict[str, Any]]:
        return list(self.paginate("GET", "/users"))

    def retrieve_page(self, page_id: str) -> dict[str, Any]:
        return self.get(f"/pages/{page_id}")

    def create_page(self, parent: dict[str, Any], properties: dict[str, Any], markdown: str | None = None,
                    icon: dict[str, Any] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"parent": parent, "properties": properties}
        if markdown:
            body["markdown"] = markdown
        if icon:
            body["icon"] = icon
        return self.post("/pages", body)

    def update_page(self, page_id: str, properties: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
        body: dict[str, Any] = dict(extra)
        if properties is not None:
            body["properties"] = properties
        return self.patch(f"/pages/{page_id}", body)

    def page_markdown(self, page_id: str) -> dict[str, Any]:
        return self.get(f"/pages/{page_id}/markdown")

    def update_markdown(self, page_id: str, command: dict[str, Any]) -> dict[str, Any]:
        return self.patch(f"/pages/{page_id}/markdown", command)

    def query_data_source(self, data_source_id: str, *, filter: dict[str, Any] | None = None,
                          sorts: list[dict[str, Any]] | None = None, max_pages: int = 200) -> list[dict[str, Any]]:
        body: dict[str, Any] = {}
        if filter:
            body["filter"] = filter
        if sorts:
            body["sorts"] = sorts
        return list(self.paginate("POST", f"/data_sources/{data_source_id}/query", body=body, max_pages=max_pages))

    def retrieve_data_source(self, data_source_id: str) -> dict[str, Any]:
        return self.get(f"/data_sources/{data_source_id}")

    def update_data_source(self, data_source_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self.patch(f"/data_sources/{data_source_id}", {"properties": properties})

    def create_database(self, parent_page_id: str, title: str, properties: dict[str, Any],
                        icon: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"parent": {"type": "page_id", "page_id": parent_page_id},
                                "title": [{"type": "text", "text": {"content": title}}], "is_inline": False,
                                "initial_data_source": {"properties": properties}}
        if icon:
            body["icon"] = {"type": "emoji", "emoji": icon}
        return self.post("/databases", body)

    def retrieve_database(self, database_id: str) -> dict[str, Any]:
        return self.get(f"/databases/{database_id}")

    def list_comments(self, block_id: str) -> list[dict[str, Any]]:
        return list(self.paginate("GET", "/comments", params={"block_id": block_id}))

    def create_comment(self, *, markdown: str, page_id: str | None = None, discussion_id: str | None = None,
                       block_id: str | None = None) -> dict[str, Any]:
        targets = [t for t in (page_id, discussion_id, block_id) if t]
        if len(targets) != 1:
            raise NotionError("exactly one comment target is required")
        body: dict[str, Any] = {"markdown": markdown}
        if discussion_id:
            body["discussion_id"] = discussion_id
        elif page_id:
            body["parent"] = {"page_id": page_id}
        else:
            body["parent"] = {"block_id": block_id}
        return self.post("/comments", body)

    def block_children(self, block_id: str) -> list[dict[str, Any]]:
        return list(self.paginate("GET", f"/blocks/{block_id}/children"))

    def create_view(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.post("/views", body)

    def list_views(self, *, database_id: str | None = None, data_source_id: str | None = None) -> list[dict[str, Any]]:
        params = {"database_id": database_id} if database_id else {"data_source_id": data_source_id}
        return list(self.paginate("GET", "/views", params=params))

    def retrieve_view(self, view_id: str) -> dict[str, Any]:
        return self.get(f"/views/{view_id}")

    def update_view(self, view_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.patch(f"/views/{view_id}", body)


def _to_error(exc: HttpError) -> NotionError:
    body = exc.body if isinstance(exc.body, dict) else {}
    return NotionError(f"Notion HTTP {exc.status}: {body.get('message') or exc}", status=exc.status,
                       code=body.get("code"), body=exc.body, retry_after=exc.retry_after)


def plain_text(rich: list[dict[str, Any]] | None) -> str:
    return "".join(part.get("plain_text") or (part.get("text") or {}).get("content", "") for part in rich or [])


def rich_text(text: str | None, limit: int = 2000) -> list[dict[str, Any]]:
    """Split text into rich-text objects within Notion's 2000-character limit (max 100 parts)."""
    if not text:
        return []
    parts = [text[i:i + limit] for i in range(0, len(text), limit)][:100]
    return [{"type": "text", "text": {"content": p}} for p in parts]
