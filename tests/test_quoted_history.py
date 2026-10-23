"""Observer review 2026-10-22: the quoted history of an email reply was always left out, on the
assumption that the earlier messages are shown on their own. When the user was added to a thread
later, or the quoted mail was never read, the quote was the only copy of the facts: candidate
visit dates and an internal deadline vanished, and the run wrote "no specific date given". A
reply header Gmail wrapped over two lines was not recognised either, so the quoted lines went
without any marker. Now the quote is left out only when an earlier message of the conversation
is in the context, and every cut says so and names the command printing the full message.
All mail is synthetic; sources are ``FakeSource`` (test double)."""

from todo_manager import clock, gate_intake, hermes_install, ops
from todo_manager.context import run_items, trim_quoted
from tests.flow import run_ctx, run_id_of, wake_of
from tests.helpers import WorkspaceCase, mail
from tests.test_intake import FakeSource

ORIGINAL = ("Dear Bob,\n\nCould the acme-lab team visit you on December 15th or 16th? We would come for a day.\n\n"
            "Best,\nAlice")
REPLY = ("Hi Alice,\r\n\r\nEither day works for us, I will confirm with the team.\r\n\r\nBob\r\n\r\n"
         "On Tue, 20 Oct 2026 at 10:15, Alice Example <\r\nalice@example.org> wrote:\r\n\r\n"
         + "".join(f"> {line}\r\n" if line else ">\r\n" for line in ORIGINAL.split("\n")))


class QuotedHistoryContextTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.gmail = FakeSource("gmail", "mailbox:primary")
        self.cfg.data["email"]["min_wake_interval_minutes"] = 0

    def context(self, *items):
        self.gmail.push(*items)
        gate_intake.run_gate(self.conn, self.cfg, [self.gmail], [])
        clock.advance(minutes=16)
        out, woke = gate_intake.run_gate(self.conn, self.cfg, [self.gmail], [])
        self.assertTrue(woke)
        self.assertEqual(woke, wake_of(out))
        return out

    def finish(self, out):
        run_id = run_id_of(out)
        ids = [i["id"] for i in run_items(self.conn, run_id)]
        ops.item_dismiss(self.conn, self.cfg, run_ctx(self.conn, self.cfg, run_id), ids, outcome="informational",
                         reason="test")
        ops.run_done(self.conn, self.cfg, run_id)

    def test_a_reply_whose_quoted_mail_was_never_read_keeps_the_quote(self):
        out = self.context(mail("m2", "t1", REPLY, subject="Re: visit"))
        self.assertIn("December 15th or 16th", out)
        self.assertIn("We would come for a day", out)
        self.assertNotIn("left out", out)

    def test_a_reply_whose_quoted_mail_is_shown_leaves_the_quote_out_with_a_marker(self):
        self.finish(self.context(mail("m1", "t1", ORIGINAL, subject="Visit")))
        clock.advance(minutes=30)
        out = self.context(mail("m2", "t1", REPLY, subject="Re: visit"))
        self.assertIn("[first message]", out)
        self.assertEqual(out.count("December 15th or 16th"), 1, "the original shows the dates; the quote is left out")
        self.assertIn(f"quoted earlier messages left out; full message: `todo-manager source show {self.item_id('m2')}`",
                      out)
        self.assertIn("Either day works for us", out)

    def test_a_reply_after_its_quoted_mail_in_the_same_batch_leaves_the_quote_out(self):
        out = self.context(mail("m1", "t1", ORIGINAL, subject="Visit"),
                           mail("m2", "t1", REPLY, subject="Re: visit", minutes=1))
        self.assertEqual(out.count("December 15th or 16th"), 1)
        self.assertIn(f"full message: `todo-manager source show {self.item_id('m2')}`", out)


class TrimQuotedTests(WorkspaceCase):
    MORE = "todo-manager source show src_1"

    def test_a_header_gmail_wrapped_inside_the_address_is_recognised(self):
        text = ("Thanks Alice, the acme-lab slides are attached.\n\n"
                "On Tue, 20 Oct 2026 at 10:15, Alice Example <\nalice@example.org> wrote:\n\n"
                "Please send the slides.")
        trimmed = trim_quoted(text, self.MORE)
        self.assertNotIn("Please send the slides", trimmed)
        self.assertNotIn("alice@example.org", trimmed)
        self.assertTrue(trimmed.endswith(f"[quoted earlier messages left out; full message: `{self.MORE}`]"))

    def test_the_address_may_start_the_second_line(self):
        text = ("Thanks Alice, the acme-lab slides are attached.\n\n"
                "On Tue, 20 Oct 2026 at 10:15, Alice Example\n<alice@example.org> wrote:\nPlease send the slides.")
        self.assertNotIn("Please send the slides", trim_quoted(text, self.MORE))

    def test_a_wrapped_chinese_header_is_recognised(self):
        text = ("Thanks Alice, the acme-lab slides are attached.\n\n"
                "在 2026年10月20日周二 10:15，Alice Example <\nalice@example.org>"
                " 写道：\n\nPlease send the slides.")
        trimmed = trim_quoted(text, self.MORE)
        self.assertNotIn("Please send the slides", trimmed)
        self.assertIn("left out", trimmed)

    def test_prose_over_two_lines_is_not_a_header(self):
        text = ("Thanks Alice, the acme-lab slides are attached.\n\n"
                "On Monday the team met at noon and\nBob wrote:\nthe plan for December.")
        self.assertEqual(trim_quoted(text, self.MORE), text)

    def test_quoted_lines_left_out_without_a_known_header_are_marked(self):
        text = ("Thanks Alice, the acme-lab slides are attached.\n\nAlice Example schrieb am 20.10.2026:\n"
                "> Please send the slides by Thursday, 22 October.")
        trimmed = trim_quoted(text, self.MORE)
        self.assertNotIn("22 October", trimmed)
        self.assertIn(f"[quoted earlier messages left out; full message: `{self.MORE}`]", trimmed)

    def test_text_without_quotes_is_unchanged(self):
        text = "Thanks Alice, the acme-lab slides are attached."
        self.assertEqual(trim_quoted(text, self.MORE), text)


class FullSourceBeforeAbsenceTests(WorkspaceCase):
    """Telling an internal deadline from the date a personal reply is due is the model's reading;
    what code can check is that the prompt sends it to the full message before it calls a date or
    detail absent."""

    def test_the_intake_reads_the_full_message_before_calling_a_detail_absent(self):
        text = " ".join(hermes_install.asset("cron/intake.md").split())
        self.assertIn("or one whose quoted earlier messages were left out, says how to read the rest", text)
        self.assertIn("before writing that a date or detail is not given", text)
