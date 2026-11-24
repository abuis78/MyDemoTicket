"""SQLite storage: connection handling, schema and idempotent migrations."""

import datetime
import json
import os
import sqlite3
import threading

# Each entry is one migration step. The index + 1 is the schema version.
# Never edit an existing step; append a new one instead.
MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL DEFAULT '',
        email TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL,
        password_hash TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS api_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL DEFAULT '',
        token_hash TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        last_used_at TEXT
    );
    CREATE TABLE IF NOT EXISTS sessions (
        session_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        csrf_token TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS teams (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS team_members (
        team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        PRIMARY KEY (team_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        number TEXT NOT NULL UNIQUE,
        short_description TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT 'new',
        priority INTEGER NOT NULL DEFAULT 3,
        assignment_group TEXT,
        assigned_to TEXT,
        caller TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT '',
        custom_fields TEXT NOT NULL DEFAULT '{}',
        external_refs TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        resolved_at TEXT,
        close_notes TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_tickets_updated ON tickets(updated_at);
    CREATE INDEX IF NOT EXISTS idx_tickets_state ON tickets(state);
    CREATE TABLE IF NOT EXISTS journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
        type TEXT NOT NULL,
        body TEXT,
        field TEXT,
        old_value TEXT,
        new_value TEXT,
        actor TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '',
        at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_journal_ticket ON journal(ticket_id);
    CREATE TABLE IF NOT EXISTS custom_field_defs (
        name TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        type TEXT NOT NULL,
        choices TEXT NOT NULL DEFAULT '[]',
        required INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS webhooks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL DEFAULT '',
        type TEXT NOT NULL,
        url TEXT NOT NULL,
        secret TEXT NOT NULL DEFAULT '',
        events TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        soar_label TEXT NOT NULL DEFAULT 'mydemoticket',
        verify_tls INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS webhook_deliveries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        webhook_id INTEGER NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
        delivery_id TEXT NOT NULL,
        event TEXT NOT NULL,
        ticket_number TEXT,
        status TEXT NOT NULL,
        response_code INTEGER,
        attempts INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_deliveries_webhook ON webhook_deliveries(webhook_id);
    """,
]

FIRST_TICKET_NUMBER = 1001


def utcnow():
    """Current UTC time as ISO-8601 string with Z suffix (second precision)."""
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value):
    """Parse an ISO-8601 timestamp (Z or offset) into the canonical UTC string.

    Returns None if the value cannot be parsed.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.datetime.combine(
                datetime.date.fromisoformat(text), datetime.time()
            )
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    parsed = parsed.astimezone(datetime.timezone.utc).replace(microsecond=0)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def default_data_dir():
    env = os.environ.get("MYDEMOTICKET_DATA_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".mydemoticket")


class Database:
    """Thread-safe wrapper around a single sqlite3 connection.

    All access is serialized with a re-entrant lock; this is plenty for a demo
    system and keeps transactions simple.
    """

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.migrate()

    # -- migrations -------------------------------------------------------
    def schema_version(self):
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self):
        with self.lock:
            current = self.schema_version()
            for index, script in enumerate(MIGRATIONS):
                version = index + 1
                if version <= current:
                    continue
                self.conn.execute("BEGIN")
                try:
                    for statement in script.split(";"):
                        if statement.strip():
                            self.conn.execute(statement)
                    self.conn.execute("PRAGMA user_version = %d" % version)
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise

    # -- helpers ----------------------------------------------------------
    def execute(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params)

    def query(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def transaction(self):
        return _Transaction(self)

    def get_meta(self, key, default=None):
        row = self.query_one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_meta(self, key, value):
        self.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    def next_ticket_number(self):
        """Allocate the next ticket number. Call inside a transaction."""
        with self.lock:
            current = int(self.get_meta("ticket_counter", FIRST_TICKET_NUMBER - 1))
            nxt = current + 1
            self.set_meta("ticket_counter", nxt)
            return "TKT%07d" % nxt

    def close(self):
        with self.lock:
            self.conn.close()


class _Transaction:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db.lock.acquire()
        self.db.conn.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.db.conn.execute("COMMIT")
            else:
                self.db.conn.execute("ROLLBACK")
        finally:
            self.db.lock.release()
        return False


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def loads(text, default=None):
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default
