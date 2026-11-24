"""Migration of a live schema-v2 database (multi-tenancy) to v3 (comment author).

The v2 schema is a frozen copy in tests/fixtures/schema_v2.sql. The test fills it
with data shaped like the production instance, starts the app (which migrates),
and checks that nothing is lost, existing journal rows get author = NULL, a
backup of the v2 state is written, a second start changes nothing and a failing
step rolls back completely.
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

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "schema_v2.sql")
DB_NAME = "mydemoticket.sqlite3"
TS = "2026-11-22T10:00:00Z"
SOAR_TOKEN = "mdt_live_v2_soar_token"
TABLES = ("tenants", "meta", "users", "api_tokens", "sessions", "teams", "team_members", "tickets",
          "journal", "custom_field_defs", "webhooks", "webhook_deliveries")


def build_v2_database(path):
    conn = sqlite3.connect(path)
    with open(FIXTURE, encoding="utf-8") as handle:
        conn.executescript(handle.read())
    conn.execute("PRAGMA user_version = 2")
    conn.execute("INSERT INTO tenants(id, slug, name, active, soar_url, splunk_index, created_at, updated_at) "
                 "VALUES(1, 'soc-games', 'SOC Games', 1, 'https://soar.soc-games.com', NULL, ?, ?)", (TS, TS))
    conn.execute("INSERT INTO tenants(id, slug, name, active, soar_url, splunk_index, created_at, updated_at) "
                 "VALUES(2, 'acme', 'ACME', 1, NULL, 'acme_audit', ?, ?)", (TS, TS))
    users = [(1, "admin", "platform_admin", None), (2, "analyst1", "agent", 1), (3, "soar", "integration", 1),
             (4, "acme-agent", "agent", 2)]
    for uid, name, role, tenant in users:
        conn.execute(
            "INSERT INTO users(id, username, display_name, email, role, password_hash, active, created_at, "
            "tenant_id) VALUES(?, ?, ?, '', ?, ?, 1, ?, ?)",
            (uid, name, name, role, hash_password("pw-" + name, 1000), TS, tenant))
    conn.execute("INSERT INTO api_tokens(user_id, name, token_hash, created_at) VALUES(3, 'soar-prod', ?, ?)",
                 (hash_token(SOAR_TOKEN), TS))
    conn.execute("INSERT INTO teams(id, tenant_id, name, description) VALUES(1, 1, 'SOC L1', '')")
    conn.execute("INSERT INTO team_members VALUES(1, 2)")
    tickets = [(1, "TKT0001001", 1, '{"es_finding_id": 1133}'), (2, "TKT0001002", 1, '{}'),
               (3, "TKT0001003", 2, '{"es_finding_id": "1133"}')]
    for tid, number, tenant, refs in tickets:
        conn.execute(
            "INSERT INTO tickets(id, number, short_description, description, state, priority, assignment_group, "
            "assigned_to, caller, category, custom_fields, external_refs, created_at, updated_at, resolved_at, "
            "close_notes, tenant_id) VALUES(?, ?, 'live', '', 'new', 3, NULL, NULL, 'soar', '', '{}', ?, ?, ?, "
            "NULL, '', ?)", (tid, number, refs, TS, TS, tenant))
        conn.execute("INSERT INTO journal(ticket_id, type, field, old_value, new_value, actor, source, at, "
                     "tenant_id) VALUES(?, 'audit', 'created', 'null', ?, 'soar', 'soar', ?, ?)",
                     (tid, '"%s"' % number, TS, tenant))
    conn.execute("INSERT INTO journal(ticket_id, type, body, actor, source, at, tenant_id) "
                 "VALUES(1, 'comment', 'line one\nline two', 'soar', 'soar', ?, 1)", (TS,))
    conn.execute("INSERT INTO meta VALUES('ticket_counter', '1003')")
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


class MigrationV3Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mdt-migrate-v3-")
        self.data_dir = os.path.join(self.tmp, "data")
        os.makedirs(self.data_dir, mode=0o700)
        self.db_path = os.path.join(self.data_dir, DB_NAME)
        build_v2_database(self.db_path)
        self.before = dump(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self):
        return App(self.data_dir, seed=True, quiet=True, audit_logger=AuditRecorder())

    def test_adds_author_column_without_touching_data(self):
        app = self.start()
        try:
            db = app.db
            self.assertEqual(db.schema_version(), 3)
            columns = [r["name"] for r in db.query("PRAGMA table_info(journal)")]
            self.assertIn("author", columns)
            for table in TABLES:
                with self.subTest(table=table):
                    rows = db.query("SELECT * FROM %s" % table)
                    self.assertEqual(len(rows), len(self.before[table]))
            self.assertEqual([r["author"] for r in db.query("SELECT author FROM journal")], [None] * 4)
            # every pre-existing journal column value is unchanged
            old_journal = sorted(self.before["journal"], key=lambda r: r[0])
            new_journal = db.conn.execute("SELECT * FROM journal ORDER BY id").fetchall()
            self.assertEqual([tuple(r)[:len(old_journal[0])] for r in new_journal], [tuple(r) for r in old_journal])
            self.assertEqual(db.query("PRAGMA foreign_key_check"), [])
            self.assertEqual(db.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            backup = self.db_path + ".pre-v3.bak"
            self.assertTrue(os.path.exists(backup))
            self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o600)
            self.assertFalse(os.path.exists(self.db_path + ".pre-v2.bak"))
            self.assertEqual(dump(backup), self.before)
        finally:
            app.shutdown()

    def test_second_start_changes_nothing(self):
        self.start().shutdown()
        first = dump(self.db_path)
        backup_mtime = os.stat(self.db_path + ".pre-v3.bak").st_mtime
        self.start().shutdown()
        self.assertEqual(dump(self.db_path), first)
        self.assertEqual(os.stat(self.db_path + ".pre-v3.bak").st_mtime, backup_mtime)

    def test_failed_step_rolls_back(self):
        # simulate a broken live database: the step fails, the file stays at v2 unchanged
        conn = sqlite3.connect(self.db_path)
        conn.execute("ALTER TABLE journal ADD COLUMN author TEXT")
        conn.commit()
        conn.close()
        broken = dump(self.db_path)
        with self.assertRaises(sqlite3.OperationalError):
            self.start()
        self.assertEqual(dump(self.db_path), broken)

    def test_api_after_migration(self):
        app = self.start()
        httpd = app.make_server("127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            soar = Client(httpd.server_address[1], SOAR_TOKEN)
            journal = soar.get("/tickets/TKT0001001/journal").json
            self.assertEqual([(e["type"], e["author"]) for e in journal], [("audit", None), ("comment", None)])
            self.assertEqual(journal[1]["body"], "line one\nline two")
            found = soar.get("/tickets?external_ref.es_finding_id=1133").json
            self.assertEqual([t["number"] for t in found["items"]], ["TKT0001001"])  # own tenant only
            self.assertEqual(found["total"], 1)
            resp = soar.post("/tickets/TKT0001002/comments", {"body": "from ES", "author": "jdoe"})
            self.assertEqual(resp.status, 201, resp)
            self.assertEqual((resp.json["author"], resp.json["actor"]), ("jdoe", "soar"))
        finally:
            httpd.shutdown()
            httpd.server_close()
            app.shutdown()


if __name__ == "__main__":
    unittest.main()
