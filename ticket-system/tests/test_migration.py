"""Migration of a live schema-v1 database (main before multi-tenancy) into tenants.

The v1 schema is a frozen copy in tests/fixtures/schema_v1.sql. The test fills it
with data shaped like the production instance, starts the app (which migrates),
and checks that nothing is lost, everything belongs to the default tenant, the
bootstrap admin became platform_admin, credentials keep working and a second
start changes nothing.
"""

import os
import shutil
import sqlite3
import stat
import tempfile
import threading
import unittest

from helpers import AuditRecorder, Client

from mydemoticket.auth import hash_password, hash_token
from mydemoticket.server import App

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "schema_v1.sql")
DB_NAME = "mydemoticket.sqlite3"
TS = "2026-11-20T10:00:00Z"

PASSWORDS = {
    "admin": "live-admin-password",
    "analyst1": "live-analyst1-password",
    "analyst2": "live-analyst2-password",
    "requester1": "live-requester1-password",
    "soar": "live-soar-password",
}
TOKENS = {"admin": "mdt_live_admin_token", "soar": "mdt_live_soar_token"}
SESSION = "live-session-analyst1"
TABLES = ("meta", "users", "api_tokens", "sessions", "teams", "team_members", "tickets", "journal",
          "custom_field_defs", "webhooks", "webhook_deliveries")


def build_v1_database(path):
    conn = sqlite3.connect(path)
    with open(FIXTURE, encoding="utf-8") as handle:
        conn.executescript(handle.read())
    conn.execute("PRAGMA user_version = 1")
    users = [("admin", "Administrator", "admin"), ("analyst1", "Analyst One", "agent"),
             ("analyst2", "Analyst Two", "agent"), ("requester1", "Requester One", "requester"),
             ("soar", "Splunk SOAR Integration", "integration")]
    for i, (name, display, role) in enumerate(users, start=1):
        conn.execute(
            "INSERT INTO users(id, username, display_name, email, role, password_hash, active, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, 1, ?)",
            (i, name, display, "%s@example.invalid" % name, role, hash_password(PASSWORDS[name], 1000), TS))
    conn.execute("INSERT INTO api_tokens(user_id, name, token_hash, created_at) VALUES(1, 'initial', ?, ?)",
                 (hash_token(TOKENS["admin"]), TS))
    conn.execute("INSERT INTO api_tokens(user_id, name, token_hash, created_at) VALUES(5, 'soar-prod', ?, ?)",
                 (hash_token(TOKENS["soar"]), TS))
    conn.execute("INSERT INTO sessions VALUES(?, 2, 'csrf-live', ?, '2999-01-01T00:00:00Z')",
                 (hash_token(SESSION), TS))
    for i, (name, desc) in enumerate([("SOC L1", "L1"), ("SOC L2", "L2"), ("IT Ops", "Ops")], start=1):
        conn.execute("INSERT INTO teams(id, name, description) VALUES(?, ?, ?)", (i, name, desc))
    conn.executemany("INSERT INTO team_members VALUES(?, ?)", [(1, 2), (2, 3)])
    for name, label, ftype, choices in [("mitre_technique", "MITRE", "string", "[]"),
                                        ("affected_host", "Host", "string", "[]"),
                                        ("risk_score", "Risk", "number", "[]"),
                                        ("incident_type", "Type", "choice", '["malware", "phishing"]')]:
        conn.execute("INSERT INTO custom_field_defs VALUES(?, ?, ?, ?, 0, ?)", (name, label, ftype, choices, TS))
    tickets = [
        (1, "TKT0001001", "Suspicious login", "in_progress", 2, "SOC L1", "analyst1", "soar",
         '{"risk_score": 80}', '{"soar_container_id": 12}', None),
        (2, "TKT0001002", "Phishing mail", "resolved", 3, "SOC L2", None, "requester1", "{}", "{}", TS),
        (3, "TKT0001003", "Malware alert", "new", 1, None, None, "admin", '{"incident_type": "malware"}', "{}",
         None),
    ]
    for tid, number, short, state, prio, group, assignee, caller, custom, refs, resolved in tickets:
        conn.execute(
            "INSERT INTO tickets(id, number, short_description, description, state, priority, assignment_group, "
            "assigned_to, caller, category, custom_fields, external_refs, created_at, updated_at, resolved_at, "
            "close_notes) VALUES(?, ?, ?, '', ?, ?, ?, ?, ?, 'security', ?, ?, ?, ?, ?, ?)",
            (tid, number, short, state, prio, group, assignee, caller, custom, refs, TS, TS, resolved,
             "done" if resolved else ""))
        conn.execute("INSERT INTO journal(ticket_id, type, field, old_value, new_value, actor, source, at) "
                     "VALUES(?, 'audit', 'created', 'null', ?, 'soar', 'soar', ?)", (tid, '"%s"' % number, TS))
    conn.execute("INSERT INTO journal(ticket_id, type, body, actor, source, at) "
                 "VALUES(1, 'work_note', 'VT 0/70', 'soar', 'soar', ?)", (TS,))
    conn.execute("INSERT INTO journal(ticket_id, type, body, actor, source, at) "
                 "VALUES(2, 'comment', 'Thanks', 'requester1', 'web', ?)", (TS,))
    conn.execute("INSERT INTO meta VALUES('ticket_counter', '1003')")
    conn.execute("INSERT INTO webhooks(id, name, type, url, secret, events, active, soar_label, verify_tls, "
                 "created_at) VALUES(1, 'SOAR', 'soar', 'https://soar.example.invalid', 'live-ph-auth-secret', "
                 "'[\"ticket.created\"]', 1, 'mydemoticket', 1, ?)", (TS,))
    conn.execute("INSERT INTO webhook_deliveries(webhook_id, delivery_id, event, ticket_number, status, "
                 "response_code, attempts, error, created_at, updated_at) "
                 "VALUES(1, 'd-1', 'ticket.created', 'TKT0001003', 'success', 200, 1, '', ?, ?)", (TS, TS))
    conn.commit()
    conn.close()


def dump(path):
    conn = sqlite3.connect(path)
    try:
        data = {}
        for table in ("tenants",) + TABLES:
            rows = conn.execute("SELECT * FROM %s" % table).fetchall()
            data[table] = sorted(rows, key=repr)
        data["schema"] = sorted(conn.execute("SELECT name, sql FROM sqlite_master").fetchall(), key=repr)
        data["user_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
        return data
    finally:
        conn.close()


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mdt-migrate-")
        self.data_dir = os.path.join(self.tmp, "data")
        os.makedirs(self.data_dir, mode=0o700)
        self.db_path = os.path.join(self.data_dir, DB_NAME)
        build_v1_database(self.db_path)
        conn = sqlite3.connect(self.db_path)
        self.before = {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in TABLES}
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self):
        return App(self.data_dir, seed=True, quiet=True, audit_logger=AuditRecorder())

    def test_migrates_live_data_into_default_tenant(self):
        app = self.start()
        try:
            self.assertIsNone(app.credentials_file)  # no bootstrap on a populated database
            db = app.db
            self.assertEqual(db.schema_version(), 4)
            for table, count in self.before.items():
                with self.subTest(table=table):
                    self.assertEqual(db.query_one("SELECT COUNT(*) AS n FROM %s" % table)["n"], count)
            tenants = db.query("SELECT * FROM tenants")
            self.assertEqual(len(tenants), 1)
            t = tenants[0]
            self.assertEqual((t["slug"], t["name"], t["active"], t["soar_url"], t["splunk_index"]),
                             ("soc-games", "SOC Games", 1, "https://soar.soc-games.com", None))
            tid = t["id"]
            users = {r["username"]: r for r in db.query("SELECT * FROM users")}
            self.assertEqual(users["admin"]["role"], "platform_admin")
            self.assertIsNone(users["admin"]["tenant_id"])
            for name in ("analyst1", "analyst2", "requester1", "soar"):
                self.assertEqual(users[name]["tenant_id"], tid, name)
            self.assertEqual(users["soar"]["role"], "integration")
            for table in ("tickets", "journal", "teams", "custom_field_defs", "webhooks", "webhook_deliveries"):
                with self.subTest(table=table):
                    rows = db.query("SELECT DISTINCT tenant_id FROM %s" % table)
                    self.assertEqual([r["tenant_id"] for r in rows], [tid])
            self.assertEqual(sorted(r["name"] for r in db.query("SELECT name FROM teams")),
                             ["IT Ops", "SOC L1", "SOC L2"])
            self.assertEqual(db.query("SELECT team_id, user_id FROM team_members ORDER BY team_id").__len__(), 2)
            self.assertEqual(db.query("PRAGMA foreign_key_check"), [])
            self.assertEqual(db.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            hook = db.query_one("SELECT secret FROM webhooks WHERE id = 1")
            self.assertEqual(hook["secret"], "live-ph-auth-secret")
            backup = self.db_path + ".pre-v2.bak"
            self.assertTrue(os.path.exists(backup))
            self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o600)
            conn = sqlite3.connect(backup)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 3)
            conn.close()
        finally:
            app.shutdown()

    def test_second_start_changes_nothing(self):
        self.start().shutdown()
        first = dump(self.db_path)
        backup_mtime = os.stat(self.db_path + ".pre-v2.bak").st_mtime
        self.start().shutdown()
        self.assertEqual(dump(self.db_path), first)
        self.assertEqual(os.stat(self.db_path + ".pre-v2.bak").st_mtime, backup_mtime)

    def test_failed_migration_rolls_back(self):
        # a broken foreign key in the live data must abort the migration without changes
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO team_members VALUES(99, 2)")
        conn.commit()
        conn.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.start()
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
            self.assertNotIn("tenants", names)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0], 3)
        finally:
            conn.close()

    def test_credentials_keep_working(self):
        app = self.start()
        httpd = app.make_server("127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        port = httpd.server_address[1]
        try:
            for name in ("admin", "analyst1", "requester1", "soar"):
                with self.subTest(login=name):
                    resp = Client(port).post("/auth/login", {"username": name, "password": PASSWORDS[name]})
                    self.assertEqual(resp.status, 200, resp)
                    self.assertEqual(resp.json["tenant"]["slug"], "soc-games")
            soar = Client(port, TOKENS["soar"])
            me = soar.get("/me").json
            self.assertEqual((me["username"], me["role"], me["tenant"]["slug"]), ("soar", "integration", "soc-games"))
            listed = soar.get("/tickets?sort=number").json
            self.assertEqual([t["number"] for t in listed["items"]], ["TKT0001001", "TKT0001002", "TKT0001003"])
            journal = soar.get("/tickets/TKT0001001/journal").json
            self.assertEqual([e["type"] for e in journal], ["audit", "work_note"])
            admin = Client(port, TOKENS["admin"])
            me = admin.get("/me").json
            self.assertEqual(me["role"], "platform_admin")
            self.assertEqual(me["active_tenant"]["slug"], "soc-games")
            self.assertEqual([h["name"] for h in admin.get("/webhooks").json], ["SOAR"])
            self.assertEqual(admin.get("/webhooks/1/deliveries").json[0]["delivery_id"], "d-1")
            self.assertEqual(sorted(f["name"] for f in admin.get("/custom-fields").json),
                             ["affected_host", "incident_type", "mitre_technique", "risk_score"])
            created = soar.post("/tickets", {"short_description": "after migration", "assignment_group": "SOC L1",
                                             "custom_fields": {"risk_score": 5}})
            self.assertEqual(created.status, 201, created)
            self.assertEqual(created.json["number"], "TKT0001004")  # global sequence continues
            session = Client(port)
            session.cookies["mdt_session"] = SESSION
            self.assertEqual(session.get("/me").json["username"], "analyst1")
        finally:
            app.shutdown()


if __name__ == "__main__":
    unittest.main()
