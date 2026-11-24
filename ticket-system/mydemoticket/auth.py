"""Roles, password/token hashing, sessions, permission checks and bootstrap."""

import datetime
import hashlib
import hmac
import json
import os
import secrets

from .db import utcnow

ROLES = ("admin", "agent", "requester", "integration")

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
    """The authenticated caller of a request."""

    def __init__(self, user_row, teams, via, csrf_token=None):
        self.id = user_row["id"]
        self.username = user_row["username"]
        self.display_name = user_row["display_name"]
        self.role = user_row["role"]
        self.teams = list(teams)
        self.via = via  # "token" or "session"
        self.csrf_token = csrf_token

    @property
    def is_admin(self):
        return self.role == "admin"


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
    return Principal(row, user_teams(db, row["id"]), "token")


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
        "SELECT u.*, s.csrf_token, s.expires_at FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.session_hash = ?",
        (hash_token(session),),
    )
    if row is None or not row["active"]:
        return None
    if row["expires_at"] < utcnow():
        db.execute("DELETE FROM sessions WHERE session_hash = ?", (hash_token(session),))
        return None
    return Principal(row, user_teams(db, row["id"]), "session", csrf_token=row["csrf_token"])


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


def can_read_ticket(principal, ticket):
    if principal.role in ("admin", "agent", "integration"):
        return True
    return ticket["caller"] == principal.username


def can_modify_ticket(principal, ticket):
    """Whether the principal may change fields / add work notes on a ticket."""
    if principal.role in ("admin", "integration"):
        return True
    if principal.role == "agent":
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

SEED_CUSTOM_FIELDS = [
    ("mitre_technique", "MITRE ATT&CK Technique", "string", []),
    ("affected_host", "Affected Host", "string", []),
    ("risk_score", "Risk Score", "number", []),
    ("incident_type", "Incident Type", "choice", ["malware", "phishing", "data_exfiltration", "other"]),
]


def _insert_user(db, username, display_name, role, password=None, email=""):
    cur = db.execute(
        "INSERT INTO users(username, display_name, email, role, password_hash, active, created_at) "
        "VALUES(?, ?, ?, ?, ?, 1, ?)",
        (username, display_name, email, role, hash_password(password) if password else None, utcnow()),
    )
    return cur.lastrowid


def _insert_token(db, user_id, token, name):
    db.execute(
        "INSERT INTO api_tokens(user_id, name, token_hash, created_at) VALUES(?, ?, ?, ?)",
        (user_id, name, hash_token(token), utcnow()),
    )


def bootstrap(db, data_dir, seed=True, admin_password=None, admin_token=None):
    """Create the admin user (and optional demo seed) on first start.

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
        admin_id = _insert_user(db, "admin", "Administrator", "admin", admin_password)
        _insert_token(db, admin_id, admin_token, "initial")
        if generated_admin_pw:
            lines.append("admin password: %s" % admin_password)
        if generated_admin_tok:
            lines.append("admin API token: %s" % admin_token)

        if seed:
            team_ids = {}
            for name, description in SEED_TEAMS:
                cur = db.execute("INSERT INTO teams(name, description) VALUES(?, ?)", (name, description))
                team_ids[name] = cur.lastrowid
            for username, display, role, team in SEED_USERS:
                password = secrets.token_urlsafe(18)
                uid = _insert_user(db, username, display, role, password, "%s@example.invalid" % username)
                lines.append("%s password: %s" % (username, password))
                if role == "integration":
                    token = new_token()
                    _insert_token(db, uid, token, "initial")
                    lines.append("%s API token: %s" % (username, token))
                if team:
                    db.execute(
                        "INSERT INTO team_members(team_id, user_id) VALUES(?, ?)", (team_ids[team], uid)
                    )
            for name, label, ftype, choices in SEED_CUSTOM_FIELDS:
                db.execute(
                    "INSERT INTO custom_field_defs(name, label, type, choices, required, created_at) "
                    "VALUES(?, ?, ?, ?, 0, ?)",
                    (name, label, ftype, _json_list(choices), utcnow()),
                )

    if not lines:
        return None
    return write_credentials(data_dir, lines)


def _json_list(values):
    return json.dumps(list(values))


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
