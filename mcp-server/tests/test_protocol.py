"""MCP / JSON-RPC protocol tests: the server runs as a subprocess and talks over stdio."""

import unittest

from helpers import CaptureServer, McpProcess, clean_env, free_port

EXPECTED_TOOLS = {"whoami", "list_tickets", "get_ticket", "create_ticket", "update_ticket", "set_state",
                  "add_comment", "get_journal", "list_custom_fields", "reports_summary"}
READ_TOOLS = {"whoami", "list_tickets", "get_ticket", "get_journal", "list_custom_fields", "reports_summary"}
JSON_TYPES = {"string", "integer", "number", "boolean", "object", "array", "null"}


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        # Unreachable URL: protocol tests must never need the API.
        env = clean_env(MYDEMOTICKET_URL="http://127.0.0.1:%d" % free_port(), MYDEMOTICKET_TOKEN="unused-test")
        self.mcp = McpProcess(env).start()

    def tearDown(self):
        self.mcp.stop()

    def test_initialize_negotiates_supported_version(self):
        for version in ("2025-06-18", "2025-03-26", "2024-11-05"):
            response = self.mcp.request("initialize", {"protocolVersion": version, "capabilities": {},
                                                       "clientInfo": {"name": "t", "version": "1"}})
            result = response["result"]
            self.assertEqual(result["protocolVersion"], version)
            self.assertEqual(result["capabilities"], {"tools": {"listChanged": False}})
            self.assertEqual(result["serverInfo"]["name"], "mydemoticket")
            self.assertTrue(result["serverInfo"]["version"])

    def test_initialize_unknown_version_gets_latest(self):
        response = self.mcp.request("initialize", {"protocolVersion": "1999-01-01", "capabilities": {}})
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")
        response = self.mcp.request("initialize", {})
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")

    def test_notifications_are_not_answered(self):
        self.mcp.initialize()  # sends notifications/initialized
        self.mcp.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}})
        self.mcp.send({"jsonrpc": "2.0", "method": "no/such/notification"})
        self.assertTrue(self.mcp.no_message(), "a notification was answered")
        self.assertEqual(self.mcp.request("ping")["result"], {})

    def test_ping(self):
        response = self.mcp.request("ping")
        self.assertEqual(response, {"jsonrpc": "2.0", "id": response["id"], "result": {}})

    def test_tools_list(self):
        self.mcp.initialize()
        tools = self.mcp.request("tools/list")["result"]["tools"]
        self.assertEqual(set(t["name"] for t in tools), EXPECTED_TOOLS)
        self.assertEqual(len(tools), 10)
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                self.assertTrue(tool["description"].strip())
                schema = tool["inputSchema"]
                self.assertEqual(schema["type"], "object")
                self.assertIsInstance(schema["properties"], dict)
                self.assertTrue(set(schema.get("required", [])) <= set(schema["properties"]))
                for name, prop in schema["properties"].items():
                    types = prop["type"] if isinstance(prop["type"], list) else [prop["type"]]
                    self.assertTrue(set(types) <= JSON_TYPES, (name, types))
                    self.assertTrue(prop.get("description"), name)
                    if "enum" in prop:
                        self.assertTrue(prop["enum"])
                annotations = tool["annotations"]
                if tool["name"] in READ_TOOLS:
                    self.assertIs(annotations["readOnlyHint"], True)
                else:
                    self.assertIs(annotations["readOnlyHint"], False)
                    self.assertIs(annotations["destructiveHint"], False)
                    self.assertIn("idempotentHint", annotations)
        by_name = dict((t["name"], t) for t in tools)
        self.assertEqual(by_name["set_state"]["inputSchema"]["properties"]["state"]["enum"],
                         ["new", "in_progress", "on_hold", "resolved", "closed", "cancelled"])
        self.assertEqual(by_name["list_tickets"]["inputSchema"]["properties"]["limit"]["maximum"], 100)
        self.assertEqual(by_name["create_ticket"]["inputSchema"]["required"], ["short_description"])

    def test_unknown_method(self):
        response = self.mcp.request("resources/list")
        self.assertEqual(response["error"]["code"], -32601)

    def test_parse_error(self):
        self.mcp.send_raw(b"{not json\n")
        response = self.mcp.recv()
        self.assertEqual(response["error"]["code"], -32700)
        self.assertIsNone(response["id"])
        self.assertEqual(self.mcp.request("ping")["result"], {})  # still alive

    def test_invalid_request(self):
        self.mcp.send({"jsonrpc": "2.0", "id": 99})
        self.assertEqual(self.mcp.recv()["error"]["code"], -32600)

    def test_unknown_tool(self):
        response = self.mcp.request("tools/call", {"name": "drop_database", "arguments": {}})
        self.assertEqual(response["error"]["code"], -32602)

    def test_argument_validation_is_tool_error(self):
        cases = [("list_tickets", {"limit": 101}), ("list_tickets", {"state": "open"}),
                 ("get_ticket", {}), ("get_ticket", {"number": "TKT1", "bogus": 1}),
                 ("create_ticket", {"short_description": "x", "priority": 5}),
                 ("set_state", {"number": "TKT1", "state": "done"}),
                 ("add_comment", {"number": "TKT1", "body": "x", "type": "email"}),
                 ("add_comment", {"number": "TKT1", "body": "x", "author": "   "}),
                 ("add_comment", {"number": "TKT1", "body": "x", "author": "y" * 65}),
                 ("list_tickets", {"external_ref": "es_finding_id=1"}),
                 ("list_tickets", {"external_ref": {"Bad": "1"}}),
                 ("update_ticket", {"number": "TKT1"})]
        for name, args in cases:
            with self.subTest(tool=name, args=args):
                result = self.mcp.call(name, args)
                self.assertIs(result["isError"], True)
                self.assertTrue(result["content"][0]["text"].startswith("Error: "))
                self.assertNotIn("cannot reach", result["content"][0]["text"])  # rejected before any request

    def test_resolved_without_close_notes_makes_no_api_call(self):
        result = self.mcp.call("set_state", {"number": "TKT0001001", "state": "resolved"})
        self.assertIs(result["isError"], True)
        self.assertIn("close_notes is required", result["content"][0]["text"])
        self.assertNotIn("cannot reach", result["content"][0]["text"])

    def test_unreachable_api_is_tool_error(self):
        result = self.mcp.call("whoami", {})
        self.assertIs(result["isError"], True)
        self.assertIn("cannot reach", result["content"][0]["text"])

    def test_batch(self):
        self.mcp.send([{"jsonrpc": "2.0", "id": "a", "method": "ping"},
                       {"jsonrpc": "2.0", "method": "notifications/initialized"}])
        self.assertEqual(self.mcp.recv(), [{"jsonrpc": "2.0", "id": "a", "result": {}}])


class MissingTokenTests(unittest.TestCase):
    def test_server_starts_and_tools_report_missing_token(self):
        mcp = McpProcess(clean_env(MYDEMOTICKET_URL="http://127.0.0.1:%d" % free_port())).start()
        try:
            self.assertEqual(mcp.initialize()["result"]["serverInfo"]["name"], "mydemoticket")
            result = mcp.call("whoami", {})
            self.assertIs(result["isError"], True)
            self.assertIn("MYDEMOTICKET_TOKEN is not set", result["content"][0]["text"])
        finally:
            mcp.stop()


class HeaderTests(unittest.TestCase):
    """Request headers against a capturing stub: auth, tenant, source, and no token in logs."""

    TOKEN = "header-test-token-0123456789"

    def setUp(self):
        self.stub = CaptureServer(body={"number": "TKT0000001"})
        env = clean_env(MYDEMOTICKET_URL=self.stub.url + "/api/v1/", MYDEMOTICKET_TOKEN=self.TOKEN,
                        MYDEMOTICKET_TENANT="acme", MYDEMOTICKET_LOG_LEVEL="DEBUG")
        self.mcp = McpProcess(env).start()

    def tearDown(self):
        self.mcp.stop()
        self.stub.stop()

    def test_headers(self):
        self.mcp.initialize()
        self.assertIs(self.mcp.call("get_ticket", {"number": "TKT0000001"})["isError"], False)
        self.assertIs(self.mcp.call("update_ticket", {"number": "TKT/../x", "assigned_to": None})["isError"], False)
        get, patch = self.stub.requests
        self.assertEqual((get["method"], get["path"]), ("GET", "/api/v1/tickets/TKT0000001"))
        self.assertEqual(get["headers"]["authorization"], "Bearer " + self.TOKEN)
        self.assertEqual(get["headers"]["x-mydemoticket-tenant"], "acme")
        self.assertNotIn("x-mydemoticket-source", get["headers"])
        self.assertEqual((patch["method"], patch["path"]), ("PATCH", "/api/v1/tickets/TKT%2F..%2Fx"))
        self.assertEqual(patch["headers"]["x-mydemoticket-source"], "mcp")
        self.assertEqual(patch["body"], b'{"assigned_to": null}')

    def test_token_not_in_logs_or_errors(self):
        self.mcp.initialize()
        self.mcp.call("whoami", {})
        stub_error = CaptureServer(status=401, body={"error": "invalid token"})
        try:
            mcp = McpProcess(clean_env(MYDEMOTICKET_URL=stub_error.url, MYDEMOTICKET_TOKEN=self.TOKEN,
                                       MYDEMOTICKET_LOG_LEVEL="DEBUG")).start()
            try:
                result = mcp.call("whoami", {})
                self.assertIs(result["isError"], True)
                self.assertIn("HTTP 401", result["content"][0]["text"])
                self.assertNotIn(self.TOKEN, result["content"][0]["text"])
                self.assertNotIn(self.TOKEN, mcp.stderr_text())
            finally:
                mcp.stop()
        finally:
            stub_error.stop()
        log = self.mcp.stderr_text()
        self.assertIn("GET /me", log)  # debug logging is really on
        self.assertNotIn(self.TOKEN, log)


if __name__ == "__main__":
    unittest.main()
