"""The HTTP API for agents (docs/api.md): the Web page's /api/* with the web token as a Bearer
token, and the writes and reads the page lacks, answered as the command line answers them."""

import contextlib
import http.client
import io
import json

from todo_manager import cli, delegation, store
from todo_manager.web import server
from tests.helpers import WorkspaceCase, mail


class ApiCase(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.site = server.WebServer(self.cfg, host="127.0.0.1", port=0)
        self.site.start()
        self.token = server.new_token(self.cfg.home)
        self.tid = store.create_task(self.conn, "Reply to Bob", origin="intake", status="decision")

    def tearDown(self):
        self.site.stop()
        super().tearDown()

    def call(self, method, path, body=None, *, token=True, headers=None):
        http_conn = http.client.HTTPConnection("127.0.0.1", self.site.port, timeout=10)
        sent = dict(headers or {})
        if token:
            sent["Authorization"] = f"Bearer {self.token if token is True else token}"
        http_conn.request(method, path, body=None if body is None else json.dumps(body), headers=sent)
        response = http_conn.getresponse()
        data = response.read()
        http_conn.close()
        return response.status, json.loads(data) if data else None

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--home", str(self.cfg.home), *args])
        return json.loads(out.getvalue())


class AuthTests(ApiCase):
    def test_the_web_token_signs_an_agent_in(self):
        self.assertEqual(self.call("GET", f"/api/tasks/{self.tid}")[0], 200)
        self.assertEqual(self.call("GET", f"/api/tasks/{self.tid}", token=False)[0], 401)
        self.assertEqual(self.call("GET", f"/api/tasks/{self.tid}", token="not-the-token")[0], 401)
        server.new_token(self.cfg.home)   # a new token signs agents out as it signs browsers out
        self.assertEqual(self.call("GET", f"/api/tasks/{self.tid}")[0], 401)

    def test_a_bearer_write_needs_no_page_header_but_a_cookie_write_still_does(self):
        status, _ = self.call("POST", f"/api/tasks/{self.tid}/act", {"action": "todo"})
        self.assertEqual(status, 200)
        self.assertEqual(store.get_task(self.conn, self.tid)["status"], "todo")
        cookie = {"Cookie": f"{server.COOKIE}={server.session_value(self.token)}"}
        status, _ = self.call("POST", f"/api/tasks/{self.tid}/act", {"action": "done"}, token=False, headers=cookie)
        self.assertEqual(status, 403)


class WriteTests(ApiCase):
    def test_an_agent_creates_and_changes_a_task_as_the_user(self):
        status, created = self.call("POST", "/api/tasks", {"title": "Book the venue", "set": {"due_date": "2026-10-20"}})
        self.assertEqual(status, 200, created)
        task = store.get_task(self.conn, created["task"])
        self.assertEqual((task["title"], task["due_date"][:10], task["origin"]), ("Book the venue", "2026-10-20", "api"))
        status, changed = self.call("PATCH", f"/api/tasks/{task['id']}",
                                    {"set": {"next_step": "Ask Alice for dates"}})
        self.assertEqual(status, 200, changed)
        self.assertEqual(store.get_task(self.conn, task["id"])["next_step"], "Ask Alice for dates")
        last = store.history(self.conn, "task", task["id"])[-1]
        self.assertEqual((last["actor"], last["origin"]), ("human", "api"))

    def test_a_retry_with_the_same_key_applies_once(self):
        body = {"title": "Send the slides", "key": "agent-slides-0001"}
        first = self.call("POST", "/api/tasks", body)[1]
        second = self.call("POST", "/api/tasks", body)[1]
        self.assertEqual(first["task"], second["task"])
        self.assertEqual(len(store.list_tasks(self.conn, store.TASK_STATUSES)), 2)

    def test_errors_read_as_the_command_line_reports_them(self):
        status, body = self.call("PATCH", "/api/tasks/tsk_missing", {"set": {"next_step": "x"}})
        self.assertEqual((status, body["error"]), (404, "NOT_FOUND"))
        self.assertIsInstance(body["details"], list)
        status, body = self.call("POST", "/api/tasks", {"title": " "})
        self.assertEqual((status, body["error"]), (400, "INVALID"))
        status, body = self.call("POST", "/api/projects/prj_x/nothing", {})
        self.assertEqual((status, body["error"]), (404, "NOT_FOUND"))

    def test_a_project_is_created_and_renamed(self):
        status, created = self.call("POST", "/api/projects", {"title": "Thesis"})
        self.assertEqual(status, 200, created)
        status, changed = self.call("PATCH", f"/api/projects/{created['project']}", {"set": {"title": "PhD thesis"}})
        self.assertEqual(status, 200, changed)
        self.assertEqual(store.get_project(self.conn, created["project"])["title"], "PhD thesis")

    def test_what_the_user_said_goes_to_the_next_feedback_run(self):
        status, body = self.call("POST", "/api/feedback", {"said": "Move the venue task to next week",
                                                           "scope": f"task:{self.tid}"})
        self.assertEqual(status, 200, body)
        row = self.conn.execute("SELECT origin, status, text FROM feedback WHERE id=?", (body["feedback"],)).fetchone()
        self.assertEqual(tuple(row), ("web", "pending", "Move the venue task to next week"))

    def test_an_agent_hands_a_task_to_agent_work(self):
        status, body = self.call("POST", "/api/delegations", {"task": self.tid, "brief": "Draft the reply to Bob",
                                                              "allow": ["read", "draft"]})
        self.assertEqual(status, 200, body)
        did = body["delegation"]
        self.assertEqual(delegation.get(self.conn, did)["task_id"], self.tid)
        status, body = self.call("POST", f"/api/delegations/{did}/messages", {"kind": "supplement", "body": "Keep it short"})
        self.assertEqual(status, 200, body)
        status, body = self.call("POST", "/api/delegations", {"task": self.tid, "brief": "x"})
        self.assertEqual((status, body["error"]), (400, "INVALID"))


class ReadTests(ApiCase):
    def test_reads_the_page_lacks_answer_as_the_command_line(self):
        did = delegation.create(self.conn, self.cfg, task_id=self.tid, title="Draft", goal="Draft only")
        self.assertEqual(self.call("GET", f"/api/delegations/{did}")[1], self.cli("delegate", "show", did))
        self.assertEqual(self.call("GET", "/api/delegations")[1], self.cli("delegate", "list"))
        self.add_items(mail("m1", "t1", "Bob: can you send the contract by Friday?"))
        item = self.item_id("m1")
        self.assertEqual(self.call("GET", f"/api/sources/{item}")[1], self.cli("source", "show", item))
        self.assertEqual(self.call("GET", "/api/sources?q=contract")[1], self.cli("source", "search", "contract"))
        self.assertEqual(self.call("GET", "/api/sources/src_missing")[0], 404)

    def test_status_is_the_command_line_status(self):
        status, body = self.call("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["tasks"], {"decision": 1})
        self.assertIn("problems", body)
