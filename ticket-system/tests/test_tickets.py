"""Ticket CRUD, filters, state machine, custom fields and source header."""

import time
import unittest

from helpers import ServerTestCase


class TicketCrudTest(ServerTestCase):
    def test_health_without_auth(self):
        from helpers import Client

        resp = Client(self.port).get("/health")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json["status"], "ok")
        self.assertIn("version", resp.json)

    def test_create_and_get(self):
        ticket = self.create_ticket(
            description="Multiple failed logins", priority=2, assignment_group="SOC L1",
            category="security", custom_fields={"affected_host": "demo-host-01", "risk_score": 80},
            external_refs={"es_finding_id": "finding-123"},
        )
        self.assertRegex(ticket["number"], r"^TKT\d{7}$")
        self.assertEqual(ticket["state"], "new")
        self.assertEqual(ticket["priority"], 2)
        self.assertEqual(ticket["caller"], "admin")
        self.assertEqual(ticket["custom_fields"], {"affected_host": "demo-host-01", "risk_score": 80})
        self.assertEqual(ticket["external_refs"], {"es_finding_id": "finding-123"})
        self.assertTrue(ticket["created_at"].endswith("Z"))
        self.assertIsNone(ticket["resolved_at"])
        got = self.admin.get("/tickets/" + ticket["number"])
        self.assertEqual(got.status, 200)
        self.assertEqual(got.json, ticket)

    def test_numbers_are_sequential(self):
        a = self.create_ticket()
        b = self.create_ticket()
        self.assertEqual(int(b["number"][3:]), int(a["number"][3:]) + 1)

    def test_create_validation(self):
        self.assertEqual(self.admin.post("/tickets", {}).status, 422)
        self.assertEqual(self.admin.post("/tickets", {"short_description": "  "}).status, 422)
        self.assertEqual(self.admin.post("/tickets", {"short_description": "x", "priority": 7}).status, 422)
        self.assertEqual(self.admin.post("/tickets", {"short_description": "x", "bogus": 1}).status, 422)
        self.assertEqual(
            self.admin.post("/tickets", {"short_description": "x", "assignment_group": "Nope"}).status, 422
        )
        self.assertEqual(
            self.admin.post("/tickets", {"short_description": "x", "assigned_to": "ghost"}).status, 422
        )
        self.assertEqual(self.admin.post("/tickets", {"short_description": "x", "state": "closed"}).status, 422)
        resp = self.admin.request("POST", "/tickets", raw=b"{not json", headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status, 400)

    def test_patch_merges_custom_fields_and_refs(self):
        t = self.create_ticket(custom_fields={"affected_host": "h1"}, external_refs={"a": "1"})
        resp = self.admin.patch(
            "/tickets/" + t["number"],
            {"custom_fields": {"risk_score": 12.5}, "external_refs": {"soar_container_id": 4, "a": None},
             "priority": 1, "assigned_to": "analyst1"},
        )
        self.assertEqual(resp.status, 200, resp)
        self.assertEqual(resp.json["custom_fields"], {"affected_host": "h1", "risk_score": 12.5})
        self.assertEqual(resp.json["external_refs"], {"soar_container_id": 4})
        self.assertEqual(resp.json["assigned_to"], "analyst1")
        self.assertGreaterEqual(resp.json["updated_at"], t["updated_at"])

    def test_patch_readonly_and_unknown(self):
        t = self.create_ticket()
        self.assertEqual(self.admin.patch("/tickets/" + t["number"], {"number": "X"}).status, 422)
        self.assertEqual(self.admin.patch("/tickets/" + t["number"], {"foo": 1}).status, 422)
        self.assertEqual(self.admin.patch("/tickets/TKT9999999", {"priority": 1}).status, 404)

    def test_list_filters_and_paging(self):
        marker = "filtertest-%d" % time.time_ns()
        a = self.create_ticket(short_description=marker + " a", assignment_group="SOC L2")
        self.create_ticket(short_description=marker + " b", assigned_to="analyst1")
        resp = self.admin.get("/tickets?q=" + marker)
        self.assertEqual(resp.json["total"], 2)
        resp = self.admin.get("/tickets?q=%s&assignment_group=SOC%%20L2" % marker)
        self.assertEqual([i["number"] for i in resp.json["items"]], [a["number"]])
        resp = self.admin.get("/tickets?q=%s&assigned_to=analyst1" % marker)
        self.assertEqual(resp.json["total"], 1)
        resp = self.admin.get("/tickets?q=%s&limit=1&offset=1" % marker)
        self.assertEqual(resp.json["total"], 2)
        self.assertEqual(len(resp.json["items"]), 1)
        resp = self.admin.get("/tickets?q=%s&state=new,in_progress" % marker)
        self.assertEqual(resp.json["total"], 2)
        resp = self.admin.get("/tickets?q=%s&state=closed" % marker)
        self.assertEqual(resp.json["total"], 0)
        self.assertEqual(self.admin.get("/tickets?state=bogus").status, 422)
        self.assertEqual(self.admin.get("/tickets?limit=0").status, 422)

    def test_updated_since(self):
        t = self.create_ticket()
        resp = self.admin.get("/tickets?updated_since=" + t["updated_at"])
        self.assertIn(t["number"], [i["number"] for i in resp.json["items"]])
        resp = self.admin.get("/tickets?updated_since=2999-01-01T00:00:00Z")
        self.assertEqual(resp.json["total"], 0)
        self.assertEqual(self.admin.get("/tickets?updated_since=yesterday").status, 422)

    def test_journal_records_audit(self):
        t = self.create_ticket()
        self.admin.patch("/tickets/" + t["number"], {"priority": 1})
        journal = self.admin.get("/tickets/%s/journal" % t["number"]).json
        audit = [e for e in journal if e["type"] == "audit" and e["field"] == "priority"]
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["old"], 3)
        self.assertEqual(audit[0]["new"], 1)
        self.assertEqual(audit[0]["actor"], "admin")
        self.assertTrue(audit[0]["at"].endswith("Z"))

    def test_source_header_is_stored(self):
        t = self.create_ticket()
        self.admin.patch("/tickets/" + t["number"], {"priority": 2}, headers={"X-MyDemoTicket-Source": "soar"})
        self.admin.post("/tickets/%s/comments" % t["number"], {"body": "hi"},
                        headers={"X-MyDemoTicket-Source": "splunk-es"})
        journal = self.admin.get("/tickets/%s/journal" % t["number"]).json
        prio = [e for e in journal if e.get("field") == "priority"][0]
        self.assertEqual(prio["source"], "soar")
        comment = [e for e in journal if e["type"] == "comment"][0]
        self.assertEqual(comment["source"], "splunk-es")
        created = [e for e in journal if e.get("field") == "created"][0]
        self.assertEqual(created["source"], "api")

    def test_noop_patch_creates_no_audit(self):
        t = self.create_ticket()
        before = len(self.admin.get("/tickets/%s/journal" % t["number"]).json)
        resp = self.admin.patch("/tickets/" + t["number"], {"priority": t["priority"]})
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.admin.get("/tickets/%s/journal" % t["number"]).json), before)

    def test_comment_validation(self):
        t = self.create_ticket()
        self.assertEqual(self.admin.post("/tickets/%s/comments" % t["number"], {"body": ""}).status, 422)
        self.assertEqual(
            self.admin.post("/tickets/%s/comments" % t["number"], {"body": "x", "type": "chat"}).status, 422
        )
        resp = self.admin.post("/tickets/%s/comments" % t["number"], {"body": "note", "type": "work_note"})
        self.assertEqual(resp.status, 201)
        self.assertEqual(resp.json["type"], "work_note")
        self.assertEqual(resp.json["body"], "note")


class StateMachineTest(ServerTestCase):
    ALLOWED = {
        "new": ["in_progress", "on_hold", "resolved", "cancelled"],
        "in_progress": ["on_hold", "resolved", "cancelled"],
        "on_hold": ["in_progress", "resolved", "cancelled"],
        "resolved": ["in_progress", "closed"],
        "closed": [],
        "cancelled": [],
    }
    STATES = ["new", "in_progress", "on_hold", "resolved", "closed", "cancelled"]
    # Paths (from 'new') to reach each state.
    PATHS = {
        "new": [],
        "in_progress": ["in_progress"],
        "on_hold": ["on_hold"],
        "resolved": ["resolved"],
        "closed": ["resolved", "closed"],
        "cancelled": ["cancelled"],
    }

    def _ticket_in(self, state):
        t = self.create_ticket()
        for step in self.PATHS[state]:
            body = {"state": step}
            if step == "resolved":
                body["close_notes"] = "fixed"
            resp = self.admin.patch("/tickets/" + t["number"], body)
            self.assertEqual(resp.status, 200, resp)
        return t["number"]

    def test_full_transition_matrix(self):
        for source in self.STATES:
            for target in self.STATES:
                if source == target:
                    continue
                with self.subTest(source=source, target=target):
                    number = self._ticket_in(source)
                    body = {"state": target}
                    if target == "resolved":
                        body["close_notes"] = "done"
                    resp = self.admin.patch("/tickets/" + number, body)
                    if target in self.ALLOWED[source]:
                        self.assertEqual(resp.status, 200, resp)
                        self.assertEqual(resp.json["state"], target)
                    else:
                        self.assertEqual(resp.status, 409, resp)
                        self.assertIn("error", resp.json)

    def test_resolve_requires_close_notes(self):
        number = self._ticket_in("in_progress")
        resp = self.admin.patch("/tickets/" + number, {"state": "resolved"})
        self.assertEqual(resp.status, 422)
        self.assertIn("error", resp.json)
        resp = self.admin.patch("/tickets/" + number, {"state": "resolved", "close_notes": "   "})
        self.assertEqual(resp.status, 422)

    def test_resolved_at_set_and_cleared(self):
        number = self._ticket_in("resolved")
        t = self.admin.get("/tickets/" + number).json
        self.assertIsNotNone(t["resolved_at"])
        self.assertEqual(t["close_notes"], "fixed")
        t = self.admin.patch("/tickets/" + number, {"state": "in_progress"}).json
        self.assertIsNone(t["resolved_at"])

    def test_close_notes_preset_allows_resolve(self):
        number = self._ticket_in("in_progress")
        self.admin.patch("/tickets/" + number, {"close_notes": "prepared"})
        self.assertEqual(self.admin.patch("/tickets/" + number, {"state": "resolved"}).status, 200)

    def test_unknown_state(self):
        number = self._ticket_in("new")
        self.assertEqual(self.admin.patch("/tickets/" + number, {"state": "done"}).status, 422)


class CustomFieldTest(ServerTestCase):
    def test_seeded_fields(self):
        fields = {f["name"]: f for f in self.admin.get("/custom-fields").json}
        self.assertEqual(fields["mitre_technique"]["type"], "string")
        self.assertEqual(fields["affected_host"]["type"], "string")
        self.assertEqual(fields["risk_score"]["type"], "number")
        self.assertEqual(fields["incident_type"]["type"], "choice")
        self.assertEqual(fields["incident_type"]["choices"],
                         ["malware", "phishing", "data_exfiltration", "other"])

    def test_value_validation(self):
        bad = [
            {"risk_score": "high"},
            {"risk_score": True},
            {"incident_type": "ransomware"},
            {"mitre_technique": 1059},
            {"not_defined": "x"},
        ]
        for values in bad:
            with self.subTest(values=values):
                resp = self.admin.post("/tickets", {"short_description": "x", "custom_fields": values})
                self.assertEqual(resp.status, 422, resp)
        t = self.create_ticket(custom_fields={"incident_type": "phishing", "mitre_technique": "T1566"})
        self.assertEqual(self.admin.patch("/tickets/" + t["number"],
                                          {"custom_fields": {"risk_score": "x"}}).status, 422)
        self.assertEqual(self.admin.patch("/tickets/" + t["number"],
                                          {"custom_fields": {"unknown_one": 1}}).status, 422)

    def test_define_boolean_date_required(self):
        resp = self.admin.post("/custom-fields", {"name": "contained", "label": "Contained", "type": "boolean"})
        self.assertEqual(resp.status, 201, resp)
        resp = self.admin.post("/custom-fields", {"name": "first_seen", "label": "First seen", "type": "date"})
        self.assertEqual(resp.status, 201)
        t = self.create_ticket(custom_fields={"contained": False, "first_seen": "2026-11-01"})
        self.assertEqual(t["custom_fields"]["contained"], False)
        self.assertEqual(self.admin.patch("/tickets/" + t["number"],
                                          {"custom_fields": {"contained": "no"}}).status, 422)
        self.assertEqual(self.admin.patch("/tickets/" + t["number"],
                                          {"custom_fields": {"first_seen": "01.11.2026"}}).status, 422)
        # required field
        resp = self.admin.post("/custom-fields", {"name": "owner_ack", "label": "Ack", "type": "string",
                                                  "required": True})
        self.assertEqual(resp.status, 201)
        self.assertEqual(self.admin.post("/tickets", {"short_description": "x"}).status, 422)
        t = self.create_ticket(custom_fields={"owner_ack": "yes"})
        self.assertEqual(self.admin.patch("/tickets/" + t["number"],
                                          {"custom_fields": {"owner_ack": None}}).status, 422)
        self.assertEqual(self.admin.delete("/custom-fields/owner_ack").status, 204)
        self.assertEqual(self.admin.delete("/custom-fields/owner_ack").status, 404)

    def test_definition_validation(self):
        self.assertEqual(self.admin.post("/custom-fields", {"name": "Bad-Name", "type": "string"}).status, 422)
        self.assertEqual(self.admin.post("/custom-fields", {"name": "x1", "type": "blob"}).status, 422)
        self.assertEqual(self.admin.post("/custom-fields", {"name": "x2", "type": "choice"}).status, 422)
        self.assertEqual(self.admin.post("/custom-fields", {"name": "risk_score", "type": "number"}).status, 409)

    def test_patch_definition(self):
        self.admin.post("/custom-fields", {"name": "sev", "type": "choice", "choices": ["a", "b"]})
        resp = self.admin.patch("/custom-fields/sev", {"choices": ["a", "b", "c"], "label": "Severity"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json["choices"], ["a", "b", "c"])
        self.assertEqual(resp.json["label"], "Severity")
        self.assertEqual(self.admin.patch("/custom-fields/sev", {"type": "string"}).status, 422)
        self.assertEqual(self.admin.patch("/custom-fields/missing", {"label": "x"}).status, 404)


if __name__ == "__main__":
    unittest.main()
