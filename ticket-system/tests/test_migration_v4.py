"""Migration of a live schema-v3 database to v4 (profile, custom roles, settings).

The v3 schema is a frozen copy in tests/fixtures/schema_v3.sql. The test fills it
with data shaped like the production instance, starts the app (which migrates),
and checks that nothing is lost, existing users get the profile defaults, the new
tables exist and are empty, a backup of the v3 state is written, a second start
changes nothing and a failing step rolls back completely.
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

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "schema_v3.sql")
DB_NAME = "mydemoticket.sqlite3"
TS = "2026-11-24T10:00:00Z"
SOAR_TOKEN = "mdt_live_v3_soar_token"
TABLES = ("tenants", "meta", "users", "api_tokens", "sessions", "teams", "team_members", "tickets",
          "journal", "custom_field_defs", "webhooks", "webhook_deliveries")


def build_v3_database(path):
    conn = sqlite3.connect(path)
    with open(FIXTURE, encoding="utf-8") as handle:
        conn.executescript(handle.read())
    conn.execute("PRAGMA user_version = 3")
    conn.execute("INSERT INTO tenants(id, slug, name, active, soar_url, splunk_index, created_at, updated_at) "
                 "VALUES(1, 'soc-games', 'SOC Games', 1, 'https://soar.soc-games.com', NULL, ?, ?)", (TS, TS))
    conn.execute("INSERT INTO tenants(id, slug, name, active, soar_url, splunk_index, created_at, updated_at) "
                 "VALUES(2, 'acme', 'ACME', 1, NULL, 'acme_audit', ?, ?)", (TS, TS))
    users = [(1, "admin", "platform_admin", None), (2, "analyst1", "agent", 1), (3, "soar", "integration", 1),
             (4, "acme-admin", "admin", 2), (5, "requester1", "requester", 1)]
    for uid, name, role, tenant in users:
        conn.execute(
            "INSERT INTO users(id, username, display_name, email, role, password_hash, active, created_at, "
            "tenant_id) VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (uid, name, name, name + "@example.invalid", role, hash_password("pw-" + name, 1000), TS, tenant))
    conn.execute("INSERT INTO api_tokens(user_id, name, token_hash, created_at) VALUES(3, 'soar-prod', ?, ?)",
                 (hash_token(SOAR_TOKEN), TS))
    conn.execute("INSERT INTO sessions(session_hash, user_id, csrf_token, created_at, expires_at) "
                 "VALUES('h1', 2, 'csrf', ?, '2099-01-01T00:00:00Z')", (TS,))
    conn.execute("INSERT INTO teams(id, tenant_id, name, description) VALUES(1, 1, 'SOC L1', '')")
    conn.execute("INSERT INTO team_members VALUES(1, 2)")
    conn.execute("INSERT INTO tickets(id, number, short_description, description, state, priority, "
                 "assignment_group, assigned_to, caller, category, custom_fields, external_refs, created_at, "
                 "updated_at, resolved_at, close_notes, tenant_id) VALUES(1, 'TKT0001001', 'live', '', 'new', 3, "
                 "'SOC L1', NULL, 'soar', '', '{}', '{\"es_finding_id\": 1133}', ?, ?, NULL, '', 1)", (TS, TS))
    conn.execute("INSERT INTO journal(ticket_id, type, body, actor, author, source, at, tenant_id) "
                 "VALUES(1, 'comment', 'from ES', 'soar', 'jdoe', 'soar', ?, 1)", (TS,))
    conn.execute("INSERT INTO webhooks(id, name, type, url, secret, events, active, soar_label, verify_tls, "
                 "created_at, tenant_id) VALUES(1, 'soar', 'soar', 'https://soar.invalid', 'live-secret', "
                 "'[\"ticket.created\"]', 1, 'mydemoticket', 1, ?, 1)", (TS,))
    conn.execute("INSERT INTO meta VALUES('ticket_counter', '1001')")
    conn.commit()
    conn.close()


def dump(path):
    conn = sqlite3.connect(path)
    try:
        data = {t: sorted(conn.execute("SELECT * FROM %s" % t).fetchall(), key=repr) for t in TABLES}
        data["schema"] = sorted(conn.execute("SELECT name, sql FROM sqlite_master").fetchall(), key=repr)
        data["user_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
        return data
    finally:
        conn.close()


class MigrationV4Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mdt-migrate-v4-")
        self.data_dir = os.path.join(self.tmp, "data")
        os.makedirs(self.data_dir, mode=0o700)
        self.db_path = os.path.join(self.data_dir, DB_NAME)
        build_v3_database(self.db_path)
        self.before = dump(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self):
        return App(self.data_dir, seed=True, quiet=True, audit_logger=AuditRecorder())

    def test_adds_profile_columns_and_tables_without_touching_data(self):
        app = self.start()
        try:
            db = app.db
            self.assertEqual(db.schema_version(), 4)
            for table in TABLES:
                with self.subTest(table=table):
                    self.assertEqual(len(db.query("SELECT * FROM %s" % table)), len(self.before[table]))
            users = db.query("SELECT username, timezone, theme, language FROM users ORDER BY id")
            self.assertEqual([tuple(r)[1:] for r in users], [("", "system", "de")] * 5)
            # every pre-existing users column value is unchanged
            old_users = sorted(self.before["users"], key=lambda r: r[0])
            new_users = db.conn.execute("SELECT * FROM users ORDER BY id").fetchall()
            self.assertEqual([tuple(r)[:len(old_users[0])] for r in new_users], [tuple(r) for r in old_users])
            for table in ("tickets", "journal", "webhooks", "sessions", "api_tokens"):
                with self.subTest(table=table):
                    rows = db.conn.execute("SELECT * FROM %s" % table).fetchall()
                    self.assertEqual(sorted((tuple(r) for r in rows), key=repr), self.before[table])
            self.assertEqual(db.query("SELECT COUNT(*) AS n FROM roles")[0]["n"], 0)
            self.assertEqual(db.query("SELECT COUNT(*) AS n FROM settings")[0]["n"], 0)
            role_cols = [r["name"] for r in db.query("PRAGMA table_info(roles)")]
            self.assertEqual(role_cols, ["id", "tenant_id", "name", "label", "description", "permissions",
                                         "created_at"])
            self.assertEqual([r["name"] for r in db.query("PRAGMA table_info(settings)")],
                             ["key", "value", "updated_at"])
            self.assertEqual(db.query("PRAGMA foreign_key_check"), [])
            self.assertEqual(db.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            backup = self.db_path + ".pre-v4.bak"
            self.assertTrue(os.path.exists(backup))
            self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o600)
            self.assertFalse(os.path.exists(self.db_path + ".pre-v3.bak"))
            self.assertEqual(dump(backup), self.before)
        finally:
            app.shutdown()

    def test_second_start_changes_nothing(self):
        self.start().shutdown()
        first = dump(self.db_path)
        backup_mtime = os.stat(self.db_path + ".pre-v4.bak").st_mtime
        self.start().shutdown()
        self.assertEqual(dump(self.db_path), first)
        self.assertEqual(os.stat(self.db_path + ".pre-v4.bak").st_mtime, backup_mtime)

    def test_failed_step_rolls_back(self):
        # simulate a broken live database: the step fails, the file stays at v3 unchanged
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()
        broken = dump(self.db_path)
        with self.assertRaises(sqlite3.OperationalError):
            self.start()
        self.assertEqual(dump(self.db_path), broken)
        conn = sqlite3.connect(self.db_path)
        try:
            columns = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
            self.assertNotIn("timezone", columns)
        finally:
            conn.close()

    def test_api_after_migration(self):
        app = self.start()
        httpd = app.make_server("127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            soar = Client(port, SOAR_TOKEN)
            self.assertEqual(soar.get("/me").json["role"], "integration")
            journal = soar.get("/tickets/TKT0001001/journal").json
            self.assertEqual([(e["type"], e["author"]) for e in journal], [("comment", "jdoe")])
            analyst = Client(port)
            resp = analyst.post("/auth/login", {"username": "analyst1", "password": "pw-analyst1"})
            self.assertEqual(resp.status, 200, resp)
            self.assertEqual(analyst.get("/me").json["role"], "agent")
        finally:
            httpd.shutdown()
            httpd.server_close()
            app.shutdown()


if __name__ == "__main__":
    unittest.main()
