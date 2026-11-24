# MyDemoTicket – REST API reference (v1)

Base URL: `http://127.0.0.1:8080/api/v1` locally, `https://ticket.soc-games.com/api/v1`
in the demo deployment. All bodies are JSON (`Content-Type: application/json`).

```bash
export MDT=http://127.0.0.1:8080/api/v1
export TOKEN=...   # an API token, see "Tokens" below; never commit it
```

## Conventions

| Topic | Rule |
|---|---|
| Auth | `Authorization: Bearer $TOKEN` (API) or session cookie + `X-CSRF-Token` (web UI) |
| Tenant | Every request works in exactly one tenant, see [Tenants](#tenants-multi-tenancy). Header `X-MyDemoTicket-Tenant: <slug>` selects it for `platform_admin`; for all other users it is optional and must name their own tenant |
| Request id | Every response carries `X-Request-Id` (UUID4); the same id is in the audit log |
| Source | Optional header `X-MyDemoTicket-Source: <text>` (max 64 chars). Stored as `source` on every journal/audit entry of the request and sent in webhooks. Default: `api` (token) or `web` (session). SOAR should send `soar`. |
| Timestamps | ISO-8601 UTC with `Z`, second precision, e.g. `2026-11-24T14:38:16Z` |
| Errors | `{"error": "<message>"}` with `400` bad JSON, `401` unauthenticated, `403` forbidden (also `tenant mismatch`, `tenant inactive`), `404` not found (also objects of other tenants), `405` method not allowed, `409` conflict / invalid state transition, `413` body > 1 MiB, `422` validation error |
| Lists | `GET /tickets` returns `{"items": [...], "total": n}`; all other list endpoints return a plain JSON array |

## Objects

### Ticket

```json
{
  "id": 1,
  "number": "TKT0001001",
  "short_description": "Suspicious login on demo-host-01",
  "description": "Multiple failed logins followed by a success",
  "state": "new",
  "priority": 2,
  "assignment_group": "SOC L1",
  "assigned_to": "analyst1",
  "caller": "soar",
  "category": "security",
  "custom_fields": {"affected_host": "demo-host-01", "risk_score": 80},
  "external_refs": {"soar_container_id": 12, "es_finding_id": "finding-placeholder"},
  "created_at": "2026-11-24T14:38:16Z",
  "updated_at": "2026-11-24T14:40:02Z",
  "resolved_at": null,
  "close_notes": ""
}
```

| Field | Type | Notes |
|---|---|---|
| `number` | string | `TKT` + 7 digits, sequential, read-only |
| `id` | int | read-only; `GET /tickets/{id}` also works |
| `short_description` | string | required, max 500 |
| `description` | string | |
| `state` | enum | `new`, `in_progress`, `on_hold`, `resolved`, `closed`, `cancelled` |
| `priority` | int | `1` critical, `2` high, `3` moderate (default), `4` low |
| `assignment_group` | string or null | must be an existing team name |
| `assigned_to` | string or null | must be an existing username |
| `caller` | string | username; defaults to the creating user (requesters: always themselves) |
| `category` | string | free text, max 100 |
| `custom_fields` | object | name → value, validated against custom field definitions |
| `external_refs` | object | free key → scalar value; conventions `soar_container_id`, `es_finding_id` |
| `created_at`, `updated_at`, `resolved_at` | string | read-only |
| `close_notes` | string | required (non-empty) to move to `resolved` |

### State transitions

| From | Allowed to |
|---|---|
| `new` | `in_progress`, `on_hold`, `resolved`, `cancelled` |
| `in_progress` | `on_hold`, `resolved`, `cancelled` |
| `on_hold` | `in_progress`, `resolved`, `cancelled` |
| `resolved` | `in_progress`, `closed` |
| `closed`, `cancelled` | – |

Other transitions → `409`. Setting the current state again is a no-op.
`resolved` without close notes (in the request or already on the ticket) → `422`.
Reopening (`resolved` → `in_progress`) clears `resolved_at`.

### Journal entry

```json
{"id": 7, "type": "comment", "body": "We are looking into it", "actor": "analyst1", "author": null, "source": "web", "at": "2026-11-24T14:41:00Z"}
{"id": 8, "type": "work_note", "body": "IOC enrichment done", "actor": "soar", "author": "jdoe", "source": "soar", "at": "..."}
{"id": 9, "type": "audit", "field": "state", "old": "new", "new": "in_progress", "actor": "analyst1", "author": null, "source": "web", "at": "..."}
```

`actor` is always the user of the token or session. `author` is the optional real
author an integration wrote on behalf of (see `POST /tickets/{number}/comments`),
otherwise `null`. The web UI shows `jdoe (via soar)` when `author` is set.

Audit `field` is the ticket field name, `custom_fields.<name>` / `external_refs.<key>`
for merged objects, or `created` for the creation entry. `old`/`new` keep their JSON types.

## Roles

All roles except `platform_admin` belong to exactly one tenant and only ever see
that tenant's data.

| Role | Summary |
|---|---|
| `platform_admin` | all tenants: manages tenants (`/tenants`) and platform admins, and has `admin` rights in the tenant of the request (header or session, default `soc-games`); has no tenant of its own |
| `admin` | tenant admin: everything inside the own tenant, including users, teams, custom fields, webhooks; cannot grant `platform_admin` |
| `agent` | reads all tickets of the tenant; changes/comments only tickets of own team, assigned to self, or fully unassigned (else `403`); may read `/users` and `/teams` |
| `integration` | like agent, but may change all tickets of the tenant (for SOAR) |
| `requester` | creates/reads/comments own tickets (`caller`); may only set `state: cancelled` on own tickets; never sees work notes; `403` on `/users`, `/teams`, admin endpoints |

## Tenants (multi-tenancy)

Tenant object:

```json
{"slug": "soc-games", "name": "SOC Games", "active": true, "soar_url": "https://soar.soc-games.com",
 "splunk_index": null, "default": true, "created_at": "...", "updated_at": "..."}
```

`slug` matches `[a-z0-9-]{2,32}` and is read-only. `soar_url` is an optional
http(s) URL, `splunk_index` an optional override of the audit index (`null` = the
default index from `MYDEMOTICKET_HEC_INDEX`, normally `mydemoticket` for all
tenants). The default tenant `soc-games` exists from the first start (or the
migration) and can neither be deactivated (`409`) nor deleted (there is no DELETE).

### Tenant context of a request

| Caller | Tenant of the request |
|---|---|
| any non-platform user | own tenant; header with another slug → `403 {"error": "tenant mismatch"}` |
| `platform_admin` | header `X-MyDemoTicket-Tenant`, else the tenant stored in the web session, else `soc-games`; unknown slug → `404 {"error": "tenant not found"}` |

* Users of a deactivated tenant get `403 {"error": "tenant inactive"}` for login
  (after a correct password), sessions and tokens.
* Objects of other tenants (tickets, journal, comments, teams, custom fields,
  webhooks, deliveries, users, tokens) are `404`, never `403`. Lists only contain
  the request's tenant. A role check still comes first: an agent calling an admin
  endpoint gets `403` regardless of the object.
* `assignment_group`, `assigned_to`, `caller`, team members and custom field
  values are validated against the request's tenant (`422` otherwise).
* Ticket numbers are one global sequence across all tenants (`TKT0001001`, ...),
  so SOAR can keep using them as `source_data_identifier`.
* Usernames are globally unique.

```bash
# platform admin works in tenant acme
curl -s -H "Authorization: Bearer $TOKEN" -H "X-MyDemoTicket-Tenant: acme" "$MDT/tickets"
```

### `GET /tenants`, `POST /tenants`, `GET/PATCH /tenants/{slug}` (platform_admin)

| Endpoint | Body / result |
|---|---|
| `GET /tenants` | array of tenant objects |
| `POST /tenants` | `{"slug", "name", "active"?, "soar_url"?, "splunk_index"?, "seed"?}` → `201`; `409` if the slug exists. `"seed": true` creates the teams `SOC L1`, `SOC L2` and the four standard custom fields in the new tenant |
| `GET /tenants/{slug}` | tenant object, `404` if unknown |
| `PATCH /tenants/{slug}` | any of `name`, `active`, `soar_url`, `splunk_index` → `200` |

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"slug": "acme", "name": "ACME Corp", "seed": true}' $MDT/tenants
curl -s -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"active": false}' $MDT/tenants/acme
```

### `POST /auth/tenant` (platform_admin, web session)

```bash
curl -s -b cookies.txt -X POST -H "X-CSRF-Token: $CSRF" -H "Content-Type: application/json" \
  -d '{"tenant": "acme"}' $MDT/auth/tenant
# 200 /me object with "active_tenant": {"slug": "acme", ...}
```

Stores the active tenant in the session (CSRF required like every UI write).
Other roles → `403`; token requests → `400` (use the header); unknown slug → `404`.
A new login starts in `soc-games` again.

---

## Health & session

### `GET /health` (no auth)

```bash
curl -s $MDT/health
# {"status": "ok", "version": "0.1.0"}
```

### `GET /me`

```bash
curl -s -H "Authorization: Bearer $TOKEN" $MDT/me
# {"username": "soar", "display_name": "...", "email": "...", "role": "integration", "tenant": {"slug": "soc-games",
#  "name": "SOC Games"}, "active": true, "teams": [], "has_password": true, "created_at": "..."}
#  (+ "csrf_token" for session auth)
```

`tenant` is the tenant of the request (for normal users their own). For a
`platform_admin`, `/me` additionally returns `active_tenant` (`{slug, name}`, same
as `tenant`) and `tenants` (all tenant objects).

### `POST /auth/login` / `POST /auth/logout` (web UI)

```bash
curl -s -c cookies.txt -H "Content-Type: application/json" \
  -d '{"username": "analyst1", "password": "'"$PASSWORD"'"}' $MDT/auth/login
# 200 user object + "csrf_token"; sets mdt_session (HttpOnly; SameSite=Strict)
curl -s -b cookies.txt -X POST -H "X-CSRF-Token: $CSRF" $MDT/auth/logout
```

Every non-GET request authenticated by the session cookie must send `X-CSRF-Token`
(else `403`). Bearer-token requests do not need it.

## Tickets

### `GET /tickets`

| Query | Meaning |
|---|---|
| `state` | one state or comma list, e.g. `new,in_progress` |
| `assignment_group` | team name; empty value = unassigned group |
| `assigned_to` | username; empty value = unassigned |
| `caller` | username |
| `priority` | 1–4 |
| `updated_since` | ISO-8601; returns tickets with `updated_at >= value` |
| `q` | substring search in number, short description, description |
| `external_ref.<key>` | exact match on `external_refs.<key>`; `<key>` must match `[a-z0-9_]{1,64}` (else `422`). The stored scalar is compared as text: a stored number `1133` matches `1133`, booleans match `true`/`false`. Several `external_ref.*` parameters are combined with AND, and with all other filters. |
| `sort` | `-updated_at` (default), `updated_at`, `number`, `-number`, `priority`, `-priority` |
| `limit` / `offset` | paging, `limit` 1–500 (default 50) |

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "$MDT/tickets?state=new,in_progress&assignment_group=SOC%20L1&updated_since=2026-11-24T00:00:00Z&limit=20"
# {"items": [ticket, ...], "total": 3}
```

As everywhere, only tickets of the request's tenant are returned (requesters: only their own).

#### Idempotency: search first, then create

A playbook that must not create a second ticket for the same ES finding looks the
ticket up by its reference first and creates one only if `total` is `0`:

```bash
FINDING=1133
found=$(curl -s -H "Authorization: Bearer $TOKEN" "$MDT/tickets?external_ref.es_finding_id=$FINDING&limit=1")
if [ "$(printf '%s' "$found" | python3 -c 'import json,sys; print(json.load(sys.stdin)["total"])')" = "0" ]; then
  curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -H "X-MyDemoTicket-Source: soar" \
    -d '{"short_description": "ES finding '"$FINDING"'", "external_refs": {"es_finding_id": "'"$FINDING"'"}}' \
    $MDT/tickets
fi
```

The check and the create are two requests; two playbook runs at the very same
moment can still both create a ticket. Run one playbook per finding (SOAR does this
per container) or check again afterwards.

### `POST /tickets` → `201` ticket

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -H "X-MyDemoTicket-Source: soar" \
  -d '{"short_description": "Phishing mail reported", "description": "User reported a mail",
       "priority": 2, "assignment_group": "SOC L1", "category": "security",
       "custom_fields": {"incident_type": "phishing", "mitre_technique": "T1566"},
       "external_refs": {"soar_container_id": 12, "es_finding_id": "finding-placeholder"}}' \
  $MDT/tickets
```

`state` may be omitted or `new`. Unknown or read-only fields → `422`.
Requesters may only send `short_description`, `description`, `priority`,
`category`, `custom_fields` (others → `403`).

### `GET /tickets/{number}` → ticket

```bash
curl -s -H "Authorization: Bearer $TOKEN" $MDT/tickets/TKT0001001
```

### `PATCH /tickets/{number}` → `200` ticket

Any of `short_description`, `description`, `state`, `priority`,
`assignment_group`, `assigned_to`, `caller`, `category`, `close_notes`,
`custom_fields` (merge, `null` removes a value), `external_refs` (merge, `null`
removes a key).

```bash
curl -s -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -H "X-MyDemoTicket-Source: soar" \
  -d '{"state": "resolved", "close_notes": "False positive, user confirmed login",
       "custom_fields": {"risk_score": 10}}' \
  $MDT/tickets/TKT0001001
```

### `GET /tickets/{number}/journal` → array

Chronological (oldest first). Requesters do not receive `work_note` entries.

```bash
curl -s -H "Authorization: Bearer $TOKEN" $MDT/tickets/TKT0001001/journal
```

### `POST /tickets/{number}/comments` → `201` journal entry

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -H "X-MyDemoTicket-Source: soar" \
  -d '{"body": "VirusTotal: 0/70 detections", "type": "work_note"}' \
  $MDT/tickets/TKT0001001/comments
```

`type` is `comment` (default, visible to the caller) or `work_note` (internal).

Optional `author`: the real person the integration writes for (e.g. the ES
analyst). String, trimmed, 1–64 characters, no control characters (else `422`);
`null` or missing means no author. `actor` stays the token user, so the UI shows
`jdoe (via soar)`. The web UI itself never sends `author`.

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -H "X-MyDemoTicket-Source: soar" \
  -d '{"body": "Host isolated.\nWaiting for forensics.", "author": "jdoe"}' \
  $MDT/tickets/TKT0001001/comments
# {"id": 12, "type": "comment", "body": "Host isolated.\nWaiting for forensics.", "actor": "soar",
#  "author": "jdoe", "source": "soar", "at": "..."}
```

The audit event `ticket.comment` carries `details.author` (or `null`).

## Custom fields

Definition: `{"name", "label", "type", "choices", "required", "created_at"}`;
`name` matches `[a-z0-9_]{1,64}`; `type` is `string`, `number`, `boolean`,
`choice` (needs `choices`) or `date` (`YYYY-MM-DD`). Seeded: `mitre_technique`
(string), `affected_host` (string), `risk_score` (number), `incident_type`
(choice: malware, phishing, data_exfiltration, other).
Definitions belong to a tenant; `name` is unique per tenant and ticket values are
validated against the definitions of the ticket's tenant. "admin" below means
tenant admin or platform admin (in the request's tenant).

| Endpoint | Role | Result |
|---|---|---|
| `GET /custom-fields` | any authenticated | array |
| `POST /custom-fields` | admin | `201`, `409` if name exists |
| `PATCH /custom-fields/{name}` | admin | `200`; `label`, `choices`, `required` (name/type read-only) |
| `DELETE /custom-fields/{name}` | admin | `204`; existing values stay on tickets |

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "contained", "label": "Contained", "type": "boolean"}' $MDT/custom-fields
```

## Users & tokens (admin)

User object: `{"username", "display_name", "email", "role", "tenant", "active", "teams", "has_password", "created_at"}`
(`tenant` is the slug, `null` for platform admins).

| Endpoint | Body / result |
|---|---|
| `GET /users` | users of the request's tenant (also allowed for agent/integration); a platform admin also sees the platform admins |
| `POST /users` | `{"username", "role", "tenant"?, "display_name"?, "email"?, "password"? (min 8), "teams"?, "active"?}` → `201` |
| `PATCH /users/{username}` | any of `display_name`, `email`, `role`, `active`, `password`, `teams` (replaces) → `200`; demoting/deactivating the last active platform admin → `409`; changing a role to or from `platform_admin` → `422` |

Rules for `POST /users`:

* `platform_admin` caller: `tenant` (slug) is required (`422` if missing or
  unknown), except for `"role": "platform_admin"`, which must not have a tenant
  or teams (`422`).
* tenant `admin` caller: the user is always created in the own tenant; a
  different `tenant` → `403 tenant mismatch`; `role: platform_admin` → `403`.
* `teams` must be teams of the target tenant (`422`).
| `GET /users/{username}/tokens` | `[{"id", "name", "created_at", "last_used_at"}]` |
| `POST /users/{username}/tokens` | `{"name"?}` → `201` `{"id", "name", "username", "token", ...}` – the plaintext `token` is returned **only once** |
| `DELETE /users/{username}/tokens/{id}` | `204`, token revoked |

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "soar-prod"}' $MDT/users/soar/tokens
```

## Reports

### `GET /reports/summary?since=<ISO>` (admin, agent, integration, platform_admin in the request's tenant)

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$MDT/reports/summary?since=2026-11-01T00:00:00Z"
```

```json
{"tenant": "soc-games", "generated_at": "2026-11-24T15:00:00Z", "tickets_total": 4, "open_count": 2,
 "by_state": {"new": 1, "in_progress": 1, "on_hold": 0, "resolved": 1, "closed": 1, "cancelled": 0},
 "by_priority": {"1": 1, "2": 1, "3": 1, "4": 1},
 "by_team": {"SOC L1": 2, "SOC L2": 1, "IT Ops": 0, "(none)": 1},
 "created_last_7d": 2, "resolved_last_7d": 1, "mttr_hours": 20.0}
```

* `since` (optional) limits all numbers to tickets created at or after it (`422` if not ISO-8601).
* `open_count` = `new` + `in_progress` + `on_hold`. `by_state`/`by_priority` list all
  values, `by_team` all teams of the tenant plus `(none)` for tickets without group.
* `created_last_7d` / `resolved_last_7d` count `created_at` / `resolved_at` within
  the last 7 days.
* `mttr_hours`: mean of `resolved_at − created_at` over tickets that have a
  `resolved_at` (resolved or closed), rounded to 2 decimals; `null` if there are none.
* `requester` → `403`; `integration` may read reports (read-only, e.g. SOAR or the MCP server).

## Audit

Every action is written to the audit log (local file + optional Splunk HEC), see
[logging.md](logging.md). Each event carries the `request_id` from the
`X-Request-Id` response header.

### `GET /audit/status` (admin, platform_admin)

```bash
curl -s -H "Authorization: Bearer $TOKEN" $MDT/audit/status
# {"hec_enabled": true, "queued": 0, "sent": 120, "failed": 0, "dropped": 0, "local_written": 120,
#  "last_error": null, "last_success_at": "..."}
```

## Teams (admin)

Team names are unique per tenant; members must be users of the same tenant (`422`).

| Endpoint | Body / result |
|---|---|
| `GET /teams` | `[{"name", "description", "members": [usernames]}]` (also agent/integration) |
| `POST /teams` | `{"name", "description"?, "members"?}` → `201` |
| `POST /teams/{name}/members` | `{"username"}` → `200` team |
| `DELETE /teams/{name}/members/{username}` | `200` team |

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"username": "analyst2"}' "$MDT/teams/SOC%20L1/members"
```

## Webhooks (admin)

Webhooks belong to the tenant of the request and only fire for tickets of that tenant.

Webhook object: `{"id", "name", "type", "url", "events", "active", "soar_label", "verify_tls", "has_secret", "created_at"}`
(the secret is never returned).

| Endpoint | Body / result |
|---|---|
| `GET /webhooks` | array |
| `POST /webhooks` | `{"type": "generic"\|"soar", "url", "secret", "events"? (default all), "name"?, "active"?, "soar_label"? (default `mydemoticket`), "verify_tls"? (default true)}` → `201` |
| `DELETE /webhooks/{id}` | `204` |
| `GET /webhooks/{id}/deliveries` | last 50: `[{"id", "delivery_id", "event", "ticket_number", "status", "response_code", "attempts", "error", "created_at", "updated_at"}]`; status `pending`, `retrying`, `success`, `failed`, `skipped` |

Events: `ticket.created`, `ticket.updated`, `ticket.state_changed`,
`ticket.commented`. One request emits exactly one event: a PATCH that changes
`state` emits `ticket.state_changed` (with all changes of that request), any other
PATCH with changes emits `ticket.updated`; no-op PATCHes emit nothing.

### Type `generic`

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"type": "generic", "name": "demo receiver", "url": "https://receiver.example.invalid/hook",
       "secret": "'"$WEBHOOK_SECRET"'", "events": ["ticket.created", "ticket.state_changed"]}' \
  $MDT/webhooks
```

Delivery: `POST <url>` with headers `X-MyDemoTicket-Event`, `X-MyDemoTicket-Delivery`,
`X-MyDemoTicket-Signature: sha256=<hex HMAC-SHA256(secret, raw body)>` and body

```json
{
  "event": "ticket.state_changed",
  "delivery_id": "1b4e28ba-2fa1-11d2-883f-0016d3cca427",
  "timestamp": "2026-11-24T14:45:00Z",
  "tenant": "soc-games",
  "source": "web",
  "actor": "analyst1",
  "ticket": {"number": "TKT0001001", "...": "full ticket object"},
  "changes": {"state": {"old": "new", "new": "in_progress"}},
  "comment": null
}
```

`comment` is the journal entry for `ticket.commented` (including `author`), else `null`. Verify the
signature over the exact raw body bytes, e.g. in Python:

```python
expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
hmac.compare_digest(expected, request.headers["X-MyDemoTicket-Signature"])
```

### Type `soar`

`url` is the SOAR base URL, `secret` the SOAR `ph-auth-token`.

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"type": "soar", "name": "SOAR", "url": "https://soar.example.invalid",
       "secret": "'"$SOAR_PH_AUTH_TOKEN"'", "soar_label": "mydemoticket", "verify_tls": true}' \
  $MDT/webhooks
```

Each event results in (header `ph-auth-token`, JSON):

1. `POST {url}/rest/container`
   ```json
   {"name": "TKT0001001: Phishing mail reported", "label": "mydemoticket",
    "source_data_identifier": "TKT0001001", "severity": "high", "description": "..."}
   ```
   Severity from priority: 1 → high, 2 → high, 3 → medium, 4 → low. The response
   `id` (new) or `existing_container_id` (duplicate) is used and stored in the
   ticket's `external_refs.soar_container_id` – this update emits no event.
2. `POST {url}/rest/artifact`
   ```json
   {"container_id": 12, "name": "ticket.state_changed", "label": "mydemoticket_event",
    "source_data_identifier": "<delivery_id>", "run_automation": true,
    "cef": {"ticketNumber": "TKT0001001", "ticketState": "resolved", "ticketPriority": 2,
            "assignmentGroup": "SOC L1", "assignedTo": "analyst1", "event": "ticket.state_changed",
            "changes": "{\"state\": {...}}", "comment": "", "commentAuthor": "", "customFields": "{...}",
            "esFindingId": "finding-placeholder", "source": "web", "tenant": "soc-games"}}
   ```
   `changes` and `customFields` are JSON strings. For `ticket.commented`,
   `comment` is the body and `commentAuthor` the optional author (else `""`). `cef.source` is an addition for
   playbook-side loop checks, `cef.tenant` is the tenant slug of the ticket. The
   container name stays `<number>: <short description>`.

Events whose `source` is `soar` (case-insensitive) are **not** delivered to
`soar` webhooks (logged as `skipped`) to avoid loops.

Retries: 3 attempts per delivery with 1 s / 2 s backoff on connection errors and
non-2xx answers (for the container call: missing `id`/`existing_container_id`).

## Polling example (SOAR fallback)

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "$MDT/tickets?updated_since=2026-11-24T14:00:00Z&sort=updated_at&limit=100"
```
