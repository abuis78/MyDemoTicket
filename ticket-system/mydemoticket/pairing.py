"""ES + SOAR pairing wizard: connects a tenant to Splunk SOAR in one run (stdlib only).

``POST /pairing/apply`` automates the manual steps of docs/installation.md sections 8-10:

1. ``user``      integration user ``soar-<tenant>`` (role integration) in the ticket system
2. ``webhook``   webhook of type ``soar`` for the SOAR URL (stores the SOAR token as webhook secret)
3. ``label``     SOAR container label
4. ``app``       MyDemoTicket SOAR app from the bundled ``soar-app/mydemoticket`` (skipped if same version)
5. ``asset``     SOAR asset of the app pointing at the ticket system, with an integration API token
6. ``playbook_es_mirror`` / ``playbook_ticket_from_es``  both playbooks imported (asset names substituted)
   and activated

Every step is idempotent and reports ``{step, status, detail}`` with status
created | updated | exists | skipped | failed.

Token rules
-----------
* The SOAR ``ph-auth-token`` is used for the SOAR REST calls of the request and is stored only as the
  secret of the ``soar`` webhook (needed for ticket -> SOAR deliveries). It is never returned, logged
  or written to the audit log.
* The integration API token is created only when the SOAR asset has to be written (new asset, changed
  asset configuration, or the token recorded in the pairing state was revoked). After the asset was
  written successfully the previous pairing token is revoked. A second run with unchanged input does
  not rotate anything.

The pairing state (no secrets: URLs, names, SOAR ids, token id, playbook hashes, last results) is kept
in the ``settings`` table under ``pairing.<tenant id>``.
"""

import base64
import hashlib
import hmac
import io
import json
import os
import re
import ssl
import tarfile
import threading
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .auth import hash_token, new_token, require_permission
from .db import utcnow
from .settings import get_settings, put_setting

BUNDLE_ENV = "MYDEMOTICKET_SOAR_BUNDLE_DIR"   # directory that contains soar-app/ and soar-playbooks/
PUBLIC_URL_ENV = "MYDEMOTICKET_PUBLIC_URL"   # default ticket_base_url (else taken from the request host)
STATE_PREFIX = "pairing."
TOKEN_NAME = "pairing"

# same file list as soar-app/build.py (tests/test_pairing.py checks that they match)
APP_FILES = (
    "__init__.py",
    "mydemoticket.json",
    "mydemoticket_connector.py",
    "mydemoticket_consts.py",
    "mydemoticket.svg",
    "mydemoticket_dark.svg",
    "README.md",
)
# playbook name -> {constant: request field}; the es mirror playbook is bound to the container label
PLAYBOOKS = (
    ("mydemoticket_es_mirror", {"SPLUNK_ASSET": "splunk_asset", "ES_ASSET": "es_asset"}, True),
    ("mydemoticket_ticket_from_es", {"MYDEMOTICKET_ASSET": "app_asset"}, False),
)
STEPS = ("user", "webhook", "label", "app", "asset") + tuple("playbook_" + n[len("mydemoticket_"):]
                                                            for n, _, _ in PLAYBOOKS)
STEP_ALIASES = {"playbooks": tuple(s for s in STEPS if s.startswith("playbook_"))}
REQUIRED_COA = {"python_version": "3.13", "schema": "5.0.9", "version": "8.6.0.530"}

DEFAULTS = {
    "label": "mydemoticket",
    "splunk_asset": "splunk",
    "es_asset": "builtin_mc_connector",
    "app_asset": "mydemoticket",
    "verify_tls": True,
    "ticket_verify_tls": True,
}
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
URL_RE = re.compile(r"^https?://[^\s/?#]+(/[^\s?#]*)?$", re.IGNORECASE)
APPLY_FIELDS = {"soar_url", "soar_token", "verify_tls", "label", "splunk_asset", "es_asset", "app_asset",
                "ticket_base_url", "ticket_verify_tls", "steps"}
CHECK_FIELDS = {"soar_url", "soar_token", "verify_tls", "label", "splunk_asset", "es_asset", "app_asset"}
MAX_DETAIL = 300

_APPLY_LOCK = threading.Lock()


# ===================================================================== bundle
def bundle_dir(override=None):
    """Directory holding ``soar-app/mydemoticket`` and ``soar-playbooks``, or None.

    Order: explicit override (tests), ``$MYDEMOTICKET_SOAR_BUNDLE_DIR``, the directory next to the
    installed package (deploy/install.sh copies both there), the repository root (source checkout).
    """
    package = os.path.dirname(os.path.abspath(__file__))
    candidates = [override, os.environ.get(BUNDLE_ENV) or None, os.path.dirname(package),
                  os.path.dirname(os.path.dirname(package))]
    for base in candidates:
        if base and os.path.isfile(os.path.join(base, "soar-app", "mydemoticket", "mydemoticket.json")) \
                and os.path.isdir(os.path.join(base, "soar-playbooks")):
            return base
    return None


def _app_dir(base):
    return os.path.join(base, "soar-app", "mydemoticket")


def app_manifest(base):
    with open(os.path.join(_app_dir(base), "mydemoticket.json"), encoding="utf-8") as handle:
        return json.load(handle)


def _tar_add(tar, name, data, mode=0o644, is_dir=False):
    info = tarfile.TarInfo(name)
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mtime = 0
    if is_dir:
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        tar.addfile(info)
        return
    info.mode = mode
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def build_app_tgz(base):
    """The SOAR app archive (root ``mydemoticket/``) built in memory like soar-app/build.py."""
    app_dir = _app_dir(base)
    missing = [f for f in APP_FILES if not os.path.isfile(os.path.join(app_dir, f))]
    if missing:
        raise ValueError("missing app files: %s" % ", ".join(missing))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _tar_add(tar, "mydemoticket", None, is_dir=True)
        for name in APP_FILES:
            with open(os.path.join(app_dir, name), "rb") as handle:
                _tar_add(tar, "mydemoticket/" + name, handle.read())
    return buf.getvalue()


def _replace_constant(source, constant, value):
    pattern = re.compile(r'^(%s\s*=\s*)"[^"\n]*"' % re.escape(constant), re.M)
    if not pattern.search(source):
        raise ValueError("constant %s not found" % constant)
    return pattern.sub(lambda m: m.group(1) + json.dumps(value), source, count=1)


def build_playbook(base, name, substitutions, label=None):
    """(tgz bytes, sha256) of a playbook with its asset constants (and container label) substituted."""
    folder = os.path.join(base, "soar-playbooks")
    with open(os.path.join(folder, name + ".py"), encoding="utf-8") as handle:
        source = handle.read()
    with open(os.path.join(folder, name + ".json"), encoding="utf-8") as handle:
        meta = json.load(handle)
    coa = meta.get("coa") or {}
    wrong = [k for k, v in REQUIRED_COA.items() if coa.get(k) != v]
    if wrong:
        raise ValueError("coa metadata %s missing or changed (playbook would stay a draft)" % ", ".join(wrong))
    for constant, value in substitutions.items():
        source = _replace_constant(source, constant, value)
    if label is not None:
        meta["labels"] = [label]
    py_bytes = source.encode("utf-8")
    json_bytes = (json.dumps(meta, indent=4) + "\n").encode("utf-8")
    digest = hashlib.sha256(json_bytes + b"\0" + py_bytes).hexdigest()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _tar_add(tar, name + ".json", json_bytes)
        _tar_add(tar, name + ".py", py_bytes)
    return buf.getvalue(), digest


# ===================================================================== SOAR client
class SoarError(Exception):
    """Connection-level failure (no HTTP answer)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follows a 3xx: urllib would resend the ``ph-auth-token`` header to the redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError with the 3xx status


class SoarClient:
    """Minimal Splunk SOAR REST client. The token goes only into the ``ph-auth-token`` header.

    Redirects are never followed (the token must not reach another host); a 3xx is a connection error.
    """

    def __init__(self, base_url, token, verify_tls=True, timeout=30):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.verify_tls = verify_tls
        self.timeout = timeout

    def request(self, method, path, body=None, query=None):
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "MyDemoTicket/%s pairing" % __version__)
        req.add_header("ph-auth-token", self._token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        context = None
        if url.lower().startswith("https://"):
            context = ssl.create_default_context()
            if not self.verify_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
        handlers = [_NoRedirect()]
        if context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=context))
        opener = urllib.request.build_opener(*handlers)
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.headers is not None else None
            try:
                raw = exc.read()
            except Exception:  # pragma: no cover - defensive
                raw = b""
            finally:
                exc.close()
            status = exc.code
            if 300 <= status < 400:
                target = urllib.parse.urlsplit(urllib.parse.urljoin(url, location or "")).hostname or "?"
                raise SoarError("SOAR answered with a redirect (HTTP %d) to %s; use the final URL"
                                % (status, target))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise SoarError("connection error: %s" % getattr(exc, "reason", exc))
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            payload = {}
        return status, payload


def _filter(value):
    return json.dumps(value)  # SOAR filter values are JSON literals: _filter_name="x"


class _StepFailed(Exception):
    pass


class _Soar:
    """SOAR operations of the wizard on top of a client; raises _StepFailed with a readable reason."""

    def __init__(self, client, redact):
        self.client = client
        self.redact = redact

    def call(self, method, path, body=None, query=None, ok=(200, 201)):
        try:
            status, payload = self.client.request(method, path, body=body, query=query)
        except SoarError as exc:
            raise _StepFailed(str(exc))
        if status in (401, 403):
            raise _StepFailed("SOAR rejected the token (HTTP %d)" % status)
        if status not in ok or (isinstance(payload, dict) and payload.get("failed")):
            raise _StepFailed("%s %s: HTTP %d %s" % (method, path, status, _message(payload)))
        return payload if isinstance(payload, dict) else {"data": payload}

    def find(self, path, **filters):
        query = {"_filter_" + k: _filter(v) for k, v in filters.items()}
        query["page_size"] = 100
        payload = self.call("GET", path, query=query)
        return [row for row in payload.get("data") or [] if isinstance(row, dict)]

    def version(self):
        return str(self.call("GET", "/rest/version").get("version") or "unknown")


def _same_value(current, wanted):
    """Compare an asset config value read from SOAR; booleans may come back as "true"/"false" strings."""
    if isinstance(wanted, bool):
        if isinstance(current, str):
            current = {"true": True, "false": False}.get(current.strip().lower(), current)
        return current is wanted
    return current == wanted


def _message(payload):
    if isinstance(payload, dict):
        text = payload.get("message") or payload.get("error") or ""
    else:
        text = ""
    return str(text)[:MAX_DETAIL]


# ===================================================================== helpers
def _state_key(tenant_id):
    return "%s%d" % (STATE_PREFIX, tenant_id)


def load_state(db, tenant_id):
    raw = get_settings(db, _state_key(tenant_id)).get(_state_key(tenant_id))
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def save_state(db, tenant_id, state):
    put_setting(db, _state_key(tenant_id), json.dumps(state, sort_keys=True))


def integration_username(slug):
    return "soar-" + slug


def _norm_url(url):
    return url.strip().rstrip("/")


def _request_base_url(ctx):
    env = (os.environ.get(PUBLIC_URL_ENV) or "").strip()
    if env:
        return _norm_url(env)
    headers = ctx.headers
    host = (headers.get("X-Forwarded-Host") or headers.get("Host") or "").split(",")[0].strip() if headers else ""
    if not host or not re.match(r"^[A-Za-z0-9.:\[\]-]+$", host):
        return ""
    proto = (headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip().lower()
    return "%s://%s" % ("https" if proto == "https" else "http", host)


def _host(url):
    return urllib.parse.urlsplit(url).hostname or ""


class Redactor:
    """Removes every secret of the run from texts that leave the module (responses, audit)."""

    def __init__(self, *secrets):
        self.secrets = [s for s in secrets if s]

    def add(self, secret):
        if secret:
            self.secrets.append(secret)

    def __call__(self, text):
        text = str(text)
        for secret in self.secrets:
            text = text.replace(secret, "[redacted]")
        return text[:MAX_DETAIL]


# ===================================================================== API mixin
class PairingMixin:
    """Handlers for /pairing (mixed into api.Api; needs self.db, self._log, ctx.tenant)."""

    # injectable for tests: factory(base_url, token, verify_tls) -> object with request(method, path, body, query)
    soar_client_factory = None
    pairing_bundle_dir = None

    # ----------------------------------------------------------- plumbing
    def _soar(self, url, token, verify, redact):
        factory = self.soar_client_factory or SoarClient
        return _Soar(factory(url, token, verify), redact)

    @staticmethod
    def _pairing_error(status, message):
        from .api import ApiError  # late import: api.py imports this module
        return ApiError(status, message)

    def _pairing_input(self, ctx, allowed, apply=False):
        body = ctx.body
        if not isinstance(body, dict):
            raise self._pairing_error(400, "request body must be a JSON object")
        unknown = set(body) - allowed
        if unknown:
            raise self._pairing_error(422, "unknown field '%s'" % sorted(unknown)[0])
        state = load_state(self.db, ctx.tenant["id"])
        values = {}
        url = body.get("soar_url", state.get("soar_url"))
        if not isinstance(url, str) or not URL_RE.match(url.strip()):
            raise self._pairing_error(422, "'soar_url' must be an http(s) URL")
        values["soar_url"] = _norm_url(url)
        token = body.get("soar_token")
        if not isinstance(token, str) or not token.strip() or len(token) > 500:
            raise self._pairing_error(422, "'soar_token' (SOAR ph-auth-token) is required")
        values["soar_token"] = token.strip()
        for key in ("verify_tls", "ticket_verify_tls"):
            if key not in allowed:
                continue
            value = body.get(key, state.get(key, DEFAULTS[key]))
            if not isinstance(value, bool):
                raise self._pairing_error(422, "'%s' must be a boolean" % key)
            values[key] = value
        for key in ("label", "splunk_asset", "es_asset", "app_asset"):
            value = body.get(key)
            if value in (None, ""):
                value = state.get(key) or DEFAULTS[key]
            if not isinstance(value, str) or not NAME_RE.match(value):
                raise self._pairing_error(422, "'%s' must match [A-Za-z0-9_.-]{1,100}" % key)
            values[key] = value
        if apply:
            base = body.get("ticket_base_url")
            if base in (None, ""):
                base = state.get("ticket_base_url") or _request_base_url(ctx)
            if not isinstance(base, str) or not URL_RE.match(base.strip()):
                raise self._pairing_error(422, "'ticket_base_url' must be an http(s) URL")
            values["ticket_base_url"] = _norm_url(base)
            steps = body.get("steps")
            if steps is None:
                selected = list(STEPS)
            else:
                if not isinstance(steps, list) or not steps or not all(isinstance(s, str) for s in steps):
                    raise self._pairing_error(422, "'steps' must be a non-empty list of step names")
                wanted = set()
                for s in steps:
                    if s in STEP_ALIASES:
                        wanted.update(STEP_ALIASES[s])
                    elif s in STEPS:
                        wanted.add(s)
                    else:
                        raise self._pairing_error(422, "unknown step '%s' (steps: %s)" % (s, ", ".join(STEPS)))
                selected = [s for s in STEPS if s in wanted]
            values["steps"] = selected
        return values, state

    def _pairing_user(self, ctx):
        return self.db.query_one("SELECT * FROM users WHERE username = ?",
                                 (integration_username(ctx.tenant["slug"]),))

    def _pairing_webhook(self, ctx, soar_url):
        rows = self.db.query("SELECT * FROM webhooks WHERE tenant_id = ? AND type = 'soar' ORDER BY id",
                             (ctx.tenant["id"],))
        for row in rows:
            if _norm_url(row["url"]) == _norm_url(soar_url):
                return row
        return None

    # ----------------------------------------------------------- GET /pairing
    def pairing_status(self, ctx):
        require_permission(ctx.principal, "pairing.manage")
        state = load_state(self.db, ctx.tenant["id"])
        user = self._pairing_user(ctx)
        if user is not None and user["tenant_id"] != ctx.tenant["id"]:
            user = None
        hook = self._pairing_webhook(ctx, state["soar_url"]) if state.get("soar_url") else None
        base = bundle_dir(self.pairing_bundle_dir)
        bundle = {"available": base is not None, "app_version": None,
                  "playbooks": [name for name, _, _ in PLAYBOOKS]}
        if base is not None:
            try:
                bundle["app_version"] = app_manifest(base).get("app_version")
            except (OSError, ValueError):
                bundle["available"] = False
        token_active = False
        if user is not None and state.get("token_id"):
            token_active = self.db.query_one("SELECT 1 FROM api_tokens WHERE id = ? AND user_id = ?",
                                             (state["token_id"], user["id"])) is not None
        public = {k: state.get(k) for k in ("soar_url", "verify_tls", "label", "splunk_asset", "es_asset",
                                             "app_asset", "ticket_base_url", "ticket_verify_tls", "soar_version",
                                             "app_version", "asset_id")}
        return 200, {
            "tenant": ctx.tenant["slug"],
            "configured": bool(state.get("last_apply")),
            "settings": public,
            "integration_user": {"username": integration_username(ctx.tenant["slug"]),
                                 "exists": user is not None, "active": bool(user and user["active"]),
                                 "token_active": token_active},
            "webhook": ({"id": hook["id"], "active": bool(hook["active"]), "has_secret": bool(hook["secret"]),
                         "soar_label": hook["soar_label"]} if hook is not None else None),
            "playbooks": {name: {"id": info.get("id"), "active": info.get("active")}
                          for name, info in (state.get("playbooks") or {}).items()},
            "last_check": state.get("last_check"),
            "last_apply": state.get("last_apply"),
            "defaults": dict(DEFAULTS, ticket_base_url=state.get("ticket_base_url") or _request_base_url(ctx)),
            "steps": list(STEPS),
            "bundle": bundle,
        }

    # ----------------------------------------------------------- POST /pairing/check
    def pairing_check(self, ctx):
        require_permission(ctx.principal, "pairing.manage")
        values, state = self._pairing_input(ctx, CHECK_FIELDS)
        redact = Redactor(values["soar_token"])
        soar = self._soar(values["soar_url"], values["soar_token"], values["verify_tls"], redact)
        checks = []

        def add(name, status, detail):
            checks.append({"check": name, "status": status, "detail": redact(detail)})

        if urllib.parse.urlsplit(values["soar_url"]).scheme.lower() == "http":
            add("insecure_http", "warn", "SOAR URL uses http://: the token is sent unencrypted, use https://")
        version = None
        try:
            version = soar.version()
            soar.call("GET", "/rest/asset", query={"page_size": 1})  # proves the token may read assets
            add("connection", "ok", "SOAR %s reachable, token accepted" % version)
        except _StepFailed as exc:
            add("connection", "failed", str(exc))
        if version is not None:
            for key, what in (("splunk_asset", "Splunk app asset"), ("es_asset", "Enterprise Security asset")):
                try:
                    rows = soar.find("/rest/asset", name=values[key])
                    if rows:
                        add(key, "ok", "%s '%s' found (%s)" % (what, values[key], rows[0].get("product_name") or "?"))
                    else:
                        add(key, "failed", "%s '%s' not found in SOAR" % (what, values[key]))
                except _StepFailed as exc:
                    add(key, "failed", str(exc))
            try:
                base = bundle_dir(self.pairing_bundle_dir)
                manifest = app_manifest(base) if base else {}
                rows = soar.find("/rest/app", appid=manifest.get("appid", "")) if manifest else []
                if not manifest:
                    add("app", "failed", "app bundle not found on the ticket server (%s)" % BUNDLE_ENV)
                elif rows:
                    add("app", "ok", "MyDemoTicket app %s installed (bundle %s)"
                        % (rows[0].get("app_version"), manifest.get("app_version")))
                else:
                    add("app", "info", "MyDemoTicket app not installed yet (bundle %s)" % manifest.get("app_version"))
            except (_StepFailed, OSError, ValueError) as exc:
                add("app", "failed", str(exc))
            label_known = self._soar_label_exists(soar, values["label"])
            if label_known is None:
                add("label", "info", "label '%s': SOAR label list not readable, apply adds it if missing"
                    % values["label"])
            else:
                add("label", "ok" if label_known else "info",
                    "label '%s' %s" % (values["label"], "exists" if label_known else "will be created"))
        ok = all(c["status"] != "failed" for c in checks)
        result = {"ok": ok, "soar_version": version, "checks": checks}
        state["last_check"] = {"at": utcnow(), "by": ctx.principal.username, "ok": ok,
                               "soar_host": _host(values["soar_url"]),
                               "checks": {c["check"]: c["status"] for c in checks}}
        save_state(self.db, ctx.tenant["id"], state)
        self._log(ctx, "pairing.check", outcome="success" if ok else "failure", object_type="pairing",
                  object_id=ctx.tenant["slug"],
                  details={"soar_host": _host(values["soar_url"]), "verify_tls": values["verify_tls"],
                           "soar_version": version, "checks": {c["check"]: c["status"] for c in checks}})
        return 200, result

    @staticmethod
    def _soar_label_exists(soar, label):
        """True/False from the container label list (GET /rest/container_options), None if unknown.

        SOAR 8.x answers GET /rest/system_settings/events with 404, so labels are only read from
        container_options; when that fails the caller falls back to POST add_label.
        """
        try:
            payload = soar.call("GET", "/rest/container_options")
        except _StepFailed:
            return None
        value = payload.get("label")
        if not isinstance(value, list):
            return None
        names = [v.get("name") if isinstance(v, dict) else v for v in value]
        return label in names

    # ----------------------------------------------------------- POST /pairing/apply
    def pairing_apply(self, ctx):
        require_permission(ctx.principal, "pairing.manage")
        values, state = self._pairing_input(ctx, APPLY_FIELDS, apply=True)
        if not _APPLY_LOCK.acquire(timeout=120):
            raise self._pairing_error(409, "another pairing run is in progress")
        try:
            return self._pairing_run(ctx, values, state)
        finally:
            _APPLY_LOCK.release()

    def _pairing_run(self, ctx, values, state):
        redact = Redactor(values["soar_token"])
        soar = self._soar(values["soar_url"], values["soar_token"], values["verify_tls"], redact)
        results = []
        selected = values["steps"]

        def report(step, status, detail):
            results.append({"step": step, "status": status, "detail": redact(detail)})

        version = None
        try:
            version = soar.version()
        except _StepFailed as exc:
            # nothing is created (not even locally) when SOAR is not reachable with this token
            connection = {"status": "failed", "detail": redact("SOAR connection failed: %s" % exc)}
            for step in STEPS:
                report(step, "skipped", "SOAR connection failed" if step in selected else "not selected")
            return self._pairing_finish(ctx, values, state, results, version, connection)

        base = bundle_dir(self.pairing_bundle_dir)
        runners = {
            "user": lambda: self._step_user(ctx),
            "webhook": lambda: self._step_webhook(ctx, values),
            "label": lambda: self._step_label(soar, values),
            "app": lambda: self._step_app(soar, base, state),
            "asset": lambda: self._step_asset(ctx, soar, base, values, state, redact),
        }
        for name, constants, bind_label in PLAYBOOKS:
            step = "playbook_" + name[len("mydemoticket_"):]
            runners[step] = (lambda n=name, c=constants, b=bind_label:
                             self._step_playbook(soar, base, values, state, n, c, b))
        for step in STEPS:
            if step not in selected:
                report(step, "skipped", "not selected")
                continue
            try:
                status, detail = runners[step]()
            except _StepFailed as exc:
                status, detail = "failed", str(exc)
            except (OSError, ValueError, tarfile.TarError) as exc:
                status, detail = "failed", "%s: %s" % (type(exc).__name__, exc)
            report(step, status, detail)
        connection = {"status": "ok", "detail": "SOAR %s reachable" % version}
        return self._pairing_finish(ctx, values, state, results, version, connection)

    def _pairing_finish(self, ctx, values, state, results, version, connection):
        ok = connection["status"] == "ok" and all(r["status"] != "failed" for r in results)
        for key in ("soar_url", "verify_tls", "label", "splunk_asset", "es_asset", "app_asset",
                    "ticket_base_url", "ticket_verify_tls"):
            state[key] = values[key]
        if version is not None:
            state["soar_version"] = version
        state["last_apply"] = {"at": utcnow(), "by": ctx.principal.username, "ok": ok,
                               "connection": dict(connection), "steps": [dict(r) for r in results]}
        save_state(self.db, ctx.tenant["id"], state)
        self._log(ctx, "pairing.apply", outcome="success" if ok else "failure", object_type="pairing",
                  object_id=ctx.tenant["slug"],
                  details={"soar_host": _host(values["soar_url"]), "verify_tls": values["verify_tls"],
                           "soar_version": version, "label": values["label"],
                           "ticket_host": _host(values["ticket_base_url"]), "connection": connection["status"],
                           "steps": {r["step"]: r["status"] for r in results}})
        return 200, {"ok": ok, "soar_version": version, "connection": connection, "steps": results}

    # ----------------------------------------------------------- steps
    def _step_user(self, ctx):
        username = integration_username(ctx.tenant["slug"])
        row = self.db.query_one("SELECT * FROM users WHERE username = ?", (username,))
        if row is not None and row["tenant_id"] != ctx.tenant["id"]:
            raise _StepFailed("username '%s' is taken by another tenant" % username)
        if row is None:
            self.db.execute(
                "INSERT INTO users(username, display_name, email, role, password_hash, active, created_at, "
                "tenant_id) VALUES(?, ?, '', 'integration', NULL, 1, ?, ?)",
                (username, "Splunk SOAR (%s)" % ctx.tenant["slug"], utcnow(), ctx.tenant["id"]))
            self._log(ctx, "user.create", object_type="user", object_id=username,
                      details={"role": "integration", "tenant": ctx.tenant["slug"], "active": True, "teams": [],
                               "changed": ["role", "username"], "password_set": False, "via": "pairing"})
            return "created", "integration user %s (role integration, no password)" % username
        if row["role"] != "integration" or not row["active"]:
            self.db.execute("UPDATE users SET role = 'integration', active = 1 WHERE id = ?", (row["id"],))
            self._log(ctx, "user.update", object_type="user", object_id=username,
                      details={"changed": ["active", "role"], "via": "pairing"})
            return "updated", "integration user %s reactivated with role integration" % username
        return "exists", "integration user %s" % username

    def _step_webhook(self, ctx, values):
        row = self._pairing_webhook(ctx, values["soar_url"])
        token = values["soar_token"]
        if row is None:
            from .webhooks import EVENTS
            cur = self.db.execute(
                "INSERT INTO webhooks(name, type, url, secret, events, active, soar_label, verify_tls, created_at, "
                "tenant_id) VALUES(?, 'soar', ?, ?, ?, 1, ?, ?, ?, ?)",
                ("SOAR (pairing)", values["soar_url"], token, json.dumps(sorted(EVENTS)), values["label"],
                 int(values["verify_tls"]), utcnow(), ctx.tenant["id"]))
            self._log(ctx, "webhook.create", object_type="webhook", object_id=str(cur.lastrowid),
                      details={"type": "soar", "name": "SOAR (pairing)", "url_host": _host(values["soar_url"]),
                               "events": sorted(EVENTS), "active": True, "verify_tls": values["verify_tls"],
                               "via": "pairing"})
            return "created", "webhook %d (type soar, label %s)" % (cur.lastrowid, values["label"])
        changed = []
        if not hmac.compare_digest((row["secret"] or "").encode("utf-8"), token.encode("utf-8")):
            changed.append("secret")
        if row["soar_label"] != values["label"]:
            changed.append("soar_label")
        if bool(row["verify_tls"]) != values["verify_tls"]:
            changed.append("verify_tls")
        if not row["active"]:
            changed.append("active")
        if not changed:
            return "exists", "webhook %d (type soar, label %s)" % (row["id"], values["label"])
        self.db.execute("UPDATE webhooks SET secret = ?, soar_label = ?, verify_tls = ?, active = 1 WHERE id = ?",
                        (token, values["label"], int(values["verify_tls"]), row["id"]))
        self._log(ctx, "webhook.update", object_type="webhook", object_id=str(row["id"]),
                  details={"changed": changed, "url_host": _host(values["soar_url"]), "via": "pairing"})
        return "updated", "webhook %d: %s changed" % (row["id"], ", ".join(changed))

    def _step_label(self, soar, values):
        label = values["label"]
        known = self._soar_label_exists(soar, label)
        if known:
            return "exists", "label %s" % label
        try:
            payload = soar.call("POST", "/rest/system_settings/events",
                                body={"add_label": True, "label_name": label})
        except _StepFailed as exc:
            if "exist" in str(exc).lower():
                return "exists", "label %s" % label
            raise
        if "exist" in _message(payload).lower():
            return "exists", "label %s" % label
        return "created", "label %s" % label

    def _step_app(self, soar, base, state):
        if base is None:
            raise _StepFailed("app bundle not found on the ticket server (set %s)" % BUNDLE_ENV)
        manifest = app_manifest(base)
        rows = soar.find("/rest/app", appid=manifest["appid"])
        installed = rows[0].get("app_version") if rows else None
        if installed == manifest["app_version"]:
            state["app_version"] = installed
            return "exists", "MyDemoTicket app %s" % installed
        archive = build_app_tgz(base)
        payload = soar.call("POST", "/rest/app", body={"app": base64.b64encode(archive).decode("ascii")})
        if payload.get("success") is False:
            raise _StepFailed("app import failed: %s" % _message(payload))
        state["app_version"] = manifest["app_version"]
        if installed:
            return "updated", "MyDemoTicket app %s -> %s" % (installed, manifest["app_version"])
        return "created", "MyDemoTicket app %s installed" % manifest["app_version"]

    def _step_asset(self, ctx, soar, base, values, state, redact):
        if base is None:
            raise _StepFailed("app bundle not found on the ticket server (set %s)" % BUNDLE_ENV)
        manifest = app_manifest(base)
        user = self._pairing_user(ctx)
        if user is None or user["tenant_id"] != ctx.tenant["id"]:
            raise _StepFailed("integration user %s missing (run step user)" % integration_username(ctx.tenant["slug"]))
        name = values["app_asset"]
        rows = soar.find("/rest/asset", name=name)
        row = rows[0] if rows else None
        if row is not None and row.get("product_name") not in (None, manifest["product_name"]):
            raise _StepFailed("asset name '%s' is used by app '%s'" % (name, row.get("product_name")))
        config = {"base_url": values["ticket_base_url"], "verify_server_cert": values["ticket_verify_tls"],
                  "source_tag": "soar", "ingest_label": values["label"]}
        old_token_id = state.get("token_id")
        token_ok = bool(old_token_id) and self.db.query_one(
            "SELECT 1 FROM api_tokens WHERE id = ? AND user_id = ?", (old_token_id, user["id"])) is not None
        if row is not None:
            # only the four plain fields count: SOAR masks api_token and may return product_name as null
            current = row.get("configuration") or {}
            same = all(_same_value(current.get(k), v) for k, v in config.items())
            if same and token_ok and state.get("asset_id") == row.get("id"):
                return "exists", "asset %s (id %s) -> %s" % (name, row.get("id"), values["ticket_base_url"])
        token = new_token()
        redact.add(token)
        cur = self.db.execute("INSERT INTO api_tokens(user_id, name, token_hash, created_at) VALUES(?, ?, ?, ?)",
                              (user["id"], TOKEN_NAME, hash_token(token), utcnow()))
        token_id = cur.lastrowid
        body_config = dict(config, api_token=token)
        try:
            if row is None:
                payload = soar.call("POST", "/rest/asset", body={
                    "name": name, "product_vendor": manifest["product_vendor"],
                    "product_name": manifest["product_name"],
                    "description": "MyDemoTicket tenant %s (created by the pairing wizard)" % ctx.tenant["slug"],
                    "configuration": body_config})
                asset_id = payload.get("id")
                status = "created"
            else:
                soar.call("POST", "/rest/asset/%s" % row["id"], body={"configuration": body_config})
                asset_id = row["id"]
                status = "updated"
        except _StepFailed:
            self.db.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
            raise
        self._log(ctx, "token.create", object_type="token", object_id=str(token_id),
                  details={"name": TOKEN_NAME, "username": user["username"], "via": "pairing"})
        if old_token_id and old_token_id != token_id:
            gone = self.db.execute("DELETE FROM api_tokens WHERE id = ? AND user_id = ?", (old_token_id, user["id"]))
            if gone.rowcount:
                self._log(ctx, "token.revoke", object_type="token", object_id=str(old_token_id),
                          details={"name": TOKEN_NAME, "username": user["username"], "via": "pairing"})
        state["token_id"] = token_id
        state["asset_id"] = asset_id
        return status, "asset %s (id %s) -> %s, new integration token" % (name, asset_id, values["ticket_base_url"])

    def _step_playbook(self, soar, base, values, state, name, constants, bind_label):
        if base is None:
            raise _StepFailed("playbooks not found on the ticket server (set %s)" % BUNDLE_ENV)
        if bind_label:
            for key, what in (("splunk_asset", "Splunk app asset"), ("es_asset", "Enterprise Security asset")):
                if not soar.find("/rest/asset", name=values[key]):
                    raise _StepFailed("%s '%s' not found in SOAR - fix the name and run again" % (what, values[key]))
        archive, digest = build_playbook(base, name, {c: values[f] for c, f in constants.items()},
                                         label=values["label"] if bind_label else None)
        known = (state.get("playbooks") or {}).get(name) or {}
        rows = soar.find("/rest/playbook", name=name)
        row = max(rows, key=lambda r: r.get("id") or 0) if rows else None
        if row is not None and known.get("sha256") == digest:
            if row.get("active"):
                self._remember_playbook(state, name, row.get("id"), digest)
                return "exists", "playbook %s (id %s, active)" % (name, row.get("id"))
            self._activate(soar, row.get("id"))
            self._remember_playbook(state, name, row.get("id"), digest)
            return "updated", "playbook %s (id %s) activated" % (name, row.get("id"))
        payload = soar.call("POST", "/rest/import_playbook", body={
            "playbook": base64.b64encode(archive).decode("ascii"), "scm": "local", "force": True})
        if payload.get("success") is False:
            raise _StepFailed("playbook import failed: %s" % _message(payload))
        playbook_id = payload.get("id")
        if playbook_id is None:
            again = soar.find("/rest/playbook", name=name)
            playbook_id = max(r.get("id") or 0 for r in again) if again else None
        if playbook_id is None:
            raise _StepFailed("playbook %s imported but not found" % name)
        self._activate(soar, playbook_id)
        self._remember_playbook(state, name, playbook_id, digest)
        return ("updated" if row is not None else "created"), "playbook %s (id %s) imported and active" % (
            name, playbook_id)

    @staticmethod
    def _activate(soar, playbook_id):
        payload = soar.call("POST", "/rest/playbook/%s" % playbook_id, body={"active": True})
        if payload.get("success") is False:
            raise _StepFailed("activation of playbook %s failed: %s" % (playbook_id, _message(payload)))

    @staticmethod
    def _remember_playbook(state, name, playbook_id, digest):
        state.setdefault("playbooks", {})[name] = {"id": playbook_id, "sha256": digest, "active": True}


__all__ = ["PairingMixin", "SoarClient", "SoarError", "build_app_tgz", "build_playbook", "bundle_dir", "STEPS"]
