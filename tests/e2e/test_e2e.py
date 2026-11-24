"""End-to-end tests: real MyDemoTicket server <-> SOAR app connector <-> mocked SOAR.

Each test class starts the real ticket server as a subprocess on a free 127.0.0.1 port
with a fresh temporary data directory (seed on), a SOAR REST mock and a generic webhook
receiver, and registers one webhook of each type via the admin API. Credentials are read
at runtime from <data-dir>/initial-credentials.txt and never printed.

Run from the repo root:  python3 -m unittest discover -s tests/e2e -t tests/e2e -v
Set MDT_E2E_DATA_ROOT to place the temporary data directories below a given folder.
"""

import ast
import http.client
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from soar_mock import GenericReceiver, SoarMock

E2E_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(os.path.dirname(E2E_DIR))
TICKET_DIR = os.path.join(REPO_DIR, "ticket-system")
SOAR_APP_DIR = os.path.join(REPO_DIR, "soar-app")
PLAYBOOK_DIR = os.path.join(REPO_DIR, "soar-playbooks")

for _path in (os.path.join(SOAR_APP_DIR, "tests", "phantom_stub"), os.path.join(SOAR_APP_DIR, "mydemoticket")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import phantom.app as phantom  # noqa: E402  (stub)
from mydemoticket_connector import MyDemoTicketConnector  # noqa: E402

ALL_EVENTS = ["ticket.created", "ticket.updated", "ticket.state_changed", "ticket.commented"]
WAIT_SECONDS = 20


# ---------------------------------------------------------------------- helpers
def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(predicate, timeout=WAIT_SECONDS, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def read_credentials(data_dir):
    """Parse initial-credentials.txt into {"<user> password": ..., "<user> API token": ...}."""
    creds = {}
    with open(os.path.join(data_dir, "initial-credentials.txt"), encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or ": " not in line:
                continue
            key, _, value = line.partition(": ")
            creds[key] = value
    return creds


class Response(object):
    def __init__(self, status, body):
        self.status = status
        self.json = body

    def __repr__(self):  # bodies never contain secrets in the requests we make with asserts
        return "<Response %s %r>" % (self.status, self.json)


class Client(object):
    """Tiny JSON client: Bearer token or session cookie + CSRF header (like the web UI)."""

    def __init__(self, port, token=None, source=None):
        self.port = port
        self._token = token
        self.source = source
        self.cookies = {}
        self.csrf = None

    def __repr__(self):
        return "<Client port=%d>" % self.port

    def request(self, method, path, body=None, send_csrf=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        if self.cookies:
            headers["Cookie"] = "; ".join("%s=%s" % kv for kv in self.cookies.items())
        if self.csrf and send_csrf and method not in ("GET", "HEAD"):
            headers["X-CSRF-Token"] = self.csrf
        if self.source:
            headers["X-MyDemoTicket-Source"] = self.source
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, "/api/v1" + path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            for name, value in resp.getheaders():
                if name.lower() == "set-cookie":
                    cname, _, rest = value.partition("=")
                    cvalue = rest.split(";", 1)[0]
                    if cvalue:
                        self.cookies[cname] = cvalue
                    else:
                        self.cookies.pop(cname, None)
        finally:
            conn.close()
        parsed = json.loads(raw.decode("utf-8")) if raw else None
        return Response(resp.status, parsed)

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body if body is not None else {}, **kw)

    def patch(self, path, body=None, **kw):
        return self.request("PATCH", path, body if body is not None else {}, **kw)

    def login(self, username, password):
        resp = self.request("POST", "/auth/login", {"username": username, "password": password})
        if resp.status != 200:
            raise AssertionError("login failed for %s: HTTP %d" % (username, resp.status))
        self.csrf = resp.json["csrf_token"]
        return resp


class TicketServer(object):
    """The real ticket server as a subprocess with its own data directory."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.proc = None
        self.port = None
        self.log_path = os.path.join(os.path.dirname(data_dir), "server-%s.log" % os.path.basename(data_dir))

    def start(self):
        env = dict(os.environ)
        for key in ("MYDEMOTICKET_ADMIN_PASSWORD", "MYDEMOTICKET_ADMIN_TOKEN", "MYDEMOTICKET_DATA_DIR"):
            env.pop(key, None)
        last_error = None
        for _attempt in range(3):  # a freshly picked free port can be taken in between
            self.port = free_port()
            log = open(self.log_path, "ab")
            try:
                self.proc = subprocess.Popen(
                    [sys.executable, "-m", "mydemoticket", "--host", "127.0.0.1", "--port", str(self.port),
                     "--data-dir", self.data_dir, "--quiet"],
                    cwd=TICKET_DIR, env=env, stdout=log, stderr=subprocess.STDOUT)
            finally:
                log.close()
            if wait_until(self._healthy, timeout=15):
                return self
            last_error = "server did not become healthy (exit code %r)" % self.proc.poll()
            self.stop()
        raise RuntimeError(last_error)

    def _healthy(self):
        if self.proc.poll() is not None:
            return False
        try:
            resp = Client(self.port).get("/health")
        except OSError:
            return False
        return resp.status == 200 and resp.json.get("status") == "ok"

    @property
    def base_url(self):
        return "http://127.0.0.1:%d" % self.port

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)


class E2ETestCase(unittest.TestCase):
    """Per class: fresh data dir, real server, SOAR mock, generic receiver, two webhooks."""

    @classmethod
    def setUpClass(cls):
        root = os.environ.get("MDT_E2E_DATA_ROOT") or None
        if root:
            os.makedirs(root, exist_ok=True)
        cls.tmpdir = tempfile.mkdtemp(prefix="mdt-e2e-", dir=root)
        cls.started_at = datetime.now(timezone.utc) - timedelta(seconds=2)
        cls.server = TicketServer(os.path.join(cls.tmpdir, "data")).start()
        try:
            cls._setup_env()
        except Exception:
            cls._teardown_env()
            raise

    @classmethod
    def _setup_env(cls):
        cls.creds = read_credentials(cls.server.data_dir)
        port = cls.server.port
        cls.admin = Client(port, cls.creds["admin API token"])
        cls.soar_token = cls.creds["soar API token"]

        cls.soar_secret = secrets.token_hex(16)
        cls.generic_secret = secrets.token_hex(16)
        cls.soar = SoarMock(cls.soar_secret)
        cls.receiver = GenericReceiver(cls.generic_secret)

        resp = cls.admin.post("/webhooks", {"type": "soar", "name": "e2e soar", "url": cls.soar.url,
                                            "secret": cls.soar_secret, "soar_label": "mydemoticket",
                                            "verify_tls": True})
        assert resp.status == 201, "soar webhook: HTTP %d" % resp.status
        cls.soar_hook_id = resp.json["id"]
        resp = cls.admin.post("/webhooks", {"type": "generic", "name": "e2e generic",
                                            "url": cls.receiver.url + "/hook", "secret": cls.generic_secret})
        assert resp.status == 201, "generic webhook: HTTP %d" % resp.status
        assert sorted(resp.json["events"]) == sorted(ALL_EVENTS), resp.json["events"]
        cls.generic_hook_id = resp.json["id"]
        cls._tokens = {}

    @classmethod
    def tearDownClass(cls):
        cls._teardown_env()

    @classmethod
    def _teardown_env(cls):
        for name in ("soar", "receiver"):
            obj = getattr(cls, name, None)
            if obj is not None:
                obj.close()
        cls.server.stop()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    # -- clients ----------------------------------------------------------------
    @classmethod
    def api_client(cls, username):
        """Bearer client for a seed user; a token is created via the admin API (never printed)."""
        if username not in cls._tokens:
            resp = cls.admin.post("/users/%s/tokens" % username, {"name": "e2e"})
            if resp.status != 201:
                raise AssertionError("token for %s: HTTP %d" % (username, resp.status))
            cls._tokens[username] = resp.json["token"]
        return Client(cls.server.port, cls._tokens[username])

    @classmethod
    def web_client(cls, username):
        client = Client(cls.server.port)
        client.login(username, cls.creds["%s password" % username])
        return client

    def connector(self, action, param=None, container_id=None, state=None, poll_now=False):
        conn = MyDemoTicketConnector()
        conn.stub_setup(config={"base_url": self.server.base_url, "api_token": self.soar_token,
                                "verify_server_cert": True, "source_tag": "soar",
                                "ingest_label": "mydemoticket"},
                        action=action, state=state, container_id=container_id, poll_now=poll_now)
        self.assertTrue(conn.initialize())
        ret = conn.handle_action(param or {})
        conn.finalize()
        result = conn.get_action_results()[-1]
        self.assertEqual(bool(ret), bool(result.get_status()))
        self.assertTrue(result.get_status(), "%s failed: %s" % (action, result.get_message()))
        return conn, result

    # -- assertions / waits -------------------------------------------------------
    def ok(self, resp, status=200):
        self.assertEqual(resp.status, status, resp)
        return resp.json

    def ticket(self, number):
        return self.ok(self.admin.get("/tickets/%s" % number))

    def journal(self, number, client=None):
        return self.ok((client or self.admin).get("/tickets/%s/journal" % number))

    def deliveries(self, hook_id, number):
        rows = self.ok(self.admin.get("/webhooks/%d/deliveries" % hook_id))
        return [r for r in reversed(rows) if r["ticket_number"] == number]

    def wait_generic(self, number, count):
        got = wait_until(lambda: len(self.receiver.for_ticket(number)) >= count)
        self.assertTrue(got, "generic receiver got %d/%d events for %s"
                        % (len(self.receiver.for_ticket(number)), count, number))
        return self.receiver.for_ticket(number)

    def wait_soar_artifacts(self, number, count):
        got = wait_until(lambda: len(self.soar.artifacts_for(number)) >= count)
        self.assertTrue(got, "SOAR mock got %d/%d artifacts for %s"
                        % (len(self.soar.artifacts_for(number)), count, number))
        return self.soar.artifacts_for(number)

    def wait_deliveries_done(self, hook_id, number, count):
        def done():
            rows = self.deliveries(hook_id, number)
            return len(rows) >= count and all(r["status"] in ("success", "skipped", "failed") for r in rows)
        self.assertTrue(wait_until(done), self.deliveries(hook_id, number))
        return self.deliveries(hook_id, number)

    def assert_generic_signed(self, deliveries, source):
        for d in deliveries:
            self.assertTrue(d["signature_ok"], "bad HMAC signature on %s" % d["event_header"])
            self.assertEqual(d["path"], "/hook")
            self.assertEqual(d["event_header"], d["payload"]["event"])
            self.assertEqual(d["delivery_header"], d["payload"]["delivery_id"])
            self.assertEqual(d["payload"]["source"], source)

    def assert_mock_clean(self):
        self.assertEqual(self.soar.auth_failures, 0)
        self.assertEqual(self.soar.bad_requests, [])


# ---------------------------------------------------------------------- direction 1
class TestTicketToSoar(E2ETestCase):
    """Analyst works in the ticket system -> SOAR webhook (mock) and generic webhook."""

    EXPECTED_EVENTS = ["ticket.created", "ticket.updated", "ticket.updated", "ticket.state_changed",
                       "ticket.commented", "ticket.commented", "ticket.state_changed"]

    def _analyst_lifecycle(self, client, source, es_finding_id):
        t = self.ok(client.post("/tickets", {
            "short_description": "E2E suspicious login (%s)" % source, "description": "created by e2e",
            "assignment_group": "SOC L1", "custom_fields": {"incident_type": "malware"},
            "external_refs": {"es_finding_id": es_finding_id}}), 201)
        number = t["number"]
        self.assertEqual(t["state"], "new")
        self.assertEqual(t["priority"], 3)
        path = "/tickets/%s" % number
        self.ok(client.patch(path, {"priority": 1}))
        self.ok(client.patch(path, {"custom_fields": {"risk_score": 85, "incident_type": "phishing"}}))
        self.ok(client.patch(path, {"state": "in_progress"}))
        self.ok(client.post(path + "/comments", {"body": "We are looking into it"}), 201)
        self.ok(client.post(path + "/comments", {"body": "IOC enrichment pending", "type": "work_note"}), 201)
        resolved = self.ok(client.patch(path, {"state": "resolved", "close_notes": "False positive"}))
        self.assertEqual(resolved["state"], "resolved")
        self.assertIsNotNone(resolved["resolved_at"])
        return number

    def _check_push(self, number, source, es_finding_id):
        artifacts = self.wait_soar_artifacts(number, len(self.EXPECTED_EVENTS))
        self.wait_deliveries_done(self.soar_hook_id, number, len(self.EXPECTED_EVENTS))
        self.assert_mock_clean()

        containers = self.soar.containers_for(number)
        self.assertEqual(len(containers), 1, containers)
        container = containers[0]
        self.assertEqual(container["label"], "mydemoticket")
        self.assertTrue(container["name"].startswith(number + ": E2E suspicious login"))
        self.assertEqual(container["severity"], "medium")  # priority 3 at creation

        self.assertEqual([a["name"] for a in artifacts], self.EXPECTED_EVENTS)
        for a in artifacts:
            self.assertEqual(a["container_id"], container["id"])
            self.assertEqual(a["label"], "mydemoticket_event")
            self.assertTrue(a["run_automation"])
            self.assertEqual(a["cef"]["event"], a["name"])
            self.assertEqual(a["cef"]["source"], source)
            self.assertEqual(a["cef"]["esFindingId"], es_finding_id)
            self.assertEqual(a["cef"]["assignmentGroup"], "SOC L1")
            json.loads(a["cef"]["changes"])
            json.loads(a["cef"]["customFields"])
        self.assertEqual(len({a["source_data_identifier"] for a in artifacts}), len(artifacts))

        created, prio, custom, in_progress, comment, work_note, resolved = [a["cef"] for a in artifacts]
        self.assertEqual(created["ticketState"], "new")
        self.assertEqual(json.loads(created["customFields"]), {"incident_type": "malware"})
        self.assertEqual(json.loads(prio["changes"]), {"priority": {"old": 3, "new": 1}})
        self.assertEqual(prio["ticketPriority"], 1)
        self.assertEqual(json.loads(custom["changes"]), {
            "custom_fields.risk_score": {"old": None, "new": 85},
            "custom_fields.incident_type": {"old": "malware", "new": "phishing"}})
        self.assertEqual(json.loads(custom["customFields"]), {"incident_type": "phishing", "risk_score": 85})
        self.assertEqual(in_progress["ticketState"], "in_progress")
        self.assertEqual(json.loads(in_progress["changes"]), {"state": {"old": "new", "new": "in_progress"}})
        self.assertEqual(comment["comment"], "We are looking into it")
        self.assertEqual(work_note["comment"], "IOC enrichment pending")
        self.assertEqual(resolved["ticketState"], "resolved")
        changes = json.loads(resolved["changes"])
        self.assertEqual(changes["state"], {"old": "in_progress", "new": "resolved"})
        self.assertEqual(changes["close_notes"]["new"], "False positive")

        # the container id from SOAR is stored on the ticket, by the system (no extra event)
        ticket = wait_until(lambda: self.ticket(number)["external_refs"].get("soar_container_id")
                            == container["id"] and self.ticket(number))
        self.assertTrue(ticket, self.ticket(number))
        self.assertEqual(ticket["external_refs"]["es_finding_id"], es_finding_id)
        audits = [e for e in self.journal(number)
                  if e["type"] == "audit" and e["field"] == "external_refs.soar_container_id"]
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["source"], "webhook")

        generic = self.wait_generic(number, len(self.EXPECTED_EVENTS))
        self.assertEqual([d["payload"]["event"] for d in generic], self.EXPECTED_EVENTS)
        self.assert_generic_signed(generic, source)
        self.assertEqual(generic[4]["payload"]["comment"]["type"], "comment")
        self.assertEqual(generic[5]["payload"]["comment"]["type"], "work_note")
        time.sleep(0.3)  # nothing else (e.g. an echo of the container-id update) may follow
        self.assertEqual(len(self.soar.artifacts_for(number)), len(self.EXPECTED_EVENTS))
        self.assertEqual(len(self.receiver.for_ticket(number)), len(self.EXPECTED_EVENTS))
        rows = self.deliveries(self.generic_hook_id, number)
        self.assertEqual([r["status"] for r in rows], ["success"] * len(self.EXPECTED_EVENTS))

    def test_1_api_lifecycle_pushes_to_soar(self):
        number = self._analyst_lifecycle(self.api_client("analyst1"), "api", "finding-e2e-api")
        self._check_push(number, "api", "finding-e2e-api")
        type(self).api_number = number

    def test_2_web_session_lifecycle_pushes_to_soar(self):
        client = self.web_client("analyst1")
        me = self.ok(client.get("/me"))
        self.assertEqual(me["username"], "analyst1")
        # like the UI: writing without the CSRF header is rejected
        self.assertEqual(client.post("/tickets", {"short_description": "no csrf"}, send_csrf=False).status, 403)
        number = self._analyst_lifecycle(client, "web", "finding-e2e-web")
        self._check_push(number, "web", "finding-e2e-web")
        type(self).web_number = number

    def test_3_on_poll_ingests_changed_tickets(self):
        numbers = [getattr(type(self), n, None) for n in ("api_number", "web_number")]
        if None in numbers:
            self.skipTest("depends on test_1 and test_2")
        since = self.started_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        conn, result = self.connector("on_poll", state={"last_poll": since})
        self.assertGreaterEqual(result.get_summary()["ingested"], 2)
        self.assertEqual(result.get_summary()["failed"], 0)
        self.assertIn("last_poll", conn.get_state())
        for number in numbers:
            ticket = self.ticket(number)
            containers = [c for c in conn.containers if c["source_data_identifier"] == number]
            self.assertEqual(len(containers), 1)
            self.assertEqual(containers[0]["label"], "mydemoticket")
            self.assertEqual(containers[0]["data"]["state"], "resolved")
            arts = [a for a in conn.artifacts if a["container_id"] == containers[0]["id"]]
            self.assertEqual(len(arts), 1)
            cef = arts[0]["cef"]
            self.assertEqual(cef["ticketNumber"], number)
            self.assertEqual(cef["ticketState"], "resolved")
            self.assertEqual(cef["ticketPriority"], 1)
            self.assertEqual(json.loads(cef["customFields"]), ticket["custom_fields"])
            self.assertEqual(cef["esFindingId"], ticket["external_refs"]["es_finding_id"])
            self.assertEqual(arts[0]["source_data_identifier"], "%s:%s" % (number, ticket["updated_at"]))


# ---------------------------------------------------------------------- direction 2
class TestSoarToTicket(E2ETestCase):
    """SOAR app actions against the real server; no echo to the SOAR webhook."""

    def test_connector_actions_reach_ticket_system(self):
        self.connector("test_connectivity")

        _, r = self.connector("create_ticket", {
            "short_description": "E2E phishing from SOAR", "description": "ES finding",
            "priority": 2, "assignment_group": "SOC L2", "category": "security",
            "custom_fields": json.dumps({"incident_type": "phishing", "risk_score": 42}),
            "es_finding_id": "finding-e2e-soar"}, container_id=4711)
        number = r.get_summary()["ticket_number"]
        self.assertTrue(number.startswith("TKT"))

        self.connector("update_ticket", {"number": number, "priority": 1, "assigned_to": "analyst2",
                                         "custom_fields": json.dumps({"risk_score": 90})})
        self.connector("set_state", {"number": number, "state": "in_progress"})
        self.connector("add_comment", {"number": number, "body": "Customer informed", "type": "comment"})
        self.connector("add_comment", {"number": number, "body": "VT 0/70", "type": "work_note"})
        self.connector("set_state", {"number": number, "state": "resolved", "close_notes": "Mail blocked"})

        _, r = self.connector("get_ticket", {"number": number})
        got = r.get_data()[0]
        self.assertEqual(r.get_summary()["state"], "resolved")
        _, r = self.connector("get_journal", {"number": number})
        self.assertEqual([e["id"] for e in r.get_data()], [e["id"] for e in self.journal(number)])
        _, r = self.connector("list_tickets", {"q": number, "state": "resolved"})
        self.assertEqual([t["number"] for t in r.get_data()], [number])
        _, r = self.connector("list_custom_fields")
        self.assertIn("risk_score", [f["name"] for f in r.get_data()])

        # everything arrived in the ticket system (checked as admin)
        t = self.ticket(number)
        self.assertEqual(t, got)
        self.assertEqual(t["state"], "resolved")
        self.assertEqual(t["priority"], 1)
        self.assertEqual(t["assignment_group"], "SOC L2")
        self.assertEqual(t["assigned_to"], "analyst2")
        self.assertEqual(t["caller"], "soar")
        self.assertEqual(t["category"], "security")
        self.assertEqual(t["close_notes"], "Mail blocked")
        self.assertEqual(t["custom_fields"], {"incident_type": "phishing", "risk_score": 90})
        self.assertEqual(t["external_refs"], {"soar_container_id": 4711, "es_finding_id": "finding-e2e-soar"})
        journal = self.journal(number)
        self.assertTrue(journal)
        self.assertEqual({e["source"] for e in journal}, {"soar"})
        self.assertEqual({e["actor"] for e in journal}, {"soar"})
        bodies = [(e["type"], e["body"]) for e in journal if e["type"] in ("comment", "work_note")]
        self.assertEqual(bodies, [("comment", "Customer informed"), ("work_note", "VT 0/70")])

        # generic webhook gets every change with source "soar" ...
        events = ["ticket.created", "ticket.updated", "ticket.state_changed", "ticket.commented",
                  "ticket.commented", "ticket.state_changed"]
        generic = self.wait_generic(number, len(events))
        self.assertEqual([d["payload"]["event"] for d in generic], events)
        self.assert_generic_signed(generic, "soar")
        for d in generic:
            self.assertEqual(d["payload"]["actor"], "soar")
        # ... while the SOAR webhook skips all of them (loop prevention, no echo)
        rows = self.wait_deliveries_done(self.soar_hook_id, number, len(events))
        self.assertEqual([r["status"] for r in rows], ["skipped"] * len(events))
        self.assertEqual(self.soar.containers_for(number), [])
        self.assertEqual(self.soar.artifacts_for(number), [])
        self.assertEqual(self.ticket(number)["external_refs"]["soar_container_id"], 4711)
        self.assert_mock_clean()

        # on poll skips tickets whose newest change came from SOAR itself
        since = self.started_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        conn, r = self.connector("on_poll", state={"last_poll": since})
        self.assertEqual([c for c in conn.containers if c["source_data_identifier"] == number], [])
        self.assertGreaterEqual(r.get_summary()["skipped"], 1)

    def test_find_by_es_finding_then_comment_with_author(self):
        finding = "e2e-finding-%s" % secrets.token_hex(4)
        _, r = self.connector("create_ticket", {"short_description": "E2E idempotent ticket",
                                                "es_finding_id": finding}, container_id=4712)
        number = r.get_summary()["ticket_number"]
        # decoys: a similar finding id and the same id as another key
        self.connector("create_ticket", {"short_description": "E2E decoy", "es_finding_id": finding + "0"})
        decoy = self.ok(self.admin.post("/tickets", {"short_description": "E2E decoy 2",
                                                     "external_refs": {"other_id": finding}}), 201)
        self.assertNotEqual(decoy["number"], number)

        _, r = self.connector("list_tickets", {"external_ref_key": "es_finding_id", "external_ref_value": finding})
        self.assertEqual([t["number"] for t in r.get_data()], [number])
        self.assertEqual(r.get_summary(), {"total": 1, "returned": 1})
        self.assertEqual(r.get_data()[0]["external_refs"]["es_finding_id"], finding)

        _, r = self.connector("add_comment", {"number": number, "body": "Contained.\nForensics next.",
                                              "author": "jdoe"})
        entry = r.get_data()[0]
        self.assertEqual((entry["author"], entry["actor"]), ("jdoe", "soar"))

        comments = [e for e in self.journal(number) if e["type"] == "comment"]
        self.assertEqual([(e["author"], e["actor"], e["source"], e["body"]) for e in comments],
                         [("jdoe", "soar", "soar", "Contained.\nForensics next.")])
        _, r = self.connector("get_journal", {"number": number})
        self.assertEqual([e["author"] for e in r.get_data() if e["type"] == "comment"], ["jdoe"])
        generic = self.wait_generic(number, 2)
        commented = [d["payload"] for d in generic if d["payload"]["event"] == "ticket.commented"]
        self.assertEqual([(c["comment"]["author"], c["actor"]) for c in commented], [("jdoe", "soar")])

    def test_invalid_transition_reported_as_error(self):
        _, r = self.connector("create_ticket", {"short_description": "E2E closed directly"})
        number = r.get_summary()["ticket_number"]
        conn = MyDemoTicketConnector()
        conn.stub_setup(config={"base_url": self.server.base_url, "api_token": self.soar_token},
                        action="set_state")
        self.assertTrue(conn.initialize())
        self.assertFalse(conn.handle_action({"number": number, "state": "closed"}))
        self.assertIn("HTTP 409", conn.get_action_results()[-1].get_message())
        self.assertEqual(self.ticket(number)["state"], "new")


# ---------------------------------------------------------------------- round trip
def es_status_mapping():
    """STATE_TO_ES_STATUS from the ES mirror playbook (pure dict, read via ast - no phantom.rules)."""
    with open(os.path.join(PLAYBOOK_DIR, "mydemoticket_es_mirror.py"), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "STATE_TO_ES_STATUS"
                                                for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("STATE_TO_ES_STATUS not found in mydemoticket_es_mirror.py")


class TestRoundTrip(E2ETestCase):
    def test_soar_ticket_changed_by_analyst_returns_with_finding_id(self):
        _, r = self.connector("create_ticket", {
            "short_description": "E2E round trip", "assignment_group": "SOC L1", "priority": 2,
            "es_finding_id": "finding-e2e-roundtrip"}, container_id=4242)
        number = r.get_summary()["ticket_number"]
        self.wait_generic(number, 1)
        self.assertEqual(self.soar.containers_for(number), [])  # creation by SOAR: no echo

        analyst = self.api_client("analyst1")
        self.ok(analyst.patch("/tickets/%s" % number, {"state": "in_progress", "assigned_to": "analyst1"}))
        self.ok(analyst.post("/tickets/%s/comments" % number, {"body": "Taking this one"}), 201)

        artifacts = self.wait_soar_artifacts(number, 2)
        self.assert_mock_clean()
        containers = self.soar.containers_for(number)
        self.assertEqual(len(containers), 1)
        self.assertEqual(containers[0]["severity"], "high")  # priority 2
        self.assertEqual([a["name"] for a in artifacts], ["ticket.state_changed", "ticket.commented"])
        for a in artifacts:
            self.assertEqual(a["cef"]["esFindingId"], "finding-e2e-roundtrip")
            self.assertEqual(a["cef"]["source"], "api")
            self.assertEqual(a["cef"]["assignedTo"], "analyst1")
        self.assertEqual(artifacts[0]["cef"]["ticketState"], "in_progress")
        self.assertEqual(artifacts[1]["cef"]["comment"], "Taking this one")
        # SOAR's answer replaces the originating container id (documented behaviour)
        self.assertTrue(wait_until(lambda: self.ticket(number)["external_refs"].get("soar_container_id")
                                   == containers[0]["id"]))

        # pull path (on poll) builds the same container as the push path (same sdi + severity)
        since = self.started_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        conn, _ = self.connector("on_poll", state={"last_poll": since})
        polled = [c for c in conn.containers if c["source_data_identifier"] == number]
        self.assertEqual(len(polled), 1)
        self.assertEqual((polled[0]["label"], polled[0]["severity"]),
                         (containers[0]["label"], containers[0]["severity"]))

        mapping = es_status_mapping()
        self.assertEqual(set(mapping), {"new", "in_progress", "on_hold", "resolved", "closed", "cancelled"})
        self.assertEqual(mapping[artifacts[0]["cef"]["ticketState"]], 2)

        # SOAR (playbook) answers by resolving the ticket: no new push to SOAR
        self.connector("set_state", {"number": number, "state": "resolved", "close_notes": "ES finding closed"})
        self.wait_generic(number, 4)
        time.sleep(0.3)
        self.assertEqual(len(self.soar.artifacts_for(number)), 2)
        self.assertEqual(mapping[self.ticket(number)["state"]], 4)


# ---------------------------------------------------------------------- roles
class TestRoles(E2ETestCase):
    def test_requester_never_sees_work_notes(self):
        requester = self.api_client("requester1")
        t = self.ok(requester.post("/tickets", {"short_description": "E2E my laptop is slow"}), 201)
        number = t["number"]
        self.assertEqual(t["caller"], "requester1")
        self.ok(requester.post("/tickets/%s/comments" % number, {"body": "Please help"}), 201)
        self.assertEqual(requester.post("/tickets/%s/comments" % number,
                                        {"body": "x", "type": "work_note"}).status, 403)

        analyst = self.api_client("analyst1")
        self.ok(analyst.post("/tickets/%s/comments" % number, {"body": "Internal: check EDR", "type": "work_note"}),
                201)
        self.connector("add_comment", {"number": number, "body": "SOAR: host isolated", "type": "work_note"})
        self.ok(analyst.post("/tickets/%s/comments" % number, {"body": "We are on it"}), 201)

        seen = self.journal(number, requester)
        self.assertNotIn("work_note", [e["type"] for e in seen])
        self.assertEqual([e["body"] for e in seen if e["type"] == "comment"], ["Please help", "We are on it"])
        full = self.journal(number, analyst)
        self.assertEqual([e["body"] for e in full if e["type"] == "work_note"],
                         ["Internal: check EDR", "SOAR: host isolated"])
        # a requester cannot read someone else's ticket
        other = self.ok(analyst.post("/tickets", {"short_description": "E2E internal"}), 201)
        self.assertIn(requester.get("/tickets/%s" % other["number"]).status, (403, 404))

    def test_agent_of_other_team_gets_403(self):
        analyst1 = self.api_client("analyst1")
        t = self.ok(analyst1.post("/tickets", {"short_description": "E2E L1 only", "assignment_group": "SOC L1"}),
                    201)
        number = t["number"]
        analyst2 = self.web_client("analyst2")  # SOC L2, via web session like the UI
        self.ok(analyst2.get("/tickets/%s" % number))  # agents read all tickets
        self.assertEqual(analyst2.patch("/tickets/%s" % number, {"priority": 1}).status, 403)
        self.assertEqual(analyst2.patch("/tickets/%s" % number, {"state": "in_progress"}).status, 403)
        self.assertEqual(analyst2.post("/tickets/%s/comments" % number, {"body": "not mine"}).status, 403)
        t = self.ticket(number)
        self.assertEqual((t["priority"], t["state"]), (3, "new"))
        # only "created" (plus, asynchronously, the system audit storing the SOAR container id)
        user_entries = [e for e in self.journal(number) if e["source"] != "webhook"]
        self.assertEqual([(e["type"], e["field"]) for e in user_entries], [("audit", "created")])
        # denied requests emit nothing: only ticket.created reaches the receivers
        self.wait_generic(number, 1)
        self.wait_soar_artifacts(number, 1)
        time.sleep(0.3)
        self.assertEqual([d["payload"]["event"] for d in self.receiver.for_ticket(number)], ["ticket.created"])
        self.assertEqual([a["name"] for a in self.soar.artifacts_for(number)], ["ticket.created"])


if __name__ == "__main__":
    unittest.main()
