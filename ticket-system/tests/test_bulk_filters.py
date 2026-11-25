"""Multi-value list filters (GET /tickets) and bulk editing (POST /tickets/bulk)."""

import contextlib
import io
import json
import time
import unittest
import unittest.mock

from helpers import Receiver, ServerTestCase
from test_webhooks import ALL_EVENTS, SoarMock


def numbers(resp):
    return sorted(i["number"] for i in resp.json["items"])


class MultiFilterTest(ServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        resp = cls.admin.post("/teams", {"name": "Red, Blue"}, headers={"X-MyDemoTicket-Tenant": "soc-games"})
        assert resp.status == 201, resp

    def setUp(self):
        self.marker = "multifilter-%d" % time.time_ns()
        mk = lambda **kw: self.create_ticket(short_description=self.marker, **kw)  # noqa: E731
        self.a = mk(priority=1, assignment_group="SOC L1", assigned_to="analyst1")
        self.b = mk(priority=2, assignment_group="SOC L2")
        self.c = mk(priority=4)
        self.d = mk(priority=3, assignment_group="Red, Blue", assigned_to="analyst2", caller="requester1")
        self.admin.patch("/tickets/" + self.b["number"], {"state": "in_progress"})

    def get(self, query):
        resp = self.admin.get("/tickets?q=%s&%s" % (self.marker, query))
        self.assertEqual(resp.status, 200, resp)
        return numbers(resp)

    def expect(self, *tickets):
        return sorted(t["number"] for t in tickets)

    def test_single_values_unchanged(self):
        self.assertEqual(self.get("priority=1"), self.expect(self.a))
        self.assertEqual(self.get("assignment_group=SOC%20L2"), self.expect(self.b))
        self.assertEqual(self.get("assigned_to=analyst1"), self.expect(self.a))
        self.assertEqual(self.get("assigned_to="), self.expect(self.b, self.c))
        self.assertEqual(self.get("assigned_to=null"), self.expect(self.b, self.c))
        self.assertEqual(self.get("assignment_group="), self.expect(self.c))
        self.assertEqual(self.get("state=in_progress"), self.expect(self.b))
        self.assertEqual(self.get("caller=requester1"), self.expect(self.d))
        # a team name with a comma still matches as a whole
        self.assertEqual(self.get("assignment_group=Red%2C%20Blue"), self.expect(self.d))

    def test_comma_separated(self):
        self.assertEqual(self.get("priority=1,4"), self.expect(self.a, self.c))
        self.assertEqual(self.get("state=new,in_progress"), self.expect(self.a, self.b, self.c, self.d))
        self.assertEqual(self.get("assignment_group=SOC%20L1,SOC%20L2"), self.expect(self.a, self.b))
        self.assertEqual(self.get("assigned_to=analyst1,analyst2"), self.expect(self.a, self.d))
        self.assertEqual(self.get("caller=admin,requester1"), self.expect(self.a, self.b, self.c, self.d))

    def test_repeated_parameters(self):
        self.assertEqual(self.get("priority=1&priority=2"), self.expect(self.a, self.b))
        self.assertEqual(self.get("state=new&state=in_progress"), self.expect(self.a, self.b, self.c, self.d))
        self.assertEqual(self.get("assignment_group=SOC%20L1&assignment_group=Red%2C%20Blue"),
                         self.expect(self.a, self.d))
        self.assertEqual(self.get("assigned_to=analyst2&assigned_to=analyst1,-"),
                         self.expect(self.a, self.b, self.c, self.d))

    def test_unassigned_token_combines_with_names(self):
        self.assertEqual(self.get("assigned_to=-"), self.expect(self.b, self.c))
        self.assertEqual(self.get("assigned_to=analyst1,-"), self.expect(self.a, self.b, self.c))
        self.assertEqual(self.get("assignment_group=-,SOC%20L1"), self.expect(self.a, self.c))
        # filters are AND-combined, values inside a filter OR-combined
        self.assertEqual(self.get("assignment_group=SOC%20L1,SOC%20L2&priority=2,3"), self.expect(self.b))

    def test_invalid_values(self):
        self.assertEqual(self.admin.get("/tickets?priority=1,9").status, 422)
        self.assertEqual(self.admin.get("/tickets?state=new&state=bogus").status, 422)
        self.assertEqual(self.get("priority=&state="), self.expect(self.a, self.b, self.c, self.d))


class BulkUpdateTest(ServerTestCase):
    use_recorder = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for name, role, teams in (("bulk_l1", "analyst_l1", ["SOC L1"]), ("bulk_ro", "read_only", [])):
            resp = cls.admin.post("/users", {"username": name, "role": role, "tenant": "soc-games", "teams": teams})
            assert resp.status == 201, resp

    def bulk(self, nums, changes, client=None, headers=None):
        return (client or self.admin).post("/tickets/bulk", {"numbers": nums, "changes": changes}, headers=headers)

    def test_validation(self):
        t = self.create_ticket()
        n = t["number"]
        for body in ({}, {"numbers": [], "changes": {"priority": 1}}, {"numbers": [n]},
                     {"numbers": [n], "changes": {}}, {"numbers": "x", "changes": {"priority": 1}},
                     {"numbers": [1], "changes": {"priority": 1}},
                     {"numbers": [n], "changes": {"short_description": "x"}},
                     {"numbers": [n], "changes": {"priority": 1}, "extra": 1}):
            self.assertEqual(self.admin.post("/tickets/bulk", body).status, 422, body)
        self.assertEqual(self.admin.post("/tickets/bulk", []).status, 400)

    def test_limit_200(self):
        too_many = ["TKT%07d" % i for i in range(1, 202)]
        resp = self.bulk(too_many, {"priority": 2})
        self.assertEqual(resp.status, 422)
        self.assertIn("200", resp.json["error"] if "error" in resp.json else json.dumps(resp.json))
        resp = self.bulk(too_many[:200], {"priority": 2}, client=self.client_for("bulk_ro"))
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(resp.json["results"]), 200)

    def test_changes_every_ticket_like_patch(self):
        tickets = [self.create_ticket(priority=3) for _ in range(3)]
        nums = [t["number"] for t in tickets]
        self.audit.clear()
        resp = self.bulk(nums + [nums[0]], {"priority": 1, "assignment_group": "SOC L2", "assigned_to": "analyst2"})
        self.assertEqual(resp.status, 200, resp)
        self.assertEqual(resp.json["results"],
                         [{"number": n, "ok": True, "status": 200, "changed": True} for n in nums])
        for n in nums:
            got = self.admin.get("/tickets/" + n).json
            self.assertEqual((got["priority"], got["assignment_group"], got["assigned_to"]), (1, "SOC L2", "analyst2"))
            journal = self.admin.get("/tickets/%s/journal" % n).json
            fields = sorted(e["field"] for e in journal if e["type"] == "audit" and e["field"] != "created")
            self.assertEqual(fields, ["assigned_to", "assignment_group", "priority"])
        self.assertEqual(sorted(e["object_id"] for e in self.audit.find("ticket.update")), sorted(nums))
        summary = self.audit.find("ticket.bulk_update")
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["details"]["count"], 3)
        self.assertEqual(summary[0]["details"]["ok"], 3)
        self.assertEqual(summary[0]["details"]["failed"], 0)
        self.assertEqual(summary[0]["details"]["fields"], ["assigned_to", "assignment_group", "priority"])
        self.assertEqual(summary[0]["outcome"], "success")
        # unchanged tickets are ok, but changed=false and no new event
        self.audit.clear()
        resp = self.bulk(nums, {"priority": 1})
        self.assertEqual([r["changed"] for r in resp.json["results"]], [False] * 3)
        self.assertEqual(self.audit.find("ticket.update"), [])

    def test_partial_failures_do_not_stop_others(self):
        ok = self.create_ticket()
        closed = self.create_ticket()
        self.admin.patch("/tickets/" + closed["number"], {"state": "cancelled"})
        self.audit.clear()
        resp = self.bulk([closed["number"], "TKT9999999", ok["number"]], {"state": "in_progress"})
        self.assertEqual(resp.status, 200)
        r = resp.json["results"]
        self.assertEqual([x["ok"] for x in r], [False, False, True])
        self.assertEqual(r[0]["status"], 409)
        self.assertIn("transition cancelled -> in_progress", r[0]["error"])
        self.assertEqual(r[1]["status"], 404)
        self.assertEqual(self.admin.get("/tickets/" + ok["number"]).json["state"], "in_progress")
        summary = self.audit.find("ticket.bulk_update")[0]
        self.assertEqual((summary["details"]["ok"], summary["details"]["failed"]), (1, 2))
        self.assertEqual(summary["details"]["failures"],
                         [{"number": closed["number"], "status": 409}, {"number": "TKT9999999", "status": 404}])
        self.assertEqual(len(self.audit.find("ticket.state_change")), 1)

    def test_unexpected_error_for_one_ticket_does_not_stop_others(self):
        a, b, c = (self.create_ticket(priority=3) for _ in range(3))
        real = self.app.api._apply_patch

        def flaky(ctx, number, body):
            if number == b["number"]:
                raise RuntimeError("boom secret-detail")
            return real(ctx, number, body)

        self.audit.clear()
        stderr = io.StringIO()
        with unittest.mock.patch.object(self.app.api, "_apply_patch", side_effect=flaky), \
                contextlib.redirect_stderr(stderr):
            resp = self.bulk([a["number"], b["number"], c["number"]], {"priority": 1})
        self.assertEqual(resp.status, 200, resp)
        self.assertEqual(resp.json["results"], [
            {"number": a["number"], "ok": True, "status": 200, "changed": True},
            {"number": b["number"], "ok": False, "status": 500, "error": "internal error"},
            {"number": c["number"], "ok": True, "status": 200, "changed": True}])
        self.assertNotIn("boom", resp.raw.decode())
        self.assertNotIn("Traceback", resp.raw.decode())
        self.assertIn("RuntimeError", stderr.getvalue())
        self.assertEqual([self.admin.get("/tickets/" + t["number"]).json["priority"] for t in (a, b, c)], [1, 3, 1])
        summary = self.audit.find("ticket.bulk_update")[0]
        self.assertEqual((summary["details"]["ok"], summary["details"]["failed"]), (2, 1))
        self.assertEqual(summary["details"]["failures"], [{"number": b["number"], "status": 500}])

    def test_close_notes_required_for_resolve(self):
        a, b = self.create_ticket(), self.create_ticket()
        resp = self.bulk([a["number"], b["number"]], {"state": "resolved"})
        self.assertEqual([x["status"] for x in resp.json["results"]], [422, 422])
        self.assertIn("close_notes", resp.json["results"][0]["error"])
        resp = self.bulk([a["number"], b["number"]], {"state": "resolved", "close_notes": "bulk fp"})
        self.assertEqual([x["ok"] for x in resp.json["results"]], [True, True])
        for t in (a, b):
            got = self.admin.get("/tickets/" + t["number"]).json
            self.assertEqual((got["state"], got["close_notes"]), ("resolved", "bulk fp"))
            self.assertIsNotNone(got["resolved_at"])

    def test_permissions_per_ticket(self):
        l1 = self.client_for("bulk_l1")
        own = self.create_ticket(assignment_group="SOC L1")
        foreign = self.create_ticket(assignment_group="SOC L2")
        self.audit.clear()
        resp = self.bulk([own["number"], foreign["number"]], {"priority": 1}, client=l1)
        self.assertEqual(resp.status, 200)
        r = resp.json["results"]
        self.assertEqual((r[0]["ok"], r[1]["ok"], r[1]["status"]), (True, False, 403))
        self.assertEqual(self.admin.get("/tickets/" + foreign["number"]).json["priority"], 3)
        denied = self.audit.find("access.denied", object_id=foreign["number"])
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["actor"], "bulk_l1")
        # analyst_l1 lacks tickets.close: closing transitions fail per ticket, others work
        resp = self.bulk([own["number"]], {"state": "cancelled"}, client=l1)
        self.assertEqual(resp.json["results"][0]["status"], 403)
        self.assertIn("tickets.close", resp.json["results"][0]["error"])
        resp = self.bulk([own["number"]], {"state": "on_hold"}, client=l1)
        self.assertTrue(resp.json["results"][0]["ok"])
        # read_only may not change anything
        resp = self.bulk([own["number"]], {"priority": 2}, client=self.client_for("bulk_ro"))
        self.assertEqual(resp.json["results"][0]["status"], 403)
        summary = self.audit.find("ticket.bulk_update", actor="bulk_ro")[0]
        self.assertEqual(summary["outcome"], "failure")

    def test_requester_may_only_cancel_own(self):
        req = self.client_for("requester1")
        mine = self.create_ticket(client=req)
        other = self.create_ticket()
        resp = self.bulk([mine["number"], other["number"]], {"state": "cancelled"}, client=req)
        r = resp.json["results"]
        self.assertTrue(r[0]["ok"])
        self.assertEqual(r[1]["status"], 403)
        resp = self.bulk([mine["number"]], {"priority": 1}, client=req)
        self.assertEqual(resp.json["results"][0]["status"], 403)

    def test_tenant_isolation(self):
        assert self.admin.post("/tenants", {"slug": "bulk-other", "name": "Other", "seed": False}).status == 201
        other = self.admin.post("/tickets", {"short_description": "other tenant"},
                                headers={"X-MyDemoTicket-Tenant": "bulk-other"}).json
        resp = self.bulk([other["number"]], {"priority": 1})
        self.assertEqual(resp.json["results"][0]["status"], 404)


class BulkWebhookTest(ServerTestCase):
    def setUp(self):
        self.soar = SoarMock()
        self.generic = Receiver()
        self.hook = self.admin.post("/webhooks", {"name": "soar", "type": "soar", "url": self.soar.url + "/",
                                                  "secret": "soar-token-placeholder", "soar_label": "demo_tickets",
                                                  "events": ALL_EVENTS}).json
        self.generic_hook = self.admin.post("/webhooks", {"url": self.generic.url, "events": ALL_EVENTS}).json

    def tearDown(self):
        self.admin.delete("/webhooks/%d" % self.hook["id"])
        self.admin.delete("/webhooks/%d" % self.generic_hook["id"])
        self.soar.close()
        self.generic.close()

    def events(self):
        return [json.loads(r["body"]) for r in self.generic.requests]

    def test_one_event_per_changed_ticket(self):
        a, b, c = self.create_ticket(priority=3), self.create_ticket(priority=3), self.create_ticket(priority=1)
        self.wait_webhooks()
        before = len(self.events())
        artifacts_before = len(self.soar.requests("/rest/artifact"))
        resp = self.admin.post("/tickets/bulk", {"numbers": [a["number"], b["number"], c["number"]],
                                                 "changes": {"priority": 1}},
                               headers={"X-MyDemoTicket-Source": "splunk-es"})
        self.assertEqual(resp.status, 200)
        self.wait_webhooks()
        new = self.events()[before:]
        self.assertEqual(sorted((e["event"], e["ticket"]["number"]) for e in new),
                         sorted([("ticket.updated", a["number"]), ("ticket.updated", b["number"])]))
        self.assertEqual({e["source"] for e in new}, {"splunk-es"})
        self.assertEqual(new[0]["changes"]["priority"], {"old": 3, "new": 1})
        # SOAR got one artifact per changed ticket, in the ticket's own container
        artifacts = self.soar.requests("/rest/artifact")[artifacts_before:]
        self.assertEqual(sorted(x["body"]["cef"]["ticketNumber"] for x in artifacts),
                         sorted([a["number"], b["number"]]))

    def test_state_change_event_and_soar_source_skipped(self):
        a, b = self.create_ticket(), self.create_ticket()
        self.wait_webhooks()
        before = len(self.events())
        artifacts_before = len(self.soar.requests("/rest/artifact"))
        resp = self.client_for("soar").post(
            "/tickets/bulk", {"numbers": [a["number"], b["number"]], "changes": {"state": "in_progress"}},
            headers={"X-MyDemoTicket-Source": "soar"})
        self.assertEqual([x["ok"] for x in resp.json["results"]], [True, True])
        self.wait_webhooks()
        new = self.events()[before:]
        self.assertEqual([e["event"] for e in new], ["ticket.state_changed"] * 2)
        self.assertEqual({e["source"] for e in new}, {"soar"})
        self.assertEqual(len(self.soar.requests("/rest/artifact")), artifacts_before)
        log = self.admin.get("/webhooks/%d/deliveries" % self.hook["id"]).json
        skipped = [d for d in log if d["status"] == "skipped"]
        self.assertEqual(sorted(d["ticket_number"] for d in skipped), sorted([a["number"], b["number"]]))
        journal = self.admin.get("/tickets/%s/journal" % a["number"]).json
        self.assertEqual([e["source"] for e in journal if e.get("field") == "state"], ["soar"])


if __name__ == "__main__":
    unittest.main()
