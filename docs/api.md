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
| Source | Optional header `X-MyDemoTicket-Source: <text>` (max 64 chars). Stored as `source` on every journal/audit entry of the request and sent in webhooks. Default: `api` (token) or `web` (session). SOAR should send `soar`. |
| Timestamps | ISO-8601 UTC with `Z`, second precision, e.g. `2026-11-24T14:38:16Z` |
| Errors | `{"error": "<message>"}` with `400` bad JSON, `401` unauthenticated, `403` forbidden, `404` not found, `405` method not allowed, `409` conflict / invalid state transition, `413` body > 1 MiB, `422` validation error |
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
{"id": 7, "type": "comment", "body": "We are looking into it", "actor": "analyst1", "source": "web", "at": "2026-11-24T14:41:00Z"}
{"id": 8, "type": "work_note", "body": "IOC enrichment done", "actor": "soar", "source": "soar", "at": "..."}
{"id": 9, "type": "audit", "field": "state", "old": "new", "new": "in_progress", "actor": "analyst1", "source": "web", "at": "..."}
```

Audit `field` is the ticket field name, `custom_fields.<name>` / `external_refs.<key>`
for merged objects, or `created` for the creation entry. `old`/`new` keep their JSON types.

## Roles

| Role | Summary |
|---|---|
| `admin` | everything, including users, teams, custom fields, webhooks |
| `agent` | reads all tickets; changes/comments only tickets of own team, assigned to self, or fully unassigned (else `403`); may read `/users` and `/teams` |
| `integration` | like agent, but may change all tickets (for SOAR) |
| `requester` | creates/reads/comments own tickets (`caller`); may only set `state: cancelled` on own tickets; never sees work notes; `403` on `/users`, `/teams`, admin endpoints |

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
# {"username": "soar", "display_name": "...", "email": "...", "role": "integration", "active": true,
#  "teams": [], "has_password": true, "created_at": "..."}   (+ "csrf_token" for session auth)
```

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
| `sort` | `-updated_at` (default), `updated_at`, `number`, `-number`, `priority`, `-priority` |
| `limit` / `offset` | paging, `limit` 1–500 (default 50) |

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "$MDT/tickets?state=new,in_progress&assignment_group=SOC%20L1&updated_since=2026-11-24T00:00:00Z&limit=20"
# {"items": [ticket, ...], "total": 3}
```

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

## Custom fields

Definition: `{"name", "label", "type", "choices", "required", "created_at"}`;
`name` matches `[a-z0-9_]{1,64}`; `type` is `string`, `number`, `boolean`,
`choice` (needs `choices`) or `date` (`YYYY-MM-DD`). Seeded: `mitre_technique`
(string), `affected_host` (string), `risk_score` (number), `incident_type`
(choice: malware, phishing, data_exfiltration, other).

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

User object: `{"username", "display_name", "email", "role", "active", "teams", "has_password", "created_at"}`.

| Endpoint | Body / result |
|---|---|
| `GET /users` | array (also allowed for agent/integration) |
| `POST /users` | `{"username", "role", "display_name"?, "email"?, "password"? (min 8), "teams"?, "active"?}` → `201` |
| `PATCH /users/{username}` | any of `display_name`, `email`, `role`, `active`, `password`, `teams` (replaces) → `200`; demoting/deactivating the last admin → `409` |
| `GET /users/{username}/tokens` | `[{"id", "name", "created_at", "last_used_at"}]` |
| `POST /users/{username}/tokens` | `{"name"?}` → `201` `{"id", "name", "username", "token", ...}` – the plaintext `token` is returned **only once** |
| `DELETE /users/{username}/tokens/{id}` | `204`, token revoked |

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "soar-prod"}' $MDT/users/soar/tokens
```

## Teams (admin)

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
  "source": "web",
  "actor": "analyst1",
  "ticket": {"number": "TKT0001001", "...": "full ticket object"},
  "changes": {"state": {"old": "new", "new": "in_progress"}},
  "comment": null
}
```

`comment` is the journal entry for `ticket.commented`, else `null`. Verify the
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
            "changes": "{\"state\": {...}}", "comment": "", "customFields": "{...}",
            "esFindingId": "finding-placeholder", "source": "web"}}
   ```
   `changes` and `customFields` are JSON strings. `cef.source` is an addition for
   playbook-side loop checks.

Events whose `source` is `soar` (case-insensitive) are **not** delivered to
`soar` webhooks (logged as `skipped`) to avoid loops.

Retries: 3 attempts per delivery with 1 s / 2 s backoff on connection errors and
non-2xx answers (for the container call: missing `id`/`existing_container_id`).

## Polling example (SOAR fallback)

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "$MDT/tickets?updated_since=2026-11-24T14:00:00Z&sort=updated_at&limit=100"
```
