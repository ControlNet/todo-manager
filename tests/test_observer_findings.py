"""Findings of the trial observer's reviews (2026-10-11..14) that the intake prompt cannot fix.
Synthetic workspace, mail and tasks only."""

from todo_manager import clock, gate_intake, health, hermes_install, store
from tests.flow import intake_context, run_id_of
from tests.helpers import WorkspaceCase, mail
from tests.test_sources import FakeHttp, gmail_message
from todo_manager.sources.gmail import GmailSource

OTHERS = [f"person{i}@x.example" for i in range(1, 14)]


class RecipientTests(WorkspaceCase):
    """A 15-recipient mail listed the user 14th; the context showed the first 8 only, and the run
    dismissed it as not addressed to the user."""

    def source(self):
        return GmailSource(account="primary", address="me@example.edu", token=lambda: "t", my_addresses=[],
                           http=FakeHttp([]), initial_lookback_days=3)

    def test_gmail_records_how_many_recipients_and_where_the_user_is(self):
        to = ", ".join(OTHERS + ["me@example.edu"])
        item = self.source().normalize(gmail_message("m1", "t1", extra=[{"name": "To", "value": to},
                                                                          {"name": "Cc", "value": "cc@x.example"}]))
        self.assertEqual((item.meta["recipients"], item.meta["me_in"]), (15, "to"))
        copied = self.source().normalize(gmail_message("m2", "t2", extra=[
            {"name": "To", "value": "a@x.example"}, {"name": "Cc", "value": "me@example.edu"}]))
        self.assertEqual(copied.meta["me_in"], "cc")
        bcc = self.source().normalize(gmail_message("m3", "t3", extra=[{"name": "To", "value": "list@x.example"}]))
        self.assertIsNone(bcc.meta["me_in"])

    def woken_context(self, meta):
        item = mail("m1", "t1", "Please send your publication list")
        item.meta.update(meta)
        self.add_items(item)
        clock.advance(minutes=61)
        out, woke = gate_intake.run_gate(self.conn, self.cfg, [], [])
        self.assertTrue(woke)
        return intake_context(self.conn, self.cfg, run_id_of(out))

    def test_the_context_says_how_many_more_and_that_the_user_is_one(self):
        text = self.woken_context({"to": OTHERS + ["me@example.edu", "z@x.example"], "recipients": 15,
                                   "me_in": "to"})
        self.assertIn("(+7 more)", text)
        self.assertIn("the user is in To", text)

    def test_earlier_mail_finds_the_user_among_the_stored_recipients(self):
        text = self.woken_context({"to": OTHERS + ["me@example.edu"]})
        self.assertIn("(+6 more)", text)
        self.assertIn("the user is a recipient", text)


class PassedDueCheckTests(WorkspaceCase):
    """A passed-due-date check that no run settled is marked failed; health reported only failed
    follow-up checks, so these were silent."""

    def test_a_failed_passed_due_date_check_is_reported(self):
        task = store.create_task(self.conn, "Submit the form", origin="intake", status="decision",
                                 due_date="2026-10-01")
        self.conn.execute("INSERT INTO followup_checks(task_id, due, state, attempts, updated_at)"
                          " VALUES (?,?,?,?,?)", (task, gate_intake.DUE_CHECK + "2026-10-01", "failed", 2, clock.iso()))
        rep = health.report(self.conn, self.cfg)
        self.assertEqual(rep["followups_failed"], [task])
        self.assertTrue(any(p.startswith("follow-ups or passed due dates") for p in rep["problems"]))

    def test_a_check_for_an_old_date_is_not_reported(self):
        task = store.create_task(self.conn, "Submit the form", origin="intake", due_date="2026-10-09")
        self.conn.execute("INSERT INTO followup_checks(task_id, due, state, attempts, updated_at)"
                          " VALUES (?,?,?,?,?)", (task, gate_intake.DUE_CHECK + "2026-10-01", "failed", 2, clock.iso()))
        self.assertEqual(health.report(self.conn, self.cfg)["followups_failed"], [])


class DeliveryMediumTests(WorkspaceCase):
    """The user asked for a copy in Google Slides, not to send it; the brief forbade the copy and
    the agent delivered a PDF."""

    def test_the_delegation_brief_keeps_the_medium_the_user_asked_for(self):
        text = " ".join(hermes_install.asset("cron/feedback.md").split())
        self.assertIn("Keep the medium the user asked for", text)
        self.assertIn('"do not send" limits sending, not the format', text)
