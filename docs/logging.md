# Audit logging and Splunk

MyDemoTicket writes an audit trail of every security-relevant action. Each event is
always written to a local file and, if configured, also sent to Splunk via the
HTTP Event Collector (HEC). Code: [`ticket-system/mydemoticket/audit.py`](../ticket-system/mydemoticket/audit.py),
tests: [`ticket-system/tests/test_audit.py`](../ticket-system/tests/test_audit.py),
Splunk configuration and dashboards: [`splunk/`](../splunk/).

## Event schema

One JSON object per event (sourcetype `mydemoticket:audit`):

| Field | Type | Meaning |
|---|---|---|
| `ts` | string | Time of the action, ISO 8601 UTC with milliseconds, e.g. `2026-11-24T10:15:30.123Z` |
| `tenant` | string | Tenant slug, e.g. `soc-games` |
| `action` | string | What happened, see the action list below |
| `actor` | string/null | User name (or token name) that acted |
| `role` | string/null | Role of the actor within the tenant |
| `outcome` | string | `success`, `failure`, `denied`, `skipped` or `error` |
| `object_type` | string/null | `ticket`, `user`, `token`, `team`, `custom_field`, `webhook`, `tenant`, ... |
| `object_id` | string/null | Id or number of the object, e.g. `INC0001001` |
| `source` | string/null | Channel: `ui`, `api`, `webhook`, `system` |
| `ip` | string/null | Client IP |
| `request_id` | string/null | Request id for correlation with access logs |
| `details` | object | Action-specific data (redacted, see below) |
| `app` | string | Always `mydemoticket` |
| `version` | string | `mydemoticket.__version__` |

Example:

```json
{"ts": "2026-11-24T10:15:30.123Z", "tenant": "soc-games", "action": "ticket.state_change",
 "actor": "alice", "role": "agent", "outcome": "success", "object_type": "ticket",
 "object_id": "INC0001001", "source": "api", "ip": "10.0.0.5", "request_id": "6f1c...",
 "details": {"from": "in_progress", "to": "resolved"}, "app": "mydemoticket", "version": "0.1.0"}
```

### Redaction

Before an event is queued, `details` is copied recursively. Every key whose name contains
(case-insensitive) `password`, `passwd`, `secret`, `token`, `authorization`, `api_key`,
`cookie` or `session` gets the value `[REDACTED]`. Strings longer than 4096 characters are
cut and marked with `...[truncated]`. The HEC token itself never appears in the log file,
in `stats()` or in error messages.

## Actions

| Action | When | Typical `details` |
|---|---|---|
| `auth.login` | Login attempt (`outcome=failure` on wrong credentials) | `{"method": "password"}` |
| `auth.logout` | Logout | |
| `auth.tenant_switch` | User switches the active tenant | `{"from": "...", "to": "..."}` |
| `access.denied` | Request rejected by authorization (`outcome=denied`) | `{"method": "...", "path": "...", "required": "..."}` |
| `ticket.create` | Ticket created | `{"short_description": "...", "priority": ...}` |
| `ticket.update` | Fields changed | `{"changed": ["priority", ...]}` |
| `ticket.state_change` | State transition | **`{"from": "<old>", "to": "<new>"}`** (required by the report dashboard) |
| `ticket.comment` | Comment / work note added | `{"kind": "comment"}` |
| `user.create` / `user.update` / `user.delete` | User management | `{"changed": [...]}` |
| `token.create` / `token.revoke` | API tokens | `{"name": "..."}` (never the token value) |
| `team.create` / `team.update` / `team.delete` | Teams | |
| `custom_field.create` / `custom_field.update` / `custom_field.delete` | Custom fields | |
| `webhook.create` / `webhook.delete` | Webhook configuration | `{"type": "generic", "url_host": "..."}` |
| `webhook.delivery` | Outbound delivery attempt; `outcome` = `success`, `failure`, `skipped` or `error` | `{"event": "ticket.created", "status": 200, "attempt": 1}` |
| `tenant.create` / `tenant.update` | Tenant administration | |

`ticket.state_change` must carry the states in `details.from` and `details.to`
(values from `STATES` in `api.py`: `new`, `in_progress`, `on_hold`, `resolved`, `closed`,
`cancelled`); the MTTR and created/resolved panels rely on it.

## Using the logger in code

```python
from mydemoticket.audit import AuditLogger

audit = AuditLogger.from_env(data_dir, index_resolver=lambda slug: tenant_index.get(slug))
audit.start()
audit.log("ticket.create", tenant="soc-games", actor="alice", role="agent",
          object_type="ticket", object_id="INC0001001", source="api",
          ip=client_ip, request_id=req_id, details={"priority": 2})
...
audit.stop()   # drains the queue for up to 5 s
```

`log()` never blocks and never raises; it only appends to a bounded queue (10 000 events,
oldest dropped first, counted in `dropped`). `stats()` returns
`hec_enabled, queued, sent, failed, dropped, local_written, last_error, last_success_at`
and is suitable for a health endpoint.

## Local fallback log

Always written, independent of HEC:

- Path: `<data_dir>/logs/audit.log` (on the EC2 deploy: `/var/lib/mydemoticket/logs/audit.log`)
- Directory `0700`, file `0600`, one JSON object per line
- Rotation at 10 MB, 5 generations (`audit.log.1` ... `audit.log.5`, `.1` is the newest)

If HEC is down for a long time, the file still holds the full trail. It can be indexed later
with a Universal Forwarder (`monitor` stanza in
[`splunk/inputs.conf.example`](../splunk/inputs.conf.example)) or inspected with `jq`:

```bash
jq -c 'select(.tenant=="soc-games" and .outcome!="success")' /var/lib/mydemoticket/logs/audit.log
```

## Setting up HEC

1. Create the indexes (one per tenant plus a fallback), see
   [`splunk/indexes.conf.example`](../splunk/indexes.conf.example). Convention:
   `mdt_<tenant slug with "-" replaced by "_">`, e.g. `mdt_soc_games`, `mdt_usa_demo`.
2. Install [`splunk/props.conf`](../splunk/props.conf) and [`splunk/fields.conf`](../splunk/fields.conf)
   in an app on indexers and search heads.
3. Create a HEC token: *Settings > Data inputs > HTTP Event Collector > New Token*,
   source type `mydemoticket:audit`, allowed indexes = all `mdt_*` indexes, default index
   `mdt_default`, indexer acknowledgement off. Equivalent stanza:
   [`splunk/inputs.conf.example`](../splunk/inputs.conf.example).
   Make sure HEC is enabled (*Global Settings*).
4. Configure the ticket system through environment variables:

   | Variable | Meaning | Default |
   |---|---|---|
   | `MYDEMOTICKET_HEC_URL` | Base URL, e.g. `https://splunk.example.com:8088`. `/services/collector/event` is appended unless the URL already contains `/services/collector` | unset = HEC off |
   | `MYDEMOTICKET_HEC_TOKEN` | HEC token | unset = HEC off |
   | `MYDEMOTICKET_HEC_VERIFY_TLS` | `1` verify TLS certificate, `0` skip (lab only) | `1` |
   | `MYDEMOTICKET_HEC_INDEX` | Default index when the tenant has no own index | unset = token default |

   Never put the token into the repo. On AWS store it in SSM Parameter Store as a
   `SecureString` and render it into the service's environment file at deploy time, e.g.:

   ```bash
   aws ssm put-parameter --name /mydemoticket/hec-token --type SecureString --value "<token>"
   aws ssm get-parameter --name /mydemoticket/hec-token --with-decryption --query Parameter.Value --output text
   ```

   The environment file (for example `/etc/mydemoticket/env`, mode `0600`) then contains
   `MYDEMOTICKET_HEC_TOKEN=...`.
5. Test from the ticket host:

   ```bash
   curl -sS "$MYDEMOTICKET_HEC_URL/services/collector/health"
   ```

   then perform a login and search `index=mdt_* sourcetype=mydemoticket:audit`.

### Delivery behaviour

- Background thread, batches of up to 50 events or every 2 seconds.
- HEC event envelope per event:
  `{"time": <epoch>, "host": <hostname>, "source": "mydemoticket", "sourcetype": "mydemoticket:audit", "index": <tenant index or default, omitted if none>, "event": {...}, "fields": {"tenant": "<slug>"}}`
- Header `Authorization: Splunk <token>`.
- Network errors, HTTP 5xx and 429: retry with exponential backoff 1, 2, 4, ... up to 60 s.
- Other 4xx (bad token, unknown index, malformed): the batch is counted as `failed`,
  dropped, and `last_error` is set (without the token). Events are still in the local file.

## Tenant separation in Splunk

Two layers keep tenants apart:

1. **Index per tenant (hard separation).** The app passes `index_resolver(tenant_slug)`,
   so each event lands in its tenant's index. In
   [`splunk/authorize.conf.example`](../splunk/authorize.conf.example) each tenant has a role
   whose `srchIndexesAllowed` contains only its own index. Splunk enforces this for every
   search, dashboard and API call. Tenant roles must not inherit from roles with wider
   index access.
2. **Indexed field `tenant`.** Every HEC event carries `fields.tenant`, which is an
   index-time field (`fields.conf`: `INDEXED = true`). Searching `tenant::soc-games` or
   `tenant=soc-games` is fast and works with `tstats`; the example roles also set
   `srchFilter = tenant::<slug>` as a second safeguard, e.g. if several tenants share the
   fallback index.

The dashboards filter every search on `tenant=$tenant$`; this is for convenience, not for
security. Security comes from the roles.

## Dashboards

Simple XML, import via *Dashboards > Create New Dashboard > Source* or copy into
`<app>/default/data/ui/views/`:

- [`splunk/dashboards/mydemoticket_audit.xml`](../splunk/dashboards/mydemoticket_audit.xml):
  tenant dropdown and time picker; actions over time per tenant, ticket state changes,
  failed logins and denied access, webhook deliveries by outcome, top actors, latest events.
- [`splunk/dashboards/mydemoticket_report.xml`](../splunk/dashboards/mydemoticket_report.xml):
  tickets created/resolved per day, state distribution, created/resolved per tenant, MTTR.

Both search `(index=mdt_* OR index=main) sourcetype="mydemoticket:audit"`; adapt the index
part if you use other index names.

## Example searches

Failed logins per tenant and IP in the last 24 h:

```
index=mdt_* sourcetype=mydemoticket:audit action=auth.login outcome=failure earliest=-24h
| stats count by tenant ip actor | sort - count
```

Everything one user did:

```
index=mdt_* sourcetype=mydemoticket:audit actor=alice | table _time tenant action outcome object_id
```

Fast event counts per tenant via the indexed field:

```
| tstats count where index=mdt_* sourcetype=mydemoticket:audit by tenant _time span=1h
```

Webhook delivery failure rate:

```
index=mdt_* sourcetype=mydemoticket:audit action=webhook.delivery
| stats count(eval(outcome="success")) as ok count as total by tenant
| eval failure_rate=round(100*(total-ok)/total,1)
```

History of one ticket:

```
index=mdt_* sourcetype=mydemoticket:audit object_type=ticket object_id=INC0001001
| table _time action actor outcome details.from details.to
```

Mean time to resolve per tenant:

```
index=mdt_* sourcetype=mydemoticket:audit (action=ticket.create OR action=ticket.state_change)
| eval c=if(action=="ticket.create",_time,null()), r=if(in('details.to',"resolved","closed"),_time,null())
| stats min(c) as c min(r) as r by tenant object_id
| where isnotnull(c) AND isnotnull(r)
| stats avg(eval(r-c)) as mttr by tenant | eval mttr=tostring(round(mttr),"duration")
```
