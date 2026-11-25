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
        for path in ("/", "/static/i18n.js", "/static/app.js", "/static/app.css"):
            resp = cls.admin.get(path)
            cls.files[path] = resp

    def text(self, path):
        return self.files[path].raw.decode("utf-8")

    def test_files_are_served_with_types(self):
        expected = {"/": "text/html", "/static/i18n.js": "text/javascript", "/static/app.js": "text/javascript",
                    "/static/app.css": "text/css"}
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
        self.assertIn('src="/static/i18n.js"', html)
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
        self.assertIn('e.author ? t("journal.via", { author: e.author, actor: e.actor }) : e.actor', js)
        i18n = self.text("/static/i18n.js")
        self.assertIn('"journal.via": "{author} (via {actor})"', i18n)
        # the comment form never sends an author: UI users are real users
        self.assertIn("{ body: text.value, type: kind })", js)
        self.assertNotRegex(js, r'/comments", \{[^}]*author')
        css = self.text("/static/app.css")
        self.assertRegex(css, r"\.journal \.body \{[^}]*white-space: pre-wrap")

    def test_ticket_detail_has_accessible_journal_tabs(self):
        js = self.text("/static/app.js")
        i18n = self.text("/static/i18n.js")
        for needle in ('role: "tablist"', 'role: "tab"', 'role: "tabpanel"', '"aria-selected"', '"aria-controls"',
                       '"aria-labelledby"', '"aria-label": t("tabs.label")', '"ArrowRight"', '"ArrowLeft"',
                       "tab.tabIndex = on ? 0 : -1", "history.replaceState(", '"?tab=" + id',
                       "parseHash().params.tab"):
            self.assertIn(needle, js)
        # tab order and titles with counters; work notes only for non-requesters (first tab = default)
        order = [js.index(s) for s in ('id: "worknotes"', 'id: "comments"', 'id: "resolution"', 'id: "audit"',
                                       'id: "history"')]
        self.assertEqual(order, sorted(order))
        # titles with counters: the texts live in i18n.js, app.js passes the counts
        for title in ('t("tabs.worknotes", { n: notes.length })', 't("tabs.comments", { n: comments.length })',
                      't("tabs.resolution")', 't("tabs.audit", { n: audits.length })',
                      't("tabs.history", { n: journal.length })'):
            self.assertIn(title, js)
        for text in ('"tabs.label": "Ticket activity"', '"tabs.worknotes": "Work notes ({n})"',
                     '"tabs.comments": "Comments ({n})"', '"tabs.resolution": "Resolution report"',
                     '"tabs.audit": "Audit trail ({n})"', '"tabs.history": "History ({n})"'):
            self.assertIn(text, i18n)
        self.assertRegex(js, r"if \(!requester\) \{\s*defs\.push\(\{ id: \"worknotes\"")
        self.assertIn("var requester = isRequester();", js)
        for text in ('t("journal.add_work_note")', 't("journal.add_comment")', 't("report.not_resolved")',
                     't("report.earlier", { n: earlier.length })', 'h("details"', 'ofType("summary")',
                     '["col.time", "col.field", "col.old_new", "col.actor", "col.source"]'):
            self.assertIn(text, js)
        for text in ('"Add work note"', '"Add comment"', '"Not resolved yet."', '"Earlier summaries ({n})"',
                     '"col.time": "Time"', '"col.field": "Field"', '"col.old_new": "Old → New"',
                     '"col.actor": "Actor"', '"col.source": "Source"'):
            self.assertIn(text, i18n)
        css = self.text("/static/app.css")
        self.assertRegex(css, r"\.report \.body, \.pre-wrap \{[^}]*white-space: pre-wrap")
        self.assertIn("[role=tab][aria-selected=true]", css)

    def test_tenant_errors_have_readable_texts(self):
        js = self.text("/static/app.js")
        for message in ("tenant mismatch", "tenant inactive", "tenant not found"):
            self.assertIn('"%s":' % message, js)


if __name__ == "__main__":
    unittest.main()
