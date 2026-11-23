"""The Web UI server: a page, its static files and a JSON API, on loopback (``web.host`` /
``web.port``), run by the daemon in a thread of its own.

Login: the web token (``todo-manager web token``, kept in ``state/web-token``), or a password the
user chose (``todo-manager web password``, kept as a scrypt hash in ``state/web-password``). The
login form takes either once and sets a session cookie derived from both, so a new token or
password signs every browser out. Writes (POST under ``/api/``) also need the ``X-TM-Request`` header, which a page
of another site cannot send without the browser asking first.

Agents (docs/api.md) send the web token as ``Authorization: Bearer``, on ``/api/*`` and on the MCP
endpoint ``/mcp``; such a request carries no cookie, so its writes need no header. The routes and
the MCP tools are the operations of ``api.OPERATIONS``.

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
from urllib.parse import parse_qs, urlsplit

from .. import db
from ..config import Config
from ..locks import LockBusy, file_lock, lock_path
from ..webhook import _write_private
from . import api, mcp

mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("font/woff2", ".woff2")

STATIC = Path(__file__).with_name("static")
COOKIE = "tm_session"
COOKIE_DAYS = 90
MAX_BODY = 64 * 1024
WRITE_HEADER = "X-TM-Request"
LOCK_TIMEOUT_SECONDS = 30
# Delivered files shown inline; anything else is plain text when it decodes as UTF-8, else a
# download. Never a page or a script on this origin (the worker wrote them).
INLINE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                ".webp": "image/webp", ".pdf": "application/pdf"}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline';"
                               " img-src 'self' data: https://www.notion.so/icons/; frame-ancestors 'none'; base-uri 'none'",
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


def password_path(home: Path) -> Path:
    return home / "state" / "web-password"


def load_password(home: Path) -> str | None:
    try:
        return password_path(home).read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=32)


def set_password(home: Path, password: str) -> None:
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, **SCRYPT)
    _write_private(password_path(home), f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${salt.hex()}${digest.hex()}")


def clear_password(home: Path) -> None:
    password_path(home).unlink(missing_ok=True)


def check_password(home: Path, given: str) -> bool:
    stored = load_password(home)
    if stored is None or not given:
        return False
    try:
        _, n, r, p, salt, digest = stored.split("$")
        return hmac.compare_digest(_scrypt(given, bytes.fromhex(salt), int(n), int(r), int(p)), bytes.fromhex(digest))
    except ValueError:
        return False


def session_key(home: Path) -> str | None:
    """What the session cookie is derived from: the token, and the password's hash when one is set."""
    parts = [x for x in (load_token(home), load_password(home)) if x]
    return "\n".join(parts) or None


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

    # ------------------------------------------------------------------ operations
    def run(self, op: api.Op, args: dict[str, Any]) -> Any:
        """One operation on the database; a refusal becomes ``api.ApiFailure``."""
        try:
            return self.read(lambda c: op.run(c, self.cfg, args))
        except Reply as r:
            raise api.ApiFailure(r.status, r.body) from None
        except Exception as exc:
            refused = api.failure(exc)
            if refused is None:
                raise
            raise refused from None

    def call(self, method: str, path: str, args: dict[str, Any]) -> Any:
        found = api.route(method, path)
        if found is None:
            raise Reply(404, {"error": "NOT_FOUND"})
        op, given = found
        return self.run(op, {**args, **given})

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

            def bearer(self) -> bool:
                """An agent's request: the web token as ``Authorization: Bearer``."""
                token = load_token(web.cfg.home)
                scheme, _, given = (self.headers.get("Authorization") or "").partition(" ")
                return (token is not None and scheme.lower() == "bearer"
                        and hmac.compare_digest(given.strip().encode(), token.encode()))

            def signed_in(self) -> bool:
                key = session_key(web.cfg.home)
                if key is None:
                    return False
                for part in (self.headers.get("Cookie") or "").split(";"):
                    name, _, value = part.strip().partition("=")
                    if name == COOKIE and hmac.compare_digest(value, session_value(key)):
                        return True
                return False

            def send(self, status: int, body: Any = None, headers: dict[str, str] | None = None) -> None:
                headers = dict(headers or {})
                if isinstance(body, Path):
                    data = body.read_bytes()
                    kind = INLINE_TYPES.get(body.suffix.lower())
                    if kind is None:
                        try:
                            data.decode("utf-8")
                            kind = "text/plain; charset=utf-8"
                        except UnicodeDecodeError:
                            kind = "application/octet-stream"
                            headers.setdefault("Content-Disposition", "attachment")
                    headers.setdefault("Content-Type", kind)
                    headers.setdefault("Cache-Control", "no-cache")
                    headers.setdefault("Content-Security-Policy", "sandbox; default-src 'none'")
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
                except api.ApiFailure as f:
                    self.send(f.status, f.body)
                except sqlite3.Error as exc:
                    self.send(500, {"error": "STATE", "message": f"{type(exc).__name__}: {str(exc)[:200]}"})
                except Exception as exc:  # noqa: BLE001 - answer instead of dropping the connection
                    self.send(500, {"error": "INTERNAL", "message": type(exc).__name__})

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                url = urlsplit(self.path)
                path = url.path
                if path.startswith("/static/"):
                    return self.static(path[len("/static/"):])
                if path in ("/manifest.webmanifest", "/icon.svg"):
                    return self.static(path[1:])
                if path == "/login":
                    return self.static("login.html")
                if path == "/mcp":
                    return self.send(405, None, {"Allow": "POST"})   # no server-sent stream
                if path.startswith("/api/"):
                    if not (self.bearer() or self.signed_in()):
                        return self.send(401, {"error": "LOGIN"})
                    query = {k: v[-1] for k, v in parse_qs(url.query).items()}
                    return self.handle_call(lambda: web.call("GET", path, query))
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
                if path == "/mcp":
                    return self.mcp()
                self.write("POST", path)

            def do_PATCH(self) -> None:  # noqa: N802
                self.write("PATCH", urlsplit(self.path).path)

            def write(self, method: str, path: str) -> None:
                if not path.startswith("/api/"):
                    return self.send(404)
                if not self.bearer():
                    if not self.signed_in():
                        return self.send(401, {"error": "LOGIN"})
                    if self.headers.get(WRITE_HEADER) != "1":
                        return self.send(403, {"error": "FORBIDDEN", "message": f"writes need the {WRITE_HEADER} header"})
                return self.handle_call(lambda: web.call(method, path, self.json_body()))

            def json_body(self) -> dict[str, Any]:
                try:
                    data = json.loads(self.body() or b"{}")
                except ValueError:
                    raise Reply(400, {"error": "INVALID", "message": "the body is not JSON"}) from None
                if not isinstance(data, dict):
                    raise Reply(400, {"error": "INVALID", "message": "the body is not a JSON object"})
                return data

            def mcp(self) -> None:
                if not self.bearer():
                    return self.send(401, {"error": "LOGIN"}, {"WWW-Authenticate": "Bearer"})
                try:
                    message = json.loads(self.body() or b"null")
                except (ValueError, Reply):
                    return self.send(400, {"jsonrpc": "2.0", "id": None,
                                           "error": {"code": -32700, "message": "the body is not JSON"}})
                try:
                    reply = mcp.handle(message, web.run)
                except Exception as exc:  # noqa: BLE001 - answer the client instead of dropping it
                    reply = {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                             "error": {"code": -32603, "message": type(exc).__name__}}
                if reply is None:
                    return self.send(202)
                self.send(200, reply)

            def cookie(self, value: str, max_age: int) -> str:
                return (f"{COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"
                        + ("; Secure" if self.https() else ""))

            def login(self) -> None:
                token = load_token(web.cfg.home)
                try:
                    given = parse_qs(self.body().decode("utf-8", "replace")).get("token", [""])[-1].strip()
                except Reply as r:
                    return self.send(r.status, r.body)
                by_token = token is not None and given and hmac.compare_digest(given.encode(), token.encode())
                if not (by_token or check_password(web.cfg.home, given)):
                    return self.send(303, None, {"Location": "/login?failed=1"})
                self.send(303, None, {"Location": "/",
                                      "Set-Cookie": self.cookie(session_value(session_key(web.cfg.home)),
                                                                COOKIE_DAYS * 86400)})

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - quiet: no URLs in logs
                pass

        return Handler
