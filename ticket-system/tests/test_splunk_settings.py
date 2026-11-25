"""Splunk HEC connection settings (GET/PUT /settings/splunk, POST /settings/splunk/test).

Uses the real AuditLogger and a local fake HEC endpoint on 127.0.0.1 - no real network.
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from helpers import ADMIN_PASSWORD, ADMIN_TOKEN, ServerTestCase
from test_audit import HecMock

from mydemoticket.server import App

TOKEN = "aaaabbbb-cccc-dddd-eeee-ffff00001111"
TOKEN2 = "99998888-7777-6666-5555-444433332222"
HEC_ENV = ("MYDEMOTICKET_HEC_URL", "MYDEMOTICKET_HEC_TOKEN", "MYDEMOTICKET_HEC_VERIFY_TLS",
           "MYDEMOTICKET_HEC_INDEX")


class SplunkSettingsTest(ServerTestCase):
    @classmethod
    def setUpClass(cls):
        cls._env = mock.patch.dict(os.environ, {})
        cls._env.start()
        for key in HEC_ENV:
            os.environ.pop(key, None)
        super().setUpClass()
        cls.hec = HecMock()
        cls.admin.post("/users", {"username": "tadmin", "role": "admin", "tenant": "soc-games"})

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.hec.close()
        cls._env.stop()

    def tearDown(self):
        self.admin.request("PUT", "/settings/splunk", {"hec_url": "", "index": "", "verify_tls": True,
                                                       "enabled": False, "token": ""})

    def put(self, body, client=None):
        return (client or self.admin).request("PUT", "/settings/splunk", body)

    def configure(self, **extra):
        body = {"hec_url": self.hec.url, "index": "mdt_audit", "verify_tls": True, "enabled": True, "token": TOKEN}
        body.update(extra)
        resp = self.put(body)
        self.assertEqual(resp.status, 200, resp)
        self.assertTrue(self.app.audit.wait_idle(10))  # the settings.splunk_update event is shipped
        return resp

    def all_output(self):
        """Every place the token must never show up: API responses, the local audit log."""
        self.assertTrue(self.app.audit.wait_idle(10))
        blobs = [self.admin.get("/settings/splunk").raw, self.admin.get("/audit/status").raw]
        with open(self.app.audit.log_path, encoding="utf-8") as handle:
            blobs.append(handle.read().encode("utf-8"))
        return b"\n".join(blobs)

    def test_initial_state(self):
        resp = self.admin.get("/settings/splunk")
        self.assertEqual(resp.status, 200, resp)
        self.assertEqual(set(resp.json), {"hec_url", "index", "verify_tls", "enabled", "token_set", "source"})
        self.assertEqual((resp.json["token_set"], resp.json["enabled"]), (False, False))

    def test_put_applies_live_and_never_returns_token(self):
        resp = self.configure()
        self.assertEqual(resp.json, {"hec_url": self.hec.url, "index": "mdt_audit", "verify_tls": True,
                                     "enabled": True, "token_set": True, "source": "settings"})
        self.assertNotIn(TOKEN.encode(), resp.raw)
        self.assertTrue(self.admin.get("/audit/status").json["hec_enabled"])
        before = len(self.hec.events())
        self.create_ticket(short_description="shipped live")
        self.assertTrue(self.app.audit.wait_idle(10))
        events = self.hec.events()[before:]
        self.assertIn("ticket.create", [e["event"]["action"] for e in events])
        self.assertEqual(events[0]["index"], "mdt_audit")
        self.assertEqual(self.hec.requests[-1]["headers"]["Authorization"], "Splunk " + TOKEN)
        update = [json.loads(line) for line in open(self.app.audit.log_path, encoding="utf-8")
                  if '"settings.splunk_update"' in line][-1]
        self.assertEqual(update["details"]["credential"], "replaced")
        self.assertNotIn(TOKEN.encode(), self.all_output())

    def test_token_keep_and_clear(self):
        self.configure()
        resp = self.put({"hec_url": self.hec.url, "index": "", "verify_tls": False, "enabled": True})
        self.assertEqual((resp.status, resp.json["token_set"], resp.json["verify_tls"]), (200, True, False))
        self.configure(token=TOKEN2)
        before = len(self.hec.requests)
        self.create_ticket(short_description="new token")
        self.assertTrue(self.app.audit.wait_idle(10))
        self.assertEqual(self.hec.requests[before]["headers"]["Authorization"], "Splunk " + TOKEN2)
        # clearing the token while enabled is refused; disabling and clearing works
        self.assertEqual(self.put({"enabled": True, "token": ""}).status, 422)
        resp = self.put({"enabled": False, "token": ""})
        self.assertEqual((resp.status, resp.json["token_set"], resp.json["enabled"]), (200, False, False))
        self.assertFalse(self.admin.get("/audit/status").json["hec_enabled"])
        blob = self.all_output()
        self.assertNotIn(TOKEN.encode(), blob)
        self.assertNotIn(TOKEN2.encode(), blob)

    def test_disable_stops_shipping(self):
        self.configure()
        self.put({"enabled": False})
        before = len(self.hec.requests)
        self.create_ticket(short_description="not shipped")
        self.assertTrue(self.app.audit.wait_idle(10))
        self.assertEqual(len(self.hec.requests), before)

    def test_validation(self):
        for body in ({"hec_url": "ftp://x"}, {"hec_url": 5}, {"index": "Bad Index"}, {"verify_tls": "yes"},
                     {"enabled": 1}, {"token": "has space"}, {"token": 123}, {"secret": "x"},
                     {"hec_url": "", "enabled": True, "token": TOKEN},
                     {"hec_url": self.hec.url, "enabled": True, "token": ""}):
            with self.subTest(body=body):
                resp = self.put(body)
                self.assertEqual(resp.status, 422, resp)
        self.assertEqual(self.admin.get("/settings/splunk").json["source"] in ("none", "settings"), True)

    def test_test_endpoint(self):
        resp = self.admin.post("/settings/splunk/test")
        self.assertEqual((resp.status, resp.json["ok"]), (200, False))
        self.configure(enabled=False)  # testing works before enabling
        before = len(self.hec.requests)
        resp = self.admin.post("/settings/splunk/test")
        self.assertEqual(resp.status, 200, resp)
        self.assertTrue(resp.json["ok"], resp)
        self.assertEqual(len(self.hec.requests), before + 1)  # exactly one synchronous event
        sent = json.loads(self.hec.requests[-1]["body"])
        self.assertEqual((sent["event"]["action"], sent["event"]["actor"], sent["fields"]["tenant"]),
                         ("splunk.test", "admin", "soc-games"))
        self.assertNotIn(TOKEN.encode(), resp.raw)

    def test_test_endpoint_reports_hec_error_without_token(self):
        self.configure()
        self.hec.statuses.extend([403])
        resp = self.admin.post("/settings/splunk/test")
        self.assertEqual((resp.status, resp.json["ok"]), (200, False))
        self.assertIn("403", resp.json["message"])
        self.assertNotIn(TOKEN, resp.json["message"])
        self.assertNotIn(TOKEN.encode(), self.all_output())

    def test_only_platform_admin(self):
        for name in ("tadmin", "analyst1", "soar"):
            client = self.client_for(name)
            with self.subTest(user=name):
                self.assertEqual(client.get("/settings/splunk").status, 403)
                self.assertEqual(self.put({"enabled": False}, client).status, 403)
                self.assertEqual(client.post("/settings/splunk/test").status, 403)


class SplunkSettingsPrecedenceTest(unittest.TestCase):
    """Stored settings win over MYDEMOTICKET_HEC_* and survive a restart."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mdt-splunk-")
        self.env_hec, self.stored_hec = HecMock(), HecMock()

    def tearDown(self):
        self.env_hec.close()
        self.stored_hec.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self):
        return App(self.tmp, seed=False, admin_password=ADMIN_PASSWORD, admin_token=ADMIN_TOKEN, quiet=True)

    def test_env_then_settings(self):
        env = {"MYDEMOTICKET_HEC_URL": self.env_hec.url, "MYDEMOTICKET_HEC_TOKEN": TOKEN,
               "MYDEMOTICKET_HEC_INDEX": "env_index"}
        with mock.patch.dict(os.environ, env):
            app = self.start()
            try:
                from mydemoticket.settings import save_splunk, splunk_config
                cfg = splunk_config(app.db)
                self.assertEqual((cfg["source"], cfg["hec_url"], cfg["index"], cfg["enabled"]),
                                 ("env", self.env_hec.url, "env_index", True))
                # token omitted on save -> the environment token keeps being used
                save_splunk(app.db, self.stored_hec.url, "", True, True, None)
                self.assertEqual(splunk_config(app.db)["token"], TOKEN)
            finally:
                app.shutdown()
            app = self.start()
            try:
                self.assertEqual(app.audit.connection()["hec_url"], self.stored_hec.url + "/services/collector/event")
                app.audit.log("auth.login", tenant="soc-games", actor="admin")
                self.assertTrue(app.audit.wait_idle(10))
                self.assertEqual(len(self.env_hec.requests), 0)
                self.assertEqual(len(self.stored_hec.requests), 1)
            finally:
                app.shutdown()


if __name__ == "__main__":
    unittest.main()
