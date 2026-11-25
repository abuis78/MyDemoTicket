"""REST API handlers. Each handler returns (status, json_payload)."""

import datetime
import json
import re
import unicodedata
import urllib.parse

from . import __version__
from .auth import (
    ADMIN_ROLES,
    ROLES,
    AuthError,
    can_modify_ticket,
    can_read_ticket,
    create_session,
    delete_session,
    hash_password,
    hash_token,
    new_token,
    require_permission,
    require_role,
    set_session_tenant,
    user_teams,
    verify_password,
)
from .db import dumps, loads, parse_iso, utcnow
from .tenants import (
    DEFAULT_TENANT_SLUG,
    INDEX_RE,
    SEED_TENANT_TEAMS,
    SLUG_RE,
    TENANT_HEADER,
    default_tenant,
    seed_custom_fields,
    seed_teams,
    tenant_by_id,
    tenant_by_slug,
    tenant_dict,
    tenant_ref,
)
from .webhooks import EVENTS, TYPES

STATES = ("new", "in_progress", "on_hold", "resolved", "closed", "cancelled")
TRANSITIONS = {
    "new": ("in_progress", "on_hold", "resolved", "cancelled"),
    "in_progress": ("on_hold", "resolved", "cancelled"),
    "on_hold": ("in_progress", "resolved", "cancelled"),
    "resolved": ("in_progress", "closed"),
    "closed": (),
    "cancelled": (),
}
PRIORITIES = {1: "critical", 2: "high", 3: "moderate", 4: "low"}
OPEN_STATES = ("new", "in_progress", "on_hold")
CLOSING_STATES = ("resolved", "closed", "cancelled")  # transitions that need tickets.close
UNASSIGNED_TEAM = "(none)"
FIELD_TYPES = ("string", "number", "boolean", "choice", "date")
# journal entry types writable via POST /tickets/{number}/comments; summary = AI or resolution
# summary, visible like a comment, but only agents, admins and integrations may write it
COMMENT_TYPES = ("comment", "work_note", "summary")

TICKET_WRITABLE = (
    "short_description", "description", "state", "priority", "assignment_group",
    "assigned_to", "caller", "category", "custom_fields", "external_refs", "close_notes",
)
TICKET_READONLY = ("id", "number", "created_at", "updated_at", "resolved_at")
REQUESTER_CREATE_FIELDS = ("short_description", "description", "priority", "category", "custom_fields")

FIELD_NAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
EXTERNAL_REF_PREFIX = "external_ref."
MAX_AUTHOR = 64
MAX_TEXT = 100_000
MAX_LIMIT = 500

_DUMMY_HASH = hash_password("dummy-password-for-timing", iterations=1000)


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _forbidden(message="forbidden"):
    return ApiError(403, message)


def _require_obj(body):
    if not isinstance(body, dict):
        raise ApiError(400, "request body must be a JSON object")
    return body


def _text(value, field, required=False, max_len=MAX_TEXT):
    if value is None:
        if required:
            raise ApiError(422, "'%s' is required" % field)
        return ""
    if not isinstance(value, str):
        raise ApiError(422, "'%s' must be a string" % field)
    if required and not value.strip():
        raise ApiError(422, "'%s' must not be empty" % field)
    if len(value) > max_len:
        raise ApiError(422, "'%s' is too long" % field)
    return value


def _priority(value):
    if isinstance(value, bool):
        raise ApiError(422, "'priority' must be 1, 2, 3 or 4")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if value not in PRIORITIES:
        raise ApiError(422, "'priority' must be 1, 2, 3 or 4")
    return value


def _author(value):
    """Validate the optional comment author: string, trimmed 1..64 chars, no control chars."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(422, "'author' must be a string")
    value = value.strip()
    if not value or len(value) > MAX_AUTHOR:
        raise ApiError(422, "'author' must be 1..%d characters" % MAX_AUTHOR)
    if any(unicodedata.category(c) == "Cc" for c in value):
        raise ApiError(422, "'author' must not contain control characters")
    return value


# Stored external_refs scalar rendered as text: strings as-is, numbers in their
# SQLite text form (1133 -> '1133'), booleans as JSON ('true'/'false').
_REF_TEXT_SQL = (
    "(CASE json_type(external_refs, ?) WHEN 'true' THEN 'true' WHEN 'false' THEN 'false' "
    "WHEN 'null' THEN NULL WHEN 'object' THEN NULL WHEN 'array' THEN NULL "
    "ELSE CAST(json_extract(external_refs, ?) AS TEXT) END)"
)


def _external_ref_filters(query):
    """Translate ``external_ref.<key>=<value>`` query parameters into SQL (AND)."""
    where, params = [], []
    for name in sorted(query):
        if not name.startswith(EXTERNAL_REF_PREFIX):
            continue
        key = name[len(EXTERNAL_REF_PREFIX):]
        if not FIELD_NAME_RE.match(key):
            raise ApiError(422, "invalid external_ref filter key (allowed: [a-z0-9_]{1,64})")
        path = '$."%s"' % key
        where.append(_REF_TEXT_SQL + " = ?")
        params.extend([path, path, query[name]])
    return where, params


def _bool(value, field):
    if not isinstance(value, bool):
        raise ApiError(422, "'%s' must be a boolean" % field)
    return value


class NullAudit:
    """Audit sink used when no logger is wired (e.g. Api used directly)."""

    def log(self, action, **kwargs):
        pass

    def stats(self):
        return {}


def _url_host(url):
    return urllib.parse.urlsplit(url).hostname or ""


class Api:
    def __init__(self, db, dispatcher, audit=None):
        self.db = db
        self.dispatcher = dispatcher
        self.audit = audit or NullAudit()

    # =====================================================================
    # audit + tenant helpers
    # =====================================================================
    def _log(self, ctx, action, outcome="success", object_type=None, object_id=None, details=None,
             tenant=None, actor=None, role=None):
        p = ctx.principal
        if tenant is None:
            tenant = ctx.tenant["slug"] if ctx.tenant is not None else "unknown"
        self.audit.log(
            action, tenant=tenant,
            actor=actor if actor is not None else (p.username if p else None),
            role=role if role is not None else (p.role if p else None),
            outcome=outcome, object_type=object_type, object_id=object_id, source=ctx.source,
            ip=ctx.ip, request_id=ctx.request_id, details=details,
        )

    @staticmethod
    def _tid(ctx):
        return ctx.tenant["id"]

    def _require_admin(self, ctx):
        require_role(ctx.principal, *ADMIN_ROLES)

    @staticmethod
    def _require_write(principal, ticket):
        """Field changes, comments and work notes of non-self-service users."""
        if not (principal.has("tickets.update_all") or principal.has("tickets.update_own")):
            raise _forbidden("forbidden for role '%s' (needs tickets.update_own)" % principal.role)
        if not can_modify_ticket(principal, ticket):
            raise _forbidden("ticket is assigned to another team or user")

    # =====================================================================
    # misc
    # =====================================================================
    def health(self, ctx):
        return 200, {"status": "ok", "version": __version__}

    def me(self, ctx):
        p = ctx.principal
        row = self.db.query_one("SELECT * FROM users WHERE id = ?", (p.id,))
        data = self._user_dict(row)
        data["tenant"] = tenant_ref(ctx.tenant)
        if p.is_platform_admin:
            data["active_tenant"] = tenant_ref(ctx.tenant)
            data["tenants"] = [tenant_dict(r) for r in self.db.query("SELECT * FROM tenants ORDER BY slug")]
        if p.via == "session":
            data["csrf_token"] = p.csrf_token
        return 200, data

    def login(self, ctx):
        """Returns (session_token, csrf_token, user_dict) or raises 401/403."""
        body = ctx.body if isinstance(ctx.body, dict) else {}
        username, password = body.get("username"), body.get("password")
        row = None
        if isinstance(username, str) and isinstance(password, str):
            row = self.db.query_one("SELECT * FROM users WHERE username = ?", (username,))
        actor = username[:64] if isinstance(username, str) else None
        user_tenant = None
        if row is not None:
            user_tenant = tenant_by_id(self.db, row["tenant_id"]) if row["tenant_id"] is not None \
                else default_tenant(self.db)

        def fail(reason):
            self._log(ctx, "auth.login", outcome="failure", object_type="user", object_id=actor,
                      details={"method": "password", "reason": reason},
                      tenant=user_tenant["slug"] if user_tenant is not None else "unknown",
                      actor=actor, role=row["role"] if row is not None else None)

        if row is None or not row["active"] or not row["password_hash"]:
            verify_password(password if isinstance(password, str) else "", _DUMMY_HASH)
            fail("unknown user" if row is None else "user inactive or without password")
            raise ApiError(401, "invalid username or password")
        if not verify_password(password, row["password_hash"]):
            fail("wrong password")
            raise ApiError(401, "invalid username or password")
        if row["role"] != "platform_admin" and (user_tenant is None or not user_tenant["active"]):
            fail("tenant inactive")
            raise AuthError(403, "tenant inactive")
        session, csrf = create_session(self.db, row["id"])
        ctx.tenant = user_tenant
        data = self._user_dict(row)
        data["tenant"] = tenant_ref(user_tenant)
        data["csrf_token"] = csrf
        self._log(ctx, "auth.login", object_type="user", object_id=row["username"],
                  details={"method": "password"}, actor=row["username"], role=row["role"])
        return session, csrf, data

    def logout(self, ctx, session):
        delete_session(self.db, session)
        self._log(ctx, "auth.logout", object_type="user", object_id=ctx.principal.username)
        return 200, {"status": "logged_out"}

    def switch_tenant(self, ctx, session):
        """POST /auth/tenant: a platform_admin stores the active tenant in the session."""
        p = ctx.principal
        require_role(p, "platform_admin")
        if p.via != "session" or not session:
            raise ApiError(400, "tenant switch needs a web session; API clients send the %s header"
                           % TENANT_HEADER)
        body = _require_obj(ctx.body)
        unknown = set(body) - {"tenant"}
        if unknown:
            raise ApiError(422, "unknown field '%s'" % sorted(unknown)[0])
        target = tenant_by_slug(self.db, body.get("tenant"))
        if target is None:
            raise ApiError(404, "tenant not found")
        before = ctx.tenant["slug"] if ctx.tenant is not None else None
        set_session_tenant(self.db, session, target["id"])
        p.active_tenant_id = target["id"]
        ctx.tenant = target
        self._log(ctx, "auth.tenant_switch", object_type="tenant", object_id=target["slug"],
                  details={"from": before, "to": target["slug"]})
        return self.me(ctx)

    def audit_status(self, ctx):
        self._require_admin(ctx)
        return 200, self.audit.stats()

    # =====================================================================
    # tenants (platform_admin)
    # =====================================================================
    def _load_tenant(self, slug):
        row = tenant_by_slug(self.db, slug)
        if row is None:
            raise ApiError(404, "tenant not found")
        return row

    @staticmethod
    def _tenant_optional_fields(body, current=None):
        values = {}
        if "soar_url" in body:
            url = body["soar_url"]
            if url in (None, ""):
                values["soar_url"] = None
            else:
                url = _text(url, "soar_url", max_len=2000).strip()
                if not re.match(r"^https?://[^\s/]+", url, re.IGNORECASE):
                    raise ApiError(422, "'soar_url' must be an http(s) URL or null")
                values["soar_url"] = url
        if "splunk_index" in body:
            index = body["splunk_index"]
            if index in (None, ""):
                values["splunk_index"] = None
            elif not isinstance(index, str) or not INDEX_RE.match(index):
                raise ApiError(422, "'splunk_index' must match [a-z0-9][a-z0-9_-]{0,79} or be null")
            else:
                values["splunk_index"] = index
        return values

    def list_tenants(self, ctx):
        require_permission(ctx.principal, "tenants.manage")
        return 200, [tenant_dict(r) for r in self.db.query("SELECT * FROM tenants ORDER BY slug")]

    def get_tenant(self, ctx, slug):
        require_permission(ctx.principal, "tenants.manage")
        return 200, tenant_dict(self._load_tenant(slug))

    def create_tenant(self, ctx):
        require_permission(ctx.principal, "tenants.manage")
        body = _require_obj(ctx.body)
        unknown = set(body) - {"slug", "name", "active", "soar_url", "splunk_index", "seed"}
        if unknown:
            raise ApiError(422, "unknown field '%s'" % sorted(unknown)[0])
        slug = body.get("slug")
        if not isinstance(slug, str) or not SLUG_RE.match(slug):
            raise ApiError(422, "'slug' must match [a-z0-9-]{2,32}")
        name = _text(body.get("name"), "name", required=True, max_len=200).strip()
        active = _bool(body.get("active", True), "active")
        seed = _bool(body.get("seed", False), "seed")
        optional = self._tenant_optional_fields(body)
        if tenant_by_slug(self.db, slug) is not None:
            raise ApiError(409, "tenant '%s' already exists" % slug)
        now = utcnow()
        with self.db.transaction():
            cur = self.db.execute(
                "INSERT INTO tenants(slug, name, active, soar_url, splunk_index, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (slug, name, int(active), optional.get("soar_url"), optional.get("splunk_index"), now, now),
            )
            if seed:
                seed_teams(self.db, cur.lastrowid, SEED_TENANT_TEAMS)
                seed_custom_fields(self.db, cur.lastrowid)
        row = self._load_tenant(slug)
        self._log(ctx, "tenant.create", object_type="tenant", object_id=slug, tenant=slug,
                  details={"name": name, "active": active, "seed": seed,
                           "splunk_index": row["splunk_index"], "soar_url": row["soar_url"]})
        return 201, tenant_dict(row)

    def patch_tenant(self, ctx, slug):
        require_permission(ctx.principal, "tenants.manage")
        body = _require_obj(ctx.body)
        row = self._load_tenant(slug)
        for key in body:
            if key in ("slug", "created_at", "updated_at", "default"):
                raise ApiError(422, "'%s' is read-only" % key)
            if key not in ("name", "active", "soar_url", "splunk_index"):
                raise ApiError(422, "unknown field '%s'" % key)
        new = {"name": row["name"], "active": bool(row["active"]), "soar_url": row["soar_url"],
               "splunk_index": row["splunk_index"]}
        if "name" in body:
            new["name"] = _text(body["name"], "name", required=True, max_len=200).strip()
        if "active" in body:
            new["active"] = _bool(body["active"], "active")
        new.update(self._tenant_optional_fields(body))
        if slug == DEFAULT_TENANT_SLUG and not new["active"]:
            raise ApiError(409, "the default tenant cannot be deactivated")
        old = {"name": row["name"], "active": bool(row["active"]), "soar_url": row["soar_url"],
               "splunk_index": row["splunk_index"]}
        changes = {k: {"old": old[k], "new": new[k]} for k in new if old[k] != new[k]}
        if changes:
            self.db.execute(
                "UPDATE tenants SET name = ?, active = ?, soar_url = ?, splunk_index = ?, updated_at = ? "
                "WHERE id = ?",
                (new["name"], int(new["active"]), new["soar_url"], new["splunk_index"], utcnow(), row["id"]),
            )
            self._log(ctx, "tenant.update", object_type="tenant", object_id=slug, tenant=slug,
                      details={"changed": sorted(changes), "changes": changes})
        return 200, tenant_dict(self._load_tenant(slug))

    # =====================================================================
    # reports
    # =====================================================================
    def reports_summary(self, ctx):
        require_permission(ctx.principal, "reports.view")
        tid = self._tid(ctx)
        where, params = "tenant_id = ?", [tid]
        since_raw = ctx.query.get("since")
        if since_raw:
            since = parse_iso(since_raw)
            if since is None:
                raise ApiError(422, "'since' must be an ISO-8601 timestamp")
            where += " AND created_at >= ?"
            params.append(since)
        rows = self.db.query(
            "SELECT state, priority, assignment_group, created_at, resolved_at FROM tickets WHERE " + where,
            params,
        )
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        week_ago = (now - datetime.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        by_state = {s: 0 for s in STATES}
        by_priority = {str(p): 0 for p in PRIORITIES}
        by_team = {r["name"]: 0 for r in self.db.query("SELECT name FROM teams WHERE tenant_id = ?", (tid,))}
        by_team[UNASSIGNED_TEAM] = 0
        created_7d = resolved_7d = 0
        durations = []
        for r in rows:
            by_state[r["state"]] = by_state.get(r["state"], 0) + 1
            by_priority[str(r["priority"])] = by_priority.get(str(r["priority"]), 0) + 1
            team = r["assignment_group"] or UNASSIGNED_TEAM
            by_team[team] = by_team.get(team, 0) + 1
            if r["created_at"] >= week_ago:
                created_7d += 1
            if r["resolved_at"]:
                if r["resolved_at"] >= week_ago:
                    resolved_7d += 1
                durations.append(_seconds_between(r["created_at"], r["resolved_at"]))
        mttr = round(sum(durations) / len(durations) / 3600.0, 2) if durations else None
        return 200, {
            "tenant": ctx.tenant["slug"],
            "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tickets_total": len(rows),
            "open_count": sum(by_state[s] for s in OPEN_STATES),
            "by_state": by_state,
            "by_priority": by_priority,
            "by_team": by_team,
            "created_last_7d": created_7d,
            "resolved_last_7d": resolved_7d,
            "mttr_hours": mttr,
        }

    # =====================================================================
    # tickets
    # =====================================================================
    def _ticket_dict(self, row):
        return {
            "id": row["id"],
            "number": row["number"],
            "short_description": row["short_description"],
            "description": row["description"],
            "state": row["state"],
            "priority": row["priority"],
            "assignment_group": row["assignment_group"],
            "assigned_to": row["assigned_to"],
            "caller": row["caller"],
            "category": row["category"],
            "custom_fields": loads(row["custom_fields"], {}),
            "external_refs": loads(row["external_refs"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "resolved_at": row["resolved_at"],
            "close_notes": row["close_notes"],
        }

    def _load_ticket(self, number, tenant_id):
        """Load a ticket of the given tenant; tickets of other tenants are 404."""
        if isinstance(number, str) and number.isdigit():
            row = self.db.query_one("SELECT * FROM tickets WHERE id = ? AND tenant_id = ?",
                                    (int(number), tenant_id))
        else:
            row = self.db.query_one("SELECT * FROM tickets WHERE number = ? AND tenant_id = ?",
                                    (number, tenant_id))
        if row is None:
            raise ApiError(404, "ticket not found")
        return row

    def _readable_ticket(self, ctx, number):
        row = self._load_ticket(number, self._tid(ctx))
        if not can_read_ticket(ctx.principal, row):
            raise _forbidden("you may not access this ticket")
        return row

    def _field_defs(self, tenant_id):
        rows = self.db.query("SELECT * FROM custom_field_defs WHERE tenant_id = ? ORDER BY name", (tenant_id,))
        return {r["name"]: self._field_def_dict(r) for r in rows}

    def _validate_custom_values(self, tenant_id, values, existing=None, creating=False):
        """Validate custom field values against the tenant's definitions; return the merged dict."""
        if not isinstance(values, dict):
            raise ApiError(422, "'custom_fields' must be an object")
        defs = self._field_defs(tenant_id)
        merged = dict(existing or {})
        for name, value in values.items():
            definition = defs.get(name)
            if definition is None:
                raise ApiError(422, "unknown custom field '%s'" % name)
            if value is None:
                if definition["required"]:
                    raise ApiError(422, "custom field '%s' is required" % name)
                merged.pop(name, None)
                continue
            merged[name] = self._coerce_custom(definition, value)
        if creating:
            for name, definition in defs.items():
                if definition["required"] and merged.get(name) is None:
                    raise ApiError(422, "custom field '%s' is required" % name)
        return merged

    @staticmethod
    def _coerce_custom(definition, value):
        name, ftype = definition["name"], definition["type"]
        if ftype == "string":
            if not isinstance(value, str):
                raise ApiError(422, "custom field '%s' must be a string" % name)
            if len(value) > MAX_TEXT:
                raise ApiError(422, "custom field '%s' is too long" % name)
            return value
        if ftype == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ApiError(422, "custom field '%s' must be a number" % name)
            return value
        if ftype == "boolean":
            if not isinstance(value, bool):
                raise ApiError(422, "custom field '%s' must be a boolean" % name)
            return value
        if ftype == "choice":
            if value not in definition["choices"]:
                raise ApiError(
                    422, "custom field '%s' must be one of: %s" % (name, ", ".join(definition["choices"]))
                )
            return value
        if ftype == "date":
            try:
                if not isinstance(value, str):
                    raise ValueError
                datetime.date.fromisoformat(value)
            except ValueError:
                raise ApiError(422, "custom field '%s' must be a date (YYYY-MM-DD)" % name)
            return value
        raise ApiError(422, "custom field '%s' has unsupported type" % name)  # pragma: no cover

    def _validate_refs(self, values, existing=None):
        if not isinstance(values, dict):
            raise ApiError(422, "'external_refs' must be an object")
        merged = dict(existing or {})
        for key, value in values.items():
            if not isinstance(key, str) or not key or len(key) > 64:
                raise ApiError(422, "invalid external_refs key")
            if value is None:
                merged.pop(key, None)
            elif isinstance(value, (str, int, float, bool)):
                merged[key] = value
            else:
                raise ApiError(422, "external_refs values must be scalar")
        return merged

    def _check_group(self, value, tenant_id):
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ApiError(422, "'assignment_group' must be a team name or null")
        if self.db.query_one("SELECT 1 FROM teams WHERE name = ? AND tenant_id = ?", (value, tenant_id)) is None:
            raise ApiError(422, "unknown assignment_group '%s'" % value)
        return value

    def _check_user(self, value, field, tenant_id, allow_null=True):
        if value is None or value == "":
            if allow_null:
                return None
            raise ApiError(422, "'%s' is required" % field)
        if not isinstance(value, str):
            raise ApiError(422, "'%s' must be a username" % field)
        if self.db.query_one("SELECT 1 FROM users WHERE username = ? AND tenant_id = ?",
                             (value, tenant_id)) is None:
            raise ApiError(422, "unknown user '%s' for '%s'" % (value, field))
        return value

    def list_tickets(self, ctx):
        p, q = ctx.principal, ctx.query
        where, params = ["tenant_id = ?"], [self._tid(ctx)]
        if p.self_service:
            where.append("caller = ?")
            params.append(p.username)
        if q.get("state"):
            states = [s.strip() for s in q["state"].split(",") if s.strip()]
            for s in states:
                if s not in STATES:
                    raise ApiError(422, "unknown state '%s'" % s)
            where.append("state IN (%s)" % ",".join("?" * len(states)))
            params.extend(states)
        for field in ("assignment_group", "assigned_to", "caller"):
            if field in q:
                if q[field] in ("", "null"):
                    where.append("%s IS NULL" % field)
                else:
                    where.append("%s = ?" % field)
                    params.append(q[field])
        if q.get("priority"):
            where.append("priority = ?")
            params.append(_priority(q["priority"]))
        if q.get("updated_since"):
            since = parse_iso(q["updated_since"])
            if since is None:
                raise ApiError(422, "'updated_since' must be an ISO-8601 timestamp")
            where.append("updated_at >= ?")
            params.append(since)
        if q.get("q"):
            like = "%" + q["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append(
                "(number LIKE ? ESCAPE '\\' OR short_description LIKE ? ESCAPE '\\' "
                "OR description LIKE ? ESCAPE '\\')"
            )
            params.extend([like, like, like])
        ref_where, ref_params = _external_ref_filters(q)
        where.extend(ref_where)
        params.extend(ref_params)
        try:
            limit = int(q.get("limit", 50))
            offset = int(q.get("offset", 0))
        except ValueError:
            raise ApiError(422, "'limit' and 'offset' must be integers")
        if limit < 1 or limit > MAX_LIMIT or offset < 0:
            raise ApiError(422, "'limit' must be 1..%d and 'offset' >= 0" % MAX_LIMIT)
        sort = q.get("sort", "-updated_at")
        sort_map = {
            "updated_at": "updated_at ASC, id ASC", "-updated_at": "updated_at DESC, id DESC",
            "number": "id ASC", "-number": "id DESC",
            "priority": "priority ASC, id DESC", "-priority": "priority DESC, id DESC",
        }
        if sort not in sort_map:
            raise ApiError(422, "unsupported sort '%s'" % sort)
        clause = " WHERE " + " AND ".join(where)
        total = self.db.query_one("SELECT COUNT(*) AS n FROM tickets" + clause, params)["n"]
        rows = self.db.query(
            "SELECT * FROM tickets%s ORDER BY %s LIMIT ? OFFSET ?" % (clause, sort_map[sort]),
            params + [limit, offset],
        )
        return 200, {"items": [self._ticket_dict(r) for r in rows], "total": total}

    def get_ticket(self, ctx, number):
        return 200, self._ticket_dict(self._readable_ticket(ctx, number))

    def create_ticket(self, ctx):
        p, body = ctx.principal, _require_obj(ctx.body)
        tid = self._tid(ctx)
        require_permission(p, "tickets.create")
        for key in body:
            if key in TICKET_READONLY:
                raise ApiError(422, "'%s' is read-only" % key)
            if key not in TICKET_WRITABLE:
                raise ApiError(422, "unknown field '%s'" % key)
        if p.self_service:
            extra = [k for k in body if k not in REQUESTER_CREATE_FIELDS]
            if extra:
                raise _forbidden("requesters may not set: %s" % ", ".join(sorted(extra)))
        state = body.get("state", "new")
        if state != "new":
            raise ApiError(422, "new tickets must start in state 'new'")
        if body.get("close_notes"):
            raise ApiError(422, "'close_notes' cannot be set on create")
        short = _text(body.get("short_description"), "short_description", required=True, max_len=500)
        description = _text(body.get("description"), "description")
        priority = _priority(body.get("priority", 3))
        category = _text(body.get("category"), "category", max_len=100)
        group = self._check_group(body.get("assignment_group"), tid)
        assignee = self._check_user(body.get("assigned_to"), "assigned_to", tid)
        caller = p.username if p.self_service else (
            self._check_user(body.get("caller"), "caller", tid) or p.username
        )
        custom = self._validate_custom_values(tid, body.get("custom_fields") or {}, creating=True)
        refs = self._validate_refs(body.get("external_refs") or {})
        now = utcnow()
        with self.db.transaction():
            number = self.db.next_ticket_number()
            cur = self.db.execute(
                "INSERT INTO tickets(number, short_description, description, state, priority, "
                "assignment_group, assigned_to, caller, category, custom_fields, external_refs, "
                "created_at, updated_at, resolved_at, close_notes, tenant_id) "
                "VALUES(?, ?, ?, 'new', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '', ?)",
                (number, short, description, priority, group, assignee, caller, category,
                 dumps(custom), dumps(refs), now, now, tid),
            )
            self._audit(cur.lastrowid, tid, "created", None, number, p.username, ctx.source, now)
        ticket = self._ticket_dict(self._load_ticket(number, tid))
        initial = {k: {"old": None, "new": ticket[k]} for k in (
            "short_description", "description", "state", "priority", "assignment_group", "assigned_to",
            "caller", "category", "custom_fields", "external_refs") if ticket[k] not in (None, "", {})}
        self._log(ctx, "ticket.create", object_type="ticket", object_id=number,
                  details={"short_description": short, "priority": priority, "changes": initial})
        self.dispatcher.emit("ticket.created", ticket, p.username, ctx.source, tenant=ctx.tenant,
                             request_id=ctx.request_id)
        return 201, ticket

    def _audit(self, ticket_id, tenant_id, field, old, new, actor, source, at):
        """Journal audit entry (ticket history), not the security audit log."""
        self.db.execute(
            "INSERT INTO journal(ticket_id, tenant_id, type, body, field, old_value, new_value, actor, source, at) "
            "VALUES(?, ?, 'audit', NULL, ?, ?, ?, ?, ?, ?)",
            (ticket_id, tenant_id, field, json.dumps(old), json.dumps(new), actor, source, at),
        )

    def patch_ticket(self, ctx, number):
        p, body = ctx.principal, _require_obj(ctx.body)
        tid = self._tid(ctx)
        row = self._readable_ticket(ctx, number)
        for key in body:
            if key in TICKET_READONLY:
                raise ApiError(422, "'%s' is read-only" % key)
            if key not in TICKET_WRITABLE:
                raise ApiError(422, "unknown field '%s'" % key)
        if p.self_service:
            if set(body) - {"state"} or body.get("state") != "cancelled":
                raise _forbidden("requesters may only cancel their own tickets")
            require_permission(p, "tickets.create")
        else:
            self._require_write(p, row)

        current = self._ticket_dict(row)
        new = dict(current)
        if "short_description" in body:
            new["short_description"] = _text(body["short_description"], "short_description", True, 500)
        if "description" in body:
            new["description"] = _text(body["description"], "description")
        if "priority" in body:
            new["priority"] = _priority(body["priority"])
        if "category" in body:
            new["category"] = _text(body["category"], "category", max_len=100)
        if "assignment_group" in body:
            new["assignment_group"] = self._check_group(body["assignment_group"], tid)
        if "assigned_to" in body:
            new["assigned_to"] = self._check_user(body["assigned_to"], "assigned_to", tid)
        if "caller" in body:
            new["caller"] = self._check_user(body["caller"], "caller", tid, allow_null=False)
        if "close_notes" in body:
            new["close_notes"] = _text(body["close_notes"], "close_notes")
        if "custom_fields" in body:
            new["custom_fields"] = self._validate_custom_values(tid, body["custom_fields"],
                                                                current["custom_fields"])
        if "external_refs" in body:
            new["external_refs"] = self._validate_refs(body["external_refs"], current["external_refs"])
        now = utcnow()
        if "state" in body:
            target = body["state"]
            if target not in STATES:
                raise ApiError(422, "unknown state '%s'" % target)
            if target != current["state"]:
                if target in CLOSING_STATES and not p.self_service:
                    require_permission(p, "tickets.close")
                if target not in TRANSITIONS[current["state"]]:
                    raise ApiError(
                        409, "transition %s -> %s is not allowed" % (current["state"], target)
                    )
                if target == "resolved" and not (new["close_notes"] or "").strip():
                    raise ApiError(422, "'close_notes' are required to resolve a ticket")
                new["state"] = target
                if target == "resolved":
                    new["resolved_at"] = now
                elif target == "in_progress":
                    new["resolved_at"] = None

        changes = self._diff(current, new)
        if not changes:
            return 200, current
        with self.db.transaction():
            self.db.execute(
                "UPDATE tickets SET short_description = ?, description = ?, state = ?, priority = ?, "
                "assignment_group = ?, assigned_to = ?, caller = ?, category = ?, custom_fields = ?, "
                "external_refs = ?, resolved_at = ?, close_notes = ?, updated_at = ? WHERE id = ?",
                (new["short_description"], new["description"], new["state"], new["priority"],
                 new["assignment_group"], new["assigned_to"], new["caller"], new["category"],
                 dumps(new["custom_fields"]), dumps(new["external_refs"]), new["resolved_at"],
                 new["close_notes"], now, row["id"]),
            )
            for field, change in changes.items():
                self._audit(row["id"], tid, field, change["old"], change["new"], p.username, ctx.source, now)
        ticket = self._ticket_dict(self._load_ticket(row["number"], tid))
        if "state" in changes:
            event = "ticket.state_changed"
            self._log(ctx, "ticket.state_change", object_type="ticket", object_id=row["number"],
                      details={"from": changes["state"]["old"], "to": changes["state"]["new"],
                               "changed": sorted(changes), "changes": changes})
        else:
            event = "ticket.updated"
            self._log(ctx, "ticket.update", object_type="ticket", object_id=row["number"],
                      details={"changed": sorted(changes), "changes": changes})
        self.dispatcher.emit(event, ticket, p.username, ctx.source, changes=changes, tenant=ctx.tenant,
                             request_id=ctx.request_id)
        return 200, ticket

    @staticmethod
    def _diff(old, new):
        changes = {}
        for field in ("short_description", "description", "state", "priority", "assignment_group",
                      "assigned_to", "caller", "category", "close_notes", "resolved_at"):
            if old[field] != new[field]:
                changes[field] = {"old": old[field], "new": new[field]}
        for field in ("custom_fields", "external_refs"):
            before, after = old[field], new[field]
            for key in sorted(set(before) | set(after)):
                if before.get(key) != after.get(key):
                    changes["%s.%s" % (field, key)] = {"old": before.get(key), "new": after.get(key)}
        return changes

    def store_soar_container_id(self, number, container_id):
        """Persist the SOAR container id without emitting a webhook event.

        Ticket numbers are globally unique, so no tenant is needed here.
        """
        with self.db.transaction():
            row = self.db.query_one("SELECT * FROM tickets WHERE number = ?", (number,))
            if row is None:
                return
            refs = loads(row["external_refs"], {})
            if refs.get("soar_container_id") == container_id:
                return
            old = refs.get("soar_container_id")
            refs["soar_container_id"] = container_id
            now = utcnow()
            self.db.execute(
                "UPDATE tickets SET external_refs = ?, updated_at = ? WHERE id = ?",
                (dumps(refs), now, row["id"]),
            )
            self._audit(row["id"], row["tenant_id"], "external_refs.soar_container_id", old, container_id,
                        "system", "webhook", now)

    # -- journal ------------------------------------------------------------
    @staticmethod
    def _journal_dict(row):
        entry = {
            "id": row["id"],
            "type": row["type"],
            "actor": row["actor"],
            "author": row["author"],
            "source": row["source"],
            "at": row["at"],
        }
        if row["type"] == "audit":
            entry["field"] = row["field"]
            entry["old"] = loads(row["old_value"])
            entry["new"] = loads(row["new_value"])
        else:
            entry["body"] = row["body"]
        return entry

    def journal(self, ctx, number):
        row = self._readable_ticket(ctx, number)
        sql = "SELECT * FROM journal WHERE ticket_id = ? AND tenant_id = ?"
        if ctx.principal.self_service:
            sql += " AND type != 'work_note'"
        rows = self.db.query(sql + " ORDER BY id ASC", (row["id"], self._tid(ctx)))
        return 200, [self._journal_dict(r) for r in rows]

    def add_comment(self, ctx, number):
        p, body = ctx.principal, _require_obj(ctx.body)
        tid = self._tid(ctx)
        row = self._readable_ticket(ctx, number)
        kind = body.get("type", "comment")
        if kind not in COMMENT_TYPES:
            raise ApiError(422, "'type' must be 'comment', 'work_note' or 'summary'")
        unknown = set(body) - {"body", "type", "author"}
        if unknown:
            raise ApiError(422, "unknown field '%s'" % sorted(unknown)[0])
        if p.self_service:
            if kind != "comment":
                raise _forbidden("requesters may only add comments")
            require_permission(p, "tickets.comment")
        else:
            require_permission(p, "tickets.comment" if kind == "comment" else "tickets.work_note")
            self._require_write(p, row)
        text = _text(body.get("body"), "body", required=True)
        author = _author(body.get("author"))
        now = utcnow()
        with self.db.transaction():
            cur = self.db.execute(
                "INSERT INTO journal(ticket_id, tenant_id, type, body, actor, author, source, at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (row["id"], tid, kind, text, p.username, author, ctx.source, now),
            )
            self.db.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now, row["id"]))
            entry = self._journal_dict(
                self.db.query_one("SELECT * FROM journal WHERE id = ?", (cur.lastrowid,))
            )
        ticket = self._ticket_dict(self._load_ticket(row["number"], tid))
        self._log(ctx, "ticket.comment", object_type="ticket", object_id=row["number"],
                  details={"type": kind, "kind": kind, "journal_id": entry["id"], "author": author})
        self.dispatcher.emit("ticket.commented", ticket, p.username, ctx.source, comment=entry,
                             tenant=ctx.tenant, request_id=ctx.request_id)
        return 201, entry

    # =====================================================================
    # custom fields
    # =====================================================================
    @staticmethod
    def _field_def_dict(row):
        return {
            "name": row["name"],
            "label": row["label"],
            "type": row["type"],
            "choices": loads(row["choices"], []),
            "required": bool(row["required"]),
            "created_at": row["created_at"],
        }

    def list_custom_fields(self, ctx):
        return 200, list(self._field_defs(self._tid(ctx)).values())

    @staticmethod
    def _choices(value, ftype):
        if ftype != "choice":
            if value not in (None, []):
                raise ApiError(422, "'choices' only apply to type 'choice'")
            return []
        if not isinstance(value, list) or not value or not all(
            isinstance(c, str) and c.strip() for c in value
        ):
            raise ApiError(422, "type 'choice' needs a non-empty list of strings in 'choices'")
        if len(set(value)) != len(value):
            raise ApiError(422, "'choices' must be unique")
        return value

    def create_custom_field(self, ctx):
        require_permission(ctx.principal, "fields.manage")
        tid = self._tid(ctx)
        body = _require_obj(ctx.body)
        unknown = set(body) - {"name", "label", "type", "choices", "required"}
        if unknown:
            raise ApiError(422, "unknown field '%s'" % sorted(unknown)[0])
        name = body.get("name")
        if not isinstance(name, str) or not FIELD_NAME_RE.match(name):
            raise ApiError(422, "'name' must match [a-z0-9_]{1,64}")
        ftype = body.get("type")
        if ftype not in FIELD_TYPES:
            raise ApiError(422, "'type' must be one of: %s" % ", ".join(FIELD_TYPES))
        label = _text(body.get("label"), "label", max_len=200) or name
        choices = self._choices(body.get("choices"), ftype)
        required = _bool(body.get("required", False), "required")
        if self.db.query_one("SELECT 1 FROM custom_field_defs WHERE name = ? AND tenant_id = ?", (name, tid)):
            raise ApiError(409, "custom field '%s' already exists" % name)
        self.db.execute(
            "INSERT INTO custom_field_defs(tenant_id, name, label, type, choices, required, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (tid, name, label, ftype, json.dumps(choices), int(required), utcnow()),
        )
        self._log(ctx, "custom_field.create", object_type="custom_field", object_id=name,
                  details={"type": ftype, "label": label, "required": required, "choices": choices})
        return 201, self._field_defs(tid)[name]

    def patch_custom_field(self, ctx, name):
        require_permission(ctx.principal, "fields.manage")
        tid = self._tid(ctx)
        body = _require_obj(ctx.body)
        current = self._field_defs(tid).get(name)
        if current is None:
            raise ApiError(404, "custom field not found")
        for key in body:
            if key in ("name", "type", "created_at"):
                raise ApiError(422, "'%s' is read-only" % key)
            if key not in ("label", "choices", "required"):
                raise ApiError(422, "unknown field '%s'" % key)
        label = _text(body["label"], "label", True, 200) if "label" in body else current["label"]
        choices = self._choices(body["choices"], current["type"]) if "choices" in body else current["choices"]
        required = _bool(body["required"], "required") if "required" in body else current["required"]
        self.db.execute(
            "UPDATE custom_field_defs SET label = ?, choices = ?, required = ? WHERE name = ? AND tenant_id = ?",
            (label, json.dumps(choices), int(required), name, tid),
        )
        updated = self._field_defs(tid)[name]
        changes = {k: {"old": current[k], "new": updated[k]} for k in ("label", "choices", "required")
                   if current[k] != updated[k]}
        self._log(ctx, "custom_field.update", object_type="custom_field", object_id=name,
                  details={"changed": sorted(changes), "changes": changes})
        return 200, updated

    def delete_custom_field(self, ctx, name):
        require_permission(ctx.principal, "fields.manage")
        cur = self.db.execute("DELETE FROM custom_field_defs WHERE name = ? AND tenant_id = ?",
                              (name, self._tid(ctx)))
        if cur.rowcount == 0:
            raise ApiError(404, "custom field not found")
        self._log(ctx, "custom_field.delete", object_type="custom_field", object_id=name)
        return 204, None

    # =====================================================================
    # users & tokens
    # =====================================================================
    def _user_dict(self, row):
        tenant = tenant_by_id(self.db, row["tenant_id"])
        return {
            "username": row["username"],
            "display_name": row["display_name"],
            "email": row["email"],
            "role": row["role"],
            "tenant": tenant["slug"] if tenant is not None else None,
            "active": bool(row["active"]),
            "teams": user_teams(self.db, row["id"]),
            "has_password": bool(row["password_hash"]),
            "created_at": row["created_at"],
        }

    def _visible_user(self, ctx, row):
        """Users of the request's tenant; platform admins additionally see platform admins."""
        if row["tenant_id"] is None:
            return ctx.principal.is_platform_admin
        return row["tenant_id"] == self._tid(ctx)

    def _load_user(self, ctx, username):
        row = self.db.query_one("SELECT * FROM users WHERE username = ?", (username,))
        if row is None or not self._visible_user(ctx, row):
            raise ApiError(404, "user not found")
        return row

    def list_users(self, ctx):
        require_permission(ctx.principal, "tickets.read_all")  # requesters (self-service) do not
        if ctx.principal.is_platform_admin:
            rows = self.db.query("SELECT * FROM users WHERE tenant_id = ? OR tenant_id IS NULL ORDER BY username",
                                 (self._tid(ctx),))
        else:
            rows = self.db.query("SELECT * FROM users WHERE tenant_id = ? ORDER BY username", (self._tid(ctx),))
        return 200, [self._user_dict(r) for r in rows]

    def _set_user_teams(self, user_id, teams, tenant_id):
        if not isinstance(teams, list) or not all(isinstance(t, str) for t in teams):
            raise ApiError(422, "'teams' must be a list of team names")
        ids = []
        for name in teams:
            team = None
            if tenant_id is not None:
                team = self.db.query_one("SELECT id FROM teams WHERE name = ? AND tenant_id = ?", (name, tenant_id))
            if team is None:
                raise ApiError(422, "unknown team '%s'" % name)
            ids.append(team["id"])
        self.db.execute("DELETE FROM team_members WHERE user_id = ?", (user_id,))
        for team_id in ids:
            self.db.execute("INSERT INTO team_members(team_id, user_id) VALUES(?, ?)", (team_id, user_id))

    @staticmethod
    def _password(value):
        if not isinstance(value, str) or len(value) < 8:
            raise ApiError(422, "'password' must be a string with at least 8 characters")
        return value

    def _target_tenant_for_new_user(self, ctx, body, role):
        """Tenant id for POST /users (None for platform_admin)."""
        p = ctx.principal
        slug = body.get("tenant")
        if role == "platform_admin":
            if not p.is_platform_admin:
                raise _forbidden("only platform admins may create platform admins")
            if slug not in (None, ""):
                raise ApiError(422, "platform_admin users have no tenant")
            if body.get("teams"):
                raise ApiError(422, "platform_admin users cannot be team members")
            return None
        if p.is_platform_admin:
            if slug in (None, ""):
                raise ApiError(422, "'tenant' (slug) is required")
            row = tenant_by_slug(self.db, slug)
            if row is None:
                raise ApiError(422, "unknown tenant '%s'" % slug)
            return row["id"]
        if slug not in (None, "") and slug != ctx.tenant["slug"]:
            raise AuthError(403, "tenant mismatch")
        return self._tid(ctx)

    def create_user(self, ctx):
        require_permission(ctx.principal, "users.manage")
        body = _require_obj(ctx.body)
        unknown = set(body) - {"username", "display_name", "email", "role", "password", "teams", "active", "tenant"}
        if unknown:
            raise ApiError(422, "unknown field '%s'" % sorted(unknown)[0])
        username = body.get("username")
        if not isinstance(username, str) or not USERNAME_RE.match(username):
            raise ApiError(422, "'username' must match [A-Za-z0-9_.@-]{1,64}")
        role = body.get("role")
        if role not in ROLES:
            raise ApiError(422, "'role' must be one of: %s" % ", ".join(ROLES))
        tenant_id = self._target_tenant_for_new_user(ctx, body, role)
        display = _text(body.get("display_name"), "display_name", max_len=200) or username
        email = _text(body.get("email"), "email", max_len=200)
        password = self._password(body["password"]) if body.get("password") is not None else None
        active = _bool(body.get("active", True), "active")
        if self.db.query_one("SELECT 1 FROM users WHERE username = ?", (username,)):
            raise ApiError(409, "user '%s' already exists" % username)
        with self.db.transaction():
            cur = self.db.execute(
                "INSERT INTO users(username, display_name, email, role, password_hash, active, created_at, "
                "tenant_id) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (username, display, email, role, hash_password(password) if password else None,
                 int(active), utcnow(), tenant_id),
            )
            if "teams" in body:
                self._set_user_teams(cur.lastrowid, body["teams"], tenant_id)
        user = self._user_dict(self.db.query_one("SELECT * FROM users WHERE username = ?", (username,)))
        self._log(ctx, "user.create", object_type="user", object_id=username,
                  tenant=user["tenant"] or ctx.tenant["slug"],
                  details={"role": role, "tenant": user["tenant"], "active": active, "teams": user["teams"],
                           "changed": sorted(k for k in body if k != "password"),
                           "password_set": password is not None})
        return 201, user

    def _active_platform_admins(self):
        return self.db.query_one(
            "SELECT COUNT(*) AS n FROM users WHERE role = 'platform_admin' AND active = 1"
        )["n"]

    def patch_user(self, ctx, username):
        require_permission(ctx.principal, "users.manage")
        p = ctx.principal
        body = _require_obj(ctx.body)
        row = self._load_user(ctx, username)
        for key in body:
            if key in ("username", "created_at", "tenant"):
                raise ApiError(422, "'%s' is read-only" % key)
            if key not in ("display_name", "email", "role", "password", "teams", "active"):
                raise ApiError(422, "unknown field '%s'" % key)
        role = body.get("role", row["role"])
        if role not in ROLES:
            raise ApiError(422, "'role' must be one of: %s" % ", ".join(ROLES))
        if role == "platform_admin" and not p.is_platform_admin:
            raise _forbidden("only platform admins may grant the role platform_admin")
        active = _bool(body["active"], "active") if "active" in body else bool(row["active"])
        if row["role"] == "platform_admin" and row["active"] and (role != "platform_admin" or not active) \
                and self._active_platform_admins() <= 1:
            raise ApiError(409, "cannot demote or deactivate the last active platform admin")
        if (role == "platform_admin") != (row["role"] == "platform_admin"):
            raise ApiError(422, "changing a role to or from platform_admin is not supported; "
                                "create a new user instead")
        display = _text(body["display_name"], "display_name", max_len=200) if "display_name" in body \
            else row["display_name"]
        email = _text(body["email"], "email", max_len=200) if "email" in body else row["email"]
        pw_hash = row["password_hash"]
        if "password" in body:
            pw_hash = hash_password(self._password(body["password"])) if body["password"] is not None else None
        before = self._user_dict(row)
        with self.db.transaction():
            self.db.execute(
                "UPDATE users SET display_name = ?, email = ?, role = ?, active = ?, password_hash = ? "
                "WHERE id = ?",
                (display, email, role, int(active), pw_hash, row["id"]),
            )
            if "teams" in body:
                self._set_user_teams(row["id"], body["teams"], row["tenant_id"])
            if not active or "password" in body:
                self.db.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        after = self._user_dict(self._load_user(ctx, username))
        changes = {k: {"old": before[k], "new": after[k]}
                   for k in ("display_name", "email", "role", "active", "teams") if before[k] != after[k]}
        self._log(ctx, "user.update", object_type="user", object_id=username,
                  tenant=after["tenant"] or ctx.tenant["slug"],
                  details={"changed": sorted(body), "changes": changes,
                           "password_changed": "password" in body})
        return 200, after

    @staticmethod
    def _token_dict(row):
        return {
            "id": row["id"],
            "name": row["name"],
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"],
        }

    def list_tokens(self, ctx, username):
        require_permission(ctx.principal, "users.manage")
        row = self._load_user(ctx, username)
        rows = self.db.query("SELECT * FROM api_tokens WHERE user_id = ? ORDER BY id", (row["id"],))
        return 200, [self._token_dict(r) for r in rows]

    def create_token(self, ctx, username):
        require_permission(ctx.principal, "users.manage")
        body = _require_obj(ctx.body)
        row = self._load_user(ctx, username)
        name = _text(body.get("name"), "name", max_len=100) or "api"
        token = new_token()
        cur = self.db.execute(
            "INSERT INTO api_tokens(user_id, name, token_hash, created_at) VALUES(?, ?, ?, ?)",
            (row["id"], name, hash_token(token), utcnow()),
        )
        data = self._token_dict(self.db.query_one("SELECT * FROM api_tokens WHERE id = ?", (cur.lastrowid,)))
        self._log(ctx, "token.create", object_type="token", object_id=str(data["id"]),
                  details={"name": name, "username": username})
        data["username"] = username
        data["token"] = token
        return 201, data

    def delete_token(self, ctx, username, token_id):
        require_permission(ctx.principal, "users.manage")
        row = self._load_user(ctx, username)
        existing = self.db.query_one("SELECT name FROM api_tokens WHERE id = ? AND user_id = ?",
                                     (int(token_id), row["id"]))
        cur = self.db.execute(
            "DELETE FROM api_tokens WHERE id = ? AND user_id = ?", (int(token_id), row["id"])
        )
        if cur.rowcount == 0:
            raise ApiError(404, "token not found")
        self._log(ctx, "token.revoke", object_type="token", object_id=str(int(token_id)),
                  details={"name": existing["name"] if existing else None, "username": username})
        return 204, None

    # =====================================================================
    # teams
    # =====================================================================
    def _team_dict(self, row):
        members = self.db.query(
            "SELECT u.username FROM users u JOIN team_members m ON m.user_id = u.id "
            "WHERE m.team_id = ? ORDER BY u.username",
            (row["id"],),
        )
        return {
            "name": row["name"],
            "description": row["description"],
            "members": [m["username"] for m in members],
        }

    def _load_team(self, ctx, name):
        row = self.db.query_one("SELECT * FROM teams WHERE name = ? AND tenant_id = ?", (name, self._tid(ctx)))
        if row is None:
            raise ApiError(404, "team not found")
        return row

    def list_teams(self, ctx):
        require_permission(ctx.principal, "tickets.read_all")  # requesters (self-service) do not
        rows = self.db.query("SELECT * FROM teams WHERE tenant_id = ? ORDER BY name", (self._tid(ctx),))
        return 200, [self._team_dict(r) for r in rows]

    def create_team(self, ctx):
        require_permission(ctx.principal, "teams.manage")
        tid = self._tid(ctx)
        body = _require_obj(ctx.body)
        unknown = set(body) - {"name", "description", "members"}
        if unknown:
            raise ApiError(422, "unknown field '%s'" % sorted(unknown)[0])
        name = _text(body.get("name"), "name", required=True, max_len=100).strip()
        description = _text(body.get("description"), "description", max_len=500)
        members = body.get("members", [])
        if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
            raise ApiError(422, "'members' must be a list of usernames")
        if self.db.query_one("SELECT 1 FROM teams WHERE name = ? AND tenant_id = ?", (name, tid)):
            raise ApiError(409, "team '%s' already exists" % name)
        with self.db.transaction():
            cur = self.db.execute("INSERT INTO teams(tenant_id, name, description) VALUES(?, ?, ?)",
                                  (tid, name, description))
            for username in members:
                user = self._load_user_422(ctx, username)
                self.db.execute(
                    "INSERT OR IGNORE INTO team_members(team_id, user_id) VALUES(?, ?)",
                    (cur.lastrowid, user["id"]),
                )
        team = self._team_dict(self._load_team(ctx, name))
        self._log(ctx, "team.create", object_type="team", object_id=name,
                  details={"description": description, "members": team["members"]})
        return 201, team

    def _load_user_422(self, ctx, username):
        """Team members must be users of the same tenant."""
        row = self.db.query_one("SELECT * FROM users WHERE username = ? AND tenant_id = ?",
                                (username, self._tid(ctx)))
        if row is None:
            raise ApiError(422, "unknown user '%s'" % username)
        return row

    def add_team_member(self, ctx, name):
        require_permission(ctx.principal, "teams.manage")
        body = _require_obj(ctx.body)
        team = self._load_team(ctx, name)
        user = self._load_user_422(ctx, body.get("username"))
        self.db.execute(
            "INSERT OR IGNORE INTO team_members(team_id, user_id) VALUES(?, ?)", (team["id"], user["id"])
        )
        self._log(ctx, "team.member_add", object_type="team", object_id=name,
                  details={"username": user["username"]})
        return 200, self._team_dict(team)

    def remove_team_member(self, ctx, name, username):
        require_permission(ctx.principal, "teams.manage")
        team = self._load_team(ctx, name)
        user = self._load_user(ctx, username)
        self.db.execute("DELETE FROM team_members WHERE team_id = ? AND user_id = ?", (team["id"], user["id"]))
        self._log(ctx, "team.member_remove", object_type="team", object_id=name,
                  details={"username": username})
        return 200, self._team_dict(team)

    # =====================================================================
    # webhooks
    # =====================================================================
    @staticmethod
    def _webhook_dict(row):
        return {
            "id": row["id"],
            "name": row["name"],
            "type": row["type"],
            "url": row["url"],
            "events": loads(row["events"], []),
            "active": bool(row["active"]),
            "soar_label": row["soar_label"],
            "verify_tls": bool(row["verify_tls"]),
            "has_secret": bool(row["secret"]),
            "created_at": row["created_at"],
        }

    def _load_webhook(self, ctx, hook_id):
        row = self.db.query_one("SELECT * FROM webhooks WHERE id = ? AND tenant_id = ?",
                                (int(hook_id), self._tid(ctx)))
        if row is None:
            raise ApiError(404, "webhook not found")
        return row

    def list_webhooks(self, ctx):
        require_permission(ctx.principal, "webhooks.manage")
        rows = self.db.query("SELECT * FROM webhooks WHERE tenant_id = ? ORDER BY id", (self._tid(ctx),))
        return 200, [self._webhook_dict(r) for r in rows]

    def create_webhook(self, ctx):
        require_permission(ctx.principal, "webhooks.manage")
        body = _require_obj(ctx.body)
        unknown = set(body) - {"name", "type", "url", "secret", "events", "active", "soar_label", "verify_tls"}
        if unknown:
            raise ApiError(422, "unknown field '%s'" % sorted(unknown)[0])
        wtype = body.get("type", "generic")
        if wtype not in TYPES:
            raise ApiError(422, "'type' must be one of: %s" % ", ".join(TYPES))
        url = _text(body.get("url"), "url", required=True, max_len=2000).strip()
        if not re.match(r"^https?://[^\s/]+", url, re.IGNORECASE):
            raise ApiError(422, "'url' must be an http(s) URL")
        events = body.get("events", list(EVENTS))
        if not isinstance(events, list) or not events or any(e not in EVENTS for e in events):
            raise ApiError(422, "'events' must be a non-empty list of: %s" % ", ".join(EVENTS))
        secret = _text(body.get("secret"), "secret", max_len=500)
        if wtype == "soar" and not secret:
            raise ApiError(422, "'secret' (SOAR ph-auth-token) is required for type 'soar'")
        name = _text(body.get("name"), "name", max_len=200)
        label = _text(body.get("soar_label"), "soar_label", max_len=100) or "mydemoticket"
        active = _bool(body.get("active", True), "active")
        verify = _bool(body.get("verify_tls", True), "verify_tls")
        cur = self.db.execute(
            "INSERT INTO webhooks(name, type, url, secret, events, active, soar_label, verify_tls, created_at, "
            "tenant_id) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, wtype, url, secret, json.dumps(sorted(set(events))), int(active), label, int(verify),
             utcnow(), self._tid(ctx)),
        )
        hook = self._webhook_dict(self.db.query_one("SELECT * FROM webhooks WHERE id = ?", (cur.lastrowid,)))
        self._log(ctx, "webhook.create", object_type="webhook", object_id=str(hook["id"]),
                  details={"type": wtype, "name": name, "url_host": _url_host(url), "events": hook["events"],
                           "active": active, "verify_tls": verify})
        return 201, hook

    def delete_webhook(self, ctx, hook_id):
        require_permission(ctx.principal, "webhooks.manage")
        hook = self._load_webhook(ctx, hook_id)
        self.db.execute("DELETE FROM webhooks WHERE id = ?", (hook["id"],))
        self._log(ctx, "webhook.delete", object_type="webhook", object_id=str(hook["id"]),
                  details={"type": hook["type"], "name": hook["name"], "url_host": _url_host(hook["url"])})
        return 204, None

    def webhook_deliveries(self, ctx, hook_id):
        require_permission(ctx.principal, "webhooks.manage")
        hook = self._load_webhook(ctx, hook_id)
        return 200, self.dispatcher.deliveries(hook["id"])


def _seconds_between(start, end):
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (datetime.datetime.strptime(end, fmt) - datetime.datetime.strptime(start, fmt)).total_seconds()


__all__ = ["Api", "ApiError", "AuthError", "NullAudit", "STATES", "TRANSITIONS"]
