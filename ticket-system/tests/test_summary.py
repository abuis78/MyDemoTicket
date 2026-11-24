"""Journal type ``summary``: roles, visibility, webhook payload, SOAR cef and audit."""

import json
import unittest

from helpers import Receiver, ServerTestCase

PASSWORD = "summary-password-123"


class SummaryTest(ServerTestCase):
    use_recorder = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        resp = cls.admin.post("/users", {"username": "tenant-admin1", "role": "admin", "tenant": "soc-games",
                                         "password": PASSWORD})
        assert resp.status == 201, resp

    def post(self, client, number, body):
        return client.post("/tickets/%s/comments" % number, body)

    def test_allowed_roles(self):
        t = self.create_ticket(assignment_group="SOC L1")
        for username in ("analyst1", "soar", "tenant-admin1"):
            with self.subTest(user=username):
                resp = self.post(self.client_for(username), t["number"], {"body": "Summary by " + username,
                                                                          "type": "summary"})
                self.assertEqual(resp.status, 201, resp)
                self.assertEqual((resp.json["type"], resp.json["actor"]), ("summary", username))
        resp = self.post(self.admin, t["number"], {"body": "platform summary", "type": "summary"})
        self.assertEqual((resp.status, resp.json["type"]), (201, "summary"))

    def test_requester_may_not_write_but_sees_summary(self):
        req = self.client_for("requester1")
        mine = self.create_ticket(req)
        resp = self.post(req, mine["number"], {"body": "my summary", "type": "summary"})
        self.assertEqual(resp.status, 403, resp)
        self.post(self.client_for("analyst1"), mine["number"], {"body": "internal", "type": "work_note"})
        ok = self.post(self.client_for("soar"), mine["number"],
                       {"body": "Line 1\nLine 2", "type": "summary", "author": "ai-assistant"})
        self.assertEqual(ok.status, 201, ok)
        seen = req.get("/tickets/%s/journal" % mine["number"]).json
        summaries = [e for e in seen if e["type"] == "summary"]
        self.assertEqual([(e["body"], e["author"], e["actor"]) for e in summaries],
                         [("Line 1\nLine 2", "ai-assistant", "soar")])
        self.assertNotIn("work_note", [e["type"] for e in seen])

    def test_agent_of_other_team_forbidden(self):
        other = self.create_ticket(assignment_group="SOC L2")
        resp = self.post(self.client_for("analyst1"), other["number"], {"body": "x", "type": "summary"})
        self.assertEqual(resp.status, 403, resp)

    def test_invalid_type_and_ticket_unchanged(self):
        t = self.create_ticket()
        resp = self.post(self.admin, t["number"], {"body": "x", "type": "resolution"})
        self.assertEqual(resp.status, 422, resp)
        self.assertIn("summary", resp.json["error"])
        self.post(self.admin, t["number"], {"body": "final", "type": "summary"})
        ticket = self.admin.get("/tickets/" + t["number"]).json
        self.assertEqual(set(ticket), set(t))  # no new ticket field: the UI reads summaries from the journal

    def test_audit_details_type(self):
        t = self.create_ticket()
        self.post(self.admin, t["number"], {"body": "final", "type": "summary", "author": "jdoe"})
        self.audit.wait_idle()
        events = self.audit.find("ticket.comment", object_id=t["number"])
        self.assertEqual([(e["details"]["type"], e["details"]["author"]) for e in events], [("summary", "jdoe")])

    def test_webhook_payload_and_soar_cef(self):
        generic, soar_rx = Receiver(), Receiver(lambda path, body: (200, {"id": 42}))
        try:
            self.admin.post("/webhooks", {"url": generic.url, "events": ["ticket.commented"]})
            self.admin.post("/webhooks", {"type": "soar", "url": soar_rx.url, "secret": "s",
                                          "events": ["ticket.commented"]})
            t = self.create_ticket()
            self.post(self.admin, t["number"], {"body": "AI summary", "type": "summary", "author": "jdoe"})
            self.post(self.admin, t["number"], {"body": "plain"})
            self.wait_webhooks()
            bodies = [json.loads(r["body"]) for r in generic.requests]
            self.assertEqual([(b["event"], b["comment"]["type"], b["comment"]["author"]) for b in bodies],
                             [("ticket.commented", "summary", "jdoe"), ("ticket.commented", "comment", None)])
            artifacts = [json.loads(r["body"]) for r in soar_rx.requests if r["path"].endswith("/rest/artifact")]
            self.assertEqual([(a["cef"]["comment"], a["cef"]["commentAuthor"], a["cef"]["commentType"])
                              for a in artifacts],
                             [("AI summary", "jdoe", "summary"), ("plain", "", "comment")])
            self.assertEqual({a["cef"]["actor"] for a in artifacts}, {"admin"})
        finally:
            generic.close()
            soar_rx.close()

    def test_non_comment_events_have_empty_comment_type(self):
        soar_rx = Receiver(lambda path, body: (200, {"id": 43}))
        try:
            self.admin.post("/webhooks", {"type": "soar", "url": soar_rx.url, "secret": "s",
                                          "events": ["ticket.created"]})
            self.create_ticket()
            self.wait_webhooks()
            artifacts = [json.loads(r["body"]) for r in soar_rx.requests if r["path"].endswith("/rest/artifact")]
            self.assertTrue(artifacts)
            self.assertEqual({a["cef"]["commentType"] for a in artifacts}, {""})
        finally:
            soar_rx.close()


class EsInvestigationIdCefTest(ServerTestCase):
    def test_cef_carries_es_investigation_id(self):
        soar_rx = Receiver(lambda path, body: (200, {"id": 44}))
        try:
            self.admin.post("/webhooks", {"type": "soar", "url": soar_rx.url, "secret": "s",
                                          "events": ["ticket.created", "ticket.updated"]})
            t = self.create_ticket()
            self.admin.patch("/tickets/" + t["number"], {"external_refs": {"es_investigation_id": "inv-0815"}})
            self.wait_webhooks()
            artifacts = [json.loads(r["body"]) for r in soar_rx.requests if r["path"].endswith("/rest/artifact")]
            self.assertEqual([(a["name"], a["cef"]["esInvestigationId"]) for a in artifacts],
                             [("ticket.created", ""), ("ticket.updated", "inv-0815")])
            analyst = self.client_for("analyst1")
            self.assertEqual(analyst.patch("/tickets/" + t["number"], {"priority": 2}).status, 200)
            self.wait_webhooks()
            artifacts = [json.loads(r["body"]) for r in soar_rx.requests if r["path"].endswith("/rest/artifact")]
            self.assertEqual([a["cef"]["actor"] for a in artifacts], ["admin", "admin", "analyst1"])
        finally:
            soar_rx.close()


if __name__ == "__main__":
    unittest.main()
