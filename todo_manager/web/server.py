"""The Web UI server: a page, its static files and a JSON API, on loopback (``web.host`` /
``web.port``), run by the daemon in a thread of its own.

Login: one secret, the web token (``todo-manager web token``, kept in ``state/web-token``). The
login form takes it once and sets a session cookie derived from it, so a new token signs every
browser out. Writes (POST under ``/api/``) also need the ``X-TM-Request`` header, which a page
of another site cannot send without the browser asking first.

Every request opens its own database connection under the shared workspace lock (like a
terminal command); the daemon's sync sees the writes as changes from another connection and
pushes them to Notion, and wakes the feedback job for new feedback.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import secrets
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from .. import db
from ..config import Config
from ..locks import LockBusy, file_lock, lock_path
from ..webhook import _write_private
from . import actions, views

mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("text/javascript", ".js")

STATIC = Path(__file__).with_name("static")
COOKIE = "tm_session"
COOKIE_DAYS = 90
MAX_BODY = 64 * 1024
WRITE_HEADER = "X-TM-Request"
LOCK_TIMEOUT_SECONDS = 30
TEXT_TYPES = (".md", ".txt", ".json", ".csv", ".tsv", ".py", ".tex", ".bib", ".yaml", ".yml", ".toml", ".log",
              ".html", ".js", ".css", ".sh")
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:;"
                               " frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


def token_path(home: Path) -> Path:
    return home / "state" / "web-token"


def load_token(home: Path) -> str | None:
    try:
        return token_path(home).read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def new_token(home: Path) -> str:
    token = secrets.token_urlsafe(32)
    _write_private(token_path(home), token)
    return token


def session_value(token: str) -> str:
    return hmac.new(token.encode(), b"todo-manager web session", hashlib.sha256).hexdigest()


class Reply(Exception):
    def __init__(self, status: int, body: Any = None, *, headers: dict[str, str] | None = None):
        super().__init__(status)
        self.status, self.body, self.headers = status, body, headers or {}


class WebServer:
    def __init__(self, cfg: Config, *, host: str | None = None, port: int | None = None) -> None:
        self.cfg = cfg
        self.server = ThreadingHTTPServer((host or str(cfg.get("web.host", "127.0.0.1")),
                                           int(cfg.get("web.port", 8656) if port is None else port)), self._handler())
        self.server.daemon_threads = True
        self.thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def start(self) -> None:
        self.thread = threading.Thread(target=self.server.serve_forever, name="web", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # ------------------------------------------------------------------ database
    def read(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        try:
            with file_lock(lock_path(self.cfg.home, "workspace"), blocking=True, timeout=LOCK_TIMEOUT_SECONDS,
                           shared=True):
                conn = db.open_existing(self.cfg.db_path)
                try:
                    return fn(conn)
                finally:
                    conn.close()
        except LockBusy:
            raise Reply(503, {"error": "BUSY", "message": "the workspace is being installed or restored"}) from None
        except RuntimeError as exc:   # absent workspace, schema waiting for install
            raise Reply(503, {"error": "STATE", "message": str(exc)}) from None

    # ------------------------------------------------------------------ routes
    def get(self, path: str, query: dict[str, list[str]]) -> Any:
        cfg = self.cfg
        parts = [unquote(p) for p in path.strip("/").split("/")][1:]   # after "api"
        q = {k: v[-1] for k, v in query.items()}
        match parts:
            case ["me"]:
                return self.read(lambda c: views.me(c, cfg))
            case ["home"]:
                return self.read(lambda c: views.home(c, cfg))
            case ["stamp"]:
                return self.read(lambda c: {"stamp": views.stamp(c)})
            case ["tasks"]:
                return self.read(lambda c: views.task_list(c, cfg, project=q.get("project") or None,
                                                           closed=q.get("closed") == "1"))
            case ["tasks", task_id]:
                return self.found(self.read(lambda c: views.task_detail(c, cfg, task_id)))
            case ["projects"]:
                return self.read(lambda c: views.project_list(c, cfg))
            case ["projects", project_id]:
                return self.found(self.read(lambda c: views.project_detail(c, cfg, project_id)))
            case ["search"]:
                return self.read(lambda c: views.search(c, cfg, (q.get("q") or "")[:200]))
            case ["deliveries", delivery_id]:
                return self.found(self.read(lambda c: views.delivery(c, delivery_id)))
            case ["deliveries", delivery_id, "files", index] if index.isdigit():
                path_ = self.found(self.read(lambda c: views.delivery_file(c, cfg, delivery_id, int(index))))
                raise Reply(200, path_)
        raise Reply(404, {"error": "NOT_FOUND"})

    def post(self, path: str, body: dict[str, Any]) -> Any:
        cfg = self.cfg
        parts = [unquote(p) for p in path.strip("/").split("/")][1:]
        match parts:
            case ["tasks", task_id, "act"]:
                params = body.get("params") if isinstance(body.get("params"), dict) else {}
                return self.read(lambda c: actions.act(c, cfg, task_id, str(body.get("action") or ""), params,
                                                       body.get("rid")))
            case ["tasks", task_id, "comment"]:
                return self.read(lambda c: actions.comment(c, cfg, task_id, body.get("text"), body.get("to"),
                                                           body.get("rid")))
        raise Reply(404, {"error": "NOT_FOUND"})

    @staticmethod
    def found(value: Any) -> Any:
        if value is None:
            raise Reply(404, {"error": "NOT_FOUND"})
        return value

    # ------------------------------------------------------------------ HTTP
    def _handler(self) -> type[BaseHTTPRequestHandler]:
        web = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "todo-manager"
            sys_version = ""
            timeout = 30

            def https(self) -> bool:
                return (self.headers.get("X-Forwarded-Proto") == "https"
                        or '"https"' in (self.headers.get("Cf-Visitor") or ""))

            def signed_in(self) -> bool:
                token = load_token(web.cfg.home)
                if token is None:
                    return False
                for part in (self.headers.get("Cookie") or "").split(";"):
                    name, _, value = part.strip().partition("=")
                    if name == COOKIE and hmac.compare_digest(value, session_value(token)):
                        return True
                return False

            def send(self, status: int, body: Any = None, headers: dict[str, str] | None = None) -> None:
                headers = dict(headers or {})
                if isinstance(body, Path):
                    data = body.read_bytes()
                    kind = mimetypes.guess_type(body.name)[0] or "application/octet-stream"
                    if body.suffix.lower() in TEXT_TYPES:
                        kind = "text/plain; charset=utf-8"   # shown as text, never run as a page
                    headers.setdefault("Content-Type", kind)
                    headers.setdefault("Cache-Control", "no-cache")
                elif isinstance(body, bytes):
                    data = body
                elif body is None:
                    data = b""
                else:
                    data = json.dumps(body, ensure_ascii=False, default=str).encode()
                    headers.setdefault("Content-Type", "application/json; charset=utf-8")
                    headers.setdefault("Cache-Control", "no-store")
                self.send_response(status)
                for name, value in {**SECURITY_HEADERS, **headers}.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            def static(self, name: str) -> None:
                path = (STATIC / name).resolve()
                if not path.is_file() or not path.is_relative_to(STATIC.resolve()):
                    return self.send(404)
                kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                if kind.startswith("text/") or kind in ("application/javascript", "application/json"):
                    kind += "; charset=utf-8"
                self.send(200, path.read_bytes(), {"Content-Type": kind, "Cache-Control": "no-cache"})

            def body(self) -> bytes:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    raise Reply(400, {"error": "INVALID"}) from None
                if length > MAX_BODY:
                    raise Reply(413, {"error": "TOO_LARGE"})
                return self.rfile.read(length) if length > 0 else b""

            def handle_call(self, fn: Callable[[], Any]) -> None:
                try:
                    result = fn()
                    self.send(200, result)
                except Reply as r:
                    self.send(r.status, r.body, r.headers)
                except actions.ActionError as exc:
                    self.send(exc.status, {"error": exc.code, "message": str(exc)})
                except sqlite3.Error as exc:
                    self.send(500, {"error": "STATE", "message": f"{type(exc).__name__}: {str(exc)[:200]}"})

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                url = urlsplit(self.path)
                path = url.path
                if path.startswith("/static/"):
                    return self.static(path[len("/static/"):])
                if path in ("/manifest.webmanifest", "/icon.svg"):
                    return self.static(path[1:])
                if path == "/login":
                    return self.static("login.html")
                if path.startswith("/api/"):
                    if not self.signed_in():
                        return self.send(401, {"error": "LOGIN"})
                    return self.handle_call(lambda: web.get(path, parse_qs(url.query)))
                if not self.signed_in():
                    return self.send(303, None, {"Location": "/login"})
                return self.static("index.html")   # the page routes the rest itself

            do_HEAD = do_GET

            def do_POST(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                if path == "/login":
                    return self.login()
                if path == "/logout":
                    return self.send(303, None, {"Location": "/login", "Set-Cookie": self.cookie("", 0)})
                if not path.startswith("/api/"):
                    return self.send(404)
                if not self.signed_in():
                    return self.send(401, {"error": "LOGIN"})
                if self.headers.get(WRITE_HEADER) != "1":
                    return self.send(403, {"error": "FORBIDDEN", "message": f"writes need the {WRITE_HEADER} header"})

                def call() -> Any:
                    try:
                        data = json.loads(self.body() or b"{}")
                    except ValueError:
                        raise Reply(400, {"error": "INVALID", "message": "the body is not JSON"}) from None
                    if not isinstance(data, dict):
                        raise Reply(400, {"error": "INVALID", "message": "the body is not a JSON object"})
                    return web.post(path, data)
                return self.handle_call(call)

            def cookie(self, value: str, max_age: int) -> str:
                return (f"{COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"
                        + ("; Secure" if self.https() else ""))

            def login(self) -> None:
                token = load_token(web.cfg.home)
                try:
                    given = parse_qs(self.body().decode("utf-8", "replace")).get("token", [""])[-1].strip()
                except Reply as r:
                    return self.send(r.status, r.body)
                if token is None or not given or not hmac.compare_digest(given.encode(), token.encode()):
                    return self.send(303, None, {"Location": "/login?failed=1"})
                self.send(303, None, {"Location": "/",
                                      "Set-Cookie": self.cookie(session_value(token), COOKIE_DAYS * 86400)})

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - quiet: no URLs in logs
                pass

        return Handler
