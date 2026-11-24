"""Web UI static files: delivery, security headers and guard rails for app.js."""

import re
import unittest

from helpers import ServerTestCase

EXPECTED_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


class StaticFilesTest(ServerTestCase):
    seed = False

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.files = {}
        for path in ("/", "/static/app.js", "/static/app.css"):
            resp = cls.admin.get(path)
            cls.files[path] = resp

    def text(self, path):
        return self.files[path].raw.decode("utf-8")

    def test_files_are_served_with_types(self):
        expected = {"/": "text/html", "/static/app.js": "text/javascript", "/static/app.css": "text/css"}
        for path, ctype in expected.items():
            resp = self.files[path]
            self.assertEqual(resp.status, 200, path)
            self.assertTrue(resp.headers["content-type"].startswith(ctype), (path, resp.headers["content-type"]))
            self.assertTrue(resp.raw, path)

    def test_csp_and_security_headers_unchanged(self):
        for path, resp in self.files.items():
            self.assertEqual(resp.headers.get("content-security-policy"), EXPECTED_CSP, path)
            self.assertEqual(resp.headers.get("x-content-type-options"), "nosniff", path)
            self.assertEqual(resp.headers.get("x-frame-options"), "DENY", path)

    def test_index_has_no_inline_code_or_external_resources(self):
        html = self.text("/")
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)[^>]*>", "inline <script> block")
        self.assertNotRegex(html, r"<style\b", "inline <style> block")
        self.assertNotRegex(html, r"\sstyle=", "inline style attribute")
        self.assertNotRegex(html, r"\son[a-z]+=", "inline event handler")
        self.assertNotRegex(html, r"(src|href)=\"(https?:)?//", "external resource")
        self.assertIn('src="/static/app.js"', html)
        self.assertIn('href="/static/app.css"', html)

    def test_index_has_tenant_switch_and_new_navigation(self):
        html = self.text("/")
        self.assertIn('id="tenant-name"', html)
        self.assertRegex(html, r'<label[^>]*for="tenant-switch"')
        self.assertIn('id="tenant-switch"', html)
        for route in ("#/reports", "#/admin/tenants", "#/admin/audit"):
            self.assertIn('href="%s"' % route, html)

    def test_app_uses_new_endpoints(self):
        js = self.text("/static/app.js")
        for endpoint in ('"/tenants"', '"/reports/summary"', '"/audit/status"', '"/auth/tenant"'):
            self.assertIn(endpoint, js)
        # writes go through api(), which adds the CSRF header for every non-GET request
        self.assertIn('headers["X-CSRF-Token"] = session.csrf', js)
        self.assertRegex(js, r'api\("POST", "/auth/tenant"')

    def test_app_has_no_dangerous_dom_or_eval(self):
        js = self.text("/static/app.js")
        # The UI has no HTML escape function on purpose: all DOM is built by h(), which only uses
        # createElement/textContent/setAttribute. So any HTML sink is forbidden outright.
        code = re.sub(r"/\*.*?\*/", "", js, flags=re.S)  # block comments may name the sinks
        for sink in (r"\.innerHTML\b", r"\.outerHTML\b", r"insertAdjacentHTML\(", r"document\.write",
                     r"\beval\(", r"new Function\b", r"setAttribute\(\s*[\"']style", r"\.cssText\b"):
            self.assertIsNone(re.search(sink, code), "forbidden sink %s in app.js" % sink)
        self.assertRegex(js, r'key === "text"\) el\.textContent = value')
        self.assertIn("document.createTextNode(String(child))", js)
        # timers only with functions, never with code strings
        self.assertIsNone(re.search(r"set(Timeout|Interval)\(\s*[\"']", js))
        self.assertNotRegex(js, r"https?://(?!soar\.example\.invalid)")

    def test_journal_shows_author_via_actor(self):
        js = self.text("/static/app.js")
        self.assertIn('e.author ? e.author + " (via " + e.actor + ")" : e.actor', js)
        # the comment form never sends an author: UI users are real users
        self.assertIn("{ body: text.value, type: kind })", js)
        self.assertNotRegex(js, r"author:\s")
        css = self.text("/static/app.css")
        self.assertRegex(css, r"\.journal \.body \{[^}]*white-space: pre-wrap")

    def test_tenant_errors_have_readable_texts(self):
        js = self.text("/static/app.js")
        for message in ("tenant mismatch", "tenant inactive", "tenant not found"):
            self.assertIn('"%s":' % message, js)


if __name__ == "__main__":
    unittest.main()
