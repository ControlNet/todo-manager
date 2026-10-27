"""R03: replies in Slack threads whose parent is older than the re-read window.

TEST DOUBLE: ``FakeSlack`` is an in-memory stand-in for the Slack Web API (conversations.history
with oldest/latest/cursor paging, conversations.replies with oldest/cursor paging, 429 injection).
It is not the real service.
"""

import json

from todo_manager import clock
from todo_manager.net import HttpError
from todo_manager.sources.base import SourceError
from todo_manager.sources.slack import SlackSource
from tests.helpers import WorkspaceCase

DAY = 86400


class FakeSlack:
    def __init__(self):
        self.messages: dict[str, dict] = {}     # ts -> message (top level and replies)
        self.fail = {}                          # method -> remaining 429 responses
        self.calls: list[tuple[str, dict]] = []

    def post(self, ts: float, text: str, *, user="U2", thread: float | None = None) -> str:
        key = f"{ts:.6f}"
        msg = {"ts": key, "user": user, "text": text}
        if thread is not None:
            parent = self.messages[f"{thread:.6f}"]
            parent.update(thread_ts=parent["ts"], reply_count=parent.get("reply_count", 0) + 1,
                          latest_reply=max(parent.get("latest_reply", key), key))
            msg["thread_ts"] = parent["ts"]
        self.messages[key] = msg
        return key

    def edit(self, ts: str, text: str, at: float) -> None:
        self.messages[ts].update(text=text, edited={"ts": f"{at:.6f}"})

    def delete(self, ts: str) -> None:
        del self.messages[ts]

    @staticmethod
    def _page(found, params):
        start = int(params.get("cursor") or 0)
        limit = int(params.get("limit") or 100)
        page = found[start:start + limit]
        nxt = str(start + limit) if start + limit < len(found) else ""
        return {"ok": True, "messages": page, "response_metadata": {"next_cursor": nxt}}

    def __call__(self, method, url, headers=None, params=None, timeout=None, body=None):
        name = url.rsplit("/", 1)[1]
        params = dict(params or {})
        self.calls.append((name, params))
        if self.fail.get(name):
            self.fail[name] -= 1
            raise HttpError(429, "ratelimited", retry_after=30)
        if name == "users.info":
            return {"ok": False, "error": "user_not_found"}
        oldest = float(params.get("oldest") or 0)
        if name == "conversations.history":
            latest = float(params.get("latest") or 1e12)
            found = [m for m in self.messages.values()
                     if m.get("thread_ts") in (None, m["ts"]) and oldest <= float(m["ts"]) < latest]
            return self._page(sorted(found, key=lambda m: -float(m["ts"])), params)
        if name == "conversations.replies":
            thread = params["ts"]
            found = [self.messages[thread]] + sorted(
                (m for m in self.messages.values()
                 if m.get("thread_ts") == thread and m["ts"] != thread and float(m["ts"]) >= oldest),
                key=lambda m: float(m["ts"]))
            return self._page(found, params)
        raise AssertionError(f"unexpected request {url}")


class SlackCase(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.slack = FakeSlack()
        self.cursor = None
        self.emitted: set[tuple[str, str]] = set()

    def source(self, **kw):
        return SlackSource(token=lambda: "t", team="T1", conversation="C1", my_user_ids=["UME"], http=self.slack,
                           page_size=kw.pop("page_size", 50), **kw)

    def fetch(self, **kw):
        """One read; ``result.new`` drops what earlier reads returned (the overlap re-read is
        deduplicated by (id, version) on persist)."""
        result = self.source(**kw).fetch(self.cursor)
        self.cursor = result.cursor
        result.new = [i for i in result.items if (i.external_id, i.version) not in self.emitted]
        self.emitted |= {(i.external_id, i.version) for i in result.items}
        return result

    def ids(self, result, route=None, kind=None):
        return sorted(i.external_id for i in result.new
                      if (route is None or i.route == route) and (kind is None or i.kind == kind))

    def now(self):
        return clock.now().timestamp()


class SlackThreadTests(SlackCase):
    def known_old_thread(self):
        """A thread from three days ago, read while it was recent; then the window moved on."""
        start = self.now()
        parent = self.slack.post(start - 3 * DAY, "can you review the dataset card?")
        self.slack.post(start - 3 * DAY + 600, "sure, tomorrow", user="UME", thread=float(parent))
        self.fetch()
        self.slack.post(start - 3600, "unrelated chat")   # moves the window forward
        clock.advance(minutes=61)
        self.fetch()
        return parent

    def test_a_reply_in_a_known_old_thread_is_read(self):
        parent = self.known_old_thread()
        reply = self.slack.post(self.now(), "any update on the card?", thread=float(parent))
        clock.advance(minutes=61)
        result = self.fetch()
        self.assertEqual(self.ids(result, route="batch"), [f"C1:{reply}"])
        [item] = [i for i in result.new if i.external_id == f"C1:{reply}"]
        self.assertEqual(item.thread_key, f"slack:C1:{parent}")
        self.assertEqual(self.ids(result, route="context"), [], "a known thread needs no context replay")

    def test_a_never_seen_thread_brings_its_parent_and_earlier_replies_as_context(self):
        start = self.now()
        parent = self.slack.post(start - 5 * DAY, "who owns the eval script?")
        earlier = self.slack.post(start - 5 * DAY + 60, "I think Bob", user="U3", thread=float(parent))
        # A workspace read before thread watching existed: its cursor has no thread index.
        self.cursor = json.dumps({"newest": start - 3600, "last_message": start - 3600, "window_ids": []})
        reply = self.slack.post(start, "@me can you take it over?", thread=float(parent))
        result = self.fetch()
        self.assertEqual(self.ids(result, route="batch"), [f"C1:{reply}"])
        self.assertEqual(self.ids(result, route="context"), sorted([f"C1:{parent}", f"C1:{earlier}"]))
        # Read once more: now known, nothing is replayed.
        clock.advance(minutes=61)
        self.slack.post(self.now() - 60, "unrelated chat")
        self.assertEqual(self.ids(self.fetch(), route="context"), [])

    def test_long_threads_and_scans_are_paged(self):
        parent = self.known_old_thread()
        replies = [self.slack.post(self.now() - 50 + i, f"reply {i}", thread=float(parent)) for i in range(5)]
        for i in range(6):   # more old parents than one history page
            self.slack.post(self.now() - 2 * DAY - i, f"old note {i}")
        clock.advance(minutes=61)
        result = self.fetch(page_size=2)
        self.assertEqual(self.ids(result, route="batch"), sorted(f"C1:{r}" for r in replies))

    def test_an_edit_in_an_old_thread_is_read(self):
        parent = self.known_old_thread()
        reply = self.slack.post(self.now(), "deadline is Friday", thread=float(parent))
        clock.advance(minutes=61)
        self.fetch()
        self.slack.edit(reply, "deadline is Monday", at=self.now())
        clock.advance(minutes=61)
        edits = [i for i in self.fetch().new if i.kind == "edit"]
        self.assertEqual([(i.external_id, i.body) for i in edits], [(f"C1:{reply}", "deadline is Monday")])

    def test_unread_is_not_deleted_and_a_real_deletion_keeps_its_thread(self):
        start = self.now()
        parent = self.slack.post(start - 23 * 3600, "draft ready?")
        reply = self.slack.post(start - 3600, "yes, see doc", user="U3", thread=float(parent))
        self.fetch()
        self.slack.post(start + 6 * 3600, "new day")      # the parent leaves the re-read window
        clock.advance(hours=7)
        result = self.fetch(thread_scan_minutes=10 ** 6)  # no scan this run: the thread is not read
        self.assertEqual(self.ids(result, kind="deletion"), [], "not read is not deleted")
        self.slack.delete(reply)
        clock.advance(minutes=61)
        result = self.fetch()                             # scanned and re-read: the reply is gone
        [gone] = [i for i in result.new if i.kind == "deletion"]
        self.assertEqual((gone.external_id, gone.thread_key), (f"C1:{reply}", f"slack:C1:{parent}"))

    def test_a_deletion_between_thread_scans_is_found_by_the_next_scan(self):
        parent = self.known_old_thread()
        reply = self.slack.post(self.now(), "numbers are in the sheet", user="U3", thread=float(parent))
        clock.advance(minutes=61)
        self.fetch()                                      # scanned: the reply is read and tracked
        clock.advance(minutes=5)
        before = len(self.slack.calls)
        self.fetch()                                      # channel poll only: the scan is not due
        self.assertNotIn("conversations.replies", [c[0] for c in self.slack.calls[before:]])
        self.assertEqual(json.loads(self.cursor)["window"].get(f"C1:{reply}"), f"r:{parent}",
                         "an unread thread keeps its tracked replies")
        self.slack.delete(reply)
        clock.advance(minutes=61)
        result = self.fetch()                             # scanned again: the reply is gone
        [gone] = [i for i in result.new if i.kind == "deletion"]
        self.assertEqual((gone.external_id, gone.thread_key), (f"C1:{reply}", f"slack:C1:{parent}"))

    def test_tracked_replies_of_an_unread_thread_leave_with_the_overlap_window(self):
        start = self.now()
        parent = self.slack.post(start - 3 * DAY, "who has the slides?")
        self.slack.post(start - 3 * DAY + 600, "me", thread=float(parent))
        reply = self.slack.post(start, "sent them", user="U3", thread=float(parent))
        self.fetch(overlap_minutes=60)
        self.assertIn(f"C1:{reply}", json.loads(self.cursor)["window"])
        self.slack.post(start + 2 * 3600, "unrelated chat")   # moves the window past the reply
        clock.advance(hours=3)
        self.fetch(overlap_minutes=60, thread_scan_minutes=10 ** 6)
        self.assertNotIn(f"C1:{reply}", json.loads(self.cursor)["window"], "outside the overlap window")

    def test_a_rate_limit_loses_nothing(self):
        parent = self.known_old_thread()
        reply = self.slack.post(self.now(), "ping on the review", thread=float(parent))
        clock.advance(minutes=61)
        before = self.cursor
        self.slack.fail["conversations.replies"] = 1
        with self.assertRaises(SourceError) as ctx:
            self.fetch()
        self.assertEqual(ctx.exception.retry_after, 30)
        self.cursor = before                              # a failed fetch does not advance the cursor
        clock.advance(minutes=5)
        self.assertEqual(self.ids(self.fetch(), route="batch"), [f"C1:{reply}"])

    def test_the_thread_index_is_bounded(self):
        start = self.now()
        for i in range(5):
            parent = self.slack.post(start - (i + 2) * DAY, f"topic {i}")
            self.slack.post(start - (i + 2) * DAY + 60, "ok", thread=float(parent))
        old = self.slack.post(start - 40 * DAY, "ancient")
        self.slack.post(start - 40 * DAY + 60, "ok", thread=float(old))
        self.fetch(thread_index_max=3, lookback_days=60)
        index = json.loads(self.cursor)["thread_index"]
        self.assertEqual(len(index), 3, "at most thread_index_max threads")
        self.assertNotIn(old, index, "threads idle for longer than thread_watch_days are dropped")


class ThreadScanProgressTests(SlackCase):
    """B07: a scan cut short by ``thread_scan_pages`` resumes from the oldest parent it reached
    (newest first), and the scan watermark only advances when a pass covered every parent back
    to the ``thread_watch_days`` horizon. Coverage is by parent time, not by thread activity."""

    def old_threads(self, n=10):
        start = self.now()
        parents = []
        for i in range(n):
            parent = self.slack.post(start - (10 + i) * DAY, f"old topic {i}")
            self.slack.post(start - (10 + i) * DAY + 60, "ok", user="U3", thread=float(parent))
            parents.append(parent)
        return parents

    def test_a_truncated_scan_resumes_and_reaches_the_oldest_thread(self):
        parents = self.old_threads()
        self.fetch(page_size=2, thread_scan_pages=1, thread_scan_minutes=0)
        reply = self.slack.post(self.now(), "still need the dataset card?", thread=float(parents[-1]))
        found = []
        for _ in range(8):
            clock.advance(minutes=5)
            found += self.ids(self.fetch(page_size=2, thread_scan_pages=1, thread_scan_minutes=0), route="batch")
        self.assertIn(f"C1:{reply}", found, "the oldest thread is reached by later runs")

    def test_the_watermark_advances_only_after_a_complete_pass(self):
        self.old_threads()
        result = self.fetch(page_size=2, thread_scan_pages=1, thread_scan_minutes=0)
        state = json.loads(self.cursor)
        self.assertIsNone(state.get("scan_since"), "a partial pass leaves the watermark alone")
        self.assertIsNotNone(state.get("scan_resume"))
        self.assertEqual(result.meta.get("thread_scan"), "partial")
        for _ in range(10):
            clock.advance(minutes=5)
            result = self.fetch(page_size=2, thread_scan_pages=1, thread_scan_minutes=0)
            if result.meta.get("thread_scan") == "complete":
                break
        state = json.loads(self.cursor)
        self.assertEqual(result.meta.get("thread_scan"), "complete")
        self.assertIsNotNone(state.get("scan_since"))
        self.assertIsNone(state.get("scan_resume"))

    def test_an_unfinished_pass_continues_before_the_next_cadence_slot(self):
        self.old_threads()
        self.fetch(page_size=2, thread_scan_pages=1, thread_scan_minutes=60)
        clock.advance(minutes=5)
        before = len([c for c in self.slack.calls if c[0] == "conversations.history"])
        self.fetch(page_size=2, thread_scan_pages=1, thread_scan_minutes=60)
        history = [c for c in self.slack.calls if c[0] == "conversations.history"][before:]
        self.assertTrue(any("latest" in p for _, p in history), "the pass goes on without waiting an hour")
