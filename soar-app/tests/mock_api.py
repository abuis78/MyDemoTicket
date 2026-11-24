"""Local stdlib HTTP mock of the MyDemoTicket API (/api/v1), bound to 127.0.0.1 port 0."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VALID_TOKEN = "test-token"
TRANSITIONS = {
    "new": {"in_progress", "on_hold", "resolved", "cancelled"},
    "in_progress": {"on_hold", "resolved", "cancelled"},
    "on_hold": {"in_progress", "resolved", "cancelled"},
    "resolved": {"closed", "in_progress"},
    "closed": set(),
    "cancelled": set(),
}


class MockTicketAPI(object):
    def __init__(self):
        self.requests = []  # (method, path, query, headers, body)
        self.role = "integration"
        self.broken_json_path = None
        self.journal_source = {}  # number -> source of newest journal entry
        self.tickets = {}
        self.counter = 1000
        self.add_ticket(short_description="Seed ticket", state="new", updated_at="2026-11-24T10:00:00Z")
        self.add_ticket(short_description="Second ticket", state="in_progress", priority=1,
                        updated_at="2026-11-24T11:00:00Z", external_refs={"es_finding_id": "abc-123"})
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, payload, raw=None):
                data = raw if raw is not None else json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method):
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw.decode("utf-8")) if raw else None
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                api.requests.append((method, parsed.path, query, {k.lower(): v for k, v in self.headers.items()}, body))
                if self.headers.get("Authorization") != "Bearer " + VALID_TOKEN:
                    return self._send(401, {"error": "invalid token"})
                if api.broken_json_path and parsed.path == api.broken_json_path:
                    return self._send(200, None, raw=b"{not json")
                code, payload = api.route(method, parsed.path, query, body)
                self._send(code, payload)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PATCH(self):
                self._handle("PATCH")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def base_url(self):
        return "http://127.0.0.1:{}".format(self.server.server_address[1])

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def add_ticket(self, **fields):
        self.counter += 1
        number = "TKT000{}".format(self.counter)
        ticket = {
            "number": number, "id": str(self.counter), "short_description": "", "description": "",
            "state": "new", "priority": 3, "assignment_group": "SOC", "assigned_to": None, "caller": "demo",
            "category": None, "custom_fields": {}, "external_refs": {}, "created_at": "2026-11-24T09:00:00Z",
            "updated_at": "2026-11-24T09:00:00Z", "resolved_at": None, "close_notes": None,
        }
        ticket.update(fields)
        self.tickets[number] = ticket
        return ticket

    def route(self, method, path, query, body):
        prefix = "/api/v1"
        if not path.startswith(prefix):
            return 404, {"error": "not found"}
        parts = [p for p in path[len(prefix):].split("/") if p]
        if parts == ["health"] and method == "GET":
            return 200, {"status": "ok", "version": "0.1.0"}
        if parts == ["me"] and method == "GET":
            return 200, {"username": "soar-integration", "role": self.role}
        if parts == ["custom-fields"] and method == "GET":
            return 200, [{"name": "mitre_tactic", "label": "MITRE Tactic", "type": "string",
                          "choices": None, "required": False}]
        if parts == ["tickets"] and method == "GET":
            items = list(self.tickets.values())
            if query.get("state"):
                items = [t for t in items if t["state"] == query["state"]]
            if query.get("updated_since"):
                items = [t for t in items if t["updated_at"] >= query["updated_since"]]
            offset, limit = int(query.get("offset", 0)), int(query.get("limit", 100))
            return 200, {"items": items[offset:offset + limit], "total": len(items)}
        if parts == ["tickets"] and method == "POST":
            if not (body or {}).get("short_description"):
                return 422, {"error": "short_description is required"}
            return 201, self.add_ticket(**body)
        if len(parts) >= 2 and parts[0] == "tickets":
            ticket = self.tickets.get(parts[1])
            if ticket is None:
                return 404, {"error": "ticket {} not found".format(parts[1])}
            if len(parts) == 2 and method == "GET":
                return 200, ticket
            if len(parts) == 2 and method == "PATCH":
                new_state = body.get("state")
                if new_state and new_state != ticket["state"] and new_state not in TRANSITIONS[ticket["state"]]:
                    return 409, {"error": "invalid transition {} -> {}".format(ticket["state"], new_state)}
                for key, value in body.items():
                    if key in ("custom_fields", "external_refs"):
                        ticket[key].update(value)
                    else:
                        ticket[key] = value
                return 200, ticket
            if parts[2:] == ["journal"] and method == "GET":
                entries = [{"id": "1", "type": "created", "author": "demo", "source": "web",
                            "body": "", "created_at": "2026-11-24T09:00:00Z"}]
                if parts[1] in self.journal_source:
                    entries.append({"id": "2", "type": "field_change", "author": "x",
                                    "source": self.journal_source[parts[1]], "body": "",
                                    "created_at": "2026-11-24T12:00:00Z"})
                return 200, entries
            if parts[2:] == ["comments"] and method == "POST":
                if body.get("type") not in ("comment", "work_note"):
                    return 422, {"error": "invalid type"}
                return 201, {"id": "99", "type": body["type"], "body": body["body"],
                             "created_at": "2026-11-24T12:00:00Z"}
        return 404, {"error": "not found"}
