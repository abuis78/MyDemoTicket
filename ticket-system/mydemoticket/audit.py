"""Audit logging: local JSON-lines file plus optional Splunk HTTP Event Collector (HEC).

Design goals:
- ``AuditLogger.log()`` never blocks (it only appends to a bounded in-memory queue),
  is thread-safe and never raises.
- Every event is always written to ``<data_dir>/logs/audit.log`` (rotated, 0600).
- If a HEC URL and token are configured, a background thread ships events in batches
  (up to 50 events or 2 s) with exponential backoff on transient failures.
- Each HEC event carries the tenant as indexed field and can be routed to a
  per-tenant index via ``index_resolver(tenant_slug)``.
- ``configure()`` changes the HEC connection at runtime (Audit / Splunk page);
  ``send_test()`` sends one event synchronously and reports the result.

Pure standard library, Python 3.9 compatible.
"""

import collections
import datetime
import json
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__

__all__ = ["AuditLogger", "redact", "OUTCOMES"]

OUTCOMES = ("success", "failure", "denied", "skipped", "error")
REDACTED = "[REDACTED]"
SENSITIVE_KEY_PARTS = ("password", "passwd", "secret", "token", "authorization", "api_key", "cookie", "session")
MAX_STRING = 4096
MAX_DEPTH = 20

MAX_QUEUE = 10000
BATCH_SIZE = 50
FLUSH_INTERVAL = 2.0
BACKOFF_MAX = 60.0
STOP_FLUSH_TIMEOUT = 5.0
ROTATE_BYTES = 10 * 1024 * 1024
ROTATE_KEEP = 5
HTTP_TIMEOUT = 10.0
TEST_TIMEOUT = 5.0


def _truncate(value):
    if len(value) > MAX_STRING:
        return value[:MAX_STRING] + "...[truncated]"
    return value


def _is_sensitive(key):
    k = str(key).lower()
    return any(part in k for part in SENSITIVE_KEY_PARTS)


def redact(value, _depth=0):
    """Return a JSON-safe copy of ``value`` with sensitive keys masked and long strings cut."""
    if _depth > MAX_DEPTH:
        return "[MAX_DEPTH]"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = str(k)
            out[key] = REDACTED if _is_sensitive(key) else redact(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [redact(v, _depth + 1) for v in value]
    if isinstance(value, str):
        return _truncate(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _truncate(str(value))


def _iso(epoch):
    dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


def _hec_endpoint(url):
    url = url.strip()
    if "/services/collector" in url:
        return url
    return url.rstrip("/") + "/services/collector/event"


def _env_bool(value, default=True):
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


class _RetryableError(Exception):
    pass


class _PermanentError(Exception):
    pass


class AuditLogger:
    """Thread-safe, non-blocking audit logger (local file + optional Splunk HEC)."""

    def __init__(self, data_dir, hec_url=None, hec_token=None, verify_tls=True, default_index=None,
                 sourcetype="mydemoticket:audit", index_resolver=None, _backoff_base=1.0,
                 _flush_interval=FLUSH_INTERVAL, _batch_size=BATCH_SIZE, _max_queue=MAX_QUEUE,
                 _rotate_bytes=ROTATE_BYTES, _rotate_keep=ROTATE_KEEP, _stop_timeout=STOP_FLUSH_TIMEOUT):
        self.data_dir = data_dir
        self.log_dir = os.path.join(data_dir, "logs")
        self.log_path = os.path.join(self.log_dir, "audit.log")
        self.hec_url = _hec_endpoint(hec_url) if hec_url else None
        self._token = hec_token or None
        self._old_tokens = set()           # replaced tokens, still masked in error messages
        self.hec_enabled = bool(self.hec_url and self._token)
        self.verify_tls = bool(verify_tls)
        self.default_index = default_index or None
        self.sourcetype = sourcetype
        self.index_resolver = index_resolver
        self._backoff_base = float(_backoff_base)
        self._flush_interval = float(_flush_interval)
        self._batch_size = int(_batch_size)
        self._max_queue = int(_max_queue)
        self._rotate_bytes = int(_rotate_bytes)
        self._rotate_keep = int(_rotate_keep)
        self._stop_timeout = float(_stop_timeout)
        self._host = socket.gethostname()

        self._cond = threading.Condition()
        self._inq = collections.deque()    # events waiting for the local writer
        self._hecq = collections.deque()   # events waiting for HEC
        self._writing = 0                  # events taken by the writer, not yet written
        self._inflight = 0                 # events in the batch currently being sent
        self._stopping = False
        self._running = False
        self._wake = threading.Event()     # interrupts backoff sleeps on stop
        self._writer = None
        self._sender = None
        self._stats = {"sent": 0, "failed": 0, "dropped": 0, "local_written": 0,
                       "last_error": None, "last_success_at": None}

    def __repr__(self):
        return "AuditLogger(data_dir=%r, hec_enabled=%r)" % (self.data_dir, self.hec_enabled)

    # ------------------------------------------------------------------ config

    @classmethod
    def from_env(cls, data_dir, index_resolver=None):
        """Build a logger from MYDEMOTICKET_HEC_URL/_TOKEN/_VERIFY_TLS/_INDEX."""
        return cls(
            data_dir,
            hec_url=os.environ.get("MYDEMOTICKET_HEC_URL") or None,
            hec_token=os.environ.get("MYDEMOTICKET_HEC_TOKEN") or None,
            verify_tls=_env_bool(os.environ.get("MYDEMOTICKET_HEC_VERIFY_TLS"), True),
            default_index=os.environ.get("MYDEMOTICKET_HEC_INDEX") or None,
            index_resolver=index_resolver,
        )

    def configure(self, hec_url=None, hec_token=None, verify_tls=True, default_index=None, enabled=True):
        """Change the HEC connection of a running (or stopped) logger. Thread-safe, never raises.

        Events already waiting for HEC are sent with the new connection; when HEC
        gets disabled they are discarded from the HEC queue (they stay in the local file).
        """
        start_sender = False
        with self._cond:
            if self._token and self._token != (hec_token or None):
                self._old_tokens.add(self._token)
            self.hec_url = _hec_endpoint(hec_url) if hec_url else None
            self._token = hec_token or None
            self.verify_tls = bool(verify_tls)
            self.default_index = default_index or None
            self.hec_enabled = bool(enabled and self.hec_url and self._token)
            if not self.hec_enabled:
                self._hecq.clear()
            if self.hec_enabled and self._running and not self._stopping and (
                    self._sender is None or not self._sender.is_alive()):
                start_sender = True
            self._cond.notify_all()
        if start_sender:
            sender = threading.Thread(target=self._sender_loop, name="audit-hec", daemon=True)
            with self._cond:
                self._sender = sender
            sender.start()

    def connection(self):
        """Current HEC connection (without the token)."""
        with self._cond:
            return {"hec_url": self.hec_url, "verify_tls": self.verify_tls, "index": self.default_index,
                    "enabled": self.hec_enabled, "token_set": bool(self._token)}

    def send_test(self, tenant=None, actor=None, role=None, source=None, ip=None, request_id=None,
                  timeout=TEST_TIMEOUT):
        """Send one ``splunk.test`` event synchronously. Returns (ok, message); never raises.

        Works with a configured URL and token even while HEC shipping is disabled.
        """
        with self._cond:
            url, token, verify = self.hec_url, self._token, self.verify_tls
        if not url or not token:
            return False, "Splunk HEC is not configured (URL and token are required)"
        try:
            now = time.time()
            event = self._build_event(now, "splunk.test", tenant=tenant, actor=actor, role=role,
                                      outcome="success", object_type="settings", object_id="splunk",
                                      source=source, ip=ip, request_id=request_id,
                                      details={"message": "MyDemoTicket connection test"})
            body = json.dumps(self._hec_event(now, event), ensure_ascii=False, default=str).encode("utf-8")
            self._post(body, url=url, token=token, verify_tls=verify, timeout=timeout)
        except (_RetryableError, _PermanentError) as exc:
            return False, self._sanitize(str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            return False, "HEC error: %s" % exc.__class__.__name__
        return True, "test event accepted by Splunk HEC (%s)" % urllib.parse.urlsplit(url).netloc

    # --------------------------------------------------------------- lifecycle

    def start(self):
        with self._cond:
            if self._running:
                return
            self._running = True
            self._stopping = False
            self._wake.clear()
        self._writer = threading.Thread(target=self._writer_loop, name="audit-writer", daemon=True)
        self._writer.start()
        if self.hec_enabled:
            self._sender = threading.Thread(target=self._sender_loop, name="audit-hec", daemon=True)
            self._sender.start()

    def stop(self):
        """Stop the background threads, trying up to 5 s to drain the queues."""
        with self._cond:
            if not self._running:
                return
        self.wait_idle(self._stop_timeout)
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        self._wake.set()
        for t in (self._writer, self._sender):
            if t is not None:
                t.join(2.0)
        with self._cond:
            self._running = False
        self._writer = self._sender = None

    def wait_idle(self, timeout=10.0):
        """Block until all queued events are written (and sent, if HEC is on). Returns True if idle."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._idle_locked():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._running:
                    return self._idle_locked()
                self._cond.wait(min(remaining, 0.05))
            return True

    def _idle_locked(self):
        return not self._inq and not self._writing and not self._hecq and not self._inflight

    # ----------------------------------------------------------------- logging

    def log(self, action, *, tenant, actor=None, role=None, outcome="success", object_type=None,
            object_id=None, source=None, ip=None, request_id=None, details=None):
        """Queue an audit event. Never blocks, never raises."""
        try:
            now = time.time()
            event = self._build_event(now, action, tenant=tenant, actor=actor, role=role, outcome=outcome,
                                      object_type=object_type, object_id=object_id, source=source, ip=ip,
                                      request_id=request_id, details=details)
            with self._cond:
                self._push_locked(self._inq, (now, event))
                self._cond.notify_all()
        except Exception:  # pragma: no cover - defensive, log() must never raise
            try:
                with self._cond:
                    self._stats["dropped"] += 1
            except Exception:
                pass

    @staticmethod
    def _build_event(now, action, *, tenant, actor=None, role=None, outcome="success", object_type=None,
                     object_id=None, source=None, ip=None, request_id=None, details=None):
        event = {
            "ts": _iso(now),
            "tenant": None if tenant is None else str(tenant),
            "action": str(action),
            "actor": None if actor is None else str(actor),
            "role": None if role is None else str(role),
            "outcome": str(outcome) if outcome else "success",
            "object_type": None if object_type is None else str(object_type),
            "object_id": None if object_id is None else redact(object_id),
            "source": None if source is None else str(source),
            "ip": None if ip is None else str(ip),
            "request_id": None if request_id is None else str(request_id),
            "details": redact(details) if isinstance(details, dict) else ({} if details is None
                                                                         else {"value": redact(details)}),
            "app": "mydemoticket",
            "version": __version__,
        }
        for key in ("tenant", "action", "actor", "role", "outcome", "object_type", "source", "ip",
                    "request_id"):
            if isinstance(event[key], str):
                event[key] = _truncate(event[key])
        return event

    def _push_locked(self, queue, item):
        if len(queue) >= self._max_queue:
            queue.popleft()
            self._stats["dropped"] += 1
        queue.append(item)

    def stats(self):
        with self._cond:
            s = dict(self._stats)
            s["hec_enabled"] = self.hec_enabled
            s["queued"] = len(self._inq) + self._writing + len(self._hecq) + self._inflight
            return s

    # ------------------------------------------------------------ local writer

    def _writer_loop(self):
        while True:
            with self._cond:
                while not self._inq and not self._stopping:
                    self._cond.wait(0.5)
                if not self._inq and self._stopping:
                    return
                items = list(self._inq)
                self._inq.clear()
                self._writing = len(items)
            try:
                self._write_local(items)
            except Exception as exc:
                self._set_error("local write failed: %s" % exc.__class__.__name__)
            with self._cond:
                self._writing = 0
                if self.hec_enabled:
                    for item in items:
                        self._push_locked(self._hecq, item)
                self._cond.notify_all()

    def _ensure_dir(self):
        if not os.path.isdir(self.log_dir):
            os.makedirs(self.log_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.log_dir, 0o700)
        except OSError:
            pass

    def _rotate(self):
        for i in range(self._rotate_keep - 1, 0, -1):
            src = "%s.%d" % (self.log_path, i)
            if os.path.exists(src):
                os.replace(src, "%s.%d" % (self.log_path, i + 1))
        os.replace(self.log_path, self.log_path + ".1")

    def _write_local(self, items):
        self._ensure_dir()
        for _, event in items:
            line = (json.dumps(event, ensure_ascii=False, sort_keys=False, default=str) + "\n").encode("utf-8")
            try:
                size = os.path.getsize(self.log_path)
            except OSError:
                size = 0
            if size and size + len(line) > self._rotate_bytes:
                self._rotate()
            fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, line)
            finally:
                os.close(fd)
            with self._cond:
                self._stats["local_written"] += 1

    # -------------------------------------------------------------- HEC sender

    def _resolve_index(self, tenant):
        if self.index_resolver is not None and tenant is not None:
            try:
                idx = self.index_resolver(tenant)
                if idx:
                    return str(idx)
            except Exception:
                pass
        return self.default_index

    def _hec_event(self, epoch, event):
        """Build the HEC envelope for one audit event."""
        out = {
            "time": round(epoch, 3),
            "host": self._host,
            "source": "mydemoticket",
            "sourcetype": self.sourcetype,
        }
        index = self._resolve_index(event.get("tenant"))
        if index:
            out["index"] = index
        # tenant travels only as indexed field; sending it in the event body as well
        # would make Splunk (KV_MODE=json) report a multivalue tenant field.
        out["event"] = {k: v for k, v in event.items() if k != "tenant"}
        out["fields"] = {"tenant": event.get("tenant") or "unknown"}
        return out

    def _sender_loop(self):
        while True:
            with self._cond:
                while not self._hecq and not self._stopping:
                    self._cond.wait(0.5)
                if not self._hecq and self._stopping:
                    return
                # wait until the batch is full or the flush interval elapsed
                deadline = time.monotonic() + self._flush_interval
                while len(self._hecq) < self._batch_size and not self._stopping:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(remaining)
                batch = [self._hecq.popleft() for _ in range(min(self._batch_size, len(self._hecq)))]
                self._inflight = len(batch)
            try:
                self._send_with_retry(batch)
            finally:
                with self._cond:
                    self._inflight = 0
                    self._cond.notify_all()

    def _send_with_retry(self, batch):
        body = "\n".join(json.dumps(self._hec_event(ts, ev), ensure_ascii=False, default=str)
                         for ts, ev in batch).encode("utf-8")
        attempt = 0
        while True:
            try:
                self._post(body)
                with self._cond:
                    self._stats["sent"] += len(batch)
                    self._stats["last_success_at"] = _iso(time.time())
                return
            except _PermanentError as exc:
                self._set_error(str(exc))
                with self._cond:
                    self._stats["failed"] += len(batch)
                return
            except _RetryableError as exc:
                self._set_error(str(exc))
            except Exception as exc:  # unexpected - treat as transient
                self._set_error("HEC error: %s" % exc.__class__.__name__)
            with self._cond:
                stopping = self._stopping
            if stopping:
                with self._cond:
                    self._stats["failed"] += len(batch)
                return
            delay = min(self._backoff_base * (2 ** attempt), BACKOFF_MAX)
            attempt += 1
            if self._wake.wait(delay):
                # stop requested during backoff: one last attempt, then give up
                with self._cond:
                    self._stopping = True

    def _post(self, body, url=None, token=None, verify_tls=None, timeout=HTTP_TIMEOUT):
        if url is None:
            with self._cond:  # snapshot: configure() may change the connection at any time
                url, token, verify_tls, enabled = self.hec_url, self._token, self.verify_tls, self.hec_enabled
            if not enabled:
                raise _PermanentError("HEC disabled")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Authorization", "Splunk " + token)
        req.add_header("Content-Type", "application/json")
        ctx = None
        if url.lower().startswith("https"):
            ctx = ssl.create_default_context()
            if not verify_tls:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(512).decode("utf-8", "replace").strip()
            except Exception:
                pass
            finally:
                exc.close()
            msg = "HEC HTTP %d%s" % (exc.code, (": " + detail) if detail else "")
            if exc.code == 429 or exc.code >= 500:
                raise _RetryableError(msg)
            raise _PermanentError(msg)
        except (urllib.error.URLError, OSError, socket.timeout) as exc:
            reason = getattr(exc, "reason", exc)
            raise _RetryableError("HEC network error: %s" % reason)

    def _sanitize(self, message):
        msg = str(message)
        with self._cond:
            tokens = [t for t in [self._token] + list(self._old_tokens) if t]
        for token in tokens:
            msg = msg.replace(token, "***")
        return _truncate(msg)[:512]

    def _set_error(self, message):
        msg = self._sanitize(message)
        with self._cond:
            self._stats["last_error"] = msg
