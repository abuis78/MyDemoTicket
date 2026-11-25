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
    # 2: multi-tenancy. Everything that exists is moved into the default tenant
    # 'soc-games'; the bootstrap user 'admin' becomes platform_admin (no tenant).
    # teams and custom_field_defs are rebuilt because their names become unique
    # per tenant instead of globally (SQLite cannot drop a UNIQUE constraint).
    """
    CREATE TABLE tenants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        soar_url TEXT,
        splunk_index TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    INSERT INTO tenants(slug, name, active, soar_url, splunk_index, created_at, updated_at)
        VALUES('soc-games', 'SOC Games', 1, 'https://soar.soc-games.com', NULL,
               strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));

    ALTER TABLE users ADD COLUMN tenant_id INTEGER REFERENCES tenants(id);
    UPDATE users SET tenant_id = (SELECT id FROM tenants WHERE slug = 'soc-games');
    UPDATE users SET role = 'platform_admin', tenant_id = NULL WHERE username = 'admin';
    CREATE INDEX idx_users_tenant ON users(tenant_id);

    ALTER TABLE sessions ADD COLUMN active_tenant_id INTEGER REFERENCES tenants(id);

    ALTER TABLE tickets ADD COLUMN tenant_id INTEGER REFERENCES tenants(id);
    UPDATE tickets SET tenant_id = (SELECT id FROM tenants WHERE slug = 'soc-games');
    CREATE INDEX idx_tickets_tenant ON tickets(tenant_id, updated_at);

    ALTER TABLE journal ADD COLUMN tenant_id INTEGER REFERENCES tenants(id);
    UPDATE journal SET tenant_id = (SELECT t.tenant_id FROM tickets t WHERE t.id = journal.ticket_id);

    CREATE TABLE teams_v2 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL REFERENCES tenants(id),
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        UNIQUE(tenant_id, name)
    );
    INSERT INTO teams_v2(id, tenant_id, name, description)
        SELECT id, (SELECT id FROM tenants WHERE slug = 'soc-games'), name, description FROM teams;
    DROP TABLE teams;
    ALTER TABLE teams_v2 RENAME TO teams;

    CREATE TABLE custom_field_defs_v2 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL REFERENCES tenants(id),
        name TEXT NOT NULL,
        label TEXT NOT NULL,
        type TEXT NOT NULL,
        choices TEXT NOT NULL DEFAULT '[]',
        required INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        UNIQUE(tenant_id, name)
    );
    INSERT INTO custom_field_defs_v2(tenant_id, name, label, type, choices, required, created_at)
        SELECT (SELECT id FROM tenants WHERE slug = 'soc-games'), name, label, type, choices, required,
               created_at
        FROM custom_field_defs ORDER BY created_at, name;
    DROP TABLE custom_field_defs;
    ALTER TABLE custom_field_defs_v2 RENAME TO custom_field_defs;

    ALTER TABLE webhooks ADD COLUMN tenant_id INTEGER REFERENCES tenants(id);
    UPDATE webhooks SET tenant_id = (SELECT id FROM tenants WHERE slug = 'soc-games');
    ALTER TABLE webhook_deliveries ADD COLUMN tenant_id INTEGER REFERENCES tenants(id);
    UPDATE webhook_deliveries SET tenant_id = (SELECT w.tenant_id FROM webhooks w
                                               WHERE w.id = webhook_deliveries.webhook_id);
    """,
    # 3: optional comment author (the real person behind an integration comment,
    # e.g. the ES analyst). Additive only: existing rows keep author = NULL.
    """
    ALTER TABLE journal ADD COLUMN author TEXT;
    """,
    # 4: user profile (time zone, theme, language), custom roles per tenant and
    # instance-wide settings (Splunk HEC connection). Additive only.
    """
    ALTER TABLE users ADD COLUMN timezone TEXT NOT NULL DEFAULT '';
    ALTER TABLE users ADD COLUMN theme TEXT NOT NULL DEFAULT 'system';
    ALTER TABLE users ADD COLUMN language TEXT NOT NULL DEFAULT 'de';
    CREATE TABLE roles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL REFERENCES tenants(id),
        name TEXT NOT NULL,
        label TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        permissions TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        UNIQUE(tenant_id, name)
    );
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    );
    """,
]

FIRST_TICKET_NUMBER = 1001


def _json_member(doc, path):
    """Resolve a single-member path ``$."key"`` (the only form the API uses)."""
    if not isinstance(path, str) or not path.startswith('$."') or not path.endswith('"'):
        raise ValueError("unsupported JSON path")
    try:
        obj = json.loads(doc) if doc is not None else None
    except ValueError:
        return False, None
    key = path[3:-1]
    if not isinstance(obj, dict) or key not in obj:
        return False, None
    return True, obj[key]


def _py_json_type(doc, path):
    found, value = _json_member(doc, path)
    if not found:
        return None
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "real"
    if isinstance(value, str):
        return "text"
    return "array" if isinstance(value, list) else "object"


def _py_json_extract(doc, path):
    found, value = _json_member(doc, path)
    if not found or value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        return value
    return json.dumps(value, separators=(",", ":"))


def ensure_json_functions(conn):
    """Make json_extract/json_type available on ``conn``.

    Returns True when SQLite's built-in JSON functions are present. Otherwise
    registers small Python equivalents (enough for ``$."key"`` paths) and
    returns False, so ticket filtering stays in SQL and totals stay correct.
    """
    try:
        conn.execute("SELECT json_extract('{\"a\":1}', '$.a'), json_type('{}')").fetchone()
        return True
    except sqlite3.OperationalError:
        conn.create_function("json_extract", 2, _py_json_extract)
        conn.create_function("json_type", 2, _py_json_type)
        return False


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
        self.json1 = ensure_json_functions(self.conn)
        self.migrate()

    # -- migrations -------------------------------------------------------
    def schema_version(self):
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self):
        """Apply pending migrations, each one in its own transaction.

        Foreign keys are switched off while migrating (SQLite only allows that
        outside a transaction) so that rebuilding a table never cascades into the
        rows that reference it; ``PRAGMA foreign_key_check`` must be clean before
        a step is committed. Before step N is applied to an existing database file,
        a copy of the state before that step is written to ``<db>.pre-v<N>.bak``
        (once per target version; an existing backup is never overwritten).
        """
        with self.lock:
            current = self.schema_version()
            if current >= len(MIGRATIONS):
                return
            self.conn.execute("PRAGMA foreign_keys = OFF")
            try:
                for index, script in enumerate(MIGRATIONS):
                    version = index + 1
                    if version <= current:
                        continue
                    if current > 0:  # existing data: keep the pre-step state
                        self._backup_before(version)
                    self.conn.execute("BEGIN IMMEDIATE")
                    try:
                        for statement in script.split(";"):
                            if statement.strip():
                                self.conn.execute(statement)
                        problems = self.conn.execute("PRAGMA foreign_key_check").fetchall()
                        if problems:
                            raise sqlite3.IntegrityError(
                                "migration %d breaks %d foreign key reference(s)" % (version, len(problems))
                            )
                        self.conn.execute("PRAGMA user_version = %d" % version)
                        self.conn.execute("COMMIT")
                    except Exception:
                        self.conn.execute("ROLLBACK")
                        raise
            finally:
                self.conn.execute("PRAGMA foreign_keys = ON")

    def _backup_before(self, target_version):
        if self.path == ":memory:":
            return
        backup_path = "%s.pre-v%d.bak" % (self.path, target_version)
        if os.path.exists(backup_path):
            return
        fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        dest = sqlite3.connect(backup_path)
        try:
            self.conn.backup(dest)
        finally:
            dest.close()
        os.chmod(backup_path, 0o600)

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
