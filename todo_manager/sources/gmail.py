"""Gmail adapter (REST API, read-only).

* Incremental via ``users.history.list`` (messageAdded) from the stored historyId, all pages.
* Changed threads are fetched in full (``format=full``) so judgement sees the whole thread,
  including the user's own sent replies (commitments / reply evidence).
* Label/read-state changes create no items: "read" or "archived" is never completion and
  never a new task signal.
* If the stored historyId has expired, falls back to a paginated ``messages.list`` over the
  window since the last success minus an overlap, capturing the profile historyId first.
* Large backlogs are worked through a persisted thread queue; nothing is silently capped.
* GitHub notification mails are keyed to the same matter as the GitHub API source
  (``github:owner/repo#n``) and superseded when that repo is tracked through the API.

Access tokens come from an existing local helper command (``token_command``), e.g. the
Hermes ``himalaya-google-token`` script; no new credentials are created or stored.
"""

from __future__ import annotations

import base64
import html
import json
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from .. import clock
from ..net import HttpError, HttpTimeout, request_json
from .base import FetchResult, Item, SourceError

API = "https://gmail.googleapis.com/gmail/v1/users/me"
GITHUB_MSGID = re.compile(r"<([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(?:issues|pull)/(\d+)", re.I)
MAX_BODY_CHARS = 20000
EXCLUDED_LABELS = {"SPAM", "TRASH", "DRAFT", "CHAT"}
BULK_LABELS = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style", "head"):
            self._skip += 1
        if tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    parser = _TextExtractor()
    parser.feed(raw)
    text = html.unescape("".join(parser.parts))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _b64(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode()).decode("utf-8", errors="replace")


def extract_body(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    plain: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict[str, Any]] = []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        if part.get("filename"):
            attachments.append({"filename": part["filename"], "mime": mime, "size": body.get("size", 0),
                                "attachment_id": body.get("attachmentId")})
        elif mime == "text/plain" and body.get("data"):
            plain.append(_b64(body["data"]))
        elif mime == "text/html" and body.get("data"):
            html_parts.append(_b64(body["data"]))
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    text = "\n".join(plain).strip() or html_to_text("\n".join(html_parts))
    return text[:MAX_BODY_CHARS], attachments


class TokenProvider:
    def __init__(self, command: list[str]):
        self.command = [str(Path(c).expanduser()) if c.startswith("~") else c for c in command]
        self._token: str | None = None
        self._at = 0.0

    def __call__(self) -> str:
        if self._token and time.monotonic() - self._at < 45 * 60:
            return self._token
        try:
            out = subprocess.run(self.command, capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SourceError(f"token helper failed: {type(exc).__name__}") from None
        if out.returncode != 0 or not out.stdout.strip():
            raise SourceError("token helper failed (exit %s)" % out.returncode)
        self._token = out.stdout.strip().splitlines()[-1].strip()
        self._at = time.monotonic()
        return self._token


class GmailSource:
    name = "gmail"

    def __init__(self, *, account: str, address: str, token: Callable[[], str], my_addresses: list[str],
                 query: str = "-in:spam -in:trash -in:chats", page_size: int = 100,
                 overlap_minutes: int = 120, initial_lookback_days: int = 14, max_threads_per_run: int = 150,
                 github_api_repos: set[str] | None = None, http: Callable[..., Any] = request_json):
        self.account = account
        self.address = address
        self.token = token
        self.me = {a.lower() for a in my_addresses} | {address.lower()}
        self.query = query
        self.page_size = page_size
        self.overlap = overlap_minutes
        self.lookback_days = initial_lookback_days
        self.max_threads = max_threads_per_run
        self.github_api_repos = {r.lower() for r in (github_api_repos or set())}
        self.http = http

    @property
    def scope(self) -> str:
        return f"mailbox:{self.account}"

    # ------------------------------------------------------------------ API helpers
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            return self.http("GET", f"{API}/{path}", headers={"Authorization": f"Bearer {self.token()}"},
                             params=params, timeout=45)
        except HttpError as exc:
            if exc.status == 404 and path.startswith("history"):
                raise SourceError("historyId expired", cursor_invalid=True) from None
            raise SourceError(str(exc), retry_after=exc.retry_after) from None
        except HttpTimeout as exc:
            raise SourceError(str(exc)) from None

    def _list_history(self, start: str) -> tuple[list[str], str]:
        threads: list[str] = []
        seen: set[str] = set()
        page = None
        latest = start
        while True:
            data = self._get("history", {"startHistoryId": start, "historyTypes": "messageAdded",
                                         "maxResults": 500, "pageToken": page})
            latest = data.get("historyId", latest)
            for record in data.get("history", []) or []:
                for added in record.get("messagesAdded", []) or []:
                    msg = added.get("message", {})
                    if EXCLUDED_LABELS & set(msg.get("labelIds", []) or []):
                        continue
                    tid = msg.get("threadId")
                    if tid and tid not in seen:
                        seen.add(tid)
                        threads.append(tid)
            page = data.get("nextPageToken")
            if not page:
                return threads, str(latest)

    def _list_window(self, after: datetime) -> list[str]:
        threads: list[str] = []
        seen: set[str] = set()
        page = None
        q = f"after:{int(after.timestamp())} {self.query}".strip()
        while True:
            data = self._get("messages", {"q": q, "maxResults": self.page_size, "pageToken": page,
                                          "includeSpamTrash": "false"})
            for msg in data.get("messages", []) or []:
                tid = msg.get("threadId")
                if tid and tid not in seen:
                    seen.add(tid)
                    threads.append(tid)
            page = data.get("nextPageToken")
            if not page:
                return threads

    # ------------------------------------------------------------------ fetch
    def fetch(self, cursor: str | None, last_success_at: str | None = None) -> FetchResult:
        state = json.loads(cursor) if cursor else {}
        queue: list[str] = list(state.get("queue", []))
        target = state.get("queue_history_id")
        history_id = state.get("history_id")
        # Messages older than the system's first window are thread context, not new work.
        floor = state.get("since") or clock.iso(clock.now() - timedelta(days=self.lookback_days))
        meta: dict[str, Any] = {}
        if not queue:
            if history_id:
                try:
                    queue, target = self._list_history(history_id)
                    meta["mode"] = "history"
                except SourceError as exc:
                    if not exc.cursor_invalid:
                        raise
                    since = clock.parse(last_success_at) or clock.now() - timedelta(days=self.lookback_days)
                    target = str(self._get("profile").get("historyId"))
                    queue = self._list_window(since - timedelta(minutes=self.overlap))
                    meta["mode"] = "window_fallback"
            else:
                target = str(self._get("profile").get("historyId"))
                queue = self._list_window(clock.now() - timedelta(days=self.lookback_days))
                meta["mode"] = "initial_window"
        batch, rest = queue[: self.max_threads], queue[self.max_threads:]
        items: list[Item] = []
        for thread_id in batch:
            items.extend(self._thread_items(thread_id))
        for item in items:
            if item.route == "batch" and item.occurred_at and item.occurred_at < floor:
                item.route = "context"
        new_state = {"history_id": history_id if rest else (target or history_id), "since": floor}
        if rest:
            new_state.update({"queue": rest, "queue_history_id": target})
        meta.update({"threads": len(batch), "queued": len(rest)})
        return FetchResult(scope=self.scope, items=items, cursor=json.dumps(new_state), complete=not rest, meta=meta)

    def thread_key_for(self, gmail_id: str) -> str | None:
        """Thread key for a message id (or a thread id), or None when this mailbox has neither."""
        for path in (f"messages/{gmail_id}", f"threads/{gmail_id}"):
            try:
                data = self._get(path, {"format": "minimal"})
            except SourceError as exc:
                if "HTTP 404" in str(exc) or "HTTP 400" in str(exc):
                    continue
                raise
            thread = data.get("threadId") or data.get("id")
            if thread:
                return f"gmail:{self.account}:{thread}"
        return None

    def _thread_items(self, thread_id: str) -> list[Item]:
        try:
            data = self._get(f"threads/{thread_id}", {"format": "full"})
        except SourceError as exc:
            if "HTTP 404" in str(exc):
                return []  # thread deleted between listing and fetch
            raise
        items = []
        for msg in data.get("messages", []) or []:
            item = self.normalize(msg)
            if item is not None:
                items.append(item)
        return items

    def normalize(self, msg: dict[str, Any]) -> Item | None:
        labels = set(msg.get("labelIds", []) or [])
        if labels & EXCLUDED_LABELS:
            return None
        payload = msg.get("payload", {}) or {}
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", []) or []}
        body, attachments = extract_body(payload)
        sender_name, sender = parseaddr(headers.get("from", ""))
        sender = sender.lower()
        author_is_me = "SENT" in labels or sender in self.me
        recipients = [a.lower() for _, a in getaddresses([headers.get("to", ""), headers.get("cc", "")]) if a]
        occurred = datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, tz=timezone.utc)
        thread_id = msg.get("threadId", "")
        conversation = f"gmail:{self.account}:{thread_id}"
        thread_key = conversation
        route = "batch"
        github_ref = None
        match = GITHUB_MSGID.search(headers.get("message-id", "")) if "github.com" in sender else None
        if match:
            github_ref = f"{match.group(1)}/{match.group(2)}#{match.group(3)}".lower()
            thread_key = conversation = f"github:{github_ref}"
            if f"{match.group(1)}/{match.group(2)}".lower() in self.github_api_repos:
                route = "superseded"  # the GitHub API source carries this event
        bulk = bool(labels & BULK_LABELS) or "list-unsubscribe" in headers or \
            headers.get("precedence", "").lower() in ("bulk", "list", "junk")
        return Item(
            source="gmail", account=self.account, external_id=msg["id"], conversation_key=conversation,
            thread_key=thread_key, occurred_at=clock.iso(occurred),
            author=f"{sender_name} <{sender}>".strip() if sender_name else sender,
            author_is_me=author_is_me, title=headers.get("subject", "(no subject)"),
            url=f"https://mail.google.com/mail/?authuser={self.address}#all/{thread_id}",
            body=body, route=route,
            meta={"to": recipients[:20], "labels": sorted(labels), "attachments": attachments,
                  "bulk_hint": bulk and not author_is_me, "github_ref": github_ref,
                  "gmail_thread": thread_id, "rfc_message_id": headers.get("message-id"),
                  "in_reply_to": headers.get("in-reply-to")},
        )
