"""The web UI's assumptions about the API match the real backend (found by the end-to-end UI smoke run)."""

import os
import re
import unittest

import helpers  # noqa: F401  (puts the package on sys.path)

from mydemoticket.auth import BUILTIN_ROLE_PERMISSIONS, PERMISSIONS

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mydemoticket", "static")


def read(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as handle:
        return handle.read()


def js_list(text):
    return re.findall(r'"([a-z_]+\.[a-z_]+)"', text)


class FallbackPermissionsTest(unittest.TestCase):
    """FALLBACK_PERMISSIONS in app.js (used when /me has no permissions) equals auth.BUILTIN_ROLE_PERMISSIONS."""

    @classmethod
    def setUpClass(cls):
        js = read("app.js")
        all_block = re.search(r"var ALL_PERMISSIONS = \[(.*?)\];", js, re.S).group(1)
        cls.all = js_list(all_block)
        block = re.search(r"var FALLBACK_PERMISSIONS = \{(.*?)\n  \};", js, re.S).group(1)
        cls.map = {}
        for name, value in re.findall(r"\n    ([a-z_0-9]+): (.*?)(?=,\n    [a-z_0-9]+: |$)", block, re.S):
            if value.strip() == "ALL_PERMISSIONS":
                perms = list(cls.all)
            elif value.startswith("ALL_PERMISSIONS.filter"):
                excluded = re.findall(r'p !== "([a-z_.]+)"', value)
                perms = [p for p in cls.all if p not in excluded]
            else:
                perms = js_list(value)
            cls.map[name] = perms

    def test_catalog_matches(self):
        self.assertEqual(sorted(self.all), sorted(PERMISSIONS))

    def test_every_builtin_role_matches_the_server(self):
        self.assertEqual(sorted(self.map), sorted(BUILTIN_ROLE_PERMISSIONS))
        for role, perms in BUILTIN_ROLE_PERMISSIONS.items():
            self.assertEqual(sorted(self.map[role]), sorted(perms), role)


if __name__ == "__main__":
    unittest.main()
