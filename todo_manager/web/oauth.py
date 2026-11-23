"""OAuth 2.1 for MCP clients that cannot send a header of their own (ChatGPT's connectors and
the like): beside the web token and the password, an access token from here signs a client in.

Nothing is stored. A client's registration is its ``client_id``, signed with ``state/oauth-key``,
so it stays registered. Codes, access tokens and refresh tokens are signed with that key and the
web session key (the token and the password), so a new token or password signs every client out
as it signs browsers out; the client then asks the user again. The user allows a client on
``/authorize`` with the web password or the web token, or with one click when the browser is
signed in.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from pathlib import Path
from typing import Any

from .. import clock
from ..webhook import _write_private

ACCESS_SECONDS = 3600
REFRESH_SECONDS = 90 * 86400
CODE_SECONDS = 600


class OAuthError(Exception):
    def __init__(self, error: str, description: str = "", status: int = 400) -> None:
        super().__init__(error)
        self.error, self.description, self.status = error, description, status

    def body(self) -> dict[str, str]:
        return {"error": self.error, **({"error_description": self.description} if self.description else {})}


def key(home: Path) -> str:
    path = home / "state" / "oauth-key"
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    except FileNotFoundError:
        pass
    value = secrets.token_urlsafe(32)
    _write_private(path, value)
    return value


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(secret: str, kind: str, payload: dict[str, Any]) -> str:
    body = _b64(json.dumps({**payload, "k": kind}, separators=(",", ":")).encode())
    return body + "." + _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())


def unsign(secret: str, kind: str, value: str) -> dict[str, Any] | None:
    body, _, mac = (value or "").partition(".")
    try:
        if not hmac.compare_digest(_unb64(mac), hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()):
            return None
        payload = json.loads(_unb64(body))
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("k") != kind:
        return None
    if "exp" in payload and payload["exp"] < now():
        return None
    return payload


def now() -> int:
    return int(clock.now().timestamp())


def _client_ref(client_id: str) -> str:
    return hashlib.sha256(client_id.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------- discovery
def resource_metadata(base: str) -> dict[str, Any]:
    return {"resource": base + "/mcp", "authorization_servers": [base], "bearer_methods_supported": ["header"]}


def server_metadata(base: str) -> dict[str, Any]:
    return {"issuer": base, "authorization_endpoint": base + "/authorize", "token_endpoint": base + "/token",
            "registration_endpoint": base + "/register", "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"]}


# ---------------------------------------------------------------------- registration
def register(client_key: str, data: Any) -> dict[str, Any]:
    uris = data.get("redirect_uris") if isinstance(data, dict) else None
    if not (isinstance(uris, list) and uris and all(isinstance(u, str) and u for u in uris)):
        raise OAuthError("invalid_redirect_uri", "redirect_uris must list the client's redirect URIs")
    name = str(data.get("client_name") or "An MCP client")[:80]
    return {"client_id": sign(client_key, "client", {"uris": uris, "name": name}), "client_id_issued_at": now(),
            "client_name": name, "redirect_uris": uris, "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none"}


def client(client_key: str, client_id: str) -> dict[str, Any]:
    found = unsign(client_key, "client", client_id)
    if found is None:
        raise OAuthError("invalid_client", "this client is not registered here", 401)
    return found


# ---------------------------------------------------------------------- authorization
def check_request(client_key: str, params: dict[str, str]) -> dict[str, Any]:
    """The client asking (its name and redirect URI); a request that cannot be answered by
    redirecting to the client raises."""
    found = client(client_key, params.get("client_id", ""))
    if params.get("redirect_uri") not in found["uris"]:
        raise OAuthError("invalid_request", "redirect_uri is not one the client registered")
    if params.get("response_type") != "code":
        raise OAuthError("unsupported_response_type", "only response_type=code")
    if not params.get("code_challenge") or params.get("code_challenge_method", "S256") != "S256":
        raise OAuthError("invalid_request", "a code_challenge with method S256 is required")
    return found


def issue_code(grant_key: str, params: dict[str, str]) -> str:
    return sign(grant_key, "code", {"c": _client_ref(params["client_id"]), "r": params["redirect_uri"],
                                    "p": params["code_challenge"], "exp": now() + CODE_SECONDS})


# ---------------------------------------------------------------------- tokens
def exchange(grant_key: str | None, form: dict[str, str]) -> dict[str, Any]:
    grant = form.get("grant_type")
    ref = _client_ref(form.get("client_id", ""))
    if grant == "authorization_code":
        code = unsign(grant_key, "code", form.get("code", "")) if grant_key else None
        verifier = form.get("code_verifier", "")
        if (code is None or code["c"] != ref or code["r"] != form.get("redirect_uri")
                or _b64(hashlib.sha256(verifier.encode()).digest()) != code["p"]):
            raise OAuthError("invalid_grant", "the code is not valid for this client and verifier")
    elif grant == "refresh_token":
        refresh = unsign(grant_key, "refresh", form.get("refresh_token", "")) if grant_key else None
        if refresh is None or (form.get("client_id") and refresh["c"] != ref):
            raise OAuthError("invalid_grant", "the refresh token is no longer valid; sign in again")
        ref = refresh["c"]
    else:
        raise OAuthError("unsupported_grant_type")
    assert grant_key is not None
    return {"access_token": sign(grant_key, "access", {"c": ref, "exp": now() + ACCESS_SECONDS}),
            "token_type": "Bearer", "expires_in": ACCESS_SECONDS,
            "refresh_token": sign(grant_key, "refresh", {"c": ref, "exp": now() + REFRESH_SECONDS})}


def access_ok(grant_key: str | None, value: str) -> bool:
    return grant_key is not None and unsign(grant_key, "access", value) is not None
