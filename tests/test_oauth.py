"""MCP sign-in for clients that only speak OAuth (docs/api.md): such a client registers itself, the
user allows it with the web password, and the access token it gets is one more way in beside the
web token and the password, which keep working."""

import base64
import hashlib
import http.client
import json
from urllib.parse import parse_qs, urlencode, urlsplit

from todo_manager import clock
from todo_manager.web import server
from tests.test_api import ApiCase

PASSWORD = "plum orchard 42"
REDIRECT = "https://chatgpt.example/connector/oauth_callback"
VERIFIER = "kZ3vQm8pT1xW5yB7nC2dF4gH6jK9mN0pR3sU5vX8zA1"


def challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class OAuthCase(ApiCase):
    def setUp(self):
        super().setUp()
        server.set_password(self.cfg.home, PASSWORD)
        self.base = f"http://127.0.0.1:{self.site.port}"

    def raw(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.site.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response, data

    def form(self, path, fields, headers=None):
        return self.raw("POST", path, urlencode(fields),
                        {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})

    def register(self, name="ChatGPT"):
        response, data = self.raw("POST", "/register", json.dumps({"client_name": name, "redirect_uris": [REDIRECT]}),
                                  {"Content-Type": "application/json"})
        self.assertEqual(response.status, 201, data)
        return json.loads(data)["client_id"]

    def request(self, client, **extra):
        return {"response_type": "code", "client_id": client, "redirect_uri": REDIRECT, "state": "s-123",
                "code_challenge": challenge(VERIFIER), "code_challenge_method": "S256", **extra}

    def allow(self, client, password=PASSWORD, decision="allow", headers=None):
        return self.form("/authorize", {**self.request(client), "password": password, "decision": decision}, headers)[0]

    def code(self, client, password=PASSWORD):
        response = self.allow(client, password=password)
        self.assertEqual(response.status, 302)
        location = response.getheader("Location")
        self.assertTrue(location.startswith(REDIRECT + "?"), location)
        query = parse_qs(urlsplit(location).query)
        self.assertEqual(query["state"], ["s-123"])
        return query["code"][0]

    def exchange(self, fields):
        response, data = self.form("/token", fields)
        return response.status, json.loads(data)

    def tokens(self, client):
        status, body = self.exchange({"grant_type": "authorization_code", "code": self.code(client),
                                      "redirect_uri": REDIRECT, "client_id": client, "code_verifier": VERIFIER})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["token_type"], "Bearer")
        return body

    def ping(self, token):
        return self.call("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=token)[0]


class DiscoveryTests(OAuthCase):
    def test_a_client_without_a_token_is_told_where_to_sign_in(self):
        response, _ = self.raw("POST", "/mcp", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}))
        self.assertEqual(response.status, 401)
        self.assertIn(f'resource_metadata="{self.base}/.well-known/oauth-protected-resource"',
                      response.getheader("WWW-Authenticate"))
        for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
            response, data = self.raw("GET", path)
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(data)["authorization_servers"], [self.base])
        response, data = self.raw("GET", "/.well-known/oauth-authorization-server")
        meta = json.loads(data)
        self.assertEqual((meta["issuer"], meta["token_endpoint"], meta["registration_endpoint"]),
                         (self.base, self.base + "/token", self.base + "/register"))
        self.assertEqual(meta["code_challenge_methods_supported"], ["S256"])


class SignInTests(OAuthCase):
    def test_the_user_allows_a_client_and_its_token_reaches_mcp_and_the_api(self):
        response, data = self.raw("GET", "/authorize?" + urlencode(self.request(self.register())))
        self.assertEqual(response.status, 200)
        page = data.decode()
        self.assertIn("ChatGPT", page)
        self.assertIn('name="password"', page)
        access = self.tokens(self.register())["access_token"]
        self.assertEqual(self.ping(access), 200)
        self.assertEqual(self.call("GET", "/api/home", token=access)[0], 200)

    def test_the_web_token_and_password_still_sign_agents_in(self):
        self.tokens(self.register())
        self.assertEqual(self.ping(self.token), 200)
        self.assertEqual(self.ping(PASSWORD), 200)

    def test_a_signed_in_browser_allows_without_the_password(self):
        client = self.register()
        cookie = {"Cookie": f"{server.COOKIE}={server.session_value(server.session_key(self.cfg.home))}"}
        _, data = self.raw("GET", "/authorize?" + urlencode(self.request(client)), headers=cookie)
        self.assertNotIn('name="password"', data.decode())
        self.assertEqual(self.allow(client, password="", headers=cookie).status, 302)

    def test_a_refresh_token_brings_a_new_access_token(self):
        client = self.register()
        refresh = self.tokens(client)["refresh_token"]
        clock.advance(hours=2)
        status, body = self.exchange({"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.ping(body["access_token"]), 200)


class RefusalTests(OAuthCase):
    def test_a_wrong_password_shows_the_page_again(self):
        response = self.allow(self.register(), password="plum orchard 41")
        self.assertEqual(response.status, 200)
        self.assertIsNone(response.getheader("Location"))

    def test_cancelling_tells_the_client(self):
        response = self.allow(self.register(), decision="deny")
        self.assertEqual(response.status, 302)
        self.assertEqual(parse_qs(urlsplit(response.getheader("Location")).query)["error"], ["access_denied"])

    def test_an_unregistered_redirect_or_client_is_refused_without_redirecting(self):
        client = self.register()
        response, _ = self.raw("GET", "/authorize?" + urlencode(self.request(client, redirect_uri="https://elsewhere.example/cb")))
        self.assertEqual(response.status, 400)
        response, _ = self.raw("GET", "/authorize?" + urlencode(self.request("not-a-client")))
        self.assertEqual(response.status, 400)

    def test_a_code_needs_its_verifier(self):
        client = self.register()
        status, body = self.exchange({"grant_type": "authorization_code", "code": self.code(client),
                                      "redirect_uri": REDIRECT, "client_id": client, "code_verifier": "x" * 43})
        self.assertEqual((status, body["error"]), (400, "invalid_grant"))

    def test_an_access_token_expires(self):
        access = self.tokens(self.register())["access_token"]
        clock.advance(hours=2)
        self.assertEqual(self.ping(access), 401)

    def test_a_new_password_or_token_signs_clients_out_but_keeps_them_registered(self):
        client = self.register()
        first = self.tokens(client)
        server.set_password(self.cfg.home, "new password 7")
        self.assertEqual(self.ping(first["access_token"]), 401)
        status, body = self.exchange({"grant_type": "refresh_token", "refresh_token": first["refresh_token"],
                                      "client_id": client})
        self.assertEqual((status, body["error"]), (400, "invalid_grant"))
        access = self.exchange({"grant_type": "authorization_code", "code": self.code(client, "new password 7"),
                                "redirect_uri": REDIRECT, "client_id": client, "code_verifier": VERIFIER})[1]["access_token"]
        server.new_token(self.cfg.home)
        self.assertEqual(self.ping(access), 401)
