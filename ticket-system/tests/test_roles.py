"""Permission model: built-in roles, analyst roles, custom roles per tenant (server-side enforcement)."""

import unittest

from helpers import ServerTestCase

from mydemoticket.auth import (
    ADMIN_PERMISSIONS,
    BUILTIN_ROLE_PERMISSIONS,
    GRANTABLE_PERMISSIONS,
    PERMISSIONS,
)

ACME = {"X-MyDemoTicket-Tenant": "acme-roles"}


class BuiltinMappingTest(unittest.TestCase):
    """The five existing roles keep exactly the rights they had before the permission model."""

    def test_existing_roles(self):
        self.assertEqual(set(BUILTIN_ROLE_PERMISSIONS["platform_admin"]), set(PERMISSIONS))
        self.assertEqual(set(BUILTIN_ROLE_PERMISSIONS["admin"]),
                         set(PERMISSIONS) - {"tenants.manage", "splunk.manage"})
        self.assertEqual(set(BUILTIN_ROLE_PERMISSIONS["agent"]),
                         {"tickets.read_all", "tickets.create", "tickets.update_own", "tickets.comment",
                          "tickets.work_note", "tickets.close", "reports.view"})
        self.assertEqual(set(BUILTIN_ROLE_PERMISSIONS["integration"]),
                         set(GRANTABLE_PERMISSIONS) - {"tickets.update_own"})
        self.assertEqual(set(BUILTIN_ROLE_PERMISSIONS["requester"]), {"tickets.create", "tickets.comment"})

    def test_analyst_roles_have_no_admin_permission(self):
        for role in ("analyst_l1", "analyst_l2", "soc_lead", "read_only"):
            with self.subTest(role=role):
                self.assertFalse(set(BUILTIN_ROLE_PERMISSIONS[role]) & set(ADMIN_PERMISSIONS))


class RolesApiTest(ServerTestCase):
    use_recorder = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        assert cls.admin.post("/tenants", {"slug": "acme-roles", "name": "ACME", "seed": True}).status == 201
        users = [("l1", "analyst_l1", ["SOC L1"]), ("l2", "analyst_l2", []), ("lead", "soc_lead", []),
                 ("ro", "read_only", []), ("tadmin", "admin", [])]
        for name, role, teams in users:
            resp = cls.admin.post("/users", {"username": name, "role": role, "tenant": "soc-games", "teams": teams})
            assert resp.status == 201, resp
        assert cls.admin.post("/users", {"username": "acme-admin", "role": "admin",
                                         "tenant": "acme-roles"}).status == 201

    def test_permission_catalog(self):
        resp = self.client_for("tadmin").get("/permissions")
        self.assertEqual(resp.status, 200, resp)
        catalog = {p["name"]: p["grantable"] for p in resp.json}
        self.assertEqual(catalog["tickets.close"], True)
        self.assertEqual(catalog["users.manage"], False)
        self.assertEqual(set(catalog), set(PERMISSIONS))
        self.assertEqual(self.client_for("analyst1").get("/permissions").status, 403)

    def test_list_roles(self):
        roles = {r["name"]: r for r in self.client_for("tadmin").get("/roles").json}
        self.assertNotIn("platform_admin", roles)  # only platform admins see it
        for name in ("admin", "agent", "requester", "integration", "analyst_l1", "analyst_l2", "soc_lead",
                     "read_only"):
            self.assertTrue(roles[name]["builtin"], name)
        self.assertEqual(roles["read_only"]["permissions"], ["tickets.read_all"])
        self.assertEqual(roles["analyst_l1"]["users"], 1)
        self.assertGreaterEqual(roles["agent"]["users"], 2)  # analyst1, analyst2 of the seed
        platform = {r["name"]: r for r in self.admin.get("/roles").json}
        self.assertEqual(platform["platform_admin"]["users"], 1)
        self.assertEqual(self.client_for("analyst1").get("/roles").status, 403)
        self.assertEqual(self.client_for("l2").get("/roles").status, 403)

    def test_custom_role_crud(self):
        tadmin = self.client_for("tadmin")
        resp = tadmin.post("/roles", {"name": "triage", "label": "Triage", "description": "first look",
                                      "permissions": ["tickets.read_all", "tickets.comment", "tickets.comment"]})
        self.assertEqual(resp.status, 201, resp)
        self.assertEqual(resp.json, {"name": "triage", "label": "Triage", "description": "first look",
                                     "builtin": False, "permissions": ["tickets.comment", "tickets.read_all"],
                                     "users": 0})
        self.assertEqual(tadmin.post("/roles", {"name": "triage", "permissions": []}).status, 409)
        resp = tadmin.patch("/roles/triage", {"permissions": ["tickets.read_all"], "label": "Triage 2"})
        self.assertEqual((resp.status, resp.json["permissions"], resp.json["label"]),
                         (200, ["tickets.read_all"], "Triage 2"))
        self.assertEqual(tadmin.post("/users", {"username": "triager", "role": "triage"}).status, 201)
        self.assertEqual(tadmin.delete("/roles/triage").status, 409)
        self.assertEqual(tadmin.patch("/users/triager", {"role": "agent"}).status, 200)
        self.assertEqual(tadmin.delete("/roles/triage").status, 204)
        self.assertEqual(tadmin.delete("/roles/triage").status, 404)
        self.assertEqual(tadmin.patch("/users/triager", {"role": "triage"}).status, 422)
        actions = [e["action"] for e in self.audit.events if e["action"].startswith("role.")
                   and e["object_id"] == "triage"]
        self.assertEqual(actions, ["role.create", "role.update", "role.delete"])

    def test_custom_role_validation(self):
        tadmin = self.client_for("tadmin")
        for body in ({"name": "agent", "permissions": []}, {"name": "Bad", "permissions": []},
                     {"name": "x", "permissions": []}, {"name": "a" * 33, "permissions": []},
                     {"name": "ok_name", "permissions": ["users.manage"]},
                     {"name": "ok_name", "permissions": ["tickets.read_all", "roles.manage"]},
                     {"name": "ok_name", "permissions": ["splunk.manage"]},
                     {"name": "ok_name", "permissions": ["tenants.manage"]},
                     {"name": "ok_name", "permissions": ["tickets.fly"]},
                     {"name": "ok_name", "permissions": "tickets.read_all"},
                     {"name": "ok_name", "permissions": [], "extra": 1}):
            with self.subTest(body=body):
                self.assertEqual(tadmin.post("/roles", body).status, 422)
        self.assertEqual(tadmin.post("/roles", {"name": "valid_one", "permissions": []}).status, 201)
        self.assertEqual(tadmin.patch("/roles/valid_one", {"permissions": ["webhooks.manage"]}).status, 422)
        self.assertEqual(tadmin.patch("/roles/valid_one", {"name": "other"}).status, 422)
        self.assertEqual(tadmin.patch("/roles/agent", {"label": "x"}).status, 422)
        self.assertEqual(tadmin.patch("/roles/agent", {"label": "x"}).json["error"], "built-in roles are read-only")
        self.assertEqual(tadmin.delete("/roles/admin").status, 422)
        self.assertEqual(self.client_for("analyst1").post("/roles", {"name": "sneaky",
                                                                    "permissions": []}).status, 403)

    def test_tenant_isolation_of_custom_roles(self):
        acme = self.client_for("acme-admin", tenant="acme-roles")
        self.assertEqual(acme.post("/roles", {"name": "acme_only", "permissions": ["tickets.read_all"]}).status,
                         201)
        tadmin = self.client_for("tadmin")
        self.assertNotIn("acme_only", [r["name"] for r in tadmin.get("/roles").json])
        self.assertEqual(tadmin.patch("/roles/acme_only", {"label": "hijack"}).status, 404)
        self.assertEqual(tadmin.delete("/roles/acme_only").status, 404)
        self.assertEqual(tadmin.post("/users", {"username": "wrong-tenant-role", "role": "acme_only"}).status, 422)
        # the platform admin cannot assign a role of another tenant either
        self.assertEqual(self.admin.post("/users", {"username": "wrong2", "role": "acme_only",
                                                    "tenant": "soc-games"}).status, 422)
        self.assertEqual(self.admin.post("/users", {"username": "acme-ro", "role": "acme_only",
                                                    "tenant": "acme-roles"}).status, 201)
        self.assertIn("acme_only", [r["name"] for r in self.admin.get("/roles", headers=ACME).json])
        # the same name may exist in another tenant independently
        self.assertEqual(tadmin.post("/roles", {"name": "acme_only", "permissions": []}).status, 201)
        me = self.client_for("acme-ro", tenant="acme-roles").get("/me").json
        self.assertEqual(me["permissions"], ["tickets.read_all"])


class AnalystEnforcementTest(ServerTestCase):
    """Server-side enforcement for each new analyst role and for custom roles."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        users = [("l1", "analyst_l1", ["SOC L1"]), ("l2", "analyst_l2", []), ("lead", "soc_lead", []),
                 ("ro", "read_only", [])]
        for name, role, teams in users:
            resp = cls.admin.post("/users", {"username": name, "role": role, "tenant": "soc-games", "teams": teams})
            assert resp.status == 201, resp
        assert cls.admin.post("/roles", {"name": "no_users", "permissions": list(GRANTABLE_PERMISSIONS)},
                              headers={"X-MyDemoTicket-Tenant": "soc-games"}).status == 201
        assert cls.admin.post("/users", {"username": "custom", "role": "no_users", "tenant": "soc-games"}).status \
            == 201
        assert cls.admin.post("/roles", {"name": "commenter", "permissions": ["tickets.comment"]}).status == 201
        assert cls.admin.post("/users", {"username": "selfsvc", "role": "commenter", "tenant": "soc-games"}).status \
            == 201

    def resolve(self, client, number):
        return client.patch("/tickets/" + number, {"state": "resolved", "close_notes": "done"})

    def test_analyst_l1(self):
        l1 = self.client_for("l1")
        own = self.create_ticket(assignment_group="SOC L1")
        foreign = self.create_ticket(assignment_group="SOC L2")
        self.assertEqual(l1.get("/tickets/" + foreign["number"]).status, 200)  # reads all
        self.assertEqual(l1.patch("/tickets/" + own["number"], {"priority": 2}).status, 200)
        self.assertEqual(l1.patch("/tickets/" + foreign["number"], {"priority": 2}).status, 403)
        self.assertEqual(l1.post("/tickets/%s/comments" % foreign["number"], {"body": "x"}).status, 403)
        self.assertEqual(l1.post("/tickets/%s/comments" % own["number"],
                                 {"body": "x", "type": "work_note"}).status, 201)
        self.assertEqual(self.resolve(l1, own["number"]).status, 403)
        self.assertEqual(l1.patch("/tickets/" + own["number"], {"state": "cancelled"}).status, 403)
        self.assertEqual(l1.patch("/tickets/" + own["number"], {"state": "in_progress"}).status, 200)
        self.assertEqual(self.admin.get("/tickets/" + own["number"]).json["state"], "in_progress")
        self.assertEqual(self.create_ticket(l1)["caller"], "l1")
        self.assertEqual(l1.get("/reports/summary").status, 403)
        self.assertEqual(l1.get("/users").status, 200)
        for method, path in (("POST", "/users"), ("POST", "/teams"), ("POST", "/custom-fields"),
                             ("GET", "/webhooks"), ("GET", "/roles"), ("GET", "/tenants")):
            with self.subTest(path=path):
                self.assertEqual(l1.request(method, path, {} if method == "POST" else None).status, 403)

    def test_analyst_l2_and_soc_lead(self):
        for name in ("l2", "lead"):
            with self.subTest(user=name):
                client = self.client_for(name)
                foreign = self.create_ticket(assignment_group="SOC L2")
                self.assertEqual(client.patch("/tickets/" + foreign["number"], {"priority": 1}).status, 200)
                self.assertEqual(self.resolve(client, foreign["number"]).status, 200)
                self.assertEqual(client.patch("/tickets/" + foreign["number"], {"state": "closed"}).status, 200)
                self.assertEqual(client.post("/users", {"username": "nope-" + name, "role": "agent"}).status, 403)
        self.assertEqual(self.client_for("l2").get("/reports/summary").status, 403)
        self.assertEqual(self.client_for("lead").get("/reports/summary").status, 200)

    def test_read_only(self):
        ro = self.client_for("ro")
        t = self.create_ticket()
        self.admin.post("/tickets/%s/comments" % t["number"], {"body": "internal", "type": "work_note"})
        self.assertEqual(ro.get("/tickets/" + t["number"]).status, 200)
        self.assertIn("work_note", [e["type"] for e in ro.get("/tickets/%s/journal" % t["number"]).json])
        self.assertGreaterEqual(ro.get("/tickets").json["total"], 1)
        self.assertEqual(ro.post("/tickets/%s/comments" % t["number"], {"body": "x"}).status, 403)
        self.assertEqual(ro.patch("/tickets/" + t["number"], {"priority": 1}).status, 403)
        self.assertEqual(ro.post("/tickets", {"short_description": "x"}).status, 403)
        self.assertEqual(ro.get("/reports/summary").status, 403)

    def test_custom_role_without_users_manage(self):
        custom = self.client_for("custom")
        self.assertEqual(custom.post("/users", {"username": "by-custom", "role": "agent"}).status, 403)
        self.assertEqual(custom.get("/roles").status, 403)
        self.assertEqual(custom.get("/webhooks").status, 403)
        t = self.create_ticket(assignment_group="SOC L2")
        self.assertEqual(self.resolve(custom, t["number"]).status, 200)  # granted analyst permissions work

    def test_custom_self_service_role(self):
        selfsvc = self.client_for("selfsvc")
        other = self.create_ticket()
        mine = self.create_ticket(caller="selfsvc")
        self.assertEqual(selfsvc.get("/tickets/" + other["number"]).status, 403)
        self.assertEqual([t["number"] for t in selfsvc.get("/tickets").json["items"]], [mine["number"]])
        self.assertEqual(selfsvc.post("/tickets/%s/comments" % mine["number"], {"body": "hi"}).status, 201)
        self.assertEqual(selfsvc.post("/tickets", {"short_description": "x"}).status, 403)  # no tickets.create
        self.assertEqual(selfsvc.patch("/tickets/" + mine["number"], {"state": "cancelled"}).status, 403)

    def test_me_carries_the_role_label(self):
        # users without roles.manage cannot read /roles; the UI header shows /me.role_label
        self.assertEqual(self.admin.post("/roles", {"name": "labelled", "label": "Triage Desk",
                                                    "permissions": ["tickets.read_all"]}).status, 201)
        self.assertEqual(self.admin.post("/users", {"username": "labeluser", "role": "labelled",
                                                    "tenant": "soc-games"}).status, 201)
        me = self.client_for("labeluser").get("/me").json
        self.assertEqual((me["role"], me["role_label"]), ("labelled", "Triage Desk"))
        self.assertEqual(self.client_for("labeluser").get("/roles").status, 403)
        self.assertEqual(self.client_for("l1").get("/me").json["role_label"], "Analyst L1")
        self.assertEqual(self.client_for("custom").get("/me").json["role_label"], "no_users")
        self.assertEqual(self.admin.get("/me").json["role_label"], "Platform admin")

    def test_role_change_applies_immediately(self):
        self.admin.post("/users", {"username": "switcher", "role": "read_only", "tenant": "soc-games"})
        client = self.client_for("switcher")
        t = self.create_ticket()
        self.assertEqual(client.patch("/tickets/" + t["number"], {"priority": 1}).status, 403)
        self.assertEqual(self.admin.patch("/users/switcher", {"role": "analyst_l2"}).status, 200)
        self.assertEqual(client.patch("/tickets/" + t["number"], {"priority": 1}).status, 200)


if __name__ == "__main__":
    unittest.main()
