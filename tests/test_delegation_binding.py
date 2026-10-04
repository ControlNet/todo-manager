"""B03: accept / revision / resume act on the delivery or pause they were written for.

The binding is taken when the message is queued (the delivery the user replied to, or the
latest one then) and kept for every retry; a newer version recorded before the message goes
out makes it unused (the user is told), whichever of send and sync runs first. Kanban is
``FakeKanban`` (test double); the same transitions run against Hermes in tests/integration.
"""

from todo_manager import delegation, feedback_flow, store
from tests.test_delegation_reliability import Base


class BindingBase(Base):
    def dashboard_v2(self, card):
        """Someone reopens the review on the dashboard and the worker delivers v2."""
        self.kb.reopen_review(card)
        self.kb.worker_start(card)
        self.kb.worker_review(card, "v2", {"deliverable_markdown": "Dear Alice v2"})

    def sync(self):
        delegation.sync_cards(self.conn, self.cfg, self.kb)

    def send(self):
        delegation.send_messages(self.conn, self.cfg, self.kb)

    def unused_notices(self):
        return [t for t in self.outbox_texts() if "没有生效" in t or "not applied" in t]


class AcceptBindingTests(BindingBase):
    def test_accept_of_v1_does_not_complete_v2_when_sync_runs_first(self):
        did, card = self.delivered()
        mid = delegation.add_message(self.conn, did, "accept", "可以", feedback_id="fbk_acc")
        self.dashboard_v2(card)
        self.sync()                                   # v2 is recorded before the accept goes out
        self.send()
        self.assertEqual(self.kb.cards[card]["status"], "review", "v2 waits for the human")
        self.assertEqual(self.msg(mid)["status"], "superseded")
        self.assertTrue(self.unused_notices())

    def test_accept_of_v1_does_not_complete_v2_when_send_runs_first(self):
        did, card = self.delivered()
        mid = delegation.add_message(self.conn, did, "accept", "可以", feedback_id="fbk_acc")
        self.dashboard_v2(card)
        self.send()                                   # v2 is on the card, not yet recorded
        self.assertEqual(self.kb.cards[card]["status"], "review")
        self.assertEqual(self.msg(mid)["status"], "superseded")

    def test_a_retried_accept_keeps_its_version(self):
        did, card = self.delivered()
        mid = delegation.add_message(self.conn, did, "accept", "可以", feedback_id="fbk_acc")
        self.kb.lose_response.add("complete")
        self.send()
        self.assertEqual(self.msg(mid)["status"], "sending")
        self.assertEqual(self.kb.cards[card]["status"], "done")
        self.tick()
        self.assertEqual(self.msg(mid)["status"], "sent")
        self.assertEqual(len(self.events(card, "completed")), 1)

    def test_a_reply_to_the_v1_notice_binds_to_v1(self):
        did, card = self.delivered()
        [v1] = delegation.deliveries(self.conn, did)
        self.dashboard_v2(card)
        self.sync()
        fb = {"id": "fbk_reply", "origin": "notion_comment", "author_is_user": 1,
              "payload": {"delegation_id": did, "delivery_id": v1["id"], "delivery_kind": "result", "delivery_seq": 1}}
        report = feedback_flow.apply_ops(self.conn, self.cfg, fb, [
            {"op": "message", "delegation": did, "kind": "accept", "body": "就用这个"}])
        mid = report["ops"][0]["message"]
        self.assertEqual(self.msg(mid)["target_seq"], 1, "bound to the version the user replied to")
        self.send()
        self.assertEqual(self.kb.cards[card]["status"], "review")
        self.assertEqual(self.msg(mid)["status"], "superseded")

    def test_an_explicit_delivery_must_belong_to_the_delegation(self):
        did, _ = self.delivered()
        fb = {"id": "fbk_x", "origin": "matrix", "author_is_user": 1, "payload": {}}
        errors = feedback_flow._validate_ops(self.conn, fb, [
            {"op": "message", "delegation": did, "kind": "accept", "body": "ok", "delivery": "dlv_other"}], "ops")
        self.assertTrue(any("delivery" in e for e in errors), errors)


class RevisionBindingTests(BindingBase):
    def test_revision_of_v1_is_not_applied_to_v2_when_sync_runs_first(self):
        did, card = self.delivered()
        mid = delegation.add_message(self.conn, did, "revision", "第二段改短", feedback_id="fbk_rev")
        self.dashboard_v2(card)
        self.sync()
        self.send()
        self.assertEqual(self.kb.cards[card]["status"], "review")
        self.assertEqual(len(self.events(card, "review_reopened")), 1, "only the dashboard's reopen")
        self.assertEqual(self.msg(mid)["status"], "superseded")
        self.assertTrue(self.unused_notices())

    def test_revision_of_v1_is_not_applied_to_v2_when_send_runs_first(self):
        did, card = self.delivered()
        mid = delegation.add_message(self.conn, did, "revision", "第二段改短", feedback_id="fbk_rev")
        self.dashboard_v2(card)
        self.send()
        self.assertEqual(len(self.events(card, "review_reopened")), 1)
        self.assertEqual(self.msg(mid)["status"], "superseded")

    def test_a_requirement_written_while_running_reopens_the_version_built_without_it(self):
        did, card = self.running()
        mid = delegation.add_message(self.conn, did, "revision", "也要提到截止日期", feedback_id="fbk_rev")
        self.kb.worker_review(card, "v1", {"deliverable_markdown": "Dear Alice v1"})
        self.sync()                                   # v1 is recorded before the requirement went out
        [v1] = delegation.deliveries(self.conn, did)
        self.assertLess(v1["requirement_version"], self.d(did)["requirement_version"],
                        "v1 was built on the requirements the worker had received")
        self.send()
        self.assertEqual(len(self.events(card, "review_reopened")), 1)
        self.assertEqual(self.msg(mid)["status"], "sent")

    def test_an_approval_waits_for_a_requirement_not_yet_sent(self):
        did, card = self.running()
        delegation.add_message(self.conn, did, "revision", "也要提到截止日期", feedback_id="fbk_rev")
        self.kb.worker_review(card, "v1", {"deliverable_markdown": "Dear Alice v1"})
        self.sync()
        [v1] = delegation.deliveries(self.conn, did)
        with self.assertRaises(delegation.DelegationError):
            delegation.record_approval(self.conn, did, v1["id"], "send_email", "", "fbk_ok")


class ResumeBindingTests(BindingBase):
    def test_resume_in_the_same_round_as_its_pause_lifts_it(self):
        did, card = self.running()
        delegation.add_message(self.conn, did, "pause", "先停", feedback_id="fbk_p")
        r = delegation.add_message(self.conn, did, "resume", "继续", feedback_id="fbk_r")
        self.send()
        self.assertEqual(self.kb.cards[card]["status"], "ready", "the pause was lifted")
        self.assertEqual(self.msg(r)["status"], "sent")
        self.tick()
        self.assertNotEqual(self.d(did)["status"], "paused")

    def test_resume_does_not_lift_a_question_asked_after_a_pause_that_never_blocked(self):
        did, card = self.running()
        self.kb.worker_block(card, "截止日期是周四还是周五？")
        self.tick()
        p = delegation.add_message(self.conn, did, "pause", "先停", feedback_id="fbk_p")
        self.tick()
        self.assertEqual(self.msg(p)["status"], "sent")
        r = delegation.add_message(self.conn, did, "resume", "继续", feedback_id="fbk_r")
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "blocked", "the open question still waits for an answer")
        self.assertEqual(self.msg(r)["status"], "sent")


class OperationIdentityTests(BindingBase):
    """B04: a delegation or message is identified by its feedback item AND the op within it."""

    def fb(self, fid="fbk_multi"):
        return {"id": fid, "origin": "matrix", "author_is_user": 1, "payload": {}}

    def test_two_supplements_in_one_feedback_are_both_sent(self):
        did, card = self.running()
        report = feedback_flow.apply_ops(self.conn, self.cfg, self.fb(), [
            {"op": "message", "delegation": did, "kind": "supplement", "body": "抄送 Bob"},
            {"op": "message", "delegation": did, "kind": "supplement", "body": "附上数据集链接"}])
        mids = [e["message"] for e in report["ops"]]
        self.assertEqual(len(set(mids)), 2, "a second op of the same kind is not swallowed")
        self.tick()
        bodies = [c["body"] for c in self.kb.comments if c["task_id"] == card]
        self.assertTrue(any("抄送 Bob" in b for b in bodies) and any("数据集链接" in b for b in bodies))

    def test_two_delegations_from_one_feedback_are_both_created(self):
        other = store.create_task(self.conn, "整理数据集说明", origin="intake")
        report = feedback_flow.apply_ops(self.conn, self.cfg, self.fb(), [
            {"op": "delegate", "task": self.task, "title": "起草回复", "goal": "Draft a reply"},
            {"op": "delegate", "task": other, "title": "整理说明", "goal": "Summarise the dataset card"}])
        dids = [e["delegation"] for e in report["ops"]]
        self.assertEqual(len(set(dids)), 2)
        self.assertEqual({self.d(d)["task_id"] for d in dids}, {self.task, other})

    def test_retrying_the_same_ops_creates_nothing_twice(self):
        did, _ = self.running()
        ops = [{"op": "message", "delegation": did, "kind": "supplement", "body": "抄送 Bob"},
               {"op": "delegate", "task": self.task, "title": "并行：查日程", "goal": "Check calendar", "parallel": True}]
        first = feedback_flow.apply_ops(self.conn, self.cfg, self.fb(), ops)
        second = feedback_flow.apply_ops(self.conn, self.cfg, self.fb(), ops)
        self.assertEqual(first["ops"], second["ops"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM delegation_messages WHERE kind='supplement'")
                         .fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM delegations").fetchone()[0], 2)

    def test_closing_the_task_does_not_add_a_second_cancel_to_an_explicit_one(self):
        did, card = self.running()
        task = store.get_task(self.conn, self.task)
        feedback_flow.apply_ops(self.conn, self.cfg, self.fb(), [
            {"op": "message", "delegation": did, "kind": "cancel", "body": "不用了"},
            {"op": "update_task", "task": self.task, "based_on_version": task["version"],
             "set": {"status": "cancelled"}}])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM delegation_messages WHERE kind='cancel'")
                         .fetchone()[0], 1)


class TransitionRaceTests(BindingBase):
    """Boundary 2: Hermes cannot check the anchor and run its state function in one transaction
    (``write_txn`` refuses nesting), so a dashboard action can land in between. FakeKanban's
    ``interleave`` runs it at exactly that point; the native version is in tests/integration."""

    def raced_notices(self):
        return [t for t in self.outbox_texts() if "有人在看板上改动了卡片" in t]

    def test_an_accept_that_lands_on_a_newer_version_is_reported(self):
        did, card = self.delivered()
        mid = delegation.add_message(self.conn, did, "accept", "可以", feedback_id="fbk_acc")
        self.kb.interleave["complete"] = lambda: self.dashboard_v2(card)
        self.send()
        self.assertEqual(self.kb.cards[card]["status"], "done", "Hermes completed the card it saw")
        self.assertEqual(self.msg(mid)["status"], "sent")
        self.assertIn("raced", self.msg(mid)["error"])
        self.assertEqual(len(self.raced_notices()), 1, "the user is told it acted on a newer version")

    def test_an_answer_that_lands_on_a_newer_question_is_reported(self):
        did, card = self.running()
        self.kb.worker_block(card, "截止日期是周四还是周五？")
        self.tick()
        mid = delegation.add_message(self.conn, did, "answer", "周五", feedback_id="fbk_ans")

        def dashboard_then_new_question():
            self.kb.unblock(card)
            self.kb.worker_start(card)
            self.kb.worker_block(card, "要抄送 Bob 吗？")

        self.kb.interleave["unblock"] = dashboard_then_new_question
        self.send()
        self.assertIn("raced", self.msg(mid)["error"])
        self.assertEqual(len(self.raced_notices()), 1)

    def test_no_interleaving_is_a_plain_success(self):
        did, card = self.delivered()
        mid = delegation.add_message(self.conn, did, "accept", "可以", feedback_id="fbk_acc")
        self.send()
        self.assertEqual((self.msg(mid)["status"], self.msg(mid)["error"]), ("sent", None))
        self.assertEqual(self.raced_notices(), [])
