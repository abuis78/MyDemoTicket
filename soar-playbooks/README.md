# MyDemoTicket – SOAR Playbooks

> **Status: not tested against a real Splunk SOAR or Splunk ES instance.**
> Only checked with `python3 -m py_compile` (also part of `soar-app/tests`). Asset names, the
> Splunk app action parameters and the import of the `.json` files must be verified on a real
> system before use.

Both playbooks are classic Python playbooks (`on_start`, `phantom.collect2`, `phantom.act`,
`phantom.add_note`). The `.json` files only carry metadata (labels, trigger, type) and a minimal
start→end graph; the logic lives in the `.py` file, so do not edit them in the Visual Editor.

## mydemoticket_es_mirror (ticket → Splunk ES)
Label: `mydemoticket`. Runs on new artifacts.

1. Collects new artifacts with label `mydemoticket_event` (pushed by the ticket system).
2. Ignores artifacts whose `cef.source` is `soar` (change came from SOAR – loop prevention).
3. With `cef.esFindingId`: Splunk app action **update event** on asset `SPLUNK_ASSET`
   (default `splunk`) with
   - `event_ids` = `esFindingId`
   - `integer_status` from `ticketState`: new→1 New, in_progress→2 In Progress, on_hold→3 Pending,
     resolved→4 Resolved, closed/cancelled→5 Closed
   - `comment` = `MyDemoTicket <number> (<event>, state <state>): <cef.comment>`
   - `owner` = `cef.assignedTo` (switch off with `MIRROR_OWNER = False` if ticket users are not ES users)
   - On failure a note is added to the container.
4. Without `esFindingId`: a markdown note with the change is added to the container.

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
| `mydemoticket_es_mirror.py` | `MIRROR_OWNER` | `True` |
| `mydemoticket_ticket_from_es.py` | `MYDEMOTICKET_ASSET` | `mydemoticket` |
| `mydemoticket_ticket_from_es.py` | `ASSIGNMENT_GROUP`, `CATEGORY` | `SOC`, `security_incident` |

## Known limits
- `update event` parameter names (`event_ids`, `integer_status`, `comment`, `owner`) follow the
  Splunk app for SOAR; check them against the installed app version.
- `mydemoticket_ticket_from_es` does not check whether a ticket already exists for the container.
  It runs once per batch of new artifacts; several ES artifacts added later can create more tickets.

## Syntax check
```bash
python3 -m py_compile soar-playbooks/mydemoticket_es_mirror.py soar-playbooks/mydemoticket_ticket_from_es.py
```
