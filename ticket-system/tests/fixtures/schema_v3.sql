-- Frozen copy of schema v3 (multi-tenancy + comment author, before profile/roles/settings).
-- Generated from MIGRATIONS[0:3]; do not edit. Used by test_migration_v4.py.

CREATE TABLE meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL DEFAULT '',
        email TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL,
        password_hash TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    , tenant_id INTEGER REFERENCES tenants(id));

CREATE TABLE api_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL DEFAULT '',
        token_hash TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        last_used_at TEXT
    );

CREATE TABLE sessions (
        session_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        csrf_token TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    , active_tenant_id INTEGER REFERENCES tenants(id));

CREATE TABLE team_members (
        team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        PRIMARY KEY (team_id, user_id)
    );

CREATE TABLE tickets (
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
    , tenant_id INTEGER REFERENCES tenants(id));

CREATE TABLE journal (
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
    , tenant_id INTEGER REFERENCES tenants(id), author TEXT);

CREATE TABLE webhooks (
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
    , tenant_id INTEGER REFERENCES tenants(id));

CREATE TABLE webhook_deliveries (
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
    , tenant_id INTEGER REFERENCES tenants(id));

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

CREATE TABLE "teams" (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL REFERENCES tenants(id),
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        UNIQUE(tenant_id, name)
    );

CREATE TABLE "custom_field_defs" (
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

CREATE INDEX idx_tickets_updated ON tickets(updated_at);

CREATE INDEX idx_tickets_state ON tickets(state);

CREATE INDEX idx_journal_ticket ON journal(ticket_id);

CREATE INDEX idx_deliveries_webhook ON webhook_deliveries(webhook_id);

CREATE INDEX idx_users_tenant ON users(tenant_id);

CREATE INDEX idx_tickets_tenant ON tickets(tenant_id, updated_at);
