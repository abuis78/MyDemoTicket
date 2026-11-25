"""Web UI translations (static/i18n.js): DE/EN key parity, every used key exists, no hard-coded UI texts."""

import json
import os
import re
import unittest

from helpers import ServerTestCase

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mydemoticket", "static")


def read(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as handle:
        return handle.read()


def strip_comments(js):
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    # line comments start with "// " (URLs like https://x have no space after the slashes)
    return re.sub(r"(^|\s)// [^\n]*", r"\1", js)


def parse_dict(js, name):
    """Extracts `var <name> = { "key": "text", ... };` from i18n.js as a Python dict."""
    match = re.search(r"var %s = \{(.*?)\n  \};" % name, js, flags=re.S)
    assert match, "dictionary %s not found" % name
    body = strip_comments(match.group(1))
    pairs = re.findall(r'"((?:[^"\\]|\\.)*)"\s*:\s*"((?:[^"\\]|\\.)*)"', body)
    result = {}
    for key, value in pairs:
        assert key not in result, "duplicate key %s in %s" % (key, name)
        result[key] = json.loads('"%s"' % value)
    return result


class I18nDictionaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.i18n = read("i18n.js")
        cls.app = strip_comments(read("app.js"))
        cls.html = read("index.html")
        cls.de = parse_dict(cls.i18n, "de")
        cls.en = parse_dict(cls.i18n, "en")

    def test_both_languages_have_identical_keys(self):
        self.assertGreater(len(self.de), 200)
        self.assertEqual(sorted(set(self.de) - set(self.en)), [], "keys only in de")
        self.assertEqual(sorted(set(self.en) - set(self.de)), [], "keys only in en")

    def test_no_empty_texts_and_placeholders_match(self):
        for key in self.de:
            self.assertTrue(self.de[key].strip(), "empty de text for %s" % key)
            self.assertTrue(self.en[key].strip(), "empty en text for %s" % key)
            self.assertEqual(sorted(re.findall(r"\{(\w+)\}", self.de[key])),
                             sorted(re.findall(r"\{(\w+)\}", self.en[key])), "placeholders differ for %s" % key)

    def test_every_literal_key_used_in_app_js_exists(self):
        used = set(re.findall(r'\bt\("([a-z0-9_.]*[a-z0-9_])"', self.app))  # "prefix." + x: see below
        # key constants passed to t() later (column headers, error texts)
        used |= set(re.findall(r'"((?:col|error)\.[a-z0-9_.]+)"', self.app))
        self.assertGreater(len(used), 150)
        missing = sorted(k for k in used if k not in self.en)
        self.assertEqual(missing, [], "keys used in app.js but missing in i18n.js")

    def test_dynamic_key_groups_are_complete(self):
        prefixes = set(re.findall(r'\bt\("([a-z0-9_.]+\.)" \+', self.app))
        prefixes |= set(re.findall(r'\blabel\("([a-z0-9_.]+\.)"', self.app))
        self.assertIn("state.", prefixes)
        for prefix in prefixes:
            self.assertTrue(any(k.startswith(prefix) for k in self.en), "no key for group %s" % prefix)
        groups = {
            "state.": ["new", "in_progress", "on_hold", "resolved", "closed", "cancelled"],
            "action.": ["in_progress", "on_hold", "resolved", "closed", "cancelled"],
            "priority.": ["1", "2", "3", "4"],
            "journal.type.": ["work_note", "comment", "summary", "audit"],
            "role.": ["platform_admin", "admin", "agent", "requester", "integration",
                      "analyst_l1", "analyst_l2", "soc_lead", "read_only"],
            "lang.": ["de", "en"],
            "fields.type.": ["string", "number", "boolean", "choice", "date"],
            "reports.range.": ["7", "30", "90", "all"],
        }
        for prefix, names in groups.items():
            for name in names:
                self.assertIn(prefix + name, self.en, prefix + name)

    def test_index_html_keys_exist_and_i18n_loads_first(self):
        keys = re.findall(r'data-i18n(?:-[a-z-]+)?="([^"]+)"', self.html)
        self.assertGreater(len(keys), 20)
        for key in keys:
            self.assertIn(key, self.en, key)
        # every visible sidebar link and header text is translated at runtime
        for anchor in re.findall(r"<a [^>]*>", self.html):
            self.assertIn("data-i18n", anchor, anchor)
        self.assertLess(self.html.index('src="/static/i18n.js"'), self.html.index('src="/static/app.js"'))

    def test_german_texts_are_really_german(self):
        same = [k for k in self.de if self.de[k] == self.en[k] and re.search(r"[a-z]{4,} [a-z]{4,}", self.en[k])]
        self.assertEqual(same, [], "untranslated sentences")
        self.assertEqual(self.de["nav.users"], "Benutzer")
        self.assertEqual(self.en["nav.users"], "Users")

    def test_app_js_has_no_hard_coded_english_ui_text(self):
        allowed = {"use strict", "Accept", "Content-Type", "Home", "End"}
        offenders = []
        for match in re.finditer(r'"((?:[^"\\]|\\.)*)"', self.app):
            text = match.group(1)
            before = self.app[max(0, match.start() - 14):match.start()]
            after = self.app[match.end():match.end() + 3]
            if text in allowed or "://" in text:
                continue
            if re.search(r"(class|type|role|autocomplete|id|for):\s*$", before) or re.search(r"className = $", before):
                continue  # attribute values, not texts
            if re.search(r"(===|!==)\s*$", before) or re.match(r"\s*:", after):
                continue  # comparisons with / keys of server messages
            capitalised = re.match(r"[A-Z][a-z]+([ ,.:!?]|$)", text)
            sentence = re.search(r"[A-Za-z]{2,} [A-Za-z]{2,}", text) and not re.search(r"[(#.\[]", text)
            if capitalised or sentence:
                offenders.append(text)
        self.assertEqual(offenders, [], "hard-coded UI texts in app.js; use t()")
        # visible attributes and messages always go through t()
        for pattern in (r'\btext: "[^"]*[A-Za-z]', r'\bplaceholder: "(?!https://)[^"]*[A-Za-z]',
                        r'\btitle: "[^"]*[A-Za-z]', r'"aria-label": "[^"]*[A-Za-z]',
                        r'toast\("', r'confirm\("', r'prompt\("'):
            self.assertIsNone(re.search(pattern, self.app), pattern)

    def test_i18n_js_has_no_dangerous_dom(self):
        code = strip_comments(self.i18n)
        for sink in (r"\.innerHTML\b", r"\.outerHTML\b", r"insertAdjacentHTML\(", r"document\.write",
                     r"\beval\(", r"new Function\b"):
            self.assertIsNone(re.search(sink, code), sink)
        self.assertIn("el.textContent = text", code)
        self.assertIn("window.MDT_I18N = I18N", code)
        self.assertIn('STORAGE_KEY = "mdt.language"', code)
        self.assertIn("navigator.language", code)


class I18nServedTest(ServerTestCase):
    seed = False

    def test_i18n_js_is_served(self):
        resp = self.admin.get("/static/i18n.js")
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.headers["content-type"].startswith("text/javascript"))
        self.assertIn(b"window.MDT_I18N", resp.raw)


if __name__ == "__main__":
    unittest.main()
