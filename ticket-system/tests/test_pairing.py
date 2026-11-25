"""ES + SOAR pairing wizard (GET /pairing, POST /pairing/check, POST /pairing/apply) against a local fake SOAR."""

import contextlib
import importlib.util
import io
import json
import os
import tarfile
import unittest
import unittest.mock

from helpers import ServerTestCase
from fake_soar import FakeSoar

from mydemoticket import pairing
from mydemoticket.auth import BUILTIN_ROLE_PERMISSIONS, GRANTABLE_PERMISSIONS, hash_token

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SOAR_TOKEN = "soar-ph-auth-token-for-tests-4b1d"
TENANT = {"X-MyDemoTicket-Tenant": "acme"}


class BundleTest(unittest.TestCase):
    def test_bundle_found_in_repo_and_env_override(self):
        self.assertEqual(pairing.bundle_dir(), REPO)
        with unittest.mock.patch.dict(os.environ, {pairing.BUNDLE_ENV: "/nonexistent"}):
            self.assertEqual(pairing.bundle_dir(), REPO)  # invalid override falls through
        self.assertIsNone(pairing.bundle_dir.__defaults__[0])

    def test_app_files_match_build_py(self):
        spec = importlib.util.spec_from_file_location("soar_build", os.path.join(REPO, "soar-app", "build.py"))
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        self.assertEqual(tuple(build.APP_FILES), pairing.APP_FILES)
        archive = pairing.build_app_tgz(REPO)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            names = tar.getnames()
        self.assertEqual(names, ["mydemoticket"] + ["mydemoticket/" + f for f in build.APP_FILES])

    def test_playbook_substitution_keeps_coa_and_binds_label(self):
        archive, digest = pairing.build_playbook(REPO, "mydemoticket_es_mirror",
                                                 {"SPLUNK_ASSET": "spl_x", "ES_ASSET": "es_y"}, label="lbl_z")
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            self.assertEqual(sorted(tar.getnames()), ["mydemoticket_es_mirror.json", "mydemoticket_es_mirror.py"])
            meta = json.load(tar.extractfile("mydemoticket_es_mirror.json"))
            py = tar.extractfile("mydemoticket_es_mirror.py").read().decode()
        self.assertIn('SPLUNK_ASSET = "spl_x"', py)
        self.assertIn('ES_ASSET = "es_y"', py)
        self.assertIn('EVENT_LABEL = "mydemoticket_event"', py)  # artifact label is not the container label
        compile(py, "mydemoticket_es_mirror.py", "exec")
        self.assertEqual(meta["labels"], ["lbl_z"])
        for key, value in pairing.REQUIRED_COA.items():
            self.assertEqual(meta["coa"][key], value)
        _, again = pairing.build_playbook(REPO, "mydemoticket_es_mirror",
                                          {"SPLUNK_ASSET": "spl_x", "ES_ASSET": "es_y"}, label="lbl_z")
        self.assertEqual(digest, again)
        _, other = pairing.build_playbook(REPO, "mydemoticket_es_mirror",
                                          {"SPLUNK_ASSET": "spl_x", "ES_ASSET": "es_other"}, label="lbl_z")
        self.assertNotEqual(digest, other)
        archive, _ = pairing.build_playbook(REPO, "mydemoticket_ticket_from_es", {"MYDEMOTICKET_ASSET": "mdt_a"})
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            self.assertIn('MYDEMOTICKET_ASSET = "mdt_a"', tar.extractfile("mydemoticket_ticket_from_es.py").read().decode())
            self.assertEqual(json.load(tar.extractfile("mydemoticket_ticket_from_es.json"))["labels"], ["notable"])

    def test_permission_is_admin_only_and_not_grantable(self):
        self.assertIn("pairing.manage", BUILTIN_ROLE_PERMISSIONS["admin"])
        self.assertIn("pairing.manage", BUILTIN_ROLE_PERMISSIONS["platform_admin"])
        self.assertNotIn("pairing.manage", GRANTABLE_PERMISSIONS)
        for role, perms in BUILTIN_ROLE_PERMISSIONS.items():
            if role not in ("admin", "platform_admin"):
                self.assertNotIn("pairing.manage", perms, role)


class PairingApiTest(ServerTestCase):
    use_recorder = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        assert cls.admin.post("/tenants", {"slug": "acme", "name": "ACME Corp", "seed": True}).status == 201
        for username, role, tenant in (("acme-admin", "admin", "acme"), ("acme-agent", "agent", "acme"),
                                       ("soc-admin", "admin", "soc-games")):
            assert cls.admin.post("/users", {"username": username, "role": role, "tenant": tenant}).status == 201
        cls.acme = cls.client_for("acme-admin", tenant="acme")
        cls.agent = cls.client_for("acme-agent", tenant="acme")
        cls.soc = cls.client_for("soc-admin")

    def setUp(self):
        self.soar = FakeSoar(SOAR_TOKEN)
        self.addCleanup(self.soar.close)
        self.audit.clear()

    def body(self, **extra):
        body = {"soar_url": self.soar.url, "soar_token": SOAR_TOKEN, "verify_tls": True,
                "ticket_base_url": "https://ticket.example.invalid"}
        body.update(extra)
        return body

    def apply(self, client=None, **extra):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            resp = (client or self.acme).post("/pairing/apply", self.body(**extra))
        self.assertNotIn(SOAR_TOKEN, stderr.getvalue())
        return resp

    def statuses(self, resp):
        return {s["step"]: s["status"] for s in resp.json["steps"]}

    def assert_no_secrets(self, *texts):
        tokens = [r[0] for r in self.app.db.query("SELECT token_hash FROM api_tokens")]
        for text in texts:
            self.assertNotIn(SOAR_TOKEN, text)
            self.assertNotIn("mdt_", text.replace("mdt_audit", ""))  # no ticket-system API token
            for digest in tokens:
                self.assertNotIn(digest, text)

    def reset_acme(self):
        """Remove the acme pairing so a test starts from scratch (other tests share the server)."""
        db = self.app.db
        tid = db.query_one("SELECT id FROM tenants WHERE slug = 'acme'")["id"]
        db.execute("DELETE FROM settings WHERE key = ?", ("pairing.%d" % tid,))
        db.execute("DELETE FROM webhooks WHERE tenant_id = ?", (tid,))
        user = db.query_one("SELECT id FROM users WHERE username = 'soar-acme'")
        if user:
            db.execute("DELETE FROM api_tokens WHERE user_id = ?", (user["id"],))
            db.execute("DELETE FROM users WHERE id = ?", (user["id"],))

    # ------------------------------------------------------------------ full run
    def test_full_apply_then_second_apply_creates_no_duplicates(self):
        self.reset_acme()
        resp = self.apply()
        self.assertEqual(resp.status, 200, resp)
        self.assertTrue(resp.json["ok"], resp.json)
        self.assertEqual(self.statuses(resp), {
            "user": "created", "webhook": "created", "label": "created", "app": "created", "asset": "created",
            "playbook_es_mirror": "created", "playbook_ticket_from_es": "created"})
        self.assertEqual(resp.json["soar_version"], "8.8.0.123")

        # ticket system: integration user, one token, one soar webhook with the SOAR token as secret
        user = self.app.db.query_one("SELECT * FROM users WHERE username = 'soar-acme'")
        self.assertEqual(user["role"], "integration")
        self.assertIsNone(user["password_hash"])
        tokens = self.app.db.query("SELECT * FROM api_tokens WHERE user_id = ?", (user["id"],))
        self.assertEqual(len(tokens), 1)
        hooks = self.acme.get("/webhooks").json
        self.assertEqual([(h["type"], h["url"], h["soar_label"], h["has_secret"]) for h in hooks],
                         [("soar", self.soar.url, "mydemoticket", True)])

        # SOAR: label, app, asset with the integration token, two active playbooks with substituted assets
        self.assertIn("mydemoticket", self.soar.labels)
        self.assertEqual([a["app_version"] for a in self.soar.apps.values()],
                         [pairing.app_manifest(REPO)["app_version"]])
        asset = [a for a in self.soar.assets.values() if a["name"] == "mydemoticket"]
        self.assertEqual(len(asset), 1)
        config = asset[0]["configuration"]
        self.assertEqual(config["base_url"], "https://ticket.example.invalid")
        self.assertEqual(config["ingest_label"], "mydemoticket")
        self.assertEqual(config["source_tag"], "soar")
        self.assertEqual(hash_token(config["api_token"]), tokens[0]["token_hash"])
        # the token in the asset authenticates as the integration user of acme
        from helpers import Client
        me = Client(self.port, config["api_token"]).get("/me").json
        self.assertEqual((me["username"], me["role"], me["tenant"]["slug"] if isinstance(me["tenant"], dict)
                          else me["tenant"]), ("soar-acme", "integration", "acme"))
        pbs = {p["name"]: p for p in self.soar.playbooks.values()}
        self.assertEqual(sorted(pbs), ["mydemoticket_es_mirror", "mydemoticket_ticket_from_es"])
        for pb in pbs.values():
            self.assertTrue(pb["active"])
            self.assertFalse(pb["draft_mode"])
        self.assertEqual(pbs["mydemoticket_es_mirror"]["labels"], ["mydemoticket"])
        self.assertIn('ES_ASSET = "builtin_mc_connector"', pbs["mydemoticket_es_mirror"]["py"])

        # second run with the same input: everything exists, nothing is written in SOAR, no token rotation
        before = len(self.soar.writes())
        again = self.apply()
        self.assertTrue(again.json["ok"], again.json)
        self.assertEqual(set(self.statuses(again).values()), {"exists"}, again.json)
        self.assertEqual(len(self.soar.writes()), before)
        self.assertEqual(len(self.acme.get("/webhooks").json), 1)
        self.assertEqual(len([a for a in self.soar.assets.values() if a["name"] == "mydemoticket"]), 1)
        self.assertEqual(len(self.soar.playbooks), 2)
        self.assertEqual([r["id"] for r in self.app.db.query("SELECT id FROM api_tokens WHERE user_id = ?",
                                                             (user["id"],))], [tokens[0]["id"]])

        # status endpoint
        status = self.acme.get("/pairing")
        self.assertEqual(status.status, 200)
        self.assertTrue(status.json["configured"])
        self.assertEqual(status.json["tenant"], "acme")
        self.assertEqual(status.json["settings"]["soar_url"], self.soar.url)
        self.assertTrue(status.json["integration_user"]["token_active"])
        self.assertTrue(status.json["webhook"]["has_secret"])
        self.assertTrue(status.json["last_apply"]["ok"])
        self.assertTrue(status.json["bundle"]["available"])

        # no secret in responses, audit events, stored pairing state or SOAR-side logs of the wizard
        state = json.dumps([r["value"] for r in self.app.db.query("SELECT value FROM settings WHERE key LIKE 'pairing.%'")])
        self.assert_no_secrets(resp.raw.decode(), again.raw.decode(), status.raw.decode(), state,
                               json.dumps(self.audit.events))
        applied = self.audit.find("pairing.apply", tenant="acme")
        self.assertEqual(len(applied), 2)
        self.assertEqual(applied[0]["details"]["steps"]["asset"], "created")
        self.assertEqual(applied[1]["outcome"], "success")
        # the SOAR token travels only in the ph-auth-token header, never in a body or URL
        for call in self.soar.calls():
            self.assertEqual(call["headers"].get("ph-auth-token"), SOAR_TOKEN)
            self.assertNotIn(SOAR_TOKEN.encode(), call["raw"])
            self.assertNotIn(SOAR_TOKEN, call["path"] + json.dumps(call["query"]))

    def test_changed_ticket_url_rotates_token_and_updates_asset(self):
        self.reset_acme()
        self.assertTrue(self.apply().json["ok"])
        user_id = self.app.db.query_one("SELECT id FROM users WHERE username = 'soar-acme'")["id"]
        old = self.app.db.query("SELECT id FROM api_tokens WHERE user_id = ?", (user_id,))
        resp = self.apply(ticket_base_url="https://other.example.invalid/")
        st = self.statuses(resp)
        self.assertEqual(st["asset"], "updated")
        self.assertEqual(st["user"], "exists")
        new = self.app.db.query("SELECT id FROM api_tokens WHERE user_id = ?", (user_id,))
        self.assertEqual(len(new), 1)
        self.assertNotEqual(new[0]["id"], old[0]["id"])
        asset = [a for a in self.soar.assets.values() if a["name"] == "mydemoticket"][0]
        self.assertEqual(asset["configuration"]["base_url"], "https://other.example.invalid")
        # new SOAR token -> webhook secret updated, no second webhook
        self.soar.token = "rotated-soar-token-77"
        resp = self.apply(soar_token="rotated-soar-token-77")
        self.assertEqual(self.statuses(resp)["webhook"], "updated")
        self.assertEqual(len(self.acme.get("/webhooks").json), 1)
        self.assertNotIn("rotated-soar-token-77", resp.raw.decode())
        # changed asset names -> mirror playbook re-imported (same SOAR playbook, no duplicate)
        self.soar._add_asset({"name": "es2", "product_name": "Enterprise Security", "product_vendor": "Splunk Inc.",
                              "configuration": {}})
        resp = self.apply(soar_token="rotated-soar-token-77", es_asset="es2")
        self.assertEqual(self.statuses(resp)["playbook_es_mirror"], "updated")
        self.assertEqual(self.statuses(resp)["playbook_ticket_from_es"], "exists")
        self.assertEqual(len(self.soar.playbooks), 2)

    # ------------------------------------------------------------------ failures
    def test_failures_are_reported_per_step(self):
        self.reset_acme()
        self.soar.fail[("POST", "/rest/app")] = (500, "disk full")
        resp = self.apply()
        self.assertEqual(resp.status, 200)
        self.assertFalse(resp.json["ok"])
        st = self.statuses(resp)
        self.assertEqual(st["user"], "created")
        self.assertEqual(st["webhook"], "created")
        self.assertEqual(st["label"], "created")
        self.assertEqual(st["app"], "failed")
        self.assertEqual(st["asset"], "failed")      # no app -> SOAR refuses the asset
        detail = {s["step"]: s["detail"] for s in resp.json["steps"]}
        self.assertIn("disk full", detail["app"])
        self.assertEqual(st["playbook_es_mirror"], "created")
        # the token minted for the failed asset is removed again
        user_id = self.app.db.query_one("SELECT id FROM users WHERE username = 'soar-acme'")["id"]
        self.assertEqual(self.app.db.query("SELECT id FROM api_tokens WHERE user_id = ?", (user_id,)), [])
        self.assertEqual(self.audit.find("pairing.apply", tenant="acme")[0]["outcome"], "failure")

        # rerun after the fix: only the missing parts are done
        del self.soar.fail[("POST", "/rest/app")]
        st = self.statuses(self.apply())
        self.assertEqual((st["user"], st["webhook"], st["label"], st["app"], st["asset"]),
                         ("exists", "exists", "exists", "created", "created"))

    def test_missing_es_asset_fails_mirror_playbook_only(self):
        self.reset_acme()
        resp = self.apply(es_asset="no_such_asset")
        st = self.statuses(resp)
        self.assertEqual(st["playbook_es_mirror"], "failed")
        self.assertEqual(st["playbook_ticket_from_es"], "created")
        self.assertIn("no_such_asset", [s for s in resp.json["steps"] if s["step"] == "playbook_es_mirror"][0]["detail"])

    def test_activation_failure_is_reported(self):
        self.reset_acme()
        self.soar.fail[("POST", "/rest/playbook/")] = (400, "cannot activate")
        st = self.statuses(self.apply())
        self.assertEqual(st["playbook_es_mirror"], "failed")
        self.assertEqual(st["playbook_ticket_from_es"], "failed")

    def test_unreachable_soar_or_bad_token_changes_nothing(self):
        self.reset_acme()
        resp = self.apply(soar_token="wrong-token-123")
        self.assertFalse(resp.json["ok"])
        self.assertEqual(resp.json["connection"]["status"], "failed")
        self.assertIn("rejected", resp.json["connection"]["detail"])
        self.assertEqual(set(self.statuses(resp).values()), {"skipped"})
        self.assertIsNone(self.app.db.query_one("SELECT 1 FROM users WHERE username = 'soar-acme'"))
        self.assertEqual(self.acme.get("/webhooks").json, [])
        self.assertNotIn("wrong-token-123", resp.raw.decode())
        resp = self.apply(soar_url="http://127.0.0.1:9")  # nothing listens on the discard port
        self.assertEqual(resp.json["connection"]["status"], "failed")
        self.assertIn("connection error", resp.json["connection"]["detail"])

    def test_steps_subset_and_validation(self):
        self.reset_acme()
        resp = self.apply(steps=["user", "label"])
        st = self.statuses(resp)
        self.assertEqual((st["user"], st["label"], st["webhook"], st["app"]), ("created", "created", "skipped", "skipped"))
        resp = self.apply(steps=["asset"])
        self.assertEqual(self.statuses(resp)["asset"], "failed")  # app not installed yet
        self.assertEqual(self.apply(steps=["nope"]).status, 422)
        self.assertEqual(self.apply(steps=[]).status, 422)
        self.assertEqual(self.apply(label="bad label").status, 422)
        self.assertEqual(self.apply(soar_url="ftp://x").status, 422)
        self.assertEqual(self.apply(soar_token="").status, 422)
        self.assertEqual(self.apply(verify_tls="yes").status, 422)
        self.assertEqual(self.apply(extra=1).status, 422)
        self.assertEqual(self.apply(ticket_base_url="not a url").status, 422)

    # ------------------------------------------------------------------ check
    def test_check_reports_connection_assets_app_and_label(self):
        resp = self.acme.post("/pairing/check", {"soar_url": self.soar.url, "soar_token": SOAR_TOKEN,
                                                 "verify_tls": False, "es_asset": "missing_es"})
        self.assertEqual(resp.status, 200, resp)
        checks = {c["check"]: c["status"] for c in resp.json["checks"]}
        self.assertEqual(checks["connection"], "ok")
        self.assertEqual(checks["splunk_asset"], "ok")
        self.assertEqual(checks["es_asset"], "failed")
        self.assertIn(checks["app"], ("ok", "info"))
        self.assertFalse(resp.json["ok"])
        self.assertEqual(resp.json["soar_version"], "8.8.0.123")
        self.assertEqual([c["method"] for c in self.soar.calls()], ["GET"] * len(self.soar.calls()))  # read only
        self.assert_no_secrets(resp.raw.decode(), json.dumps(self.audit.events))
        event = self.audit.find("pairing.check", tenant="acme")[0]
        self.assertEqual(event["outcome"], "failure")
        self.assertEqual(event["details"]["checks"]["es_asset"], "failed")
        bad = self.acme.post("/pairing/check", {"soar_url": self.soar.url, "soar_token": "nope-nope"})
        self.assertEqual({c["check"]: c["status"] for c in bad.json["checks"]}, {"connection": "failed"})

    # ------------------------------------------------------------------ permissions + tenants
    def test_permissions(self):
        for method, path in (("GET", "/pairing"), ("POST", "/pairing/check"), ("POST", "/pairing/apply")):
            resp = self.agent.request(method, path, self.body() if method == "POST" else None)
            self.assertEqual(resp.status, 403, (path, resp))
        self.assertEqual(self.soar.calls(), [])
        # custom roles can never carry pairing.manage
        resp = self.acme.post("/roles", {"name": "pairer", "permissions": ["tickets.read_all", "pairing.manage"]})
        self.assertEqual(resp.status, 422, resp)
        # platform admin works in the tenant given by the header
        resp = self.admin.get("/pairing", headers=TENANT)
        self.assertEqual((resp.status, resp.json["tenant"]), (200, "acme"))

    def test_tenant_isolation(self):
        self.reset_acme()
        self.assertTrue(self.apply().json["ok"])
        soc = self.soc.get("/pairing")
        self.assertEqual(soc.status, 200)
        self.assertEqual(soc.json["tenant"], "soc-games")
        self.assertNotEqual(soc.json["settings"]["soar_url"], self.soar.url)
        self.assertFalse(soc.json["integration_user"]["exists"])
        self.assertIsNone(soc.json["webhook"])
        # the acme admin cannot address another tenant
        self.assertEqual(self.acme.get("/pairing", headers={"X-MyDemoTicket-Tenant": "soc-games"}).status, 403)
        # pairing soc-games creates its own user/webhook; acme's stay untouched
        resp = self.apply(client=self.soc)
        st = self.statuses(resp)
        self.assertEqual((st["user"], st["webhook"]), ("created", "created"))
        self.assertEqual(st["app"], "exists")
        self.assertEqual(len(self.acme.get("/webhooks").json), 1)
        self.assertEqual(self.app.db.query_one("SELECT u.username FROM users u JOIN tenants t ON t.id = u.tenant_id "
                                               "WHERE t.slug = 'soc-games' AND u.username LIKE 'soar-%'")["username"],
                         "soar-soc-games")

    def test_default_ticket_base_url_from_request(self):
        self.reset_acme()
        resp = self.acme.get("/pairing")
        self.assertEqual(resp.json["defaults"]["ticket_base_url"], "http://127.0.0.1:%d" % self.port)
        self.assertFalse(resp.json["configured"])
        with unittest.mock.patch.dict(os.environ, {pairing.PUBLIC_URL_ENV: "https://public.example.invalid/"}):
            self.reset_acme()
            self.assertEqual(self.acme.get("/pairing").json["defaults"]["ticket_base_url"],
                             "https://public.example.invalid")


class SoarClientTest(unittest.TestCase):
    def test_injectable_client_factory(self):
        calls = []

        class Recorder:
            def __init__(self, url, token, verify):
                calls.append((url, verify))

            def request(self, method, path, body=None, query=None):
                return 200, {"version": "9.9"} if path == "/rest/version" else {"data": []}

        class Holder(pairing.PairingMixin):
            soar_client_factory = Recorder

        soar = Holder()._soar("https://soar.invalid", "t", False, pairing.Redactor("t"))
        self.assertEqual(soar.version(), "9.9")
        self.assertEqual(calls, [("https://soar.invalid", False)])

    def test_redactor(self):
        r = pairing.Redactor("abc123")
        r.add("mdt_x")
        self.assertEqual(r("x abc123 mdt_x"), "x [redacted] [redacted]")


if __name__ == "__main__":
    unittest.main()
