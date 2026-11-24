"""Multi-tenancy: isolation matrix, tenant context, tenants API, user rules, reports, webhooks."""

import datetime
import json
import unittest

from helpers import ADMIN_PASSWORD, Client, Receiver, ServerTestCase
from test_webhooks import SoarMock

ACME = {"X-MyDemoTicket-Tenant": "acme"}
PASSWORD = "acme-password-123"


class TenantFixture(ServerTestCase):
    """soc-games (seed) + tenant 'acme' (seeded) with one user per role and own objects."""

    use_recorder = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        resp = cls.admin.post("/tenants", {"slug": "acme", "name": "ACME Corp", "seed": True})
        assert resp.status == 201, resp
        cls.acme_admin = cls.make_user("acme-admin", "admin", "acme")
        cls.acme_agent = cls.make_user("acme-agent", "agent", "acme", teams=["SOC L1"])
        cls.acme_req = cls.make_user("acme-req", "requester", "acme")
        cls.acme_int = cls.make_user("acme-int", "integration", "acme")
        cls.sg_admin = cls.make_user("sg-admin", "admin", "soc-games")
        cls.analyst1 = cls.client_for("analyst1")
        cls.requester1 = cls.client_for("requester1")
        cls.soar = cls.client_for("soar")

        # objects in soc-games (admin without header works in the default tenant)
        cls.sg_ticket = cls.admin.post("/tickets", {"short_description": "sg ticket"}).json
        cls.sg_req_ticket = cls.requester1.post("/tickets", {"short_description": "sg requester"}).json
        cls.admin.post("/teams", {"name": "SG Team"})
        cls.admin.post("/custom-fields", {"name": "sg_only", "type": "string"})
        cls.sg_hook = cls.admin.post("/webhooks", {"url": "http://127.0.0.1:9/sg", "active": False}).json
        cls.sg_token = cls.admin.post("/users/analyst2/tokens", {"name": "sg"}).json

        # objects in acme
        cls.acme_ticket = cls.acme_agent.post("/tickets", {"short_description": "acme ticket"}).json
        cls.acme_admin.post("/teams", {"name": "Acme Team"})
        cls.acme_admin.post("/custom-fields", {"name": "acme_only", "type": "string"})
        cls.acme_hook = cls.acme_admin.post("/webhooks", {"url": "http://127.0.0.1:9/acme", "active": False}).json
        cls.acme_token = cls.acme_admin.post("/users/acme-agent/tokens", {"name": "acme"}).json

    @classmethod
    def make_user(cls, username, role, tenant, teams=None):
        body = {"username": username, "role": role, "tenant": tenant, "password": PASSWORD}
        if teams:
            body["teams"] = teams
        resp = cls.admin.post("/users", body)
        assert resp.status == 201, resp
        return cls.client_for(username, tenant)


class IsolationMatrixTest(TenantFixture):
    def foreign_requests(self, own, other):
        """(method, path, body) for objects of the other tenant."""
        t = other["ticket"]["number"]
        return [
            ("GET", "/tickets/%s" % t, None),
            ("GET", "/tickets/%d" % other["ticket"]["id"], None),
            ("PATCH", "/tickets/%s" % t, {"priority": 1}),
            ("GET", "/tickets/%s/journal" % t, None),
            ("POST", "/tickets/%s/comments" % t, {"body": "x"}),
            ("PATCH", "/custom-fields/%s" % other["field"], {"label": "x"}),
            ("DELETE", "/custom-fields/%s" % other["field"], None),
            ("POST", "/teams/%s/members" % other["team"].replace(" ", "%20"), {"username": own["user"]}),
            ("DELETE", "/teams/%s/members/%s" % (other["team"].replace(" ", "%20"), other["user"]), None),
            ("PATCH", "/users/%s" % other["user"], {"display_name": "x"}),
            ("GET", "/users/%s/tokens" % other["user"], None),
            ("POST", "/users/%s/tokens" % other["user"], {}),
            ("DELETE", "/users/%s/tokens/%d" % (other["user"], other["token_id"]), None),
            ("DELETE", "/webhooks/%d" % other["hook_id"], None),
            ("GET", "/webhooks/%d/deliveries" % other["hook_id"], None),
        ]

    ADMIN_ONLY = ("/custom-fields/", "/teams/", "/users/", "/webhooks/")

    def expected(self, role, path):
        # role checks come first (403); objects of other tenants are always 404
        if role not in ("admin", "platform_admin") and path.startswith(self.ADMIN_ONLY):
            return 403
        return 404

    def run_matrix(self, clients, own, other):
        for role, client in clients.items():
            for method, path, body in self.foreign_requests(own, other):
                with self.subTest(role=role, method=method, path=path):
                    resp = client.request(method, path, body)
                    self.assertEqual(resp.status, self.expected(role, path), resp)

    def test_acme_roles_on_soc_games_objects(self):
        own = {"user": "acme-agent"}
        other = {"ticket": self.sg_ticket, "field": "sg_only", "team": "SG Team", "user": "analyst2",
                 "token_id": self.sg_token["id"], "hook_id": self.sg_hook["id"]}
        self.run_matrix({"admin": self.acme_admin, "agent": self.acme_agent, "requester": self.acme_req,
                         "integration": self.acme_int}, own, other)

    def test_soc_games_roles_on_acme_objects(self):
        own = {"user": "analyst1"}
        other = {"ticket": self.acme_ticket, "field": "acme_only", "team": "Acme Team", "user": "acme-agent",
                 "token_id": self.acme_token["id"], "hook_id": self.acme_hook["id"]}
        self.run_matrix({"admin": self.sg_admin, "agent": self.analyst1, "requester": self.requester1,
                         "integration": self.soar, "platform_admin": self.admin}, own, other)

    def test_foreign_objects_unchanged(self):
        self.assertEqual(self.admin.get("/tickets/" + self.sg_ticket["number"]).json["priority"], 3)
        self.assertIn("sg_only", [f["name"] for f in self.admin.get("/custom-fields").json])
        self.assertEqual(self.admin.get("/webhooks").status, 200)
        self.assertIn(self.sg_hook["id"], [h["id"] for h in self.admin.get("/webhooks").json])

    def test_lists_contain_only_own_tenant(self):
        for name, client in (("admin", self.acme_admin), ("agent", self.acme_agent), ("int", self.acme_int)):
            with self.subTest(client=name):
                numbers = [t["number"] for t in client.get("/tickets?limit=500").json["items"]]
                self.assertIn(self.acme_ticket["number"], numbers)
                self.assertNotIn(self.sg_ticket["number"], numbers)
                users = [u["username"] for u in client.get("/users").json]
                self.assertIn("acme-agent", users)
                self.assertNotIn("analyst1", users)
                self.assertNotIn("admin", users)  # platform admins are not tenant members
                self.assertTrue(all(u["tenant"] == "acme" for u in client.get("/users").json))
                teams = sorted(t["name"] for t in client.get("/teams").json)
                self.assertEqual(teams, ["Acme Team", "SOC L1", "SOC L2"])
                fields = sorted(f["name"] for f in client.get("/custom-fields").json)
                self.assertNotIn("sg_only", fields)
                self.assertIn("acme_only", fields)
        self.assertEqual([h["id"] for h in self.acme_admin.get("/webhooks").json], [self.acme_hook["id"]])
        self.assertEqual(self.acme_req.get("/tickets").json["total"], 0)
        sg_numbers = [t["number"] for t in self.analyst1.get("/tickets?limit=500").json["items"]]
        self.assertNotIn(self.acme_ticket["number"], sg_numbers)
        sg_hooks = [h["id"] for h in self.admin.get("/webhooks").json]
        self.assertNotIn(self.acme_hook["id"], sg_hooks)
        self.assertEqual(self.analyst1.get("/tickets?q=acme").json["total"], 0)

    def test_assignment_and_custom_fields_only_within_tenant(self):
        cases = [
            {"assigned_to": "analyst1"},
            {"assignment_group": "IT Ops"},     # exists only in soc-games
            {"assignment_group": "SG Team"},
            {"caller": "requester1"},
            {"custom_fields": {"sg_only": "x"}},
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                body = dict({"short_description": "x"}, **extra)
                self.assertEqual(self.acme_admin.post("/tickets", body).status, 422)
                self.assertEqual(self.acme_admin.patch("/tickets/" + self.acme_ticket["number"], extra).status, 422)
        ok = self.acme_admin.post("/tickets", {"short_description": "ok", "assignment_group": "SOC L1",
                                               "assigned_to": "acme-agent", "custom_fields": {"acme_only": "y"}})
        self.assertEqual(ok.status, 201, ok)
        self.assertEqual(self.admin.post("/tickets", {"short_description": "x",
                                                      "custom_fields": {"acme_only": "x"}}).status, 422)
        self.assertEqual(self.admin.post("/tickets", {"short_description": "x",
                                                      "assigned_to": "acme-agent"}).status, 422)
        # team members only from the same tenant
        self.assertEqual(self.acme_admin.post("/teams/Acme%20Team/members", {"username": "analyst1"}).status, 422)
        self.assertEqual(self.acme_admin.post("/teams", {"name": "Mixed", "members": ["analyst1"]}).status, 422)
        self.assertEqual(self.acme_admin.patch("/users/acme-agent", {"teams": ["IT Ops"]}).status, 422)

    def test_team_and_field_names_unique_per_tenant(self):
        self.assertEqual(self.acme_admin.post("/teams", {"name": "IT Ops"}).status, 201)
        self.assertEqual(self.acme_admin.post("/teams", {"name": "IT Ops"}).status, 409)
        self.assertEqual(self.acme_admin.post("/custom-fields", {"name": "sg_only", "type": "number"}).status, 201)
        sg = {f["name"]: f for f in self.admin.get("/custom-fields").json}
        self.assertEqual(sg["sg_only"]["type"], "string")
        acme_t = self.acme_admin.post("/tickets", {"short_description": "n", "custom_fields": {"sg_only": 3}})
        self.assertEqual(acme_t.status, 201)
        self.assertEqual(self.admin.post("/tickets", {"short_description": "n",
                                                      "custom_fields": {"sg_only": 3}}).status, 422)

    def test_ticket_numbers_are_global(self):
        a = self.acme_agent.post("/tickets", {"short_description": "a"}).json
        b = self.analyst1.post("/tickets", {"short_description": "b"}).json
        c = self.acme_agent.post("/tickets", {"short_description": "c"}).json
        nums = [int(t["number"][3:]) for t in (a, b, c)]
        self.assertEqual(nums, [nums[0], nums[0] + 1, nums[0] + 2])


class TenantContextTest(TenantFixture):
    def test_platform_admin_with_and_without_header(self):
        own = [t["number"] for t in self.admin.get("/tickets?limit=500").json["items"]]
        self.assertIn(self.sg_ticket["number"], own)
        self.assertNotIn(self.acme_ticket["number"], own)
        acme = [t["number"] for t in self.admin.get("/tickets?limit=500", headers=ACME).json["items"]]
        self.assertEqual(acme, [self.acme_ticket["number"]])
        self.assertEqual(self.admin.get("/tickets/" + self.acme_ticket["number"], headers=ACME).status, 200)
        self.assertEqual(self.admin.get("/tickets/" + self.acme_ticket["number"]).status, 404)
        resp = self.admin.get("/tickets", headers={"X-MyDemoTicket-Tenant": "nope"})
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.json, {"error": "tenant not found"})
        created = self.admin.post("/tickets", {"short_description": "pa in acme", "assigned_to": "acme-agent"},
                                  headers=ACME)
        self.assertEqual(created.status, 201, created)
        self.assertEqual(created.json["caller"], "admin")
        me = self.admin.get("/me", headers=ACME).json
        self.assertEqual(me["active_tenant"], {"slug": "acme", "name": "ACME Corp"})
        self.assertEqual(me["tenant"], {"slug": "acme", "name": "ACME Corp"})
        self.assertEqual({t["slug"] for t in me["tenants"]} >= {"soc-games", "acme"}, True)
        self.assertEqual(self.admin.get("/me").json["active_tenant"]["slug"], "soc-games")

    def test_header_of_other_tenant_is_mismatch(self):
        resp = self.acme_agent.get("/tickets", headers={"X-MyDemoTicket-Tenant": "soc-games"})
        self.assertEqual(resp.status, 403)
        self.assertEqual(resp.json, {"error": "tenant mismatch"})
        resp = self.acme_agent.get("/tickets", headers={"X-MyDemoTicket-Tenant": "does-not-exist"})
        self.assertEqual(resp.json, {"error": "tenant mismatch"})
        self.assertEqual(self.acme_agent.get("/tickets", headers=ACME).status, 200)
        me = self.acme_agent.get("/me").json
        self.assertEqual(me["tenant"], {"slug": "acme", "name": "ACME Corp"})
        self.assertNotIn("tenants", me)
        self.assertNotIn("active_tenant", me)

    def test_session_tenant_switch_with_csrf(self):
        client = Client(self.port)
        login = client.post("/auth/login", {"username": "admin", "password": ADMIN_PASSWORD})
        self.assertEqual(login.status, 200)
        self.assertEqual(client.post("/auth/tenant", {"tenant": "acme"}).status, 403)  # no CSRF
        client.csrf = "forged"
        self.assertEqual(client.post("/auth/tenant", {"tenant": "acme"}).status, 403)
        client.csrf = login.json["csrf_token"]
        self.assertEqual(client.post("/auth/tenant", {"tenant": "nope"}).status, 404)
        self.assertEqual(client.post("/auth/tenant", {}).status, 404)
        resp = client.post("/auth/tenant", {"tenant": "acme"})
        self.assertEqual(resp.status, 200, resp)
        self.assertEqual(resp.json["active_tenant"]["slug"], "acme")
        numbers = [t["number"] for t in client.get("/tickets?limit=500").json["items"]]
        self.assertIn(self.acme_ticket["number"], numbers)
        self.assertNotIn(self.sg_ticket["number"], numbers)
        self.assertEqual(client.get("/me").json["active_tenant"]["slug"], "acme")
        # header beats the session
        hdr = client.get("/tickets?limit=500", headers={"X-MyDemoTicket-Tenant": "soc-games"}).json
        self.assertIn(self.sg_ticket["number"], [t["number"] for t in hdr["items"]])
        # a new session starts in the default tenant again
        other = Client(self.port)
        other.post("/auth/login", {"username": "admin", "password": ADMIN_PASSWORD})
        self.assertEqual(other.get("/me").json["active_tenant"]["slug"], "soc-games")
        self.assertEqual(client.post("/auth/tenant", {"tenant": "soc-games"}).json["active_tenant"]["slug"],
                         "soc-games")

    def test_tenant_switch_not_for_others(self):
        client = Client(self.port)
        login = client.post("/auth/login", {"username": "acme-admin", "password": PASSWORD})
        self.assertEqual(login.status, 200)
        client.csrf = login.json["csrf_token"]
        self.assertEqual(client.post("/auth/tenant", {"tenant": "acme"}).status, 403)
        self.assertEqual(self.admin.post("/auth/tenant", {"tenant": "acme"}).status, 400)  # token, no session

    def test_inactive_tenant_blocks_its_users(self):
        self.assertEqual(self.admin.post("/tenants", {"slug": "gone", "name": "Gone"}).status, 201)
        self.admin.post("/users", {"username": "gone-agent", "role": "agent", "tenant": "gone",
                                   "password": PASSWORD})
        token = self.admin.post("/users/gone-agent/tokens", {}, headers={"X-MyDemoTicket-Tenant": "gone"}).json
        client = Client(self.port, token["token"])
        session = Client(self.port)
        self.assertEqual(session.post("/auth/login", {"username": "gone-agent", "password": PASSWORD}).status, 200)
        self.assertEqual(client.get("/me").status, 200)
        self.assertEqual(self.admin.patch("/tenants/gone", {"active": False}).json["active"], False)
        for resp in (client.get("/me"), session.get("/me"),
                     Client(self.port).post("/auth/login", {"username": "gone-agent", "password": PASSWORD})):
            self.assertEqual(resp.status, 403)
            self.assertEqual(resp.json, {"error": "tenant inactive"})
        # a wrong password still says 401, not 403 (no tenant oracle)
        resp = Client(self.port).post("/auth/login", {"username": "gone-agent", "password": "wrong-password"})
        self.assertEqual(resp.status, 401)
        # platform admin can still look into the inactive tenant
        self.assertEqual(self.admin.get("/tickets", headers={"X-MyDemoTicket-Tenant": "gone"}).status, 200)
        self.admin.patch("/tenants/gone", {"active": True})
        self.assertEqual(client.get("/me").status, 200)


class TenantApiTest(TenantFixture):
    def test_only_platform_admin(self):
        for client in (self.acme_admin, self.sg_admin, self.acme_agent, self.soar):
            for method, path, body in (("GET", "/tenants", None), ("POST", "/tenants", {"slug": "x1", "name": "x"}),
                                       ("GET", "/tenants/acme", None), ("PATCH", "/tenants/acme", {"name": "y"})):
                with self.subTest(client=client, path=path, method=method):
                    self.assertEqual(client.request(method, path, body).status, 403)

    def test_create_validate_and_patch(self):
        bad = [{"slug": "A", "name": "x"}, {"slug": "a", "name": "x"}, {"slug": "a_b", "name": "x"},
               {"slug": "x" * 33, "name": "x"}, {"slug": "ok-slug"}, {"slug": "ok-slug", "name": "x", "foo": 1},
               {"slug": "ok-slug", "name": "x", "soar_url": "ftp://x"},
               {"slug": "ok-slug", "name": "x", "splunk_index": "Bad Index"},
               {"slug": "ok-slug", "name": "x", "seed": "yes"}]
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual(self.admin.post("/tenants", body).status, 422)
        resp = self.admin.post("/tenants", {"slug": "beta-2", "name": "Beta", "soar_url": "https://soar.beta.invalid",
                                            "splunk_index": "mydemoticket_beta"})
        self.assertEqual(resp.status, 201, resp)
        self.assertEqual(resp.json["slug"], "beta-2")
        self.assertEqual(resp.json["active"], True)
        self.assertEqual(resp.json["splunk_index"], "mydemoticket_beta")
        self.assertFalse(resp.json["default"])
        self.assertEqual(self.admin.post("/tenants", {"slug": "beta-2", "name": "Again"}).status, 409)
        # without seed: no teams, no fields
        beta = {"X-MyDemoTicket-Tenant": "beta-2"}
        self.assertEqual(self.admin.get("/teams", headers=beta).json, [])
        self.assertEqual(self.admin.get("/custom-fields", headers=beta).json, [])
        self.assertEqual(self.admin.get("/tenants/beta-2").json["name"], "Beta")
        self.assertEqual(self.admin.get("/tenants/missing").status, 404)
        resp = self.admin.patch("/tenants/beta-2", {"name": "Beta GmbH", "splunk_index": None, "soar_url": ""})
        self.assertEqual(resp.status, 200)
        self.assertEqual((resp.json["name"], resp.json["splunk_index"], resp.json["soar_url"]),
                         ("Beta GmbH", None, None))
        self.assertEqual(self.admin.patch("/tenants/beta-2", {"slug": "other"}).status, 422)
        self.assertEqual(self.admin.patch("/tenants/missing", {"name": "x"}).status, 404)
        self.assertEqual(self.admin.delete("/tenants/beta-2").status, 405)
        self.assertIn("beta-2", [t["slug"] for t in self.admin.get("/tenants").json])

    def test_seed_creates_teams_and_fields(self):
        teams = sorted(t["name"] for t in self.admin.get("/teams", headers=ACME).json)
        self.assertIn("SOC L1", teams)
        self.assertIn("SOC L2", teams)
        self.assertNotIn("IT Ops", teams)
        fields = {f["name"]: f for f in self.admin.get("/custom-fields", headers=ACME).json}
        for name in ("mitre_technique", "affected_host", "risk_score", "incident_type"):
            self.assertIn(name, fields)
        self.assertEqual(fields["incident_type"]["choices"], ["malware", "phishing", "data_exfiltration", "other"])

    def test_default_tenant_protected(self):
        default = self.admin.get("/tenants/soc-games").json
        self.assertTrue(default["default"])
        self.assertEqual(default["soar_url"], "https://soar.soc-games.com")
        self.assertEqual(self.admin.patch("/tenants/soc-games", {"active": False}).status, 409)
        self.assertEqual(self.admin.delete("/tenants/soc-games").status, 405)
        self.assertTrue(self.admin.get("/tenants/soc-games").json["active"])


class UserRulesTest(TenantFixture):
    def test_platform_admin_creates_users(self):
        self.assertEqual(self.admin.post("/users", {"username": "u-no-tenant", "role": "agent"}).status, 422)
        self.assertEqual(self.admin.post("/users", {"username": "u-bad", "role": "agent", "tenant": "nope"}).status,
                         422)
        self.assertEqual(self.admin.post("/users", {"username": "pa2", "role": "platform_admin",
                                                    "tenant": "acme"}).status, 422)
        resp = self.admin.post("/users", {"username": "pa2", "role": "platform_admin", "password": PASSWORD})
        self.assertEqual(resp.status, 201, resp)
        self.assertIsNone(resp.json["tenant"])
        resp = self.admin.post("/users", {"username": "acme-new", "role": "agent", "tenant": "acme",
                                          "teams": ["SOC L2"]})
        self.assertEqual(resp.status, 201)
        self.assertEqual((resp.json["tenant"], resp.json["teams"]), ("acme", ["SOC L2"]))
        # usernames are global
        self.assertEqual(self.admin.post("/users", {"username": "analyst1", "role": "agent",
                                                    "tenant": "acme"}).status, 409)

    def test_tenant_admin_creates_users_only_in_own_tenant(self):
        resp = self.acme_admin.post("/users", {"username": "acme-made", "role": "agent", "teams": ["SOC L1"]})
        self.assertEqual(resp.status, 201, resp)
        self.assertEqual(resp.json["tenant"], "acme")
        resp = self.acme_admin.post("/users", {"username": "acme-made2", "role": "agent", "tenant": "acme"})
        self.assertEqual(resp.status, 201)
        resp = self.acme_admin.post("/users", {"username": "acme-x", "role": "agent", "tenant": "soc-games"})
        self.assertEqual((resp.status, resp.json), (403, {"error": "tenant mismatch"}))
        self.assertEqual(self.acme_admin.post("/users", {"username": "acme-pa", "role": "platform_admin"}).status,
                         403)
        self.assertEqual(self.acme_admin.patch("/users/acme-made", {"role": "platform_admin"}).status, 403)
        self.assertEqual(self.acme_admin.post("/users", {"username": "acme-y", "role": "agent",
                                                         "teams": ["IT Ops"]}).status, 422)
        self.assertEqual(self.acme_admin.post("/users", {"username": "analyst2", "role": "agent"}).status, 409)
        self.assertEqual(self.acme_agent.post("/users", {"username": "acme-z", "role": "agent"}).status, 403)
        self.assertEqual(self.acme_admin.patch("/users/acme-made", {"role": "admin"}).json["role"], "admin")
        self.assertEqual(self.acme_admin.patch("/users/admin", {"display_name": "x"}).status, 404)
        token = self.acme_admin.post("/users/acme-made/tokens", {"name": "t"})
        self.assertEqual(token.status, 201)
        self.assertEqual(Client(self.port, token.json["token"]).get("/me").json["tenant"]["slug"], "acme")

    def test_platform_admin_role_boundaries(self):
        self.admin.post("/users", {"username": "pa3", "role": "platform_admin", "password": PASSWORD})
        self.assertEqual(self.admin.patch("/users/pa3", {"role": "agent"}).status, 422)
        self.assertEqual(self.admin.patch("/users/analyst2", {"role": "platform_admin"}).status, 422)
        self.assertEqual(self.admin.patch("/users/pa3", {"display_name": "PA 3"}).status, 200)
        self.assertEqual(self.admin.get("/users/pa3/tokens", headers=ACME).status, 200)


class ReportsTest(TenantFixture):
    def set_times(self, number, created, resolved=None):
        self.app.db.execute("UPDATE tickets SET created_at = ?, resolved_at = ? WHERE number = ?",
                            (created, resolved, number))

    def test_summary_numbers(self):
        self.assertEqual(self.admin.post("/tenants", {"slug": "rpt", "name": "Reports", "seed": True}).status, 201)
        rpt = {"X-MyDemoTicket-Tenant": "rpt"}
        self.admin.post("/users", {"username": "rpt-agent", "role": "agent", "tenant": "rpt", "teams": ["SOC L1"]})
        self.admin.post("/users", {"username": "rpt-req", "role": "requester", "tenant": "rpt"})
        self.admin.post("/users", {"username": "rpt-int", "role": "integration", "tenant": "rpt"})
        agent = self.client_for("rpt-agent", "rpt")
        empty = agent.get("/reports/summary").json
        self.assertEqual(empty["tickets_total"], 0)
        self.assertIsNone(empty["mttr_hours"])
        self.assertEqual(empty["tenant"], "rpt")

        def mk(**kw):
            return self.admin.post("/tickets", dict({"short_description": "r"}, **kw), headers=rpt).json["number"]

        a = mk(priority=1, assignment_group="SOC L1")
        b = mk(priority=2, assignment_group="SOC L1")
        c = mk(priority=3)
        d = mk(priority=4, assignment_group="SOC L2")
        self.admin.patch("/tickets/" + a, {"state": "resolved", "close_notes": "x"}, headers=rpt)
        self.admin.patch("/tickets/" + b, {"state": "resolved", "close_notes": "x"}, headers=rpt)
        self.admin.patch("/tickets/" + b, {"state": "closed"}, headers=rpt)
        self.admin.patch("/tickets/" + c, {"state": "in_progress"}, headers=rpt)
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)

        def ago(**delta):
            return (now - datetime.timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")

        self.set_times(a, ago(days=3), ago(days=2, hours=14))   # resolved after 10 h
        self.set_times(b, ago(days=20), ago(days=18, hours=18))                  # 30 h, resolved 18 d ago
        self.set_times(d, "2020-01-01T00:00:00Z")                                # old, open
        # a ticket in another tenant must not count
        self.admin.post("/tickets", {"short_description": "other", "priority": 1})

        rep = agent.get("/reports/summary").json
        self.assertEqual(rep["tickets_total"], 4)
        self.assertEqual(rep["open_count"], 2)
        self.assertEqual(rep["by_state"], {"new": 1, "in_progress": 1, "on_hold": 0, "resolved": 1, "closed": 1,
                                           "cancelled": 0})
        self.assertEqual(rep["by_priority"], {"1": 1, "2": 1, "3": 1, "4": 1})
        self.assertEqual(rep["by_team"], {"SOC L1": 2, "SOC L2": 1, "(none)": 1})
        self.assertEqual(rep["mttr_hours"], 20.0)
        self.assertEqual(rep["created_last_7d"], 2)   # a and c
        self.assertEqual(rep["resolved_last_7d"], 1)  # a
        self.assertEqual(set(rep), {"tenant", "generated_at", "tickets_total", "open_count", "by_state",
                                    "by_priority", "by_team", "created_last_7d", "resolved_last_7d", "mttr_hours"})
        since = agent.get("/reports/summary?since=2026-01-01T00:00:00Z").json
        self.assertEqual(since["tickets_total"], 3)
        recent = agent.get("/reports/summary?since=" + ago(days=7)).json
        self.assertEqual((recent["tickets_total"], recent["mttr_hours"]), (2, 10.0))
        self.assertEqual(since["by_team"]["SOC L2"], 0)
        self.assertEqual(agent.get("/reports/summary?since=garbage").status, 422)
        self.assertEqual(self.admin.get("/reports/summary", headers=rpt).json, dict(rep, generated_at=self.admin.get(
            "/reports/summary", headers=rpt).json["generated_at"]))
        self.assertEqual(self.client_for("rpt-req", "rpt").get("/reports/summary").status, 403)
        integration_report = self.client_for("rpt-int", "rpt").get("/reports/summary")  # read-only for SOAR/MCP
        self.assertEqual(integration_report.status, 200)
        self.assertEqual(integration_report.json["tenant"], "rpt")
        self.assertEqual(self.acme_admin.get("/reports/summary").json["tenant"], "acme")


class TenantWebhookTest(TenantFixture):
    def test_webhooks_fire_only_for_own_tenant(self):
        sg_rx, acme_rx, soar = Receiver(), Receiver(), SoarMock()
        try:
            sg = self.admin.post("/webhooks", {"url": sg_rx.url, "events": ["ticket.created"]}).json
            acme = self.acme_admin.post("/webhooks", {"url": acme_rx.url, "events": ["ticket.created"]}).json
            acme_soar = self.acme_admin.post("/webhooks", {"type": "soar", "url": soar.url, "secret": "acme-ph-token",
                                                           "events": ["ticket.created"]}).json
            t_acme = self.acme_agent.post("/tickets", {"short_description": "acme hook"}).json
            t_sg = self.analyst1.post("/tickets", {"short_description": "sg hook"}).json
            self.wait_webhooks()
            sg_bodies = [json.loads(r["body"]) for r in sg_rx.requests]
            acme_bodies = [json.loads(r["body"]) for r in acme_rx.requests]
            self.assertEqual([(b["ticket"]["number"], b["tenant"]) for b in sg_bodies], [(t_sg["number"], "soc-games")])
            self.assertEqual([(b["ticket"]["number"], b["tenant"]) for b in acme_bodies],
                             [(t_acme["number"], "acme")])
            artifacts = soar.requests("/rest/artifact")
            self.assertEqual([a["body"]["cef"]["ticketNumber"] for a in artifacts], [t_acme["number"]])
            self.assertEqual(artifacts[0]["body"]["cef"]["tenant"], "acme")
            self.assertEqual(soar.requests("/rest/container")[0]["body"]["name"],
                             "%s: acme hook" % t_acme["number"])
            log = self.acme_admin.get("/webhooks/%d/deliveries" % acme["id"]).json
            self.assertEqual([d["ticket_number"] for d in log], [t_acme["number"]])
            rows = self.app.db.query("SELECT DISTINCT tenant_id FROM webhook_deliveries WHERE webhook_id = ?",
                                     (acme_soar["id"],))
            self.assertEqual(len(rows), 1)
            for hook in (sg,):
                self.admin.delete("/webhooks/%d" % hook["id"])
            for hook in (acme, acme_soar):
                self.acme_admin.delete("/webhooks/%d" % hook["id"])
        finally:
            sg_rx.close()
            acme_rx.close()
            soar.close()


if __name__ == "__main__":
    unittest.main()
