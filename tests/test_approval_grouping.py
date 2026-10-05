"""C09: every approval keeps its own identity, and all approvals for one delivered version reach
the worker in the same run.

* Two approvals for the same version (one feedback with two approve ops, or two feedback items)
  are each recorded with their own message; at send time they go out together - one comment
  each, then one reopen of that version's review - so neither is merged into the other nor
  voided by the reopen the other caused.
* A retried command (same feedback item, same arguments) returns its approval; an identical approval that has
  not gone out yet is the same approval, from any entry point.
* An approval that can no longer reach the run of its version is refused when it is received
  (the version was already sent back, or approvals for it are being delivered), never reported
  as recorded and dropped later.

Kanban is ``FakeKanban`` (test double).
"""

from todo_manager import delegation
from tests.flow import apply_feedback_ops, user_feedback
from tests.test_delegation_binding import BindingBase


class ApprovalGroupTests(BindingBase):
    def v1(self, did):
        return delegation.deliveries(self.conn, did)[0]["id"]

    def approved(self, card):
        return [c["body"] for c in self.kb.comments if c["task_id"] == card and "APPROVED" in c["body"]]

    def approvals(self, did):
        return self.conn.execute("SELECT a.*, m.status AS mstatus FROM approvals a LEFT JOIN delegation_messages m"
                                 " ON m.approval_id=a.id WHERE a.delegation_id=? ORDER BY a.granted_at, a.rowid",
                                 (did,)).fetchall()

    def assert_both_delivered_in_one_run(self, did, card):
        self.assertEqual(len(self.approved(card)), 2, "one comment per approval")
        self.assertEqual(len(self.events(card, "review_reopened")), 1, "one run for both")
        rows = self.approvals(did)
        self.assertEqual([r["mstatus"] for r in rows], ["sent", "sent"])
        self.assertTrue(all(r["consumed_at"] and not r["revoked_at"] for r in rows))
        self.assertFalse([t for t in self.outbox_texts() if "void" in t or "作废" in t])
        reopen = self.events(card, "review_reopened")[0]["id"]
        comments = [e["id"] for e in self.kb.events if e["task_id"] == card and e["kind"] == "commented"]
        self.assertTrue(all(c < reopen for c in comments[-2:]), "both are on the card before the run starts")

    def test_two_approve_ops_in_one_feedback_are_both_delivered(self):
        did, card = self.delivered()
        fb = user_feedback(self.conn, "c-two", origin="notion_comment")
        ops = [{"op": "approve", "delegation": did, "delivery": self.v1(did), "action": "send_email", "detail": "发给 Alice"},
               {"op": "approve", "delegation": did, "delivery": self.v1(did), "action": "comment_github",
                "detail": "在 issue 下回复"}]
        apply_feedback_ops(self.conn, self.cfg, fb, ops)
        self.assertEqual(len(self.approvals(did)), 2)
        self.assertTrue(all(r["mstatus"] == "pending" for r in self.approvals(did)), "each has its own message")
        self.tick()
        self.assert_both_delivered_in_one_run(did, card)

    def test_approvals_from_two_feedback_items_go_out_together(self):
        did, card = self.delivered()
        delegation.record_approval(self.conn, did, self.v1(did), "send_email", "发给 Alice", "fbk_a")
        delegation.record_approval(self.conn, did, self.v1(did), "comment_github", "", "fbk_b")
        self.tick()
        self.assert_both_delivered_in_one_run(did, card)

    def test_a_retried_command_returns_its_approval(self):
        did, card = self.delivered()
        fb = user_feedback(self.conn, "c-r", origin="notion_comment")
        ops = [{"op": "approve", "delegation": did, "delivery": self.v1(did), "action": "send_email", "detail": ""}]
        [first] = apply_feedback_ops(self.conn, self.cfg, fb, ops)
        [again] = apply_feedback_ops(self.conn, self.cfg, fb, ops)
        self.assertEqual(first["approval"], again["approval"])
        self.assertEqual(len(self.approvals(did)), 1)

    def test_the_same_approval_twice_is_one_approval(self):
        did, card = self.delivered()
        a = delegation.record_approval(self.conn, did, self.v1(did), "send_email", "发给 Alice", None)
        b = delegation.record_approval(self.conn, did, self.v1(did), "send_email", "发给 Alice", None)
        self.assertEqual(a, b)
        self.tick()
        self.assertEqual(len(self.approved(card)), 1)

    def test_an_approval_after_the_version_was_sent_back_is_refused_at_once(self):
        did, card = self.delivered()
        delegation.record_approval(self.conn, did, self.v1(did), "send_email", "", "fbk_a")
        self.tick()                                   # sent: the run with the approval is queued
        with self.assertRaises(delegation.DelegationError):
            delegation.record_approval(self.conn, did, self.v1(did), "comment_github", "", "fbk_b")
        self.assertEqual(len(self.approvals(did)), 1)

    def test_an_approval_while_others_are_in_flight_is_refused(self):
        did, card = self.delivered()
        delegation.record_approval(self.conn, did, self.v1(did), "send_email", "", "fbk_a")
        self.kb.lose_response.add("reopen_review")
        self.send()
        with self.assertRaises(delegation.DelegationError):
            delegation.record_approval(self.conn, did, self.v1(did), "comment_github", "", "fbk_b")
        self.send()                                   # the retry finds its reopen: sent once
        self.assertEqual(len(self.events(card, "review_reopened")), 1)
        self.assertEqual([r["mstatus"] for r in self.approvals(did)], ["sent"])

    def test_a_new_version_in_between_voids_both_with_a_notice(self):
        did, card = self.delivered()
        delegation.record_approval(self.conn, did, self.v1(did), "send_email", "", "fbk_a")
        delegation.record_approval(self.conn, did, self.v1(did), "comment_github", "", "fbk_b")
        self.dashboard_v2(card)
        self.sync()
        self.send()
        self.assertEqual(self.approved(card), [])
        self.assertEqual([r["mstatus"] for r in self.approvals(did)], ["superseded", "superseded"])
        self.assertEqual(len([t for t in self.outbox_texts() if "void" in t or "作废" in t]), 2)
        v2 = delegation.deliveries(self.conn, did)[1]["id"]
        delegation.record_approval(self.conn, did, v2, "send_email", "", "fbk_c")
        self.send()
        self.assertEqual(len(self.approved(card)), 1)

    def test_a_paused_delegation_holds_the_whole_group(self):
        did, card = self.delivered()
        delegation.record_approval(self.conn, did, self.v1(did), "send_email", "", "fbk_a")
        delegation.record_approval(self.conn, did, self.v1(did), "comment_github", "", "fbk_b")
        delegation.add_message(self.conn, did, "pause", "先停", feedback_id="fbk_p")
        self.send()
        self.assertEqual([r["mstatus"] for r in self.approvals(did)], ["waiting_capacity", "waiting_capacity"])
        self.assertEqual(self.events(card, "review_reopened"), [])
        delegation.add_message(self.conn, did, "resume", "继续", feedback_id="fbk_r")
        self.send()
        self.assert_both_delivered_in_one_run(did, card)

    def test_both_run_results_are_settled_together(self):
        did, card = self.delivered()
        delegation.record_approval(self.conn, did, self.v1(did), "send_email", "", "fbk_a")
        delegation.record_approval(self.conn, did, self.v1(did), "comment_github", "", "fbk_b")
        self.tick()
        self.kb.worker_start(card)
        self.kb.worker_crash(card)
        self.tick()
        self.assertEqual([r["outcome"] for r in self.approvals(did)], ["unknown", "unknown"],
                         "the run that had both ended without reporting: both may or may not have happened")
