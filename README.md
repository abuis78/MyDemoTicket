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

## Tests

```bash
cd ticket-system
python3 -m unittest discover -s tests -v > "${TMPDIR:-/tmp}/mdt-tests.txt" 2>&1; tail -3 "${TMPDIR:-/tmp}/mdt-tests.txt"
```

Success is the line `OK` after `Ran N tests`.

## Structure

```
ticket-system/mydemoticket/   server (api, auth, db, webhooks, server, static web UI)
ticket-system/tests/          unittest suite (server on a random port, temp data dir)
docs/architecture.md          components, data flow ticket <-> SOAR <-> ES, roles, deployment
docs/api.md                   REST API reference with curl examples
docs/mockups/                 static HTML mockups of the UI
deploy/                       systemd + Caddy installer for ticket.soc-games.com (EC2)
soar-app/                     Splunk SOAR app for MyDemoTicket
```

## Moving to another machine

Clone the repo and run the quick start. State lives only in the data directory;
copy it to keep tickets and users.
