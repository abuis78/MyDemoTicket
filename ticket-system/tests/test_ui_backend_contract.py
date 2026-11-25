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


class AppJsContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = read("app.js")

    def function_body(self, name):
        start = self.js.index("function %s(" % name)
        depth, i = 0, self.js.index("{", start)
        for i in range(i, len(self.js)):
            depth += {"{": 1, "}": -1}.get(self.js[i], 0)
            if depth == 0:
                return self.js[start:i + 1]
        raise AssertionError(name)

    def test_own_custom_role_label_comes_from_me(self):
        # users with a custom role but without roles.manage get 403 on /roles; GET /me has role_label
        self.assertIn("me.role_label", self.function_body("roleLabel"))

    def test_admin_pages_are_guarded_like_their_nav_links(self):
        # e.g. analyst_l1 may GET /users (tickets.read_all) but must not get the users admin page via URL
        html = read("index.html")
        block = re.search(r"var ROUTE_PERMISSIONS = \{(.*?)\};", self.js, re.S).group(1)
        guards = dict(re.findall(r'"(/[a-z/-]+)": "([a-z_.]+)"', block))
        links = dict(re.findall(r'href="#(/[a-z/-]+)" data-nav="[a-z-]+" data-perm="([a-z_.]+)"', html))
        self.assertTrue(links)
        self.assertEqual(guards, links)
        route = self.function_body("route")
        self.assertLess(route.index("routeAllowed(r.path)"), route.index("viewUsers()"))
        allowed = self.function_body("routeAllowed")
        self.assertIn('path === "/new") return canCreate()', allowed)
        self.assertIn('path === "/admin/audit") return isAdmin()', allowed)


if __name__ == "__main__":
    unittest.main()
