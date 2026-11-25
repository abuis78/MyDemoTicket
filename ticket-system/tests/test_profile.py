"""Own profile (GET/PATCH /me) and own password change (POST /me/password)."""

import unittest

from helpers import ADMIN_PASSWORD, Client, ServerTestCase

PASSWORD = "profile-pass-1"


def login(port, username, password):
    client = Client(port)
    resp = client.post("/auth/login", {"username": username, "password": password})
    assert resp.status == 200, resp
    client.csrf = resp.json["csrf_token"]
    return client


class ProfileTest(ServerTestCase):
    use_recorder = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for name in ("prof1", "prof2", "pwuser", "pwshort"):
            resp = cls.admin.post("/users", {"username": name, "role": "agent", "password": PASSWORD,
                                             "tenant": "soc-games", "email": name + "@example.invalid"})
            assert resp.status == 201, resp

    def test_me_has_profile_fields_and_permissions(self):
        me = login(self.port, "prof1", PASSWORD).get("/me").json
        self.assertEqual((me["email"], me["timezone"], me["theme"], me["language"]),
                         ("prof1@example.invalid", "", "system", "de"))
        self.assertEqual(me["permissions"], sorted(["tickets.read_all", "tickets.create", "tickets.update_own",
                                                    "tickets.comment", "tickets.work_note", "tickets.close",
                                                    "reports.view"]))
        admin_me = self.admin.get("/me").json
        self.assertIn("splunk.manage", admin_me["permissions"])
        self.assertIn("tenants.manage", admin_me["permissions"])

    def test_patch_me_updates_and_returns_me_shape(self):
        client = login(self.port, "prof1", PASSWORD)
        resp = client.patch("/me", {"display_name": "  Profile One ", "email": "p1@example.invalid",
                                    "timezone": "Europe/Berlin", "theme": "dark", "language": "en"})
        self.assertEqual(resp.status, 200, resp)
        self.assertEqual((resp.json["display_name"], resp.json["email"], resp.json["timezone"],
                          resp.json["theme"], resp.json["language"]),
                         ("Profile One", "p1@example.invalid", "Europe/Berlin", "dark", "en"))
        self.assertEqual(set(resp.json), set(client.get("/me").json))
        self.assertIn("csrf_token", resp.json)
        self.assertEqual(client.get("/me").json["language"], "en")
        # admin sees the changed display name in the user list
        users = {u["username"]: u for u in self.admin.get("/users").json}
        self.assertEqual(users["prof1"]["display_name"], "Profile One")
        event = self.audit.find("profile.update", actor="prof1")[-1]
        self.assertEqual(event["details"]["changed"], ["display_name", "email", "language", "theme", "timezone"])
        # timezone can be reset to "" (= browser)
        self.assertEqual(client.patch("/me", {"timezone": ""}).json["timezone"], "")

    def test_patch_me_validation(self):
        client = self.client_for("prof2")
        before = client.get("/me").json
        for body in ({"timezone": "Mars/Olympus"}, {"timezone": "../etc/passwd"}, {"timezone": 5},
                     {"theme": "blue"}, {"language": "fr"}, {"display_name": "   "}, {"display_name": None},
                     {"email": "not-an-email"}, {"role": "admin"}, {"username": "x"}, {"password": "12345678"},
                     {"display_name": "x" * 201}):
            with self.subTest(body=body):
                resp = client.patch("/me", body)
                self.assertEqual(resp.status, 422, resp)
        self.assertEqual(client.patch("/me", ["x"]).status, 400)
        self.assertEqual(client.get("/me").json, before)
        self.assertEqual(client.patch("/me", {"email": ""}).json["email"], "")
        self.assertEqual(client.patch("/me", {"timezone": "UTC"}).json["timezone"], "UTC")

    def test_patch_me_requires_csrf_for_sessions(self):
        client = login(self.port, "prof2", PASSWORD)
        client.csrf = None
        self.assertEqual(client.patch("/me", {"theme": "light"}).status, 403)

    def test_requester_and_integration_can_edit_profile(self):
        for name in ("requester1", "soar"):
            with self.subTest(user=name):
                resp = self.client_for(name).patch("/me", {"language": "en"})
                self.assertEqual((resp.status, resp.json["language"]), (200, "en"))

    def test_own_password_change_keeps_current_session_ends_others(self):
        current = login(self.port, "pwuser", PASSWORD)
        other = login(self.port, "pwuser", PASSWORD)
        resp = current.post("/me/password", {"current_password": PASSWORD, "new_password": "new-pass-123"})
        self.assertEqual(resp.status, 200, resp)
        self.assertEqual(resp.json, {"ok": True})
        self.assertEqual(current.get("/me").status, 200)
        self.assertEqual(current.patch("/me", {"theme": "light"}).status, 200)  # CSRF token still valid
        self.assertEqual(other.get("/me").status, 401)
        self.assertEqual(Client(self.port).post("/auth/login", {"username": "pwuser",
                                                                "password": PASSWORD}).status, 401)
        login(self.port, "pwuser", "new-pass-123")
        event = self.audit.find("profile.password_change", actor="pwuser", outcome="success")[-1]
        self.assertEqual(event["details"], {"other_sessions_ended": 1})

    def test_own_password_change_errors(self):
        client = login(self.port, "pwshort", PASSWORD)
        resp = client.post("/me/password", {"current_password": "wrong-one", "new_password": "new-pass-123"})
        self.assertEqual(resp.status, 403, resp)
        self.assertEqual(len(self.audit.find("profile.password_change", actor="pwshort", outcome="failure")), 1)
        self.assertEqual(client.post("/me/password", {"current_password": PASSWORD,
                                                      "new_password": "short"}).status, 422)
        self.assertEqual(client.post("/me/password", {"new_password": "new-pass-123"}).status, 422)
        self.assertEqual(client.post("/me/password", {"current_password": PASSWORD, "new_password": "x" * 8,
                                                      "extra": 1}).status, 422)
        self.assertEqual(client.get("/me").status, 200)  # failures do not end the session
        login(self.port, "pwshort", PASSWORD)  # password unchanged

    def test_token_only_user_has_no_password(self):
        self.admin.post("/users", {"username": "tokenonly", "role": "integration", "tenant": "soc-games"})
        resp = self.client_for("tokenonly").post("/me/password", {"current_password": "x",
                                                                  "new_password": "y" * 8})
        self.assertEqual(resp.status, 422, resp)
        self.assertEqual(resp.json["error"], "no password set")

    def test_no_password_in_audit(self):
        client = login(self.port, "prof2", PASSWORD)
        client.post("/me/password", {"current_password": "wrong-secret-xyz", "new_password": "never-logged-1"})
        self.assertNotIn("never-logged-1", repr(self.audit.events))
        self.assertNotIn("wrong-secret-xyz", repr(self.audit.events))


class AdminOwnPasswordTest(ServerTestCase):
    """Regression: an admin setting the own password via PATCH /users/<self> lost the session."""

    def test_admin_sets_own_password_then_creates_user(self):
        admin = login(self.port, "admin", ADMIN_PASSWORD)
        other = login(self.port, "admin", ADMIN_PASSWORD)
        resp = admin.patch("/users/admin", {"password": "brand-new-admin-pw"})
        self.assertEqual(resp.status, 200, resp)
        created = admin.post("/users", {"username": "after-pw", "role": "agent", "tenant": "soc-games",
                                        "password": "after-pw-pass"})
        self.assertEqual(created.status, 201, created)
        self.assertEqual(other.get("/me").status, 401)  # the other session of the admin is gone
        login(self.port, "admin", "brand-new-admin-pw")

    def test_password_of_other_user_ends_all_their_sessions(self):
        self.admin.post("/users", {"username": "victim", "role": "agent", "tenant": "soc-games",
                                   "password": PASSWORD})
        self.admin.post("/users", {"username": "tenant-admin", "role": "admin", "tenant": "soc-games",
                                   "password": PASSWORD})
        victim = login(self.port, "victim", PASSWORD)
        admin = login(self.port, "tenant-admin", PASSWORD)
        self.assertEqual(admin.patch("/users/victim", {"password": "reset-by-admin"}).status, 200)
        self.assertEqual(victim.get("/me").status, 401)
        self.assertEqual(admin.get("/me").status, 200)


if __name__ == "__main__":
    unittest.main()
