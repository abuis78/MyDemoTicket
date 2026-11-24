# Changelog

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
