"""REST API handlers. Each handler returns (status, json_payload)."""

import datetime
import json
import re

from . import __version__
from .auth import (
    ROLES,
    AuthError,
    can_modify_ticket,
    can_read_ticket,
    create_session,
    delete_session,
    hash_password,
    hash_token,
    new_token,
    require_role,
    user_teams,
    verify_password,
)
from .db import dumps, loads, parse_iso, utcnow
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
FIELD_TYPES = ("string", "number", "boolean", "choice", "date")

TICKET_WRITABLE = (
    "short_description", "description", "state", "priority", "assignment_group",
    "assigned_to", "caller", "category", "custom_fields", "external_refs", "close_notes",
)
TICKET_READONLY = ("id", "number", "created_at", "updated_at", "resolved_at")
REQUESTER_CREATE_FIELDS = ("short_description", "description", "priority", "category", "custom_fields")

FIELD_NAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
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


def _bool(value, field):
    if not isinstance(value, bool):
        raise ApiError(422, "'%s' must be a boolean" % field)
    return value


class Api:
    def __init__(self, db, dispatcher):
        self.db = db
        self.dispatcher = dispatcher

    # =====================================================================
    # misc
    # =====================================================================
    def health(self, ctx):
        return 200, {"status": "ok", "version": __version__}

    def me(self, ctx):
        p = ctx.principal
        row = self.db.query_one("SELECT * FROM users WHERE id = ?", (p.id,))
        data = self._user_dict(row)
        if p.via == "session":
            data["csrf_token"] = p.csrf_token
        return 200, data

    def login(self, username, password):
        """Returns (session_token, csrf_token, user_dict) or raises 401."""
        row = None
        if isinstance(username, str) and isinstance(password, str):
            row = self.db.query_one("SELECT * FROM users WHERE username = ?", (username,))
        if row is None or not row["active"] or not row["password_hash"]:
            verify_password(password if isinstance(password, str) else "", _DUMMY_HASH)
            raise ApiError(401, "invalid username or password")
        if not verify_password(password, row["password_hash"]):
            raise ApiError(401, "invalid username or password")
        session, csrf = create_session(self.db, row["id"])
        data = self._user_dict(row)
        data["csrf_token"] = csrf
        return session, csrf, data

    def logout(self, session):
        delete_session(self.db, session)
        return 200, {"status": "logged_out"}

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

    def _load_ticket(self, number):
        if isinstance(number, str) and number.isdigit():
            row = self.db.query_one("SELECT * FROM tickets WHERE id = ?", (int(number),))
        else:
            row = self.db.query_one("SELECT * FROM tickets WHERE number = ?", (number,))
        if row is None:
            raise ApiError(404, "ticket not found")
        return row

    def _readable_ticket(self, principal, number):
        row = self._load_ticket(number)
        if not can_read_ticket(principal, row):
            raise _forbidden("you may not access this ticket")
        return row

    def _field_defs(self):
        rows = self.db.query("SELECT * FROM custom_field_defs ORDER BY name")
        return {r["name"]: self._field_def_dict(r) for r in rows}

    def _validate_custom_values(self, values, existing=None, creating=False):
        """Validate custom field values and return the merged dict."""
        if not isinstance(values, dict):
            raise ApiError(422, "'custom_fields' must be an object")
        defs = self._field_defs()
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

    def _check_group(self, value):
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ApiError(422, "'assignment_group' must be a team name or null")
        if self.db.query_one("SELECT 1 FROM teams WHERE name = ?", (value,)) is None:
            raise ApiError(422, "unknown assignment_group '%s'" % value)
        return value

    def _check_user(self, value, field, allow_null=True):
        if value is None or value == "":
            if allow_null:
                return None
            raise ApiError(422, "'%s' is required" % field)
        if not isinstance(value, str):
            raise ApiError(422, "'%s' must be a username" % field)
        if self.db.query_one("SELECT 1 FROM users WHERE username = ?", (value,)) is None:
            raise ApiError(422, "unknown user '%s' for '%s'" % (value, field))
        return value

    def list_tickets(self, ctx):
        p, q = ctx.principal, ctx.query
        where, params = [], []
        if p.role == "requester":
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
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = self.db.query_one("SELECT COUNT(*) AS n FROM tickets" + clause, params)["n"]
        rows = self.db.query(
            "SELECT * FROM tickets%s ORDER BY %s LIMIT ? OFFSET ?" % (clause, sort_map[sort]),
            params + [limit, offset],
        )
        return 200, {"items": [self._ticket_dict(r) for r in rows], "total": total}

    def get_ticket(self, ctx, number):
        return 200, self._ticket_dict(self._readable_ticket(ctx.principal, number))

    def create_ticket(self, ctx):
        p, body = ctx.principal, _require_obj(ctx.body)
        for key in body:
            if key in TICKET_READONLY:
                raise ApiError(422, "'%s' is read-only" % key)
            if key not in TICKET_WRITABLE:
                raise ApiError(422, "unknown field '%s'" % key)
        if p.role == "requester":
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
        group = self._check_group(body.get("assignment_group"))
        assignee = self._check_user(body.get("assigned_to"), "assigned_to")
        caller = p.username if p.role == "requester" else (
            self._check_user(body.get("caller"), "caller") or p.username
        )
        custom = self._validate_custom_values(body.get("custom_fields") or {}, creating=True)
        refs = self._validate_refs(body.get("external_refs") or {})
        now = utcnow()
        with self.db.transaction():
            number = self.db.next_ticket_number()
            cur = self.db.execute(
                "INSERT INTO tickets(number, short_description, description, state, priority, "
                "assignment_group, assigned_to, caller, category, custom_fields, external_refs, "
                "created_at, updated_at, resolved_at, close_notes) "
                "VALUES(?, ?, ?, 'new', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '')",
                (number, short, description, priority, group, assignee, caller, category,
                 dumps(custom), dumps(refs), now, now),
            )
            self._audit(cur.lastrowid, "created", None, number, p.username, ctx.source, now)
        ticket = self._ticket_dict(self._load_ticket(number))
        self.dispatcher.emit("ticket.created", ticket, p.username, ctx.source)
        return 201, ticket

    def _audit(self, ticket_id, field, old, new, actor, source, at):
        self.db.execute(
            "INSERT INTO journal(ticket_id, type, body, field, old_value, new_value, actor, source, at) "
            "VALUES(?, 'audit', NULL, ?, ?, ?, ?, ?, ?)",
            (ticket_id, field, json.dumps(old), json.dumps(new), actor, source, at),
        )

    def patch_ticket(self, ctx, number):
        p, body = ctx.principal, _require_obj(ctx.body)
        row = self._readable_ticket(p, number)
        for key in body:
            if key in TICKET_READONLY:
                raise ApiError(422, "'%s' is read-only" % key)
            if key not in TICKET_WRITABLE:
                raise ApiError(422, "unknown field '%s'" % key)
        if p.role == "requester":
            if set(body) - {"state"} or body.get("state") != "cancelled":
                raise _forbidden("requesters may only cancel their own tickets")
        elif not can_modify_ticket(p, row):
            raise _forbidden("ticket is assigned to another team or user")

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
            new["assignment_group"] = self._check_group(body["assignment_group"])
        if "assigned_to" in body:
            new["assigned_to"] = self._check_user(body["assigned_to"], "assigned_to")
        if "caller" in body:
            new["caller"] = self._check_user(body["caller"], "caller", allow_null=False)
        if "close_notes" in body:
            new["close_notes"] = _text(body["close_notes"], "close_notes")
        if "custom_fields" in body:
            new["custom_fields"] = self._validate_custom_values(body["custom_fields"], current["custom_fields"])
        if "external_refs" in body:
            new["external_refs"] = self._validate_refs(body["external_refs"], current["external_refs"])
        now = utcnow()
        if "state" in body:
            target = body["state"]
            if target not in STATES:
                raise ApiError(422, "unknown state '%s'" % target)
            if target != current["state"]:
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
                self._audit(row["id"], field, change["old"], change["new"], p.username, ctx.source, now)
        ticket = self._ticket_dict(self._load_ticket(row["number"]))
        event = "ticket.state_changed" if "state" in changes else "ticket.updated"
        self.dispatcher.emit(event, ticket, p.username, ctx.source, changes=changes)
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
        """Persist the SOAR container id without emitting a webhook event."""
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
            self._audit(row["id"], "external_refs.soar_container_id", old, container_id,
                        "system", "webhook", now)

    # -- journal ------------------------------------------------------------
    @staticmethod
    def _journal_dict(row):
        entry = {
            "id": row["id"],
            "type": row["type"],
            "actor": row["actor"],
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
        row = self._readable_ticket(ctx.principal, number)
        sql = "SELECT * FROM journal WHERE ticket_id = ?"
        if ctx.principal.role == "requester":
            sql += " AND type != 'work_note'"
        rows = self.db.query(sql + " ORDER BY id ASC", (row["id"],))
        return 200, [self._journal_dict(r) for r in rows]

    def add_comment(self, ctx, number):
        p, body = ctx.principal, _require_obj(ctx.body)
        row = self._readable_ticket(p, number)
        kind = body.get("type", "comment")
        if kind not in ("comment", "work_note"):
            raise ApiError(422, "'type' must be 'comment' or 'work_note'")
        unknown = set(body) - {"body", "type"}
        if unknown:
            raise ApiError(422, "unknown field '%s'" % sorted(unknown)[0])
        if p.role == "requester":
            if kind != "comment":
                raise _forbidden("requesters may only add comments")
        elif not can_modify_ticket(p, row):
            raise _forbidden("ticket is assigned to another team or user")
        text = _text(body.get("body"), "body", required=True)
        now = utcnow()
        with self.db.transaction():
            cur = self.db.execute(
                "INSERT INTO journal(ticket_id, type, body, actor, source, at) VALUES(?, ?, ?, ?, ?, ?)",
                (row["id"], kind, text, p.username, ctx.source, now),
            )
            self.db.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now, row["id"]))
            entry = self._journal_dict(
                self.db.query_one("SELECT * FROM journal WHERE id = ?", (cur.lastrowid,))
            )
        ticket = self._ticket_dict(self._load_ticket(row["number"]))
        self.dispatcher.emit("ticket.commented", ticket, p.username, ctx.source, comment=entry)
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
        return 200, list(self._field_defs().values())

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
        require_role(ctx.principal, "admin")
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
        if self.db.query_one("SELECT 1 FROM custom_field_defs WHERE name = ?", (name,)):
            raise ApiError(409, "custom field '%s' already exists" % name)
        self.db.execute(
            "INSERT INTO custom_field_defs(name, label, type, choices, required, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (name, label, ftype, json.dumps(choices), int(required), utcnow()),
        )
        return 201, self._field_defs()[name]

    def patch_custom_field(self, ctx, name):
        require_role(ctx.principal, "admin")
        body = _require_obj(ctx.body)
        current = self._field_defs().get(name)
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
            "UPDATE custom_field_defs SET label = ?, choices = ?, required = ? WHERE name = ?",
            (label, json.dumps(choices), int(required), name),
        )
        return 200, self._field_defs()[name]

    def delete_custom_field(self, ctx, name):
        require_role(ctx.principal, "admin")
        cur = self.db.execute("DELETE FROM custom_field_defs WHERE name = ?", (name,))
        if cur.rowcount == 0:
            raise ApiError(404, "custom field not found")
        return 204, None

    # =====================================================================
    # users & tokens
    # =====================================================================
    def _user_dict(self, row):
        return {
            "username": row["username"],
            "display_name": row["display_name"],
            "email": row["email"],
            "role": row["role"],
            "active": bool(row["active"]),
            "teams": user_teams(self.db, row["id"]),
            "has_password": bool(row["password_hash"]),
            "created_at": row["created_at"],
        }

    def _load_user(self, username):
        row = self.db.query_one("SELECT * FROM users WHERE username = ?", (username,))
        if row is None:
            raise ApiError(404, "user not found")
        return row

    def list_users(self, ctx):
        require_role(ctx.principal, "admin", "agent", "integration")
        rows = self.db.query("SELECT * FROM users ORDER BY username")
        return 200, [self._user_dict(r) for r in rows]

    def _set_user_teams(self, user_id, teams):
        if not isinstance(teams, list) or not all(isinstance(t, str) for t in teams):
            raise ApiError(422, "'teams' must be a list of team names")
        ids = []
        for name in teams:
            team = self.db.query_one("SELECT id FROM teams WHERE name = ?", (name,))
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

    def create_user(self, ctx):
        require_role(ctx.principal, "admin")
        body = _require_obj(ctx.body)
        unknown = set(body) - {"username", "display_name", "email", "role", "password", "teams", "active"}
        if unknown:
            raise ApiError(422, "unknown field '%s'" % sorted(unknown)[0])
        username = body.get("username")
        if not isinstance(username, str) or not USERNAME_RE.match(username):
            raise ApiError(422, "'username' must match [A-Za-z0-9_.@-]{1,64}")
        role = body.get("role")
        if role not in ROLES:
            raise ApiError(422, "'role' must be one of: %s" % ", ".join(ROLES))
        display = _text(body.get("display_name"), "display_name", max_len=200) or username
        email = _text(body.get("email"), "email", max_len=200)
        password = self._password(body["password"]) if body.get("password") is not None else None
        active = _bool(body.get("active", True), "active")
        if self.db.query_one("SELECT 1 FROM users WHERE username = ?", (username,)):
            raise ApiError(409, "user '%s' already exists" % username)
        with self.db.transaction():
            cur = self.db.execute(
                "INSERT INTO users(username, display_name, email, role, password_hash, active, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (username, display, email, role, hash_password(password) if password else None,
                 int(active), utcnow()),
            )
            if "teams" in body:
                self._set_user_teams(cur.lastrowid, body["teams"])
        return 201, self._user_dict(self._load_user(username))

    def _active_admins(self):
        return self.db.query_one("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND active = 1")["n"]

    def patch_user(self, ctx, username):
        require_role(ctx.principal, "admin")
        body = _require_obj(ctx.body)
        row = self._load_user(username)
        for key in body:
            if key in ("username", "created_at"):
                raise ApiError(422, "'%s' is read-only" % key)
            if key not in ("display_name", "email", "role", "password", "teams", "active"):
                raise ApiError(422, "unknown field '%s'" % key)
        role = body.get("role", row["role"])
        if role not in ROLES:
            raise ApiError(422, "'role' must be one of: %s" % ", ".join(ROLES))
        active = _bool(body["active"], "active") if "active" in body else bool(row["active"])
        if row["role"] == "admin" and row["active"] and (role != "admin" or not active) \
                and self._active_admins() <= 1:
            raise ApiError(409, "cannot demote or deactivate the last active admin")
        display = _text(body["display_name"], "display_name", max_len=200) if "display_name" in body \
            else row["display_name"]
        email = _text(body["email"], "email", max_len=200) if "email" in body else row["email"]
        pw_hash = row["password_hash"]
        if "password" in body:
            pw_hash = hash_password(self._password(body["password"])) if body["password"] is not None else None
        with self.db.transaction():
            self.db.execute(
                "UPDATE users SET display_name = ?, email = ?, role = ?, active = ?, password_hash = ? "
                "WHERE id = ?",
                (display, email, role, int(active), pw_hash, row["id"]),
            )
            if "teams" in body:
                self._set_user_teams(row["id"], body["teams"])
            if not active or "password" in body:
                self.db.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        return 200, self._user_dict(self._load_user(username))

    @staticmethod
    def _token_dict(row):
        return {
            "id": row["id"],
            "name": row["name"],
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"],
        }

    def list_tokens(self, ctx, username):
        require_role(ctx.principal, "admin")
        row = self._load_user(username)
        rows = self.db.query("SELECT * FROM api_tokens WHERE user_id = ? ORDER BY id", (row["id"],))
        return 200, [self._token_dict(r) for r in rows]

    def create_token(self, ctx, username):
        require_role(ctx.principal, "admin")
        body = _require_obj(ctx.body)
        row = self._load_user(username)
        name = _text(body.get("name"), "name", max_len=100) or "api"
        token = new_token()
        cur = self.db.execute(
            "INSERT INTO api_tokens(user_id, name, token_hash, created_at) VALUES(?, ?, ?, ?)",
            (row["id"], name, hash_token(token), utcnow()),
        )
        data = self._token_dict(self.db.query_one("SELECT * FROM api_tokens WHERE id = ?", (cur.lastrowid,)))
        data["username"] = username
        data["token"] = token
        return 201, data

    def delete_token(self, ctx, username, token_id):
        require_role(ctx.principal, "admin")
        row = self._load_user(username)
        cur = self.db.execute(
            "DELETE FROM api_tokens WHERE id = ? AND user_id = ?", (int(token_id), row["id"])
        )
        if cur.rowcount == 0:
            raise ApiError(404, "token not found")
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

    def _load_team(self, name):
        row = self.db.query_one("SELECT * FROM teams WHERE name = ?", (name,))
        if row is None:
            raise ApiError(404, "team not found")
        return row

    def list_teams(self, ctx):
        require_role(ctx.principal, "admin", "agent", "integration")
        return 200, [self._team_dict(r) for r in self.db.query("SELECT * FROM teams ORDER BY name")]

    def create_team(self, ctx):
        require_role(ctx.principal, "admin")
        body = _require_obj(ctx.body)
        unknown = set(body) - {"name", "description", "members"}
        if unknown:
            raise ApiError(422, "unknown field '%s'" % sorted(unknown)[0])
        name = _text(body.get("name"), "name", required=True, max_len=100).strip()
        description = _text(body.get("description"), "description", max_len=500)
        members = body.get("members", [])
        if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
            raise ApiError(422, "'members' must be a list of usernames")
        if self.db.query_one("SELECT 1 FROM teams WHERE name = ?", (name,)):
            raise ApiError(409, "team '%s' already exists" % name)
        with self.db.transaction():
            cur = self.db.execute("INSERT INTO teams(name, description) VALUES(?, ?)", (name, description))
            for username in members:
                user = self._load_user_422(username)
                self.db.execute(
                    "INSERT OR IGNORE INTO team_members(team_id, user_id) VALUES(?, ?)",
                    (cur.lastrowid, user["id"]),
                )
        return 201, self._team_dict(self._load_team(name))

    def _load_user_422(self, username):
        row = self.db.query_one("SELECT * FROM users WHERE username = ?", (username,))
        if row is None:
            raise ApiError(422, "unknown user '%s'" % username)
        return row

    def add_team_member(self, ctx, name):
        require_role(ctx.principal, "admin")
        body = _require_obj(ctx.body)
        team = self._load_team(name)
        user = self._load_user_422(body.get("username"))
        self.db.execute(
            "INSERT OR IGNORE INTO team_members(team_id, user_id) VALUES(?, ?)", (team["id"], user["id"])
        )
        return 200, self._team_dict(team)

    def remove_team_member(self, ctx, name, username):
        require_role(ctx.principal, "admin")
        team = self._load_team(name)
        user = self._load_user(username)
        self.db.execute("DELETE FROM team_members WHERE team_id = ? AND user_id = ?", (team["id"], user["id"]))
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

    def list_webhooks(self, ctx):
        require_role(ctx.principal, "admin")
        return 200, [self._webhook_dict(r) for r in self.db.query("SELECT * FROM webhooks ORDER BY id")]

    def create_webhook(self, ctx):
        require_role(ctx.principal, "admin")
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
            "INSERT INTO webhooks(name, type, url, secret, events, active, soar_label, verify_tls, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, wtype, url, secret, json.dumps(sorted(set(events))), int(active), label, int(verify),
             utcnow()),
        )
        return 201, self._webhook_dict(self.db.query_one("SELECT * FROM webhooks WHERE id = ?", (cur.lastrowid,)))

    def delete_webhook(self, ctx, hook_id):
        require_role(ctx.principal, "admin")
        cur = self.db.execute("DELETE FROM webhooks WHERE id = ?", (int(hook_id),))
        if cur.rowcount == 0:
            raise ApiError(404, "webhook not found")
        return 204, None

    def webhook_deliveries(self, ctx, hook_id):
        require_role(ctx.principal, "admin")
        if self.db.query_one("SELECT 1 FROM webhooks WHERE id = ?", (int(hook_id),)) is None:
            raise ApiError(404, "webhook not found")
        return 200, self.dispatcher.deliveries(int(hook_id))


__all__ = ["Api", "ApiError", "AuthError", "STATES", "TRANSITIONS"]
