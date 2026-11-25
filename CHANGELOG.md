# Changelog

## 1.2.0

### Ticket system

- ES + SOAR pairing wizard (admin page `#/admin/pairing`, permission `pairing.manage` for admin
  and platform admin, not grantable): `GET /pairing`, `POST /pairing/check`,
  `POST /pairing/apply` set up integration user + token, `soar` webhook, SOAR label, the
  MyDemoTicket SOAR app, its asset and both playbooks (active) in one repeatable run, with a
  result per step; tokens are never returned or logged.
- Deploy: the AWS bundle and `deploy/install.sh` ship `soar-app/mydemoticket` and
  `soar-playbooks` for the wizard (`MYDEMOTICKET_SOAR_BUNDLE_DIR` overrides the location,
  `MYDEMOTICKET_PUBLIC_URL` sets the default ticket base URL).

## 1.1.0 – 2026-11-25

| Component | Version |
|---|---|
| Ticket system (`ticket-system/mydemoticket`) | 1.1.0 |
| REST API | v1 |
| Database schema | 4 |

### Ticket system

- Own profile: `GET /me` returns `timezone`, `theme`, `language` and `permissions`;
  `PATCH /me` changes display name, e-mail, time zone (IANA), theme and language;
  `POST /me/password` changes the own password after checking the current one.
- Fix: changing the own password (`POST /me/password` or `PATCH /users/<self>`)
  keeps the current session and ends only the other sessions; before, the admin was
  logged out and creating a user failed afterwards.
- Permission model behind all role checks; the five existing roles keep their exact
  behaviour. New built-in analyst roles `analyst_l1`, `analyst_l2`, `soc_lead`,
  `read_only`.
- Role management: `GET /permissions`, `GET/POST /roles`, `PATCH/DELETE /roles/<name>`;
  custom roles per tenant, only analyst permissions grantable (server-side enforced).
- Splunk HEC connection configurable at runtime: `GET/PUT /settings/splunk`,
  `POST /settings/splunk/test` (platform admin); stored settings win over
  `MYDEMOTICKET_HEC_*`; the token is write-only and never logged.
- Schema migration v4 (additive, backup `*.pre-v4.bak`): user profile columns,
  tables `roles` and `settings`.
- `GET /me` also returns `role_label`, so users with a custom role see its label
  without access to `GET /roles`.

### Web UI

- Whole UI in German and English (`static/i18n.js`): texts, labels, messages,
  state and role names; the login page uses the last chosen language, otherwise
  the browser language.
- Profile page (click on your name top right, `#/profile`): display name, e-mail,
  time zone (empty = browser), theme light/dark/system, language and own password
  (current + new twice). Saved on the server and applied immediately; all times
  are shown in the chosen time zone.
- Users admin: set password in a dialog with the new password twice (mismatch or
  fewer than 8 characters sends nothing); display name and e-mail editable in the row;
  role selects list built-in and custom roles.
- Roles page (`#/admin/roles`): all roles with translated permissions; create, edit
  and delete custom roles from the analyst permissions only.
- Audit / Splunk: platform admins create and change the Splunk HEC connection
  (URL, index, write-only token, TLS check, on/off) and send a test event.
- Navigation and actions follow the permissions from `/me`; admin pages opened by
  URL without the permission show a "no permission" message.

## 1.0.0 – 2026-11-24

First complete release. Component versions:

| Component | Version |
|---|---|
| Ticket system (`ticket-system/mydemoticket`) | 1.0.0 |
| REST API | v1 |
| Database schema | 3 |
| SOAR app (`soar-app`) | 1.2.0 |
| MCP server (`mcp-server`) | 1.0.0 |

### Ticket system

- ServiceNow-like tickets with state machine (`new`, `in_progress`, `on_hold`,
  `resolved`, `closed`, `cancelled`), priorities, assignment groups, custom fields,
  journal, roles (`platform_admin`, `admin`, `agent`, `integration`, `requester`) and
  teams; REST API `/api/v1` and single-page web UI, standard library only (#1).
- Outbound webhooks of type `generic` (HMAC-signed) and `soar` (container + artifact
  per event) with retries and loop protection via `X-MyDemoTicket-Source` (#1).
- Multi-tenancy with strict per-tenant isolation, default tenant `soc-games`,
  schema migration v2 with automatic `*.pre-v2.bak` backup (#8).
- Audit log for every action and every `401`/`403`: local `logs/audit.log` plus
  optional Splunk HEC (sourcetype `mydemoticket:audit`, indexed field `tenant`);
  `GET /audit/status` (#8).
- Reports: `GET /reports/summary` and a Reports page (counts, MTTR) (#8).
- `GET /tickets?external_ref.<key>=<value>` for find-before-create, optional comment
  `author` (schema v3, `journal.author`) (#11).
- Ticket detail tabs (Work notes, Comments, Resolution report, Audit trail, History),
  journal type `summary`, convention `external_refs.es_investigation_id` with CEF
  `esInvestigationId` in SOAR artifacts (#13).
- CEF `closeNotes` in SOAR artifacts for the ES close note (#15).

### Splunk SOAR

- SOAR app "MyDemoTicket" with 10 actions incl. `on poll` (#1); 1.1.0: `author` and
  `external_ref_key`/`external_ref_value` (#11); 1.2.0: comment type `summary`,
  `es_investigation_id` (#13).
- Playbook `mydemoticket_es_mirror` (ticket → ES): finding status via the Splunk app
  asset with string `integer_status` (#4), import metadata `coa.python_version`,
  `coa.schema`, `coa.version` (#9), ES 8 notes and investigation status via the
  Enterprise Security app (#14), close note in ES on resolve/close/cancel (#15).
- Playbook `mydemoticket_ticket_from_es` (ES notable → ticket), syntax-checked only (#1).

### Deployment and integrations

- `deploy/install.sh`: systemd + Caddy installer with `--dry-run`, `--uninstall`,
  `--purge-data` (#1).
- AWS EC2 deployment `deploy/aws/deploy.sh` with per-boot `boot.sh`, secrets via
  keychain → SSM SecureString, sslip.io host name (#2); routing through an existing
  ALB, `--alb-port` (#3); HEC parameters and `deploy/aws/backup.sh` (#8).
- MCP server for Claude Code / Claude Desktop (stdio, journal source `mcp`) (#7);
  `external_ref` filter and `author` (#11); comment type `summary` (#13).
- Splunk examples (`splunk/`): index, HEC input, props, fields, per-tenant roles and
  two dashboards (#8).
- End-to-end tests ticket system ↔ SOAR app against a SOAR mock (`scripts/e2e.sh`) (#1).

### Documentation

- New: `docs/installation.md`, `docs/operations.md`, this changelog; README
  restructured; architecture, API and logging docs updated (#16).
