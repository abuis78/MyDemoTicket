"""Test helpers: MCP server subprocess over stdio, real ticket server, header-capturing HTTP stub."""

import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
MCP_DIR = os.path.dirname(TESTS_DIR)
REPO_DIR = os.path.dirname(MCP_DIR)
TICKET_DIR = os.path.join(REPO_DIR, "ticket-system")

ENV_KEYS = ("MYDEMOTICKET_URL", "MYDEMOTICKET_TOKEN", "MYDEMOTICKET_TENANT", "MYDEMOTICKET_VERIFY_TLS",
            "MYDEMOTICKET_TIMEOUT", "MYDEMOTICKET_LOG_LEVEL", "MYDEMOTICKET_KEYCHAIN_ACCOUNT",
            "MYDEMOTICKET_ADMIN_PASSWORD", "MYDEMOTICKET_ADMIN_TOKEN", "MYDEMOTICKET_DATA_DIR")


def clean_env(**overrides):
    env = dict(os.environ)
    for key in ENV_KEYS:
        env.pop(key, None)
    env.update(overrides)
    return env


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class McpProcess(object):
    """The MCP server as a subprocess; one JSON message per line on stdin/stdout."""

    def __init__(self, env=None, argv=None):
        self._env = env if env is not None else clean_env()
        self._env.setdefault("PYTHONPATH", MCP_DIR)
        self._argv = argv or [sys.executable, "-m", "mydemoticket_mcp"]
        self.proc = None
        self._lines = queue.Queue()
        self._next_id = 0
        self.stderr_path = None

    def __repr__(self):  # env may hold a token; never show it
        return "<McpProcess>"

    def start(self):
        fd, self.stderr_path = tempfile.mkstemp(prefix="mcp-stderr-", suffix=".log")
        self._stderr = os.fdopen(fd, "wb")
        self.proc = subprocess.Popen(self._argv, cwd=MCP_DIR, env=self._env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self._stderr)
        threading.Thread(target=self._read, daemon=True).start()
        return self

    def _read(self):
        for line in iter(self.proc.stdout.readline, b""):
            self._lines.put(line)
        self._lines.put(None)

    def send_raw(self, data):
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def send(self, message):
        self.send_raw(json.dumps(message).encode("utf-8") + b"\n")

    def recv(self, timeout=15):
        line = self._lines.get(timeout=timeout)
        if line is None:
            raise EOFError("MCP server closed stdout")
        return json.loads(line.decode("utf-8"))

    def no_message(self, timeout=0.5):
        try:
            self._lines.get(timeout=timeout)
        except queue.Empty:
            return True
        return False

    def request(self, method, params=None):
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        response = self.recv()
        assert response.get("id") == self._next_id, response
        return response

    def initialize(self, version="2025-06-18"):
        response = self.request("initialize", {"protocolVersion": version, "capabilities": {},
                                               "clientInfo": {"name": "unittest", "version": "1"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return response

    def call(self, name, arguments=None):
        params = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        response = self.request("tools/call", params)
        assert "result" in response, response
        return response["result"]

    def stderr_text(self):
        self._stderr.flush()
        with open(self.stderr_path, "rb") as handle:
            return handle.read().decode("utf-8", "replace")

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            self.proc.stdout.close()
            self.proc = None
        if self.stderr_path:
            self._stderr.close()
            os.unlink(self.stderr_path)
            self.stderr_path = None


class TicketServer(object):
    """The real MyDemoTicket server (python3 -m mydemoticket) with a fresh data directory."""

    def __init__(self):
        self.tmpdir = tempfile.mkdtemp(prefix="mdt-mcp-")
        self.data_dir = os.path.join(self.tmpdir, "data")
        self.proc = None
        self.port = None

    def __repr__(self):
        return "<TicketServer port=%s>" % self.port

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def start(self):
        env = clean_env(PYTHONPATH=TICKET_DIR)
        for _attempt in range(3):  # a free port can be taken in between
            self.port = free_port()
            log = open(os.path.join(self.tmpdir, "server.log"), "ab")
            try:
                self.proc = subprocess.Popen(
                    [sys.executable, "-m", "mydemoticket", "--host", "127.0.0.1", "--port", str(self.port),
                     "--data-dir", self.data_dir, "--quiet"],
                    cwd=TICKET_DIR, env=env, stdout=log, stderr=subprocess.STDOUT)
            finally:
                log.close()
            deadline = time.time() + 15
            while time.time() < deadline and self.proc.poll() is None:
                try:
                    with urllib.request.urlopen(self.url + "/api/v1/health", timeout=2) as resp:
                        if json.loads(resp.read().decode("utf-8")).get("status") == "ok":
                            return self
                except OSError:
                    time.sleep(0.1)
            self._stop_proc()
        self.stop()
        raise RuntimeError("ticket server did not become healthy")

    def credentials(self):
        """Parse initial-credentials.txt into {"<user> API token": ..., ...}; values are never printed."""
        creds = {}
        with open(os.path.join(self.data_dir, "initial-credentials.txt"), encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#") and ": " in line:
                    key, _, value = line.partition(": ")
                    creds[key] = value
        return creds

    def _stop_proc(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None

    def stop(self):
        self._stop_proc()
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class CaptureServer(object):
    """HTTP stub on 127.0.0.1 that records method, path and headers and answers with fixed JSON."""

    def __init__(self, status=200, body=None):
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = self.rfile.read(length) if length else b""
                owner.requests.append({"method": self.command, "path": self.path,
                                       "headers": dict((k.lower(), v) for k, v in self.headers.items()),
                                       "body": payload})
                raw = json.dumps(body if body is not None else {"ok": True}).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PATCH = _answer

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
