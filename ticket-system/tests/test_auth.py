"""Authentication, roles, sessions/CSRF, bootstrap and admin endpoints."""

import os
import stat
import tempfile
import unittest

from helpers import ADMIN_PASSWORD, Client, ServerTestCase

from mydemoticket.auth import CREDENTIALS_FILE, hash_password, verify_password
from mydemoticket.db import Database
from mydemoticket.server import App


class TokenAuthTest(ServerTestCase):
    def test_missing_token_401(self):
        resp = Client(self.port).get("/tickets")
        self.assertEqual(resp.status, 401)
        self.assertIn("error", resp.json)
        self.assertIn("www-authenticate", resp.headers)

    def test_invalid_token_401(self):
        self.assertEqual(Client(self.port, "nope").get("/me").status, 401)
        resp = Client(self.port).get("/me", headers={"Authorization": "Basic abc"})
        self.assertEqual(resp.status, 401)

    def test_me(self):
        resp = self.client_for("analyst1").get("/me")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json["username"], "analyst1")
        self.assertEqual(resp.json["role"], "agent")
        self.assertEqual(resp.json["teams"], ["SOC L1"])

    def test_token_lifecycle(self):
        created = self.admin.post("/users/analyst2/tokens", {"name": "ci"})
        self.assertEqual(created.status, 201)
        token = created.json["token"]
        self.assertTrue(token)
        listed = self.admin.get("/users/analyst2/tokens").json
        self.assertTrue(all("token" not in t for t in listed))
        self.assertEqual(Client(self.port, token).get("/me").json["username"], "analyst2")
        self.assertEqual(self.admin.delete("/users/analyst2/tokens/%d" % created.json["id"]).status, 204)
        self.assertEqual(Client(self.port, token).get("/me").status, 401)

    def test_tokens_stored_hashed(self):
        created = self.admin.post("/users/analyst1/tokens", {"name": "hash-check"}).json
        rows = self.app.db.query("SELECT token_hash FROM api_tokens")
        self.assertNotIn(created["token"], [r["token_hash"] for r in rows])
        users = self.app.db.query("SELECT password_hash FROM users WHERE password_hash IS NOT NULL")
        self.assertTrue(all(r["password_hash"].startswith("pbkdf2_sha256$") for r in users))

    def test_deactivated_user_401(self):
        self.admin.post("/users", {"username": "temp1", "role": "agent"})
        token = self.admin.post("/users/temp1/tokens", {}).json["token"]
        self.assertEqual(Client(self.port, token).get("/me").status, 200)
        self.admin.patch("/users/temp1", {"active": False})
        self.assertEqual(Client(self.port, token).get("/me").status, 401)


class RoleTest(ServerTestCase):
    def setUp(self):
        self.analyst1 = self.client_for("analyst1")  # agent, SOC L1
        self.analyst2 = self.client_for("analyst2")  # agent, SOC L2
        self.requester = self.client_for("requester1")
        self.soar = self.client_for("soar")

    def test_agent_can_modify_own_team_assigned_or_unassigned(self):
        own_team = self.create_ticket(assignment_group="SOC L1")
        assigned = self.create_ticket(assignment_group="SOC L2", assigned_to="analyst1")
        unassigned = self.create_ticket()
        for t in (own_team, assigned, unassigned):
            with self.subTest(ticket=t["number"]):
                self.assertEqual(self.analyst1.patch("/tickets/" + t["number"], {"priority": 2}).status, 200)

    def test_agent_forbidden_on_other_team(self):
        other = self.create_ticket(assignment_group="SOC L2")
        self.assertEqual(self.analyst1.get("/tickets/" + other["number"]).status, 200)
        self.assertEqual(self.analyst1.patch("/tickets/" + other["number"], {"priority": 1}).status, 403)
        self.assertEqual(
            self.analyst1.post("/tickets/%s/comments" % other["number"], {"body": "x"}).status, 403
        )
        self.assertEqual(self.analyst2.patch("/tickets/" + other["number"], {"priority": 1}).status, 200)

    def test_agent_forbidden_on_admin_endpoints(self):
        for method, path in [("POST", "/users"), ("POST", "/teams"), ("POST", "/custom-fields"),
                             ("GET", "/webhooks"), ("POST", "/webhooks"), ("POST", "/users/analyst1/tokens"),
                             ("PATCH", "/users/analyst1"), ("DELETE", "/custom-fields/risk_score")]:
            with self.subTest(method=method, path=path):
                body = {} if method in ("POST", "PATCH") else None
                self.assertEqual(self.analyst1.request(method, path, body).status, 403)

    def test_integration_can_modify_all(self):
        t = self.create_ticket(assignment_group="IT Ops", assigned_to="analyst2")
        self.assertEqual(self.soar.patch("/tickets/" + t["number"], {"state": "in_progress"}).status, 200)
        self.assertEqual(self.soar.post("/tickets/%s/comments" % t["number"],
                                        {"body": "enriched", "type": "work_note"}).status, 201)
        self.assertEqual(self.soar.get("/webhooks").status, 403)
        self.assertEqual(self.soar.post("/users", {}).status, 403)

    def test_requester_own_tickets_only(self):
        mine = self.create_ticket(client=self.requester, short_description="My laptop is slow")
        self.assertEqual(mine["caller"], "requester1")
        others = self.create_ticket()
        self.assertEqual(self.requester.get("/tickets/" + mine["number"]).status, 200)
        self.assertEqual(self.requester.get("/tickets/" + others["number"]).status, 403)
        self.assertEqual(self.requester.get("/tickets/%s/journal" % others["number"]).status, 403)
        self.assertEqual(self.requester.post("/tickets/%s/comments" % others["number"], {"body": "x"}).status, 403)
        listed = self.requester.get("/tickets?limit=500").json
        self.assertTrue(all(i["caller"] == "requester1" for i in listed["items"]))
        self.assertEqual(listed["total"], len(listed["items"]))

    def test_requester_create_restrictions(self):
        resp = self.requester.post("/tickets", {"short_description": "x", "assignment_group": "SOC L1"})
        self.assertEqual(resp.status, 403)
        resp = self.requester.post("/tickets", {"short_description": "x", "caller": "analyst1"})
        self.assertEqual(resp.status, 403)

    def test_requester_state_changes(self):
        mine = self.create_ticket(client=self.requester)
        path = "/tickets/" + mine["number"]
        self.assertEqual(self.requester.patch(path, {"state": "in_progress"}).status, 403)
        self.assertEqual(self.requester.patch(path, {"priority": 1}).status, 403)
        resp = self.requester.patch(path, {"state": "cancelled"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json["state"], "cancelled")
        others = self.create_ticket()
        self.assertEqual(self.requester.patch("/tickets/" + others["number"], {"state": "cancelled"}).status, 403)

    def test_requester_cannot_see_work_notes(self):
        mine = self.create_ticket(client=self.requester)
        path = "/tickets/%s/comments" % mine["number"]
        self.assertEqual(self.analyst1.post(path, {"body": "visible reply", "type": "comment"}).status, 201)
        self.assertEqual(self.analyst1.post(path, {"body": "internal only", "type": "work_note"}).status, 201)
        self.assertEqual(self.requester.post(path, {"body": "thanks", "type": "comment"}).status, 201)
        self.assertEqual(self.requester.post(path, {"body": "sneaky", "type": "work_note"}).status, 403)
        journal = self.requester.get("/tickets/%s/journal" % mine["number"]).json
        bodies = [e.get("body") for e in journal]
        self.assertIn("visible reply", bodies)
        self.assertIn("thanks", bodies)
        self.assertNotIn("internal only", bodies)
        self.assertFalse([e for e in journal if e["type"] == "work_note"])
        agent_view = self.analyst1.get("/tickets/%s/journal" % mine["number"]).json
        self.assertIn("internal only", [e.get("body") for e in agent_view])

    def test_requester_admin_and_directory_403(self):
        for path in ("/users", "/teams", "/webhooks"):
            with self.subTest(path=path):
                self.assertEqual(self.requester.get(path).status, 403)
        self.assertEqual(self.requester.get("/custom-fields").status, 200)


class AdminEndpointTest(ServerTestCase):
    def test_users_and_teams(self):
        resp = self.admin.post("/users", {"username": "newagent", "role": "agent", "display_name": "New Agent",
                                          "password": "longenough1", "teams": ["IT Ops"]})
        self.assertEqual(resp.status, 201, resp)
        self.assertEqual(resp.json["teams"], ["IT Ops"])
        self.assertNotIn("password", resp.json)
        self.assertEqual(self.admin.post("/users", {"username": "newagent", "role": "agent"}).status, 409)
        self.assertEqual(self.admin.post("/users", {"username": "bad user", "role": "agent"}).status, 422)
        self.assertEqual(self.admin.post("/users", {"username": "x2", "role": "god"}).status, 422)
        self.assertEqual(self.admin.post("/users", {"username": "x3", "role": "agent", "password": "short"}).status,
                         422)
        resp = self.admin.post("/teams", {"name": "CSIRT", "description": "Incident team"})
        self.assertEqual(resp.status, 201)
        resp = self.admin.post("/teams/CSIRT/members", {"username": "newagent"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json["members"], ["newagent"])
        self.assertEqual(self.admin.post("/teams/CSIRT/members", {"username": "ghost"}).status, 422)
        self.assertEqual(self.admin.post("/teams/Nope/members", {"username": "newagent"}).status, 404)
        teams = {t["name"]: t for t in self.admin.get("/teams").json}
        self.assertEqual(set(teams) >= {"SOC L1", "SOC L2", "IT Ops", "CSIRT"}, True)
        self.assertIn("analyst1", teams["SOC L1"]["members"])
        resp = self.admin.patch("/users/newagent", {"role": "integration", "teams": []})
        self.assertEqual(resp.json["role"], "integration")
        self.assertEqual(resp.json["teams"], [])
        self.assertEqual(self.admin.patch("/users/ghost", {"role": "agent"}).status, 404)

    def test_seed_users(self):
        users = {u["username"]: u for u in self.admin.get("/users").json}
        self.assertEqual(users["admin"]["role"], "admin")
        self.assertEqual(users["analyst1"]["role"], "agent")
        self.assertEqual(users["analyst2"]["teams"], ["SOC L2"])
        self.assertEqual(users["requester1"]["role"], "requester")
        self.assertEqual(users["soar"]["role"], "integration")

    def test_last_admin_protected(self):
        self.assertEqual(self.admin.patch("/users/admin", {"role": "agent"}).status, 409)
        self.assertEqual(self.admin.patch("/users/admin", {"active": False}).status, 409)

    def test_webhook_validation(self):
        self.assertEqual(self.admin.post("/webhooks", {"type": "generic", "url": "ftp://x"}).status, 422)
        self.assertEqual(self.admin.post("/webhooks", {"type": "smtp", "url": "http://x"}).status, 422)
        self.assertEqual(self.admin.post("/webhooks", {"url": "http://x", "events": ["ticket.deleted"]}).status, 422)
        self.assertEqual(self.admin.post("/webhooks", {"type": "soar", "url": "https://soar.example"}).status, 422)
        resp = self.admin.post("/webhooks", {"url": "http://127.0.0.1:9/hook", "secret": "s3cr3t", "active": False})
        self.assertEqual(resp.status, 201)
        self.assertNotIn("secret", resp.json)
        self.assertTrue(resp.json["has_secret"])
        self.assertEqual(resp.json["events"], sorted(["ticket.created", "ticket.updated",
                                                      "ticket.state_changed", "ticket.commented"]))
        self.assertEqual(self.admin.delete("/webhooks/%d" % resp.json["id"]).status, 204)
        self.assertEqual(self.admin.delete("/webhooks/%d" % resp.json["id"]).status, 404)

    def test_unknown_route_and_method(self):
        self.assertEqual(self.admin.get("/nope").status, 404)
        self.assertEqual(self.admin.request("PUT", "/tickets", {}).status, 405)


class SessionTest(ServerTestCase):
    def test_login_sets_strict_httponly_cookie(self):
        client = Client(self.port)
        resp = client.post("/auth/login", {"username": "admin", "password": ADMIN_PASSWORD})
        self.assertEqual(resp.status, 200, resp)
        cookie = resp.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertTrue(resp.json["csrf_token"])
        self.assertEqual(client.get("/me").json["username"], "admin")

    def test_wrong_password(self):
        resp = Client(self.port).post("/auth/login", {"username": "admin", "password": "wrong"})
        self.assertEqual(resp.status, 401)
        resp = Client(self.port).post("/auth/login", {"username": "ghost", "password": "wrong"})
        self.assertEqual(resp.status, 401)

    def test_csrf_required_for_writes(self):
        client = Client(self.port)
        login = client.post("/auth/login", {"username": "admin", "password": ADMIN_PASSWORD})
        resp = client.post("/tickets", {"short_description": "from ui"})
        self.assertEqual(resp.status, 403)
        self.assertIn("CSRF", resp.json["error"])
        resp = client.post("/tickets", {"short_description": "from ui"}, headers={"X-CSRF-Token": "forged"})
        self.assertEqual(resp.status, 403)
        client.csrf = login.json["csrf_token"]
        resp = client.post("/tickets", {"short_description": "from ui"})
        self.assertEqual(resp.status, 201)
        journal = client.get("/tickets/%s/journal" % resp.json["number"]).json
        self.assertEqual(journal[0]["source"], "web")
        self.assertEqual(client.get("/me").json["csrf_token"], client.csrf)

    def test_logout(self):
        client = Client(self.port)
        login = client.post("/auth/login", {"username": "admin", "password": ADMIN_PASSWORD})
        client.csrf = login.json["csrf_token"]
        session = client.cookies.get("mdt_session")
        self.assertEqual(client.post("/auth/logout").status, 200)
        stale = Client(self.port)
        stale.cookies["mdt_session"] = session
        self.assertEqual(stale.get("/me").status, 401)

    def test_static_ui(self):
        resp = Client(self.port).get("/")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"MyDemoTicket", resp.raw)
        self.assertIn("default-src 'self'", resp.headers["content-security-policy"])
        for name in ("app.js", "app.css"):
            self.assertEqual(Client(self.port).get("/static/" + name).status, 200)
        self.assertEqual(Client(self.port).get("/static/../server.py").status, 404)


class BootstrapTest(unittest.TestCase):
    def test_generated_credentials_file(self):
        saved = {k: os.environ.pop(k, None) for k in ("MYDEMOTICKET_ADMIN_PASSWORD", "MYDEMOTICKET_ADMIN_TOKEN")}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                app = App(tmp, seed=True, quiet=True)
                try:
                    path = os.path.join(tmp, CREDENTIALS_FILE)
                    self.assertEqual(app.credentials_file, path)
                    self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
                    with open(path, encoding="utf-8") as handle:
                        content = handle.read()
                    for name in ("admin password", "admin API token", "analyst1 password",
                                 "analyst2 password", "requester1 password", "soar password", "soar API token"):
                        self.assertIn(name, content)
                finally:
                    app.shutdown()
                # second start: no new users, no new credentials, migrations idempotent
                app2 = App(tmp, seed=True, quiet=True)
                try:
                    self.assertIsNone(app2.credentials_file)
                    count = app2.db.query_one("SELECT COUNT(*) AS n FROM users")["n"]
                    self.assertEqual(count, 5)
                finally:
                    app2.shutdown()
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value

    def test_env_credentials_and_no_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MYDEMOTICKET_ADMIN_PASSWORD"] = "env-password-123"
            os.environ["MYDEMOTICKET_ADMIN_TOKEN"] = "env-token-123"
            try:
                app = App(tmp, seed=False, quiet=True)
            finally:
                del os.environ["MYDEMOTICKET_ADMIN_PASSWORD"]
                del os.environ["MYDEMOTICKET_ADMIN_TOKEN"]
            try:
                self.assertIsNone(app.credentials_file)
                self.assertFalse(os.path.exists(os.path.join(tmp, CREDENTIALS_FILE)))
                self.assertEqual(app.db.query_one("SELECT COUNT(*) AS n FROM users")["n"], 1)
                self.assertEqual(app.db.query_one("SELECT COUNT(*) AS n FROM teams")["n"], 0)
                row = app.db.query_one("SELECT password_hash FROM users WHERE username = 'admin'")
                self.assertTrue(verify_password("env-password-123", row["password_hash"]))
            finally:
                app.shutdown()

    def test_migrations_idempotent(self):
        db = Database(":memory:")
        version = db.schema_version()
        db.migrate()
        db.migrate()
        self.assertEqual(db.schema_version(), version)
        db.close()

    def test_password_hash_roundtrip(self):
        stored = hash_password("correct horse", iterations=1000)
        self.assertTrue(verify_password("correct horse", stored))
        self.assertFalse(verify_password("wrong", stored))
        self.assertFalse(verify_password("x", "garbage"))


if __name__ == "__main__":
    unittest.main()
