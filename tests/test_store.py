from todo_manager import store
from tests.helpers import WorkspaceCase


class StoreTests(WorkspaceCase):
    def test_state_normalisation(self):
        self.assertEqual(store.normalize_state("waiting", "me"), ("waiting", "external"))
        self.assertEqual(store.normalize_state("todo", "external"), ("waiting", "external"))
        self.assertEqual(store.normalize_state("review", "agent"), ("review", "me"))
        self.assertEqual(store.normalize_state("in_progress", "agent"), ("in_progress", "agent"))

    def test_agent_cannot_overwrite_newer_human_correction(self):
        tid = store.create_task(self.conn, "Reply to Alice", origin="intake", due_date="2026-10-10")
        # Agent reads v1; meanwhile the human moves the date.
        store.update_task(self.conn, tid, {"due_date": "2026-10-15"}, actor="human", origin="notion")
        result = store.update_task(self.conn, tid, {"due_date": "2026-10-09", "next_step": "draft"},
                                   actor="agent", origin="intake", based_on_version=1)
        self.assertIn("due_date", result.rejected)
        self.assertEqual(result.applied, {"next_step": "draft"})
        self.assertEqual(store.get_task(self.conn, tid)["due_date"], "2026-10-15")

    def test_seen_human_value_needs_explicit_override(self):
        tid = store.create_task(self.conn, "Review paper", origin="intake")
        v = store.update_task(self.conn, tid, {"due_date": "2026-10-20"}, actor="human", origin="notion").version
        plain = store.update_task(self.conn, tid, {"due_date": "2026-10-27"}, actor="agent", origin="intake",
                                  based_on_version=v)
        self.assertIn("due_date", plain.rejected)
        forced = store.update_task(self.conn, tid, {"due_date": "2026-10-27"}, actor="agent", origin="intake",
                                   based_on_version=v, override_human=True)
        self.assertEqual(forced.applied, {"due_date": "2026-10-27"})

    def test_human_closed_task_is_not_reopened_by_agent(self):
        tid = store.create_task(self.conn, "Old matter", origin="intake")
        store.update_task(self.conn, tid, {"status": "cancelled"}, actor="human", origin="notion")
        result = store.update_task(self.conn, tid, {"status": "todo"}, actor="agent", origin="intake",
                                   based_on_version=99, override_human=True)
        self.assertIn("status", result.rejected)
        self.assertEqual(store.get_task(self.conn, tid)["status"], "cancelled")

    def test_history_is_recorded(self):
        tid = store.create_task(self.conn, "X", origin="cli", actor="human")
        store.update_task(self.conn, tid, {"next_step": "call"}, actor="human", origin="matrix", cause_ref="fbk_1")
        hist = store.history(self.conn, "task", tid)
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[1]["changes"], {"next_step": [None, "call"]})
        self.assertEqual(hist[1]["cause_ref"], "fbk_1")


class StatePairTests(WorkspaceCase):
    """B06: status and next_actor are validated as one pair AFTER the human-field protection;
    a change that would leave an inconsistent pair is rejected as a whole."""

    def consistent(self, tid):
        t = store.get_task(self.conn, tid)
        self.assertEqual(store.normalize_state(t["status"], t["next_actor"]), (t["status"], t["next_actor"]),
                         f"{t['status']}/{t['next_actor']}")
        return t

    def human(self, tid, **changes):
        return store.update_task(self.conn, tid, changes, actor="human", origin="notion").version

    def agent(self, tid, v, **changes):
        return store.update_task(self.conn, tid, changes, actor="agent", origin="intake", based_on_version=v)

    def test_waiting_is_not_half_applied_over_a_human_next_actor(self):
        tid = store.create_task(self.conn, "Reply to Bob", origin="intake")
        v = self.human(tid, next_actor="agent")
        result = self.agent(tid, v, status="waiting", waiting_on="Bob")
        self.assertIn("status", result.rejected)
        self.assertIn("pair", result.rejected["status"])
        self.assertEqual(result.applied, {"waiting_on": "Bob"}, "unrelated fields still apply")
        self.assertEqual(self.consistent(tid)["status"], "todo")

    def test_external_next_actor_is_not_half_applied_over_a_human_status(self):
        tid = store.create_task(self.conn, "Reply to Bob", origin="intake")
        v = self.human(tid, status="in_progress")
        result = self.agent(tid, v, next_actor="external")
        self.assertEqual(set(result.rejected), {"status", "next_actor"})
        self.assertEqual(self.consistent(tid)["status"], "in_progress")

    def test_review_is_not_half_applied(self):
        tid = store.create_task(self.conn, "Draft reply", origin="intake")
        v = self.human(tid, next_actor="agent")
        result = self.agent(tid, v, status="review")
        self.assertIn("status", result.rejected)
        self.consistent(tid)

    def test_closing_is_not_half_applied(self):
        tid = store.create_task(self.conn, "Draft reply", origin="intake")
        v = self.human(tid, next_actor="agent")
        result = self.agent(tid, v, status="done")
        self.assertIn("status", result.rejected)
        t = self.consistent(tid)
        self.assertNotIn(t["status"], store.CLOSED_STATUSES)

    def test_a_consistent_partial_change_still_applies(self):
        tid = store.create_task(self.conn, "Draft reply", origin="intake")
        v = self.human(tid, next_actor="me")          # explicit, same as the default
        result = self.agent(tid, v, status="in_progress")
        self.assertEqual(result.applied.get("status"), "in_progress")
        self.consistent(tid)

    def test_delegation_write_back_keeps_the_pair(self):
        from todo_manager import delegation
        from todo_manager.kanban import FakeKanban
        kb = FakeKanban()
        tid = store.create_task(self.conn, "Draft reply", origin="intake")
        did = delegation.create(self.conn, self.cfg, task_id=tid, title="draft", goal="draft only")
        delegation.submit_pending(self.conn, self.cfg, kb)
        card = delegation.get(self.conn, did)["card_id"]
        kb.worker_start(card)
        delegation.sync_cards(self.conn, self.cfg, kb)
        self.human(tid, next_actor="agent", status="todo")   # the user's own state after delegating
        kb.worker_review(card, "v1", {"deliverable_markdown": "Dear Bob"})
        delegation.sync_cards(self.conn, self.cfg, kb)
        self.consistent(tid)
