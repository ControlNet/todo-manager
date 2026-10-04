"""Slack adapter (Web API, read-only) with a user/bot token from a private file or an environment variable.

* Read scope: the conversations listed in ``slack.conversations``, plus - when
  ``slack.direct_messages`` / ``slack.group_direct_messages`` is on - every DM / group DM of the
  user, discovered each run by ``SlackDirectory`` (so new DMs are included without config changes).
* Threaded replies are keyed by thread (``slack:<channel>:<thread_ts>``); unthreaded chat is
  keyed by the whole channel/DM (no guessing of topic boundaries in v1).
* Each read re-reads an overlap window so edits (new ``edited.ts`` version) and deletions
  (a stored message missing from the window) become versioned items. Reactions and other
  non-semantic changes produce nothing.
* Cadence: a conversation with a message in the last ``active_hours`` is read every run; a
  quiet one at most once per ``quiet_poll_minutes`` (staggered per conversation), which keeps
  dozens of DMs within Slack's rate limits.
* The batch quiet window (>= 30 min) is applied by ``batching`` on message time.
"""

from __future__ import annotations

import json
import re
import zlib
from datetime import datetime, timedelta, timezone
from functools import cached_property
from typing import Any, Callable

from .. import clock
from ..net import HttpError, HttpTimeout, request_json
from .base import FetchResult, Item, SourceError

API = "https://slack.com/api"
MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
SLACKBOT = "USLACKBOT"   # its DM is not readable through conversations.history (channel_not_found)
SKIP_SUBTYPES = {"channel_join", "channel_leave", "bot_add", "channel_topic", "channel_purpose", "reminder_add"}


def ts_to_iso(ts: str) -> str:
    return clock.iso(datetime.fromtimestamp(float(ts), tz=timezone.utc))


class SlackApi:
    token: Callable[[], str]
    http: Callable[..., Any]
    page_size: int

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

    def _paged(self, method: str, params: dict[str, Any], key: str = "messages") -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        cursor = None
        for _ in range(200):
            data = self._call(method, {**params, "limit": self.page_size, "cursor": cursor})
            found.extend(data.get(key, []) or [])
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return found
        raise SourceError(f"slack {method}: pagination limit reached; refusing to truncate")


class SlackDirectory(SlackApi):
    """Lists the user's DMs (``im``) and group DMs (``mpim``) so that all of them are read.

    The list is the cursor: when listing fails, the last known conversations are still read.
    ``expand`` turns it into one ``SlackSource`` per conversation (built by ``make``)."""

    name = "slack"
    scope = "directory"

    def __init__(self, *, token: Callable[[], str], types: list[str], make: Callable[..., "SlackSource"],
                 page_size: int = 200, http: Callable[..., Any] = request_json):
        self.token = token
        self.types = list(types)
        self.make = make
        self.page_size = page_size
        self.http = http

    def fetch(self, cursor: str | None, last_success_at: str | None = None) -> FetchResult:
        found = []
        for conv in self._paged("users.conversations", {"types": ",".join(self.types), "exclude_archived": "true"},
                                key="channels"):
            if conv.get("is_im"):
                if conv.get("user") == SLACKBOT:
                    continue
                found.append({"id": conv["id"], "peer": conv.get("user")})
            else:
                found.append({"id": conv["id"], "title": f"group DM ({conv.get('name') or conv['id']})"})
        return FetchResult(scope=self.scope, items=[], cursor=json.dumps({"conversations": found}),
                           meta={"conversations": len(found)})

    def expand(self, cursor: str | None) -> list["SlackSource"]:
        state = json.loads(cursor) if cursor else {}
        return [self.make(c["id"], peer=c.get("peer"), title=c.get("title")) for c in state.get("conversations", [])]


class SlackSource(SlackApi):
    name = "slack"

    def __init__(self, *, token: Callable[[], str], team: str, conversation: str, my_user_ids: list[str],
                 overlap_minutes: int = 24 * 60, lookback_days: int = 7, page_size: int = 200,
                 http: Callable[..., Any] = request_json, known_ids: Callable[[set[str]], set[str]] | None = None,
                 user_names: dict[str, str | None] | None = None, peer: str | None = None,
                 title: str | None = None, active_hours: int = 7 * 24, quiet_poll_minutes: int = 0):
        self.token = token
        self.team = team
        self.channel = conversation
        self.me = set(my_user_ids)
        self.overlap = overlap_minutes
        self.lookback_days = lookback_days
        self.page_size = page_size
        self.http = http
        self.known_ids = known_ids
        self.names = user_names if user_names is not None else {}   # shared cache: user id -> name
        self.peer = peer            # the other member of a 1:1 DM
        self.title = title          # conversation label shown with each item
        self.active_hours = active_hours
        self.quiet_poll = quiet_poll_minutes

    @property
    def scope(self) -> str:
        return f"conversation:{self.channel}"

    def due(self, cursor_row: dict[str, Any] | None) -> bool:
        """Read in this run? Always without a quiet interval, when never read successfully, or
        with a message in the last ``active_hours``; otherwise once per ``quiet_poll_minutes``
        slot, offset per conversation so quiet conversations are not all read in one burst."""
        if not self.quiet_poll or not cursor_row or not cursor_row.get("cursor") \
                or not cursor_row.get("last_success_at"):
            return True
        now = clock.now().timestamp()
        last_message = float(json.loads(cursor_row["cursor"]).get("last_message") or 0)
        if now - last_message < self.active_hours * 3600:
            return True
        interval = self.quiet_poll * 60
        phase = zlib.crc32(self.channel.encode()) % interval
        succeeded = clock.parse(cursor_row["last_success_at"]).timestamp()
        return (now - phase) // interval > (succeeded - phase) // interval

    @cached_property
    def conversation_title(self) -> str | None:
        if not self.peer:
            return self.title
        if self.peer in self.me:
            return "DM with yourself"
        return f"DM with {self._name(self.peer) or self.peer}"

    def _name(self, user_id: str) -> str | None:
        """Display name of a workspace member (users.info, cached); None when unknown."""
        if user_id not in self.names:
            try:
                user = self._call("users.info", {"user": user_id}).get("user") or {}
            except SourceError as exc:
                if exc.retry_after:
                    raise
                user = {}
            profile = user.get("profile") or {}
            self.names[user_id] = (user.get("real_name") or profile.get("real_name")
                                   or profile.get("display_name") or user.get("name") or None)
        return self.names[user_id]

    def _mentions(self, text: str) -> str:
        def name(match: re.Match[str]) -> str:
            uid = match.group(1)
            label = self._name(uid)
            if not label:
                return match.group(0)
            return f"@{label}" + (" [ME]" if uid in self.me else "")
        return MENTION.sub(name, text or "")

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
        last_message = max([float(state.get("last_message") or 0)]
                           + [float(i.meta["ts"]) for i in items if i.kind != "deletion"])
        new_state = {"newest": max_ts, "last_message": last_message or None,
                     "window_ids": sorted(i for i in seen_ids if float(i.split(":")[1]) >= max_ts - self.overlap * 60)}
        meta: dict[str, Any] = {"messages": len(items), "threads": len(threads)}
        if self.quiet_poll:
            meta["poll_interval_minutes"] = self.quiet_poll   # widens the health staleness threshold
        return FetchResult(scope=self.scope, items=items, cursor=json.dumps(new_state), meta=meta)

    def _normalize(self, msg: dict[str, Any], thread_ts: str | None) -> Item:
        external = f"{self.channel}:{msg['ts']}"
        edited = (msg.get("edited") or {}).get("ts", "")
        key = f"slack:{self.channel}:{thread_ts}" if thread_ts else f"slack:{self.channel}"
        user = msg.get("user") or msg.get("bot_id") or ""
        is_bot = bool(msg.get("bot_id")) and user not in self.me
        files = [f.get("name") for f in msg.get("files", []) or []]
        if is_bot:
            label = (msg.get("bot_profile") or {}).get("name") or msg.get("username")
        else:
            label = self._name(user) if msg.get("user") else None
        author = f"{label} ({user})" if label else user
        return Item(source="slack", account=self.team, external_id=external, version=edited,
                    kind="edit" if edited else "message", conversation_key=key, thread_key=key,
                    occurred_at=ts_to_iso(edited or msg["ts"]), author=author, author_is_me=user in self.me,
                    title=self.conversation_title, url=None, body=self._mentions(msg.get("text", "")),
                    route="context" if is_bot else "batch",
                    meta={"files": files, "thread_ts": thread_ts, "ts": msg["ts"]})
