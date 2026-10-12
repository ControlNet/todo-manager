"""Minimal JSON-over-HTTPS client (stdlib) with rate-limit awareness.

Errors are classified so callers can decide between retry, cursor reset and failure.
Request bodies and tokens are never logged.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any


class HttpError(Exception):
    def __init__(self, status: int, message: str, retry_after: float | None = None, body: Any = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.retry_after = retry_after
        self.body = body


class HttpTimeout(Exception):
    """Request sent but no response: outcome unknown for non-idempotent calls."""


class RawBody:
    """A request body sent as given (e.g. multipart/form-data), not as JSON."""

    def __init__(self, data: bytes, content_type: str):
        self.data = data
        self.content_type = content_type


def multipart(fields: dict[str, tuple[str, str, bytes]]) -> RawBody:
    """``fields``: name -> (filename, content type, bytes)."""
    boundary = f"todo-manager-{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, (filename, content_type, data) in fields.items():
        quoted = filename.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")
        parts += [f"--{boundary}\r\n".encode(),
                  f'Content-Disposition: form-data; name="{name}"; filename="{quoted}"\r\n'.encode(),
                  f"Content-Type: {content_type}\r\n\r\n".encode(), data, b"\r\n"]
    parts.append(f"--{boundary}--\r\n".encode())
    return RawBody(b"".join(parts), f"multipart/form-data; boundary={boundary}")


class RateLimiter:
    """Spaces requests at least ``interval`` seconds apart (per connection)."""

    def __init__(self, interval: float):
        self.interval = interval
        self._last = 0.0

    def wait(self) -> None:
        delta = time.monotonic() - self._last
        if delta < self.interval:
            time.sleep(self.interval - delta)
        self._last = time.monotonic()


def request_json(method: str, url: str, *, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None,
                 body: Any = None, timeout: float = 30.0, limiter: RateLimiter | None = None,
                 max_429_retries: int = 3) -> Any:
    if params:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
        url = f"{url}{'&' if '?' in url else '?'}{query}"
    data = None
    all_headers = {"Accept": "application/json", "User-Agent": "todo-manager/0.1"}
    if headers:
        all_headers.update(headers)
    if isinstance(body, RawBody):
        data = body.data
        all_headers["Content-Type"] = body.content_type
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        all_headers["Content-Type"] = "application/json"
    attempt = 0
    while True:
        if limiter:
            limiter.wait()
        req = urllib.request.Request(url, data=data, method=method, headers=all_headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw) if raw else None
            except ValueError:
                parsed = None
            retry_after = _retry_after(exc.headers.get("Retry-After"))
            if exc.code == 429 and attempt < max_429_retries and (retry_after or 1) <= 60:
                attempt += 1
                time.sleep(retry_after or min(30, 2 ** attempt))
                continue
            message = _error_message(parsed) or exc.reason or "error"
            raise HttpError(exc.code, str(message)[:500], retry_after=retry_after, body=parsed) from None
        except (socket.timeout, TimeoutError) as exc:
            raise HttpTimeout(f"timeout after {timeout}s: {method} {urllib.parse.urlsplit(url).path}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise HttpTimeout(f"timeout: {method} {urllib.parse.urlsplit(url).path}") from exc
            raise HttpError(0, f"network error: {exc.reason}") from None


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _error_message(parsed: Any) -> str | None:
    if isinstance(parsed, dict):
        if isinstance(parsed.get("error"), dict):
            return parsed["error"].get("message")
        return parsed.get("message") or parsed.get("error")
    return None
