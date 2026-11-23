"""The MCP endpoint (docs/api.md): JSON-RPC over POST /mcp with the web token, whose tools are
the API's operations."""

import json

from todo_manager import store
from tests.test_api import ApiCase


class McpTests(ApiCase):
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

    def test_a_client_starts_a_session_and_lists_the_tools(self):
        status, reply = self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                                "clientInfo": {"name": "test", "version": "0"}})
        self.assertEqual(status, 200)
        self.assertEqual(reply["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("tools", reply["result"]["capabilities"])
        self.assertIn("own edits", reply["result"]["instructions"])
        self.assertEqual(self.rpc("notifications/initialized", rid=None)[0], 202)
        tools = {t["name"]: t for t in self.rpc("tools/list")[1]["result"]["tools"]}
        self.assertIn("create_task", tools)
        self.assertNotIn("delivery_file", tools)   # a file, not something to read as JSON
        self.assertEqual(tools["show_task"]["inputSchema"]["required"], ["id"])

    def test_a_tool_call_writes_as_the_api_does(self):
        error, created = self.tool("create_task", title="Book the venue", set={"due_date": "2026-10-20"})
        self.assertFalse(error, created)
        self.assertEqual(store.get_task(self.conn, created["task"])["origin"], "api")
        error, task = self.tool("show_task", id=created["task"])
        self.assertEqual((error, task["title"], task["due"]), (False, "Book the venue", "2026-10-20"))
        error, body = self.tool("show_task", id="tsk_missing")
        self.assertEqual((error, body["error"]), (True, "NOT_FOUND"))
        error, body = self.tool("create_task")
        self.assertEqual((error, body["error"]), (True, "INVALID"))

    def test_protocol_errors_and_sign_in(self):
        self.assertEqual(self.rpc("tools/call", {"name": "drop_tables"})[1]["error"]["code"], -32602)
        self.assertEqual(self.rpc("resources/list")[1]["error"]["code"], -32601)
        self.assertEqual(self.rpc("ping", token=False)[0], 401)
        self.assertEqual(self.call("GET", "/mcp")[0], 405)
