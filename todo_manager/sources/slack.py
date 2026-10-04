"""Slack adapter (Web API, read-only) with a user/bot token from a private file or an environment variable.

* Read scope: the conversations listed in ``slack.conversations``, plus - when
  ``slack.direct_messages`` / ``group_direct_messages`` / ``channels`` is on - every DM, group DM
  or channel the user is a member of, discovered each run by ``SlackDirectory`` (new ones are
  included without config changes) minus the ``slack.exclude`` blacklist.
* Threaded replies are keyed by thread (``slack:<channel>:<thread_ts>``); unthreaded chat is
  keyed by the whole channel/DM (no guessing of topic boundaries in v1).
* Each read re-reads an overlap window so edits (new ``edited.ts`` version) and deletions
  (a stored message missing from the window) become versioned items. Reactions and other
  non-semantic changes produce nothing. A message is reported deleted only when the listing
  that contains it (the history window, or that thread's replies) was read in this run.
* Threads whose parent is older than the window: a bounded index (``thread_index_max`` threads
  active within ``thread_watch_days``) remembers the last reply read per thread, and a scan of
  older parents reads a thread again when it has a newer reply or activity inside the overlap
  window. A scan pass walks parents newest first back to the ``thread_watch_days`` horizon; one
  run reads at most ``thread_scan_pages`` pages and an unfinished pass resumes from the oldest
  parent it reached on the next run, so no parent is starved. A new pass starts every
  ``thread_scan_minutes``. The watermark below which an unknown thread's replies count as already
  covered (``scan_since``) advances only when a pass completes. Coverage is by parent time: a
  thread whose parent is older than the horizon is not read, however recent its replies.
  A thread never seen before also brings its parent and up to ``CONTEXT_REPLIES`` earlier
  replies as context.
* Cadence: a conversation with a human (non-bot) message in the last ``active_hours`` is read
  every run; a quiet one at most once per ``quiet_poll_minutes`` (staggered per conversation),
  which keeps dozens of conversations within Slack's rate limits.
* The batch quiet window (>= 30 min) is applied by ``batching`` on message time.
"""

from __future__ import annotations

import json
import re
import zlib
from dataclasses import replace
from fnmatch import fnmatchcase
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
CONTEXT_REPLIES = 10   # earlier replies shown with a never-seen thread


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

    def _paged(self, method: str, params: dict[str, Any], key: str = "messages",
               max_pages: int | None = None) -> list[dict[str, Any]]:
        """All pages; with ``max_pages`` the first pages only (the caller accepts truncation)."""
        found: list[dict[str, Any]] = []
        cursor = None
        for _ in range(max_pages or 200):
            data = self._call(method, {**params, "limit": self.page_size, "cursor": cursor})
            found.extend(data.get(key, []) or [])
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return found
        if max_pages:
            self.truncated = True
            return found
        raise SourceError(f"slack {method}: pagination limit reached; refusing to truncate")


class SlackDirectory(SlackApi):
    """Lists the user's DMs (``im``), group DMs (``mpim``) and member channels
    (``public_channel``, ``private_channel``) so that all of them are read.

    ``exclude`` is a blacklist; an entry matches a conversation id, a DM partner's user id, or a
    channel / group DM name (glob, case-insensitive, optional leading ``#``).
    The list is the cursor: when listing fails, the last known conversations are still read.
    ``expand`` turns it into one ``SlackSource`` per conversation (built by ``make``)."""

    name = "slack"
    scope = "directory"

    def __init__(self, *, token: Callable[[], str], types: list[str], make: Callable[..., "SlackSource"],
                 exclude: list[str] | None = None, page_size: int = 200, http: Callable[..., Any] = request_json):
        self.token = token
        self.types = list(types)
        self.make = make
        self.exclude = list(exclude or [])
        self.page_size = page_size
        self.http = http

    def excluded(self, conv: dict[str, Any]) -> bool:
        keys = {conv["id"], conv.get("user") or ""}
        name = (conv.get("name") or "").lower()
        for entry in self.exclude:
            if entry in keys or (name and fnmatchcase(name, entry.lower().lstrip("#"))):
                return True
        return False

    def fetch(self, cursor: str | None, last_success_at: str | None = None) -> FetchResult:
        found = []
        for conv in self._paged("users.conversations", {"types": ",".join(self.types), "exclude_archived": "true"},
                                key="channels"):
            if self.excluded(conv):
                continue
            if conv.get("is_im"):
                if conv.get("user") == SLACKBOT:
                    continue
                found.append({"id": conv["id"], "peer": conv.get("user")})
            elif conv.get("is_mpim"):
                found.append({"id": conv["id"], "title": f"group DM ({conv.get('name') or conv['id']})"})
            else:
                found.append({"id": conv["id"], "title": f"#{conv.get('name') or conv['id']}"})
        return FetchResult(scope=self.scope, items=[], cursor=json.dumps({"conversations": found}),
                           meta={"conversations": len(found)})

    @staticmethod
    def conversation_prefix(scope: str) -> str:
        """Conversation keys of a discovered scope (``slack:<id>`` and its threads)."""
        return f"slack:{scope.partition(':')[2]}"

    def expand(self, cursor: str | None) -> list["SlackSource"]:
        state = json.loads(cursor) if cursor else {}
        return [self.make(c["id"], peer=c.get("peer"), title=c.get("title")) for c in state.get("conversations", [])]


class SlackSource(SlackApi):
    name = "slack"

    def __init__(self, *, token: Callable[[], str], team: str, conversation: str, my_user_ids: list[str],
                 overlap_minutes: int = 24 * 60, lookback_days: int = 7, page_size: int = 200,
                 http: Callable[..., Any] = request_json, known_ids: Callable[[set[str]], set[str]] | None = None,
                 user_names: dict[str, str | None] | None = None, peer: str | None = None,
                 title: str | None = None, active_hours: int = 7 * 24, quiet_poll_minutes: int = 0,
                 thread_watch_days: int = 30, thread_index_max: int = 200, thread_scan_minutes: int = 60,
                 thread_scan_pages: int = 5):
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
        self.watch_days = thread_watch_days
        self.index_max = thread_index_max
        self.scan_minutes = thread_scan_minutes
        self.scan_pages = thread_scan_pages
        self.truncated = False

    @property
    def scope(self) -> str:
        return f"conversation:{self.channel}"

    def due(self, cursor_row: dict[str, Any] | None) -> bool:
        """Read in this run? Always without a quiet interval, when never read successfully, or
        with a human message in the last ``active_hours``; otherwise once per ``quiet_poll_minutes``
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
        now = started.timestamp()
        newest = float(state.get("newest", 0)) or (started - timedelta(days=self.lookback_days)).timestamp()
        oldest = newest - self.overlap * 60
        top = self._paged("conversations.history", {"channel": self.channel, "oldest": f"{oldest:.6f}",
                                                    "inclusive": "true"})
        items: list[Item] = []
        seen: dict[str, str] = {}          # external id -> container ("" history, "p:<ts>" parent, "r:<ts>" reply)
        read_threads: dict[str, float] = {}   # thread ts -> replies read from this ts on
        index = {ts: float(v) for ts, v in (state.get("thread_index") or {}).items()}
        max_ts = newest
        threads: dict[str, dict[str, Any]] = {}
        for msg in top:
            if msg.get("subtype") in SKIP_SUBTYPES:
                continue
            max_ts = max(max_ts, float(msg["ts"]))
            parent = bool(msg.get("reply_count")) and msg.get("thread_ts") == msg.get("ts")
            if parent:
                threads[msg["ts"]] = msg
            item = self._normalize(msg, thread_ts=msg["ts"] if msg.get("reply_count") else None)
            items.append(item)
            seen[item.external_id] = f"p:{msg['ts']}" if parent else ""
        for thread_ts, parent in threads.items():
            latest = float(parent.get("latest_reply", "0"))
            if latest < oldest:
                index[thread_ts] = max(index.get(thread_ts, 0.0), latest)
                continue
            max_ts = max(max_ts, self._read_thread(thread_ts, oldest, None, items, seen, read_threads, index))
        scan = self._scan_threads(state, now, oldest, items, seen, read_threads, index)
        if scan.get("max_ts"):
            max_ts = max(max_ts, scan["max_ts"])
        items.extend(self._deletions(state, oldest, seen, read_threads, started))
        # Last human message: bot traffic alone does not make a conversation active.
        last_message = max([float(state.get("last_message") or 0)]
                           + [float(i.meta["ts"]) for i in items if i.kind != "deletion" and i.route == "batch"])
        horizon = now - self.watch_days * 86400
        kept = sorted(((ts, v) for ts, v in index.items() if v >= horizon), key=lambda kv: -kv[1])[:self.index_max]
        new_state = {"newest": max_ts, "last_message": last_message or None,
                     "window": {i: c for i, c in seen.items() if float(i.split(":")[1]) >= max_ts - self.overlap * 60},
                     "thread_index": {ts: f"{v:.6f}" for ts, v in kept}}
        for key in ("scanned_at", "scan_since", "scan_resume", "scan_pass_since"):
            new_state[key] = scan[key] if key in scan else state.get(key)
        meta: dict[str, Any] = {"messages": len(items), "threads": len(read_threads)}
        if scan.get("progress"):
            meta["thread_scan"] = scan["progress"]          # complete | partial (resumes next run)
        if scan.get("progress") == "partial":
            meta["thread_scan_truncated"] = True
        if self.quiet_poll:
            meta["poll_interval_minutes"] = self.quiet_poll   # widens the health staleness threshold
        return FetchResult(scope=self.scope, items=items, cursor=json.dumps(new_state), meta=meta)

    def _read_thread(self, thread_ts: str, since: float, context_before: float | None, items: list[Item],
                     seen: dict[str, str], read_threads: dict[str, float], index: dict[str, float]) -> float:
        """Replies of one thread from ``since`` on. With ``context_before`` (a thread never seen
        before) the parent and the last earlier replies are added as context only."""
        replies = self._paged("conversations.replies", {"channel": self.channel, "ts": thread_ts,
                                                        "oldest": f"{since:.6f}"})
        read_threads[thread_ts] = since
        max_ts = 0.0
        earlier = []
        for reply in replies:
            if reply.get("subtype") in SKIP_SUBTYPES:
                continue
            if reply.get("ts") == thread_ts:
                if context_before is not None:
                    earlier.insert(0, reply)
                continue
            if context_before is not None and float(reply["ts"]) < context_before:
                earlier.append(reply)
                continue
            max_ts = max(max_ts, float(reply["ts"]))
            item = self._normalize(reply, thread_ts=thread_ts)
            items.append(item)
            seen[item.external_id] = f"r:{thread_ts}"
        if earlier:
            shown = earlier[:1] + earlier[1:][-CONTEXT_REPLIES:] if earlier[0].get("ts") == thread_ts \
                else earlier[-CONTEXT_REPLIES:]
            for msg in shown:
                items.append(replace(self._normalize(msg, thread_ts=thread_ts), route="context"))
        index[thread_ts] = max(index.get(thread_ts, 0.0), max_ts,
                               max((float(m["ts"]) for m in earlier if m.get("ts") != thread_ts), default=0.0))
        return max_ts

    def _scan_threads(self, state: dict[str, Any], now: float, oldest: float, items: list[Item],
                      seen: dict[str, str], read_threads: dict[str, float], index: dict[str, float]) -> dict[str, Any]:
        """Parents older than the window whose thread has a reply we have not read, or activity
        inside the overlap window (edits, deletions). One run reads at most ``scan_pages`` pages;
        an unfinished pass resumes below the oldest parent it reached."""
        resume = state.get("scan_resume")
        scanned_at = float(state.get("scanned_at") or 0)
        if resume is None and self.scan_minutes and now - scanned_at < self.scan_minutes * 60:
            return {}
        horizon = now - self.watch_days * 86400
        pass_since = float(state.get("scan_pass_since") or oldest) if resume is not None else oldest
        upper = min(float(resume), oldest) if resume is not None else oldest
        done = {"scanned_at": now, "scan_since": pass_since, "scan_resume": None, "scan_pass_since": None,
                "progress": "complete"}
        if horizon >= upper:
            return done
        # Replies newer than this were possibly never read: the window start when the last
        # COMPLETE pass began (a partial pass does not move it).
        scan_since = float(state.get("scan_since") or pass_since)
        max_ts = 0.0
        self.truncated = False
        parents = self._paged("conversations.history", {"channel": self.channel, "oldest": f"{horizon:.6f}",
                                                        "latest": f"{upper:.6f}",
                                                        "inclusive": "false" if resume is not None else "true"},
                              max_pages=self.scan_pages)
        partial = self.truncated and bool(parents)
        for parent in parents:
            ts = parent.get("ts")
            if not parent.get("reply_count") or parent.get("thread_ts") != ts or ts in read_threads:
                continue
            latest = float(parent.get("latest_reply", "0"))
            known = index.get(ts)
            if known is None:
                if latest < scan_since:
                    index[ts] = latest          # activity we predate or already covered: only remember it
                    continue
                max_ts = max(max_ts, self._read_thread(ts, 0.0, scan_since, items, seen, read_threads, index))
            elif latest > known or latest >= oldest:
                max_ts = max(max_ts, self._read_thread(ts, min(known, oldest), None, items, seen, read_threads, index))
        if partial:
            return {"scan_resume": min(float(p["ts"]) for p in parents), "scan_pass_since": pass_since,
                    "max_ts": max_ts, "progress": "partial"}
        return {**done, "max_ts": max_ts}

    def _deletions(self, state: dict[str, Any], oldest: float, seen: dict[str, str],
                   read_threads: dict[str, float], started: datetime) -> list[Item]:
        """Stored messages missing from a listing that was read in full this run. A legacy
        cursor (``window_ids`` without containers) is not used: unread is not deleted."""
        found = []
        for missing, container in sorted((state.get("window") or {}).items()):
            if missing in seen:
                continue
            ts = float(missing.partition(":")[2] or 0)
            kind, _, thread = container.partition(":")
            if kind == "r":
                if thread not in read_threads or ts < read_threads[thread]:
                    continue
            elif ts < oldest:
                continue
            key = f"slack:{self.channel}:{thread}" if thread else f"slack:{self.channel}"
            found.append(Item(source="slack", account=self.team, external_id=missing, version="deleted",
                              kind="deletion", conversation_key=key, thread_key=key, occurred_at=clock.iso(started),
                              title="(message deleted)", body="", meta={"deleted": True}))
        return found

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
