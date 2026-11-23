"""The MCP endpoint (docs/api.md): JSON-RPC over POST /mcp with the web token, whose tools are
the API's operations."""

import json
import urllib.request

from todo_manager import delegation, store
from tests.helpers import mail
from tests.test_api import ApiCase


class McpCase(ApiCase):
    def rpc(self, method, params=None, *, token=True, rid=1):
        message = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
        if rid is not None:
            message["id"] = rid
        return self.call("POST", "/mcp", message, token=token)

    def tool(self, name, **arguments):
        status, reply = self.rpc("tools/call", {"name": name, "arguments": arguments})
        self.assertEqual(status, 200, reply)
        result = reply["result"]
        return result["isError"], json.loads(result["content"][0]["text"])


class McpTests(McpCase):
    def test_a_client_starts_a_session_and_lists_the_tools(self):
        status, reply = self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                                "clientInfo": {"name": "test", "version": "0"}})
        self.assertEqual(status, 200)
        self.assertEqual(reply["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("tools", reply["result"]["capabilities"])
        self.assertIn("own edits", reply["result"]["instructions"])
        icons = reply["result"]["serverInfo"]["icons"]
        base = f"http://127.0.0.1:{self.site.port}"
        self.assertIn({"src": base + "/icon.svg", "mimeType": "image/svg+xml", "sizes": ["any"]}, icons)
        for icon in icons:
            with urllib.request.urlopen(icon["src"], timeout=10) as response:
                self.assertEqual(response.headers["Content-Type"].split(";")[0], icon["mimeType"])
        self.assertEqual(self.rpc("notifications/initialized", rid=None)[0], 202)
        tools = {t["name"]: t for t in self.rpc("tools/list")[1]["result"]["tools"]}
        self.assertIn("create_task", tools)
        self.assertNotIn("delivery_file", tools)   # a file, not something to read as JSON
        self.assertEqual(tools["show"]["inputSchema"]["required"], ["id"])

    def test_a_tool_call_writes_as_the_api_does(self):
        error, created = self.tool("create_task", title="Book the venue", set={"due_date": "2026-10-20"})
        self.assertFalse(error, created)
        self.assertEqual(store.get_task(self.conn, created["task"])["origin"], "api")
        error, task = self.tool("show", id=created["task"])
        self.assertEqual((error, task["title"], task["due"]), (False, "Book the venue", "2026-10-20"))
        error, body = self.tool("show", id="tsk_missing")
        self.assertEqual((error, body["error"]), (True, "NOT_FOUND"))
        error, body = self.tool("create_task")
        self.assertEqual((error, body["error"]), (True, "INVALID"))

    def test_protocol_errors_and_sign_in(self):
        self.assertEqual(self.rpc("tools/call", {"name": "drop_tables"})[1]["error"]["code"], -32602)
        self.assertEqual(self.rpc("resources/list")[1]["error"]["code"], -32601)
        self.assertEqual(self.rpc("ping", token=False)[0], 401)
        self.assertEqual(self.call("GET", "/mcp")[0], 405)


class FewerToolsTests(McpCase):
    """Several operations of the HTTP API are one tool, so a client has fewer to choose from."""

    def test_the_tools(self):
        self.assertEqual({t["name"] for t in self.rpc("tools/list")[1]["result"]["tools"]}, {
            "overview", "list_tasks", "show", "search", "list_delegations", "status",
            "act_on_task", "comment_on_task", "create_task", "update_task", "create_project", "update_project",
            "record_feedback", "create_delegation", "message_delegation"})

    def test_show_reads_whatever_the_id_names(self):
        self.add_items(mail("m1", "t1", "Bob: can you send the contract by Friday?"))
        item = self.item_id("m1")
        pid = self.tool("create_project", title="Venue")[1]["project"]
        did = delegation.create(self.conn, self.cfg, task_id=self.tid, title="Draft", goal="Draft only")
        self.assertEqual(self.tool("show", id=self.tid)[1]["title"], "Reply to Bob")
        self.assertEqual(self.tool("show", id=pid)[1]["title"], "Venue")
        self.assertEqual(self.tool("show", id=did), (False, self.cli("delegate", "show", did)))
        self.assertEqual(self.tool("show", id=item), (False, self.cli("source", "show", item)))
        for missing in ("dlv_missing", "nothing"):
            self.assertEqual(self.tool("show", id=missing)[1]["error"], "NOT_FOUND")

    def test_search_finds_tasks_and_filters_the_material(self):
        self.add_items(mail("m1", "t1", "Bob: can you send the contract by Friday?"))
        found = self.tool("search", q="Bob")[1]
        self.assertEqual([t["title"] for t in found["tasks"]], ["Reply to Bob"])
        self.assertEqual(found["sources"], self.cli("source", "search", "Bob")["items"])
        self.assertEqual(self.tool("search", q="contract", mine=True)[1]["sources"], [])

    def test_answers_and_approvals_are_messages_of_their_kind(self):
        did = delegation.create(self.conn, self.cfg, task_id=self.tid, title="Draft", goal="Draft only")
        error, body = self.tool("message_delegation", id=did, kind="answer", body="Yes")
        self.assertEqual((error, body["details"]), (True, ["no matching question in this delegation"]))
        error, body = self.tool("message_delegation", id=did, kind="approve", delivery="dlv_x", action="launch")
        self.assertEqual((error, body["details"]), (True, ["unknown action 'launch'"]))
        error, body = self.tool("message_delegation", id=did, kind="supplement", body="Keep it short")
        self.assertFalse(error, body)
