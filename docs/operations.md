# MyDemoTicket – Operations

Day-2 tasks for a running MyDemoTicket: backup and restore, updates and schema
migrations, moving to a new machine, monitoring, troubleshooting and uninstall.
Installation: [installation.md](installation.md).

Host names, profiles and ids in examples are placeholders or examples from the
demo environment; use your own.

## Where the state lives

| State | Location | Notes |
|---|---|---|
| Database (all tenants, users, tokens, tickets, journal, webhooks incl. secrets) | `<data-dir>/mydemoticket.sqlite3` (+ `-wal`, `-shm`) | `<data-dir>` = `--data-dir`, `$MYDEMOTICKET_DATA_DIR` or `~/.mydemoticket`; on servers `/var/lib/mydemoticket` |
| Migration backups | `<data-dir>/mydemoticket.sqlite3.pre-v<N>.bak` | Written automatically before schema step N |
| Initial credentials | `<data-dir>/initial-credentials.txt` | Only after the first start; delete after storing |
| Local audit log | `<data-dir>/logs/audit.log` (+ `.1` … `.5`) | Always written |
| Service config (option B/C) | `/etc/mydemoticket/env` (0600) | Admin bootstrap and HEC variables; on AWS rewritten from SSM on every boot |
| Splunk connection set in the UI | table `settings` in the database | Since 1.1.0; wins over the `MYDEMOTICKET_HEC_*` variables; moves with the database |
| AWS config | SSM `/$NAME/*`, keychain service `mydemoticket` on your Mac | See [installation.md](installation.md#keychain-items-and-ssm-parameters) |
| SOAR side | SOAR app, asset (base URL + token), label, playbooks | Not stored in MyDemoTicket |
| MCP | keychain account `mcp-api-token`, Claude Code registration | Per workstation |

The code (`/opt/mydemoticket`, `/opt/$NAME-src` on AWS) holds no state.

## Backup and restore

The database runs in WAL mode, so copy the **whole data directory** while the
service is stopped, or use SQLite's online backup.

### AWS (deploy/aws/backup.sh)

```bash
export AWS_PROFILE=<your-profile>
deploy/aws/backup.sh --dry-run     # prints the command, no AWS call
deploy/aws/backup.sh               # waits for "OK: backup /var/lib/mydemoticket.bak-<UTC timestamp> on <instance-id>"
```

It finds the running instance tagged `Name=$NAME` (env `NAME`, default
`mydemoticket`; `AWS_REGION` default `eu-central-1`) and runs via SSM Run Command:
stop the service, `cp -a /var/lib/mydemoticket /var/lib/mydemoticket.bak-<ts>`,
start the service (also if the copy failed). Backups stay on the instance volume
(8 GB) and are lost with the instance – copy important ones off the instance and
delete old `.bak-*` directories by hand.

Restore on the instance (`aws ssm start-session --target <instance-id>`):

```bash
sudo systemctl stop mydemoticket
sudo mv /var/lib/mydemoticket /var/lib/mydemoticket.broken
sudo cp -a /var/lib/mydemoticket.bak-<ts> /var/lib/mydemoticket
sudo systemctl start mydemoticket
```

### Linux server (option B)

```bash
sudo systemctl stop mydemoticket
sudo tar czf mdt-backup-$(date -u +%Y%m%dT%H%M%SZ).tgz -C /var/lib mydemoticket
sudo systemctl start mydemoticket
```

Restore: stop the service, move the old directory away, `sudo tar xzf
mdt-backup-<ts>.tgz -C /var/lib`, make sure it is owned by `mydemoticket` and mode
0700 (`sudo chown -R mydemoticket:mydemoticket /var/lib/mydemoticket`), start the
service.

What else to keep with the backup (not inside the data directory): `/etc/mydemoticket/env`
(admin bootstrap values, HEC settings; root-only) and, for option C, the keychain items and SSM
parameters listed in [installation.md](installation.md#keychain-items-and-ssm-parameters).
The data directory itself contains `initial-credentials.txt` and `logs/audit.log`.

Scheduled backup (example, daily at 02:30, keeps 14 days) – `sudo crontab -e`:

```cron
30 2 * * * systemctl stop mydemoticket && tar czf /var/backups/mdt-$(date -u +\%Y\%m\%d).tgz -C /var/lib mydemoticket; systemctl start mydemoticket; find /var/backups -name 'mdt-*.tgz' -mtime +14 -delete
```

### Online copy without stopping (any option)

Python's `sqlite3` backup API (standard library) writes a consistent copy of the
database file while the server runs:

```bash
python3 -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' \
  /var/lib/mydemoticket/mydemoticket.sqlite3 /root/mydemoticket-$(date -u +%Y%m%dT%H%M%SZ).sqlite3
```

This copies only the database; the audit log and credentials file are not included.
Backups contain password hashes, token hashes and webhook secrets: keep them
private (0600).

### Local (option A)

Stop the server (`Ctrl+C`) and copy the data directory, e.g.
`cp -a ~/.mydemoticket ~/mydemoticket-backup-<date>`.

## Updates and schema migrations

| Option | Update |
|---|---|
| A – local | `git pull`, restart `python3 -m mydemoticket` |
| B – Linux server | backup, `git pull`, `sudo deploy/install.sh <same options as before>` (e.g. the same `--domain`/`--alb-port`; without them the defaults are used). Data and `/etc/mydemoticket/env` are kept |
| C – AWS | `deploy/aws/backup.sh`, `deploy/aws/deploy.sh` (same env as before), `aws ec2 reboot-instances --instance-ids <instance-id>`, `curl -fsS https://<host>/api/v1/health` |
| SOAR app | `python3 build.py`, install the new `dist/mydemoticket.tgz` over the old one; assets stay |
| Playbooks | re-import the `.tgz` (keep the asset constants you changed) |
| MCP server | `git pull` in the stable checkout; run `./install.sh` again only if the path changed |

Schema migrations run **automatically at start** (`Database.migrate()` in
`ticket-system/mydemoticket/db.py`); the version is `PRAGMA user_version`:

| Version | Change |
|---|---|
| 1 | Base schema: users, tokens, sessions, teams, tickets, journal, custom fields, webhooks, deliveries |
| 2 | Multi-tenancy: table `tenants`, `tenant_id` everywhere; existing data moves to tenant `soc-games`, user `admin` becomes `platform_admin` |
| 3 | `journal.author` (optional comment author), additive |

Rules: each step runs in its own transaction with foreign keys off and must pass
`PRAGMA foreign_key_check` before `COMMIT`; a failing step rolls back and the
server does not start. Before step N is applied to an existing database, a copy
`mydemoticket.sqlite3.pre-v<N>.bak` (0600) is written; an existing backup is never
overwritten. A new database gets no `.bak` files. A second start on a current
database does nothing.

Rolling back after a migration: stop the service, move
`mydemoticket.sqlite3`, `-wal` and `-shm` away, copy `mydemoticket.sqlite3.pre-v<N>.bak`
to `mydemoticket.sqlite3` and run the **previous code version** (a newer server
would migrate it again). Changes made after the migration are lost.

## Moving to a new machine, server or AWS account

What has to move:

| Item | How |
|---|---|
| Data directory | Backup as above, restore on the target; same ownership/mode (0700) |
| `/etc/mydemoticket/env` | Copy (0600, root) for option B; on AWS it is generated from SSM |
| Keychain items (service `mydemoticket`: `admin-password`, `admin-token`, `splunk-hec-token`, `mcp-api-token`) | Recreate on the new Mac with `security add-generic-password -s mydemoticket -a <account> -w`. `admin-password`/`admin-token` only matter for a fresh database; with a restored database the old admin password stays valid |
| SSM parameters `/$NAME/*` | Written by `deploy.sh` in the target account from the keychain |
| DNS | Point the host name to the new IP (direct) or ALB (CNAME) |
| SOAR | Update the asset `base_url` if the host name changed; tokens stay valid when the database moves. Webhook `url`/`secret` are in the database: recreate the webhook if SOAR moved |
| Splunk | HEC token / URL in the env (B) or keychain + SSM (C); a connection set in the UI lives in the database |
| MCP | Store `mcp-api-token` in the keychain and run `mcp-server/install.sh` again (absolute path) |

### Checklist: server → new server (option B)

1. On the old server: `sudo systemctl stop mydemoticket`, `sudo tar czf mdt-backup.tgz -C /var/lib mydemoticket`,
   copy `mdt-backup.tgz` and `/etc/mydemoticket/env` to the new server (scp / your tooling).
2. New server: install Python 3.9+ and Caddy, clone the repo.
3. `sudo deploy/install.sh --domain <host>` (creates user, unit, Caddy site; starts an empty service).
4. `sudo systemctl stop mydemoticket`, `sudo rm -rf /var/lib/mydemoticket`,
   `sudo tar xzf mdt-backup.tgz -C /var/lib`, `sudo chown -R mydemoticket:mydemoticket /var/lib/mydemoticket`,
   `sudo chmod 700 /var/lib/mydemoticket`; put the old env file to `/etc/mydemoticket/env` (0600).
5. `sudo systemctl start mydemoticket`, switch DNS, check `curl -s https://<host>/api/v1/health`.
6. SOAR: *Test Connectivity* on the asset; create a test ticket and check the webhook deliveries.
7. Stop or uninstall the old server.

### Checklist: AWS → new AWS account (option C)

1. Old account: `deploy/aws/backup.sh`, then copy the backup off the instance (e.g. tar it on the
   instance in a Session Manager shell and transfer it with your usual tooling).
2. New Mac (if changed): clone the repo, recreate the keychain items.
3. New account: `aws sso login --profile <new-profile>`, `export AWS_PROFILE=<new-profile>`, set `DOMAIN`,
   `ALB_NAME`, `HEC_URL`, `EXTRA_TAGS` for that account, `deploy/aws/deploy.sh --dry-run`, then
   `deploy/aws/deploy.sh`.
4. Restore the data directory on the new instance (restore steps above), start the service.
5. DNS: `A` record to the new IP, or `CNAME` to the new ALB.
6. SOAR asset `base_url`, MCP `MYDEMOTICKET_URL` if the host name changed.
7. Old account: `deploy/aws/deploy.sh --uninstall` with the old profile.

### Checklist: local → other workstation

Copy the data directory (server stopped), clone the repo, start with the same `--data-dir`.
For the MCP server: keychain item + `mcp-server/install.sh`.

## Monitoring

| What | How |
|---|---|
| Liveness | `curl -s https://<host>/api/v1/health` → `{"status": "ok", "version": "1.0.0"}` (no auth; also the ALB target-group health check) |
| Audit / HEC | `GET /api/v1/audit/status` (admin): `hec_enabled`, `queued`, `sent`, `failed`, `dropped`, `local_written`, `last_error`, `last_success_at` |
| Webhooks | `GET /api/v1/webhooks/<id>/deliveries` (last 50: `pending`, `retrying`, `success`, `failed`, `skipped`) or `#/admin/webhooks` |
| Tickets | `GET /api/v1/reports/summary` or `#/reports` |
| Splunk | Dashboards *MyDemoTicket - Audit* (failed logins, denied access, webhook deliveries) and *MyDemoTicket - Ticket report* (created/resolved, MTTR) |
| Service logs | `sudo journalctl -u mydemoticket -u caddy`; on AWS also `-u mydemoticket-boot` |
| Local audit log | `<data-dir>/logs/audit.log`, e.g. `jq -c 'select(.outcome!="success")' /var/lib/mydemoticket/logs/audit.log` |

```bash
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" https://<host>/api/v1/audit/status
```

Splunk search for webhook problems:

```
index=mydemoticket sourcetype=mydemoticket:audit action=webhook.delivery outcome!=success
| stats count by tenant outcome details.event
```

### Web UI for users (1.1.0)

| Task | Where |
|---|---|
| Own profile: display name, e-mail, password, time zone, light/dark theme, language (Deutsch/English) | Click on your name top right (`#/profile`); saved on the server, applies at once |
| Set another user's password (entered twice), edit display name / e-mail, change role | `#/admin/users` |
| Roles: view all, create / edit / delete custom roles (analyst permissions only) | `#/admin/roles` (admins) |
| Splunk HEC connection: URL, index, token (write-only), TLS check, on/off, test event | `#/admin/audit` (platform admins); the saved connection wins over `MYDEMOTICKET_HEC_*` and applies without restart. Empty token field keeps the stored token |

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `401` on every API call | Token wrong or revoked; tokens are shown only once – create a new one (`POST /users/<user>/tokens`) |
| `403 {"error": "tenant mismatch"}` | `X-MyDemoTicket-Tenant` names a tenant other than the user's own. Remove the header (normal users) or use a token of a user in that tenant (SOAR asset, MCP `MYDEMOTICKET_TENANT`) |
| `403 {"error": "tenant inactive"}` | Tenant deactivated: `PATCH /tenants/<slug>` `{"active": true}` as platform admin |
| `404` for a ticket/user that exists | It belongs to another tenant (never `403`). A platform admin has to send `X-MyDemoTicket-Tenant: <slug>`, e.g. when creating a token for a user of another tenant |
| `409` on a state change | Transition not allowed (e.g. `new → closed`); see the state machine in [api.md](api.md#state-transitions) |
| `422` when resolving | `close_notes` missing |
| SOAR test connectivity: `API token role '...' is not allowed` | The asset uses a `platform_admin` or `requester` token; use an `integration`, `agent` or `admin` token of the tenant |
| SOAR *create ticket* from ES fails with `HTTP 422` | `ASSIGNMENT_GROUP` in `mydemoticket_ticket_from_es.py` (default `SOC L1`) is not a team of the tenant |
| Playbook import stays in draft mode ("Python linting and validation could not be run") | `coa.python_version`, `coa.schema` or `coa.version` missing in the playbook `.json`; import the files from this repo unchanged |
| Playbook does not run | Not active, or the container label is not `mydemoticket`, or the artifact label is not `mydemoticket_event` |
| Webhook delivery `skipped` with "change originated from SOAR (loop prevention)" | Expected: the change came with `X-MyDemoTicket-Source: soar` (e.g. from the SOAR app) |
| Webhook delivery `failed` | Check `error`/`response_code` in the deliveries: wrong SOAR URL, invalid `ph-auth-token` (secret), missing label `mydemoticket` in SOAR, TLS (`verify_tls`) |
| ES notes written by the ES asset user instead of the analyst | ES records the SOAR ES app's service account as note author. The real author is in the note text: `MyDemoTicket <number> – <type>/<author>: <text>` (author = journal `author`, else `actor`) |
| ES finding status does not change on comments | By design: only `ticket.created` and `ticket.state_changed` update the status; comments become notes |
| `/audit/status` shows `last_error: "HEC HTTP 403: ..."` (Splunk answers "Invalid token") | Wrong or disabled HEC token; fix it (env file or keychain + `deploy.sh` + reboot). 4xx batches are counted as `failed` and dropped from HEC, the events remain in `logs/audit.log` |
| `last_error: "HEC HTTP 400: ..."` mentioning the index | Token not allowed to write to `MYDEMOTICKET_HEC_INDEX`; add the index to the token's allowed indexes |
| `last_error: "HEC network error: ..."` | URL/port/firewall/TLS; retried with backoff up to 60 s |
| Tenant role in Splunk sees nothing / too much | Events from a Universal Forwarder (local file) carry `tenant` only in JSON, not as indexed field; roles must inherit only from `user` |
| ALB target `unhealthy` | Instance port `$ALB_PORT` must be open from the ALB security group (`deploy.sh` adds it), Caddy must have the `:$ALB_PORT` listener (SSM parameter `/$NAME/ALB_PORT`), `GET /api/v1/health` must answer 200; check `journalctl -u mydemoticket-boot -u caddy` |
| `deploy.sh` prints "Not healthy after 10 minutes" | First boot (dnf, Docker, certificate) can take long; then `aws ssm start-session --target <instance-id>` and `sudo journalctl -u mydemoticket-boot -u mydemoticket -u caddy` |
| `run-instances` fails with `UnauthorizedOperation` | Tags required by a service control policy are missing: set `EXTRA_TAGS` |
| sslip.io name / `A` record stopped working | Public IP changed after stop/start (no Elastic IP); update DNS and the SOAR asset |
| Web login succeeds but the UI shows the login again | `--secure-cookies` over plain HTTP: the browser does not send a `Secure` cookie. Use HTTPS, or drop the option for local use |

## Uninstall

### Linux server (option B)

```bash
sudo deploy/install.sh --uninstall                 # removes unit, code, managed env file and Caddy files; keeps data
sudo deploy/install.sh --uninstall --purge-data    # also deletes /var/lib/mydemoticket and the system user
```

Back up first if you might need the data. If your own Caddyfile imports
`/etc/caddy/mydemoticket.caddy`, remove that line. An env file you edited yourself
(without the `# managed by mydemoticket` marker) is kept.

### AWS (option C)

```bash
export AWS_PROFILE=<your-profile>
deploy/aws/deploy.sh --uninstall
```

Deletes the ALB rule and target group (with `ALB_NAME` set), the instance (**data on
the instance is lost**, including `.bak-*` backups), the security group, IAM role and
instance profile, all `/$NAME/*` SSM parameters and the bucket. Keychain items stay;
remove them with `security delete-generic-password -s mydemoticket -a <account>`. Delete
the DNS record yourself.

### SOAR, Splunk, MCP

1. SOAR: deactivate and delete the playbooks `mydemoticket_es_mirror` and
   `mydemoticket_ticket_from_es`, delete the MyDemoTicket asset, uninstall the app,
   delete the label `mydemoticket` if unused, revoke the automation token used as webhook
   secret.
2. MyDemoTicket (if it keeps running): delete the webhooks (`DELETE /webhooks/<id>`) and
   revoke the integration tokens (`DELETE /users/<user>/tokens/<id>`).
3. Splunk: disable the HEC token; remove dashboards, roles and – if no longer needed – the index.
4. MCP: `mcp-server/install.sh --uninstall`, then
   `security delete-generic-password -s mydemoticket -a mcp-api-token`.
5. Local: stop the server and delete the data directory (`~/.mydemoticket` by default).
