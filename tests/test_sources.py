"""Source adapters against recorded-shape API responses.

TEST DOUBLES: ``FakeHttp`` / ``FakeGitHub`` return hand-written payloads shaped like the Gmail,
Slack and GitHub REST responses; they are not the real services. Real read-only runs are done
separately in a sandbox workspace.
"""

import base64
import json

from todo_manager import clock
from todo_manager.net import HttpError
from todo_manager.sources.github import GitHubNotificationSource
from todo_manager.sources.gmail import GmailSource
from todo_manager.sources.slack import SlackSource
from tests.helpers import WorkspaceCase


class FakeHttp:
    """Routes (method, url) to canned responses; a callable route receives the params."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, headers=None, params=None, timeout=None, body=None):
        self.calls.append((method, url, dict(params or {})))
        for prefix, response in self.routes:
            if url.endswith(prefix) or prefix in url:
                result = response(params or {}) if callable(response) else response
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"unexpected request {method} {url}")


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


def gmail_message(mid, thread, *, sender="Alice <alice@x.org>", labels=("INBOX",), body="Hello", extra=()):
    headers = [{"name": "From", "value": sender}, {"name": "To", "value": "me@example.edu"},
               {"name": "Subject", "value": f"Subject {mid}"}, {"name": "Message-ID", "value": f"<{mid}@x.org>"},
               *extra]
    return {"id": mid, "threadId": thread, "labelIds": list(labels),
            "internalDate": str(int(clock.now().timestamp() * 1000)),
            "payload": {"mimeType": "text/plain", "headers": headers, "body": {"data": b64(body)}}}


class GmailAdapterTests(WorkspaceCase):
    def source(self, http):
        return GmailSource(account="primary", address="me@example.edu", token=lambda: "t", my_addresses=[],
                           http=http, initial_lookback_days=3)

    def test_normalize_marks_own_bulk_excluded_and_github_mail(self):
        src = self.source(FakeHttp([]))
        mine = src.normalize(gmail_message("m1", "t1", sender="Me <me@example.edu>", labels=("SENT",)))
        self.assertTrue(mine.author_is_me)
        bulk = src.normalize(gmail_message("m2", "t2", extra=[{"name": "List-Unsubscribe", "value": "<x>"}]))
        self.assertTrue(bulk.meta["bulk_hint"])
        self.assertIsNone(src.normalize(gmail_message("m3", "t3", labels=("SPAM",))))
        gh = src.normalize(gmail_message("m4", "t4", sender="Bob <notifications@github.com>", extra=[
            {"name": "Message-ID", "value": "<octo/repo/pull/7/c123@github.com>"}]))
        self.assertEqual(gh.thread_key, "github:octo/repo#7")

    def test_expired_history_falls_back_to_a_paginated_window(self):
        pages = {None: {"messages": [{"id": "m1", "threadId": "t1"}], "nextPageToken": "p2"},
                 "p2": {"messages": [{"id": "m2", "threadId": "t2"}, {"id": "m3", "threadId": "t1"}]}}
        http = FakeHttp([
            ("/history", HttpError(404, "Requested entity was not found.")),
            ("/profile", {"historyId": "900"}),
            ("/messages", lambda p: pages[p.get("pageToken")]),
            ("/threads/t1", {"messages": [gmail_message("m1", "t1"), gmail_message("m3", "t1")]}),
            ("/threads/t2", {"messages": [gmail_message("m2", "t2")]}),
        ])
        result = self.source(http).fetch(json.dumps({"history_id": "100", "since": clock.iso()}),
                                         last_success_at=clock.iso())
        self.assertEqual(result.meta["mode"], "window_fallback")
        self.assertEqual(sorted(i.external_id for i in result.items), ["m1", "m2", "m3"])
        self.assertEqual(json.loads(result.cursor)["history_id"], "900")

    def test_other_errors_are_failures_not_empty_results(self):
        http = FakeHttp([("/history", HttpError(503, "backend error", retry_after=30))])
        with self.assertRaises(Exception) as ctx:
            self.source(http).fetch(json.dumps({"history_id": "100"}))
        self.assertIn("503", str(ctx.exception))


class FakeGitHub:
    def __init__(self, notifications, objects):
        self.notifications = notifications
        self.objects = objects

    def paged(self, path, params=None, max_pages=50):
        assert path == "notifications"
        return self.notifications

    def get(self, path, params=None):
        return self.objects[path]


def notification(nid, reason, subject_url, latest_url, title="Fix it"):
    return {"id": nid, "reason": reason, "updated_at": "2026-10-05T00:00:00Z",
            "repository": {"full_name": "octo/repo"},
            "subject": {"type": "PullRequest", "title": title, "url": subject_url, "latest_comment_url": latest_url}}


class GitHubNotificationTests(WorkspaceCase):
    PR = "https://api.github.com/repos/octo/repo/pulls/7"

    def fetch(self, notes, objects):
        src = GitHubNotificationSource(FakeGitHub(notes, objects), set(), ["me-gh"], bot_logins=["dependabot[bot]"])
        return src.fetch(None).items

    def test_own_merged_pr_is_marked_as_mine_with_its_state(self):
        pr = {"user": {"login": "me-gh"}, "state": "closed", "merged": True, "body": "Adds the loader"}
        [item] = self.fetch([notification("n1", "state_change", self.PR, self.PR)], {"repos/octo/repo/pulls/7": pr})
        self.assertEqual(item.author, "me-gh")
        self.assertTrue(item.author_is_me)
        self.assertIn("merged", item.body)
        self.assertIn("opened by me-gh", item.body)

    def test_latest_comment_by_someone_else_is_attributed_to_them(self):
        pr = {"user": {"login": "me-gh"}, "state": "open", "merged": False, "body": ""}
        comment = {"user": {"login": "alice"}, "body": "Could you rebase?"}
        [item] = self.fetch([notification("n2", "comment", self.PR,
                                          "https://api.github.com/repos/octo/repo/issues/comments/5")],
                            {"repos/octo/repo/pulls/7": pr, "repos/octo/repo/issues/comments/5": comment})
        self.assertEqual((item.author, item.author_is_me), ("alice", False))
        self.assertIn("Could you rebase?", item.body)
        self.assertIn("state: open", item.body)

    def test_bot_activity_is_context_only(self):
        pr = {"user": {"login": "dependabot[bot]", "type": "Bot"}, "state": "open", "merged": False, "body": "bump"}
        [item] = self.fetch([notification("n3", "author", self.PR, self.PR)], {"repos/octo/repo/pulls/7": pr})
        self.assertEqual(item.route, "context")


class SlackAdapterTests(WorkspaceCase):
    NO_USERS = ("users.info", {"ok": False, "error": "user_not_found"})

    def source(self, http):
        return SlackSource(token=lambda: "t", team="T1", conversation="C1", my_user_ids=["UME"], http=http,
                           page_size=2)

    def test_threads_pagination_edits_and_deletions(self):
        now = clock.now().timestamp()
        top_pages = {None: {"ok": True, "messages": [
                        {"ts": f"{now - 60:.6f}", "user": "U2", "text": "lunch?"},
                        {"ts": f"{now - 50:.6f}", "user": "U2", "text": "can you review the draft?",
                         "thread_ts": f"{now - 50:.6f}", "reply_count": 1, "latest_reply": f"{now - 40:.6f}"}],
                        "response_metadata": {"next_cursor": "c2"}},
                     "c2": {"ok": True, "messages": [
                         {"ts": f"{now - 30:.6f}", "user": "UME", "text": "sure", "edited": {"ts": f"{now - 20:.6f}"}},
                         {"ts": f"{now - 25:.6f}", "subtype": "channel_join", "user": "U3"}]}}
        http = FakeHttp([
            ("conversations.history", lambda p: top_pages[p.get("cursor")]),
            ("conversations.replies", {"ok": True, "messages": [
                {"ts": f"{now - 50:.6f}", "user": "U2", "text": "parent"},
                {"ts": f"{now - 40:.6f}", "user": "UME", "text": "will do by Friday"}]}),
            self.NO_USERS,
        ])
        result = self.source(http).fetch(None)
        keys = {i.external_id: i for i in result.items}
        self.assertEqual(len(keys), 4, "join message skipped, thread reply added")
        reply = keys[f"C1:{now - 40:.6f}"]
        self.assertEqual(reply.thread_key, f"slack:C1:{now - 50:.6f}")
        self.assertTrue(reply.author_is_me)
        self.assertEqual(keys[f"C1:{now - 60:.6f}"].thread_key, "slack:C1", "unthreaded chat keyed per channel")
        self.assertEqual(keys[f"C1:{now - 30:.6f}"].kind, "edit")
        # Next run: the unthreaded message vanished inside the re-read window -> a deletion item.
        http2 = FakeHttp([("conversations.history", {"ok": True, "messages": [
            m for m in top_pages[None]["messages"][1:] + top_pages["c2"]["messages"]]}),
            ("conversations.replies", {"ok": True, "messages": [
                {"ts": f"{now - 50:.6f}", "user": "U2", "text": "parent"},
                {"ts": f"{now - 40:.6f}", "user": "UME", "text": "will do by Friday"}]}), self.NO_USERS])
        result2 = self.source(http2).fetch(result.cursor)
        deleted = [i for i in result2.items if i.kind == "deletion"]
        self.assertEqual([i.external_id for i in deleted], [f"C1:{now - 60:.6f}"])

    def test_authors_and_mentions_are_resolved_to_names(self):
        now = clock.now().timestamp()
        users = {"U2": {"ok": True, "user": {"id": "U2", "real_name": "Alice Chen", "profile": {"display_name": "alice"}}},
                 "UME": {"ok": True, "user": {"id": "UME", "real_name": "Test User", "profile": {}}}}
        http = FakeHttp([
            ("conversations.history", {"ok": True, "messages": [
                {"ts": f"{now - 60:.6f}", "user": "U2", "text": "<@UME> can you send the slides? cc <@U9>"}]}),
            ("users.info", lambda p: users.get(p["user"], {"ok": False, "error": "user_not_found"})),
        ])
        [item] = self.source(http).fetch(None).items
        self.assertEqual(item.author, "Alice Chen (U2)")
        self.assertIn("@Test User [ME] can you send the slides?", item.body)
        self.assertIn("<@U9>", item.body, "unknown users stay as ids")
        lookups = [c[2]["user"] for c in http.calls if "users.info" in c[1]]
        self.assertEqual(sorted(lookups), ["U2", "U9", "UME"], "each user looked up once, unknown ones too")

    def test_api_error_is_a_failure(self):
        http = FakeHttp([("conversations.history", {"ok": False, "error": "not_in_channel"})])
        with self.assertRaises(Exception) as ctx:
            self.source(http).fetch(None)
        self.assertIn("not_in_channel", str(ctx.exception))


class SlackRegistryTests(WorkspaceCase):
    def test_token_can_come_from_a_private_file(self):
        from todo_manager.sources import registry
        from todo_manager.sources.base import SourceError
        token_file = self.tmp / "slack_user_token"
        token_file.write_text("xoxp-test-token\n", encoding="utf-8")
        self.cfg.data["slack"].update(enabled=True, token_file=str(token_file), conversations=["C1"], team="T1")
        [src] = [s for s in registry.conversation_sources(self.cfg) if s.name == "slack"]
        self.assertEqual(src.token(), "xoxp-test-token")
        token_file.unlink()
        with self.assertRaises(SourceError) as ctx:
            src.token()
        self.assertNotIn("xoxp", str(ctx.exception))
