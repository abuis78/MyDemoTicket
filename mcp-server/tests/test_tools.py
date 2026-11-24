"""Every tool against the REAL MyDemoTicket server (python3 -m mydemoticket on 127.0.0.1).

Credentials are read at runtime from <data-dir>/initial-credentials.txt and never printed.
"""

import json
import unittest
from datetime import datetime, timedelta, timezone

from helpers import McpProcess, TicketServer, clean_env


class ToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.started_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cls.server = TicketServer().start()
        try:
            token = cls.server.credentials()["soar API token"]  # role integration
            cls.mcp = McpProcess(clean_env(MYDEMOTICKET_URL=cls.server.url, MYDEMOTICKET_TOKEN=token)).start()
            cls.mcp.initialize()
        except Exception:
            cls.server.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.mcp.stop()
        cls.server.stop()

    def ok(self, name, arguments=None):
        result = self.mcp.call(name, arguments)
        self.assertIs(result["isError"], False, result["content"][0]["text"])
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        return result["structuredContent"]

    def error(self, name, arguments=None):
        result = self.mcp.call(name, arguments)
        self.assertIs(result["isError"], True, result)
        self.assertNotIn("structuredContent", result)
        return result["content"][0]["text"]

    def create(self, **fields):
        fields.setdefault("short_description", "MCP test ticket")
        return self.ok("create_ticket", fields)

    def test_full_ticket_lifecycle(self):
        ticket = self.create(short_description="Suspicious login via MCP", description="created by unittest",
                             priority=2, assignment_group="SOC L1", assigned_to="analyst1", category="security",
                             custom_fields={"affected_host": "demo-host-01", "incident_type": "phishing"})
        number = ticket["number"]
        self.assertRegex(number, r"^TKT\d{7}$")
        self.assertEqual((ticket["state"], ticket["priority"], ticket["assignment_group"]), ("new", 2, "SOC L1"))
        self.assertEqual(ticket["custom_fields"]["incident_type"], "phishing")

        updated = self.ok("update_ticket", {"number": number, "priority": 1, "custom_fields": {"risk_score": 80},
                                            "external_refs": {"es_finding_id": "finding-placeholder"}})
        self.assertEqual(updated["priority"], 1)
        self.assertEqual(updated["custom_fields"]["risk_score"], 80)
        self.assertEqual(updated["custom_fields"]["affected_host"], "demo-host-01")  # merged, not replaced
        self.assertEqual(updated["description"], "created by unittest")  # untouched field
        self.assertEqual(updated["external_refs"], {"es_finding_id": "finding-placeholder"})

        self.assertEqual(self.ok("set_state", {"number": number, "state": "in_progress"})["state"], "in_progress")
        comment = self.ok("add_comment", {"number": number, "body": "We are looking into it"})
        self.assertEqual(comment["type"], "comment")
        note = self.ok("add_comment", {"number": number, "body": "IOC enrichment done", "type": "work_note"})
        self.assertEqual(note["type"], "work_note")

        journal = self.ok("get_journal", {"number": number})
        self.assertEqual(journal["total"], len(journal["items"]))
        bodies = [(e["type"], e.get("body")) for e in journal["items"] if e["type"] != "audit"]
        self.assertEqual(bodies, [("comment", "We are looking into it"), ("work_note", "IOC enrichment done")])
        self.assertTrue(journal["items"])
        self.assertEqual(set(e["source"] for e in journal["items"]), {"mcp"})
        self.assertTrue(any(e["type"] == "audit" and e.get("field") == "state" and e.get("new") == "in_progress"
                            for e in journal["items"]))

        resolved = self.ok("set_state", {"number": number, "state": "resolved",
                                         "close_notes": "False positive, user confirmed login"})
        self.assertEqual(resolved["state"], "resolved")
        self.assertEqual(resolved["close_notes"], "False positive, user confirmed login")
        self.assertTrue(resolved["resolved_at"])

        got = self.ok("get_ticket", {"number": number})
        self.assertEqual((got["number"], got["state"]), (number, "resolved"))

        listed = self.ok("list_tickets", {"state": "resolved", "assignment_group": "SOC L1",
                                          "assigned_to": "analyst1", "q": "Suspicious login via MCP",
                                          "updated_since": self.started_at})
        self.assertEqual([t["number"] for t in listed["items"]], [number])
        self.assertEqual(listed["total"], 1)
        self.assertEqual(self.ok("list_tickets", {"state": "new,on_hold", "q": "Suspicious login via MCP"})["total"], 0)

    def test_list_paging(self):
        for i in range(3):
            self.create(short_description="paging-probe %d" % i)
        page = self.ok("list_tickets", {"q": "paging-probe", "limit": 2})
        self.assertEqual((len(page["items"]), page["total"]), (2, 3))
        rest = self.ok("list_tickets", {"q": "paging-probe", "limit": 2, "offset": 2})
        self.assertEqual(len(rest["items"]), 1)
        default = self.ok("list_tickets", {})
        self.assertLessEqual(len(default["items"]), 20)

    def test_find_by_external_ref_then_comment_with_author(self):
        number = self.create(short_description="extref-probe")["number"]
        self.ok("update_ticket", {"number": number, "external_refs": {"es_finding_id": 4711, "mcp_probe": "a"}})
        other = self.create(short_description="extref-probe other")["number"]
        self.ok("update_ticket", {"number": other, "external_refs": {"es_finding_id": "4712", "mcp_probe": "a"}})
        found = self.ok("list_tickets", {"external_ref": {"es_finding_id": "4711"}})
        self.assertEqual(([t["number"] for t in found["items"]], found["total"]), ([number], 1))
        self.assertEqual(self.ok("list_tickets", {"external_ref": {"es_finding_id": 4711}})["total"], 1)
        both = self.ok("list_tickets", {"external_ref": {"mcp_probe": "a"}, "q": "extref-probe"})
        self.assertEqual(both["total"], 2)
        anded = self.ok("list_tickets", {"external_ref": {"mcp_probe": "a", "es_finding_id": "4712"}})
        self.assertEqual([t["number"] for t in anded["items"]], [other])
        self.assertEqual(self.ok("list_tickets", {"external_ref": {"es_finding_id": "nope"}})["total"], 0)
        self.assertIn("external_ref key", self.error("list_tickets", {"external_ref": {"Bad-Key": "x"}}))
        self.assertIn("must be strings, numbers or booleans",
                      self.error("list_tickets", {"external_ref": {"es_finding_id": ["x"]}}))

        entry = self.ok("add_comment", {"number": number, "body": "line 1\nline 2", "author": "jdoe"})
        self.assertEqual((entry["author"], entry["actor"]), ("jdoe", "soar"))
        plain = self.ok("add_comment", {"number": number, "body": "no author"})
        self.assertIsNone(plain["author"])
        self.assertIn("longer than 64", self.error("add_comment", {"number": number, "body": "x", "author": "y" * 65}))
        self.assertIn("HTTP 422", self.error("add_comment", {"number": number, "body": "x", "author": "a\tb"}))
        journal = self.ok("get_journal", {"number": number})["items"]
        self.assertEqual([e["author"] for e in journal if e["type"] == "comment"], ["jdoe", None])

    def test_list_custom_fields(self):
        fields = self.ok("list_custom_fields")
        names = set(f["name"] for f in fields["items"])
        self.assertTrue({"mitre_technique", "affected_host", "risk_score", "incident_type"} <= names)
        self.assertEqual(fields["total"], len(fields["items"]))

    def test_whoami(self):
        me = self.ok("whoami", {})
        self.assertEqual((me["username"], me["role"]), ("soar", "integration"))

    def test_reports_summary_data_or_clear_message(self):
        result = self.mcp.call("reports_summary", {})
        text = result["content"][0]["text"]
        if result["isError"]:
            self.assertIn("not available on this server version", text)
        else:
            self.assertIsInstance(result["structuredContent"], dict)

    def test_unknown_ticket(self):
        for name in ("get_ticket", "get_journal"):
            text = self.error(name, {"number": "TKT9999999"})
            self.assertIn("HTTP 404", text)

    def test_invalid_transition(self):
        number = self.create()["number"]
        text = self.error("set_state", {"number": number, "state": "closed"})
        self.assertIn("HTTP 409", text)
        self.assertEqual(self.ok("get_ticket", {"number": number})["state"], "new")

    def test_resolved_without_close_notes_is_rejected_before_the_api(self):
        number = self.create()["number"]
        before = self.ok("get_journal", {"number": number})["total"]
        for args in ({"number": number, "state": "resolved"},
                     {"number": number, "state": "resolved", "close_notes": "   "}):
            self.assertIn("close_notes is required", self.error("set_state", args))
        self.assertEqual(self.ok("get_journal", {"number": number})["total"], before)
        self.assertEqual(self.ok("get_ticket", {"number": number})["state"], "new")

    def test_api_validation_error(self):
        text = self.error("create_ticket", {"short_description": "x", "assignment_group": "No Such Team"})
        self.assertIn("HTTP 422", text)

    def test_unassign_with_null(self):
        number = self.create(assignment_group="SOC L1", assigned_to="analyst1")["number"]
        ticket = self.ok("update_ticket", {"number": number, "assigned_to": None})
        self.assertIsNone(ticket["assigned_to"])
        self.assertEqual(ticket["assignment_group"], "SOC L1")


class WrongTokenTests(unittest.TestCase):
    def test_wrong_and_missing_token(self):
        server = TicketServer().start()
        try:
            for env, expected in ((clean_env(MYDEMOTICKET_URL=server.url, MYDEMOTICKET_TOKEN="wrong-token"),
                                   "HTTP 401"),
                                  (clean_env(MYDEMOTICKET_URL=server.url), "MYDEMOTICKET_TOKEN is not set")):
                mcp = McpProcess(env).start()
                try:
                    mcp.initialize()
                    result = mcp.call("list_tickets", {})
                    self.assertIs(result["isError"], True)
                    self.assertIn(expected, result["content"][0]["text"])
                finally:
                    mcp.stop()
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
