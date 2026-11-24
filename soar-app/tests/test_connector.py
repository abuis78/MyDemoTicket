import json
import unittest

import support  # noqa: F401  (sys.path setup)
from mock_api import VALID_TOKEN, MockTicketAPI

import phantom.app as phantom
from mydemoticket_connector import MyDemoTicketConnector


class ConnectorTestBase(unittest.TestCase):
    def setUp(self):
        self.api = MockTicketAPI().start()
        self.config = {"base_url": self.api.base_url, "api_token": VALID_TOKEN,
                       "verify_server_cert": True, "source_tag": "soar", "ingest_label": "mydemoticket"}

    def tearDown(self):
        self.api.stop()

    def run_action(self, action, param=None, config=None, state=None, container_id=None, poll_now=False):
        conn = MyDemoTicketConnector()
        conn.stub_setup(config=config or self.config, action=action, state=state,
                        container_id=container_id, poll_now=poll_now)
        self.assertTrue(conn.initialize())
        ret = conn.handle_action(param or {})
        conn.finalize()
        result = conn.get_action_results()[-1]
        self.assertEqual(bool(ret), bool(result.get_status()))
        return conn, result

    def last_request(self):
        return self.api.requests[-1]


class TestConnectivity(ConnectorTestBase):
    def test_success(self):
        _, r = self.run_action("test_connectivity")
        self.assertTrue(r.get_status(), r.get_message())
        self.assertEqual(r.get_summary()["role"], "integration")

    def test_bad_token_401(self):
        cfg = dict(self.config, api_token="wrong")
        _, r = self.run_action("test_connectivity", config=cfg)
        self.assertFalse(r.get_status())
        self.assertIn("HTTP 401", r.get_message())
        self.assertIn("invalid token", r.get_message())

    def test_role_not_allowed(self):
        self.api.role = "requester"
        _, r = self.run_action("test_connectivity")
        self.assertFalse(r.get_status())
        self.assertIn("requester", r.get_message())

    def test_connection_refused(self):
        cfg = dict(self.config, base_url="http://127.0.0.1:1")
        _, r = self.run_action("test_connectivity", config=cfg)
        self.assertFalse(r.get_status())
        self.assertIn("Connection error", r.get_message())

    def test_invalid_base_url_fails_initialize(self):
        conn = MyDemoTicketConnector()
        conn.stub_setup(config=dict(self.config, base_url="ticket.example"), action="test_connectivity")
        self.assertFalse(conn.initialize())


class TestCreateTicket(ConnectorTestBase):
    def test_success_sets_refs_and_source_header(self):
        _, r = self.run_action("create_ticket", {
            "short_description": "Phishing", "priority": 2, "custom_fields": '{"mitre_tactic": "TA0001"}',
            "es_finding_id": "notable-1"}, container_id=42)
        self.assertTrue(r.get_status(), r.get_message())
        method, path, _, headers, body = self.last_request()
        self.assertEqual((method, path), ("POST", "/api/v1/tickets"))
        self.assertEqual(headers.get("x-mydemoticket-source"), "soar")
        self.assertEqual(body["external_refs"], {"soar_container_id": 42, "es_finding_id": "notable-1"})
        self.assertEqual(body["custom_fields"], {"mitre_tactic": "TA0001"})
        self.assertEqual(body["priority"], 2)
        self.assertTrue(r.get_summary()["ticket_number"].startswith("TKT"))

    def test_invalid_custom_fields_json(self):
        _, r = self.run_action("create_ticket", {"short_description": "x", "custom_fields": "{broken"})
        self.assertFalse(r.get_status())
        self.assertIn("not valid JSON", r.get_message())
        self.assertEqual(self.api.requests, [])

    def test_custom_fields_must_be_object(self):
        _, r = self.run_action("create_ticket", {"short_description": "x", "custom_fields": "[1, 2]"})
        self.assertFalse(r.get_status())
        self.assertIn("JSON object", r.get_message())

    def test_invalid_priority(self):
        _, r = self.run_action("create_ticket", {"short_description": "x", "priority": 9})
        self.assertFalse(r.get_status())

    def test_validation_422(self):
        _, r = self.run_action("create_ticket", {"short_description": ""})
        self.assertFalse(r.get_status())
        self.assertIn("HTTP 422", r.get_message())

    def test_custom_source_tag(self):
        self.run_action("create_ticket", {"short_description": "x"}, config=dict(self.config, source_tag="soar-lab"))
        self.assertEqual(self.last_request()[3].get("x-mydemoticket-source"), "soar-lab")


class TestGetTicket(ConnectorTestBase):
    def test_success(self):
        _, r = self.run_action("get_ticket", {"number": "TKT0001001"})
        self.assertTrue(r.get_status(), r.get_message())
        self.assertEqual(r.get_data()[0]["number"], "TKT0001001")
        self.assertNotIn("x-mydemoticket-source", self.last_request()[3])

    def test_not_found_404(self):
        _, r = self.run_action("get_ticket", {"number": "TKT9999999"})
        self.assertFalse(r.get_status())
        self.assertIn("HTTP 404", r.get_message())
        self.assertIn("not found", r.get_message())

    def test_invalid_json_response(self):
        self.api.broken_json_path = "/api/v1/tickets/TKT0001001"
        _, r = self.run_action("get_ticket", {"number": "TKT0001001"})
        self.assertFalse(r.get_status())
        self.assertIn("Invalid JSON", r.get_message())


class TestUpdateTicket(ConnectorTestBase):
    def test_success_merges_custom_fields(self):
        _, r = self.run_action("update_ticket", {"number": "TKT0001001", "assigned_to": "alice",
                                                 "custom_fields": '{"a": 1}', "priority": "1"})
        self.assertTrue(r.get_status(), r.get_message())
        method, _, _, headers, body = self.last_request()
        self.assertEqual(method, "PATCH")
        self.assertEqual(headers.get("x-mydemoticket-source"), "soar")
        self.assertEqual(body, {"assigned_to": "alice", "custom_fields": {"a": 1}, "priority": 1})
        self.assertEqual(r.get_data()[0]["assigned_to"], "alice")

    def test_nothing_to_update(self):
        _, r = self.run_action("update_ticket", {"number": "TKT0001001"})
        self.assertFalse(r.get_status())

    def test_invalid_custom_fields_json(self):
        _, r = self.run_action("update_ticket", {"number": "TKT0001001", "custom_fields": "nope"})
        self.assertFalse(r.get_status())
        self.assertIn("not valid JSON", r.get_message())

    def test_not_found_404(self):
        _, r = self.run_action("update_ticket", {"number": "TKT0000000", "category": "x"})
        self.assertFalse(r.get_status())
        self.assertIn("HTTP 404", r.get_message())


class TestSetState(ConnectorTestBase):
    def test_success(self):
        _, r = self.run_action("set_state", {"number": "TKT0001001", "state": "resolved", "close_notes": "done"})
        self.assertTrue(r.get_status(), r.get_message())
        self.assertEqual(self.last_request()[4], {"state": "resolved", "close_notes": "done"})
        self.assertEqual(r.get_summary()["state"], "resolved")

    def test_invalid_transition_409(self):
        self.api.tickets["TKT0001001"]["state"] = "closed"
        _, r = self.run_action("set_state", {"number": "TKT0001001", "state": "in_progress"})
        self.assertFalse(r.get_status())
        self.assertIn("HTTP 409", r.get_message())
        self.assertIn("invalid transition", r.get_message())

    def test_unknown_state_rejected_locally(self):
        _, r = self.run_action("set_state", {"number": "TKT0001001", "state": "done"})
        self.assertFalse(r.get_status())
        self.assertEqual(self.api.requests, [])


class TestAddComment(ConnectorTestBase):
    def test_success_work_note(self):
        _, r = self.run_action("add_comment", {"number": "TKT0001001", "body": "hi", "type": "work_note"})
        self.assertTrue(r.get_status(), r.get_message())
        method, path, _, headers, body = self.last_request()
        self.assertEqual((method, path), ("POST", "/api/v1/tickets/TKT0001001/comments"))
        self.assertEqual(body, {"body": "hi", "type": "work_note"})
        self.assertEqual(headers.get("x-mydemoticket-source"), "soar")

    def test_default_type_comment(self):
        self.run_action("add_comment", {"number": "TKT0001001", "body": "hi"})
        self.assertEqual(self.last_request()[4]["type"], "comment")

    def test_invalid_type(self):
        _, r = self.run_action("add_comment", {"number": "TKT0001001", "body": "hi", "type": "email"})
        self.assertFalse(r.get_status())

    def test_not_found_404(self):
        _, r = self.run_action("add_comment", {"number": "TKT0000000", "body": "hi"})
        self.assertFalse(r.get_status())
        self.assertIn("HTTP 404", r.get_message())


class TestListTickets(ConnectorTestBase):
    def test_success_with_filter(self):
        _, r = self.run_action("list_tickets", {"state": "in_progress", "limit": 10})
        self.assertTrue(r.get_status(), r.get_message())
        self.assertEqual(len(r.get_data()), 1)
        self.assertEqual(r.get_summary(), {"total": 1, "returned": 1})
        self.assertEqual(self.last_request()[2], {"state": "in_progress", "limit": "10", "offset": "0"})

    def test_bad_limit(self):
        _, r = self.run_action("list_tickets", {"limit": "abc"})
        self.assertFalse(r.get_status())

    def test_unauthorized_401(self):
        _, r = self.run_action("list_tickets", {}, config=dict(self.config, api_token="x"))
        self.assertFalse(r.get_status())
        self.assertIn("HTTP 401", r.get_message())


class TestJournalAndCustomFields(ConnectorTestBase):
    def test_get_journal(self):
        _, r = self.run_action("get_journal", {"number": "TKT0001001"})
        self.assertTrue(r.get_status(), r.get_message())
        self.assertEqual(r.get_summary()["entries"], 1)

    def test_get_journal_404(self):
        _, r = self.run_action("get_journal", {"number": "TKT0000000"})
        self.assertFalse(r.get_status())
        self.assertIn("HTTP 404", r.get_message())

    def test_list_custom_fields(self):
        _, r = self.run_action("list_custom_fields")
        self.assertTrue(r.get_status(), r.get_message())
        self.assertEqual(r.get_data()[0]["name"], "mitre_tactic")

    def test_list_custom_fields_401(self):
        _, r = self.run_action("list_custom_fields", config=dict(self.config, api_token="x"))
        self.assertFalse(r.get_status())
        self.assertIn("HTTP 401", r.get_message())


class TestOnPoll(ConnectorTestBase):
    def test_first_poll_creates_containers_and_saves_last_poll(self):
        conn, r = self.run_action("on_poll", {})
        self.assertTrue(r.get_status(), r.get_message())
        self.assertEqual(len(conn.containers), 2)
        c = conn.containers[0]
        self.assertEqual(c["source_data_identifier"], "TKT0001001")
        self.assertEqual(c["label"], "mydemoticket")
        self.assertEqual(len(conn.artifacts), 2)
        a = next(x for x in conn.artifacts if x["cef"]["ticketNumber"] == "TKT0001002")
        self.assertEqual(a["name"], "ticket snapshot")
        self.assertEqual(a["cef"]["esFindingId"], "abc-123")
        self.assertEqual(a["cef"]["ticketState"], "in_progress")
        self.assertEqual(json.loads(a["cef"]["customFields"]), {})
        self.assertRegex(conn.get_state()["last_poll"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertNotIn("updated_since", self.api.requests[0][2])

    def test_uses_last_poll_and_skips_soar_changes(self):
        self.api.journal_source = {"TKT0001002": "soar", "TKT0001001": "web"}
        conn, r = self.run_action("on_poll", {}, state={"last_poll": "2026-11-24T00:00:00Z"})
        self.assertTrue(r.get_status(), r.get_message())
        self.assertEqual(self.api.requests[0][2]["updated_since"], "2026-11-24T00:00:00Z")
        self.assertEqual([c["source_data_identifier"] for c in conn.containers], ["TKT0001001"])
        self.assertEqual(r.get_summary()["skipped"], 1)
        self.assertNotEqual(conn.get_state()["last_poll"], "2026-11-24T00:00:00Z")

    def test_repeat_poll_updates_existing_container(self):
        conn = MyDemoTicketConnector()
        conn.stub_setup(config=self.config, action="on_poll")
        conn.initialize()
        conn.handle_action({})
        self.api.tickets["TKT0001001"]["state"] = "resolved"
        self.api.tickets["TKT0001001"]["updated_at"] = "2099-01-01T00:00:00Z"
        conn.handle_action({})
        self.assertEqual(len(conn.containers), 2)
        list_calls = [q for m, p, q, _, _ in self.api.requests if (m, p) == ("GET", "/api/v1/tickets")]
        self.assertNotIn("updated_since", list_calls[0])
        self.assertIn("updated_since", list_calls[-1])
        resolved = [a for a in conn.artifacts if a["cef"]["ticketState"] == "resolved"]
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["container_id"], conn.containers[0]["id"])

    def test_poll_now_respects_container_count(self):
        for i in range(5):
            self.api.add_ticket(short_description="extra {}".format(i))
        conn, r = self.run_action("on_poll", {"container_count": 3}, poll_now=True,
                                  state={"last_poll": "2026-11-24T00:00:00Z"})
        self.assertTrue(r.get_status(), r.get_message())
        self.assertEqual(len(conn.containers), 3)
        self.assertEqual(conn.get_state()["last_poll"], "2026-11-24T00:00:00Z")

    def test_paging(self):
        for i in range(150):
            self.api.add_ticket(short_description="bulk {}".format(i))
        conn, r = self.run_action("on_poll", {})
        self.assertTrue(r.get_status(), r.get_message())
        self.assertEqual(len(conn.containers), 152)

    def test_poll_state_filter(self):
        conn, _ = self.run_action("on_poll", {}, config=dict(self.config, poll_state_filter="in_progress"))
        self.assertEqual(self.api.requests[0][2]["state"], "in_progress")
        self.assertEqual(len(conn.containers), 1)

    def test_poll_401_keeps_state(self):
        conn, r = self.run_action("on_poll", {}, config=dict(self.config, api_token="x"),
                                  state={"last_poll": "2026-11-24T00:00:00Z"})
        self.assertFalse(r.get_status())
        self.assertEqual(conn.get_state()["last_poll"], "2026-11-24T00:00:00Z")


class TestDispatch(ConnectorTestBase):
    def test_unknown_action(self):
        _, r = self.run_action("does_not_exist")
        self.assertFalse(r.get_status())

    def test_stub_run_loop(self):
        conn = MyDemoTicketConnector()
        ret = conn._handle_action(json.dumps({"identifier": "get_ticket", "config": self.config,
                                              "parameters": [{"number": "TKT0001001"}]}), None)
        self.assertEqual(ret, phantom.APP_SUCCESS)


if __name__ == "__main__":
    unittest.main()
