"""MyDemoTicket MCP server: Model Context Protocol over stdio, JSON-RPC 2.0.

One JSON message per line on stdin/stdout (newline-delimited, as the MCP stdio
transport requires). Logs go to stderr only. Python 3.9+, standard library only.

Configuration (environment):
  MYDEMOTICKET_URL         base URL (default https://ticket.soc-games.com)
  MYDEMOTICKET_TOKEN       API token (required for tool calls; never logged)
  MYDEMOTICKET_TENANT      optional, sent as header X-MyDemoTicket-Tenant
  MYDEMOTICKET_VERIFY_TLS  1 (default) or 0
  MYDEMOTICKET_TIMEOUT     seconds (default 30)
  MYDEMOTICKET_LOG_LEVEL   WARNING (default), INFO, DEBUG
"""

import json
import logging
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

from . import __version__

LOG = logging.getLogger("mydemoticket_mcp")

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")  # newest first
DEFAULT_URL = "https://ticket.soc-games.com"
SOURCE = "mcp"

STATES = ["new", "in_progress", "on_hold", "resolved", "closed", "cancelled"]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


# ---------------------------------------------------------------------- configuration
class Config(object):
    def __init__(self, url, token, tenant, verify_tls, timeout):
        self.url = url
        self._token = token
        self.tenant = tenant
        self.verify_tls = verify_tls
        self.timeout = timeout

    def __repr__(self):  # never show the token
        return "<Config url=%s tenant=%s verify_tls=%s timeout=%s token=%s>" % (
            self.url, self.tenant or "-", self.verify_tls, self.timeout, "set" if self._token else "missing")

    @property
    def token(self):
        return self._token

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        url = (env.get("MYDEMOTICKET_URL") or DEFAULT_URL).strip().rstrip("/")
        if url.endswith("/api/v1"):
            url = url[: -len("/api/v1")]
        verify = (env.get("MYDEMOTICKET_VERIFY_TLS") or "1").strip().lower() not in ("0", "false", "no", "off")
        try:
            timeout = float(env.get("MYDEMOTICKET_TIMEOUT") or 30)
        except ValueError:
            timeout = 30.0
        return cls(url, (env.get("MYDEMOTICKET_TOKEN") or "").strip(),
                   (env.get("MYDEMOTICKET_TENANT") or "").strip(), verify, timeout)


# ---------------------------------------------------------------------- REST client
class ApiError(Exception):
    """A failed API call with a message that is safe to show (never contains the token)."""

    def __init__(self, message, status=None):
        Exception.__init__(self, message)
        self.status = status


class ApiClient(object):
    def __init__(self, config):
        self.config = config
        self._ssl = None
        if config.url.startswith("https") and not config.verify_tls:
            self._ssl = ssl.create_default_context()  # verification off: explicitly requested via env
            self._ssl.check_hostname = False
            self._ssl.verify_mode = ssl.CERT_NONE

    def request(self, method, path, body=None, query=None):
        url = self.config.url + "/api/v1" + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Accept": "application/json", "User-Agent": "mydemoticket-mcp/" + __version__,
                   "Authorization": "Bearer " + self.config.token}
        if self.config.tenant:
            headers["X-MyDemoTicket-Tenant"] = self.config.tenant
        if method != "GET":
            headers["X-MyDemoTicket-Source"] = SOURCE
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        LOG.info("%s %s", method, path)
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout, context=self._ssl) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b""
            message = ""
            try:
                message = json.loads(raw.decode("utf-8")).get("error") or ""
            except (ValueError, AttributeError, UnicodeDecodeError):
                message = ""
            raise ApiError("HTTP %d %s%s" % (exc.code, exc.reason or "", (": " + message) if message else ""),
                           exc.code)
        except urllib.error.URLError as exc:
            raise ApiError("cannot reach %s: %s" % (self.config.url, exc.reason))
        except (OSError, ValueError) as exc:  # timeouts, connection resets, bad URL
            raise ApiError("request to %s failed: %s" % (self.config.url, exc.__class__.__name__ + ": " + str(exc)))
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError("the server at %s did not return JSON" % self.config.url)


def ticket_path(number):
    return "/tickets/" + urllib.parse.quote(str(number), safe="")


# ---------------------------------------------------------------------- tool definitions
NUMBER = {"type": "string", "minLength": 1,
          "description": "Ticket number, e.g. \"TKT0001001\" (the numeric id also works)."}
PRIORITY = {"type": "integer", "minimum": 1, "maximum": 4,
            "description": "Priority: 1 critical, 2 high, 3 moderate, 4 low."}
GROUP = {"type": ["string", "null"],
         "description": "Name of an existing team, e.g. \"SOC L1\"; null removes the assignment group."}
ASSIGNEE = {"type": ["string", "null"],
            "description": "Username of an existing user, e.g. \"analyst1\"; null unassigns the ticket."}
CUSTOM_FIELDS = {"type": "object",
                 "description": "Custom field values by field name, validated by the server against the "
                                "definitions from list_custom_fields (merge; a null value removes a field)."}
READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}


def _write(idempotent):
    return {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": idempotent, "openWorldHint": False}


def _schema(properties, required=()):
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return schema


TOOLS = [
    {
        "name": "whoami",
        "title": "Who am I",
        "description": "Return the MyDemoTicket user that the configured API token belongs to "
                       "(username, display name, role admin|agent|integration|requester, teams).",
        "inputSchema": _schema({}),
        "annotations": dict(READ_ONLY, title="Who am I"),
    },
    {
        "name": "list_tickets",
        "title": "List tickets",
        "description": "List tickets, newest update first. All filters are optional and combined with AND. "
                       "Returns {items: [ticket], total: n}; total counts all matches, not only this page.",
        "inputSchema": _schema({
            "state": {"type": "string",
                      "description": "One state or a comma-separated list. Allowed: new, in_progress, on_hold, "
                                     "resolved, closed, cancelled. Example: \"new,in_progress\"."},
            "assignment_group": {"type": "string",
                                 "description": "Team name, e.g. \"SOC L1\"; an empty string matches tickets "
                                                "without a group."},
            "assigned_to": {"type": "string",
                            "description": "Username; an empty string matches unassigned tickets."},
            "q": {"type": "string",
                  "description": "Case-insensitive substring search in number, short description and description."},
            "updated_since": {"type": "string",
                              "description": "ISO-8601 UTC timestamp, e.g. \"2026-11-24T00:00:00Z\"; "
                                             "returns tickets with updated_at >= this value."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20,
                      "description": "Page size, 1-100 (default 20)."},
            "offset": {"type": "integer", "minimum": 0, "default": 0,
                       "description": "Number of tickets to skip for paging (default 0)."},
        }),
        "annotations": dict(READ_ONLY, title="List tickets"),
    },
    {
        "name": "get_ticket",
        "title": "Get ticket",
        "description": "Return one ticket with all fields (state, priority, assignment, custom_fields, "
                       "external_refs, timestamps, close_notes).",
        "inputSchema": _schema({"number": NUMBER}, ["number"]),
        "annotations": dict(READ_ONLY, title="Get ticket"),
    },
    {
        "name": "create_ticket",
        "title": "Create ticket",
        "description": "Create a new ticket in state \"new\" and return it (including its new number). "
                       "The caller is the token's user.",
        "inputSchema": _schema({
            "short_description": {"type": "string", "minLength": 1, "maxLength": 500,
                                  "description": "One-line summary (required, max 500 characters)."},
            "description": {"type": "string", "description": "Longer description."},
            "priority": dict(PRIORITY, description=PRIORITY["description"] + " Default 3."),
            "assignment_group": {"type": "string", "description": "Name of an existing team, e.g. \"SOC L1\"."},
            "assigned_to": {"type": "string", "description": "Username of an existing user, e.g. \"analyst1\"."},
            "category": {"type": "string", "maxLength": 100, "description": "Free text, e.g. \"security\"."},
            "custom_fields": CUSTOM_FIELDS,
        }, ["short_description"]),
        "annotations": dict(_write(False), title="Create ticket"),
    },
    {
        "name": "update_ticket",
        "title": "Update ticket",
        "description": "Change fields of a ticket. Only the fields you pass are sent; custom_fields and "
                       "external_refs are merged (null removes a key). Use set_state to change the state.",
        "inputSchema": _schema({
            "number": NUMBER,
            "short_description": {"type": "string", "minLength": 1, "maxLength": 500,
                                  "description": "One-line summary (max 500 characters)."},
            "description": {"type": "string", "description": "Longer description."},
            "priority": PRIORITY,
            "assignment_group": GROUP,
            "assigned_to": ASSIGNEE,
            "category": {"type": "string", "maxLength": 100, "description": "Free text, e.g. \"security\"."},
            "custom_fields": CUSTOM_FIELDS,
            "external_refs": {"type": "object",
                              "description": "References to other systems, key -> scalar value (merge; null "
                                             "removes a key). Conventions: soar_container_id, es_finding_id."},
        }, ["number"]),
        "annotations": dict(_write(True), title="Update ticket"),
    },
    {
        "name": "set_state",
        "title": "Set ticket state",
        "description": "Move a ticket to another state. Allowed transitions: new -> in_progress|on_hold|resolved|"
                       "cancelled; in_progress -> on_hold|resolved|cancelled; on_hold -> in_progress|resolved|"
                       "cancelled; resolved -> in_progress|closed; closed and cancelled are final. "
                       "close_notes is required for resolved.",
        "inputSchema": _schema({
            "number": NUMBER,
            "state": {"type": "string", "enum": STATES, "description": "Target state."},
            "close_notes": {"type": "string",
                            "description": "Resolution summary; required (non-empty) when state is resolved."},
        }, ["number", "state"]),
        "annotations": dict(_write(True), title="Set ticket state"),
    },
    {
        "name": "add_comment",
        "title": "Add comment or work note",
        "description": "Add a journal entry to a ticket. type comment (default) is visible to the requester; "
                       "work_note is internal and never shown to requesters.",
        "inputSchema": _schema({
            "number": NUMBER,
            "body": {"type": "string", "minLength": 1, "description": "Text of the comment or work note."},
            "type": {"type": "string", "enum": ["comment", "work_note"], "default": "comment",
                     "description": "comment (default, visible to the caller) or work_note (internal)."},
        }, ["number", "body"]),
        "annotations": dict(_write(False), title="Add comment or work note"),
    },
    {
        "name": "get_journal",
        "title": "Get ticket journal",
        "description": "Return the ticket's journal, oldest first: comments, work notes and audit entries "
                       "(field changes with old/new values), each with actor, source and timestamp.",
        "inputSchema": _schema({"number": NUMBER}, ["number"]),
        "annotations": dict(READ_ONLY, title="Get ticket journal"),
    },
    {
        "name": "list_custom_fields",
        "title": "List custom fields",
        "description": "Return the custom field definitions (name, label, type string|number|boolean|choice|date, "
                       "choices, required) that create_ticket and update_ticket accept in custom_fields.",
        "inputSchema": _schema({}),
        "annotations": dict(READ_ONLY, title="List custom fields"),
    },
    {
        "name": "reports_summary",
        "title": "Ticket summary report",
        "description": "Return summary statistics of tickets (GET /reports/summary). Not every server version "
                       "has this endpoint; then the tool says so.",
        "inputSchema": _schema({
            "since": {"type": "string",
                      "description": "Optional ISO-8601 UTC timestamp, e.g. \"2026-11-01T00:00:00Z\"; "
                                     "only count tickets from this time on."},
        }),
        "annotations": dict(READ_ONLY, title="Ticket summary report"),
    },
]
TOOLS_BY_NAME = dict((tool["name"], tool) for tool in TOOLS)


# ---------------------------------------------------------------------- argument validation
class ToolError(Exception):
    """A tool failure shown to the model as a result with isError: true."""


_JSON_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def validate(schema, args):
    """Check args against the (small) subset of JSON Schema used by TOOLS; raise ToolError."""
    if not isinstance(args, dict):
        raise ToolError("arguments must be a JSON object")
    props = schema.get("properties", {})
    unknown = sorted(set(args) - set(props))
    if unknown:
        raise ToolError("unknown argument(s): %s; allowed: %s" % (", ".join(unknown), ", ".join(sorted(props)) or "none"))
    for name in schema.get("required", []):
        if name not in args:
            raise ToolError("missing required argument: %s" % name)
    for name, value in args.items():
        prop = props[name]
        types = prop["type"] if isinstance(prop["type"], list) else [prop["type"]]
        if not any(_JSON_TYPES[t](value) for t in types):
            raise ToolError("argument %s must be of type %s" % (name, " or ".join(types)))
        if value is None:
            continue
        if "enum" in prop and value not in prop["enum"]:
            raise ToolError("argument %s must be one of: %s" % (name, ", ".join(prop["enum"])))
        if isinstance(value, str):
            if len(value.strip()) < prop.get("minLength", 0):
                raise ToolError("argument %s must not be empty" % name)
            if "maxLength" in prop and len(value) > prop["maxLength"]:
                raise ToolError("argument %s is longer than %d characters" % (name, prop["maxLength"]))
        if isinstance(value, int):
            if "minimum" in prop and value < prop["minimum"]:
                raise ToolError("argument %s must be >= %d" % (name, prop["minimum"]))
            if "maximum" in prop and value > prop["maximum"]:
                raise ToolError("argument %s must be <= %d" % (name, prop["maximum"]))


# ---------------------------------------------------------------------- tool implementations
def _pick(args, names):
    return dict((name, args[name]) for name in names if name in args)


def tool_whoami(api, args):
    return api.request("GET", "/me")


def tool_list_tickets(api, args):
    query = _pick(args, ("state", "assignment_group", "assigned_to", "q", "updated_since"))
    if "state" in query:
        bad = [s for s in (p.strip() for p in query["state"].split(",")) if s not in STATES]
        if bad:
            raise ToolError("unknown state(s): %s; allowed: %s" % (", ".join(bad), ", ".join(STATES)))
    query["limit"] = args.get("limit", 20)
    query["offset"] = args.get("offset", 0)
    return api.request("GET", "/tickets", query=query)


def tool_get_ticket(api, args):
    return api.request("GET", ticket_path(args["number"]))


def tool_create_ticket(api, args):
    body = _pick(args, ("short_description", "description", "priority", "assignment_group", "assigned_to",
                        "category", "custom_fields"))
    return api.request("POST", "/tickets", body=body)


def tool_update_ticket(api, args):
    body = _pick(args, ("short_description", "description", "priority", "assignment_group", "assigned_to",
                        "category", "custom_fields", "external_refs"))
    if not body:
        raise ToolError("nothing to update: pass at least one field besides number")
    return api.request("PATCH", ticket_path(args["number"]), body=body)


def tool_set_state(api, args):
    body = {"state": args["state"]}
    notes = args.get("close_notes")
    if args["state"] == "resolved" and not (notes and notes.strip()):
        raise ToolError("close_notes is required to set state resolved; describe how the ticket was resolved")
    if notes:
        body["close_notes"] = notes
    return api.request("PATCH", ticket_path(args["number"]), body=body)


def tool_add_comment(api, args):
    body = {"body": args["body"], "type": args.get("type", "comment")}
    return api.request("POST", ticket_path(args["number"]) + "/comments", body=body)


def tool_get_journal(api, args):
    return api.request("GET", ticket_path(args["number"]) + "/journal")


def tool_list_custom_fields(api, args):
    return api.request("GET", "/custom-fields")


def tool_reports_summary(api, args):
    query = _pick(args, ("since",))
    try:
        return api.request("GET", "/reports/summary", query=query or None)
    except ApiError as exc:
        if exc.status == 404:
            raise ToolError("reports_summary is not available on this server version "
                            "(GET /api/v1/reports/summary returned 404)")
        raise


HANDLERS = {
    "whoami": tool_whoami,
    "list_tickets": tool_list_tickets,
    "get_ticket": tool_get_ticket,
    "create_ticket": tool_create_ticket,
    "update_ticket": tool_update_ticket,
    "set_state": tool_set_state,
    "add_comment": tool_add_comment,
    "get_journal": tool_get_journal,
    "list_custom_fields": tool_list_custom_fields,
    "reports_summary": tool_reports_summary,
}


def _text_result(text, is_error=False, structured=None):
    result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if structured is not None:
        result["structuredContent"] = structured
    return result


def call_tool(config, name, args):
    """Run one tool; always returns a CallToolResult (errors as isError: true)."""
    tool = TOOLS_BY_NAME[name]
    try:
        validate(tool["inputSchema"], args)
        if not config.token:
            raise ToolError("MYDEMOTICKET_TOKEN is not set. Create an API token in MyDemoTicket (Admin -> Users -> "
                            "Tokens) and provide it via the environment or, on macOS, the keychain item "
                            "used by run.sh (service \"mydemoticket\").")
        data = HANDLERS[name](ApiClient(config), args)
    except ToolError as exc:
        return _text_result("Error: %s" % exc, is_error=True)
    except ApiError as exc:
        return _text_result("Error: %s failed: %s" % (name, exc), is_error=True)
    if isinstance(data, list):
        data = {"items": data, "total": len(data)}
    elif not isinstance(data, dict):
        data = {"result": data}
    return _text_result(json.dumps(data, separators=(",", ":"), ensure_ascii=False), structured=data)


# ---------------------------------------------------------------------- JSON-RPC / MCP
def _error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


class McpServer(object):
    def __init__(self, config):
        self.config = config
        self.protocol_version = None

    def handle(self, message):
        """Handle one decoded JSON-RPC message; return a response dict or None (notifications, responses)."""
        if not isinstance(message, dict):
            return _error(None, INVALID_REQUEST, "Invalid Request: expected a JSON object")
        is_request = "id" in message
        method = message.get("method")
        if not isinstance(method, str):
            if is_request and ("result" in message or "error" in message):
                return None  # a response to a server request; we never send any
            return _error(message.get("id"), INVALID_REQUEST, "Invalid Request: missing method") if is_request else None
        msg_id = message.get("id")
        params = message.get("params") or {}
        if not is_request:
            LOG.debug("notification %s", method)
            return None  # notifications (incl. notifications/initialized) are never answered
        if not isinstance(params, dict):
            return _error(msg_id, INVALID_PARAMS, "params must be an object")
        try:
            if method == "initialize":
                return _result(msg_id, self.initialize(params))
            if method == "ping":
                return _result(msg_id, {})
            if method == "tools/list":
                return _result(msg_id, {"tools": TOOLS})
            if method == "tools/call":
                name = params.get("name")
                if name not in TOOLS_BY_NAME:
                    return _error(msg_id, INVALID_PARAMS, "Unknown tool: %s" % (name,))
                args = params.get("arguments")
                return _result(msg_id, call_tool(self.config, name, {} if args is None else args))
        except Exception:  # never crash the server on one bad message
            LOG.exception("internal error in %s", method)
            return _error(msg_id, INTERNAL_ERROR, "Internal error")
        return _error(msg_id, METHOD_NOT_FOUND, "Method not found: %s" % method)

    def initialize(self, params):
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
        self.protocol_version = version
        info = params.get("clientInfo") or {}
        LOG.info("initialize from %s, protocol %s", info.get("name", "?"), version)
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "mydemoticket", "title": "MyDemoTicket", "version": __version__},
            "instructions": "Tools for the MyDemoTicket ticket system: list, read, create and update tickets, "
                            "change states, add comments/work notes, read journals and custom field definitions.",
        }

    def handle_line(self, line):
        """Handle one raw input line; return the response object (dict/list) or None."""
        line = line.strip()
        if not line:
            return None
        try:
            message = json.loads(line)
        except ValueError:
            return _error(None, PARSE_ERROR, "Parse error")
        if isinstance(message, list):  # JSON-RPC batch (protocol 2025-03-26 and older)
            if not message:
                return _error(None, INVALID_REQUEST, "Invalid Request: empty batch")
            responses = [r for r in (self.handle(m) for m in message) if r is not None]
            return responses or None
        return self.handle(message)

    def serve(self, stdin, stdout):
        for raw in iter(stdin.readline, b""):
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                response = _error(None, PARSE_ERROR, "Parse error: input is not UTF-8")
            else:
                response = self.handle_line(line)
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
                stdout.flush()


def main(argv=None):
    logging.basicConfig(stream=sys.stderr, format="mydemoticket-mcp %(levelname)s %(message)s",
                        level=getattr(logging, (os.environ.get("MYDEMOTICKET_LOG_LEVEL") or "WARNING").upper(),
                                      logging.WARNING))
    config = Config.from_env()
    if not config.token:
        LOG.warning("MYDEMOTICKET_TOKEN is not set; tool calls will return an error")
    LOG.info("starting %s, %r", __version__, config)
    try:
        McpServer(config).serve(sys.stdin.buffer, sys.stdout.buffer)
    except KeyboardInterrupt:
        pass
    return 0
