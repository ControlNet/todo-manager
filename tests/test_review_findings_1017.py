"""Findings of the trial observer's reviews of 2026-10-17. Synthetic workspace and Kanban
(FakeKanban) only."""

from todo_manager import i18n, ops, store
from tests.test_delegation_reliability import Base


class DecisionDelegationHandoverTests(Base):
    """A post draft: agent work asked for on a task in Decision moved it to to-do / next by
    Agent after the work's baseline version, so that move looked like a later choice of the user's
    and the delivered draft never handed the task back for review."""

    def test_delivered_work_started_from_decision_goes_back_to_the_user(self):
        store.update_task(self.conn, self.task, {"status": "decision", "next_actor": "me"},
                          actor="agent", origin="test")
        ctx = ops.context(self.conn, self.cfg, human=True, origin="cli")
        did = ops.delegate_create(self.conn, self.cfg, ctx, task_id=self.task, title="起草短文",
                                  goal="Draft only", allowed=["read", "draft"], submit=False)["delegation"]
        task = store.get_task(self.conn, self.task)
        self.assertEqual((task["status"], task["next_actor"]), ("todo", "agent"))
        _, card = self.running(did)
        self.kb.worker_complete(card, "Draft ready", {"deliverable_markdown": "Here is the draft ..."})
        self.tick()
        task = store.get_task(self.conn, self.task)
        self.assertEqual((task["status"], task["next_actor"]), ("todo", "me"))
        self.assertEqual(task["change_note"], i18n.t("dlg.to_review"))

    def test_a_next_actor_the_user_sets_later_is_still_kept(self):
        store.update_task(self.conn, self.task, {"status": "decision", "next_actor": "me"},
                          actor="agent", origin="test")
        ctx = ops.context(self.conn, self.cfg, human=True, origin="cli")
        did = ops.delegate_create(self.conn, self.cfg, ctx, task_id=self.task, title="起草短文",
                                  goal="Draft only", allowed=["read", "draft"], submit=False)["delegation"]
        _, card = self.running(did)
        store.update_task(self.conn, self.task, {"next_actor": "external"}, actor="human", origin="notion")
        self.kb.worker_complete(card, "Draft ready", {"deliverable_markdown": "Here is the draft ..."})
        self.tick()
        self.assertEqual(store.get_task(self.conn, self.task)["next_actor"], "external")
