# MyDemoTicket – Splunk SOAR App

## 1. Overview
Connector for the MyDemoTicket demo ticket system (`/api/v1`). It creates, reads, updates and
comments tickets from SOAR playbooks and ingests changed tickets into SOAR via **on poll**.
Code uses only the Python standard library (`urllib`) plus the SOAR `phantom` modules.

## 2. Prerequisites
- Splunk SOAR (On-Premises) 6.0.0 or newer
- An API token of a MyDemoTicket user with role `integration`, `agent` or `admin`
- Network access from SOAR to the ticket system (HTTPS recommended)

## 3. Binary/System Installation
None. No pip dependencies, no system binaries.

## 4. App Installation
```bash
cd soar-app
python3 build.py            # -> soar-app/dist/mydemoticket.tgz
```
SOAR: **Apps → Install App** → upload `dist/mydemoticket.tgz`. The archive root is `mydemoticket/`.

### Asset configuration
| Field | Type | Required | Default | Purpose |
|---|---|---|---|---|
| `base_url` | string | yes | – | e.g. `https://ticket.soc-games.com` (without `/api/v1`) |
| `api_token` | password | yes | – | Bearer token |
| `verify_server_cert` | boolean | no | `true` | TLS certificate verification |
| `source_tag` | string | no | `soar` | Sent as `X-MyDemoTicket-Source` on every write (loop prevention) |
| `ingest_label` | string | no | `mydemoticket` | Label of containers created by on poll |
| `poll_state_filter` | string | no | – | Only poll tickets in this state |

## 5. Test Connectivity
Calls `GET /health` (must return `status: ok`) and `GET /me` (field `role` must be
`integration`, `agent` or `admin`).

## 6. Actions
All requests use a 30 s timeout. HTTP errors are returned as failed action with
`HTTP <status>: <error text>`; invalid JSON responses and connection errors fail as well.

| Action | Parameters | Main output |
|---|---|---|
| test connectivity | – | `summary.version`, `summary.role` |
| create ticket | `short_description`*, `description`, `priority` (1–4), `assignment_group`, `assigned_to`, `category`, `custom_fields` (JSON object string), `es_finding_id`, `es_investigation_id` | ticket; `external_refs.soar_container_id` is set automatically from the current container |
| get ticket | `number`* | ticket |
| update ticket | `number`*, all ticket fields optional, `custom_fields` (JSON, merged), `es_finding_id`, `es_investigation_id` (merged into `external_refs`) | ticket |
| set state | `number`*, `state`* (`new`, `in_progress`, `on_hold`, `resolved`, `closed`, `cancelled`), `close_notes` | ticket |
| add comment | `number`*, `body`*, `type` (`comment` \| `work_note` \| `summary`, default `comment`), `author` (1–64 chars, e.g. the ES analyst) | journal entry (`actor` = token user, `author` or null) |
| list tickets | `state`, `assignment_group`, `assigned_to`, `updated_since` (ISO-8601 UTC `Z`), `q`, `external_ref_key` + `external_ref_value` (both or neither), `limit` (default 100), `offset` | tickets, `summary.total`, `summary.returned` |
| get journal | `number`* | journal entries (incl. `author`), `summary.entries` |
| list custom fields | – | definitions (`name`, `label`, `type`, `choices`, `required`) |
| on poll | `container_count` (Poll Now) | containers + artifacts |

Ticket data paths (`action_result.data.*.`): `number`, `id`, `short_description`, `description`,
`state`, `priority`, `assignment_group`, `assigned_to`, `caller`, `category`,
`external_refs.soar_container_id`, `external_refs.es_finding_id`, `external_refs.es_investigation_id`, `created_at`, `updated_at`,
`resolved_at`, `close_notes`. The full list is in `mydemoticket.json`.

**Find before create (idempotent playbooks):** `list tickets` with `external_ref_key = es_finding_id`
and `external_ref_value = <finding id>` sends `external_ref.es_finding_id=<id>` and returns exactly
the tickets that carry this reference (stored numbers match their text, `1133` = `"1133"`). Create a
ticket only when `summary.total` is 0.

**Comment author:** `add comment` with `author` keeps the API user as `actor` (e.g. `soar`) and
stores the real person separately; the ticket UI shows `jdoe (via soar)`. Pushed SOAR artifacts of
`ticket.commented` carry it as CEF `commentAuthor`.

**Summary:** `add comment` with `type = summary` writes an AI or resolution summary. It is visible
like a comment (also to the requester) and shown in the ticket UI tab "Resolution report" (newest
summary first, older ones collapsed). Pushed artifacts carry `commentType = summary`.
Needs ticket system with journal type `summary`.

**ES investigation:** `es_investigation_id` is stored in `external_refs.es_investigation_id`
(update ticket merges it, other refs stay). Pushed SOAR artifacts carry it as CEF
`esInvestigationId`.

## Sync concept
Two channels keep SOAR and the ticket system in sync:

1. **Push from the ticket system** (built by the ticket system, not this app): on every change it
   creates/updates a container (`source_data_identifier` = ticket number, label `mydemoticket`) and
   adds an artifact with label `mydemoticket_event` (`run_automation: true`). The playbook
   `soar-playbooks/mydemoticket_es_mirror.py` reacts to these artifacts.
2. **Pull by this app (on poll)**, as fallback/backfill: fetches tickets with
   `updated_since = last_poll` (stored in the asset state), paged by 100. Per ticket it saves a
   container (`source_data_identifier` = ticket number; SOAR de-duplicates, so a second poll adds to
   the existing container) and one artifact `ticket snapshot` (label `mydemoticket_snapshot`,
   `source_data_identifier` = `<number>:<updated_at>`, `run_automation: false`).
   CEF fields: `ticketNumber`, `ticketState`, `ticketPriority`, `shortDescription`,
   `assignmentGroup`, `assignedTo`, `caller`, `category`, `updatedAt`, `customFields` (JSON string),
   `esFindingId`, `esInvestigationId`.
   - Poll Now ingests at most `container_count` tickets, ignores and does not change `last_poll`.
   - Scheduled poll: the first run ingests all tickets; later runs skip a ticket when the newest
     journal entry has `source` equal to `source_tag` (the change came from SOAR). If the journal
     has no `source` field or cannot be read, the ticket is ingested anyway.
   - `last_poll` is the UTC start time of the poll and is only saved when the poll succeeded.

**Loop prevention:** every write of this app sends `X-MyDemoTicket-Source: <source_tag>`. The ticket
system records it as journal source; playbooks ignore events from source `soar`.

## 7. Widget Display
No custom widget. All actions use the standard table render.

## 8. Local Testing
Unit tests run against a local mock of the API and a `phantom` stub (no SOAR needed):
```bash
cd soar-app
python3 -m unittest discover -s tests -v
```
On a SOAR host a single action can be run with a test JSON file:
```bash
phenv python3 mydemoticket_connector.py /tmp/test_get_ticket.json
```
```json
{"identifier": "get_ticket", "parameters": [{"number": "TKT0001001"}],
 "config": {"base_url": "https://ticket.soc-games.com", "api_token": "<token>"}}
```

## 9. Troubleshooting
| Message | Cause / fix |
|---|---|
| `HTTP 401: ...` | Token wrong or expired – check `api_token` |
| `API token role '...' is not allowed` | Use a token of an integration/agent/admin user |
| `HTTP 409: ...` | State transition not allowed by the ticket workflow |
| `HTTP 422: ...` | Validation error (e.g. missing short description, `author` longer than 64 chars) |
| `Parameters external_ref_key and external_ref_value must be given together` | Set both or none |
| `Parameter external_ref_key must match [a-z0-9_]{1,64}` | Use a lower-case key such as `es_finding_id` |
| `Parameter custom_fields is not valid JSON` | Pass a JSON object string, e.g. `{"k": "v"}` |
| `Connection error` / `Timeout after 30 s` | Network, DNS, proxy or TLS – check `base_url`, `verify_server_cert` |

## 10. Changelog
- 1.2.0 (2026-11-24): `add comment` `type` also `summary`; `create ticket` / `update ticket` optional
  `es_investigation_id` (external_refs); data path `external_refs.es_investigation_id`; poll snapshot
  CEF `esInvestigationId`. Not yet tested on a real SOAR instance.
- 1.1.0 (2026-11-24): `add comment` optional `author`; `list tickets` optional `external_ref_key` /
  `external_ref_value` (needs ticket system with `external_ref.<key>` filter); data paths `author`,
  `actor`. Not yet tested on a real SOAR instance.
- 1.0.0 (2026-11-24): First version – 10 actions incl. on poll. Not yet tested on a real SOAR instance.
