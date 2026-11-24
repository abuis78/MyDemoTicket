"""Local stand-ins for the systems MyDemoTicket pushes to (test-only, stdlib, 127.0.0.1).

* ``SoarMock`` emulates the part of the Splunk SOAR REST API the ticket system uses:
  ``POST /rest/container`` (duplicate detection on ``source_data_identifier`` like real
  SOAR: HTTP 400 with ``existing_container_id``) and ``POST /rest/artifact``. Every request
  must carry the expected ``ph-auth-token`` header, otherwise it is answered with 401.
* ``GenericReceiver`` records generic webhooks and verifies their HMAC-SHA256 signature.
"""

import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _JsonServer(object):
    """Small threaded HTTP server on 127.0.0.1:0 dispatching POSTs to ``handle_post``."""

    def __init__(self):
        self.lock = threading.Lock()
        owner = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output clean
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length)
                headers = {k.lower(): v for k, v in self.headers.items()}
                with owner.lock:
                    status, payload = owner.handle_post(self.path, headers, raw)
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def handle_post(self, path, headers, raw):  # pragma: no cover - overridden
        raise NotImplementedError

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class SoarMock(_JsonServer):
    def __init__(self, auth_token):
        self.auth_token = auth_token
        self.containers = []          # created containers (dicts incl. "id")
        self.duplicate_container_posts = 0
        self.artifacts = []           # created artifacts (dicts incl. "id")
        self.auth_failures = 0
        self.bad_requests = []
        self._next_container_id = 100
        self._next_artifact_id = 9000
        super(SoarMock, self).__init__()

    # -- inspection helpers (copies, safe to use from the test thread) ----------
    def snapshot(self):
        with self.lock:
            return ([dict(c) for c in self.containers], [dict(a) for a in self.artifacts])

    def containers_for(self, number):
        containers, _ = self.snapshot()
        return [c for c in containers if c.get("source_data_identifier") == number]

    def artifacts_for(self, number):
        _, artifacts = self.snapshot()
        return [a for a in artifacts if (a.get("cef") or {}).get("ticketNumber") == number]

    # -- request handling (runs under self.lock) --------------------------------
    def handle_post(self, path, headers, raw):
        if headers.get("ph-auth-token") != self.auth_token:
            self.auth_failures += 1
            return 401, {"failed": True, "message": "Authentication required"}
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self.bad_requests.append((path, "invalid json"))
            return 400, {"failed": True, "message": "invalid json"}
        if path.rstrip("/") == "/rest/container":
            return self._container(body)
        if path.rstrip("/") == "/rest/artifact":
            return self._artifact(body)
        self.bad_requests.append((path, "unknown endpoint"))
        return 404, {"failed": True, "message": "not found"}

    def _container(self, body):
        sdi = body.get("source_data_identifier")
        if not sdi or not body.get("name") or not body.get("label"):
            self.bad_requests.append(("/rest/container", "missing name/label/sdi"))
            return 400, {"failed": True, "message": "name, label and source_data_identifier required"}
        for existing in self.containers:
            if existing["source_data_identifier"] == sdi and existing["label"] == body["label"]:
                self.duplicate_container_posts += 1
                return 400, {"failed": True, "message": "duplicate", "existing_container_id": existing["id"]}
        stored = dict(body)
        stored["id"] = self._next_container_id
        self._next_container_id += 1
        self.containers.append(stored)
        return 200, {"success": True, "id": stored["id"], "new_artifacts": []}

    def _artifact(self, body):
        cid = body.get("container_id")
        if not any(c["id"] == cid for c in self.containers):
            self.bad_requests.append(("/rest/artifact", "unknown container %r" % (cid,)))
            return 404, {"failed": True, "message": "container not found"}
        sdi = body.get("source_data_identifier")
        for existing in self.artifacts:
            if existing["container_id"] == cid and sdi and existing.get("source_data_identifier") == sdi:
                return 400, {"failed": True, "message": "duplicate", "existing_artifact_id": existing["id"]}
        stored = dict(body)
        stored["id"] = self._next_artifact_id
        self._next_artifact_id += 1
        self.artifacts.append(stored)
        return 200, {"success": True, "id": stored["id"]}


class GenericReceiver(_JsonServer):
    """Records generic webhooks; ``signature_ok`` is computed over the raw body bytes."""

    def __init__(self, secret):
        self.secret = secret
        self.deliveries = []
        super(GenericReceiver, self).__init__()

    def handle_post(self, path, headers, raw):
        expected = "sha256=" + hmac.new(self.secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        signature_ok = hmac.compare_digest(expected, headers.get("x-mydemoticket-signature", ""))
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = None
        self.deliveries.append({
            "path": path,
            "event_header": headers.get("x-mydemoticket-event"),
            "delivery_header": headers.get("x-mydemoticket-delivery"),
            "signature_ok": signature_ok,
            "payload": payload,
        })
        if not signature_ok:
            return 401, {"error": "bad signature"}
        return 200, {"ok": True}

    def for_ticket(self, number):
        with self.lock:
            return [dict(d) for d in self.deliveries
                    if d["payload"] and (d["payload"].get("ticket") or {}).get("number") == number]
