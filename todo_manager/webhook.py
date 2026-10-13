"""Notion webhook receiver of the sync daemon (stdlib HTTP server on loopback).

Notion reaches it through a tunnel the user runs (a Cloudflare Tunnel). An event is only a hint
that a page changed: the daemon re-reads Notion through the normal incremental paths, so a lost,
late, repeated or reordered event costs nothing but latency, and no payload text is stored or
shown to a model.

Authentication is Notion's own: the subscription's ``verification_token`` keys an HMAC-SHA256
of the raw body, sent as ``X-Notion-Signature: sha256=<hex>``. While no token is stored, the one
request that carries ``verification_token`` (Notion's handshake) is accepted and the token kept
for the user to paste into Notion's Verify dialog; every other request needs a valid signature.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import clock

SIGNATURE_HEADER = "X-Notion-Signature"
MAX_BODY = 1 << 20
SEEN_EVENTS = 2000
# Events the daemon acts on; others are acknowledged and dropped.
EVENT_TYPES = ("comment.created", "comment.updated", "comment.deleted", "page.created", "page.properties_updated",
               "page.content_updated", "page.moved", "page.deleted", "page.undeleted")


def token_path(home: Path) -> Path:
    return home / "state" / "notion-webhook-token"


def load_token(home: Path) -> str | None:
    try:
        return token_path(home).read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def save_token(home: Path, token: str) -> None:
    path = token_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token)
    os.replace(tmp, path)


def reset_token(home: Path) -> bool:
    try:
        token_path(home).unlink()
        return True
    except FileNotFoundError:
        return False


def sign(token: str, body: bytes) -> str:
    return "sha256=" + hmac.new(token.encode(), body, hashlib.sha256).hexdigest()


def verify(token: str, body: bytes, header: str | None) -> bool:
    return bool(header) and hmac.compare_digest(sign(token, body), header.strip())


@dataclass(frozen=True)
class Event:
    id: str
    type: str
    page_id: str | None           # the page the event concerns (a comment's page for comment.*)
    authors: tuple[str, ...]      # author ids
    bot_only: bool                # every author is a bot (our own writes, other integrations)


def parse(payload: Any) -> Event | None:
    """The fields the daemon uses, validated; None for anything it does not act on."""
    if not isinstance(payload, dict):
        return None
    kind, event_id, entity = payload.get("type"), payload.get("id"), payload.get("entity")
    if kind not in EVENT_TYPES or not isinstance(event_id, str) or not isinstance(entity, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    page = data.get("page_id") if kind.startswith("comment.") else entity.get("id")
    authors = [a for a in payload.get("authors") or [] if isinstance(a, dict) and isinstance(a.get("id"), str)]
    return Event(id=event_id, type=kind, page_id=page if isinstance(page, str) else None,
                 authors=tuple(a["id"] for a in authors),
                 bot_only=bool(authors) and all(a.get("type") == "bot" for a in authors))


@dataclass
class Stats:
    """What the receiver saw, read by the daemon for its status file (thread-safe enough:
    single assignments and counters under the GIL)."""
    last_event_at: str | None = None
    bad_signatures: int = 0
    handshakes: int = 0
    accepted: int = 0
    duplicates: int = 0
    ignored: int = 0
    seen: OrderedDict = field(default_factory=OrderedDict)


class Receiver:
    """``POST <path>`` on ``host:port``; accepted events go to ``events``."""

    def __init__(self, home: Path, events: "queue.Queue[Event]", *, host: str = "127.0.0.1", port: int = 8655,
                 path: str = "/notion") -> None:
        self.home = home
        self.events = events
        self.path = path
        self.stats = Stats()
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer((host, port), self._handler())
        self.server.daemon_threads = True
        self.thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def start(self) -> None:
        self.thread = threading.Thread(target=self.server.serve_forever, name="webhook", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def receive(self, body: bytes, signature: str | None) -> int:
        """Handle one delivery; returns the HTTP status."""
        token = load_token(self.home)
        if token is None:
            try:
                payload = json.loads(body)
            except ValueError:
                return 400
            offered = payload.get("verification_token") if isinstance(payload, dict) else None
            if not isinstance(offered, str) or not offered.strip():
                return 401
            save_token(self.home, offered.strip())
            self.stats.handshakes += 1
            return 200
        if not verify(token, body, signature):
            self.stats.bad_signatures += 1
            return 401
        try:
            payload = json.loads(body)
        except ValueError:
            return 400
        if isinstance(payload, dict) and "verification_token" in payload:
            return 200   # "Resend token" for a subscription already verified
        self.stats.last_event_at = clock.iso()
        event = parse(payload)
        if event is None:
            self.stats.ignored += 1
            return 200
        with self.lock:
            if event.id in self.stats.seen:
                self.stats.duplicates += 1
                return 200
            self.stats.seen[event.id] = True
            while len(self.stats.seen) > SEEN_EVENTS:
                self.stats.seen.popitem(last=False)
        self.stats.accepted += 1
        self.events.put(event)
        return 200

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "todo-manager"
            sys_version = ""
            timeout = 30

            def _reply(self, status: int) -> None:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                if self.path.split("?", 1)[0] != receiver.path:
                    return self._reply(404)
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return self._reply(400)
                if length <= 0 or length > MAX_BODY:
                    return self._reply(413 if length > MAX_BODY else 400)
                body = self.rfile.read(length)
                self._reply(receiver.receive(body, self.headers.get(SIGNATURE_HEADER)))

            def do_GET(self) -> None:  # noqa: N802
                self._reply(404)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - quiet: no bodies in logs
                pass

        return Handler
