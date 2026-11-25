"""Roles, password/token hashing, sessions, permission checks and bootstrap."""

import datetime
import hashlib
import hmac
import json
import os
import secrets

from .db import utcnow
from .tenants import (
    default_tenant,
    seed_custom_fields,
    seed_teams,
    tenant_by_id,
    tenant_by_slug,
)

ROLES = ("platform_admin", "admin", "agent", "requester", "integration")
ADMIN_ROLES = ("platform_admin", "admin")
ANALYST_ROLES = ("analyst_l1", "analyst_l2", "soc_lead", "read_only")
BUILTIN_ROLES = ROLES + ANALYST_ROLES

# -- permissions -----------------------------------------------------------
# Analyst permissions: may be granted to custom roles.
GRANTABLE_PERMISSIONS = (
    "tickets.read_all",     # see every ticket of the tenant (without: only tickets I am the caller of)
    "tickets.create",
    "tickets.update_own",   # change tickets that are unassigned, assigned to me or to one of my teams
    "tickets.update_all",   # change any ticket of the tenant
    "tickets.comment",
    "tickets.work_note",    # internal work notes and summaries
    "tickets.close",        # transition to resolved / closed / cancelled
    "reports.view",
)
# Administrative permissions: only built-in roles carry them.
ADMIN_PERMISSIONS = (
    "users.manage", "roles.manage", "teams.manage", "fields.manage", "webhooks.manage",
    "tenants.manage", "splunk.manage",
)
PERMISSIONS = GRANTABLE_PERMISSIONS + ADMIN_PERMISSIONS

_ANALYST_L2 = ("tickets.read_all", "tickets.create", "tickets.update_all", "tickets.comment",
               "tickets.work_note", "tickets.close")
# Derived 1:1 from the role checks before v1.1.0 (see docs/api.md "Roles and permissions").
BUILTIN_ROLE_PERMISSIONS = {
    "platform_admin": PERMISSIONS,
    "admin": GRANTABLE_PERMISSIONS + tuple(p for p in ADMIN_PERMISSIONS
                                           if p not in ("tenants.manage", "splunk.manage")),
    "agent": ("tickets.read_all", "tickets.create", "tickets.update_own", "tickets.comment",
              "tickets.work_note", "tickets.close", "reports.view"),
    "integration": _ANALYST_L2 + ("reports.view",),
    "requester": ("tickets.create", "tickets.comment"),
    "analyst_l1": ("tickets.read_all", "tickets.create", "tickets.update_own", "tickets.comment",
                   "tickets.work_note"),
    "analyst_l2": _ANALYST_L2,
    "soc_lead": _ANALYST_L2 + ("reports.view",),
    "read_only": ("tickets.read_all",),
}
BUILTIN_ROLE_INFO = {
    "platform_admin": ("Platform admin", "Manages all tenants, platform admins and the Splunk connection."),
    "admin": ("Admin", "Manages users, roles, teams, fields and webhooks of a tenant; full ticket access."),
    "agent": ("Agent", "Works tickets of the own teams (or unassigned), resolves and closes them, sees reports."),
    "requester": ("Requester", "Raises tickets, sees and comments only on own tickets, may cancel them."),
    "integration": ("Integration", "API account for Splunk SOAR / ES: full ticket access, no administration."),
    "analyst_l1": ("Analyst L1", "Reads all tickets, works unassigned and own/team tickets; cannot close."),
    "analyst_l2": ("Analyst L2", "Reads and works all tickets of the tenant, may resolve, close and cancel."),
    "soc_lead": ("SOC lead", "Like Analyst L2, plus reports."),
    "read_only": ("Read only", "Reads all tickets of the tenant, no changes and no comments."),
}


def role_permissions(db, role, tenant_id):
    """Permission set of ``role`` for a user of ``tenant_id`` (built-in or custom role)."""
    if role in BUILTIN_ROLE_PERMISSIONS:
        return frozenset(BUILTIN_ROLE_PERMISSIONS[role])
    if tenant_id is None or db is None:
        return frozenset()
    row = db.query_one("SELECT permissions FROM roles WHERE tenant_id = ? AND name = ?", (tenant_id, role))
    if row is None:
        return frozenset()
    try:
        perms = json.loads(row["permissions"] or "[]")
    except ValueError:
        return frozenset()
    # defence in depth: a custom role can never carry administrative permissions
    return frozenset(p for p in perms if p in GRANTABLE_PERMISSIONS)

PBKDF2_ITERATIONS = 200_000
SESSION_TTL_HOURS = 12
SESSION_COOKIE = "mdt_session"
CSRF_HEADER = "X-CSRF-Token"
CREDENTIALS_FILE = "initial-credentials.txt"


class AuthError(Exception):
    """Raised for authentication (401) or authorization (403) failures."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# -- hashing ---------------------------------------------------------------
def hash_password(password, iterations=None):
    iterations = iterations or PBKDF2_ITERATIONS
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256$%d$%s$%s" % (iterations, salt.hex(), digest.hex())


def verify_password(password, stored):
    if not stored or not isinstance(password, str):
        return False
    try:
        algo, iterations, salt_hex, digest_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token():
    return "mdt_" + secrets.token_urlsafe(32)


# -- principal -------------------------------------------------------------
class Principal:
    """The authenticated caller of a request.

    ``tenant_id`` is the user's own tenant (None for platform_admin);
    ``active_tenant_id`` is the tenant a platform_admin chose in the session.
    """

    def __init__(self, user_row, teams, via, csrf_token=None, active_tenant_id=None, permissions=None,
                 session_hash=None):
        self.id = user_row["id"]
        self.username = user_row["username"]
        self.display_name = user_row["display_name"]
        self.role = user_row["role"]
        self.tenant_id = user_row["tenant_id"]
        self.teams = list(teams)
        self.via = via  # "token" or "session"
        self.csrf_token = csrf_token
        self.active_tenant_id = active_tenant_id
        self.session_hash = session_hash  # hash of the session cookie (None for API tokens)
        if permissions is None:
            permissions = BUILTIN_ROLE_PERMISSIONS.get(self.role, ())
        self.permissions = frozenset(permissions)

    def has(self, permission):
        return permission in self.permissions

    @property
    def is_admin(self):
        """Tenant admin or platform admin (admin rights inside the request's tenant)."""
        return self.role in ADMIN_ROLES

    @property
    def self_service(self):
        """Without tickets.read_all a user only sees tickets they are the caller of (requester rules)."""
        return not self.has("tickets.read_all")

    @property
    def is_platform_admin(self):
        return self.role == "platform_admin"


def user_teams(db, user_id):
    rows = db.query(
        "SELECT t.name FROM teams t JOIN team_members m ON m.team_id = t.id "
        "WHERE m.user_id = ? ORDER BY t.name",
        (user_id,),
    )
    return [r["name"] for r in rows]


def principal_from_token(db, token):
    if not token:
        return None
    row = db.query_one(
        "SELECT u.*, t.id AS token_id FROM api_tokens t JOIN users u ON u.id = t.user_id "
        "WHERE t.token_hash = ?",
        (hash_token(token),),
    )
    if row is None or not row["active"]:
        return None
    db.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (utcnow(), row["token_id"]))
    return Principal(row, user_teams(db, row["id"]), "token",
                     permissions=role_permissions(db, row["role"], row["tenant_id"]))


def create_session(db, user_id):
    session = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    now = datetime.datetime.now(datetime.timezone.utc)
    expires = (now + datetime.timedelta(hours=SESSION_TTL_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute(
        "INSERT INTO sessions(session_hash, user_id, csrf_token, created_at, expires_at) "
        "VALUES(?, ?, ?, ?, ?)",
        (hash_token(session), user_id, csrf, utcnow(), expires),
    )
    return session, csrf


def principal_from_session(db, session):
    if not session:
        return None
    row = db.query_one(
        "SELECT u.*, s.csrf_token, s.expires_at, s.active_tenant_id FROM sessions s "
        "JOIN users u ON u.id = s.user_id WHERE s.session_hash = ?",
        (hash_token(session),),
    )
    if row is None or not row["active"]:
        return None
    if row["expires_at"] < utcnow():
        db.execute("DELETE FROM sessions WHERE session_hash = ?", (hash_token(session),))
        return None
    return Principal(row, user_teams(db, row["id"]), "session", csrf_token=row["csrf_token"],
                     active_tenant_id=row["active_tenant_id"],
                     permissions=role_permissions(db, row["role"], row["tenant_id"]),
                     session_hash=hash_token(session))


def set_session_tenant(db, session, tenant_id):
    db.execute("UPDATE sessions SET active_tenant_id = ? WHERE session_hash = ?", (tenant_id, hash_token(session)))


def resolve_tenant(db, principal, header_value=None):
    """Return the tenant row a request works in, or raise AuthError.

    * normal users: their own tenant; a header naming another tenant -> 403
      ``tenant mismatch``; an inactive tenant -> 403 ``tenant inactive``.
    * platform_admin: header slug, else the tenant chosen in the session, else
      the default tenant. An unknown slug -> 404 ``tenant not found``.
    """
    header_value = (header_value or "").strip()
    if principal.is_platform_admin:
        if header_value:
            row = tenant_by_slug(db, header_value)
            if row is None:
                raise AuthError(404, "tenant not found")
            return row
        row = tenant_by_id(db, principal.active_tenant_id)
        return row if row is not None else default_tenant(db)
    row = tenant_by_id(db, principal.tenant_id)
    if row is None:
        raise AuthError(403, "user has no tenant")
    if header_value and header_value != row["slug"]:
        raise AuthError(403, "tenant mismatch")
    if not row["active"]:
        raise AuthError(403, "tenant inactive")
    return row


def delete_session(db, session):
    if session:
        db.execute("DELETE FROM sessions WHERE session_hash = ?", (hash_token(session),))


def check_csrf(principal, header_value):
    """Session-authenticated write requests must echo the CSRF token."""
    if principal.via != "session":
        return
    if not header_value or not hmac.compare_digest(header_value, principal.csrf_token or ""):
        raise AuthError(403, "missing or invalid CSRF token")


# -- permission checks -----------------------------------------------------
def require_role(principal, *roles):
    if principal.role not in roles:
        raise AuthError(403, "forbidden for role '%s'" % principal.role)


def require_permission(principal, permission):
    if not principal.has(permission):
        raise AuthError(403, "forbidden for role '%s' (needs %s)" % (principal.role, permission))


def can_read_ticket(principal, ticket):
    """Same-tenant check is done by the caller (other tenants' tickets are 404)."""
    if principal.has("tickets.read_all"):
        return True
    return ticket["caller"] == principal.username


def can_modify_ticket(principal, ticket):
    """Whether the principal may change fields / add comments and work notes on a ticket.

    tickets.update_all: any ticket. tickets.update_own: unassigned tickets, tickets
    assigned to the principal and tickets of one of the principal's teams.
    """
    if principal.has("tickets.update_all"):
        return True
    if principal.has("tickets.update_own"):
        group = ticket["assignment_group"]
        assignee = ticket["assigned_to"]
        if group is None and assignee is None:
            return True
        if assignee == principal.username:
            return True
        return group is not None and group in principal.teams
    return False


# -- bootstrap -------------------------------------------------------------
SEED_TEAMS = [
    ("SOC L1", "First-level security operations"),
    ("SOC L2", "Second-level security operations / incident response"),
    ("IT Ops", "IT operations"),
]

SEED_USERS = [
    # username, display name, role, team
    ("analyst1", "Analyst One", "agent", "SOC L1"),
    ("analyst2", "Analyst Two", "agent", "SOC L2"),
    ("requester1", "Requester One", "requester", None),
    ("soar", "Splunk SOAR Integration", "integration", None),
]

def _insert_user(db, username, display_name, role, password=None, email="", tenant_id=None):
    cur = db.execute(
        "INSERT INTO users(username, display_name, email, role, password_hash, active, created_at, tenant_id) "
        "VALUES(?, ?, ?, ?, ?, 1, ?, ?)",
        (username, display_name, email, role, hash_password(password) if password else None, utcnow(),
         tenant_id),
    )
    return cur.lastrowid


def _insert_token(db, user_id, token, name):
    db.execute(
        "INSERT INTO api_tokens(user_id, name, token_hash, created_at) VALUES(?, ?, ?, ?)",
        (user_id, name, hash_token(token), utcnow()),
    )


def bootstrap(db, data_dir, seed=True, admin_password=None, admin_token=None):
    """Create the admin user (and optional demo seed) on first start.

    ``admin`` is a platform_admin without tenant; the demo seed (teams, users,
    custom fields) goes into the default tenant created by the migration.

    Credentials that were not provided via environment are generated randomly and
    written to <data_dir>/initial-credentials.txt with mode 0600 - never printed.
    Returns the path of the credentials file if one was written, else None.
    """
    if db.query_one("SELECT 1 FROM users LIMIT 1") is not None:
        return None

    if admin_password is None:
        admin_password = os.environ.get("MYDEMOTICKET_ADMIN_PASSWORD") or None
    if admin_token is None:
        admin_token = os.environ.get("MYDEMOTICKET_ADMIN_TOKEN") or None

    lines = []
    generated_admin_pw = admin_password is None
    generated_admin_tok = admin_token is None
    admin_password = admin_password or secrets.token_urlsafe(18)
    admin_token = admin_token or new_token()

    with db.transaction():
        admin_id = _insert_user(db, "admin", "Administrator", "platform_admin", admin_password)
        _insert_token(db, admin_id, admin_token, "initial")
        if generated_admin_pw:
            lines.append("admin password: %s" % admin_password)
        if generated_admin_tok:
            lines.append("admin API token: %s" % admin_token)

        if seed:
            tenant_id = default_tenant(db)["id"]
            team_ids = seed_teams(db, tenant_id, SEED_TEAMS)
            for username, display, role, team in SEED_USERS:
                password = secrets.token_urlsafe(18)
                uid = _insert_user(db, username, display, role, password, "%s@example.invalid" % username,
                                   tenant_id)
                lines.append("%s password: %s" % (username, password))
                if role == "integration":
                    token = new_token()
                    _insert_token(db, uid, token, "initial")
                    lines.append("%s API token: %s" % (username, token))
                if team:
                    db.execute(
                        "INSERT INTO team_members(team_id, user_id) VALUES(?, ?)", (team_ids[team], uid)
                    )
            seed_custom_fields(db, tenant_id)

    if not lines:
        return None
    return write_credentials(data_dir, lines)


def write_credentials(data_dir, lines):
    path = os.path.join(data_dir, CREDENTIALS_FILE)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("# MyDemoTicket initial credentials - generated %s\n" % utcnow())
        handle.write("# Store them in a password manager, then delete this file.\n")
        for line in lines:
            handle.write(line + "\n")
    os.chmod(path, 0o600)
    return path
