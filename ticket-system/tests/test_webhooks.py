"""Outbound webhooks: generic (signed) and SOAR delivery against local mocks."""

import hashlib
import hmac
import json
import threading
import unittest

from helpers import Receiver, ServerTestCase

ALL_EVENTS = ["ticket.created", "ticket.updated", "ticket.state_changed", "ticket.commented"]


class GenericWebhookTest(ServerTestCase):
    SECRET = "generic-test-secret"

    def setUp(self):
        self.receiver = Receiver()
        resp = self.admin.post("/webhooks", {"name": "test", "type": "generic", "url": self.receiver.url + "/hook",
                                             "secret": self.SECRET, "events": ALL_EVENTS})
        self.assertEqual(resp.status, 201)
        self.hook_id = resp.json["id"]

    def tearDown(self):
        self.admin.delete("/webhooks/%d" % self.hook_id)
        self.receiver.close()

    def bodies(self):
        return [json.loads(r["body"]) for r in self.receiver.requests]

    def test_signed_payloads_for_all_events(self):
        t = self.create_ticket(priority=2)
        n = t["number"]
        self.admin.patch("/tickets/" + n, {"assigned_to": "analyst1"})
        self.admin.patch("/tickets/" + n, {"state": "in_progress"}, headers={"X-MyDemoTicket-Source": "splunk-es"})
        self.admin.post("/tickets/%s/comments" % n, {"body": "looking into it"})
        self.wait_webhooks()
        self.assertEqual(len(self.receiver.requests), 4)
        for req in self.receiver.requests:
            expected = "sha256=" + hmac.new(self.SECRET.encode(), req["body"], hashlib.sha256).hexdigest()
            self.assertEqual(req["headers"]["x-mydemoticket-signature"], expected)
            self.assertEqual(req["headers"]["content-type"], "application/json")
            self.assertEqual(req["path"], "/hook")
        events = [b["event"] for b in self.bodies()]
        self.assertEqual(events, ALL_EVENTS[:1] + ["ticket.updated", "ticket.state_changed", "ticket.commented"])
        self.assertEqual([r["headers"]["x-mydemoticket-event"] for r in self.receiver.requests], events)
        created, updated, state, commented = self.bodies()
        for body in self.bodies():
            for key in ("event", "delivery_id", "timestamp", "source", "actor", "ticket", "changes", "comment"):
                self.assertIn(key, body)
            self.assertEqual(body["ticket"]["number"], n)
            self.assertEqual(body["actor"], "admin")
        self.assertIsNone(created["comment"])
        self.assertEqual(created["changes"], {})
        self.assertEqual(updated["changes"], {"assigned_to": {"old": None, "new": "analyst1"}})
        self.assertEqual(state["changes"]["state"], {"old": "new", "new": "in_progress"})
        self.assertEqual(state["source"], "splunk-es")
        self.assertEqual(created["source"], "api")
        self.assertEqual(commented["comment"]["body"], "looking into it")
        self.assertEqual(commented["comment"]["type"], "comment")
        self.assertEqual(len({b["delivery_id"] for b in self.bodies()}), 4)

    def test_event_filter_and_inactive(self):
        other = Receiver()
        try:
            hook = self.admin.post("/webhooks", {"url": other.url, "events": ["ticket.commented"]}).json
            inactive = self.admin.post("/webhooks", {"url": other.url, "active": False}).json
            t = self.create_ticket()
            self.admin.post("/tickets/%s/comments" % t["number"], {"body": "x", "type": "work_note"})
            self.wait_webhooks()
            self.assertEqual([json.loads(r["body"])["event"] for r in other.requests], ["ticket.commented"])
            self.admin.delete("/webhooks/%d" % hook["id"])
            self.admin.delete("/webhooks/%d" % inactive["id"])
        finally:
            other.close()

    def test_delivery_log(self):
        self.create_ticket()
        self.wait_webhooks()
        log = self.admin.get("/webhooks/%d/deliveries" % self.hook_id).json
        self.assertEqual(log[0]["status"], "success")
        self.assertEqual(log[0]["response_code"], 200)
        self.assertEqual(log[0]["attempts"], 1)
        self.assertEqual(log[0]["event"], "ticket.created")
        self.assertEqual(self.admin.get("/webhooks/9999/deliveries").status, 404)


class RetryWebhookTest(ServerTestCase):
    def test_retry_then_success(self):
        calls = {"n": 0}
        lock = threading.Lock()

        def flaky(path, body):
            with lock:
                calls["n"] += 1
                return (500, {"error": "boom"}) if calls["n"] < 3 else (200, {"ok": True})

        receiver = Receiver(flaky)
        try:
            hook = self.admin.post("/webhooks", {"url": receiver.url, "events": ["ticket.created"]}).json
            self.create_ticket()
            self.wait_webhooks()
            log = self.admin.get("/webhooks/%d/deliveries" % hook["id"]).json
            self.assertEqual(log[0]["status"], "success")
            self.assertEqual(log[0]["attempts"], 3)
            self.admin.delete("/webhooks/%d" % hook["id"])
        finally:
            receiver.close()

    def test_gives_up_after_three_attempts(self):
        receiver = Receiver(lambda path, body: (503, {"error": "down"}))
        try:
            hook = self.admin.post("/webhooks", {"url": receiver.url, "events": ["ticket.created"]}).json
            self.create_ticket()
            self.wait_webhooks()
            log = self.admin.get("/webhooks/%d/deliveries" % hook["id"]).json
            self.assertEqual(log[0]["status"], "failed")
            self.assertEqual(log[0]["attempts"], 3)
            self.assertEqual(log[0]["response_code"], 503)
            self.assertEqual(len(receiver.requests), 3)
            self.admin.delete("/webhooks/%d" % hook["id"])
        finally:
            receiver.close()


class SoarMock:
    """Minimal fake of the SOAR REST API (/rest/container, /rest/artifact)."""

    def __init__(self):
        self.containers = {}  # source_data_identifier -> id
        self.next_id = 100
        self.lock = threading.Lock()
        self.receiver = Receiver(self.respond)
        self.url = self.receiver.url

    def respond(self, path, body):
        with self.lock:
            if path == "/rest/container":
                sdi = body.get("source_data_identifier")
                if sdi in self.containers:
                    return 400, {"failed": True, "message": "duplicate", "existing_container_id": self.containers[sdi]}
                self.next_id += 1
                self.containers[sdi] = self.next_id
                return 200, {"success": True, "id": self.next_id}
            if path == "/rest/artifact":
                return 200, {"success": True, "id": 5000}
            return 404, {"failed": True}

    def requests(self, path=None):
        return [
            {"headers": r["headers"], "body": json.loads(r["body"]), "path": r["path"]}
            for r in self.receiver.requests if path is None or r["path"] == path
        ]

    def close(self):
        self.receiver.close()


class SoarWebhookTest(ServerTestCase):
    SOAR_TOKEN = "soar-ph-auth-token-placeholder"

    def setUp(self):
        self.soar = SoarMock()
        self.generic = Receiver()
        self.hook = self.admin.post("/webhooks", {"name": "soar", "type": "soar", "url": self.soar.url + "/",
                                                  "secret": self.SOAR_TOKEN, "soar_label": "demo_tickets",
                                                  "events": ALL_EVENTS}).json
        self.generic_hook = self.admin.post("/webhooks", {"url": self.generic.url, "events": ALL_EVENTS}).json

    def tearDown(self):
        self.admin.delete("/webhooks/%d" % self.hook["id"])
        self.admin.delete("/webhooks/%d" % self.generic_hook["id"])
        self.soar.close()
        self.generic.close()

    def test_container_and_artifact(self):
        t = self.create_ticket(short_description="Phishing mail reported", description="User got a mail",
                               priority=1, assignment_group="SOC L1",
                               custom_fields={"incident_type": "phishing"},
                               external_refs={"es_finding_id": "es-finding-0001"})
        self.wait_webhooks()
        containers = self.soar.requests("/rest/container")
        artifacts = self.soar.requests("/rest/artifact")
        self.assertEqual(len(containers), 1)
        self.assertEqual(len(artifacts), 1)
        c = containers[0]
        self.assertEqual(c["headers"]["ph-auth-token"], self.SOAR_TOKEN)
        self.assertEqual(c["body"], {
            "name": "%s: Phishing mail reported" % t["number"],
            "label": "demo_tickets",
            "source_data_identifier": t["number"],
            "severity": "high",
            "description": "User got a mail",
        })
        a = artifacts[0]["body"]
        self.assertEqual(artifacts[0]["headers"]["ph-auth-token"], self.SOAR_TOKEN)
        self.assertEqual(a["container_id"], 101)
        self.assertEqual(a["name"], "ticket.created")
        self.assertEqual(a["label"], "mydemoticket_event")
        self.assertTrue(a["run_automation"])
        self.assertTrue(a["source_data_identifier"])
        cef = a["cef"]
        self.assertEqual(cef["ticketNumber"], t["number"])
        self.assertEqual(cef["ticketState"], "new")
        self.assertEqual(cef["ticketPriority"], 1)
        self.assertEqual(cef["assignmentGroup"], "SOC L1")
        self.assertEqual(cef["assignedTo"], "")
        self.assertEqual(cef["event"], "ticket.created")
        self.assertEqual(json.loads(cef["changes"]), {})
        self.assertEqual(cef["comment"], "")
        self.assertEqual(json.loads(cef["customFields"]), {"incident_type": "phishing"})
        self.assertEqual(cef["esFindingId"], "es-finding-0001")
        # container id stored on the ticket ...
        stored = self.admin.get("/tickets/" + t["number"]).json
        self.assertEqual(stored["external_refs"]["soar_container_id"], 101)
        # ... without an echo event: generic hook saw exactly one delivery
        self.assertEqual([json.loads(r["body"])["event"] for r in self.generic.requests], ["ticket.created"])
        log = self.admin.get("/webhooks/%d/deliveries" % self.hook["id"]).json
        self.assertEqual(log[0]["status"], "success")

    def test_duplicate_uses_existing_container_id(self):
        t = self.create_ticket(priority=4)
        self.wait_webhooks()
        first_id = self.admin.get("/tickets/" + t["number"]).json["external_refs"]["soar_container_id"]
        self.admin.post("/tickets/%s/comments" % t["number"], {"body": "please check"})
        self.wait_webhooks()
        containers = self.soar.requests("/rest/container")
        self.assertEqual(len(containers), 2)
        self.assertEqual(containers[0]["body"]["severity"], "low")
        artifacts = self.soar.requests("/rest/artifact")
        self.assertEqual(artifacts[1]["body"]["container_id"], first_id)
        self.assertEqual(artifacts[1]["body"]["cef"]["comment"], "please check")
        self.assertEqual(artifacts[1]["body"]["name"], "ticket.commented")

    def test_preexisting_container_in_soar(self):
        with self.soar.lock:
            number = "TKT%07d" % (int(self.app.db.get_meta("ticket_counter")) + 1)
            self.soar.containers[number] = 777
        t = self.create_ticket(priority=3)
        self.assertEqual(t["number"], number)
        self.wait_webhooks()
        self.assertEqual(self.soar.requests("/rest/container")[0]["body"]["severity"], "medium")
        self.assertEqual(self.soar.requests("/rest/artifact")[0]["body"]["container_id"], 777)
        stored = self.admin.get("/tickets/" + t["number"]).json
        self.assertEqual(stored["external_refs"]["soar_container_id"], 777)
        journal = self.admin.get("/tickets/%s/journal" % t["number"]).json
        ref_audit = [e for e in journal if e.get("field") == "external_refs.soar_container_id"]
        self.assertEqual(ref_audit[0]["new"], 777)
        self.assertEqual(ref_audit[0]["source"], "webhook")

    def test_state_change_artifact_changes(self):
        t = self.create_ticket()
        self.admin.patch("/tickets/" + t["number"], {"state": "resolved", "close_notes": "false positive"})
        self.wait_webhooks()
        art = self.soar.requests("/rest/artifact")[-1]["body"]
        self.assertEqual(art["name"], "ticket.state_changed")
        self.assertEqual(art["cef"]["ticketState"], "resolved")
        changes = json.loads(art["cef"]["changes"])
        self.assertEqual(changes["state"], {"old": "new", "new": "resolved"})

    def test_source_soar_is_not_echoed(self):
        soar_client = self.client_for("soar")
        resp = soar_client.post("/tickets", {"short_description": "Created by playbook"},
                                headers={"X-MyDemoTicket-Source": "soar"})
        self.assertEqual(resp.status, 201)
        soar_client.patch("/tickets/" + resp.json["number"], {"priority": 1},
                          headers={"X-MyDemoTicket-Source": "soar"})
        self.wait_webhooks()
        self.assertEqual(self.soar.requests(), [])
        log = self.admin.get("/webhooks/%d/deliveries" % self.hook["id"]).json
        self.assertEqual([d["status"] for d in log], ["skipped", "skipped"])
        # generic webhooks still get the events, with source for their own loop checks
        sources = [json.loads(r["body"])["source"] for r in self.generic.requests]
        self.assertEqual(sources, ["soar", "soar"])


if __name__ == "__main__":
    unittest.main()
