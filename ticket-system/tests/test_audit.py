"""Audit logger: local file log, redaction, HEC format/batching/retry against a local mock."""

import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mydemoticket import __version__  # noqa: E402
from mydemoticket.audit import AuditLogger, redact  # noqa: E402

TOKEN = "11111111-2222-3333-4444-555555555555"


class HecMock:
    """Minimal HEC endpoint. ``statuses`` is consumed per request; afterwards 200."""

    def __init__(self, statuses=None):
        self.requests = []
        self.statuses = list(statuses or [])
        self.lock = threading.Lock()
        mock_self = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8")
                with mock_self.lock:
                    status = mock_self.statuses.pop(0) if mock_self.statuses else 200
                    mock_self.requests.append({"path": self.path, "headers": dict(self.headers),
                                               "body": body, "status": status})
                payload = b'{"text":"Success","code":0}' if status == 200 else b'{"text":"nope","code":9}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def events(self, only_ok=True):
        out = []
        with self.lock:
            reqs = list(self.requests)
        for r in reqs:
            if only_ok and r["status"] != 200:
                continue
            for line in r["body"].splitlines():
                if line.strip():
                    out.append(json.loads(line))
        return out

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mdt-audit-")
        self.loggers = []
        self.mocks = []

    def tearDown(self):
        for lg in self.loggers:
            lg.stop()
        for m in self.mocks:
            m.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def make(self, **kw):
        kw.setdefault("_backoff_base", 0.01)
        kw.setdefault("_flush_interval", 0.1)
        lg = AuditLogger(self.dir, **kw)
        self.loggers.append(lg)
        return lg

    def hec(self, statuses=None):
        m = HecMock(statuses)
        self.mocks.append(m)
        return m

    def read_log(self):
        with open(os.path.join(self.dir, "logs", "audit.log"), encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


class LocalFileTest(BaseCase):
    def test_writes_json_lines_with_schema(self):
        lg = self.make()
        lg.start()
        lg.log("ticket.create", tenant="soc-games", actor="alice", role="agent", object_type="ticket",
               object_id="INC0001001", source="api", ip="127.0.0.1", request_id="r-1",
               details={"short_description": "Phishing"})
        self.assertTrue(lg.wait_idle(5))
        events = self.read_log()
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(set(ev), {"ts", "tenant", "action", "actor", "role", "outcome", "object_type",
                                   "object_id", "source", "ip", "request_id", "details", "app", "version"})
        self.assertEqual(ev["tenant"], "soc-games")
        self.assertEqual(ev["action"], "ticket.create")
        self.assertEqual(ev["outcome"], "success")
        self.assertEqual(ev["app"], "mydemoticket")
        self.assertEqual(ev["version"], __version__)
        self.assertRegex(ev["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual(ev["details"], {"short_description": "Phishing"})
        self.assertEqual(lg.stats()["local_written"], 1)
        self.assertFalse(lg.stats()["hec_enabled"])

    def test_permissions(self):
        lg = self.make()
        lg.start()
        lg.log("auth.login", tenant="t")
        self.assertTrue(lg.wait_idle(5))
        logs = os.path.join(self.dir, "logs")
        self.assertEqual(stat.S_IMODE(os.stat(logs).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(logs, "audit.log")).st_mode), 0o600)

    def test_rotation_keeps_five_generations(self):
        lg = self.make(_rotate_bytes=600)
        lg.start()
        for i in range(80):
            lg.log("ticket.update", tenant="t", object_id=str(i), details={"pad": "x" * 100})
        self.assertTrue(lg.wait_idle(5))
        logs = os.path.join(self.dir, "logs")
        for i in range(1, 6):
            path = os.path.join(logs, "audit.log.%d" % i)
            self.assertTrue(os.path.exists(path), path)
            self.assertLessEqual(os.path.getsize(path), 600)
        self.assertFalse(os.path.exists(os.path.join(logs, "audit.log.6")))
        self.assertLessEqual(os.path.getsize(os.path.join(logs, "audit.log")), 600)
        # newest event is in the active file
        self.assertEqual(self.read_log()[-1]["object_id"], "79")


class RedactionTest(BaseCase):
    def test_nested_redaction_and_truncation(self):
        data = {
            "Password": "p", "new_passwd": "p", "client_secret": "s", "API_KEY": "k", "Cookie": "c",
            "session_id": "s", "nested": {"Authorization": "Bearer x", "ok": "keep",
                                          "list": [{"access_token": "t", "name": "n"}]},
            "long": "a" * 5000,
        }
        out = redact(data)
        for key in ("Password", "new_passwd", "client_secret", "API_KEY", "Cookie", "session_id"):
            self.assertEqual(out[key], "[REDACTED]", key)
        self.assertEqual(out["nested"]["Authorization"], "[REDACTED]")
        self.assertEqual(out["nested"]["ok"], "keep")
        self.assertEqual(out["nested"]["list"][0], {"access_token": "[REDACTED]", "name": "n"})
        self.assertTrue(out["long"].startswith("a" * 4096))
        self.assertLess(len(out["long"]), 4200)
        self.assertEqual(data["Password"], "p")  # input untouched

    def test_redaction_applied_to_logged_event(self):
        lg = self.make()
        lg.start()
        lg.log("user.update", tenant="t", details={"user": {"password": "hunter2"}})
        self.assertTrue(lg.wait_idle(5))
        self.assertEqual(self.read_log()[0]["details"], {"user": {"password": "[REDACTED]"}})
        with open(os.path.join(self.dir, "logs", "audit.log"), encoding="utf-8") as fh:
            self.assertNotIn("hunter2", fh.read())


class HecTest(BaseCase):
    def test_event_format_and_endpoint(self):
        m = self.hec()
        lg = self.make(hec_url=m.url + "/", hec_token=TOKEN, sourcetype="mydemoticket:audit")
        lg.start()
        lg.log("ticket.state_change", tenant="soc-games", actor="bob", object_type="ticket",
               object_id="INC1", details={"from": "new", "to": "resolved"})
        self.assertTrue(lg.wait_idle(5))
        self.assertEqual(len(m.requests), 1)
        req = m.requests[0]
        self.assertEqual(req["path"], "/services/collector/event")
        self.assertEqual(req["headers"].get("Authorization"), "Splunk " + TOKEN)
        ev = m.events()[0]
        self.assertIsInstance(ev["time"], float)
        self.assertEqual(ev["host"], socket.gethostname())
        self.assertEqual(ev["source"], "mydemoticket")
        self.assertEqual(ev["sourcetype"], "mydemoticket:audit")
        self.assertNotIn("index", ev)
        self.assertEqual(ev["fields"], {"tenant": "soc-games"})
        self.assertEqual(ev["event"]["action"], "ticket.state_change")
        # tenant only as indexed field, not in the event body (avoids multivalue in Splunk)
        self.assertNotIn("tenant", ev["event"])
        # the local file keeps tenant
        self.assertEqual(self.read_log()[0]["tenant"], "soc-games")
        self.assertEqual(ev["event"]["details"], {"from": "new", "to": "resolved"})
        st = lg.stats()
        self.assertTrue(st["hec_enabled"])
        self.assertEqual(st["sent"], 1)
        self.assertEqual(st["queued"], 0)
        self.assertIsNotNone(st["last_success_at"])

    def test_collector_url_kept_unchanged(self):
        m = self.hec()
        lg = self.make(hec_url=m.url + "/services/collector", hec_token=TOKEN)
        lg.start()
        lg.log("auth.login", tenant="t")
        self.assertTrue(lg.wait_idle(5))
        self.assertEqual(m.requests[0]["path"], "/services/collector")

    def test_index_resolution(self):
        m = self.hec()
        indexes = {"soc-games": "mdt_soc_games", "usa-demo": "mdt_usa_demo"}
        lg = self.make(hec_url=m.url, hec_token=TOKEN, default_index="mdt_default",
                       index_resolver=indexes.get)
        lg.start()
        lg.log("auth.login", tenant="soc-games")
        lg.log("auth.login", tenant="usa-demo")
        lg.log("auth.login", tenant="other")
        self.assertTrue(lg.wait_idle(5))
        got = {e["fields"]["tenant"]: e.get("index") for e in m.events()}
        self.assertEqual(got, {"soc-games": "mdt_soc_games", "usa-demo": "mdt_usa_demo",
                               "other": "mdt_default"})

    def test_index_resolver_error_falls_back(self):
        m = self.hec()

        def boom(_slug):
            raise RuntimeError("db down")

        lg = self.make(hec_url=m.url, hec_token=TOKEN, index_resolver=boom)
        lg.start()
        lg.log("auth.login", tenant="t")
        self.assertTrue(lg.wait_idle(5))
        self.assertNotIn("index", m.events()[0])

    def test_batching(self):
        m = self.hec()
        lg = self.make(hec_url=m.url, hec_token=TOKEN, _flush_interval=0.5)
        for i in range(120):
            lg.log("ticket.update", tenant="t", object_id=str(i))
        lg.start()
        self.assertTrue(lg.wait_idle(10))
        sizes = [len(r["body"].splitlines()) for r in m.requests]
        self.assertEqual(sum(sizes), 120)
        self.assertEqual(sizes[:2], [50, 50])
        self.assertTrue(all(s <= 50 for s in sizes))
        self.assertEqual(lg.stats()["sent"], 120)

    def test_retry_on_503_then_success(self):
        m = self.hec(statuses=[503, 429])
        lg = self.make(hec_url=m.url, hec_token=TOKEN)
        lg.start()
        lg.log("auth.login", tenant="t")
        self.assertTrue(lg.wait_idle(5))
        self.assertEqual([r["status"] for r in m.requests], [503, 429, 200])
        st = lg.stats()
        self.assertEqual(st["sent"], 1)
        self.assertEqual(st["failed"], 0)
        self.assertIn("429", st["last_error"])

    def test_retry_on_network_error(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()  # nothing listens here
        lg = self.make(hec_url="http://127.0.0.1:%d" % port, hec_token=TOKEN, _stop_timeout=0.2)
        lg.start()
        lg.log("auth.login", tenant="t")
        self.assertFalse(lg.wait_idle(0.5))
        st = lg.stats()
        self.assertIn("network error", st["last_error"])
        self.assertEqual(st["queued"], 1)
        self.assertEqual(st["local_written"], 1)  # local log unaffected

    def test_400_counts_failed(self):
        m = self.hec(statuses=[400])
        lg = self.make(hec_url=m.url, hec_token=TOKEN)
        lg.start()
        lg.log("auth.login", tenant="t")
        lg.log("auth.logout", tenant="t")
        self.assertTrue(lg.wait_idle(5))
        self.assertEqual(len(m.requests), 1)
        st = lg.stats()
        self.assertEqual(st["failed"], 2)
        self.assertEqual(st["sent"], 0)
        self.assertIn("400", st["last_error"])

    def test_queue_overflow_drops_oldest(self):
        lg = self.make(_max_queue=10)
        for i in range(15):
            lg.log("ticket.update", tenant="t", object_id=str(i))
        self.assertEqual(lg.stats()["dropped"], 5)
        self.assertEqual(lg.stats()["queued"], 10)
        lg.start()
        self.assertTrue(lg.wait_idle(5))
        self.assertEqual([e["object_id"] for e in self.read_log()], [str(i) for i in range(5, 15)])

    def test_token_never_in_file_or_stats(self):
        m = self.hec(statuses=[403])
        lg = self.make(hec_url=m.url, hec_token=TOKEN)
        lg.start()
        lg.log("token.create", tenant="t", details={"token": TOKEN, "nested": {"Authorization": "Splunk " + TOKEN}})
        self.assertTrue(lg.wait_idle(5))
        with open(os.path.join(self.dir, "logs", "audit.log"), encoding="utf-8") as fh:
            self.assertNotIn(TOKEN, fh.read())
        st = lg.stats()
        self.assertNotIn(TOKEN, json.dumps(st))
        self.assertNotIn(TOKEN, repr(lg))
        # an error message that happens to contain the token is masked
        lg._set_error("boom " + TOKEN)
        self.assertNotIn(TOKEN, lg.stats()["last_error"])

    def test_stop_drains_queue(self):
        m = self.hec()
        lg = self.make(hec_url=m.url, hec_token=TOKEN, _flush_interval=1.0)
        lg.start()
        for i in range(5):
            lg.log("ticket.update", tenant="t", object_id=str(i))
        lg.stop()
        self.assertEqual(len(m.events()), 5)
        self.assertEqual(lg.stats()["queued"], 0)


class RobustnessTest(BaseCase):
    def test_log_never_raises(self):
        lg = self.make()

        class Weird:
            def __str__(self):
                raise ValueError("no str")

        lg.log("x", tenant="t", details={"obj": Weird()})
        lg.log(Weird(), tenant=Weird())
        lg.log("x", tenant=None, details="not-a-dict")
        lg.log("x", tenant="t", details={"cyc": [object()]})
        with mock.patch("mydemoticket.audit._iso", side_effect=RuntimeError("clock")):
            lg.log("x", tenant="t")
        lg.start()
        self.assertTrue(lg.wait_idle(5))

    def test_log_is_thread_safe(self):
        lg = self.make()
        lg.start()

        def worker(n):
            for i in range(100):
                lg.log("ticket.update", tenant="t%d" % n, object_id=str(i))

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertTrue(lg.wait_idle(10))
        self.assertEqual(len(self.read_log()), 800)
        self.assertEqual(lg.stats()["local_written"], 800)

    def test_unwritable_dir_does_not_raise(self):
        blocker = os.path.join(self.dir, "logs")
        with open(blocker, "w") as fh:
            fh.write("file, not a dir")
        lg = self.make()
        lg.start()
        lg.log("x", tenant="t")
        self.assertTrue(lg.wait_idle(5))
        self.assertIn("local write failed", lg.stats()["last_error"])


class FromEnvTest(BaseCase):
    def test_from_env(self):
        env = {"MYDEMOTICKET_HEC_URL": "https://splunk.example:8088", "MYDEMOTICKET_HEC_TOKEN": TOKEN,
               "MYDEMOTICKET_HEC_VERIFY_TLS": "0", "MYDEMOTICKET_HEC_INDEX": "mdt_default"}
        resolver = {"a": "mdt_a"}.get
        with mock.patch.dict(os.environ, env, clear=False):
            lg = AuditLogger.from_env(self.dir, index_resolver=resolver)
        self.assertTrue(lg.hec_enabled)
        self.assertEqual(lg.hec_url, "https://splunk.example:8088/services/collector/event")
        self.assertFalse(lg.verify_tls)
        self.assertEqual(lg.default_index, "mdt_default")
        self.assertIs(lg.index_resolver, resolver)
        self.assertEqual(lg.sourcetype, "mydemoticket:audit")

    def test_from_env_defaults(self):
        keys = ["MYDEMOTICKET_HEC_URL", "MYDEMOTICKET_HEC_TOKEN", "MYDEMOTICKET_HEC_VERIFY_TLS",
                "MYDEMOTICKET_HEC_INDEX"]
        clean = {k: v for k, v in os.environ.items() if k not in keys}
        with mock.patch.dict(os.environ, clean, clear=True):
            lg = AuditLogger.from_env(self.dir)
        self.assertFalse(lg.hec_enabled)
        self.assertTrue(lg.verify_tls)
        self.assertIsNone(lg.default_index)
        self.assertEqual(lg.stats()["hec_enabled"], False)

    def test_url_without_token_disables_hec(self):
        with mock.patch.dict(os.environ, {"MYDEMOTICKET_HEC_URL": "https://x:8088",
                                          "MYDEMOTICKET_HEC_TOKEN": ""}):
            lg = AuditLogger.from_env(self.dir)
        self.assertFalse(lg.hec_enabled)


if __name__ == "__main__":
    unittest.main()
