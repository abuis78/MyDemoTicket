# MyDemoTicket – Installation and integration

This guide takes you from an empty machine to a running MyDemoTicket that is
connected to Splunk SOAR, Splunk Enterprise Security (ES), Splunk (audit log via
HEC) and, optionally, Claude Code / Claude Desktop (MCP server).

Names such as `ticket.soc-games.com`, `soar.soc-games.com`,
`splunk.soc-games.com`, the AWS profile `janus-env2` or the load balancer
`alb-aws` are **examples from the demo environment** (and some of them are script
defaults). Replace them with your own values. Placeholders: `<TOKEN>`,
`$TOKEN`, `<instance-id>` and so on – never paste real tokens into files or
tickets.

Contents:

1. [Prerequisites](#1-prerequisites)
2. [Option A – local](#2-option-a--local)
3. [Option B – own Linux server (systemd + Caddy)](#3-option-b--own-linux-server-systemd--caddy)
4. [Option C – AWS EC2 (deploy/aws/deploy.sh)](#4-option-c--aws-ec2-deployawsdeploysh)
5. [Configuration reference](#5-configuration-reference)
6. [First login and initial credentials](#6-first-login-and-initial-credentials)
7. [Tenants](#7-tenants)
8. [Integration user and API token](#8-integration-user-and-api-token)
9. [Connect Splunk SOAR](#9-connect-splunk-soar)
10. [ES mirror playbook](#10-es-mirror-playbook)
11. [Splunk: audit log via HEC, dashboards, roles](#11-splunk-audit-log-via-hec-dashboards-roles)
12. [MCP server (Claude Code / Claude Desktop)](#12-mcp-server-claude-code--claude-desktop)
13. [Verification after installation](#13-verification-after-installation)

## 1. Prerequisites

| For | You need |
|---|---|
| Ticket system (every option) | Python 3.9 or newer. No packages, no internet access at run time |
| Option B (Linux server) | Linux with systemd, root (sudo), Python 3.9+, [Caddy](https://caddyserver.com/docs/install) as systemd service `caddy`, a DNS name pointing to the server, ports 80 and 443 reachable (ACME challenge) |
| Option C (AWS) | macOS workstation (the script uses the `security` keychain tool), a git checkout of this repo, AWS CLI v2, `python3`, `curl`, `tar`, `gzip`; an AWS profile (e.g. SSO) allowed to manage EC2, IAM roles/instance profiles, S3, SSM Parameter Store and (for the ALB variant) Elastic Load Balancing; a default VPC in the region |
| SOAR integration | Splunk SOAR 6.0.0 or newer (`min_phantom_version`), rights to install apps, configure assets, import playbooks and create labels; an automation user with a `ph-auth-token` |
| ES mirror | Splunk ES connected to SOAR with the SOAR apps "Splunk" (asset, action `update event`) and "Enterprise Security" (asset, actions `add finding or investigation note`, `update finding or investigation`) |
| Audit log in Splunk | Rights to create an index, a HEC token, knowledge objects and roles |
| MCP server | Python 3.9+, Claude Code (`claude` CLI) or Claude Desktop; on macOS the keychain for the token |

Get the code:

```bash
git clone <repo-url> MyDemoTicket
cd MyDemoTicket
```

## 2. Option A – local

```bash
cd ticket-system
python3 -m mydemoticket --host 127.0.0.1 --port 8080
# MyDemoTicket 1.0.0 listening on http://127.0.0.1:8080 (data: ~/.mydemoticket)
```

* Data directory: `--data-dir`, else `$MYDEMOTICKET_DATA_DIR`, else `~/.mydemoticket`.
* Open `http://127.0.0.1:8080` and log in (see [section 6](#6-first-login-and-initial-credentials)).
* Plain HTTP is fine on `127.0.0.1`. Never expose the port directly to a network:
  put a TLS proxy in front (option B) and use `--secure-cookies`.
* Stop with `Ctrl+C`. Everything (database, credentials file, audit log) is in the
  data directory.

## 3. Option B – own Linux server (systemd + Caddy)

`deploy/install.sh` copies the package to `/opt/mydemoticket`, creates the system
user `mydemoticket`, the data directory `/var/lib/mydemoticket` (0700), the env
file `/etc/mydemoticket/env` (0600, only if missing), a hardened systemd unit and a
Caddy site, then enables and restarts both services. It is idempotent.

1. Install Caddy so that `caddy` is on `PATH` and runs as systemd service `caddy`.
2. Point DNS (`A`/`AAAA` record) for your host name to the server. Open 80 and
   443, keep the app port (8080) closed.
3. Preview, then install:

   ```bash
   deploy/install.sh --dry-run --domain ticket.example.com      # renders into a new temp dir, prints the path
   sudo deploy/install.sh --domain ticket.example.com
   curl -s https://ticket.example.com/api/v1/health
   sudo cat /var/lib/mydemoticket/initial-credentials.txt     # once, then store in a password manager and delete
   ```

### install.sh options

| Option | Default | Meaning |
|---|---|---|
| `--domain NAME[,NAME...]` | `ticket.soc-games.com` | Public host name(s); several names become one Caddy site address list |
| `--port N` | `8080` | Local port of the app (bound to 127.0.0.1) |
| `--user NAME` | `mydemoticket` | System user |
| `--install-dir DIR` | `/opt/mydemoticket` | Code location |
| `--data-dir DIR` | `/var/lib/mydemoticket` | SQLite database, credentials file, audit log |
| `--python PATH` | first `python3` on `PATH` | Interpreter (must be 3.9+) |
| `--alb-port N` | off | Extra plain-HTTP Caddy listener `:N` for a load balancer that terminates TLS (allow that port only from the load balancer) |
| `--root DIR` | – | Prefix for all written files (testing); a temp dir is implied with `--dry-run` |
| `--dry-run` | – | Change nothing on the system, render into a temp dir |
| `--uninstall` | – | Remove unit, code, managed env file and managed Caddy files; data is kept |
| `--purge-data` | – | With `--uninstall`: also delete the data directory and the system user |

Notes:

* If `/etc/caddy/Caddyfile` exists and was not written by the installer, it is
  kept; the site goes to `/etc/caddy/mydemoticket.caddy` and the installer prints
  the line `import /etc/caddy/mydemoticket.caddy` you have to add.
* The service runs `python3 -m mydemoticket --host 127.0.0.1 --port <port>
  --data-dir <data-dir> --secure-cookies` and reads `/etc/mydemoticket/env`
  (`EnvironmentFile`). `install.sh` never overwrites an existing env file.
* Fixed admin credentials (optional): put `MYDEMOTICKET_ADMIN_PASSWORD=...` and
  `MYDEMOTICKET_ADMIN_TOKEN=...` into `/etc/mydemoticket/env` **before the first
  start**; they are only read while the database has no users.
* Audit log to Splunk: add the `MYDEMOTICKET_HEC_*` variables
  ([section 5](#5-configuration-reference)) to the env file and run
  `sudo systemctl restart mydemoticket`.
* Behind your own load balancer (TLS on the LB): `sudo deploy/install.sh --domain
  <name> --alb-port 8081`, then forward the LB target to port 8081 and health-check
  `/api/v1/health`.

## 4. Option C – AWS EC2 (deploy/aws/deploy.sh)

`deploy/aws/deploy.sh` runs on your Mac and creates, all named `$NAME`
(default `mydemoticket`) and tagged `Name=$NAME`, `app=mydemoticket` plus
`EXTRA_TAGS`:

* IAM role + instance profile (`AmazonSSMManagedInstanceCore`, read `/$NAME/*` SSM
  parameters and the code bundle),
* security group: 80 and 443 from anywhere, no SSH (access via SSM Session Manager),
* private S3 bucket `$NAME-deploy-<account>-<region>` with the code bundle
  (`ticket-system/mydemoticket` + `deploy/`),
* SSM parameters (see below),
* one EC2 instance (Amazon Linux 2023 arm64, default `t4g.nano`, gp3 8 GB
  encrypted, IMDSv2 required). User data adds 1 GB swap, installs Docker, runs
  Caddy as container `caddy:2.8.4` and the boot unit `mydemoticket-boot.service`.

On **every boot** `deploy/aws/boot.sh` downloads the bundle, rewrites
`/etc/mydemoticket/env` from SSM, derives `<public-ip-with-dashes>.sslip.io` from
the current public IP and runs `install.sh`. Data in `/var/lib/mydemoticket` is kept.

> Creating these resources costs money (rough estimate in `deploy/README.md`).

### Steps

```bash
aws sso login --profile <your-profile>            # example: janus-env2
export AWS_PROFILE=<your-profile>
export AWS_REGION=eu-central-1                    # default if unset
export DOMAIN=ticket.example.com                  # or "none" for the sslip.io name only
export ALB_NAME=none                              # or the name of your existing ALB, see below
export HEC_URL=none                               # or https://<splunk-host>:8088, see section 11
# export EXTRA_TAGS="key1=value1,key2=value2"     # tags required by an SCP in your organization

deploy/aws/deploy.sh --dry-run                    # read-only: checks and plan
deploy/aws/deploy.sh                              # create or update (idempotent)
```

> **Always set `DOMAIN`, `ALB_NAME` and `HEC_URL` explicitly.** The script defaults are the
> values of the demo environment (`ticket.soc-games.com`, ALB `alb-aws`, HEC on
> `splunk.soc-games.com`); without your own values it would look for a load balancer and a
> Splunk that do not exist in your account. `ALB_NAME=none` and `HEC_URL=none` switch both off.

The script waits up to 10 minutes for `https://<ip>.sslip.io/api/v1/health` and
prints the instance id, public IP and the DNS record to set.

### Environment variables of deploy.sh

| Variable | Default | Meaning |
|---|---|---|
| `AWS_PROFILE` | – | AWS CLI profile |
| `AWS_REGION` | `eu-central-1` | Region |
| `NAME` | `mydemoticket` | Name/tag of all resources and SSM prefix `/$NAME/` |
| `INSTANCE_TYPE` | `t4g.nano` | Instance type (arm64) |
| `DOMAIN` | `ticket.soc-games.com` | Public host name; `none` = only `<ip>.sslip.io` |
| `ALB_NAME` | `alb-aws` | Existing Application Load Balancer that serves `$DOMAIN`; `none` = no load balancer |
| `ALB_PORT` | `8081` | Instance port the ALB forwards to (plain HTTP, only from the ALB security group) |
| `ALB_RULE_PRIORITY` | `40` | Priority of the host-header rule on the ALB's 443 listener (must be free) |
| `EXTRA_TAGS` | – | Extra tags `k=v,k=v` on instance, volume and security group (e.g. tags required by a service control policy) |
| `HEC_URL` | `https://splunk.soc-games.com:8088` | Splunk HEC base URL; `none` = HEC off |
| `HEC_INDEX` | `mydemoticket` | Target index |

**Required tags:** if your organization enforces tags with a service control
policy, `run-instances` fails with `UnauthorizedOperation` unless you pass them via
`EXTRA_TAGS`. Example from the demo account:
`EXTRA_TAGS="splunkit_environment_type=non-prd,splunkit_data_classification=private"`.

### Host name: ALB or direct

| Variant | Settings | TLS | DNS |
|---|---|---|---|
| Direct (no load balancer) | `ALB_NAME=none`, `DOMAIN=ticket.example.com` | Caddy on the instance gets Let's Encrypt certificates for `$DOMAIN` and the sslip.io name | `A` record `$DOMAIN` → public IP of the instance |
| Existing ALB | `ALB_NAME=<your-alb>`, `DOMAIN=ticket.example.com` | ALB 443 listener with an ACM certificate covering `$DOMAIN`; the script adds a host rule → target group `$NAME-$ALB_PORT` (health check `/api/v1/health`) → instance port `$ALB_PORT` | `CNAME` `$DOMAIN` → ALB DNS name (printed by the script) |
| sslip.io only | `DOMAIN=none` | Caddy, `<ip-with-dashes>.sslip.io` | none needed |

The sslip.io name always works (Caddy serves it on 443). The script does not
allocate an Elastic IP: the public IP – and with it the sslip.io name and a direct
`A` record – changes after a stop/start (not after a reboot). Update DNS and the
SOAR asset `base_url` then. The ALB variant does not depend on the IP.

Test the ALB route before DNS exists:

```bash
curl --connect-to ticket.example.com:443:<alb-dns-name>:443 https://ticket.example.com/api/v1/health
```

### Keychain items and SSM parameters

| Keychain (service `mydemoticket`), account | Created by | Copied to SSM |
|---|---|---|
| `admin-password` | `deploy.sh` (random, if missing) | `/$NAME/ADMIN_PASSWORD` (SecureString) |
| `admin-token` | `deploy.sh` (random, if missing) | `/$NAME/ADMIN_TOKEN` (SecureString) |
| `splunk-hec-token` | you (optional) | `/$NAME/HEC_TOKEN` (SecureString) |

Other parameters: `/$NAME/DOMAIN`, `/$NAME/ALB_PORT` (`none` without ALB),
`/$NAME/HEC_URL` (`none` = off), `/$NAME/HEC_INDEX`. Secret values go to SSM via a
temporary 0600 JSON file, never on the command line or in output.

```bash
security add-generic-password -s mydemoticket -a splunk-hec-token -w      # prompts for the HEC token
security find-generic-password -s mydemoticket -a admin-password -w       # admin password for the web login
```

If `splunk-hec-token` is missing, `deploy.sh` prints a warning, writes
`HEC_URL=none`, deletes `HEC_TOKEN` and continues.

### Updating code or parameters

`deploy.sh` uploads a new bundle and rewrites the parameters, but a running
instance only picks them up on its next boot:

```bash
deploy/aws/backup.sh                                 # 1. copy the data dir on the instance (see docs/operations.md)
deploy/aws/deploy.sh                                 # 2. new bundle + parameters
aws ec2 reboot-instances --instance-ids <instance-id>   # 3. boot.sh reinstalls
curl -fsS https://ticket.example.com/api/v1/health   # 4. smoke test
```

The admin credentials in SSM only matter for the first start (empty database).
On AWS `initial-credentials.txt` therefore holds only the seed users; read it via
Session Manager: `aws ssm start-session --target <instance-id>`, then
`sudo cat /var/lib/mydemoticket/initial-credentials.txt`.

## 5. Configuration reference

### `python3 -m mydemoticket` (run from `ticket-system/` or the install dir)

| Option | Environment variable | Default | Meaning |
|---|---|---|---|
| `--host` | `MYDEMOTICKET_HOST` | `127.0.0.1` | Bind address |
| `--port` | `MYDEMOTICKET_PORT` | `8080` | Port |
| `--data-dir` | `MYDEMOTICKET_DATA_DIR` | `~/.mydemoticket` | Database, credentials file, audit log |
| `--no-seed` | – | off | First start creates only `admin` (and the default tenant), no demo teams/users/fields |
| `--secure-cookies` | `MYDEMOTICKET_SECURE_COOKIES=1` | off | Mark the session cookie `Secure` (always behind HTTPS). The cookie is also `Secure` when the request has `X-Forwarded-Proto: https` |
| `--quiet` | – | off | Do not log requests to stderr |
| `--version` | – | – | Print the version and exit |

Further environment variables:

| Variable | Read | Default | Meaning |
|---|---|---|---|
| `MYDEMOTICKET_ADMIN_PASSWORD` | first start only (no users in the DB) | random | Password of `admin` |
| `MYDEMOTICKET_ADMIN_TOKEN` | first start only | random | API token of `admin` |
| `MYDEMOTICKET_HEC_URL` | every start | unset = HEC off | HEC base URL, e.g. `https://splunk.example.com:8088`; `/services/collector/event` is appended unless the URL contains `/services/collector` |
| `MYDEMOTICKET_HEC_TOKEN` | every start | unset = HEC off | HEC token |
| `MYDEMOTICKET_HEC_VERIFY_TLS` | every start | `1` | `0`, `false`, `no`, `off` disable certificate checks (lab only) |
| `MYDEMOTICKET_HEC_INDEX` | every start | unset = token's default index | Target index, normally `mydemoticket` |

Fixed limits: request body max 1 MiB; webhook deliveries 3 attempts (1 s, 2 s
backoff); audit log rotation at 10 MB, 5 generations.

### Files in the data directory

| Path | Content |
|---|---|
| `mydemoticket.sqlite3` (+ `-wal`, `-shm`) | The database (all tenants) |
| `mydemoticket.sqlite3.pre-v<N>.bak` | Automatic copy before schema migration step N (only for existing databases) |
| `initial-credentials.txt` | Generated passwords and tokens of the first start (0600) – delete after storing them |
| `logs/audit.log`, `logs/audit.log.1` … `.5` | Local audit log (JSON lines, 0600) |

## 6. First login and initial credentials

On the first start (empty database) the server creates:

* tenant `soc-games` ("SOC Games", default tenant, cannot be deactivated),
* user `admin`, role `platform_admin`, no tenant, with one API token,
* unless `--no-seed`: teams `SOC L1`, `SOC L2`, `IT Ops`; users `analyst1` (agent,
  SOC L1), `analyst2` (agent, SOC L2), `requester1` (requester), `soar`
  (integration, with one API token); custom fields `mitre_technique`,
  `affected_host`, `risk_score`, `incident_type` – all in `soc-games`.

Every password or token that was not given via environment is written to
`<data-dir>/initial-credentials.txt` (mode 0600) with lines such as
`admin password: ...`, `admin API token: ...`, `soar API token: ...`. They are never
printed. Store them in a password manager and delete the file.

```bash
TOKEN=$(sed -n 's/^admin API token: //p' "<data-dir>/initial-credentials.txt")
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/api/v1/me
```

Web UI: log in as `admin`. Admin pages: `#/admin/users` (users, "New token"),
`#/admin/teams`, `#/admin/custom-fields`, `#/admin/webhooks`, `#/admin/audit`,
`#/admin/tenants` (platform admin), `#/reports`.

## 7. Tenants

Every user except `platform_admin` belongs to exactly one tenant and only sees its
data. A platform admin manages tenants and selects the tenant of a request with
the header `X-MyDemoTicket-Tenant: <slug>` (in the UI with the tenant switcher).
Without the header the platform admin works in `soc-games`.

```bash
export MDT=https://ticket.example.com/api/v1      # or http://127.0.0.1:8080/api/v1
export TOKEN=<platform admin token>

# new tenant with teams SOC L1, SOC L2 and the four standard custom fields
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"slug": "acme", "name": "ACME Corp", "soar_url": "https://soar.example.com", "seed": true}' "$MDT/tenants"

# tenant admin
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"username": "acme-admin", "role": "admin", "tenant": "acme", "password": "<min 8 characters>"}' "$MDT/users"
```

`slug` matches `[a-z0-9-]{2,32}` and cannot be changed. Deactivate a tenant with
`PATCH /tenants/<slug>` `{"active": false}`. Details: [api.md](api.md#tenants-multi-tenancy).

## 8. Integration user and API token

SOAR (and the MCP server) authenticate with a Bearer token of a user in the
tenant they work in. Use role `integration` (may change all tickets of the
tenant, see `can_modify_ticket` in `ticket-system/mydemoticket/auth.py`). The SOAR app's *test connectivity* accepts only `integration`, `agent` or
`admin` – not a `platform_admin` token.

In `soc-games` the seeded user `soar` already exists (token in
`initial-credentials.txt`). For other tenants, or to get a fresh token:

```bash
# user in tenant acme (platform admin: "tenant" is required)
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"username": "soar-acme", "role": "integration", "tenant": "acme", "display_name": "Splunk SOAR (acme)"}' \
  "$MDT/users"

# token: the request must run in the user's tenant, so the platform admin sends the tenant header
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "X-MyDemoTicket-Tenant: acme" \
  -H "Content-Type: application/json" -d '{"name": "soar"}' "$MDT/users/soar-acme/tokens"
# 201 {"id": ..., "name": "soar", "username": "soar-acme", "token": "<shown only once>", ...}
```

Without the header the token request answers `404` (the user is in another
tenant). In the UI: switch to the tenant, `#/admin/users`, "New token". Tokens are
stored as SHA-256 hashes; a lost token cannot be shown again – create a new one
and revoke the old one (`DELETE /users/<username>/tokens/<id>`).

## 9. Connect Splunk SOAR

Two directions:

* **SOAR → ticket:** the SOAR app "MyDemoTicket" calls the REST API with the
  integration token.
* **Ticket → SOAR:** a webhook of type `soar` creates one container per ticket
  (`source_data_identifier` = ticket number, label `mydemoticket`) and one artifact
  per change (label `mydemoticket_event`, `run_automation: true`), which starts the
  ES mirror playbook.

### 9.0 SOAR automation user

The webhook (ticket → SOAR) and the REST imports below need a SOAR `ph-auth-token`:
SOAR → *Administration → User Management → Users → + User*, type **Automation**, with a role
that may create containers and artifacts and run playbooks (for the REST imports also: install
apps, import playbooks, edit event settings – e.g. the built-in *Administrator* role). Under
*Allowed IPs* enter `any` or the egress IP of the ticket server. Open the user and copy the
token (shown as JSON; use only the value of `ph-auth-token`). Do not click *Generate* later –
that invalidates the token stored in the webhook.

### 9.1 Build and install the app

```bash
cd soar-app
python3 build.py              # Built .../soar-app/dist/mydemoticket.tgz
```

* **UI:** SOAR → *Apps* → *Install App* → upload `soar-app/dist/mydemoticket.tgz`.
* **REST** (SOAR REST API, base64 of the archive in the field `app`; the token is a
  SOAR automation user's `ph-auth-token`):

  ```bash
  SOAR=https://soar.example.com
  python3 -c 'import base64,json,sys; print(json.dumps({"app": base64.b64encode(open(sys.argv[1],"rb").read()).decode()}))' \
    soar-app/dist/mydemoticket.tgz > "${TMPDIR:-/tmp}/mdt-app.json"
  curl -s -X POST -H "ph-auth-token: $SOAR_TOKEN" -H "Content-Type: application/json" \
    --data-binary @"${TMPDIR:-/tmp}/mdt-app.json" "$SOAR/rest/app"
  ```

  Verified live on 2026-11-24 against Splunk SOAR 8.7 and 8.8 (Splunk Cloud): the answer is
  `{"success": true, "message": "App successfully imported.", "id": <app id>}`. Re-importing a
  newer version upgrades the app in place; existing assets keep their configuration.

The app has no pip dependencies (`python_version` 3.13, app version 1.2.0).

### 9.2 Asset

SOAR → *Apps* → MyDemoTicket → *Configure New Asset*. Name it `mydemoticket` (the
default `MYDEMOTICKET_ASSET` of the `mydemoticket_ticket_from_es` playbook).

| Field | Value |
|---|---|
| `base_url` | `https://ticket.example.com` (without `/api/v1`) |
| `api_token` | integration token from [section 8](#8-integration-user-and-api-token) |
| `verify_server_cert` | `true` |
| `source_tag` | `soar` – keep it: the ticket system skips SOAR webhooks only for source `soar` |
| `ingest_label` | `mydemoticket` (label of containers created by *on poll*) |
| `poll_state_filter` | empty, or one state |

Run **test connectivity**: it calls `GET /api/v1/health` and `GET /api/v1/me`.
Optional: enable polling as a fallback for missed webhooks (see the app README).

### 9.3 Label

The webhook and *on poll* create containers with label `mydemoticket`. Create it
in SOAR (*Administration → Event Settings → Label Settings*) if it does not exist;
SOAR rejects containers with unknown labels. Via REST (verified live on SOAR 8.7 and 8.8):

```bash
curl -s -X POST -H "ph-auth-token: $SOAR_TOKEN" -H "Content-Type: application/json" \
  -d '{"add_label": true, "label_name": "mydemoticket"}' "$SOAR/rest/system_settings/events"
```

### 9.4 Webhook of type `soar` (one per tenant)

Webhooks belong to a tenant and fire only for its tickets. Create one per tenant
that should go to SOAR (tenant admin in the UI: `#/admin/webhooks`, or via API).
`url` is the SOAR base URL, `secret` the `ph-auth-token` of a SOAR automation user
that may create containers and artifacts.

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "X-MyDemoTicket-Tenant: acme" \
  -H "Content-Type: application/json" \
  -d '{"type": "soar", "name": "SOAR", "url": "https://soar.example.com",
       "secret": "'"$SOAR_PH_AUTH_TOKEN"'", "soar_label": "mydemoticket", "verify_tls": true}' \
  "$MDT/webhooks"
```

`events` defaults to all four (`ticket.created`, `ticket.updated`,
`ticket.state_changed`, `ticket.commented`). The secret is never returned. There
is no PATCH: to change a webhook, delete it (`DELETE /webhooks/<id>`) and create it
again. Deliveries are visible in `#/admin/webhooks` or
`GET /webhooks/<id>/deliveries`.

**Loop protection:** changes made through the SOAR app carry
`X-MyDemoTicket-Source: soar`; `soar` webhooks do not deliver them (delivery status
`skipped`), and the ES mirror playbook ignores artifacts with `cef.source = soar`.

## 10. ES mirror playbook

Files: `soar-playbooks/mydemoticket_es_mirror.py` + `.json` (ticket → ES) and
`soar-playbooks/mydemoticket_ticket_from_es.py` + `.json` (ES notable → ticket,
only syntax-checked so far). Logic and limits: [soar-playbooks/README.md](../soar-playbooks/README.md).

1. **Set the asset constants** at the top of the `.py` file to your SOAR asset names:

   | File | Constant | Default |
   |---|---|---|
   | `mydemoticket_es_mirror.py` | `SPLUNK_ASSET` (Splunk app, `update event`) | `splunk` |
   | `mydemoticket_es_mirror.py` | `ES_ASSET` (Enterprise Security app, notes and investigations) | `builtin_mc_connector` |
   | `mydemoticket_es_mirror.py` | `MIRROR_OWNER` | `True` (set `False` if ticket users do not exist in ES) |
   | `mydemoticket_es_mirror.py` | `INVESTIGATION_CLOSE_DISPOSITION` | `""` (not sent), e.g. `"disposition:1"` |
   | `mydemoticket_ticket_from_es.py` | `MYDEMOTICKET_ASSET` | `mydemoticket` |
   | `mydemoticket_ticket_from_es.py` | `ASSIGNMENT_GROUP`, `CATEGORY` | `SOC L1`, `security_incident` |

   `builtin_mc_connector` is the ES app asset name of the demo Splunk Cloud SOAR;
   check yours under *Apps → Enterprise Security*. `ASSIGNMENT_GROUP` must be an
   existing team of the integration user's tenant (the seeded teams are `SOC L1`,
   `SOC L2`, `IT Ops`), otherwise *create ticket* fails with `HTTP 422`.

2. **Keep the `coa` metadata** in the `.json` (`coa.python_version` `3.13`,
   `coa.schema`, `coa.version`). Without them SOAR imports the playbook in draft mode
   ("Python linting and validation could not be run") and it cannot be activated.

3. **Package and import.** A playbook import is a `.tgz` with the `.json` and the
   `.py` of the same name:

   ```bash
   cd soar-playbooks
   tar -czf "${TMPDIR:-/tmp}/mydemoticket_es_mirror.tgz" mydemoticket_es_mirror.json mydemoticket_es_mirror.py
   ```

   SOAR → *Playbooks* → *Import Playbook* → upload the archive, or REST
   `POST /rest/import_playbook` (SOAR REST API: base64 of the archive in
   `playbook`, plus `scm` and `force`):

   ```bash
   python3 -c 'import base64,json,sys; print(json.dumps({"playbook": base64.b64encode(open(sys.argv[1],"rb").read()).decode(), "scm": "local", "force": True}))' \
     "${TMPDIR:-/tmp}/mydemoticket_es_mirror.tgz" > "${TMPDIR:-/tmp}/mdt-pb.json"
   curl -s -X POST -H "ph-auth-token: $SOAR_TOKEN" -H "Content-Type: application/json" \
     --data-binary @"${TMPDIR:-/tmp}/mdt-pb.json" "$SOAR/rest/import_playbook"
   ```

   Verified live on 2026-11-24 (SOAR 8.8): `{"success": true, "message": "Playbook \"mydemoticket_es_mirror\" imported.", "id": <id>}`.
   Then activate it (*Playbooks* → toggle *Active*, or `POST /rest/playbook/<id>` with `{"active": true}`).

4. **Activate** the playbook (label `mydemoticket`, trigger *artifact created*,
   type automation). Do not edit it in the Visual Playbook Editor: the `.json` holds
   only a minimal graph, the logic is in the `.py`.

What it does: status changes (`ticket.created`, `ticket.state_changed`) set the ES
finding status (new→1 … closed/cancelled→5) via `update event`, or close an ES 8
investigation via `update finding or investigation` when the ticket has
`external_refs.es_investigation_id`; comments and work notes become ES notes via
`add finding or investigation note`; resolve/close/cancel adds a close note with the
close notes. The ticket needs `external_refs.es_finding_id` and/or
`external_refs.es_investigation_id` (the SOAR app's *create ticket* / *update ticket*
set them from `es_finding_id` / `es_investigation_id`).

### 10b. Optional: ticket from ES notable (`mydemoticket_ticket_from_es`)

Creates a MyDemoTicket ticket from an ES notable container (sets `es_finding_id` from the
artifact's `event_id`). It is only syntax-checked, not live-tested.

1. Set the constants in `soar-playbooks/mydemoticket_ticket_from_es.py` (`MYDEMOTICKET_ASSET`
   = the asset name from 9.2, `ASSIGNMENT_GROUP` = an existing team of the integration user's
   tenant, default `SOC L1`).
2. The playbook is bound to container label **`notable`** (`labels` in the `.json`). If your ES
   notables arrive in SOAR with a different label, change `labels` in the `.json` or the
   playbook's label after import. It does not check whether a ticket already exists for the
   container.
3. Package, import and activate like section 10:

   ```bash
   cd soar-playbooks
   tar -czf "${TMPDIR:-/tmp}/mydemoticket_ticket_from_es.tgz" mydemoticket_ticket_from_es.json mydemoticket_ticket_from_es.py
   ```

   Then *Playbooks → Import Playbook* (or `POST /rest/import_playbook` as in section 10) and
   activate it.

## 11. Splunk: audit log via HEC, dashboards, roles

Full reference: [logging.md](logging.md). Files in [`splunk/`](../splunk/).

1. **Index** `mydemoticket` for all tenants on the indexers
   ([`indexes.conf.example`](../splunk/indexes.conf.example); in Splunk Cloud create it in
   *Settings → Indexes*).
2. **Knowledge objects:** [`props.conf`](../splunk/props.conf) (indexers/heavy forwarders and
   search heads) and [`fields.conf`](../splunk/fields.conf) (search heads, declares the indexed
   field `tenant`) in an app.
3. **HEC token:** *Settings → Data inputs → HTTP Event Collector → New Token*, source type
   `mydemoticket:audit`, allowed and default index `mydemoticket`, indexer acknowledgement off
   ([`inputs.conf.example`](../splunk/inputs.conf.example)). HEC must be enabled in *Global Settings*.
   HEC URL format: Splunk Enterprise `https://<splunk-host>:8088`; Splunk Cloud Platform
   `https://http-inputs-<stack>.splunkcloud.com:443` (see Splunk docs "Set up and use HTTP Event
   Collector"). The ticket system appends `/services/collector/event` if the URL has no
   `/services/collector` path.
4. **Configure the ticket system** with `MYDEMOTICKET_HEC_URL`, `MYDEMOTICKET_HEC_TOKEN`,
   `MYDEMOTICKET_HEC_INDEX=mydemoticket` (and `MYDEMOTICKET_HEC_VERIFY_TLS`):
   * local: export them before `python3 -m mydemoticket`;
   * option B: add them to `/etc/mydemoticket/env`, then `sudo systemctl restart mydemoticket`;
   * option C: `security add-generic-password -s mydemoticket -a splunk-hec-token -w`, run
     `deploy.sh` with `HEC_URL=https://<splunk-host>:8088`, reboot the instance.
5. **Roles** ([`authorize.conf.example`](../splunk/authorize.conf.example)): one role per tenant with
   `srchIndexesAllowed = mydemoticket` and `srchFilter = tenant::<slug>`. Let tenant roles inherit
   **only** from `user` (`importRoles = user`): search filters of inherited roles are OR-combined,
   so inheriting `power` or `admin` would widen access.
6. **Dashboards:** import [`mydemoticket_audit.xml`](../splunk/dashboards/mydemoticket_audit.xml) and
   [`mydemoticket_report.xml`](../splunk/dashboards/mydemoticket_report.xml) via *Dashboards → Create
   New Dashboard → Source*, or copy them into `<app>/default/data/ui/views/`.

The local file `<data-dir>/logs/audit.log` is always written, with or without HEC.

## 12. MCP server (Claude Code / Claude Desktop)

Full reference: [mcp-server/README.md](../mcp-server/README.md).

1. Create a user for the MCP server (role `integration`, or `agent` for one team) and a
   token ([section 8](#8-integration-user-and-api-token)). Do not use an admin token.
2. Store the token (macOS keychain, prompts for the value):

   ```bash
   security add-generic-password -U -s mydemoticket -a mcp-api-token -w
   ```

   On other systems set `MYDEMOTICKET_TOKEN` in the MCP client's environment instead.
3. Register it in Claude Code from a **stable checkout** (the registration stores the
   absolute path of `mcp-server/run.sh`; a checkout you switch branches in or delete breaks it):

   ```bash
   cd mcp-server
   MYDEMOTICKET_URL=https://ticket.example.com ./install.sh --dry-run
   MYDEMOTICKET_URL=https://ticket.example.com ./install.sh      # scope user, name mydemoticket
   claude mcp list
   ```

   Options: `--scope user|project`, `--name <name>`, `--uninstall`. Without the `claude` CLI the
   script prints the Claude Desktop snippet. That snippet has no `env` block – add your server URL
   by hand, otherwise the MCP server uses the demo default `https://ticket.soc-games.com`:

   ```json
   {
     "mcpServers": {
       "mydemoticket": {
         "command": "/path/to/MyDemoTicket/mcp-server/run.sh",
         "env": { "MYDEMOTICKET_URL": "https://ticket.example.com" }
       }
     }
   }
   ```

| Variable | Default | Meaning |
|---|---|---|
| `MYDEMOTICKET_URL` | `https://ticket.soc-games.com` | Base URL (`/api/v1` is added) |
| `MYDEMOTICKET_TOKEN` | – | API token; if empty, `run.sh` reads the keychain item |
| `MYDEMOTICKET_KEYCHAIN_ACCOUNT` | `mcp-api-token` | Keychain account read by `run.sh` (service `mydemoticket`) |
| `MYDEMOTICKET_TENANT` | – | Sent as `X-MyDemoTicket-Tenant` |
| `MYDEMOTICKET_VERIFY_TLS` | `1` | `0` only for test servers |
| `MYDEMOTICKET_TIMEOUT` | `30` | Seconds |
| `MYDEMOTICKET_LOG_LEVEL` | `WARNING` | `INFO`/`DEBUG` log to stderr |

## 13. Verification after installation

```bash
export MDT=https://ticket.example.com/api/v1
export TOKEN=<integration token>

# 1. health (no auth): {"status": "ok", "version": "1.0.0"}
curl -s "$MDT/health"

# 2. token, role and tenant
curl -s -H "Authorization: Bearer $TOKEN" "$MDT/me"

# 3. test ticket (source soar would skip the SOAR webhook, so use the default source here)
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"short_description": "Installation test", "priority": 4}' "$MDT/tickets"

# 4. webhook deliveries (admin token): last status should be "success"
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" "$MDT/webhooks"
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" "$MDT/webhooks/<id>/deliveries"

# 5. audit / HEC (admin token): "hec_enabled": true, "sent" > 0, "last_error": null
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" "$MDT/audit/status"
```

6. SOAR: asset *mydemoticket* → **Test Connectivity** succeeds; a container
   `TKT…: Installation test` with label `mydemoticket` and an artifact
   `ticket.created` exists.
7. Splunk:

   ```
   | tstats count where index=mydemoticket sourcetype=mydemoticket:audit by tenant
   ```

8. ES (with the mirror playbook active): set a ticket that has `es_finding_id` to
   `in_progress` in the UI; the finding status changes to *In Progress*.
