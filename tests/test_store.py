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
