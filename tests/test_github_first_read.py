"""C11: on the first read of a GitHub matter (or again after it was idle for
``REACTIVATE_DAYS``), what happened inside the read window is news and only what came before
it is history.

* The window starts where this read starts (the cursor's ``since``; the lookback on a first
  run). A person's comment in the window is news (batch) even when a bot commented after it -
  however many bot comments follow; bots are context. The opening post is news when it was
  written in the window by a person and is not already the notification's own text.
* Before the window: the opening post and the last ``CONTEXT_COMMENTS`` comments are history
  (context); nothing older is turned into a todo.
* A comment stored earlier as context does not block the same comment from becoming news
  later (``persist_fetch`` promotes it into a batch once).

TEST DOUBLE: ``FakeGitHubApi`` (tests/test_github_context.py) stands in for the GitHub REST API.
"""

import json

from todo_manager import clock
from todo_manager.sources.base import FetchResult, Item, persist_fetch
from todo_manager.sources.github import GitHubNotificationSource
from tests.helpers import WorkspaceCase
from tests.test_github_context import REPO, FakeGitHubApi, ago, by_kind


class FirstReadTests(WorkspaceCase):
    URL = f"https://api.github.com/repos/{REPO}/issues/7"

    def setUp(self):
        super().setUp()
        self.api = FakeGitHubApi()
        self.api.issue(7, title="Crash on load", body="ORIGINAL: crashes with empty config", created=ago(days=40))
        for n in range(4):
            self.api.comment(7, 70 + n, "carol", f"old comment {n}", ago(days=30 - n))
        self.cursor = None

    def bot(self, cid, minutes):
        self.api.comment(7, cid, "ci-bot", f"CI run {cid}", ago(minutes=minutes))
        self.api.comments[7][-1]["user"]["type"] = "Bot"

    def notify(self, latest_cid, url=None):
        url = url or self.URL
        self.api.notifications = [{"id": "n1", "reason": "mention", "updated_at": ago(minutes=1),
                                   "repository": {"full_name": REPO},
                                   "subject": {"type": "Issue", "title": "Crash on load", "url": url,
                                               "latest_comment_url": f"https://api.github.com/repos/{REPO}/issues/"
                                                                     f"comments/{latest_cid}" if latest_cid else url}}]

    def fetch(self):
        result = GitHubNotificationSource(self.api, set(), ["me-gh"]).fetch(self.cursor)
        self.cursor = result.cursor
        return result

    def routes(self, items):
        return {i.external_id: i.route for i in items}

    def test_a_bot_comment_after_the_request_does_not_hide_it_on_first_read(self):
        self.api.comment(7, 99, "dave", "@me-gh please review the fix", ago(minutes=20))
        self.bot(100, 1)
        self.notify(100)
        result = self.fetch()
        routes = self.routes(result.items)
        self.assertEqual(routes["notification:n1"], "context", "the bot's comment itself is context")
        self.assertEqual(routes["comment:99"], "batch", "the person's request is news")
        self.assertEqual(routes["issue:1007"], "context", "the 40-day-old opening post is history")
        self.assertEqual([k for k, r in routes.items() if k.startswith("comment:7")],
                         ["comment:71", "comment:72", "comment:73"])
        self.assertEqual(set(routes[k] for k in ("comment:71", "comment:72", "comment:73")), {"context"})
        stats = persist_fetch(self.conn, "github", result)
        self.assertEqual(stats["batched"], 1)

    def test_consecutive_bot_comments(self):
        self.api.comment(7, 99, "dave", "@me-gh please review the fix", ago(minutes=30))
        for cid, minutes in ((100, 20), (101, 10), (102, 1)):
            self.bot(cid, minutes)
        self.notify(102)
        routes = self.routes(self.fetch().items)
        self.assertEqual(routes["comment:99"], "batch")
        self.assertEqual((routes["comment:100"], routes["comment:101"]), ("context", "context"))

    def test_a_request_further_back_than_the_last_three_comments(self):
        self.api.comment(7, 99, "dave", "@me-gh can you take this?", ago(minutes=60))
        for n in range(4):
            self.bot(100 + n, 40 - 10 * n)
        self.notify(103)
        routes = self.routes(self.fetch().items)
        self.assertEqual(routes.get("comment:99"), "batch", "not dropped behind the bots")

    def test_older_history_is_not_turned_into_todos(self):
        self.api.comment(7, 90, "dave", "@me-gh could you check this sometime?", ago(days=20))
        self.api.comment(7, 99, "erin", "any news?", ago(minutes=5))
        self.notify(99)
        routes = self.routes(self.fetch().items)
        self.assertEqual(routes["comment:90"], "context", "before the read window: history")
        self.assertEqual(routes["notification:n1"], "batch")

    def test_an_issue_opened_in_the_window_with_a_bot_reply(self):
        self.api.issue(8, title="Need your sign-off", body="@me-gh please sign off the release", user="dave",
                       created=ago(minutes=15))
        self.bot_on(8, 800, 1)
        self.notify(800, url=f"https://api.github.com/repos/{REPO}/issues/8")
        routes = self.routes(self.fetch().items)
        self.assertEqual(routes["issue:1008"], "batch", "the opening post is the request")
        self.assertEqual(routes["notification:n1"], "context")

    def bot_on(self, number, cid, minutes):
        self.api.comment(number, cid, "ci-bot", "labelled", ago(minutes=minutes))
        self.api.comments[number][-1]["user"]["type"] = "Bot"

    def test_reactivation_after_a_long_pause_reads_the_window_too(self):
        self.api.comment(7, 99, "erin", "@me-gh fyi", ago(minutes=1))
        self.notify(99)
        persist_fetch(self.conn, "github", self.fetch())
        clock.advance(days=30)                          # idle longer than REACTIVATE_DAYS
        self.api.comment(7, 500, "dave", "@me-gh is this still broken for you?", ago(minutes=20))
        self.bot(501, 1)
        self.notify(501)
        result = self.fetch()
        routes = self.routes(result.items)
        self.assertEqual(routes["comment:500"], "batch")
        self.assertNotEqual(routes.get("comment:99"), "batch", "delivered a month ago as the notification")
        self.assertEqual(routes["issue:1007"], "context")

    def test_incremental_reads_keep_their_rule(self):
        self.api.comment(7, 99, "erin", "@me-gh fyi", ago(minutes=1))
        self.notify(99)
        self.fetch()
        clock.advance(hours=2)
        self.api.comment(7, 600, "dave", "@me-gh please look", ago(minutes=20))
        self.bot(601, 1)
        self.notify(601)
        routes = self.routes(self.fetch().items)
        self.assertEqual(routes["comment:600"], "batch")
        self.assertNotIn("issue:1007", routes, "known matter: no body replay")


class PromotionTests(WorkspaceCase):
    def item(self, route):
        return Item(source="github", account=REPO, external_id="comment:99", kind="comment",
                    conversation_key=f"github:{REPO}#7", thread_key=f"github:{REPO}#7", occurred_at=ago(minutes=20),
                    author="dave", body="@me-gh please review the fix", route=route,
                    meta={"github_event": "comment:99"})

    def test_a_context_row_does_not_block_the_same_comment_as_news(self):
        persist_fetch(self.conn, "github", FetchResult(scope="notifications", items=[self.item("context")], cursor=None))
        stats = persist_fetch(self.conn, "github", FetchResult(scope="notifications", items=[self.item("batch")],
                                                               cursor=None))
        self.assertEqual(stats["batched"], 1)
        row = self.conn.execute("SELECT state, batch_id, meta FROM source_items WHERE external_id='comment:99'").fetchone()
        self.assertEqual(row["state"], "batched")
        self.assertIsNotNone(row["batch_id"])
        self.assertEqual(json.loads(row["meta"])["route"], "batch")
        again = persist_fetch(self.conn, "github", FetchResult(scope="notifications", items=[self.item("batch")],
                                                               cursor=None))
        self.assertEqual((again["batched"], again["duplicate"]), (0, 1), "promoted once")
