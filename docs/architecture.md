# MyDemoTicket – Architecture

MyDemoTicket is a small, ServiceNow-like ticketing system built for Splunk SOAR
and Splunk Enterprise Security (ES) demos. It runs on the Python standard library
only (Python 3.9+): no third-party packages, no build step, no CDN.

## Components

```
ticket-system/mydemoticket/
  __main__.py   CLI: python3 -m mydemoticket --host --port --data-dir --no-seed
  server.py     ThreadingHTTPServer, routing, auth (Bearer / session + CSRF), static files
  api.py        REST handlers: tickets, journal, custom fields, users, teams, webhooks
  auth.py       roles, pbkdf2 passwords, sha256 tokens, sessions, tenant context, first-start bootstrap
  tenants.py    tenant lookup, validation, per-tenant seed (teams, custom fields)
  audit.py      audit logger: local JSON log + optional Splunk HEC (see docs/logging.md)
  webhooks.py   background worker for outbound webhooks (generic + SOAR), retry 3x
  db.py         sqlite3 connection, schema + idempotent migrations (PRAGMA user_version)
  static/       single-page web UI (index.html, app.js, app.css)
```

| Component | Responsibility | State |
|---|---|---|
| HTTP server | Parses requests, authenticates, dispatches to `Api`, serves UI | none |
| `Api` | Validation, state machine, permissions, journal/audit, emits events | SQLite |
| `WebhookDispatcher` | Queues events, delivers with retry, writes delivery log | SQLite + in-memory queue |
| `AuditLogger` | One audit event per action, request id, never secrets | `<data-dir>/logs/audit.log` + HEC |
| SQLite | Tenants, tickets, journal, users, tokens, sessions, teams, custom fields, webhooks | `<data-dir>/mydemoticket.sqlite3` |

The data directory (`$MYDEMOTICKET_DATA_DIR` or `~/.mydemoticket`) holds the
database and, after the first start, `initial-credentials.txt` (mode 0600).
Nothing else is written outside it.

## Data flow Ticket ↔ SOAR ↔ ES

```mermaid
sequenceDiagram
    participant ES as Splunk ES
    participant SOAR as Splunk SOAR
    participant MDT as MyDemoTicket
    participant UI as Analyst (Web UI)

    ES->>SOAR: Finding / notable (container + artifacts)
    SOAR->>MDT: POST /api/v1/tickets (Bearer soar token, X-MyDemoTicket-Source: soar,<br/>external_refs.es_finding_id, soar_container_id)
    MDT-->>SOAR: 201 ticket (number TKT000xxxx)
    Note over MDT: source = soar → SOAR-type webhooks skip delivery (no echo)
    UI->>MDT: PATCH state / assign / work note (session cookie + CSRF)
    MDT->>SOAR: webhook type soar: POST /rest/container (dedupe by number)<br/>POST /rest/artifact (cef: ticketState, changes, comment ...)
    SOAR->>SOAR: run_automation → playbook syncs state/notes
    SOAR->>ES: update finding status / add note
    SOAR->>MDT: PATCH /api/v1/tickets/{number} (source soar) e.g. resolve with close_notes
```

ASCII overview:

```
 +-----------+   finding    +-------------+   REST (Bearer, source=soar)   +---------------+
 | Splunk ES | -----------> | Splunk SOAR | -----------------------------> | MyDemoTicket  |
 |           | <----------- |  (app +     | <----------------------------- |  REST + UI    |
 +-----------+  status/note |  playbooks) |  webhook: container+artifact   |  SQLite       |
                            +-------------+  or generic signed JSON        +-------+-------+
                                                                                    ^
                                                                    browser (session|CSRF)
                                                                                    |
                                                                             SOC analysts
```

## Sync concept

* **Identity:** the ticket `number` is the stable key. It is used as the SOAR
  container `source_data_identifier`, so container creation is idempotent: SOAR
  answers duplicates with `existing_container_id`, which MyDemoTicket stores in
  `external_refs.soar_container_id`.
* **Cross references:** `external_refs` is a free key/value object that is merged
  on PATCH (`null` deletes a key). Conventions: `soar_container_id` (int),
  `es_finding_id` (string).
* **Push (ticket → SOAR):** every change emits exactly one event:
  `ticket.created`, `ticket.state_changed` (if `state` changed, includes all other
  changes of that request), `ticket.updated` (all other field changes) or
  `ticket.commented` (comment or work note). Webhooks subscribe per event.
* **Pull (SOAR → ticket):** SOAR can poll `GET /tickets?updated_since=<ISO>`
  (inclusive, second precision) as a fallback for missed webhooks.
* **Loop prevention:** callers set `X-MyDemoTicket-Source`. The value is stored on
  each journal/audit entry and sent as `source` in every webhook. SOAR-type
  webhooks do not deliver events whose source is `soar`; generic receivers get
  `source` and filter themselves. Storing `soar_container_id` after a delivery is
  a system update that never emits an event.
* **Reliability:** deliveries run on one background thread, 3 attempts with
  exponential backoff (1 s, 2 s). The last 50 deliveries per webhook are visible
  via `GET /webhooks/{id}/deliveries` and in the admin UI. The queue is in memory:
  events queued at shutdown are lost; use `updated_since` polling to reconcile.

## State machine

```mermaid
stateDiagram-v2
    [*] --> new
    new --> in_progress
    new --> on_hold
    new --> resolved
    new --> cancelled
    in_progress --> on_hold
    in_progress --> resolved
    in_progress --> cancelled
    on_hold --> in_progress
    on_hold --> resolved
    on_hold --> cancelled
    resolved --> in_progress
    resolved --> closed
    closed --> [*]
    cancelled --> [*]
```

Invalid transitions return `409`; `resolved` requires non-empty `close_notes`
(`422`). `resolved_at` is set on resolve and cleared when reopened.

## Tenant model (multi-tenancy)

```
tenants ──< users (tenant_id; NULL only for platform_admin) ──< api_tokens, sessions
   │                                                             (sessions.active_tenant_id:
   ├──< tickets ──< journal (tenant_id)                           platform_admin's choice)
   ├──< teams (name unique per tenant) ──< team_members
   ├──< custom_field_defs (name unique per tenant)
   └──< webhooks ──< webhook_deliveries (tenant_id)
```

* **One database, one shared schema**, every tenant-owned row carries `tenant_id`.
  Sessions and tokens belong to a user and inherit the user's tenant.
* **Default tenant** `soc-games` ("SOC Games", `soar_url https://soar.soc-games.com`)
  is created by the migration on the first start and cannot be deactivated.
* **Request context:** after authentication `resolve_tenant()` (auth.py) fixes the
  tenant of the request: the user's own tenant, or for `platform_admin` the
  `X-MyDemoTicket-Tenant` header, the tenant stored in the session, or the default.
  A header naming another tenant → `403 tenant mismatch`; an inactive tenant →
  `403 tenant inactive`. Every handler only queries with `tenant_id = <context>`.
* **Isolation:** objects of other tenants are answered with `404`, so their
  existence is not revealed; lists never contain other tenants' rows. References
  (assignment group, assignee, caller, team members, custom field values) are
  validated inside the tenant (`422`). Webhooks are selected by the ticket's
  tenant and carry `tenant` in the payload / SOAR `cef`.
* **Global identifiers:** ticket numbers stay one global sequence (SOAR uses them as
  `source_data_identifier`), usernames are globally unique.
* **Migration (schema v2):** runs at start in one transaction with foreign keys
  off, `PRAGMA foreign_key_check` must be clean before `COMMIT`; before upgrading
  an existing file a copy is written to `mydemoticket.sqlite3.pre-v2.bak` (0600).
  Everything existing moves to `soc-games`; user `admin` becomes `platform_admin`.
  A second start finds `user_version = 2` and does nothing.
* **Audit:** every action and every `401`/`403` is logged via `AuditLogger` with
  the tenant slug; HEC sends all tenants to one index (`MYDEMOTICKET_HEC_INDEX`,
  normally `mydemoticket`) with `tenant` as indexed field; `tenants.splunk_index`
  is an optional per-tenant override. Details: [logging.md](logging.md).

## Role model

All rows refer to the tenant of the request; nobody except `platform_admin` can
leave the own tenant.

| Role | Tickets read | Tickets change / work notes | Comments | Admin (users, teams, fields, webhooks) |
|---|---|---|---|---|
| `platform_admin` | all (of the selected tenant) | all | all | yes, in every tenant, plus tenants and platform admins |
| `admin` | all | all | all | yes, own tenant only |
| `agent` | all | own team's, assigned to self, or unassigned; else 403 | same as change | no (may read users/teams) |
| `integration` | all | all | all | no (may read users/teams) |
| `requester` | own (`caller`) only | only `state: cancelled` on own tickets | own tickets, `comment` only; never sees work notes | no |

Authentication:

* **API:** `Authorization: Bearer <token>`. Tokens are random, stored as SHA-256
  hashes, created by an admin (`POST /users/{username}/tokens`) and shown once.
* **Web UI:** `POST /auth/login` with username/password (PBKDF2-HMAC-SHA256,
  200k iterations) sets an `HttpOnly; SameSite=Strict` session cookie (`Secure`
  behind HTTPS). Writing requests must send `X-CSRF-Token` (returned at login and
  by `GET /me`).
* **Bootstrap:** on first start `admin` (role `platform_admin`, no tenant) is created with
  `MYDEMOTICKET_ADMIN_PASSWORD` / `MYDEMOTICKET_ADMIN_TOKEN`; missing values, all
  seed users' passwords and the `soar` user's token are generated randomly and
  written to `<data-dir>/initial-credentials.txt` (0600), never to stdout.

## Deployment target

```
Internet ──443/TLS──> Caddy (ticket.soc-games.com, automatic Let's Encrypt)
                         │ reverse_proxy 127.0.0.1:8080, X-Forwarded-Proto
                         ▼
               systemd: mydemoticket.service (user mydemoticket)
               python3 -m mydemoticket --host 127.0.0.1 --port 8080 --secure-cookies
                         │
               /var/lib/mydemoticket (data dir, SQLite, 0700)
```

* Small AWS EC2 instance (t3.micro / t4g.small is plenty), Amazon Linux 2023 or
  Ubuntu with system Python 3.9+. Security group: 80/443 from the internet (or
  demo IP ranges), 22 only from admin IPs. Port 8080 is never exposed.
* DNS: `A` record `ticket.soc-games.com` → Elastic IP of the instance.
* `deploy/install.sh` renders the systemd unit and Caddyfile (`--dry-run`,
  `--uninstall`); see `deploy/README.md`.
* Backup: copy the data directory (stop the service or use `sqlite3 .backup`).
