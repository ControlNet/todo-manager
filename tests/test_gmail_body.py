"""B09: an email's whole body is stored; only the model context clips it (with a pointer to
``todo-manager source show``). Body parts that Gmail returns by reference (``attachmentId``
instead of inline data, as it does for large bodies) are read; a part that cannot be read is
recorded as an incomplete body, never silently dropped. HTTP is ``FakeHttp`` (test double)."""

import json

from todo_manager import gate_intake
from todo_manager.context import run_items
from todo_manager.net import HttpError
from todo_manager.sources.base import SourceError, item_body, persist_fetch
from todo_manager.sources.gmail import GmailSource
from tests.helpers import WorkspaceCase
from tests.test_sources import FakeHttp, b64, gmail_message

TAIL = "Could you please send me the signed consent form by Friday?"


def by_reference(mid, thread, *, plain=None, html=None):
    """A multipart message whose text parts are stored as attachments (Gmail does this for large bodies)."""
    msg = gmail_message(mid, thread)
    parts = []
    if plain is not None:
        parts.append({"mimeType": "text/plain", "filename": "", "body": {"attachmentId": "ATT_PLAIN", "size": len(plain)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "filename": "", "body": {"attachmentId": "ATT_HTML", "size": len(html)}})
    parts.append({"mimeType": "application/pdf", "filename": "form.pdf", "body": {"attachmentId": "ATT_PDF", "size": 9}})
    msg["payload"] = {"mimeType": "multipart/mixed", "headers": msg["payload"]["headers"], "parts": parts}
    return msg


class GmailBodyTests(WorkspaceCase):
    def source(self, http):
        return GmailSource(account="primary", address="me@example.edu", token=lambda: "t", my_addresses=[],
                           http=http, initial_lookback_days=3)

    def history(self, msg, attachment):
        return FakeHttp([
            ("/history", {"history": [{"messagesAdded": [{"message": {"id": msg["id"], "threadId": msg["threadId"]}}]}],
                          "historyId": "200"}),
            (f"/threads/{msg['threadId']}", {"messages": [msg]}),
            ("/attachments/", attachment),
        ])

    def test_a_request_at_the_tail_of_a_long_mail_is_kept(self):
        body = "Background. " * 5000 + "\n" + TAIL
        http = self.history(gmail_message("m1", "t1", body=body), {})
        result = self.source(http).fetch(json.dumps({"history_id": "100"}))
        persist_fetch(self.conn, "gmail", result)
        [row] = self.conn.execute("SELECT id FROM source_items").fetchall()
        self.assertTrue(item_body(self.conn, row["id"]).endswith(TAIL), "the stored body is complete")

    def test_a_body_stored_by_reference_is_read(self):
        msg = by_reference("m1", "t1", plain="Hi,\n" + TAIL)
        http = self.history(msg, lambda p: {"data": b64("Hi,\n" + TAIL), "size": 60})
        [item] = self.source(http).fetch(json.dumps({"history_id": "100"})).items
        self.assertIn(TAIL, item.body)
        self.assertFalse(item.meta.get("body_incomplete"))
        fetched = [url for _, url, _ in http.calls if "/attachments/" in url]
        self.assertEqual(len(fetched), 1, "the file attachment itself is not downloaded")
        self.assertIn("/messages/m1/attachments/ATT_PLAIN", fetched[0])

    def test_an_html_body_stored_by_reference_becomes_text(self):
        html = f"<html><body><p>Hi,</p><p>{TAIL}</p><style>p{{}}</style></body></html>"
        msg = by_reference("m1", "t1", html=html)
        http = self.history(msg, lambda p: {"data": b64(html), "size": len(html)})
        [item] = self.source(http).fetch(json.dumps({"history_id": "100"})).items
        self.assertIn(TAIL, item.body)
        self.assertNotIn("<p>", item.body)

    def test_a_failed_body_read_fails_the_fetch_and_recovers(self):
        msg = by_reference("m1", "t1", plain="Hi,\n" + TAIL)
        answers = [HttpError(503, "backend error"), {"data": b64("Hi,\n" + TAIL), "size": 60}]
        http = self.history(msg, lambda p: answers.pop(0))
        with self.assertRaises(SourceError):
            self.source(http).fetch(json.dumps({"history_id": "100"}))   # cursor not advanced: retried
        [item] = self.source(http).fetch(json.dumps({"history_id": "100"})).items
        self.assertIn(TAIL, item.body)

    def test_an_unreadable_body_part_is_recorded_as_incomplete(self):
        msg = by_reference("m1", "t1", plain="Hi,\n" + TAIL)
        http = self.history(msg, HttpError(404, "Requested entity was not found."))
        [item] = self.source(http).fetch(json.dumps({"history_id": "100"})).items
        self.assertTrue(item.meta["body_incomplete"])
        self.assertIn("could not be read", item.body)


class GmailContextTests(WorkspaceCase):
    def test_a_clipped_item_in_the_context_says_how_to_read_the_rest(self):
        from tests.helpers import mail
        from tests.flow import run_id_of
        from tests.test_intake import FakeSource
        from todo_manager import clock
        gmail = FakeSource("gmail", "mailbox:primary")
        self.cfg.data["email"]["min_wake_interval_minutes"] = 0
        gmail.push(mail("m1", "t1", "Background. " * 5000 + "\n" + TAIL))
        gate_intake.run_gate(self.conn, self.cfg, [gmail], [])
        clock.advance(minutes=16)
        out, _ = gate_intake.run_gate(self.conn, self.cfg, [gmail], [])
        run_id = run_id_of(out)
        context = out
        self.assertNotIn(TAIL, context)
        self.assertIn("source show", context)
        [item] = run_items(self.conn, run_id)
        self.assertTrue(item_body(self.conn, item["id"]).endswith(TAIL))
