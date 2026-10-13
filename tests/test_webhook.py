"""The daemon's Notion webhook receiver: handshake, signature, parsing, dedupe, HTTP surface.

Requests are the ones ``FakeNotion`` (test double) records for its changes, signed like Notion
signs them. The server binds an ephemeral loopback port.
"""

import json
import queue
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from todo_manager import clock, webhook
from tests.fake_notion import BOT_ID, USER_ID, FakeNotion


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.events = queue.Queue()
        self.receiver = webhook.Receiver(self.home, self.events, port=0)
        webhook.open_enrollment(self.home)   # the operator ran `daemon enroll`
        self.fake = FakeNotion()
        self.page = self.fake._new_page({"type": "workspace"}, {}, "", USER_ID)["id"]
        self.fake.events.clear()

    def handshake(self, token="secret_abc"):
        return self.receiver.receive(json.dumps({"verification_token": token}).encode(), None)

    def deliver_all(self, token="secret_abc"):
        return [self.receiver.receive(body, headers[webhook.SIGNATURE_HEADER])
                for body, headers in self.fake.deliveries(token)]

    def queued(self):
        out = []
        while not self.events.empty():
            out.append(self.events.get_nowait())
        return out

    def test_the_first_request_stores_the_verification_token_with_owner_only_access(self):
        self.assertEqual(self.handshake(), 200)
        self.assertEqual(webhook.load_token(self.home), "secret_abc")
        self.assertEqual(webhook.token_path(self.home).stat().st_mode & 0o777, 0o600)

    def test_without_a_token_nothing_but_a_handshake_is_accepted(self):
        self.fake.user_comment(self.page, "hi")
        self.assertEqual(self.deliver_all(), [401])
        self.assertIsNone(webhook.load_token(self.home))
        self.assertEqual(self.queued(), [])

    def test_once_a_token_is_stored_a_second_handshake_cannot_replace_it(self):
        self.handshake()
        self.assertEqual(self.handshake("attacker"), 401)
        self.assertEqual(webhook.load_token(self.home), "secret_abc")

    def test_two_handshakes_at_once_store_one_token(self):
        import threading
        import time
        save = webhook.save_token

        def slow_save(home, token):
            time.sleep(0.2)
            save(home, token)
        webhook.save_token = slow_save
        self.addCleanup(setattr, webhook, "save_token", save)
        results = {}
        threads = [threading.Thread(target=lambda t=t: results.__setitem__(t, self.handshake(t))) for t in ("first", "second")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [t for t, status in results.items() if status == 200]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(webhook.load_token(self.home), winners[0])

    def test_a_signed_comment_event_is_queued_with_its_page(self):
        self.handshake()
        self.fake.user_comment(self.page, "hi")
        self.assertEqual(self.deliver_all(), [200])
        [event] = self.queued()
        self.assertEqual((event.type, event.page_id, event.bot_only), ("comment.created", self.page, False))

    def test_a_wrong_signature_is_refused_and_counted(self):
        self.handshake()
        self.fake.user_comment(self.page, "hi")
        self.assertEqual(self.deliver_all(token="other"), [401])
        self.assertEqual(self.receiver.stats.bad_signatures, 1)
        self.assertEqual(self.queued(), [])

    def test_a_tampered_body_is_refused(self):
        self.handshake()
        self.fake.user_comment(self.page, "hi")
        [(body, headers)] = self.fake.deliveries("secret_abc")
        self.assertEqual(self.receiver.receive(body.replace(b"comment.created", b"page.deleted"),
                                               headers[webhook.SIGNATURE_HEADER]), 401)

    def test_a_retried_event_is_queued_once(self):
        self.handshake()
        self.fake.user_comment(self.page, "hi")
        [(body, headers)] = self.fake.deliveries("secret_abc")
        for _ in range(3):
            self.assertEqual(self.receiver.receive(body, headers[webhook.SIGNATURE_HEADER]), 200)
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(self.receiver.stats.duplicates, 2)

    def test_our_own_writes_are_marked_bot_only(self):
        self.handshake()
        self.fake.user_comment(self.page, "reply", author=BOT_ID)
        self.deliver_all()
        [event] = self.queued()
        self.assertTrue(event.bot_only)

    def test_page_events_name_the_page(self):
        self.handshake()
        self.fake.user_set_markdown(self.page, "new body")
        self.fake.user_trash(self.page)
        self.deliver_all()
        self.assertEqual([(e.type, e.page_id) for e in self.queued()],
                         [("page.content_updated", self.page), ("page.deleted", self.page)])

    def test_unknown_event_types_are_acknowledged_and_dropped(self):
        self.handshake()
        body = json.dumps({"id": "e1", "type": "database.schema_updated", "entity": {"id": "x", "type": "database"}}).encode()
        self.assertEqual(self.receiver.receive(body, webhook.sign("secret_abc", body)), 200)
        self.assertEqual(self.queued(), [])
        self.assertIsNotNone(self.receiver.stats.last_event_at, "still proof that deliveries arrive")

    def test_reset_token_allows_a_new_handshake_once_enrollment_is_open_again(self):
        self.handshake()
        self.assertTrue(webhook.reset_token(self.home))
        self.assertEqual(self.handshake("new"), 403, "the stored handshake closed the enrollment")
        webhook.open_enrollment(self.home)
        self.assertEqual(self.handshake("new"), 200)
        self.assertEqual(webhook.load_token(self.home), "new")


class EnrollmentTests(unittest.TestCase):
    """Without a token, anyone who reaches the public URL could send the first handshake; it is
    only accepted while the operator has opened enrollment (`daemon enroll`), and only once."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        clock.set_now(datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc))
        self.addCleanup(clock.set_now, None)
        self.receiver = webhook.Receiver(self.home, queue.Queue(), port=0)
        self.addCleanup(self.receiver.server.server_close)

    def handshake(self, token="secret_abc"):
        return self.receiver.receive(json.dumps({"verification_token": token}).encode(), None)

    def test_a_handshake_without_open_enrollment_is_refused(self):
        self.assertEqual(self.handshake("attacker"), 403)
        self.assertIsNone(webhook.load_token(self.home))
        self.assertEqual(self.receiver.stats.refused_handshakes, 1)

    def test_enrollment_expires(self):
        until = webhook.open_enrollment(self.home, minutes=15)
        self.assertEqual(until, "2026-10-13T12:15:00.000Z")
        self.assertEqual(webhook.enrollment_until(self.home), until)
        clock.advance(minutes=16)
        self.assertIsNone(webhook.enrollment_until(self.home))
        self.assertEqual(self.handshake(), 403)
        self.assertIsNone(webhook.load_token(self.home))

    def test_the_accepted_handshake_closes_enrollment(self):
        webhook.open_enrollment(self.home)
        self.assertEqual(self.handshake(), 200)
        self.assertIsNone(webhook.enrollment_until(self.home))
        self.assertFalse(webhook.enroll_path(self.home).exists())

    def test_a_damaged_enrollment_file_counts_as_closed(self):
        webhook.enroll_path(self.home).parent.mkdir(parents=True, exist_ok=True)
        webhook.enroll_path(self.home).write_text("not a time")
        self.assertIsNone(webhook.enrollment_until(self.home))
        self.assertEqual(self.handshake(), 403)


class HttpSurfaceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.events = queue.Queue()
        self.receiver = webhook.Receiver(self.home, self.events, port=0)
        self.receiver.start()
        self.addCleanup(self.receiver.stop)
        self.url = f"http://127.0.0.1:{self.receiver.port}"

    def request(self, path, body=b"", method="POST", headers=None):
        req = urllib.request.Request(self.url + path, data=body if method == "POST" else None, method=method,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_handshake_and_signed_event_over_http(self):
        self.assertEqual(self.request("/notion", json.dumps({"verification_token": "t"}).encode()), 403)
        webhook.open_enrollment(self.home)
        self.assertEqual(self.request("/notion", json.dumps({"verification_token": "t"}).encode()), 200)
        fake = FakeNotion()
        page = fake._new_page({"type": "workspace"}, {}, "", USER_ID)["id"]
        fake.events.clear()
        fake.user_comment(page, "hi")
        [(body, headers)] = fake.deliveries("t")
        self.assertEqual(self.request("/notion", body, headers=headers), 200)
        self.assertEqual(self.events.get(timeout=2).page_id, page)

    def test_only_post_to_the_webhook_path_is_served(self):
        self.assertEqual(self.request("/other", b"{}"), 404)
        self.assertEqual(self.request("/notion", method="GET"), 404)

    def test_an_oversized_body_is_refused(self):
        self.assertEqual(self.request("/notion", b"x", headers={"Content-Length": str(webhook.MAX_BODY + 1)}), 413)


if __name__ == "__main__":
    unittest.main()
