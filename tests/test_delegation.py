import threading

from todo_manager import db, delegation, store
from todo_manager.kanban import FakeKanban, KanbanError
from tests.helpers import WorkspaceCase


class DelegationTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.kb = FakeKanban()
        self.task = store.create_task(self.conn, "回复 Alice 关于数据集的邮件", origin="intake",
                                      responsibility="Alice asked me", summary="Alice wants the split files")

    def delegate(self, **kw):
        args = {"task_id": self.task, "title": "起草回复", "goal": "Draft a reply to Alice; do not send",
                "deliverable": "email draft", "feedback_id": kw.pop("feedback_id", None)}
        args.update(kw)
        return delegation.create(self.conn, self.cfg, **args)

    def tick(self):
        """One B-cycle of deterministic connector work."""
        delegation.send_messages(self.conn, self.cfg, self.kb)
        delegation.submit_pending(self.conn, self.cfg, self.kb)
        delegation.sync_cards(self.conn, self.cfg, self.kb)

    def d(self, did):
        return delegation.get(self.conn, did)

    def test_submit_creates_one_card_and_updates_task(self):
        did = self.delegate(feedback_id="fbk_1")
        self.assertEqual(self.delegate(feedback_id="fbk_1"), did, "same feedback must not create a second delegation")
        self.tick()
        self.tick()
        self.assertEqual(len(self.kb.cards), 1)
        card = next(iter(self.kb.cards.values()))
        self.assertIn(f"todo-manager-delegation: {did}", card["body"])
        self.assertEqual(card["idem_key"], f"todo-manager:{did}")
        self.assertEqual(self.d(did)["status"], "queued")
        task = store.get_task(self.conn, self.task)
        self.assertEqual((task["status"], task["next_actor"]), ("in_progress", "agent"))

    def test_lost_create_response_is_reconciled_not_duplicated(self):
        did = self.delegate()
        self.kb.lose_response.add("create")
        self.tick()
        self.assertEqual(self.d(did)["status"], "reconcile")
        self.assertIsNone(self.d(did)["card_id"])
        self.tick()
        self.assertEqual(len(self.kb.cards), 1)
        self.assertEqual(self.d(did)["card_id"], next(iter(self.kb.cards)))

    def test_failed_lookup_never_creates(self):
        did = self.delegate()
        self.kb.fail_next["find"] = KanbanError("db locked")
        self.tick()
        self.assertEqual(len(self.kb.cards), 0)
        self.assertIn("lookup failed", self.d(did)["last_error"])

    def test_archived_card_is_found_and_not_recreated(self):
        did = self.delegate()
        self.tick()
        card_id = self.d(did)["card_id"]
        self.kb.archive(card_id)
        # Simulate local state lost after the remote create (e.g. restored older backup).
        self.conn.execute("UPDATE delegations SET card_id=NULL, status='reconcile' WHERE id=?", (did,))
        self.tick()
        self.assertEqual(len(self.kb.cards), 1)
        self.assertEqual(self.d(did)["status"], "cancelled")

    def test_multiple_matches_are_quarantined(self):
        did = self.delegate()
        marker = f"todo-manager-delegation: {did}"
        self.kb.cards["x1"] = {"id": "x1", "title": "a", "body": marker, "idem_key": "other", "status": "ready",
                               "block_kind": None, "block_recurrences": 0}
        self.kb.cards["x2"] = {"id": "x2", "title": "b", "body": marker, "idem_key": "other2", "status": "ready",
                               "block_kind": None, "block_recurrences": 0}
        self.tick()
        self.assertEqual(self.d(did)["status"], "quarantined")
        self.assertEqual(len(self.kb.cards), 2)

    def test_concurrent_submitters_create_one_card(self):
        did = self.delegate()
        errors = []

        def worker():
            conn = db.connect(self.cfg.db_path)
            try:
                delegation.submit_pending(conn, self.cfg, self.kb)
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors)
        self.assertEqual(len(self.kb.cards), 1)
        self.conn.execute("SELECT 1")  # refresh
        self.assertIsNotNone(self.d(did)["card_id"])

    def test_capacity_one_queues_second_delegation_locally(self):
        first = self.delegate()
        other = store.create_task(self.conn, "Another", origin="cli")
        second = delegation.create(self.conn, self.cfg, task_id=other, title="t2", goal="g2")
        self.tick()
        self.assertEqual(len(self.kb.cards), 1)
        self.assertEqual(self.d(second)["status"], "pending_submit")
        card = self.d(first)["card_id"]
        self.kb.worker_start(card)
        self.kb.worker_review(card, "草稿完成", {"deliverable_markdown": "Dear Alice ..."})
        self.tick()
        self.tick()
        self.assertEqual(len(self.kb.cards), 2, "second delegation submitted once the first stopped executing")

    def test_block_answer_resume_and_stale_answer(self):
        did = self.delegate()
        self.tick()
        card = self.d(did)["card_id"]
        self.kb.worker_start(card)
        self.kb.worker_block(card, "截止日期是周四还是周五？")
        self.tick()
        self.assertEqual(self.d(did)["status"], "blocked")
        self.assertEqual(store.get_task(self.conn, self.task)["status"], "review")
        comments = self.conn.execute("SELECT payload FROM outbox WHERE op='comment'").fetchall()
        self.assertTrue(any("截止日期" in c["payload"] for c in comments))
        # Many dispatcher ticks / a day later: still blocked, nothing dispatches it.
        for _ in range(5):
            self.tick()
            self.assertNotIn(card, self.kb.dispatch_tick())
        old_answer = delegation.add_message(self.conn, did, "answer", "周四", feedback_id="fbk_a1")
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "ready")
        self.assertIn(f"[tm:{old_answer}]", self.kb.comments[-1]["body"])
        # Re-delivering the same feedback is a no-op.
        self.assertEqual(delegation.add_message(self.conn, did, "answer", "周四", feedback_id="fbk_a1"), old_answer)
        self.tick()
        self.assertEqual(sum(1 for c in self.kb.comments if f"[tm:{old_answer}]" in c["body"]), 1)
        # Worker asks a second question: the loop breaker parks it in triage; still waits.
        self.kb.worker_start(card)
        self.kb.worker_block(card, "收件人要抄送谁？")
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "triage")
        self.assertEqual(self.d(did)["status"], "blocked")
        # A late duplicate of the OLD answer cannot unblock the NEW question. It is bound to the
        # first block event, which was already resolved by this very message (its marker is on
        # the card), so the replay is settled as sent without touching the card.
        stale = self.conn.execute("SELECT * FROM delegation_messages WHERE id=?", (old_answer,)).fetchone()
        self.assertEqual(stale["status"], "sent")
        self.conn.execute("UPDATE delegation_messages SET status='pending' WHERE id=?", (old_answer,))
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "triage")
        self.assertEqual(self.conn.execute("SELECT status FROM delegation_messages WHERE id=?",
                                           (old_answer,)).fetchone()[0], "sent")
        self.assertEqual(sum(1 for c in self.kb.comments if f"[tm:{old_answer}]" in c["body"]), 1)
        delegation.add_message(self.conn, did, "answer", "抄送 Bob", feedback_id="fbk_a2")
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "ready")

    def test_review_revision_accept_cycle(self):
        did = self.delegate()
        self.tick()
        card = self.d(did)["card_id"]
        self.kb.worker_start(card)
        self.kb.worker_review(card, "草稿 v1", {"deliverable_markdown": "Dear Alice v1"})
        self.tick()
        self.assertEqual(self.d(did)["status"], "review")
        self.assertNotIn(card, self.kb.dispatch_tick(), "review waits for the human")
        delegation.add_message(self.conn, did, "revision", "第二段改委婉一点", feedback_id="fbk_r1")
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "ready")
        self.kb.worker_start(card)
        self.kb.worker_review(card, "草稿 v2", {"deliverable_markdown": "Dear Alice v2"})
        self.tick()
        dl = delegation.deliveries(self.conn, did)
        self.assertEqual([x["seq"] for x in dl], [1, 2])
        self.assertEqual(dl[-1]["body"], "Dear Alice v2")
        delegation.add_message(self.conn, did, "accept", "可以了", feedback_id="fbk_ok")
        self.tick()
        self.tick()
        self.assertEqual(self.d(did)["status"], "ended")
        self.assertEqual(self.kb.cards[card]["status"], "done")
        self.assertEqual(len(delegation.deliveries(self.conn, did)), 2, "acceptance is not a new delivery")
        task = store.get_task(self.conn, self.task)
        self.assertNotIn(task["status"], ("done", "cancelled"), "card done does not complete the Task")

    def test_approval_binds_to_latest_delivery(self):
        did = self.delegate()
        self.tick()
        card = self.d(did)["card_id"]
        self.kb.worker_start(card)
        self.kb.worker_review(card, "v1", {"deliverable_markdown": "v1"})
        self.tick()
        v1 = delegation.deliveries(self.conn, did)[0]["id"]
        delegation.add_message(self.conn, did, "revision", "改短", feedback_id="fbk_r")
        self.tick()
        self.kb.worker_start(card)
        self.kb.worker_review(card, "v2", {"deliverable_markdown": "v2"})
        self.tick()
        with self.assertRaises(delegation.DelegationError):
            delegation.record_approval(self.conn, did, v1, "send_email", "", "fbk_send")
        v2 = delegation.deliveries(self.conn, did)[-1]["id"]
        delegation.record_approval(self.conn, did, v2, "send_email", "send to Alice", "fbk_send2")
        self.tick()
        self.assertIn(f"APPROVED send_email for delivery {v2}", self.kb.comments[-1]["body"])

    def test_pause_running_and_resume(self):
        did = self.delegate()
        self.tick()
        card = self.d(did)["card_id"]
        self.kb.worker_start(card)
        delegation.add_message(self.conn, did, "pause", "先别做", feedback_id="fbk_p")
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "blocked")
        self.assertEqual(self.d(did)["status"], "paused")
        self.assertTrue(any("PAUSE" in c["body"] for c in self.kb.comments))
        for _ in range(3):
            self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "blocked")
        delegation.add_message(self.conn, did, "resume", "继续", feedback_id="fbk_go")
        self.tick()
        self.assertEqual(self.kb.cards[card]["status"], "ready")

    def test_human_status_change_after_delegation_is_respected(self):
        did = self.delegate()
        self.tick()
        store.update_task(self.conn, self.task, {"status": "waiting", "waiting_on": "Alice"}, actor="human",
                          origin="notion")
        card = self.d(did)["card_id"]
        self.kb.worker_start(card)
        self.kb.worker_review(card, "v1", {"deliverable_markdown": "v1"})
        self.tick()
        self.assertEqual(store.get_task(self.conn, self.task)["status"], "waiting")
