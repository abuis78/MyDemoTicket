"""Audit calls of the ticket system (AuditRecorder injected; no HEC)."""

import json
import os
import shutil
import tempfile
import unittest

from helpers import ADMIN_PASSWORD, ADMIN_TOKEN, Client, Receiver, ServerTestCase

from mydemoticket.audit import AuditLogger
from mydemoticket.server import App

PASSWORD = "audit-user-password-1"
HOOK_SECRET = "audit-hook-secret-value"
SOAR_SECRET = "audit-soar-ph-auth-token"


class AuditWiringTest(ServerTestCase):
    use_recorder = True

    def setUp(self):
        self.audit.clear()

    def one(self, action, **match):
        events = self.audit.find(action, **match)
        self.assertEqual(len(events), 1, (action, match, self.audit.find(action)))
        return events[0]

    def test_request_id_header_matches_event(self):
        resp = self.admin.post("/tickets", {"short_description": "rid"})
        rid = resp.headers["x-request-id"]
        self.assertRegex(rid, r"^[0-9a-f-]{36}$")
        event = self.one("ticket.create", object_id=resp.json["number"])
        self.assertEqual(event["request_id"], rid)
        self.assertEqual((event["tenant"], event["actor"], event["role"], event["source"]),
                         ("soc-games", "admin", "platform_admin", "api"))
        self.assertEqual(event["ip"], "127.0.0.1")
        self.assertNotEqual(self.admin.get("/health").headers["x-request-id"], rid)
        self.assertIn("x-request-id", Client(self.port).get("/").headers)

    def test_auth_events(self):
        client = Client(self.port)
        login = client.post("/auth/login", {"username": "admin", "password": ADMIN_PASSWORD})
        client.csrf = login.json["csrf_token"]
        ok = self.one("auth.login", outcome="success")
        self.assertEqual((ok["actor"], ok["tenant"], ok["details"]["method"]), ("admin", "soc-games", "password"))
        Client(self.port).post("/auth/login", {"username": "analyst1", "password": "wrong-one"})
        failed = self.one("auth.login", outcome="failure", actor="analyst1")
        self.assertEqual(failed["tenant"], "soc-games")
        Client(self.port).post("/auth/login", {"username": "ghost", "password": "wrong-one"})
        self.assertEqual(self.one("auth.login", outcome="failure", actor="ghost")["tenant"], "unknown")
        self.admin.post("/tenants", {"slug": "aud-b", "name": "B"})
        client.post("/auth/tenant", {"tenant": "aud-b"})
        switch = self.one("auth.tenant_switch")
        self.assertEqual(switch["details"], {"from": "soc-games", "to": "aud-b"})
        self.assertEqual(switch["tenant"], "aud-b")
        client.post("/auth/logout")
        self.assertEqual(self.one("auth.logout")["actor"], "admin")

    def test_access_denied_for_401_and_403(self):
        Client(self.port).get("/tickets")
        e401 = self.one("access.denied", actor=None)
        self.assertEqual((e401["outcome"], e401["tenant"], e401["details"]["path"], e401["details"]["status"]),
                         ("denied", "unknown", "/api/v1/tickets", 401))
        self.client_for("analyst1").get("/webhooks")
        e403 = self.one("access.denied", actor="analyst1")
        self.assertEqual((e403["tenant"], e403["role"], e403["details"]["path"], e403["details"]["method"]),
                         ("soc-games", "agent", "/api/v1/webhooks", "GET"))
        self.client_for("analyst1").get("/tickets", headers={"X-MyDemoTicket-Tenant": "other"})
        self.assertEqual(self.audit.find("access.denied", actor="analyst1")[-1]["details"]["reason"],
                         "tenant mismatch")
        self.client_for("analyst1").get("/tickets")
        self.assertEqual(len(self.audit.find("access.denied", actor="analyst1")), 2)

    def test_ticket_events_carry_changes(self):
        t = self.admin.post("/tickets", {"short_description": "audit me", "priority": 2}).json
        n = t["number"]
        create = self.one("ticket.create", object_id=n)
        self.assertEqual(create["object_type"], "ticket")
        self.assertEqual(create["details"]["changes"]["priority"], {"old": None, "new": 2})
        self.admin.patch("/tickets/" + n, {"priority": 1, "assigned_to": "analyst1"})
        upd = self.one("ticket.update", object_id=n)
        self.assertEqual(upd["details"]["changes"]["priority"], {"old": 2, "new": 1})
        self.assertEqual(upd["details"]["changed"], ["assigned_to", "priority"])
        self.admin.patch("/tickets/" + n, {"state": "resolved", "close_notes": "done"},
                         headers={"X-MyDemoTicket-Source": "soar"})
        sc = self.one("ticket.state_change", object_id=n)
        self.assertEqual((sc["details"]["from"], sc["details"]["to"], sc["source"]), ("new", "resolved", "soar"))
        self.assertEqual(sc["details"]["changes"]["close_notes"], {"old": "", "new": "done"})
        self.assertEqual(self.audit.find("ticket.update", object_id=n).__len__(), 1)
        self.admin.patch("/tickets/" + n, {"priority": 1})  # no-op: nothing logged
        self.assertEqual(len(self.audit.find("ticket.update", object_id=n)), 1)
        self.admin.post("/tickets/%s/comments" % n, {"body": "internal", "type": "work_note"})
        com = self.one("ticket.comment", object_id=n)
        self.assertEqual((com["details"]["type"], com["details"]["kind"]), ("work_note", "work_note"))

    def test_admin_object_events(self):
        a = self.admin
        a.post("/users", {"username": "aud-user", "role": "agent", "tenant": "soc-games", "password": PASSWORD})
        a.patch("/users/aud-user", {"display_name": "Aud", "password": PASSWORD + "x"})
        tok = a.post("/users/aud-user/tokens", {"name": "ci"}).json
        a.delete("/users/aud-user/tokens/%d" % tok["id"])
        a.post("/teams", {"name": "AudTeam"})
        a.post("/teams/AudTeam/members", {"username": "aud-user"})
        a.delete("/teams/AudTeam/members/aud-user")
        a.post("/custom-fields", {"name": "aud_f", "type": "string"})
        a.patch("/custom-fields/aud_f", {"label": "Aud F"})
        a.delete("/custom-fields/aud_f")
        hook = a.post("/webhooks", {"url": "http://127.0.0.1:9/x", "secret": HOOK_SECRET, "active": False}).json
        a.delete("/webhooks/%d" % hook["id"])
        a.post("/tenants", {"slug": "aud-t", "name": "T"})
        a.patch("/tenants/aud-t", {"splunk_index": "mydemoticket"})
        expected = {
            "user.create": "aud-user", "user.update": "aud-user", "token.create": str(tok["id"]),
            "token.revoke": str(tok["id"]), "team.create": "AudTeam", "team.member_add": "AudTeam",
            "team.member_remove": "AudTeam", "custom_field.create": "aud_f", "custom_field.update": "aud_f",
            "custom_field.delete": "aud_f", "webhook.create": str(hook["id"]), "webhook.delete": str(hook["id"]),
            "tenant.create": "aud-t", "tenant.update": "aud-t",
        }
        for action, object_id in expected.items():
            with self.subTest(action=action):
                event = self.one(action, object_id=object_id)
                self.assertEqual(event["outcome"], "success")
        self.assertEqual(self.one("user.update")["details"]["password_changed"], True)
        self.assertEqual(self.one("webhook.create")["details"]["url_host"], "127.0.0.1")
        self.assertEqual(self.one("tenant.update")["tenant"], "aud-t")
        self.assertEqual(self.one("tenant.update")["details"]["changes"]["splunk_index"],
                         {"old": None, "new": "mydemoticket"})
        self.assertEqual(self.one("token.create")["details"]["username"], "aud-user")
        self.assert_no_secrets([PASSWORD, PASSWORD + "x", tok["token"], HOOK_SECRET, ADMIN_TOKEN])

    def test_webhook_delivery_outcomes(self):
        ok_rx = Receiver()
        bad_rx = Receiver(lambda path, body: (500, {"error": "boom"}))
        soar_rx = Receiver(lambda path, body: (200, {"id": 9}))
        try:
            ok = self.admin.post("/webhooks", {"url": ok_rx.url, "events": ["ticket.created"],
                                               "secret": HOOK_SECRET}).json
            bad = self.admin.post("/webhooks", {"url": bad_rx.url, "events": ["ticket.created"]}).json
            soar = self.admin.post("/webhooks", {"type": "soar", "url": soar_rx.url, "secret": SOAR_SECRET,
                                                 "events": ["ticket.created"]}).json
            t = self.client_for("soar").post("/tickets", {"short_description": "from soar"},
                                             headers={"X-MyDemoTicket-Source": "soar"}).json
            self.wait_webhooks()
            events = {e["details"]["webhook_id"]: e for e in self.audit.find("webhook.delivery")}
            self.assertEqual(events[ok["id"]]["outcome"], "success")
            self.assertEqual(events[ok["id"]]["details"]["status"], 200)
            self.assertEqual(events[ok["id"]]["details"]["event"], "ticket.created")
            self.assertEqual(events[bad["id"]]["outcome"], "failure")
            self.assertEqual((events[bad["id"]]["details"]["status"], events[bad["id"]]["details"]["attempt"]),
                             (500, 3))
            self.assertEqual(events[soar["id"]]["outcome"], "skipped")
            for e in events.values():
                self.assertEqual((e["tenant"], e["details"]["ticket"]), ("soc-games", t["number"]))
            self.assert_no_secrets([HOOK_SECRET, SOAR_SECRET])
            for hook in (ok, bad, soar):
                self.admin.delete("/webhooks/%d" % hook["id"])
        finally:
            ok_rx.close()
            bad_rx.close()
            soar_rx.close()

    def test_audit_status_endpoint(self):
        resp = self.admin.get("/audit/status")
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.json["recorder"])
        self.assertEqual(self.client_for("analyst1").get("/audit/status").status, 403)
        self.assertEqual(self.client_for("requester1").get("/audit/status").status, 403)

    def assert_no_secrets(self, secrets):
        blob = json.dumps(self.audit.events, default=str)
        for secret in secrets:
            self.assertNotIn(secret, blob)
        for event in self.audit.events:
            for key in event["details"]:
                self.assertNotIn(key.lower(), ("password", "token", "secret"))


class DefaultAuditLoggerTest(unittest.TestCase):
    def test_app_wires_real_logger_with_tenant_index(self):
        saved = {k: os.environ.pop(k, None) for k in ("MYDEMOTICKET_HEC_URL", "MYDEMOTICKET_HEC_TOKEN")}
        tmp = tempfile.mkdtemp(prefix="mdt-audit-")
        try:
            app = App(tmp, seed=False, admin_password=ADMIN_PASSWORD, admin_token=ADMIN_TOKEN, quiet=True)
            try:
                self.assertIsInstance(app.audit, AuditLogger)
                self.assertFalse(app.audit.stats()["hec_enabled"])
                self.assertIsNone(app.tenant_index("soc-games"))
                app.db.execute("UPDATE tenants SET splunk_index = 'mydemoticket_x' WHERE slug = 'soc-games'")
                self.assertEqual(app.tenant_index("soc-games"), "mydemoticket_x")
                self.assertIsNone(app.tenant_index("missing"))
                app.audit.log("auth.login", tenant="soc-games", actor="admin")
                self.assertTrue(app.audit.wait_idle(5))
            finally:
                app.shutdown()
            with open(os.path.join(tmp, "logs", "audit.log"), encoding="utf-8") as handle:
                self.assertIn('"auth.login"', handle.read())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
