"""R12: context for GitHub activity on old issues/PRs (original body, recent discussion, state,
the user's participation), content-versioned body edits, and mail/API duplicates.

TEST DOUBLE: ``FakeGitHubApi`` is an in-memory stand-in for the GitHub REST endpoints the
adapters read (issues with ``since``, comments with ``since``/paging, events, reviews,
notifications, single issue/PR objects). It is not the real service.
"""

import re
from dataclasses import replace
from datetime import timedelta

from todo_manager import clock, gate_intake, intake
from todo_manager.db import rules_version
from todo_manager.sources.base import FetchResult, Item, persist_fetch
from todo_manager.sources.github import GitHubNotificationSource, GitHubRepoSource
from todo_manager.sources import registry
from todo_manager.sources.gmail import GmailSource
from tests.helpers import WorkspaceCase
from tests.test_sources import gmail_message
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

    def edit_comment(self, number, cid, body, at):
        for c in self.comments[number]:
            if c["id"] == cid:
                c.update(body=body, updated_at=at)
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
        self.assertEqual([c.external_id for c in items[("comment", "batch")]], ["comment:100"],
                         "a person's comment since the last view is news, not only context")
        self.assertNotIn(("opened", "context"), items, "known matter: no body replay")

    def seen_once(self):
        self.api.comment(7, 99, "dave", "@me-gh can you look?", ago(minutes=1))
        self.notify("n1", 99)
        self.fetch()
        clock.advance(hours=2)

    def test_every_comment_since_the_last_view_is_kept(self):
        self.seen_once()
        for n in range(15):
            self.api.comment(7, 200 + n, "erin", f"step {n}", ago(minutes=60 - n))
        self.api.comment(7, 300, "dave", "@me-gh which one?", ago(minutes=1))
        self.notify("n1", 300)
        items = by_kind(self.fetch())
        self.assertEqual(len(items[("comment", "batch")]), 15, "no comment is dropped; the budget segments later")

    def test_an_edit_of_an_older_comment_is_read(self):
        self.seen_once()
        self.api.edit_comment(7, 72, "old comment 2 (EDITED: deadline is Friday)", ago(minutes=10))
        self.api.comment(7, 301, "dave", "@me-gh see the edit above", ago(minutes=1))
        self.notify("n1", 301)
        edits = [i for i in self.fetch() if i.kind == "edit"]
        self.assertEqual([(i.external_id, i.route) for i in edits], [("comment:72", "if_open_task")])
        self.assertIn("deadline is Friday", edits[0].body)

    def test_a_comment_just_before_the_watermark_is_not_lost(self):
        self.api.comment(7, 99, "dave", "@me-gh can you look?", ago(minutes=1))
        self.notify("n1", 99)
        self.fetch()
        # Created a moment before that check but not yet listed by it (clock skew / listing lag).
        self.api.comment(7, 150, "erin", "the log is attached", ago(seconds=30))
        clock.advance(hours=2)
        self.api.comment(7, 151, "dave", "@me-gh ping", ago(minutes=1))
        self.notify("n1", 151)
        self.assertIn("comment:150", [c.external_id for c in by_kind(self.fetch())[("comment", "batch")]])

    def test_a_bot_comment_last_does_not_hide_the_request_before_it(self):
        self.seen_once()
        self.api.comment(7, 400, "dave", "@me-gh please review the fix", ago(minutes=20))
        self.api.comment(7, 401, "ci-bot", "CI passed", ago(minutes=1))
        self.api.comments[7][-1]["user"]["type"] = "Bot"
        self.notify("n1", 401)
        items = by_kind(self.fetch())
        self.assertEqual([i.route for i in items[("notification", "context")]], ["context"])
        self.assertEqual([c.external_id for c in items[("comment", "batch")]], ["comment:400"])


class MailApiDuplicateTests(WorkspaceCase):
    KEY = "github:octo/repo#7"

    def api_comment(self, ext, author, at):
        return Item(source="github", account=REPO, external_id=ext, kind="comment", conversation_key=self.KEY,
                    thread_key=self.KEY, occurred_at=at, author=author, body="Could you rebase?",
                    meta={"github_event": ext})

    def mail(self, ext, sender, at, event=None):
        return Item(source="gmail", account="primary", external_id=ext, conversation_key=self.KEY,
                    thread_key=self.KEY, occurred_at=at, author="Alice Chen <notifications@github.com>",
                    body="Could you rebase?\n\nReply to this email directly",
                    meta={"github_sender": sender, "github_event": event})

    def state(self, ext):
        return self.conn.execute("SELECT state FROM source_items WHERE external_id=?", (ext,)).fetchone()[0]

    def test_the_same_event_from_mail_and_api_is_batched_once(self):
        at = clock.iso()
        persist_fetch(self.conn, "github", FetchResult(scope="repo:octo/repo", items=[self.api_comment("comment:5", "alice", at)], cursor="c"))
        later = clock.iso(clock.now() + timedelta(seconds=40))
        persist_fetch(self.conn, "gmail", FetchResult(scope="mailbox:primary", items=[self.mail("m1", "alice", later, "comment:5")], cursor="c"))
        self.assertEqual((self.state("comment:5"), self.state("m1")), ("batched", "superseded"))
        # Mail first, API later: the API item is the duplicate.
        persist_fetch(self.conn, "gmail", FetchResult(scope="mailbox:primary", items=[self.mail("m2", "bob", at, "comment:6")], cursor="c"))
        persist_fetch(self.conn, "github", FetchResult(scope="repo:octo/repo", items=[self.api_comment("comment:6", "bob", later)], cursor="c"))
        self.assertEqual((self.state("m2"), self.state("comment:6")), ("batched", "superseded"))

    def test_different_events_are_not_duplicates(self):
        at = clock.iso()
        persist_fetch(self.conn, "github", FetchResult(scope="repo:octo/repo", items=[self.api_comment("comment:5", "alice", at)], cursor="c"))
        persist_fetch(self.conn, "gmail", FetchResult(scope="mailbox:primary", items=[
            self.mail("m1", "carol", at, "comment:8"),
            self.mail("m2", "alice", clock.iso(clock.now() + timedelta(seconds=30)), "comment:9")], cursor="c"))
        self.assertEqual((self.state("m1"), self.state("m2")), ("batched", "batched"),
                         "the same person commenting twice within minutes is two events")

    def test_a_mail_whose_event_is_unknown_is_kept(self):
        at = clock.iso()
        persist_fetch(self.conn, "github", FetchResult(scope="repo:octo/repo", items=[self.api_comment("comment:5", "alice", at)], cursor="c"))
        persist_fetch(self.conn, "gmail", FetchResult(scope="mailbox:primary", items=[self.mail("m1", "alice", at)], cursor="c"))
        self.assertEqual(self.state("m1"), "batched", "sameness not confirmed: the evidence is kept")

    def test_a_context_copy_does_not_swallow_the_mail(self):
        at = clock.iso()
        context = replace(self.api_comment("comment:5", "alice", at), route="context")
        persist_fetch(self.conn, "github", FetchResult(scope="notifications", items=[context], cursor="c"))
        persist_fetch(self.conn, "gmail", FetchResult(scope="mailbox:primary", items=[self.mail("m1", "alice", at, "comment:5")], cursor="c"))
        self.assertEqual(self.state("m1"), "batched")


class TrackedRepoMailTests(WorkspaceCase):
    """B10 reproduction: mail about a repo that is also read through the API was dropped at
    normalisation ("the API carries it"), so an event the API run missed (failed read, an event
    kind it does not list) was lost. Now the mail is kept unless the same event is known."""

    def gmail(self):
        """The mailbox adapter as production wiring builds it while octo/repo is read through the API."""
        self.cfg.data["email"].update({"enabled": True, "accounts": [
            {"name": "primary", "address": "me@example.edu", "token_command": ["true"]}]})
        self.cfg.data["github"].update({"enabled": True, "repos": [REPO], "notifications": False})
        [source] = [a for a in registry.conversation_sources(self.cfg) if isinstance(a, GmailSource)]
        return source

    def mail(self, msgid, body="Looks good, one nit.", sender="carol"):
        return gmail_message("m1", "t1", sender="Carol <notifications@github.com>", body=body, extra=[
            {"name": "Message-ID", "value": msgid}, {"name": "X-GitHub-Sender", "value": sender}])

    def test_tracked_repo_mail_is_kept_when_the_api_did_not_deliver_the_event(self):
        item = self.gmail().normalize(self.mail("<octo/repo/pull/7/review/555@github.com>"))
        persist_fetch(self.conn, "gmail", FetchResult(scope="mailbox:primary", items=[item], cursor="c"))
        state = self.conn.execute("SELECT state FROM source_items WHERE external_id='m1'").fetchone()[0]
        self.assertEqual(state, "batched")

    def test_event_identity_from_message_id_and_link(self):
        cases = {
            "<octo/repo/issues/7@github.com>": ("", "opened"),
            "<octo/repo/pull/7/c123@github.com>": ("", "comment:123"),
            "<octo/repo/issues/7/123@github.com>": ("", "comment:123"),
            "<octo/repo/pull/7/review/555@github.com>": ("", "review:555"),
            "<octo/repo/issues/7/issue_event/42@github.com>": ("", "event:42"),
            "<octo/repo/pull/7/push/9@github.com>": ("", None),
            "<octo/repo/pull/7/x@github.com>": (
                "nit\n—\nReply to this email directly, view it on GitHub:\n"
                "https://github.com/octo/repo/pull/7#discussion_r777", "review_comment:777"),
        }
        for msgid, (body, event) in cases.items():
            item = self.gmail().normalize(self.mail(msgid, body=body or "text"))
            self.assertEqual(item.meta.get("github_event"), event, msgid)

    def test_api_items_carry_the_same_identity(self):
        api = FakeGitHubApi()
        api.issue(7, title="Leak", body="body", created=ago(minutes=5))
        api.comment(7, 123, "carol", "please rebase", ago(minutes=2))
        api.event(7, 42, "closed", "carol", ago(minutes=1))
        items = GitHubRepoSource(api, REPO, ["me-gh"], []).fetch(ago(hours=1)).items
        events = {i.meta.get("github_event") for i in items if i.route == "batch"}
        self.assertTrue({"opened", "comment:123", "event:42"} <= events, events)
