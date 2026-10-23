"""A request the user made against an older version (``--based-on N``, interpreted by an agent
with ``actor="human"``) never overwrites what the user changed after version N: not through the
status/next_actor normalisation of tasks, and not on projects (fields and repositories)."""

from todo_manager import store
from tests.flow import apply_direct
from tests.helpers import WorkspaceCase


class TaskStatePairTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.task = store.create_task(self.conn, "Draft the report", origin="intake", status="in_progress")

    def request(self, version, changes, source_id="req_late"):
        [result] = apply_direct(self.conn, self.cfg, text="(late request)", source_id=source_id, op_list=[
            {"op": "update_task", "task": self.task, "based_on_version": version, "set": changes}])
        return result

    def test_surviving_next_actor_does_not_bring_a_stale_status_back(self):
        version = store.get_task(self.conn, self.task)["version"]
        store.update_task(self.conn, self.task, {"status": "todo"}, actor="human", origin="notion")
        result = self.request(version, {"status": "waiting", "next_actor": "external", "due_date": "2026-10-20"})
        task = store.get_task(self.conn, self.task)
        self.assertEqual((task["status"], task["next_actor"]), ("todo", "me"), "the user's newer status stands")
        self.assertEqual(task["due_date"], "2026-10-20", "the rest of the request is applied")
        self.assertEqual(result["applied"], ["due_date"])
        self.assertIn("status", result["rejected"])
        self.assertIn("next_actor", result["rejected"])

    def test_status_does_not_overwrite_a_newer_next_actor_through_normalisation(self):
        version = store.get_task(self.conn, self.task)["version"]
        store.update_task(self.conn, self.task, {"next_actor": "agent"}, actor="human", origin="notion")
        result = self.request(version, {"status": "waiting"})
        task = store.get_task(self.conn, self.task)
        self.assertEqual((task["status"], task["next_actor"]), ("in_progress", "agent"))
        self.assertEqual(result["applied"], [])
        self.assertEqual(set(result["rejected"]), {"status", "next_actor"})

    def test_a_current_request_still_moves_the_pair(self):
        store.update_task(self.conn, self.task, {"status": "todo"}, actor="human", origin="notion")
        version = store.get_task(self.conn, self.task)["version"]
        result = self.request(version, {"status": "waiting", "next_actor": "external"})
        task = store.get_task(self.conn, self.task)
        self.assertEqual((task["status"], task["next_actor"]), ("waiting", "external"))
        self.assertEqual(result["rejected"], {})


class ProjectBasedOnTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.project = store.create_project(self.conn, "Sample project", origin="intake", goal="Ship v1",
                                            domain="Research", repos=["owner/alpha"])

    def request(self, op, source_id="req_late"):
        [result] = apply_direct(self.conn, self.cfg, text="(late request)", source_id=source_id,
                                op_list=[{"op": "update_project", "project": self.project, **op}])
        return result

    def test_late_goal_change_keeps_the_users_newer_goal_and_applies_the_rest(self):
        version = store.get_project(self.conn, self.project)["version"]
        store.update_project(self.conn, self.project, {"goal": "Ship v2"}, actor="human", origin="notion")
        result = self.request({"based_on_version": version, "set": {"goal": "Ship v1.1", "domain": "Teaching"}})
        project = store.get_project(self.conn, self.project)
        self.assertEqual(project["goal"], "Ship v2", "the user's newer goal stands")
        self.assertEqual(project["domain"], "Teaching")
        self.assertEqual(result["applied"], ["domain"])
        self.assertTrue(result["rejected"]["goal"].startswith("stale"), result["rejected"])

    def test_late_repository_change_keeps_the_users_newer_repositories(self):
        version = store.get_project(self.conn, self.project)["version"]
        store.update_project(self.conn, self.project, {}, actor="human", origin="notion", repos_add=["owner/beta"])
        result = self.request({"based_on_version": version, "repos_remove": ["owner/alpha"]})
        self.assertEqual(store.get_project(self.conn, self.project)["repos"], ["owner/alpha", "owner/beta"])
        self.assertIn("repos", result["rejected"])

    def test_without_based_on_the_request_applies_as_before(self):
        store.update_project(self.conn, self.project, {"goal": "Ship v2"}, actor="human", origin="notion")
        result = self.request({"set": {"goal": "Ship v3"}})
        self.assertEqual(store.get_project(self.conn, self.project)["goal"], "Ship v3")
        self.assertEqual(result["rejected"], {})

    def test_a_request_based_on_the_current_version_applies(self):
        store.update_project(self.conn, self.project, {"goal": "Ship v2"}, actor="human", origin="notion")
        version = store.get_project(self.conn, self.project)["version"]
        result = self.request({"based_on_version": version, "set": {"goal": "Ship v3"}})
        self.assertEqual(store.get_project(self.conn, self.project)["goal"], "Ship v3")
        self.assertEqual(result["rejected"], {})
