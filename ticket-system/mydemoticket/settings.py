"""Instance-wide settings (table ``settings``) and the effective Splunk HEC connection.

The Splunk connection can come from two places:

* stored settings (Audit / Splunk page, ``PUT /settings/splunk``) - they win;
* the environment (``MYDEMOTICKET_HEC_URL`` / ``_TOKEN`` / ``_VERIFY_TLS`` / ``_INDEX``).

The token is stored only to be able to send events; it is never returned by the
API and never written to a log line.
"""

import os

from .db import utcnow

SPLUNK_FIELDS = ("hec_url", "index", "verify_tls", "enabled")
SPLUNK_TOKEN_KEY = "splunk.token"


def _key(field):
    return "splunk." + field


def get_settings(db, prefix):
    rows = db.query("SELECT key, value FROM settings WHERE key LIKE ?", (prefix + "%",))
    return {r["key"]: r["value"] for r in rows}


def put_setting(db, key, value):
    db.execute(
        "INSERT INTO settings(key, value, updated_at) VALUES(?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, utcnow()),
    )


def _env_bool(value, default=True):
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


def splunk_config(db, environ=None):
    """Effective HEC connection: {hec_url, index, verify_tls, enabled, token, source}.

    ``source`` is "settings" once the connection was saved via the API (then the
    stored values win completely; a token that was never stored falls back to the
    environment token), "env" when MYDEMOTICKET_HEC_URL is set, else "none".
    """
    env = os.environ if environ is None else environ
    env_token = env.get("MYDEMOTICKET_HEC_TOKEN") or ""
    stored = get_settings(db, "splunk.")
    if any(_key(f) in stored for f in SPLUNK_FIELDS):
        token = stored[SPLUNK_TOKEN_KEY] if SPLUNK_TOKEN_KEY in stored else env_token
        return {
            "hec_url": stored.get(_key("hec_url"), ""),
            "index": stored.get(_key("index"), ""),
            "verify_tls": stored.get(_key("verify_tls"), "1") == "1",
            "enabled": stored.get(_key("enabled"), "0") == "1",
            "token": token,
            "source": "settings",
        }
    url = env.get("MYDEMOTICKET_HEC_URL") or ""
    return {
        "hec_url": url,
        "index": env.get("MYDEMOTICKET_HEC_INDEX") or "",
        "verify_tls": _env_bool(env.get("MYDEMOTICKET_HEC_VERIFY_TLS"), True),
        "enabled": bool(url and env_token),
        "token": env_token,
        "source": "env" if url else "none",
    }


def save_splunk(db, hec_url, index, verify_tls, enabled, token=None):
    """Store the connection. ``token``: None = keep, "" = clear, else replace."""
    with db.transaction():
        put_setting(db, _key("hec_url"), hec_url)
        put_setting(db, _key("index"), index)
        put_setting(db, _key("verify_tls"), "1" if verify_tls else "0")
        put_setting(db, _key("enabled"), "1" if enabled else "0")
        if token is not None:
            put_setting(db, SPLUNK_TOKEN_KEY, token)


def apply_splunk(db, audit, environ=None):
    """Push the effective connection into a logger that supports runtime configuration."""
    if not hasattr(audit, "configure"):
        return False
    cfg = splunk_config(db, environ)
    audit.configure(hec_url=cfg["hec_url"] or None, hec_token=cfg["token"] or None,
                    verify_tls=cfg["verify_tls"], default_index=cfg["index"] or None, enabled=cfg["enabled"])
    return True
