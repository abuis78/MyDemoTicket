import json
import os
import py_compile
import re
import sys
import tarfile
import tempfile
import unittest

import support

import mydemoticket_connector

MANIFEST = os.path.join(support.APP_DIR, "mydemoticket.json")


def connector_action_ids():
    with open(mydemoticket_connector.__file__) as fh:
        src = fh.read()
    block = src.split("handlers = {", 1)[1].split("}", 1)[0]
    return set(re.findall(r'"([a-z_]+)":\s*self\._handle_', block))


class TestManifest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(MANIFEST) as f:
            cls.app = json.load(f)

    def test_required_metadata(self):
        a = self.app
        self.assertRegex(a["appid"], r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        self.assertEqual(a["name"], "MyDemoTicket")
        self.assertEqual(a["product_vendor"], "SOC Games Demo")
        self.assertEqual(a["product_name"], "MyDemoTicket")
        self.assertEqual(a["type"], "ticketing")
        self.assertEqual(a["min_phantom_version"], "6.0.0")
        self.assertEqual(a["main_module"], "mydemoticket_connector.py")
        self.assertTrue(a["python_version"].startswith("3"))
        for key in ("main_module", "logo", "logo_dark"):
            self.assertTrue(os.path.isfile(os.path.join(support.APP_DIR, a[key])), key)

    def test_configuration(self):
        c = self.app["configuration"]
        self.assertEqual(c["api_token"]["data_type"], "password")
        self.assertIs(c["verify_server_cert"]["default"], True)
        self.assertEqual(c["verify_server_cert"]["data_type"], "boolean")
        self.assertEqual(c["source_tag"]["default"], "soar")
        self.assertEqual(c["ingest_label"]["default"], "mydemoticket")
        self.assertFalse(c["poll_state_filter"]["required"])
        self.assertTrue(c["base_url"]["required"])

    def test_actions_match_connector(self):
        manifest_ids = [a["identifier"] for a in self.app["actions"]]
        self.assertEqual(len(manifest_ids), len(set(manifest_ids)))
        self.assertEqual(set(manifest_ids), connector_action_ids())
        self.assertEqual(len(manifest_ids), 10)

    def test_action_schema(self):
        for a in self.app["actions"]:
            for key in ("action", "identifier", "description", "type", "read_only", "parameters", "output", "versions"):
                self.assertIn(key, a, "{} missing {}".format(a.get("identifier"), key))
            orders = [p["order"] for p in a["parameters"].values()]
            self.assertEqual(sorted(orders), list(range(len(orders))), a["identifier"])
            for o in a["output"]:
                self.assertTrue(o["data_path"].startswith(("action_result.", "summary.")), o)
            for pname in a["parameters"]:
                if a["output"]:
                    self.assertIn("action_result.parameter." + pname, [o["data_path"] for o in a["output"]])
        types = {a["identifier"]: a["type"] for a in self.app["actions"]}
        self.assertEqual(types["test_connectivity"], "test")
        self.assertEqual(types["on_poll"], "ingest")

    def test_no_secrets_in_manifest(self):
        self.assertNotIn("default", self.app["configuration"]["api_token"])


class TestBuild(unittest.TestCase):
    def test_build_creates_tgz_with_all_files(self):
        sys.path.insert(0, support.SOAR_APP_DIR)
        try:
            import build
        finally:
            sys.path.remove(support.SOAR_APP_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            path = build.build(os.path.join(tmp, "mydemoticket.tgz"))
            with tarfile.open(path, "r:gz") as tar:
                names = set(tar.getnames())
        expected = {"mydemoticket/" + f for f in build.APP_FILES} | {"mydemoticket"}
        self.assertEqual(names, expected)
        self.assertTrue(all(n == "mydemoticket" or n.startswith("mydemoticket/") for n in names))


class TestPlaybooksCompile(unittest.TestCase):
    def test_py_compile(self):
        files = [f for f in os.listdir(support.PLAYBOOK_DIR) if f.endswith(".py")]
        self.assertEqual(sorted(files), ["mydemoticket_es_mirror.py", "mydemoticket_ticket_from_es.py"])
        with tempfile.TemporaryDirectory() as tmp:
            for f in files:
                py_compile.compile(os.path.join(support.PLAYBOOK_DIR, f), cfile=os.path.join(tmp, f + "c"),
                                   doraise=True)
                json_path = os.path.join(support.PLAYBOOK_DIR, f[:-3] + ".json")
                with open(json_path) as fh:
                    json.load(fh)

    def test_connector_py39_syntax(self):
        import ast
        for f in ("mydemoticket_connector.py", "mydemoticket_consts.py"):
            with open(os.path.join(support.APP_DIR, f)) as fh:
                ast.parse(fh.read(), feature_version=(3, 9))


if __name__ == "__main__":
    unittest.main()
