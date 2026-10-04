"""Slack adapter (Web API, read-only) with a bot/user token from an environment variable.

* Only conversations listed in ``slack.conversations`` are read.
* Threaded replies are keyed by thread (``slack:<channel>:<thread_ts>``); unthreaded chat is
  keyed by the whole channel/DM (no guessing of topic boundaries in v1).
* Each run re-reads an overlap window so edits (new ``edited.ts`` version) and deletions
  (a stored message missing from the window) become versioned items. Reactions and other
  non-semantic changes produce nothing.
* The batch quiet window (>= 30 min) is applied by ``batching`` on message time.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .. import clock
from ..net import HttpError, HttpTimeout, request_json
from .base import FetchResult, Item, SourceError

API = "https://slack.com/api"
SKIP_SUBTYPES = {"channel_join", "channel_leave", "bot_add", "channel_topic", "channel_purpose", "reminder_add"}


def ts_to_iso(ts: str) -> str:
    return clock.iso(datetime.fromtimestamp(float(ts), tz=timezone.utc))


class SlackSource:
    name = "slack"

    def __init__(self, *, token: Callable[[], str], team: str, conversation: str, my_user_ids: list[str],
                 overlap_minutes: int = 24 * 60, lookback_days: int = 7, page_size: int = 200,
                 http: Callable[..., Any] = request_json, known_ids: Callable[[set[str]], set[str]] | None = None):
        self.token = token
        self.team = team
        self.channel = conversation
        self.me = set(my_user_ids)
        self.overlap = overlap_minutes
        self.lookback_days = lookback_days
        self.page_size = page_size
        self.http = http
        self.known_ids = known_ids

    @property
    def scope(self) -> str:
        return f"conversation:{self.channel}"

    def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            data = self.http("GET", f"{API}/{method}", params=params, timeout=30,
                             headers={"Authorization": f"Bearer {self.token()}"})
        except HttpError as exc:
            raise SourceError(str(exc), retry_after=exc.retry_after) from None
        except HttpTimeout as exc:
            raise SourceError(str(exc)) from None
        if not data.get("ok"):
            raise SourceError(f"slack {method}: {data.get('error')}")
        return data

    def _paged(self, method: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        cursor = None
        for _ in range(200):
            data = self._call(method, {**params, "limit": self.page_size, "cursor": cursor})
            messages.extend(data.get("messages", []) or [])
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return messages
        raise SourceError(f"slack {method}: pagination limit reached; refusing to truncate")

    def fetch(self, cursor: str | None, last_success_at: str | None = None) -> FetchResult:
        state = json.loads(cursor) if cursor else {}
        started = clock.now()
        newest = float(state.get("newest", 0)) or (started - timedelta(days=self.lookback_days)).timestamp()
        oldest = newest - self.overlap * 60
        top = self._paged("conversations.history", {"channel": self.channel, "oldest": f"{oldest:.6f}",
                                                    "inclusive": "true"})
        items: list[Item] = []
        seen_ids: set[str] = set()
        max_ts = newest
        threads: dict[str, dict[str, Any]] = {}
        for msg in top:
            if msg.get("subtype") in SKIP_SUBTYPES:
                continue
            max_ts = max(max_ts, float(msg["ts"]))
            if msg.get("reply_count") and msg.get("thread_ts") == msg.get("ts"):
                threads[msg["ts"]] = msg
            item = self._normalize(msg, thread_ts=msg["ts"] if msg.get("reply_count") else None)
            items.append(item)
            seen_ids.add(item.external_id)
        for thread_ts, parent in threads.items():
            if float(parent.get("latest_reply", "0")) < oldest:
                continue
            for reply in self._paged("conversations.replies", {"channel": self.channel, "ts": thread_ts,
                                                               "oldest": f"{oldest:.6f}"}):
                if reply.get("ts") == thread_ts or reply.get("subtype") in SKIP_SUBTYPES:
                    continue
                max_ts = max(max_ts, float(reply["ts"]))
                item = self._normalize(reply, thread_ts=thread_ts)
                items.append(item)
                seen_ids.add(item.external_id)
        # Deletions: messages we stored inside the re-read window that are gone now.
        window_ids = set(state.get("window_ids", []))
        for missing in sorted(window_ids - seen_ids):
            channel, _, ts = missing.partition(":")
            if ts and float(ts) >= oldest:
                items.append(Item(source="slack", account=self.team, external_id=missing, version="deleted",
                                  kind="deletion", conversation_key=f"slack:{self.channel}",
                                  thread_key=f"slack:{self.channel}", occurred_at=clock.iso(started),
                                  title="(message deleted)", body="", meta={"deleted": True}))
        new_state = {"newest": max_ts, "window_ids": sorted(i for i in seen_ids if float(i.split(":")[1]) >= max_ts - self.overlap * 60)}
        return FetchResult(scope=self.scope, items=items, cursor=json.dumps(new_state),
                           meta={"messages": len(items), "threads": len(threads)})

    def _normalize(self, msg: dict[str, Any], thread_ts: str | None) -> Item:
        external = f"{self.channel}:{msg['ts']}"
        edited = (msg.get("edited") or {}).get("ts", "")
        key = f"slack:{self.channel}:{thread_ts}" if thread_ts else f"slack:{self.channel}"
        user = msg.get("user") or msg.get("bot_id") or ""
        is_bot = bool(msg.get("bot_id")) and user not in self.me
        files = [f.get("name") for f in msg.get("files", []) or []]
        return Item(source="slack", account=self.team, external_id=external, version=edited,
                    kind="edit" if edited else "message", conversation_key=key, thread_key=key,
                    occurred_at=ts_to_iso(edited or msg["ts"]), author=user, author_is_me=user in self.me,
                    title=None, url=None, body=msg.get("text", ""), route="context" if is_bot else "batch",
                    meta={"files": files, "thread_ts": thread_ts, "ts": msg["ts"]})
