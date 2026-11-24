"""external_ref.<key> list filter and the optional comment author."""

import json
import sqlite3
import time
import unittest
import urllib.parse

from helpers import Receiver, ServerTestCase

from mydemoticket import db as dbmod


class ExternalRefFilterTest(ServerTestCase):
    def setUp(self):
        self.marker = "extref-%d" % time.time_ns()

    def make(self, refs, client=None, **fields):
        return self.create_ticket(client, short_description=self.marker, external_refs=refs, **fields)

    def numbers(self, query, client=None):
        resp = (client or self.admin).get("/tickets?q=%s&sort=number&%s" % (self.marker, query))
        self.assertEqual(resp.status, 200, resp)
        self.assertEqual(resp.json["total"], len(resp.json["items"]))
        return [t["number"] for t in resp.json["items"]]

    def test_exact_string_match_number_as_string(self):
        num = self.make({"es_finding_id": 1133})["number"]
        txt = self.make({"es_finding_id": "1133"})["number"]
        self.make({"es_finding_id": "11330"})
        self.make({"es_finding_id": " 1133"})
        self.make({"other": "1133"})
        self.assertEqual(self.numbers("external_ref.es_finding_id=1133"), [num, txt])
        self.assertEqual(self.numbers("external_ref.es_finding_id=113"), [])

    def test_other_scalar_types(self):
        flt = self.make({"score": 1.5})["number"]
        yes = self.make({"flag": True})["number"]
        no = self.make({"flag": False})["number"]
        empty = self.make({"name": ""})["number"]
        self.assertEqual(self.numbers("external_ref.score=1.5"), [flt])
        self.assertEqual(self.numbers("external_ref.flag=true"), [yes])
        self.assertEqual(self.numbers("external_ref.flag=false"), [no])
        self.assertEqual(self.numbers("external_ref.flag=1"), [])
        self.assertEqual(self.numbers("external_ref.name="), [empty])

    def test_special_characters_are_data(self):
        value = "a'b\"c%_\\x ü"
        t = self.make({"weird": value})["number"]
        self.make({"weird": "a'b"})
        self.assertEqual(self.numbers("external_ref.weird=" + urllib.parse.quote(value)), [t])
        self.assertEqual(self.numbers("external_ref.weird=%25"), [])

    def test_multiple_filters_are_and_and_combine(self):
        both = self.make({"es_finding_id": "f1", "soar_container_id": 7}, assignment_group="SOC L1")["number"]
        self.make({"es_finding_id": "f1", "soar_container_id": 8})
        self.make({"es_finding_id": "f2", "soar_container_id": 7})
        self.assertEqual(self.numbers("external_ref.es_finding_id=f1&external_ref.soar_container_id=7"), [both])
        self.assertEqual(self.numbers("external_ref.es_finding_id=f1&assignment_group=SOC%20L1"), [both])
        self.assertEqual(self.numbers("external_ref.es_finding_id=f1&state=closed"), [])
        resp = self.admin.get("/tickets?q=%s&external_ref.es_finding_id=f1&limit=1&sort=number" % self.marker)
        self.assertEqual(resp.json["total"], 2)
        self.assertEqual(len(resp.json["items"]), 1)

    def test_invalid_keys_are_422(self):
        for key in ("", "Upper", "with-dash", "dot.ted", "x" * 65, "sp%20ace", "%27"):
            with self.subTest(key=key):
                resp = self.admin.get("/tickets?external_ref.%s=1" % key)
                self.assertEqual(resp.status, 422, resp)
        self.assertEqual(self.admin.get("/tickets?external_ref.%s=1" % ("x" * 64)).status, 200)

    def test_requester_only_sees_own(self):
        req = self.client_for("requester1")
        own = self.create_ticket(req, short_description=self.marker)["number"]
        self.admin.patch("/tickets/" + own, {"external_refs": {"k": "v"}})
        self.make({"k": "v"})
        self.assertEqual(self.numbers("external_ref.k=v", client=req), [own])


class ExternalRefTenantTest(ServerTestCase):
    def test_only_own_tenant(self):
        resp = self.admin.post("/tenants", {"slug": "acme", "name": "ACME", "seed": True})
        self.assertEqual(resp.status, 201, resp)
        acme_headers = {"X-MyDemoTicket-Tenant": "acme"}
        self.admin.post("/users", {"username": "acme-int", "role": "integration", "tenant": "acme",
                                   "password": "acme-password-123"}, headers=acme_headers)
        acme = self.client_for("acme-int", "acme")
        own = self.create_ticket(external_refs={"es_finding_id": "shared-1"})["number"]
        theirs = self.create_ticket(acme, external_refs={"es_finding_id": "shared-1"})["number"]
        mine = self.admin.get("/tickets?external_ref.es_finding_id=shared-1").json
        self.assertEqual(([t["number"] for t in mine["items"]], mine["total"]), ([own], 1))
        other = acme.get("/tickets?external_ref.es_finding_id=shared-1").json
        self.assertEqual(([t["number"] for t in other["items"]], other["total"]), ([theirs], 1))


class JsonFallbackTest(unittest.TestCase):
    """The Python stand-ins must give the same answers as SQLite's JSON1."""

    DOCS = ['{"a": 1133}', '{"a": "1133"}', '{"a": 1.5}', '{"a": true}', '{"a": false}', '{"a": null}',
            '{"a": ""}', '{"b": 1}', '{}', 'not json']

    def test_same_results_as_builtin(self):
        from mydemoticket.api import _REF_TEXT_SQL

        builtin = sqlite3.connect(":memory:")
        self.assertTrue(dbmod.ensure_json_functions(builtin))
        fallback = sqlite3.connect(":memory:")
        fallback.create_function("json_extract", 2, dbmod._py_json_extract)
        fallback.create_function("json_type", 2, dbmod._py_json_type)
        sql = "SELECT " + _REF_TEXT_SQL.replace("external_refs", "?")
        for doc in self.DOCS[:-1]:
            with self.subTest(doc=doc):
                params = (doc, '$."a"', doc, '$."a"')
                self.assertEqual(fallback.execute(sql, params).fetchone(), builtin.execute(sql, params).fetchone())
        self.assertEqual(fallback.execute(sql, ("not json", '$."a"', "not json", '$."a"')).fetchone(), (None,))


class CommentAuthorTest(ServerTestCase):
    use_recorder = True

    def test_author_stored_and_returned(self):
        soar = self.client_for("soar")
        t = self.create_ticket()
        resp = soar.post("/tickets/%s/comments" % t["number"], {"body": "Hi\nthere", "author": "  jdoe@es  "})
        self.assertEqual(resp.status, 201, resp)
        self.assertEqual((resp.json["author"], resp.json["actor"], resp.json["body"]), ("jdoe@es", "soar", "Hi\nthere"))
        plain = self.admin.post("/tickets/%s/comments" % t["number"], {"body": "no author"})
        self.assertIsNone(plain.json["author"])
        null = self.admin.post("/tickets/%s/comments" % t["number"], {"body": "null author", "author": None})
        self.assertEqual(null.status, 201)
        self.assertIsNone(null.json["author"])
        journal = self.admin.get("/tickets/%s/journal" % t["number"]).json
        self.assertTrue(all("author" in e for e in journal))
        self.assertEqual([e.get("author") for e in journal if e["type"] != "audit"], ["jdoe@es", None, None])
        row = self.app.db.query_one("SELECT actor, author FROM journal WHERE id = ?", (resp.json["id"],))
        self.assertEqual((row["actor"], row["author"]), ("soar", "jdoe@es"))
        self.audit.wait_idle()
        events = self.audit.find("ticket.comment", object_id=t["number"])
        self.assertEqual([e["details"]["author"] for e in events], ["jdoe@es", None, None])

    def test_author_validation(self):
        t = self.create_ticket()
        for bad in ("", "   ", "x" * 65, "a\nb", "a\tb", "a\x00b", "a\x7fb", "a\x85b", 5, True, ["x"], {"a": 1}):
            with self.subTest(author=bad):
                resp = self.admin.post("/tickets/%s/comments" % t["number"], {"body": "x", "author": bad})
                self.assertEqual(resp.status, 422, resp)
        ok = self.admin.post("/tickets/%s/comments" % t["number"], {"body": "x", "author": " " + "y" * 64 + " "})
        self.assertEqual(ok.status, 201, ok)
        self.assertEqual(ok.json["author"], "y" * 64)
        journal = self.admin.get("/tickets/%s/journal" % t["number"]).json
        self.assertEqual(len([e for e in journal if e["type"] == "comment"]), 1)

    def test_webhook_and_soar_artifact_carry_author(self):
        generic, soar_rx = Receiver(), Receiver(lambda path, body: (200, {"id": 42}))
        try:
            self.admin.post("/webhooks", {"url": generic.url, "events": ["ticket.commented"]})
            self.admin.post("/webhooks", {"type": "soar", "url": soar_rx.url, "secret": "s",
                                          "events": ["ticket.commented"]})
            t = self.create_ticket()
            self.admin.post("/tickets/%s/comments" % t["number"], {"body": "from ES", "author": "jdoe"})
            self.admin.post("/tickets/%s/comments" % t["number"], {"body": "plain"})
            self.wait_webhooks()
            bodies = [json.loads(r["body"]) for r in generic.requests]
            self.assertEqual([(b["comment"]["author"], b["actor"]) for b in bodies], [("jdoe", "admin"), (None, "admin")])
            artifacts = [json.loads(r["body"]) for r in soar_rx.requests if r["path"].endswith("/rest/artifact")]
            self.assertEqual([(a["cef"]["comment"], a["cef"]["commentAuthor"]) for a in artifacts],
                             [("from ES", "jdoe"), ("plain", "")])
        finally:
            generic.close()
            soar_rx.close()


if __name__ == "__main__":
    unittest.main()
