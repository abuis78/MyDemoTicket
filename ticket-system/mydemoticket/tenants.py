"""Tenants (Mandanten): lookup helpers, validation and per-tenant seed data."""

import json
import re

from .db import utcnow

DEFAULT_TENANT_SLUG = "soc-games"
TENANT_HEADER = "X-MyDemoTicket-Tenant"
SLUG_RE = re.compile(r"^[a-z0-9-]{2,32}$")
INDEX_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")

# Seed for a new tenant created with {"seed": true}.
SEED_TENANT_TEAMS = [
    ("SOC L1", "First-level security operations"),
    ("SOC L2", "Second-level security operations / incident response"),
]

SEED_CUSTOM_FIELDS = [
    ("mitre_technique", "MITRE ATT&CK Technique", "string", []),
    ("affected_host", "Affected Host", "string", []),
    ("risk_score", "Risk Score", "number", []),
    ("incident_type", "Incident Type", "choice", ["malware", "phishing", "data_exfiltration", "other"]),
]


def tenant_by_id(db, tenant_id):
    if tenant_id is None:
        return None
    return db.query_one("SELECT * FROM tenants WHERE id = ?", (tenant_id,))


def tenant_by_slug(db, slug):
    if not isinstance(slug, str):
        return None
    return db.query_one("SELECT * FROM tenants WHERE slug = ?", (slug,))


def default_tenant(db):
    return tenant_by_slug(db, DEFAULT_TENANT_SLUG)


def tenant_ref(row):
    """Short form used in /me and user objects."""
    if row is None:
        return None
    return {"slug": row["slug"], "name": row["name"]}


def tenant_dict(row):
    return {
        "slug": row["slug"],
        "name": row["name"],
        "active": bool(row["active"]),
        "soar_url": row["soar_url"],
        "splunk_index": row["splunk_index"],
        "default": row["slug"] == DEFAULT_TENANT_SLUG,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def seed_custom_fields(db, tenant_id):
    for name, label, ftype, choices in SEED_CUSTOM_FIELDS:
        db.execute(
            "INSERT INTO custom_field_defs(tenant_id, name, label, type, choices, required, created_at) "
            "VALUES(?, ?, ?, ?, ?, 0, ?)",
            (tenant_id, name, label, ftype, json.dumps(list(choices)), utcnow()),
        )


def seed_teams(db, tenant_id, teams):
    """Insert teams and return {name: id}."""
    ids = {}
    for name, description in teams:
        cur = db.execute(
            "INSERT INTO teams(tenant_id, name, description) VALUES(?, ?, ?)", (tenant_id, name, description)
        )
        ids[name] = cur.lastrowid
    return ids
