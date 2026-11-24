# MyDemoTicket – SOAR Playbooks

> **Status (2026-11-24):** `mydemoticket_es_mirror` is live-tested on Splunk SOAR 8.8 (Splunk Cloud) with
> Splunk ES: imported via `POST /rest/import_playbook`, active on label `mydemoticket`, a ticket change
> set the linked notable from New to In Progress automatically. `mydemoticket_ticket_from_es` is only
> syntax-checked (`python3 -m py_compile`, part of `soar-app/tests`).
>
> Import note: SOAR only validates and activates an imported playbook when `coa.python_version`
> ("3.13"), `coa.schema` and `coa.version` are set inside the `coa` block. Without them the import
> stays in draft mode ("Python linting and validation could not be run") and cannot be activated.

Both playbooks are classic Python playbooks (`on_start`, `phantom.collect2`, `phantom.act`,
`phantom.add_note`). The `.json` files only carry metadata (labels, trigger, type) and a minimal
start→end graph; the logic lives in the `.py` file, so do not edit them in the Visual Editor.

## mydemoticket_es_mirror (ticket → Splunk ES)
Label: `mydemoticket`. Runs on new artifacts.

1. Collects new artifacts with label `mydemoticket_event` (pushed by the ticket system).
2. Ignores artifacts whose `cef.source` is `soar` (change came from SOAR – loop prevention).
3. Target: `cef.esInvestigationId` (ES 8 investigation GUID) wins over `cef.esFindingId`.
   A missing `esInvestigationId` field (older ticket servers) is treated as empty.
4. **Notes** (every event with a non-empty `cef.comment`, i.e. comments and work notes):
   Enterprise Security app action **add finding or investigation note** on asset `ES_ASSET`
   (default `builtin_mc_connector`) with `id` = investigation GUID or finding id and
   `content` = `MyDemoTicket <number> – <type>/<author>: <text>`. Author is `cef.commentAuthor`,
   else `cef.actor`; type comes from `cef.commentType` (`comment`/`work_note`) and is left out when
   the ticket server does not send it.
5. **Status**, only on `ticket.created` / `ticket.state_changed` (comments and other updates do not
   touch the status). Mapping: new→1 New, in_progress→2 In Progress, on_hold→3 Pending,
   resolved→4 Resolved, closed/cancelled→5 Closed.
   - Finding: Splunk app action **update event** on asset `SPLUNK_ASSET` (default `splunk`) with
     `event_ids`, `integer_status` and `owner` = `cef.assignedTo` (switch off with
     `MIRROR_OWNER = False`). No `comment`: in ES 8 it would appear as an extra "Finding Update" note.
   - Investigation: only for resolved/closed/cancelled, ES app action **update finding or
     investigation** with `id` = GUID, `status` (4 or 5) and optional `disposition`
     (`INVESTIGATION_CLOSE_DISPOSITION`, e.g. `disposition:1`).
6. Notes and status are separate actions with separate callbacks: a failing note does not stop the
   status update. Every failure adds a note to the container.
7. Without `esInvestigationId` and `esFindingId`: a markdown note with the change is added to the
   container.

### ES 8 notes (why not `update event` comments)
ES 8 keeps the `update event` status, but the comment does not land in `incident_review_lookup`;
it only shows up as a note titled "Finding Update" written by the Splunk app user. Notes are now
written with the ES app (SOAR app "Enterprise Security", app ID 198 on the Cloud SOAR), which wraps
the ES 8 API "Create a note in a finding or investigation" (POST):
https://help.splunk.com/en/splunk-enterprise-security-8/api-reference/8.4/splunk-enterprise-security-api-reference/findings/public_v2_create_note_in_investigation
Verified live 2026-11-24 against finding `<guid>@@notable@@time<epoch>`: HTTP 201, note readable
via **get notes in finding or investigation**. ES stores `title` as the first content line, so
the playbook sends no title.

## mydemoticket_ticket_from_es (Splunk ES → ticket)
Label: `notable` (adjust to the label your Splunk app ingests ES notables with). Runs on new artifacts.

1. Takes the first new artifact with `cef.event_id`.
2. Calls MyDemoTicket action **create ticket** on asset `MYDEMOTICKET_ASSET` (default
   `mydemoticket`): `short_description` = `cef.rule_name` (or container name), `priority` from
   `cef.urgency` (critical 1, high 2, medium 3, low/informational 4), `es_finding_id` = `event_id`.
   The app sets `external_refs.soar_container_id` from the current container.
3. Adds a note with the new ticket number (or the error).

## Constants to set before import
| File | Constant | Default |
|---|---|---|
| `mydemoticket_es_mirror.py` | `SPLUNK_ASSET` | `splunk` |
| `mydemoticket_es_mirror.py` | `ES_ASSET` | `builtin_mc_connector` |
| `mydemoticket_es_mirror.py` | `MIRROR_OWNER` | `True` |
| `mydemoticket_es_mirror.py` | `INVESTIGATION_CLOSE_DISPOSITION` | `""` (not sent) |
| `mydemoticket_ticket_from_es.py` | `MYDEMOTICKET_ASSET` | `mydemoticket` |
| `mydemoticket_ticket_from_es.py` | `ASSIGNMENT_GROUP`, `CATEGORY` | `SOC L1`, `security_incident` |

## Known limits
- Parameter names are checked against the Cloud SOAR app schemas (`GET /rest/app_action?_filter_app=163`
  Splunk 4.0.2, `?_filter_app=198` Enterprise Security 6.8.0); check them after app upgrades.
- The investigation path (`esInvestigationId`, **update finding or investigation**) was verified live
  on 2026-11-24 (ES-00004 → status 5).
- On a state change to resolved/closed/cancelled (not from source `soar`) the playbook also adds a
  note "MyDemoTicket <number> geschlossen von <actor> (<state>): <close_notes>" to the investigation
  (or the finding). Needs a ticket server that sends `cef.closeNotes`; otherwise the note has no text part.
- Owner changes without a state change are not mirrored any more (no `update event` without a
  state change).
- `mydemoticket_ticket_from_es` does not check whether a ticket already exists for the container.
  It runs once per batch of new artifacts; several ES artifacts added later can create more tickets.

## Syntax check
```bash
python3 -m py_compile soar-playbooks/mydemoticket_es_mirror.py soar-playbooks/mydemoticket_ticket_from_es.py
```
