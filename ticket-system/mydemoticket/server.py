"""HTTP server: routing, authentication, JSON handling and static files."""

import http.cookies
import json
import mimetypes
import os
import re
import sys
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .api import Api, ApiError
from .audit import AuditLogger
from .auth import (
    CSRF_HEADER,
    SESSION_COOKIE,
    SESSION_TTL_HOURS,
    AuthError,
    bootstrap,
    check_csrf,
    principal_from_session,
    principal_from_token,
    resolve_tenant,
)
from .db import Database
from .settings import apply_splunk, splunk_config
from .tenants import TENANT_HEADER, default_tenant, tenant_by_id, tenant_by_slug
from .webhooks import WebhookDispatcher

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_BODY = 1024 * 1024
SOURCE_HEADER = "X-MyDemoTicket-Source"

# (method, pattern, handler name, auth required)
ROUTES = [
    ("GET", r"/health", "health", False),
    ("POST", r"/auth/login", "_login", False),
    ("POST", r"/auth/logout", "_logout", True),
    ("POST", r"/auth/tenant", "_switch_tenant", True),
    ("GET", r"/me", "me", True),
    ("PATCH", r"/me", "patch_me", True),
    ("POST", r"/me/password", "change_own_password", True),
    ("GET", r"/tenants", "list_tenants", True),
    ("POST", r"/tenants", "create_tenant", True),
    ("GET", r"/tenants/(?P<slug>[^/]+)", "get_tenant", True),
    ("PATCH", r"/tenants/(?P<slug>[^/]+)", "patch_tenant", True),
    ("GET", r"/reports/summary", "reports_summary", True),
    ("GET", r"/audit/status", "audit_status", True),
    ("GET", r"/settings/splunk", "get_splunk_settings", True),
    ("PUT", r"/settings/splunk", "put_splunk_settings", True),
    ("POST", r"/settings/splunk/test", "test_splunk_settings", True),
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
    ("GET", r"/permissions", "list_permissions", True),
    ("GET", r"/roles", "list_roles", True),
    ("POST", r"/roles", "create_role", True),
    ("PATCH", r"/roles/(?P<name>[^/]+)", "patch_role", True),
    ("DELETE", r"/roles/(?P<name>[^/]+)", "delete_role", True),
    ("GET", r"/webhooks", "list_webhooks", True),
    ("POST", r"/webhooks", "create_webhook", True),
    ("DELETE", r"/webhooks/(?P<hook_id>\d+)", "delete_webhook", True),
    ("GET", r"/webhooks/(?P<hook_id>\d+)/deliveries", "webhook_deliveries", True),
    ("GET", r"/pairing", "pairing_status", True),
    ("POST", r"/pairing/check", "pairing_check", True),
    ("POST", r"/pairing/apply", "pairing_apply", True),
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
    def __init__(self, principal, query, body, source, headers, tenant=None, request_id=None, ip=None,
                 method=None, path=None):
        self.principal = principal
        self.query = query
        self.body = body
        self.source = source
        self.headers = headers
        self.tenant = tenant          # tenants row the request works in (None before auth)
        self.request_id = request_id
        self.ip = ip
        self.method = method
        self.path = path


class App:
    """Wires database, API and webhook dispatcher together."""

    def __init__(self, data_dir, seed=True, admin_password=None, admin_token=None,
                 webhook_backoff=1.0, secure_cookies=False, quiet=False, audit_logger=None):
        self.data_dir = os.path.abspath(os.path.expanduser(data_dir))
        os.makedirs(self.data_dir, mode=0o700, exist_ok=True)
        self.db = Database(os.path.join(self.data_dir, "mydemoticket.sqlite3"))
        self.credentials_file = bootstrap(
            self.db, self.data_dir, seed=seed, admin_password=admin_password, admin_token=admin_token
        )
        # audit_logger is injectable for tests (any object with log/stats/start/stop)
        self.audit = audit_logger if audit_logger is not None else AuditLogger.from_env(
            self.data_dir, index_resolver=self.tenant_index
        )
        if splunk_config(self.db)["source"] == "settings":
            apply_splunk(self.db, self.audit)  # stored connection wins over MYDEMOTICKET_HEC_*
        if hasattr(self.audit, "start"):
            self.audit.start()
        self.api = Api(self.db, None, audit=self.audit)
        self.dispatcher = WebhookDispatcher(
            self.db, self.api.store_soar_container_id, backoff_seconds=webhook_backoff, audit=self.audit
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

    def tenant_index(self, slug):
        """index_resolver for the audit logger: tenants.splunk_index or None (= default index)."""
        try:
            row = tenant_by_slug(self.db, slug)
        except Exception:  # database already closed during shutdown
            return None
        return row["splunk_index"] if row is not None and row["splunk_index"] else None

    def shutdown(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.dispatcher.stop()
        if hasattr(self.audit, "stop"):
            self.audit.stop()
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
        self.send_header("X-Request-Id", getattr(self, "_request_id", None) or str(uuid.uuid4()))
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

    def _client_ip(self):
        peer = self.client_address[0] if self.client_address else None
        forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if forwarded and peer in ("127.0.0.1", "::1"):
            return forwarded[:64]  # behind the local reverse proxy (Caddy)
        return peer

    def _denied_tenant(self, principal, ctx):
        """Tenant slug for an access.denied event: request tenant, else the user's tenant."""
        if ctx is not None and ctx.tenant is not None:
            return ctx.tenant["slug"]
        if principal is not None:
            row = tenant_by_id(self.app.db, principal.tenant_id) if principal.tenant_id is not None \
                else default_tenant(self.app.db)
            if row is not None:
                return row["slug"]
        return "unknown"

    def _log_denied(self, status, message, principal, ctx, source):
        path = urllib.parse.urlsplit(self.path).path
        if status == 401 and path == "/api/v1/auth/login":
            return  # already logged as auth.login/failure with the user's tenant
        self.app.audit.log(
            "access.denied", tenant=self._denied_tenant(principal, ctx),
            actor=principal.username if principal is not None else None,
            role=principal.role if principal is not None else None, outcome="denied",
            source=source, ip=self._client_ip(), request_id=self._request_id,
            details={"method": self.command, "path": urllib.parse.urlsplit(self.path).path[:512],
                     "status": status, "reason": message},
        )

    def _source(self, principal):
        value = (self.headers.get(SOURCE_HEADER) or "").strip()
        if value:
            return value[:64]
        if principal is not None and principal.via == "session":
            return "web"
        return "api"

    # -- dispatch ---------------------------------------------------------
    def _handle(self):
        self._request_id = str(uuid.uuid4())
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if not path.startswith("/api/"):
            if self.command in ("GET", "HEAD"):
                return self._static(path)
            return self._error(405, "method not allowed")
        principal, ctx = None, None
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
            ctx = RequestContext(None, {}, {}, self._source(None), self.headers, request_id=self._request_id,
                                 ip=self._client_ip(), method=self.command, path=path)
            principal = self._authenticate() if auth else None
            ctx.principal = principal
            ctx.source = self._source(principal)
            if principal is not None:
                ctx.tenant = resolve_tenant(self.app.db, principal, self.headers.get(TENANT_HEADER))
            ctx.body = self._read_body() if self.command in ("POST", "PATCH", "PUT") else {}
            ctx.query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}
            if handler == "_login":
                return self._login(ctx)
            if handler == "_logout":
                return self._logout(ctx)
            if handler == "_switch_tenant":
                status, payload = self.app.api.switch_tenant(ctx, self._cookie(SESSION_COOKIE))
                return self._send(status, payload)
            status, payload = getattr(self.app.api, handler)(ctx, **params)
            self._send(status, payload)
        except AuthError as exc:
            if exc.status in (401, 403):
                self._log_denied(exc.status, exc.message, principal, ctx, ctx.source if ctx else None)
            headers = {"WWW-Authenticate": 'Bearer realm="mydemoticket"'} if exc.status == 401 else None
            self._error(exc.status, exc.message, headers)
        except ApiError as exc:
            if exc.status in (401, 403):
                self._log_denied(exc.status, exc.message, principal, ctx, ctx.source if ctx else None)
            self._error(exc.status, exc.message)
        except Exception as exc:  # pragma: no cover - last resort
            sys.stderr.write("internal error: %r\n" % (exc,))
            self._error(500, "internal server error")

    def _login(self, ctx):
        session, _csrf, user = self.app.api.login(ctx)
        self._send(200, user, headers={"Set-Cookie": self._session_cookie(session, SESSION_TTL_HOURS * 3600)})

    def _logout(self, ctx):
        status, payload = self.app.api.logout(ctx, self._cookie(SESSION_COOKIE))
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
