"""HTTP server: routing, authentication, JSON handling and static files."""

import http.cookies
import json
import mimetypes
import os
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .api import Api, ApiError
from .auth import (
    CSRF_HEADER,
    SESSION_COOKIE,
    SESSION_TTL_HOURS,
    AuthError,
    bootstrap,
    check_csrf,
    principal_from_session,
    principal_from_token,
)
from .db import Database
from .webhooks import WebhookDispatcher

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_BODY = 1024 * 1024
SOURCE_HEADER = "X-MyDemoTicket-Source"

# (method, pattern, handler name, auth required)
ROUTES = [
    ("GET", r"/health", "health", False),
    ("POST", r"/auth/login", "_login", False),
    ("POST", r"/auth/logout", "_logout", True),
    ("GET", r"/me", "me", True),
    ("GET", r"/tickets", "list_tickets", True),
    ("POST", r"/tickets", "create_ticket", True),
    ("GET", r"/tickets/(?P<number>[^/]+)", "get_ticket", True),
    ("PATCH", r"/tickets/(?P<number>[^/]+)", "patch_ticket", True),
    ("GET", r"/tickets/(?P<number>[^/]+)/journal", "journal", True),
    ("POST", r"/tickets/(?P<number>[^/]+)/comments", "add_comment", True),
    ("GET", r"/custom-fields", "list_custom_fields", True),
    ("POST", r"/custom-fields", "create_custom_field", True),
    ("PATCH", r"/custom-fields/(?P<name>[^/]+)", "patch_custom_field", True),
    ("DELETE", r"/custom-fields/(?P<name>[^/]+)", "delete_custom_field", True),
    ("GET", r"/users", "list_users", True),
    ("POST", r"/users", "create_user", True),
    ("PATCH", r"/users/(?P<username>[^/]+)", "patch_user", True),
    ("GET", r"/users/(?P<username>[^/]+)/tokens", "list_tokens", True),
    ("POST", r"/users/(?P<username>[^/]+)/tokens", "create_token", True),
    ("DELETE", r"/users/(?P<username>[^/]+)/tokens/(?P<token_id>\d+)", "delete_token", True),
    ("GET", r"/teams", "list_teams", True),
    ("POST", r"/teams", "create_team", True),
    ("POST", r"/teams/(?P<name>[^/]+)/members", "add_team_member", True),
    ("DELETE", r"/teams/(?P<name>[^/]+)/members/(?P<username>[^/]+)", "remove_team_member", True),
    ("GET", r"/webhooks", "list_webhooks", True),
    ("POST", r"/webhooks", "create_webhook", True),
    ("DELETE", r"/webhooks/(?P<hook_id>\d+)", "delete_webhook", True),
    ("GET", r"/webhooks/(?P<hook_id>\d+)/deliveries", "webhook_deliveries", True),
]
COMPILED = [(m, re.compile("^/api/v1" + p + "/?$"), h, a) for m, p, h, a in ROUTES]

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


class RequestContext:
    def __init__(self, principal, query, body, source, headers):
        self.principal = principal
        self.query = query
        self.body = body
        self.source = source
        self.headers = headers


class App:
    """Wires database, API and webhook dispatcher together."""

    def __init__(self, data_dir, seed=True, admin_password=None, admin_token=None,
                 webhook_backoff=1.0, secure_cookies=False, quiet=False):
        self.data_dir = os.path.abspath(os.path.expanduser(data_dir))
        os.makedirs(self.data_dir, mode=0o700, exist_ok=True)
        self.db = Database(os.path.join(self.data_dir, "mydemoticket.sqlite3"))
        self.credentials_file = bootstrap(
            self.db, self.data_dir, seed=seed, admin_password=admin_password, admin_token=admin_token
        )
        self.api = Api(self.db, None)
        self.dispatcher = WebhookDispatcher(
            self.db, self.api.store_soar_container_id, backoff_seconds=webhook_backoff
        )
        self.api.dispatcher = self.dispatcher
        self.secure_cookies = secure_cookies
        self.quiet = quiet
        self.httpd = None

    def make_server(self, host="127.0.0.1", port=8080):
        handler = type("BoundHandler", (Handler,), {"app": self})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.dispatcher.start()
        return self.httpd

    def shutdown(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.dispatcher.stop()
        self.db.close()


class Handler(BaseHTTPRequestHandler):
    app = None  # set on the bound subclass
    server_version = "MyDemoTicket/" + __version__
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):
        if not self.app.quiet:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status, payload=None, headers=None, raw=None, content_type="application/json"):
        if raw is None and payload is not None:
            raw = json.dumps(payload).encode("utf-8")
        raw = raw or b""
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Type", content_type + ("; charset=utf-8" if "json" in content_type
                                                             or content_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(raw)) if status != 204 else "0")
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        if content_type == "application/json":
            self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            if isinstance(value, list):
                for item in value:
                    self.send_header(key, item)
            else:
                self.send_header(key, value)
        self.end_headers()
        if status != 204 and self.command != "HEAD":
            self.wfile.write(raw)

    def _error(self, status, message, headers=None):
        self._send(status, {"error": message}, headers=headers)

    def _read_body(self):
        length = self.headers.get("Content-Length")
        if length is None:
            return {}
        try:
            length = int(length)
        except ValueError:
            raise ApiError(400, "invalid Content-Length")
        if length > MAX_BODY:
            raise ApiError(413, "request body too large")
        data = self.rfile.read(length) if length > 0 else b""
        if not data.strip():
            return {}
        try:
            return json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "request body is not valid JSON")

    def _cookie(self, name):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            return None
        morsel = jar.get(name)
        return morsel.value if morsel else None

    def _session_cookie(self, value, max_age):
        parts = ["%s=%s" % (SESSION_COOKIE, value), "Path=/", "HttpOnly", "SameSite=Strict",
                 "Max-Age=%d" % max_age]
        if self.app.secure_cookies or self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            parts.append("Secure")
        return "; ".join(parts)

    def _authenticate(self):
        auth_header = self.headers.get("Authorization", "")
        if auth_header:
            scheme, _, token = auth_header.partition(" ")
            if scheme.lower() != "bearer" or not token.strip():
                raise AuthError(401, "unsupported Authorization header (use Bearer)")
            principal = principal_from_token(self.app.db, token.strip())
            if principal is None:
                raise AuthError(401, "invalid or revoked token")
            return principal
        principal = principal_from_session(self.app.db, self._cookie(SESSION_COOKIE))
        if principal is None:
            raise AuthError(401, "authentication required")
        if self.command not in ("GET", "HEAD"):
            check_csrf(principal, self.headers.get(CSRF_HEADER))
        return principal

    def _source(self, principal):
        value = (self.headers.get(SOURCE_HEADER) or "").strip()
        if value:
            return value[:64]
        if principal is not None and principal.via == "session":
            return "web"
        return "api"

    # -- dispatch ---------------------------------------------------------
    def _handle(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if not path.startswith("/api/"):
            if self.command in ("GET", "HEAD"):
                return self._static(path)
            return self._error(405, "method not allowed")
        try:
            match, allowed = None, []
            for method, regex, handler, auth in COMPILED:
                m = regex.match(path)
                if m:
                    allowed.append(method)
                    if method == self.command or (method == "GET" and self.command == "HEAD"):
                        match = (handler, auth, m.groupdict())
                        break
            if match is None:
                if allowed:
                    return self._error(405, "method not allowed", {"Allow": ", ".join(sorted(set(allowed)))})
                return self._error(404, "not found")
            handler, auth, params = match
            params = {k: urllib.parse.unquote(v) for k, v in params.items()}
            principal = self._authenticate() if auth else None
            body = self._read_body() if self.command in ("POST", "PATCH", "PUT") else {}
            query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}
            ctx = RequestContext(principal, query, body, self._source(principal), self.headers)
            if handler == "_login":
                return self._login(ctx)
            if handler == "_logout":
                return self._logout(ctx)
            status, payload = getattr(self.app.api, handler)(ctx, **params)
            self._send(status, payload)
        except AuthError as exc:
            headers = {"WWW-Authenticate": 'Bearer realm="mydemoticket"'} if exc.status == 401 else None
            self._error(exc.status, exc.message, headers)
        except ApiError as exc:
            self._error(exc.status, exc.message)
        except Exception as exc:  # pragma: no cover - last resort
            sys.stderr.write("internal error: %r\n" % (exc,))
            self._error(500, "internal server error")

    def _login(self, ctx):
        body = ctx.body if isinstance(ctx.body, dict) else {}
        session, _csrf, user = self.app.api.login(body.get("username"), body.get("password"))
        self._send(200, user, headers={"Set-Cookie": self._session_cookie(session, SESSION_TTL_HOURS * 3600)})

    def _logout(self, ctx):
        status, payload = self.app.api.logout(self._cookie(SESSION_COOKIE))
        self._send(status, payload, headers={"Set-Cookie": self._session_cookie("", 0)})

    def _static(self, path):
        if path in ("/", "/index.html"):
            name = "index.html"
        elif path.startswith("/static/"):
            name = path[len("/static/"):]
        else:
            return self._error(404, "not found")
        if not re.match(r"^[A-Za-z0-9_-]+\.(html|js|css|svg|ico|png)$", name):
            return self._error(404, "not found")
        file_path = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(file_path):
            return self._error(404, "not found")
        with open(file_path, "rb") as handle:
            data = handle.read()
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.endswith(".js"):
            ctype = "text/javascript"
        self._send(200, raw=data, content_type=ctype, headers={"Cache-Control": "no-cache"})

    def do_GET(self):
        self._handle()

    def do_HEAD(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PATCH(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def do_PUT(self):
        self._handle()
