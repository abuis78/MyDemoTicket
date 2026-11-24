# MyDemoTicket

A small, ServiceNow-like ticketing system for Splunk SOAR / Splunk ES demos.
Pure Python standard library (Python 3.9+), SQLite, vanilla JS web UI – no
packages to install.

## Quick start

```bash
cd ticket-system
python3 -m mydemoticket --host 127.0.0.1 --port 8080
# open http://127.0.0.1:8080
```

On first start the data directory (`$MYDEMOTICKET_DATA_DIR` or `~/.mydemoticket`,
or `--data-dir`) is created with demo teams (SOC L1, SOC L2, IT Ops), users
(`admin`, `analyst1`, `analyst2`, `requester1`, `soar`) and custom fields.
Passwords and tokens are random and written to
`<data-dir>/initial-credentials.txt` (mode 0600) – never printed. Set
`MYDEMOTICKET_ADMIN_PASSWORD` / `MYDEMOTICKET_ADMIN_TOKEN` beforehand to choose
the admin credentials, or use `--no-seed` to create only the admin.

```bash
export TOKEN=...   # admin or soar API token from initial-credentials.txt
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/api/v1/tickets
```

## Tenants

MyDemoTicket is multi-tenant: every user (except `platform_admin`) belongs to one
tenant and only sees its tickets, teams, custom fields and webhooks. The default
tenant is `soc-games`; existing data is migrated into it on the first start of
this version (backup `mydemoticket.sqlite3.pre-v2.bak`), and `admin` becomes
`platform_admin`. A platform admin creates tenants with `POST /api/v1/tenants`
and selects one per request with the header `X-MyDemoTicket-Tenant: <slug>`.
Details: [docs/api.md](docs/api.md#tenants-multi-tenancy),
[docs/architecture.md](docs/architecture.md#tenant-model-multi-tenancy); audit
logging to Splunk: [docs/logging.md](docs/logging.md).

## Tests

```bash
cd ticket-system
python3 -m unittest discover -s tests -v > "${TMPDIR:-/tmp}/mdt-tests.txt" 2>&1; tail -3 "${TMPDIR:-/tmp}/mdt-tests.txt"
```

Success is the line `OK` after `Ran N tests`.

## End-to-end tests

`tests/e2e/` checks both directions between the ticket system and the SOAR app,
using only the standard library and 127.0.0.1:

```bash
bash scripts/e2e.sh 3 > "${TMPDIR:-/tmp}/mdt-e2e.txt" 2>&1; echo "exit $?"; grep "E2E run" "${TMPDIR:-/tmp}/mdt-e2e.txt"
```

Each run prints `E2E run i/N: OK`; the script stops with exit code 1 at the
first failed run. Every test class starts the real server
(`python3 -m mydemoticket`) on a free port with a fresh temporary data
directory, reads the generated credentials from `initial-credentials.txt`
(never printed) and registers two webhooks:

* a `soar` webhook pointing at `tests/e2e/soar_mock.py`, which emulates
  `POST /rest/container` (duplicate detection by `source_data_identifier`) and
  `POST /rest/artifact` and checks `ph-auth-token`;
* a `generic` webhook whose receiver verifies the HMAC signature.

Covered:

| Direction | What is checked |
|---|---|
| Ticket → SOAR | analyst via API and via web login + CSRF: create, priority, custom fields, `new → in_progress → resolved`, comment, work note; one container per ticket, one artifact per event with the right `cef` values, `soar_container_id` stored on the ticket, signed generic webhooks; `on poll` ingests the changed tickets |
| SOAR → Ticket | connector actions create/update ticket, set state, add comment/work note, get ticket, get journal, list tickets/custom fields; journal source `soar`, no echo to the SOAR webhook (`skipped`), generic webhook with source `soar`; `on poll` skips SOAR-made changes |
| Round trip | SOAR creates ticket, analyst changes it, push arrives with `esFindingId`; polled and pushed container agree; ES status mapping of the mirror playbook |
| Roles | requester never sees work notes; agent of another team gets 403 |

Not covered: a real Splunk SOAR or Splunk ES instance, the playbooks' runtime
behaviour (only their state → ES status mapping), and the browser UI itself
(the web path is exercised via the same session/CSRF API calls the UI makes).

## Structure

```
ticket-system/mydemoticket/   server (api, auth, db, webhooks, server, static web UI)
ticket-system/tests/          unittest suite (server on a random port, temp data dir)
docs/architecture.md          components, data flow ticket <-> SOAR <-> ES, roles, deployment
docs/api.md                   REST API reference with curl examples
docs/mockups/                 static HTML mockups of the UI
deploy/                       systemd + Caddy installer for ticket.soc-games.com (EC2)
soar-app/                     Splunk SOAR app for MyDemoTicket
tests/e2e/                    end-to-end tests ticket system <-> SOAR app (scripts/e2e.sh)
```

## Moving to another machine

Clone the repo and run the quick start. State lives only in the data directory;
copy it to keep tickets and users.
