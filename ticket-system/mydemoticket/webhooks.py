"""Outbound webhooks: generic signed JSON POSTs and native Splunk SOAR delivery.

Deliveries run on a background worker thread; each delivery is attempted up to
three times with exponential backoff and logged in webhook_deliveries.
"""

import hashlib
import hmac
import json
import queue
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid

from . import __version__
from .db import loads, utcnow

EVENTS = ("ticket.created", "ticket.updated", "ticket.state_changed", "ticket.commented")
TYPES = ("generic", "soar")
MAX_ATTEMPTS = 3
PRIORITY_TO_SEVERITY = {1: "high", 2: "high", 3: "medium", 4: "low"}
DELIVERY_LOG_LIMIT = 50


def sign(secret, body):
    """HMAC-SHA256 signature header value for a webhook body (bytes)."""
    digest = hmac.new((secret or "").encode("utf-8"), body, hashlib.sha256).hexdigest()
    return "sha256=" + digest


class DeliveryError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _http_post(url, payload, headers, verify_tls=True, timeout=10):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", "MyDemoTicket/%s" % __version__)
    for key, value in headers.items():
        request.add_header(key, value)
    context = None
    if url.lower().startswith("https://"):
        context = ssl.create_default_context()
        if not verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        try:
            data = exc.read()
        except Exception:  # pragma: no cover - defensive
            data = b""
        finally:
            exc.close()
        return exc.code, data
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DeliveryError("connection error: %s" % getattr(exc, "reason", exc))


def _json_or_empty(data):
    try:
        value = json.loads(data.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


class WebhookDispatcher:
    def __init__(self, db, store_soar_container_id, backoff_seconds=1.0, timeout=10):
        self.db = db
        self.store_soar_container_id = store_soar_container_id
        self.backoff_seconds = backoff_seconds
        self.timeout = timeout
        self.queue = queue.Queue()
        self._thread = None
        self._stopping = threading.Event()

    # -- lifecycle --------------------------------------------------------
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="mdt-webhooks", daemon=True)
            self._thread.start()

    def stop(self):
        self._stopping.set()
        self.queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def wait_idle(self, timeout=10.0):
        """Block until all queued deliveries are processed (used by tests)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.queue.unfinished_tasks == 0:
                return True
            time.sleep(0.02)
        return False

    # -- emit -------------------------------------------------------------
    def emit(self, event, ticket, actor, source, changes=None, comment=None):
        rows = self.db.query("SELECT * FROM webhooks WHERE active = 1")
        now = utcnow()
        for hook in rows:
            events = loads(hook["events"], [])
            if event not in events:
                continue
            delivery_id = str(uuid.uuid4())
            skipped = hook["type"] == "soar" and (source or "").strip().lower() == "soar"
            cur = self.db.execute(
                "INSERT INTO webhook_deliveries(webhook_id, delivery_id, event, ticket_number, status, "
                "attempts, error, created_at, updated_at) VALUES(?, ?, ?, ?, ?, 0, ?, ?, ?)",
                (
                    hook["id"],
                    delivery_id,
                    event,
                    ticket["number"],
                    "skipped" if skipped else "pending",
                    "change originated from SOAR (loop prevention)" if skipped else "",
                    now,
                    now,
                ),
            )
            if skipped:
                continue
            payload = {
                "event": event,
                "delivery_id": delivery_id,
                "timestamp": now,
                "source": source or "",
                "actor": actor,
                "ticket": ticket,
                "changes": changes or {},
                "comment": comment,
            }
            self.queue.put((cur.lastrowid, hook["id"], payload))

    # -- worker -----------------------------------------------------------
    def _run(self):
        while not self._stopping.is_set():
            item = self.queue.get()
            try:
                if item is None:
                    continue
                self._process(*item)
            except Exception:  # never let the worker die
                pass
            finally:
                self.queue.task_done()

    def _update(self, row_id, **fields):
        fields["updated_at"] = utcnow()
        keys = sorted(fields)
        self.db.execute(
            "UPDATE webhook_deliveries SET %s WHERE id = ?" % ", ".join("%s = ?" % k for k in keys),
            tuple(fields[k] for k in keys) + (row_id,),
        )

    def _process(self, row_id, hook_id, payload):
        hook = self.db.query_one("SELECT * FROM webhooks WHERE id = ?", (hook_id,))
        if hook is None:
            return
        last_error, last_status = "", None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                if hook["type"] == "soar":
                    status = self._deliver_soar(hook, payload)
                else:
                    status = self._deliver_generic(hook, payload)
                self._update(row_id, status="success", response_code=status, attempts=attempt, error="")
                return
            except DeliveryError as exc:
                last_error, last_status = str(exc), exc.status
                self._update(
                    row_id, status="retrying" if attempt < MAX_ATTEMPTS else "failed",
                    response_code=last_status, attempts=attempt, error=last_error[:500],
                )
            if attempt < MAX_ATTEMPTS and not self._stopping.is_set():
                time.sleep(self.backoff_seconds * (2 ** (attempt - 1)))

    def _deliver_generic(self, hook, payload):
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        headers = {
            "X-MyDemoTicket-Event": payload["event"],
            "X-MyDemoTicket-Delivery": payload["delivery_id"],
            "X-MyDemoTicket-Signature": sign(hook["secret"], body),
        }
        status, _ = _http_post(hook["url"], body, headers, bool(hook["verify_tls"]), self.timeout)
        if not 200 <= status < 300:
            raise DeliveryError("receiver answered HTTP %d" % status, status)
        return status

    def _deliver_soar(self, hook, payload):
        ticket = payload["ticket"]
        base = hook["url"].rstrip("/")
        headers = {"ph-auth-token": hook["secret"]}
        verify = bool(hook["verify_tls"])
        container = {
            "name": "%s: %s" % (ticket["number"], ticket["short_description"]),
            "label": hook["soar_label"] or "mydemoticket",
            "source_data_identifier": ticket["number"],
            "severity": PRIORITY_TO_SEVERITY.get(ticket["priority"], "medium"),
            "description": ticket.get("description") or "",
        }
        status, data = _http_post(base + "/rest/container", container, headers, verify, self.timeout)
        answer = _json_or_empty(data)
        container_id = answer.get("id") or answer.get("existing_container_id")
        if not container_id:
            raise DeliveryError("SOAR container request failed (HTTP %d)" % status, status)
        container_id = int(container_id)
        if ticket.get("external_refs", {}).get("soar_container_id") != container_id:
            self.store_soar_container_id(ticket["number"], container_id)

        refs = ticket.get("external_refs") or {}
        comment = payload.get("comment") or {}
        artifact = {
            "container_id": container_id,
            "name": payload["event"],
            "label": "mydemoticket_event",
            "source_data_identifier": payload["delivery_id"],
            "cef": {
                "ticketNumber": ticket["number"],
                "ticketState": ticket["state"],
                "ticketPriority": ticket["priority"],
                "assignmentGroup": ticket.get("assignment_group") or "",
                "assignedTo": ticket.get("assigned_to") or "",
                "event": payload["event"],
                "changes": json.dumps(payload.get("changes") or {}, sort_keys=True),
                "comment": comment.get("body") or "",
                "customFields": json.dumps(ticket.get("custom_fields") or {}, sort_keys=True),
                "esFindingId": str(refs.get("es_finding_id") or ""),
                "source": payload.get("source") or "",
            },
            "run_automation": True,
        }
        status, data = _http_post(base + "/rest/artifact", artifact, headers, verify, self.timeout)
        answer = _json_or_empty(data)
        if 200 <= status < 300 or answer.get("existing_artifact_id"):
            return status
        raise DeliveryError("SOAR artifact request failed (HTTP %d)" % status, status)

    # -- log --------------------------------------------------------------
    def deliveries(self, hook_id):
        rows = self.db.query(
            "SELECT * FROM webhook_deliveries WHERE webhook_id = ? ORDER BY id DESC LIMIT ?",
            (hook_id, DELIVERY_LOG_LIMIT),
        )
        return [
            {
                "id": r["id"],
                "delivery_id": r["delivery_id"],
                "event": r["event"],
                "ticket_number": r["ticket_number"],
                "status": r["status"],
                "response_code": r["response_code"],
                "attempts": r["attempts"],
                "error": r["error"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
