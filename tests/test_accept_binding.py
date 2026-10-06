"""C04/C06: an accept completes exactly the delivered version it was given for, from every entry
point, and a retry after a lost response checks the card before deciding anything.

C04 * An accept needs a delivered version under review when it is queued: the CLI, Matrix
      (``--request``) and the scheduled feedback run (``--feedback``) share ``delegation.add_message``, which
      refuses an accept with nothing to accept instead of storing one that would later complete
      whatever arrives next.
    * Messages queued by older code: ``target_seq`` NULL (before versions were bound) is bound
      to the latest version that existed when it was queued, or not used; ``target_seq`` 0 (an
      accept stored without a version) is never sent.
C06 * An accept whose complete call lost its response is checked against its bound review event
      when retried, whether or not a sync recorded the card as done meanwhile: the earlier call
      succeeded -> sent; nothing is completed twice and no card is reopened.

Kanban is ``FakeKanban`` (test double).
"""

import contextlib
import io
from datetime import timedelta

from todo_manager import cli, clock, delegation
from todo_manager.ops import OpError
from tests.flow import apply_direct, apply_feedback_ops, user_feedback
from tests.helpers import T0
from tests.test_delegation_binding import BindingBase


class AcceptNeedsADeliveryTests(BindingBase):
    def test_the_cli_refuses_an_accept_before_anything_was_delivered(self):
        did, card = self.running()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--home", str(self.cfg.home), "delegate", "message", did, "--kind", "accept", "--human",
                             "--body", "可以"])
        self.assertNotEqual(code, 0)
        self.assertIn("INVALID", out.getvalue())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM delegation_messages WHERE kind='accept'").fetchone()[0], 0)
        self.kb.worker_review(card, "v1", {"deliverable_markdown": "Dear Alice v1"})
        self.sync()
        self.send()
        self.assertEqual(self.kb.cards[card]["status"], "review", "the first result waits for review")

    def test_matrix_and_scheduled_feedback_refuse_it_too(self):
        did, card = self.running()
        ops = [{"op": "message", "delegation": did, "kind": "accept", "body": "可以"}]
        with self.assertRaises(OpError):
            apply_direct(self.conn, self.cfg, text="可以", op_list=ops, source_id="$evt1")
        with self.assertRaises(OpError):
            apply_feedback_ops(self.conn, self.cfg, user_feedback(self.conn, "c-x", origin="notion_comment"), ops)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM delegation_messages WHERE kind='accept'").fetchone()[0], 0)

    def test_an_accept_stored_without_a_version_is_never_sent(self):
        did, card = self.running()
        now = "2026-10-05T00:00:00.000Z"
        self.conn.execute("INSERT INTO delegation_messages(id, delegation_id, kind, body, requirement_version, status,"
                          " target_seq, created_at) VALUES ('evt_old', ?, 'accept', '可以', 1, 'pending', 0, ?)", (did, now))
        self.kb.worker_review(card, "v1", {"deliverable_markdown": "Dear Alice v1"})
        self.sync()
        self.send()
        self.assertEqual(self.kb.cards[card]["status"], "review")
        self.assertEqual(self.msg("evt_old")["status"], "superseded")
        self.assertTrue(self.unused_notices(), "the user is told")

    def legacy_accept(self, did, minutes):
        self.conn.execute("INSERT INTO delegation_messages(id, delegation_id, kind, body, requirement_version, status,"
                          " created_at) VALUES ('evt_legacy', ?, 'accept', '可以', 1, 'pending', ?)",
                          (did, clock.iso(T0 + timedelta(minutes=minutes))))

    def test_a_legacy_accept_binds_to_the_version_that_existed_when_it_was_queued(self):
        did, card = self.delivered()                  # v1 received at T0
        self.legacy_accept(did, 1)
        clock.set_now(T0 + timedelta(minutes=2))
        self.dashboard_v2(card)
        self.sync()
        self.send()
        self.assertEqual(self.kb.cards[card]["status"], "review", "v2 was not what the user accepted")
        self.assertEqual(self.msg("evt_legacy")["status"], "superseded")

    def test_a_legacy_accept_completes_the_version_it_was_given_for(self):
        did, card = self.delivered()
        self.legacy_accept(did, 1)
        clock.set_now(T0 + timedelta(minutes=2))
        self.send()
        self.assertEqual(self.kb.cards[card]["status"], "done")
        self.assertEqual(self.msg("evt_legacy")["status"], "sent")

    def test_a_legacy_accept_queued_in_the_same_instant_as_a_version_is_not_used(self):
        did, card = self.delivered()
        self.legacy_accept(did, 0)                    # cannot tell which version it was for
        self.send()
        self.assertEqual(self.kb.cards[card]["status"], "review")
        self.assertEqual(self.msg("evt_legacy")["status"], "superseded")

    def test_a_legacy_accept_queued_before_any_version_is_not_used(self):
        did, card = self.running()
        self.conn.execute("INSERT INTO delegation_messages(id, delegation_id, kind, body, requirement_version, status,"
                          " created_at) VALUES ('evt_legacy', ?, 'accept', '可以', 1, 'pending', ?)",
                          (did, "2026-10-04T00:00:00.000Z"))
        self.kb.worker_review(card, "v1", {"deliverable_markdown": "Dear Alice v1"})
        self.sync()
        self.send()
        self.assertEqual(self.kb.cards[card]["status"], "review")
        self.assertEqual(self.msg("evt_legacy")["status"], "superseded")


class AcceptRetryTests(BindingBase):
    def lost_accept(self):
        did, card = self.delivered()
        mid = delegation.add_message(self.conn, did, "accept", "可以", feedback_id="fbk_acc")
        self.kb.lose_response.add("complete")
        self.send()
        self.assertEqual(self.msg(mid)["status"], "sending")
        self.assertEqual(self.kb.cards[card]["status"], "done")
        return did, card, mid

    def test_sync_before_the_retry_does_not_turn_it_into_a_failure(self):
        did, card, mid = self.lost_accept()
        self.sync()
        self.assertEqual(self.d(did)["status"], "ended")
        self.send()
        self.assertEqual(self.msg(mid)["status"], "sent")
        self.assertIsNone(self.msg(mid)["error"])
        self.assertEqual(len(self.events(card, "completed")), 1, "completed once")

    def test_retry_before_sync(self):
        did, card, mid = self.lost_accept()
        self.send()
        self.sync()
        self.assertEqual(self.msg(mid)["status"], "sent")
        self.assertEqual(self.d(did)["status"], "ended")
        self.assertEqual(len(self.events(card, "completed")), 1)

    def test_later_card_events_do_not_confuse_the_check(self):
        did, card, mid = self.lost_accept()
        self.kb.archive(card)                         # someone archives the finished card
        self.sync()
        self.send()
        self.assertEqual(self.msg(mid)["status"], "sent", "the complete before the archive was ours")
        self.assertEqual(len(self.events(card, "completed")), 1)
        self.assertEqual(self.events(card, "review_reopened"), [], "nothing is reopened")

    def test_a_new_accept_on_an_ended_delegation_is_still_refused(self):
        did, card, mid = self.lost_accept()
        self.send()
        self.sync()
        with self.assertRaises(delegation.DelegationError):
            delegation.add_message(self.conn, did, "accept", "再接受一次")
