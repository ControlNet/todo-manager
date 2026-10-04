"""R12: context for GitHub activity on old issues/PRs (original body, recent discussion, state,
the user's participation), content-versioned body edits, and mail/API duplicates.

TEST DOUBLE: ``FakeGitHubApi`` is an in-memory stand-in for the GitHub REST endpoints the
adapters read (issues with ``since``, comments with ``since``/paging, events, reviews,
notifications, single issue/PR objects). It is not the real service.
"""

import re
from datetime import timedelta

from todo_manager import clock, gate_intake, intake
from todo_manager.db import rules_version
from todo_manager.sources.base import FetchResult, Item, persist_fetch
from todo_manager.sources.github import GitHubNotificationSource, GitHubRepoSource
from tests.helpers import WorkspaceCase
from tests.test_intake import run_id_of

REPO = "octo/repo"


def ago(**kw):
    return clock.iso(clock.now() - timedelta(**kw)).replace(".000Z", "Z")


class FakeGitHubApi:
    def __init__(self):
        self.issues: dict[int, dict] = {}
        self.comments: dict[int, list[dict]] = {}
        self.events: dict[int, list[dict]] = {}
        self.notifications: list[dict] = []
        self.calls: list[str] = []

    def issue(self, number, *, title, body, user="bob", created, pr=False, assignees=(), state="open"):
        self.issues[number] = {"number": number, "id": 1000 + number, "title": title, "body": body,
                               "user": {"login": user}, "state": state, "created_at": created,
                               "updated_at": created, "comments": 0,
                               "assignees": [{"login": a} for a in assignees], "labels": [],
                               "html_url": f"https://github.com/{REPO}/{'pull' if pr else 'issues'}/{number}",
                               **({"pull_request": {}} if pr else {})}
        self.comments[number] = []
        self.events[number] = []

    def comment(self, number, cid, user, body, at):
        self.comments[number].append({"id": cid, "user": {"login": user}, "body": body, "created_at": at,
                                      "updated_at": at, "html_url": f"https://github.com/{REPO}/issues/{number}#c{cid}"})
        self.issues[number]["comments"] = len(self.comments[number])
        self.touch(number, at)

    def event(self, number, eid, event, actor, at, **extra):
        self.events[number].append({"id": eid, "event": event, "actor": {"login": actor}, "created_at": at, **extra})
        self.touch(number, at)

    def edit_body(self, number, body, at):
        self.issues[number]["body"] = body
        self.touch(number, at)

    def touch(self, number, at):
        self.issues[number]["updated_at"] = max(self.issues[number]["updated_at"], at)

    def paged(self, path, params=None, max_pages=50):
        self.calls.append(path)
        params = params or {}
        since = params.get("since") or ""
        if path == "notifications":
            return list(self.notifications)
        if path == f"repos/{REPO}/issues":
            return sorted((i for i in self.issues.values() if i["updated_at"] >= since), key=lambda i: i["updated_at"])
        m = re.fullmatch(rf"repos/{REPO}/(issues|pulls)/(\d+)/(comments|events|reviews)", path)
        if not m:
            raise AssertionError(f"unexpected path {path}")
        kind, number, what = m.group(1), int(m.group(2)), m.group(3)
        if kind == "pulls":
            return []
        if what == "events":
            return list(self.events[number])
        return [c for c in self.comments[number] if c["updated_at"] >= since]

    def get(self, path, params=None):
        self.calls.append(path)
        m = re.fullmatch(rf"repos/{REPO}/issues/comments/(\d+)", path)
        if m:
            return next(c for cs in self.comments.values() for c in cs if c["id"] == int(m.group(1)))
        m = re.fullmatch(rf"repos/{REPO}/issues/(\d+)/comments", path)
        if m:
            per_page, page = int(params["per_page"]), int(params["page"])
            found = self.comments[int(m.group(1))]
            return found[(page - 1) * per_page: page * per_page]
        m = re.fullmatch(rf"repos/{REPO}/(issues|pulls)/(\d+)", path)
        if m:
            return self.issues[int(m.group(2))]
        raise AssertionError(f"unexpected get {path}")


def by_kind(items):
    found = {}
    for i in items:
        found.setdefault((i.kind, i.route), []).append(i)
    return found


class RepoContextTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.api = FakeGitHubApi()
        self.cursor = ago(hours=1)      # a cursor from before this version (plain time)

    def fetch(self):
        result = GitHubRepoSource(self.api, REPO, ["me-gh"], []).fetch(self.cursor)
        self.cursor = result.cursor
        return result.items

    def old_issue(self):
        self.api.issue(7, title="Dataset split is leaking", body="ORIGINAL: test images overlap with train",
                       created=ago(days=30), assignees=["me-gh"])
        for n in range(4):
            self.api.comment(7, 70 + n, "alice", f"old comment {n}", ago(days=20 - n))

    def test_old_issue_new_comment_brings_body_discussion_state_and_participation(self):
        self.old_issue()
        self.api.comment(7, 99, "alice", "any progress on the split?", ago(minutes=5))
        items = by_kind(self.fetch())
        [new] = items[("comment", "batch")]
        self.assertEqual(new.external_id, "comment:99")
        [opened] = items[("opened", "context")]
        self.assertIn("ORIGINAL", opened.body)
        self.assertEqual([c.body for c in items[("comment", "context")]], ["old comment 1", "old comment 2",
                                                                            "old comment 3"])
        self.assertEqual((new.meta["state"], new.meta["opener"], new.meta["assignees"]), ("open", "bob", ["me-gh"]))

    def test_enrichment_is_cached_until_the_matter_reactivates(self):
        self.old_issue()
        self.api.comment(7, 99, "alice", "any progress?", ago(minutes=5))
        self.fetch()
        clock.advance(minutes=30)
        self.api.comment(7, 100, "bob", "ping", ago(minutes=1))
        self.assertEqual({(i.kind, i.route) for i in self.fetch()}, {("comment", "batch")}, "no context replay")
        clock.advance(days=15)
        self.api.comment(7, 101, "bob", "still blocked", ago(minutes=1))
        kinds = {(i.kind, i.route) for i in self.fetch()}
        self.assertIn(("opened", "context"), kinds, "a reactivated matter gets its context again")

    def test_old_pr_review_request(self):
        self.api.issue(9, title="New loader", body="ORIGINAL: replaces the CSV loader", created=ago(days=20), pr=True)
        self.api.event(9, 1, "review_requested", "bob", ago(minutes=3), requested_reviewer={"login": "me-gh"})
        items = by_kind(self.fetch())
        [event] = items[("event", "batch")]
        self.assertEqual(event.meta["requested_reviewers"], ["me-gh"])
        self.assertIn("ORIGINAL", items[("opened", "context")][0].body)

    def test_body_edits_are_versioned_by_content_and_do_not_retrigger(self):
        self.old_issue()
        self.api.comment(7, 99, "alice", "hello", ago(minutes=5))
        self.fetch()
        clock.advance(minutes=30)
        self.api.comment(7, 100, "alice", "another", ago(minutes=1))
        self.assertNotIn("edit", {i.kind for i in self.fetch()}, "activity without a body change is no edit")
        clock.advance(minutes=30)
        self.api.edit_body(7, "ORIGINAL: test images overlap with train. UPDATE: also val", ago(minutes=1))
        [edit] = [i for i in self.fetch() if i.kind == "edit"]
        self.assertEqual((edit.external_id, edit.route), ("issue:1007", "if_open_task"))
        self.assertTrue(edit.version)
        clock.advance(minutes=30)
        self.api.comment(7, 101, "alice", "more", ago(minutes=1))
        self.assertNotIn("edit", {i.kind for i in self.fetch()}, "the same body is not reported again")

    def test_context_is_rendered_with_status_and_the_original_body(self):
        self.old_issue()
        for n in range(8):   # more recent discussion than the earlier-context limit
            self.api.comment(7, 200 + n, "alice", f"recent {n}", ago(days=2, minutes=-n))
        src = GitHubRepoSource(self.api, REPO, ["me-gh"], [])
        self.wake_and_finish(src)                     # first read: the recent discussion is processed
        self.api.comment(7, 99, "alice", "any progress on the split?", ago(minutes=1))
        run_id = self.wake(src)
        context = intake.render_context(self.conn, self.cfg, run_id, gate_intake.manifest_for(self.cfg, run_id))
        self.assertIn("ORIGINAL: test images overlap with train", context, "the first message is always shown")
        self.assertIn("state: open; opened by bob; assignees: me-gh", context)
        self.assertIn("you: assignee", context)

    def wake(self, src):
        """Gate runs until the matter is mature and the email/GitHub wake interval allows it."""
        out, woke = gate_intake.run_gate(self.conn, self.cfg, [src], [])
        if not woke:
            clock.advance(minutes=61)
            out, woke = gate_intake.run_gate(self.conn, self.cfg, [src], [])
        self.assertTrue(woke)
        return run_id_of(out)

    def wake_and_finish(self, src):
        run_id = self.wake(src)
        ids = [i["id"] for i in intake.run_new_items(self.conn, run_id)]
        intake.apply_decisions(self.conn, self.cfg, run_id, {
            "run_id": run_id, "rules_version": rules_version(self.conn),
            "items": [{"op": "ignore", "items": ids, "category": "fyi_no_action", "rationale": "test"}]},
            gate_intake.manifest_for(self.cfg, run_id))


class NotificationContextTests(WorkspaceCase):
    URL = f"https://api.github.com/repos/{REPO}/issues/7"

    def setUp(self):
        super().setUp()
        self.api = FakeGitHubApi()
        self.api.issue(7, title="Crash on load", body="ORIGINAL: crashes with empty config", created=ago(days=40))
        for n in range(4):
            self.api.comment(7, 70 + n, "carol", f"old comment {n}", ago(days=30 - n))
        self.cursor = None

    def notify(self, nid, latest_cid):
        self.api.notifications = [{"id": nid, "reason": "mention", "updated_at": ago(minutes=1),
                                   "repository": {"full_name": REPO},
                                   "subject": {"type": "Issue", "title": "Crash on load", "url": self.URL,
                                               "latest_comment_url": f"https://api.github.com/repos/{REPO}/issues/comments/{latest_cid}"}}]

    def fetch(self):
        result = GitHubNotificationSource(self.api, set(), ["me-gh"]).fetch(self.cursor)
        self.cursor = result.cursor
        return result.items

    def test_first_sight_brings_the_body_and_recent_discussion(self):
        self.api.comment(7, 99, "dave", "@me-gh can you look?", ago(minutes=1))
        self.notify("n1", 99)
        items = by_kind(self.fetch())
        [note] = items[("notification", "batch")]
        self.assertIn("can you look?", note.body)
        self.assertIn("ORIGINAL", items[("opened", "context")][0].body)
        context = [c.external_id for c in items[("comment", "context")]]
        self.assertEqual(context, ["comment:71", "comment:72", "comment:73"], "latest comment is in the notification")

    def test_later_notifications_bring_the_discussion_since_the_last_one(self):
        self.api.comment(7, 99, "dave", "@me-gh can you look?", ago(minutes=1))
        self.notify("n1", 99)
        self.fetch()
        clock.advance(hours=2)
        self.api.comment(7, 100, "erin", "I can reproduce it", ago(minutes=50))
        self.api.comment(7, 101, "dave", "@me-gh any idea?", ago(minutes=1))
        self.notify("n1", 101)
        items = by_kind(self.fetch())
        self.assertEqual([c.external_id for c in items[("comment", "context")]], ["comment:100"])
        self.assertNotIn(("opened", "context"), items, "known matter: no body replay")


class MailApiDuplicateTests(WorkspaceCase):
    KEY = "github:octo/repo#7"

    def api_comment(self, ext, author, at):
        return Item(source="github", account=REPO, external_id=ext, kind="comment", conversation_key=self.KEY,
                    thread_key=self.KEY, occurred_at=at, author=author, body="Could you rebase?")

    def mail(self, ext, sender, at):
        return Item(source="gmail", account="primary", external_id=ext, conversation_key=self.KEY,
                    thread_key=self.KEY, occurred_at=at, author="Alice Chen <notifications@github.com>",
                    body="Could you rebase?\n\nReply to this email directly", meta={"github_sender": sender})

    def state(self, ext):
        return self.conn.execute("SELECT state FROM source_items WHERE external_id=?", (ext,)).fetchone()[0]

    def test_the_same_event_from_mail_and_api_is_batched_once(self):
        at = clock.iso()
        persist_fetch(self.conn, "github", FetchResult(scope="repo:octo/repo", items=[self.api_comment("comment:5", "alice", at)], cursor="c"))
        later = clock.iso(clock.now() + timedelta(seconds=40))
        persist_fetch(self.conn, "gmail", FetchResult(scope="mailbox:primary", items=[self.mail("m1", "alice", later)], cursor="c"))
        self.assertEqual((self.state("comment:5"), self.state("m1")), ("batched", "superseded"))
        # Mail first, API later: the API item is the duplicate.
        persist_fetch(self.conn, "gmail", FetchResult(scope="mailbox:primary", items=[self.mail("m2", "bob", at)], cursor="c"))
        persist_fetch(self.conn, "github", FetchResult(scope="repo:octo/repo", items=[self.api_comment("comment:6", "bob", later)], cursor="c"))
        self.assertEqual((self.state("m2"), self.state("comment:6")), ("batched", "superseded"))

    def test_different_people_or_times_are_not_duplicates(self):
        at = clock.iso()
        persist_fetch(self.conn, "github", FetchResult(scope="repo:octo/repo", items=[self.api_comment("comment:5", "alice", at)], cursor="c"))
        persist_fetch(self.conn, "gmail", FetchResult(scope="mailbox:primary", items=[
            self.mail("m1", "carol", at),
            self.mail("m2", "alice", clock.iso(clock.now() + timedelta(minutes=30)))], cursor="c"))
        self.assertEqual((self.state("m1"), self.state("m2")), ("batched", "batched"))
