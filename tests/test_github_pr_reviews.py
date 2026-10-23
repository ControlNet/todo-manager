"""Intake-2: a PR notification brings the review submissions and inline review comments, not
only the notification's latest comment and the issue-comment discussion.

* Reviews (``pulls/{n}/reviews``, filtered by ``submitted_at``) and inline review comments
  (``pulls/{n}/comments?since=``) inside the read window / since the previous view are items
  with the identities the tracked-repo reader uses (``review:<id>``, ``review_comment:<id>``):
  a person's is news (batch), a bot's is context.
* An empty ``COMMENTED`` review only wraps inline comments, which are items themselves.
* The matter's watermark is stored only when every stream was read; a failed stream fails the
  read and the next run repeats it.

TEST DOUBLE: ``FakeGitHubApi`` (tests/test_github_context.py) stands in for the GitHub REST API.
"""

from todo_manager import clock
from todo_manager.sources.base import SourceError, persist_fetch
from todo_manager.sources.github import GitHubNotificationSource
from tests.helpers import WorkspaceCase
from tests.test_github_context import REPO, FakeGitHubApi, ago


class PullRequestReviewTests(WorkspaceCase):
    PR = f"https://api.github.com/repos/{REPO}/pulls/9"

    def setUp(self):
        super().setUp()
        self.api = FakeGitHubApi()
        self.api.issue(9, title="Faster widget loader", body="ORIGINAL: replaces the CSV loader", user="me-gh",
                       created=ago(days=20), pr=True)
        self.api.comment(9, 90, "bob", "old discussion", ago(days=10))
        self.cursor = None

    def notify(self, latest=None, reason="author"):
        """``latest``: ("issues" | "pulls", comment id); none means the PR itself (a review)."""
        url = f"https://api.github.com/repos/{REPO}/{latest[0]}/comments/{latest[1]}" if latest else self.PR
        self.api.notifications = [{"id": "n1", "reason": reason, "updated_at": ago(minutes=1),
                                   "repository": {"full_name": REPO},
                                   "subject": {"type": "PullRequest", "title": "Faster widget loader",
                                               "url": self.PR, "latest_comment_url": url}}]

    def fetch(self):
        result = GitHubNotificationSource(self.api, set(), ["me-gh"]).fetch(self.cursor)
        self.cursor = result.cursor
        return result

    def routes(self, items):
        ids = [i.external_id for i in items]
        self.assertEqual(len(ids), len(set(ids)), f"each event once: {ids}")
        return {i.external_id: i.route for i in items}

    def reviewed(self):
        """Alice requests changes with two inline comments; Bob leaves one inline comment."""
        self.api.review(9, 700, "alice", "CHANGES_REQUESTED", "Please split the parser out.", ago(minutes=30))
        self.api.review_comment(9, 501, "alice", "loader.py", "This reads the whole file into memory.",
                                ago(minutes=30))
        self.api.review_comment(9, 502, "alice", "loader.py", "Missing a test for empty rows.", ago(minutes=30))
        self.api.review(9, 701, "bob", "COMMENTED", "", ago(minutes=25))
        self.api.review_comment(9, 503, "bob", "README.md", "Typo: widgte.", ago(minutes=25))

    def test_reviews_and_inline_comments_on_my_pr_are_read(self):
        self.reviewed()
        self.notify()
        items = self.fetch().items
        routes = self.routes(items)
        for ext in ("review:700", "review_comment:501", "review_comment:502", "review_comment:503"):
            self.assertEqual(routes.get(ext), "batch", ext)
        self.assertNotIn("review:701", routes, "an empty COMMENTED review only wraps inline comments")
        by_id = {i.external_id: i for i in items}
        self.assertIn("[CHANGES_REQUESTED] Please split the parser out.", by_id["review:700"].body)
        self.assertEqual(by_id["review_comment:501"].body, "loader.py: This reads the whole file into memory.")
        self.assertEqual((by_id["review:700"].kind, by_id["review_comment:501"].kind), ("review", "comment"))
        self.assertEqual((by_id["review:700"].meta["github_event"], by_id["review_comment:501"].meta["github_event"]),
                         ("review:700", "review_comment:501"), "the tracked-repo reader's identities")

    def test_the_watermark_prevents_re_emission(self):
        self.reviewed()
        self.notify()
        persist_fetch(self.conn, "github", self.fetch())
        clock.advance(hours=2)
        self.api.review_comment(9, 504, "alice", "loader.py", "Still unresolved?", ago(minutes=3))
        self.notify(("pulls", 504))
        result = self.fetch()
        routes = self.routes(result.items)
        self.assertNotIn("review:700", routes)
        self.assertFalse({"review_comment:501", "review_comment:502", "review_comment:503"} & set(routes))
        self.assertNotIn("review_comment:504", routes, "the notification's own latest comment")
        [note] = [i for i in result.items if i.kind == "notification"]
        self.assertEqual(note.meta["github_event"], "review_comment:504")
        clock.advance(hours=2)
        self.api.comment(9, 91, "bob", "Any update?", ago(minutes=2))
        self.notify(("issues", 91))
        self.assertNotIn("review_comment:504", self.routes(self.fetch().items), "delivered as a latest comment")

    def test_a_bot_review_is_context(self):
        self.notify(("issues", 90))
        self.fetch()
        clock.advance(hours=2)
        self.api.review(9, 710, "lint-bot", "COMMENTED", "Coverage dropped by 2%.", ago(minutes=10), bot=True)
        self.api.review(9, 711, "alice", "APPROVED", "", ago(minutes=5))
        self.notify()
        routes = self.routes(self.fetch().items)
        self.assertEqual(routes["review:710"], "context")
        self.assertEqual(routes["review:711"], "batch", "an approval without text is still news")

    def test_reviews_mix_with_issue_comments(self):
        self.notify(("issues", 90))
        self.fetch()
        clock.advance(hours=2)
        self.api.comment(9, 600, "bob", "Can you rebase?", ago(minutes=20))
        self.api.review_comment(9, 601, "alice", "loader.py", "Rename this.", ago(minutes=15))
        self.api.review(9, 720, "alice", "APPROVED", "LGTM after the rename.", ago(minutes=10))
        self.api.review_comment(9, 600, "alice", "cli.py", "Same id as Bob's issue comment.", ago(minutes=5))
        self.api.comment(9, 602, "bob", "@me-gh merging tomorrow", ago(minutes=2))
        self.notify(("issues", 602))
        routes = self.routes(self.fetch().items)
        self.assertEqual({k: v for k, v in routes.items() if k != "notification:n1"},
                         {"comment:600": "batch", "review_comment:601": "batch", "review:720": "batch",
                          "review_comment:600": "batch"}, "issue and review comment ids are separate")

    def test_a_failed_stream_does_not_advance_coverage(self):
        self.notify(("issues", 90))
        self.fetch()
        before = self.cursor
        clock.advance(hours=2)
        self.reviewed()
        self.notify()
        self.api.fail[f"repos/{REPO}/pulls/9/reviews"] = SourceError("HTTP 502: Bad Gateway")
        with self.assertRaises(SourceError):
            self.fetch()
        self.assertEqual(self.cursor, before, "the matter's watermark stays")
        del self.api.fail[f"repos/{REPO}/pulls/9/reviews"]
        clock.advance(minutes=20)
        routes = self.routes(self.fetch().items)
        self.assertEqual(routes.get("review:700"), "batch")
        self.assertEqual(routes.get("review_comment:501"), "batch")

    def test_request_volume(self):
        self.notify(("issues", 90))
        self.fetch()
        self.assertEqual([c for c in self.api.calls if "/pulls/9/" in c], [f"repos/{REPO}/pulls/9/reviews"],
                         "no inline comments listed while the PR has none")
        self.api.calls.clear()
        self.reviewed()
        clock.advance(hours=2)
        self.notify()
        self.fetch()
        self.assertEqual(sorted(c for c in self.api.calls if "/pulls/9/" in c),
                         [f"repos/{REPO}/pulls/9/comments", f"repos/{REPO}/pulls/9/reviews"])

    def test_an_issue_notification_reads_no_pull_streams(self):
        self.api.issue(7, title="Crash", body="crashes", created=ago(days=3))
        self.api.comment(7, 77, "bob", "@me-gh see this", ago(minutes=2))
        self.api.notifications = [{"id": "n2", "reason": "mention", "updated_at": ago(minutes=1),
                                   "repository": {"full_name": REPO},
                                   "subject": {"type": "Issue", "title": "Crash",
                                               "url": f"https://api.github.com/repos/{REPO}/issues/7",
                                               "latest_comment_url": f"https://api.github.com/repos/{REPO}/issues/comments/77"}}]
        self.fetch()
        self.assertFalse([c for c in self.api.calls if "/pulls/" in c])

    def test_a_re_read_inside_the_overlap_is_stored_once(self):
        self.api.review(9, 730, "alice", "CHANGES_REQUESTED", "One more thing.", ago(minutes=2))
        self.api.review_comment(9, 530, "alice", "loader.py", "Close the file.", ago(minutes=2))
        self.notify()
        persist_fetch(self.conn, "github", self.fetch())
        clock.advance(minutes=5)            # re-read from before the watermark, dropped by id when stored
        self.notify()
        persist_fetch(self.conn, "github", self.fetch())
        rows = self.conn.execute("SELECT external_id FROM source_items WHERE external_id LIKE 'review%'"
                                 " ORDER BY external_id").fetchall()
        self.assertEqual([r[0] for r in rows], ["review:730", "review_comment:530"])
