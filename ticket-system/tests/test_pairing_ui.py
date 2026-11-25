"""Pairing page (static/pairing.js): wiring into app.js/index.html, DE/EN texts, no hard-coded texts or HTML sinks."""

import re
import unittest

from helpers import ServerTestCase
from test_i18n import parse_dict, read, strip_comments

from mydemoticket.pairing import STEPS


class PairingUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = strip_comments(read("pairing.js"))
        cls.app = read("app.js")
        cls.html = read("index.html")
        i18n = read("i18n.js")
        cls.de = parse_dict(i18n, "de")
        cls.en = parse_dict(i18n, "en")

    def test_every_key_exists_in_both_languages(self):
        used = set(re.findall(r'\bt\("([a-z0-9_.]*[a-z0-9_])"', self.js))
        self.assertGreater(len(used), 30)
        for key in sorted(used):
            self.assertIn(key, self.de, key)
            self.assertIn(key, self.en, key)
        for name in STEPS:
            self.assertIn("pairing.step." + name, self.en)
            self.assertIn('"%s"' % name, self.js)
        for check in ("connection", "splunk_asset", "es_asset", "app", "label"):
            self.assertIn("pairing.check." + check, self.en)
        for status in ("created", "updated", "exists", "skipped", "failed", "ok", "info"):
            self.assertIn("pairing.status." + status, self.de)
        self.assertEqual(self.de["nav.pairing"], "Kopplung ES + SOAR")
        self.assertIn("perm.pairing.manage", self.de)

    def test_no_hard_coded_texts_and_no_html_sinks(self):
        for pattern in (r'\btext: "[^"]*[A-Za-z]', r'\bplaceholder: "(?!https://)[^"]*[A-Za-z]',
                        r'\btitle: "[^"]*[A-Za-z]', r'"aria-label": "[^"]*[A-Za-z]', r'toast\("', r'confirm\("'):
            self.assertIsNone(re.search(pattern, self.js), pattern)
        for sink in (r"\.innerHTML\b", r"\.outerHTML\b", r"insertAdjacentHTML\(", r"document\.write",
                     r"\beval\(", r"new Function\b", r"localStorage", r"console\."):
            self.assertIsNone(re.search(sink, self.js), sink)

    def test_token_field_is_a_password_and_is_cleared(self):
        self.assertRegex(self.js, r'id: "pa-token", type: "password", autocomplete: "new-password"')
        self.assertIn('token.value = "";', self.js)
        self.assertIn('kit.api("POST", "/pairing/check"', self.js)
        self.assertIn('kit.api("POST", "/pairing/apply"', self.js)
        self.assertIn('kit.api("GET", "/pairing")', self.js)

    def test_wired_into_app_and_index(self):
        self.assertIn('"/admin/pairing": "pairing.manage"', self.app)
        self.assertIn('window.MDT_PAIRING.view(pageKit())', self.app)
        self.assertIn('href="#/admin/pairing" data-nav="pairing" data-perm="pairing.manage"', self.html)
        self.assertLess(self.html.index('src="/static/i18n.js"'), self.html.index('src="/static/pairing.js"'))
        self.assertLess(self.html.index('src="/static/pairing.js"'), self.html.index('src="/static/app.js"'))
        self.assertIn("window.MDT_PAIRING = {", self.js)


class PairingJsServedTest(ServerTestCase):
    seed = False

    def test_pairing_js_is_served(self):
        resp = self.admin.get("/static/pairing.js")
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.headers["content-type"].startswith("text/javascript"))


if __name__ == "__main__":
    unittest.main()
