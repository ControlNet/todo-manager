"""Delegation lifecycle under lost responses, late results and concurrent human actions.

Uses ``FakeKanban`` (test double of the Hermes board; the same transitions run against the
installed Hermes code in ``tests/integration``). Ids R01/R02/R05/R08/R15 refer to the
reliability review (docs/acceptance.md).
"""

import contextlib
import io

from todo_manager import cli, delegation, health, store
from todo_manager.kanban import FakeKanban
from tests.helpers import WorkspaceCase


class Base(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.kb = FakeKanban()
        self.task = store.create_task(self.conn, "回复 Alice 关于数据集的邮件", origin="intake",
                                      responsibility="Alice asked me", summary="Alice wants the split files")

    def delegate(self, task=None, **kw):
        return delegation.create(self.conn, self.cfg, task_id=task or self.task, title=kw.pop("title", "起草回复"),
                                 goal="Draft a reply to Alice; do not send", **kw)

    def tick(self):
        delegation.send_messages(self.conn, self.cfg, self.kb)
        delegation.submit_pending(self.conn, self.cfg, self.kb)
        delegation.sync_cards(self.conn, self.cfg, self.kb)

    def d(self, did):
        return delegation.get(self.conn, did)

    def msg(self, mid):
        return self.conn.execute("SELECT * FROM delegation_messages WHERE id=?", (mid,)).fetchone()

    def events(self, card, kind):
        return [e for e in self.kb.events if e["task_id"] == card and e["kind"] == kind]

    def outbox_texts(self):
        return [r["payload"] for r in self.conn.execute("SELECT payload FROM outbox ORDER BY created_at")]

    def running(self, did=None):
        did = did or self.delegate()
        self.tick()
        card = self.d(did)["card_id"]
        self.kb.worker_start(card)
        self.tick()
        return did, card

    def delivered(self, did=None, body="Dear Alice v1"):
        did, card = self.running(did)
        self.kb.worker_review(card, body[:20], {"deliverable_markdown": body})
        self.tick()
        return did, card


class CancellationTests(Base):
    """R01: a cancelled Task stops its delegations from every entry point."""

    def test_cancel_before_submit_never_creates_a_card(self):
        did = self.delegate()
        # Any entry (CLI, Notion import, migration) closes the Task without calling the delegation hooks.
        store.update_task(self.conn, self.task, {"status": "cancelled"}, actor="human", origin="cli")
        self.tick()
        self.tick()
        self.assertEqual(self.kb.cards, {})
        self.assertEqual(self.d(did)["status"], "cancelled")

    def test_submit_rechecks_the_task(self):
        did = self.delegate()
        store.update_task(self.conn, self.task, {"status": "done"}, actor="human", origin="notion")
        delegation.submit_pending(self.conn, self.cfg, self.kb)
        self.assertEqual(self.kb.cards, {})
        self.assertEqual(self.d(did)["status"], "pending_submit")

    def test_cancel_after_uncertain_create_reconciles_then_archives(self):
        did = self.delegate()
        self.kb.lose_response.add("create")
        self.tick()
        self.assertEqual(self.d(did)["status"], "reconcile")
        store.update_task(self.conn, self.task, {"status": "cancelled"}, actor="human", origin="cli")
        self.tick()
        self.assertEqual(len(self.kb.cards), 1, "no second card")
        card = next(iter(self.kb.cards.values()))
        self.assertEqual(card["status"], "archived")
        self.assertEqual(self.d(did)["status"], "cancelled")
        self.assertEqual(self.d(did)["card_id"], card["id"])

    def test_cli_cancel_of_running_delegation_terminates_and_confirms(self):
        did, card = self.running()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--home", str(self.tmp), "task", "update", self.task,
                                       "--set", "status=cancelled"]), 0)
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "archived")
        self.assertEqual(len(self.events(card, "archive_worker_termination")), 1)
        d = self.d(did)
        self.assertEqual((d["status"], d["cancel_state"]), ("cancelled", "confirmed"))
        self.assertEqual(store.get_task(self.conn, self.task)["status"], "cancelled")

    def test_unconfirmed_termination_is_reported(self):
        did, card = self.running()
        self.kb.terminate_ok = False
        store.update_task(self.conn, self.task, {"status": "cancelled"}, actor="human", origin="cli")
        self.tick()
        self.assertEqual(self.d(did)["cancel_state"], "unconfirmed")
        self.assertTrue(any("worker" in t.lower() or "进程" in t for t in self.outbox_texts()))
        self.assertTrue(any(did in p for p in health.report(self.conn, self.cfg)["problems"]))

    def test_lost_archive_response_is_confirmed_not_repeated(self):
        did, card = self.running()
        self.kb.lose_response.add("archive")
        store.update_task(self.conn, self.task, {"status": "cancelled"}, actor="human", origin="cli")
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "archived")
        cancel = "SELECT status FROM delegation_messages WHERE delegation_id=? AND kind='cancel'"
        self.assertEqual(self.conn.execute(cancel, (did,)).fetchone()[0], "sending")
        self.assertIsNone(self.d(did)["cancel_state"], "not confirmed yet")
        self.tick()
        self.assertEqual(self.conn.execute(cancel, (did,)).fetchone()[0], "sent")
        self.assertEqual((self.d(did)["status"], self.d(did)["cancel_state"]), ("cancelled", "confirmed"))
        self.assertEqual(len(self.events(card, "archived")), 1)

    def test_late_result_after_cancel_is_kept_but_changes_nothing(self):
        did, card = self.running()
        # The worker hands in a result while the user is cancelling (before our next sync).
        self.kb.worker_review(card, "迟到的草稿", {"deliverable_markdown": "late draft"})
        store.update_task(self.conn, self.task, {"status": "cancelled"}, actor="human", origin="cli")
        before = len(self.outbox_texts())
        self.tick()
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "archived")
        dl = delegation.deliveries(self.conn, did)
        self.assertEqual([(x["body"], x["after_cancel"]) for x in dl], [("late draft", 1)])
        self.assertFalse([t for t in self.outbox_texts()[before:] if "迟到" in t], "no announcement")
        self.assertEqual(store.get_task(self.conn, self.task)["status"], "cancelled")

    def test_reopened_task_withdraws_unsent_cancel(self):
        did, card = self.running()
        store.update_task(self.conn, self.task, {"status": "cancelled"}, actor="human", origin="notion")
        delegation.on_task_closed(self.conn, self.task, "cancelled", "不做了", None)
        store.update_task(self.conn, self.task, {"status": "in_progress"}, actor="human", origin="notion")
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "running")
        self.assertEqual(self.d(did)["status"], "running")
        cancel = self.conn.execute("SELECT status FROM delegation_messages WHERE delegation_id=? AND kind='cancel'",
                                   (did,)).fetchone()
        self.assertEqual(cancel["status"], "superseded")


class StaleTransitionTests(Base):
    """R02: a retried answer/resume/revision/approval only acts on the event it was bound to."""

    def blocked(self, question="截止日期是周四还是周五？"):
        did, card = self.running()
        self.kb.worker_block(card, question)
        self.tick()
        self.assertEqual(self.d(did)["status"], "blocked")
        return did, card

    def test_retried_answer_after_lost_unblock_does_not_release_the_next_question(self):
        did, card = self.blocked()
        a1 = delegation.add_message(self.conn, did, "answer", "周四", feedback_id="fbk_a1")
        self.kb.lose_response.add("unblock")
        delegation.send_messages(self.conn, self.cfg, self.kb)
        self.assertEqual(self.msg(a1)["status"], "sending")
        self.assertEqual(self.kb.cards[card]["status"], "ready", "the remote unblock happened")
        self.kb.worker_start(card)
        self.kb.worker_block(card, "收件人要抄送谁？")
        status_q2 = self.kb.cards[card]["status"]
        self.tick()  # send (retry) runs before sync sees the new question
        self.assertEqual(self.kb.cards[card]["status"], status_q2, "Q2 must stay open")
        self.assertEqual(self.msg(a1)["status"], "sent", "A1 was delivered by the lost call")
        self.assertEqual(self.d(did)["block_seq"], 2)
        delegation.add_message(self.conn, did, "answer", "抄送 Bob", feedback_id="fbk_a2")
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "ready")

    def test_answer_after_someone_else_unblocked_is_not_applied_to_the_next_question(self):
        did, card = self.blocked()
        a1 = delegation.add_message(self.conn, did, "answer", "周四", feedback_id="fbk_a1")
        self.kb.unblock(card)  # resolved on the dashboard before our message went out
        self.kb.worker_start(card)
        self.kb.worker_block(card, "收件人要抄送谁？", kind="capability")
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "blocked")
        self.assertEqual(self.msg(a1)["status"], "superseded")
        self.assertFalse([c for c in self.kb.comments if f"[tm:{a1}]" in c["body"]], "not posted to the new question")
        self.assertTrue(any("周四" in t for t in self.outbox_texts()), "the user is told their answer was not used")

    def test_retried_resume_does_not_release_a_later_question(self):
        did, card = self.running()
        delegation.add_message(self.conn, did, "pause", "先停", feedback_id="fbk_p")
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "blocked")
        r = delegation.add_message(self.conn, did, "resume", "继续", feedback_id="fbk_r")
        self.kb.lose_response.add("unblock")
        delegation.send_messages(self.conn, self.cfg, self.kb)
        self.assertEqual(self.kb.cards[card]["status"], "ready")
        self.kb.worker_start(card)
        self.kb.worker_block(card, "要附上数据集链接吗？")
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "blocked")
        self.assertEqual(self.msg(r)["status"], "sent")
        self.assertEqual(self.d(did)["status"], "blocked")

    def test_second_pause_parked_in_triage_still_resumes(self):
        did, card = self.running()
        for n in (1, 2):
            delegation.add_message(self.conn, did, "pause", f"停 {n}", feedback_id=f"fbk_p{n}")
            self.tick()
            delegation.add_message(self.conn, did, "resume", f"继续 {n}", feedback_id=f"fbk_r{n}")
            self.tick()
            self.assertEqual(self.kb.cards[card]["status"], "ready", f"round {n}")
            self.kb.worker_start(card)

    def test_retried_revision_does_not_reopen_the_next_version(self):
        did, card = self.delivered()
        r1 = delegation.add_message(self.conn, did, "revision", "改短", feedback_id="fbk_rev")
        self.kb.lose_response.add("reopen_review")
        delegation.send_messages(self.conn, self.cfg, self.kb)
        self.assertEqual(self.kb.cards[card]["status"], "ready")
        self.kb.worker_start(card)
        self.kb.worker_review(card, "v2", {"deliverable_markdown": "Dear Alice v2"})
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "review", "v2 waits for the human")
        self.assertEqual(self.msg(r1)["status"], "sent")
        self.assertEqual(len(self.events(card, "review_reopened")), 1)


class CapacityTests(Base):
    """R05: one shared reservation for submit/answer/resume/revision/approval."""

    overrides = {"kanban": {"max_running": 1}}

    def two_blocked(self):
        other = store.create_task(self.conn, "Another", origin="cli")
        first = self.delegate()
        self.tick()
        c1 = self.d(first)["card_id"]
        self.kb.worker_start(c1)
        self.kb.worker_block(c1, "Q1?")
        self.tick()
        second = self.delegate(task=other, title="t2")
        self.tick()
        c2 = self.d(second)["card_id"]
        self.kb.worker_start(c2)
        self.kb.worker_block(c2, "Q2?")
        self.tick()
        return (first, c1), (second, c2)

    def test_two_answers_in_one_round_respect_max_running(self):
        (first, c1), (second, c2) = self.two_blocked()
        delegation.add_message(self.conn, first, "answer", "A1", feedback_id="fbk_1")
        delegation.add_message(self.conn, second, "answer", "A2", feedback_id="fbk_2")
        delegation.send_messages(self.conn, self.cfg, self.kb)
        self.assertEqual(sorted(self.kb.cards[c]["status"] for c in (c1, c2)), ["blocked", "ready"])

    def test_uncertain_resume_keeps_its_reservation(self):
        (first, c1), (second, c2) = self.two_blocked()
        delegation.add_message(self.conn, first, "answer", "A1", feedback_id="fbk_1")
        self.kb.fail_next["transition"] = delegation.KanbanError("timeout", uncertain=True)
        delegation.send_messages(self.conn, self.cfg, self.kb)
        delegation.add_message(self.conn, second, "answer", "A2", feedback_id="fbk_2")
        delegation.send_messages(self.conn, self.cfg, self.kb)
        self.assertEqual(self.kb.cards[c2]["status"], "blocked", "the uncertain transition still holds the slot")


class ApprovalTests(Base):
    """R08: an approval is sent only while it is valid, and used at most once."""

    def approved_v1(self):
        did, card = self.delivered()
        v1 = delegation.deliveries(self.conn, did)[0]["id"]
        aid = delegation.record_approval(self.conn, did, v1, "send_email", "发给 Alice", "fbk_ok")
        return did, card, v1, aid

    def approval_msg(self, aid):
        return self.conn.execute("SELECT * FROM delegation_messages WHERE approval_id=?", (aid,)).fetchone()

    def test_new_version_voids_an_unsent_approval(self):
        did, card, v1, aid = self.approved_v1()
        # Before our message goes out, the card is reopened elsewhere and v2 arrives.
        self.kb.reopen_review(card)
        self.kb.worker_start(card)
        self.kb.worker_review(card, "v2", {"deliverable_markdown": "Dear Alice v2"})
        delegation.sync_cards(self.conn, self.cfg, self.kb)
        self.tick()
        self.assertFalse([c for c in self.kb.comments if "APPROVED" in c["body"]])
        self.assertEqual(self.kb.cards[card]["status"], "review")
        self.assertEqual(self.approval_msg(aid)["status"], "superseded")
        self.assertIsNotNone(self.conn.execute("SELECT revoked_at FROM approvals WHERE id=?", (aid,)).fetchone()[0])

    def test_requirement_change_voids_an_unsent_approval(self):
        did, card, v1, aid = self.approved_v1()
        delegation.add_message(self.conn, did, "supplement", "另外抄送 Bob", feedback_id="fbk_sup")
        self.tick()
        self.assertFalse([c for c in self.kb.comments if "APPROVED" in c["body"]])
        self.assertEqual(self.kb.cards[card]["status"], "review")
        self.assertEqual(self.approval_msg(aid)["status"], "superseded")

    def test_send_time_check_does_not_trust_the_stored_message(self):
        did, card, v1, aid = self.approved_v1()
        self.conn.execute("UPDATE approvals SET revoked_at='2026-10-05T00:00:00+00:00' WHERE id=?", (aid,))
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "review")
        self.assertEqual(self.approval_msg(aid)["status"], "superseded")

    def test_approval_is_used_once(self):
        did, card, v1, aid = self.approved_v1()
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "ready")
        self.assertIsNotNone(self.conn.execute("SELECT consumed_at FROM approvals WHERE id=?", (aid,)).fetchone()[0])
        self.kb.worker_start(card)
        self.kb.worker_review(card, "sent", {"deliverable_markdown": "sent", "external_actions_done": ["send_email"]})
        self.tick()
        # A replay of the old message (e.g. restored backup) must not reopen the new version.
        self.conn.execute("UPDATE delegation_messages SET status='pending' WHERE approval_id=?", (aid,))
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "review")
        self.assertEqual(len(self.events(card, "review_reopened")), 1)

    def test_crash_after_approval_is_held_for_the_user(self):
        did, card, v1, aid = self.approved_v1()
        self.tick()
        self.kb.worker_start(card)
        self.kb.worker_crash(card)
        self.tick()
        self.assertEqual(self.conn.execute("SELECT outcome FROM approvals WHERE id=?", (aid,)).fetchone()[0], "unknown")
        self.tick()
        self.assertIn(self.kb.cards[card]["status"], ("blocked", "triage"), "no automatic retry of the external action")
        self.assertTrue(any("send_email" in t for t in self.outbox_texts()))


class DeliveryRunTests(Base):
    """R15: each review event is read from its own run."""

    def two_versions_unsynced(self):
        did, card = self.running()
        self.kb.worker_review(card, "v1", {"deliverable_markdown": "Dear Alice v1"})
        self.kb.reopen_review(card)
        self.kb.worker_start(card)
        self.kb.worker_review(card, "v2", {"deliverable_markdown": "Dear Alice v2"})
        return did, card

    def test_each_review_event_reads_its_own_run(self):
        did, card = self.two_versions_unsynced()
        self.tick()
        dl = delegation.deliveries(self.conn, did)
        self.assertEqual([(x["seq"], x["run_id"], x["body"]) for x in dl],
                         [(1, "1", "Dear Alice v1"), (2, "2", "Dear Alice v2")])

    def test_offline_recovery_and_replay_do_not_duplicate(self):
        did, card = self.two_versions_unsynced()
        self.kb.fail_next["snapshot"] = delegation.KanbanError("offline")
        delegation.sync_cards(self.conn, self.cfg, self.kb)
        self.assertEqual(delegation.deliveries(self.conn, did), [])
        self.tick()
        outbox = len(self.outbox_texts())
        # Restored backup: the event cursor is older than the deliveries we already hold.
        self.conn.execute("UPDATE delegations SET event_cursor=0 WHERE id=?", (did,))
        self.tick()
        self.assertEqual(len(delegation.deliveries(self.conn, did)), 2)
        self.assertEqual(len(self.outbox_texts()), outbox, "nothing announced twice")

    def test_approval_after_replay_binds_to_the_latest_version(self):
        did, card = self.two_versions_unsynced()
        self.tick()
        v1, v2 = [x["id"] for x in delegation.deliveries(self.conn, did)]
        with self.assertRaises(delegation.DelegationError):
            delegation.record_approval(self.conn, did, v1, "send_email", "", "fbk_x")
        aid = delegation.record_approval(self.conn, did, v2, "send_email", "", "fbk_y")
        self.tick()
        self.assertIn(f"APPROVED send_email for delivery {v2}", self.kb.comments[-1]["body"])
        self.assertEqual(self.conn.execute("SELECT delivery_id FROM approvals WHERE id=?", (aid,)).fetchone()[0], v2)
