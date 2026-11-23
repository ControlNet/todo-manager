"""The Web UI server: a page, its static files and a JSON API, on loopback (``web.host`` /
``web.port``), run by the daemon in a thread of its own.

Login: the web token (``todo-manager web token``, kept in ``state/web-token``), or a password the
user chose (``todo-manager web password``, kept as a scrypt hash in ``state/web-password``). The
login form takes either once and sets a session cookie derived from both, so a new token or
password signs every browser out. Writes (POST under ``/api/``) also need the ``X-TM-Request`` header, which a page
of another site cannot send without the browser asking first.

Agents (docs/api.md) send the web token or the password as ``Authorization: Bearer``, on
``/api/*`` and on the MCP endpoint ``/mcp``; such a request carries no cookie, so its writes need
no header. The routes and the MCP tools are the operations of ``api.OPERATIONS``. A client that
cannot send a header of its own signs in with OAuth instead (``oauth.py``), and its access token is
taken the same way; the token and the password keep working beside it.

Every request opens its own database connection under the shared workspace lock (like a
terminal command); the daemon's sync sees the writes as changes from another connection and
pushes them to Notion, and wakes the feedback job for new feedback.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import mimetypes
import secrets
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

from .. import db, i18n
from ..config import Config
from ..locks import LockBusy, file_lock, lock_path
from ..webhook import _write_private
from . import api, mcp, oauth

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


LOGO = ('<svg viewBox="0 0 64 64" width="32" height="32" aria-hidden="true"><path d="M19 33l9 9 17-19" fill="none" '
        'stroke="currentColor" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/></svg>')


def consent_page(name: str, params: dict[str, str], *, ask_password: bool, failed: bool, language: str) -> bytes:
    """The page on which the user allows an OAuth client (oauth.py), styled as the login page."""
    words = {k: i18n.t(f"web.consent.{k}", language) for k in ("password", "allow", "cancel", "failed")}
    hidden = "".join(f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
                     for k, v in params.items() if k not in ("password", "decision"))
    field = (f'<label class="field"><span class="label">{words["password"]}</span>'
             '<input name="password" type="password" autocomplete="current-password" required autofocus></label>'
             if ask_password else "")
    return (f'<!doctype html><html lang="{language}"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            '<title>Todo Manager</title><link rel="icon" href="/icon.svg" type="image/svg+xml">'
            '<link rel="stylesheet" href="/static/app.css"><link rel="stylesheet" href="/static/login.css">'
            '<script src="/static/theme.js"></script></head><body><main class="login">'
            '<form method="post" action="/authorize" class="login-card">'
            f'<div class="brand"><span class="logo">{LOGO}</span><span>Todo Manager</span></div>'
            f'<p style="margin: 0">{html.escape(i18n.t("web.consent.ask", language, name=name))}</p>{hidden}{field}'
            + (f'<p class="login-failed">{words["failed"]}</p>' if failed else "")
            + f'<button class="btn primary" type="submit" name="decision" value="allow">{words["allow"]}</button>'
            f'<button class="btn" type="submit" name="decision" value="deny" formnovalidate>{words["cancel"]}</button>'
            '</form></main></body></html>').encode()


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
        self.password_seen: tuple[str, str] | None = None   # (stored hash, digest of a password that matched)

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def password_ok(self, given: str) -> bool:
        """The web password, sent by an agent on every call: one that matched is remembered (as a
        digest, until the password changes) so that only its first call pays for scrypt."""
        stored = load_password(self.cfg.home)
        if stored is None:
            return False
        seen = (stored, hashlib.sha256(given.encode()).hexdigest())
        if self.password_seen == seen:
            return True
        if check_password(self.cfg.home, given):
            self.password_seen = seen
            return True
        return False

    def grant_key(self) -> str | None:
        """What OAuth grants are signed with: they end when the token or the password changes."""
        session = session_key(self.cfg.home)
        return None if session is None else f"{oauth.key(self.cfg.home)}\n{session}"

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
                """An agent's request: the web token, or the password, as ``Authorization: Bearer``."""
                scheme, _, given = (self.headers.get("Authorization") or "").partition(" ")
                given = given.strip()
                if scheme.lower() != "bearer" or not given:
                    return False
                token = load_token(web.cfg.home)
                if token is not None and hmac.compare_digest(given.encode(), token.encode()):
                    return True
                return oauth.access_ok(web.grant_key(), given) or web.password_ok(given)

            def base(self) -> str:
                return f"{'https' if self.https() else 'http'}://{self.headers.get('Host') or 'localhost'}"

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
                if path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
                    return self.send(200, oauth.resource_metadata(self.base()))
                if path in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
                    return self.send(200, oauth.server_metadata(self.base()))
                if path == "/authorize":
                    return self.authorize({k: v[-1] for k, v in parse_qs(url.query).items()})
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
                if path in ("/register", "/token", "/authorize"):
                    return self.oauth(path)
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
                    where = f"{self.base()}/.well-known/oauth-protected-resource"
                    return self.send(401, {"error": "LOGIN"}, {"WWW-Authenticate": f'Bearer resource_metadata="{where}"'})
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

            def oauth(self, path: str) -> None:
                try:
                    raw = self.body()
                except Reply as r:
                    return self.send(r.status, r.body)
                try:
                    if path == "/register":
                        try:
                            data = json.loads(raw or b"{}")
                        except ValueError:
                            raise oauth.OAuthError("invalid_client_metadata", "the body is not JSON") from None
                        return self.send(201, oauth.register(oauth.key(web.cfg.home), data))
                    form = {k: v[-1] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
                    if path == "/token":
                        return self.send(200, oauth.exchange(web.grant_key(), form))
                    return self.authorize(form, posted=True)
                except oauth.OAuthError as exc:
                    self.send(exc.status, exc.body())

            def authorize(self, params: dict[str, str], *, posted: bool = False) -> None:
                """The consent page (GET), and the user's answer to it (POST)."""
                try:
                    found = oauth.check_request(oauth.key(web.cfg.home), params)
                except oauth.OAuthError as exc:
                    return self.send(400, exc.body())
                language = "zh-CN" if (self.headers.get("Accept-Language") or "").lower().startswith("zh") else "en"
                signed_in = self.signed_in()

                def back(**answer: str) -> None:
                    target = params["redirect_uri"]
                    extra = {**answer, **({"state": params["state"]} if "state" in params else {})}
                    self.send(302, None, {"Location": target + ("&" if "?" in target else "?") + urlencode(extra)})

                if posted and params.get("decision") == "deny":
                    return back(error="access_denied")
                if posted and params.get("decision") == "allow":
                    given = params.get("password", "").strip()
                    token = load_token(web.cfg.home)
                    by_token = token is not None and given and hmac.compare_digest(given.encode(), token.encode())
                    grant_key = web.grant_key()
                    if grant_key and (signed_in or by_token or check_password(web.cfg.home, given)):
                        return back(code=oauth.issue_code(grant_key, params))
                page = consent_page(found["name"], params, ask_password=not signed_in, failed=posted, language=language)
                self.send(200, page, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"})

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
