"""Shared test helpers: in-process server on a random port and HTTP clients."""

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mydemoticket.server import App  # noqa: E402

ADMIN_PASSWORD = "test-admin-password"
ADMIN_TOKEN = "test-admin-token"


class Client:
    def __init__(self, port, token=None):
        self.port = port
        self.token = token
        self.cookies = {}
        self.csrf = None

    def request(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = {"Accept": "application/json"}
        if self.token:
            hdrs["Authorization"] = "Bearer " + self.token
        if self.cookies:
            hdrs["Cookie"] = "; ".join("%s=%s" % kv for kv in self.cookies.items())
        if self.csrf and method not in ("GET", "HEAD"):
            hdrs["X-CSRF-Token"] = self.csrf
        data = raw
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        raw_path = path == "/" or path.startswith("/api/") or path.startswith("/static/")
        url = path if raw_path else "/api/v1" + path
        conn.request(method, url, body=data, headers=hdrs)
        resp = conn.getresponse()
        payload = resp.read()
        for header, value in resp.getheaders():
            if header.lower() == "set-cookie":
                name, _, rest = value.partition("=")
                cookie_value = rest.split(";", 1)[0]
                if cookie_value:
                    self.cookies[name] = cookie_value
                else:
                    self.cookies.pop(name, None)
        conn.close()
        content_type = resp.getheader("Content-Type", "")
        parsed = None
        if payload and "json" in content_type:
            parsed = json.loads(payload.decode("utf-8"))
        return Response(resp.status, parsed, payload, dict((k.lower(), v) for k, v in resp.getheaders()))

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body if body is not None else {}, **kw)

    def patch(self, path, body=None, **kw):
        return self.request("PATCH", path, body if body is not None else {}, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)


class Response:
    def __init__(self, status, json_body, raw, headers):
        self.status = status
        self.json = json_body
        self.raw = raw
        self.headers = headers

    def __repr__(self):
        return "<Response %s %r>" % (self.status, self.json)


class ServerTestCase(unittest.TestCase):
    """Starts one server per test class on port 0 with a temporary data dir."""

    seed = True

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="mdt-test-")
        cls.app = App(
            os.path.join(cls.tmpdir, "data"), seed=cls.seed, admin_password=ADMIN_PASSWORD,
            admin_token=ADMIN_TOKEN, webhook_backoff=0.02, quiet=True,
        )
        cls.httpd = cls.app.make_server("127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.admin = Client(cls.port, ADMIN_TOKEN)
        cls._tokens = {}

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    @classmethod
    def client_for(cls, username):
        """API client authenticated with a fresh token for a seed user."""
        if username not in cls._tokens:
            resp = cls.admin.post("/users/%s/tokens" % username, {"name": "test"})
            assert resp.status == 201, resp
            cls._tokens[username] = resp.json["token"]
        return Client(cls.port, cls._tokens[username])

    def create_ticket(self, client=None, **fields):
        body = {"short_description": "Suspicious login on demo-host-01"}
        body.update(fields)
        resp = (client or self.admin).post("/tickets", body)
        self.assertEqual(resp.status, 201, resp)
        return resp.json

    def wait_webhooks(self):
        self.assertTrue(self.app.dispatcher.wait_idle(10), "webhook queue did not drain")


class Receiver:
    """Local HTTP receiver recording requests; optional custom responder."""

    def __init__(self, responder=None):
        self.requests = []
        self.responder = responder
        self.lock = threading.Lock()
        receiver = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                with receiver.lock:
                    receiver.requests.append(
                        {"path": self.path, "body": body,
                         "headers": {k.lower(): v for k, v in self.headers.items()}}
                    )
                status, payload = (200, {"ok": True})
                if receiver.responder:
                    status, payload = receiver.responder(self.path, json.loads(body or b"{}"))
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
