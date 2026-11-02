"""GitHub PRs and issues that open tasks wait for: their state is read directly.

GitHub does not notify the actor of their own action, and the notifications ``since`` filter
skips a thread whose only change is the user's own merge or close (its shown ``updated_at``
moves, the filter does not). A task waiting for the user's own PR or issue therefore never saw
it merged or closed. The watch source reads the state of every matter an open task is linked
to or names, and reports a change of state as news for that task.

TEST DOUBLE: ``FakeGitHubApi`` (from test_github_context) stands in for the GitHub REST API.
"""

import json
from datetime import timedelta

from todo_manager import clock, gate_intake, store
from todo_manager.sources import registry
from todo_manager.sources.base import FetchResult, Item, SourceError, open_task_matters, persist_fetch
from todo_manager.sources.github import GitHubWatchSource
from tests.helpers import WorkspaceCase
from tests.test_github_context import REPO, FakeGitHubApi, ago

KEY = f"github:{REPO}#9"


def merge(api, number, *, by="me-gh", at=None):
    at = at or ago(minutes=2)
    api.issues[number].update(state="closed", closed_at=at, updated_at=at, closed_by={"login": by},
                              pull_request={"merged_at": at})


def close(api, number, *, by="me-gh", reason="completed", at=None):
    at = at or ago(minutes=2)
    api.issues[number].update(state="closed", closed_at=at, updated_at=at, closed_by={"login": by},
                              state_reason=reason)


class WatchSourceTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.api = FakeGitHubApi()
        self.matters = [KEY]
        self.cursor = None

    def fetch(self, tracked=()):
        source = GitHubWatchSource(self.api, set(tracked), ["me-gh"], [], matters=lambda: list(self.matters))
        result = source.fetch(self.cursor)
        self.cursor = result.cursor
        return result

    def test_the_users_own_merge_is_reported_once(self):
        self.api.issue(9, title="Refresh metrics", body="b", user="me-gh", created=ago(days=1), pr=True)
        self.assertEqual(self.fetch().items, [], "an open PR seen for the first time is no news")
        merge(self.api, 9)
        [item] = self.fetch().items
        self.assertEqual((item.kind, item.route, item.thread_key, item.author, item.author_is_me),
                         ("event", "if_open_task", KEY, "me-gh", True))
        self.assertEqual((item.meta["state"], item.meta["is_pr"]), ("merged", True))
        self.assertIn("merged", item.body)
        self.assertTrue(item.title.startswith(f"PullRequest {REPO}#9"))
        self.assertEqual(self.fetch().items, [], "an unchanged state is not reported again")

    def test_a_matter_already_merged_when_first_watched_is_reported(self):
        self.api.issue(9, title="Refresh metrics", body="b", user="me-gh", created=ago(days=1), pr=True)
        merge(self.api, 9)
        [item] = self.fetch().items
        self.assertEqual(item.meta["state"], "merged")

    def test_issue_closed_reopened_and_closed_again(self):
        self.api.issue(9, title="Loader crashes", body="b", created=ago(days=3))
        self.fetch()
        close(self.api, 9, by="alice", reason="not_planned")
        [closed] = self.fetch().items
        self.assertEqual((closed.meta["state"], closed.meta["state_reason"], closed.meta["is_pr"], closed.author),
                         ("closed", "not_planned", False, "alice"))
        self.assertIn("not_planned", closed.body)
        self.assertTrue(closed.title.startswith(f"Issue {REPO}#9"))
        clock.advance(minutes=20)
        self.api.issues[9].update(state="open", closed_at=None, closed_by=None, state_reason="reopened",
                                  updated_at=ago(minutes=1))
        [reopened] = self.fetch().items
        self.assertEqual(reopened.meta["state"], "open")
        clock.advance(minutes=20)
        close(self.api, 9, reason="completed")
        [again] = self.fetch().items
        self.assertEqual(again.meta["state"], "closed")
        self.assertEqual(len({closed.external_id, reopened.external_id, again.external_id}), 3)

    def test_tracked_repositories_are_left_to_their_events(self):
        self.api.issue(9, title="t", body="b", created=ago(days=1), pr=True)
        merge(self.api, 9)
        self.assertEqual(self.fetch(tracked=[REPO.upper()]).items, [])
        self.assertNotIn(f"repos/{REPO}/issues/9", self.api.calls)

    def test_a_deleted_matter_is_skipped_and_other_failures_fail_the_read(self):
        self.api.issue(9, title="t", body="b", created=ago(days=1))
        self.matters = [f"github:{REPO}#404", KEY]
        api_get = self.api.get

        def get(path, params=None):
            if path.endswith("/404"):
                raise SourceError("HTTP 404 Not Found")
            return api_get(path, params)

        self.api.get = get
        close(self.api, 9)
        self.assertEqual([i.meta["state"] for i in self.fetch().items], ["closed"])
        self.api.get = lambda path, params=None: (_ for _ in ()).throw(SourceError("HTTP 502 Bad Gateway"))
        with self.assertRaises(SourceError):
            self.fetch()

    def test_states_are_kept_only_for_watched_matters(self):
        self.api.issue(9, title="t", body="b", created=ago(days=1))
        self.fetch()
        self.matters = []
        self.fetch()
        self.assertEqual(json.loads(self.cursor)["states"], {})

    def test_read_every_watch_interval(self):
        source = GitHubWatchSource(self.api, set(), ["me-gh"], [], matters=lambda: [], watch_minutes=15)
        self.assertTrue(source.due(None))
        row = {"cursor": "{}", "last_success_at": clock.iso()}
        clock.advance(minutes=10)
        self.assertFalse(source.due(row))
        clock.advance(minutes=5)
        self.assertTrue(source.due(row))
        self.assertEqual(source.fetch(None).meta["poll_interval_minutes"], 15)


class OpenTaskMatterTests(WorkspaceCase):
    def linked_task(self, key, status="decision"):
        repo, number = key.removeprefix("github:").split("#")
        persist_fetch(self.conn, "github", FetchResult(scope="notifications", cursor="c", items=[Item(
            source="github", account=repo, external_id=f"pull:{number}", kind="opened", conversation_key=key,
            thread_key=key, occurred_at=clock.iso(), title="t", body="b")]))
        item_id = self.item_id(f"pull:{number}")
        task = store.create_task(self.conn, f"Decide on {key}", origin="intake", status=status)
        store.link_evidence(self.conn, "task", task, item_id, "origin")
        return task

    def test_matters_of_open_tasks_from_evidence_and_text(self):
        self.linked_task("github:octo/repo#9")
        self.linked_task("github:octo/repo#10", status="done")
        store.create_task(self.conn, "Wait for the upstream fix", origin="intake", status="waiting",
                          next_step="see https://github.com/Acme-Lab/Tools/issues/31 and acme-lab/data.sets#4")
        store.create_task(self.conn, "Old https://github.com/acme-lab/tools/pull/77", origin="intake",
                          status="cancelled")
        self.assertEqual(sorted(open_task_matters(self.conn)),
                         ["github:acme-lab/data.sets#4", "github:acme-lab/tools#31", "github:octo/repo#9"])

    def test_the_merge_of_a_watched_pr_reaches_the_waiting_task(self):
        self.linked_task(KEY)
        api = FakeGitHubApi()
        api.issue(9, title="t", body="b", user="me-gh", created=ago(days=1), pr=True)
        merge(api, 9)
        source = GitHubWatchSource(api, set(), ["me-gh"], [], matters=lambda: open_task_matters(self.conn))
        report = gate_intake.fetch_all(self.conn, [source])
        self.assertEqual(report["github:watched"]["batched"], 1)

    def test_production_wiring_watches_open_tasks(self):
        self.cfg.data["github"].update({"enabled": True, "repos": []})
        self.assertNotIn("watched", [a.scope for a in registry.conversation_sources(self.cfg)],
                         "without the database there is nothing to watch")
        [source] = [a for a in registry.conversation_sources(self.cfg, self.conn) if a.scope == "watched"]
        self.linked_task(KEY)
        self.assertEqual(source.matters(), [KEY])
