# MyDemoTicket MCP server

A [Model Context Protocol](https://modelcontextprotocol.io) server that lets Claude Code and
Claude Desktop work with MyDemoTicket: list, read, create and update tickets, change states,
add comments and work notes, read journals and custom field definitions.

* Transport: stdio, JSON-RPC 2.0, one JSON message per line. Logs go to stderr only.
* Protocol versions: `2025-06-18`, `2025-03-26`, `2024-11-05`.
* Python 3.9+, standard library only – nothing to install.
* Every change is recorded in the ticket journal with `source: mcp`.

## 1. Create an API token

1. Log in to MyDemoTicket as admin.
2. **Admin → Users**: add a user for the MCP server, e.g. `mcp` with role `integration`
   (may change all tickets) or `agent` (only tickets of its teams, assigned to it or unassigned).
3. Click **New token** in that user's row and copy the token. It is shown only once.
4. Store it in the macOS keychain. The command asks for the token, so it does not end up in
   your shell history:

   ```bash
   security add-generic-password -U -s mydemoticket -a mcp-api-token -w
   ```

`run.sh` reads the token from this keychain item at start time when `MYDEMOTICKET_TOKEN` is
not set. On other systems, set `MYDEMOTICKET_TOKEN` in the environment of the MCP client.

## 2. Install

Claude Code (registers `run.sh` from this folder, default scope `user`):

```bash
cd mcp-server
./install.sh --dry-run      # shows what would happen
./install.sh                # registers the server "mydemoticket"
claude mcp list             # check
./install.sh --uninstall    # removes it again
```

Options: `--scope user|project` (default `user`), `--name <name>` (default `mydemoticket`).
Running `install.sh` again replaces the registration. If `MYDEMOTICKET_URL` is set while
installing, it is stored with the registration. The registration holds the absolute path
of `run.sh`: after moving the repo or switching machines, run `./install.sh` again. For the
same reason prefer `user` scope; a `project` scope entry in `.mcp.json` only works on
machines with the same path.

Claude Desktop: without the `claude` CLI, `install.sh` prints this snippet with the right path.
Add it to `~/Library/Application Support/Claude/claude_desktop_config.json` and restart
Claude Desktop:

```json
{
  "mcpServers": {
    "mydemoticket": {
      "command": "/path/to/MyDemoTicket/mcp-server/run.sh"
    }
  }
}
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MYDEMOTICKET_URL` | `https://ticket.soc-games.com` | Base URL of the ticket system (`/api/v1` is added) |
| `MYDEMOTICKET_TOKEN` | – (required) | API token. Missing: the server starts, every tool call returns an error with a hint |
| `MYDEMOTICKET_TENANT` | – | Sent as header `X-MyDemoTicket-Tenant` |
| `MYDEMOTICKET_VERIFY_TLS` | `1` | `0` turns off certificate checks (only for test servers) |
| `MYDEMOTICKET_TIMEOUT` | `30` | Request timeout in seconds |
| `MYDEMOTICKET_KEYCHAIN_ACCOUNT` | `mcp-api-token` | Keychain account `run.sh` reads (service `mydemoticket`) |
| `MYDEMOTICKET_LOG_LEVEL` | `WARNING` | `INFO` or `DEBUG` logs requests (method and path) to stderr |

Example for a local server: `claude mcp add mydemoticket-local -e MYDEMOTICKET_URL=http://127.0.0.1:8080 -- "$PWD/run.sh"`.

## Tools

| Tool | API | Kind |
|---|---|---|
| `whoami` | `GET /me` | read |
| `list_tickets` | `GET /tickets` – `state` (one or comma list), `assignment_group`, `assigned_to`, `q`, `updated_since`, `limit` 1–100 (default 20), `offset` | read |
| `get_ticket` | `GET /tickets/{number}` | read |
| `create_ticket` | `POST /tickets` – `short_description` (required), `description`, `priority` 1–4, `assignment_group`, `assigned_to`, `category`, `custom_fields` | write |
| `update_ticket` | `PATCH /tickets/{number}` – only the fields passed; `custom_fields`/`external_refs` are merged | write, idempotent |
| `set_state` | `PATCH /tickets/{number}` – `state` new, in_progress, on_hold, resolved, closed, cancelled; `close_notes` required for resolved (checked before calling the API) | write, idempotent |
| `add_comment` | `POST /tickets/{number}/comments` – `body`, `type` comment (default) or work_note | write |
| `get_journal` | `GET /tickets/{number}/journal` | read |
| `list_custom_fields` | `GET /custom-fields` | read |
| `reports_summary` | `GET /reports/summary` – optional `since`; says "not available on this server version" if the server has no such endpoint | read |

Results come as compact JSON text plus `structuredContent` (lists as `{"items": [...], "total": n}`).
API errors (HTTP 4xx/5xx) and invalid arguments are returned as tool results with
`isError: true` and a readable message, e.g. `HTTP 409 Conflict: transition new -> closed is not allowed`.
No tool deletes anything.

## Example prompts

* "Show me all new and in-progress tickets of SOC L1."
* "Create a high-priority ticket for a phishing mail reported by demo-host-01, incident type phishing, assign it to SOC L1."
* "Take TKT0001001: set it to in progress, add a work note with the IOC findings, and a comment for the caller."
* "Resolve TKT0001001 as false positive and explain why in the close notes."
* "What happened on TKT0001001? Summarise the journal."
* "Which tickets changed since this morning?"

## Security

* The token is read from the environment or the keychain at run time. It is never written to
  the repo, the Claude configuration (with `install.sh`), logs or error texts.
* The server can do exactly what the token's user may do. Use a dedicated user with the
  smallest role that fits (`agent` for one team, `integration` for all tickets); never an admin token.
* Revoke a token in **Admin → Users** if it leaks, then store the new one with the
  `security add-generic-password -U ...` command above.
* Write tools change real tickets and trigger webhooks (for example to Splunk SOAR). Claude
  asks before calling them unless you allowed the tools permanently.
* Keep `MYDEMOTICKET_VERIFY_TLS=1` for anything but local test servers.

## Tests

```bash
cd mcp-server
python3 -m unittest discover -s tests -v > "${TMPDIR:-/tmp}/mdt-mcp-tests.txt" 2>&1; tail -3 "${TMPDIR:-/tmp}/mdt-mcp-tests.txt"
```

Success is the line `OK` after `Ran N tests`. The tests start the real ticket server
(`python3 -m mydemoticket`) on a free 127.0.0.1 port with a temporary data directory, run
`install.sh` only with `--dry-run` and use a fake `security` tool, so the real keychain and
Claude configuration are never touched.
