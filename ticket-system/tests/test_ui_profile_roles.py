"""Web UI guard rails for profile, theme, time zone, users admin, roles admin and the Splunk connection form."""

import os
import re
import unittest

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mydemoticket", "static")


def read(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as handle:
        return handle.read()


def function_body(js, name):
    """Source of `function <name>(...) { ... }` (brace matching, good enough for app.js)."""
    start = js.index("function %s(" % name)
    depth, i = 0, js.index("{", start)
    for i in range(i, len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[start:i + 1]
    raise AssertionError("unbalanced function %s" % name)


class UiProfileRolesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = read("app.js")
        cls.html = read("index.html")
        cls.css = read("app.css")

    def test_user_name_opens_profile_and_is_keyboard_accessible(self):
        self.assertRegex(self.html, r'<a class="user-name" id="user-name" href="#/profile"')
        self.assertIn('r.path === "/profile"', self.js)
        self.assertIn("function viewProfile()", self.js)

    def test_profile_patches_me_with_the_five_settings(self):
        body = function_body(self.js, "viewProfile")
        self.assertIn('api("PATCH", "/me", body)', body)
        for key in ("display_name:", "email:", "timezone:", "theme:", "language:"):
            self.assertIn(key, body)
        self.assertIn("applyMe(updated)", body)  # applied at once
        self.assertIn('api("POST", "/me/password", { current_password: current.value, new_password: next.value })', body)

    def test_new_passwords_are_checked_twice_before_any_request(self):
        self.assertIn("var MIN_PASSWORD = 8;", self.js)
        check = function_body(self.js, "passwordProblem")
        self.assertIn('"pw.too_short"', check)
        self.assertIn('"pw.mismatch"', check)
        for name in ("viewProfile", "passwordDialog"):
            body = function_body(self.js, name)
            pos_check = body.index("passwordProblem(")
            pos_return = body.index("if (problem) {", pos_check)
            pos_api = body.index("api(", pos_check)
            self.assertLess(pos_return, pos_api, name)

    def test_set_password_uses_a_dialog_not_window_prompt(self):
        self.assertNotIn("window.prompt", self.js)
        self.assertNotRegex(self.js, r"\bprompt\(")
        body = function_body(self.js, "passwordDialog")
        self.assertIn('h("dialog"', body)
        self.assertIn("showModal", body)
        self.assertIn('api("PATCH", "/users/" + encodeURIComponent(u.username), { password: first.value })', body)
        self.assertIn("dialog.modal::backdrop", self.css)

    def test_users_inline_edit_of_display_name_and_email(self):
        body = function_body(self.js, "viewUsers")
        self.assertIn("body.display_name = display.value.trim()", body)
        self.assertIn("body.email = email.value.trim()", body)
        self.assertIn('api("PATCH", path, body)', body)
        self.assertIn("roleOptions(", body)
        self.assertIn('loadRoles()', body)

    def test_roles_admin_page(self):
        self.assertIn('href="#/admin/roles"', self.html)
        self.assertRegex(self.html, r'href="#/admin/roles"[^>]*data-perm="roles.manage"')
        body = function_body(self.js, "viewRoles")
        self.assertIn('api("GET", "/roles")', body)
        self.assertIn('api("GET", "/permissions")', body)
        self.assertIn("filter(function (p) { return p.grantable; })", body)
        self.assertIn("r.builtin ?", body)  # built-in roles have no edit/delete
        self.assertIn('api("DELETE", "/roles/" + encodeURIComponent(r.name))', body)
        form = function_body(self.js, "roleForm")
        self.assertIn('api("PATCH", "/roles/" + encodeURIComponent(role.name), body)', form)
        self.assertIn('api("POST", "/roles"', form)

    def test_splunk_connection_form(self):
        panel = function_body(self.js, "splunkPanel")
        self.assertIn('api("GET", "/settings/splunk")', panel)
        form = function_body(self.js, "splunkForm")
        self.assertIn('api("PUT", "/settings/splunk", body)', form)
        self.assertIn('api("POST", "/settings/splunk/test", {})', form)
        self.assertIn('type: "password"', form)
        self.assertIn('if (clearToken.checked) body.token = "";', form)
        self.assertIn("else if (token.value) body.token = token.value;", form)
        self.assertIn("s.token_set", form)
        self.assertIn("s.source", form)
        self.assertIn('can("splunk.manage") ? splunkPanel() : null', self.js)

    def test_theme_and_time_zone(self):
        self.assertIn('document.documentElement.setAttribute("data-theme", dark ? "dark" : "light")', self.js)
        self.assertIn('matchMedia("(prefers-color-scheme: dark)")', self.js)
        self.assertIn(":root[data-theme=dark] {", self.css)
        dark = self.css[self.css.index(":root[data-theme=dark] {"):]
        dark = dark[:dark.index("}")]
        light = self.css[self.css.index(":root {"):]
        light = light[:light.index("}")]
        light_tokens = set(re.findall(r"(--[a-z0-9-]+):", light))
        dark_tokens = set(re.findall(r"(--[a-z0-9-]+):", dark))
        self.assertEqual(sorted(light_tokens - dark_tokens), [], "tokens without a dark value")
        fmt = function_body(self.js, "formatter")
        self.assertIn("opts.timeZone = tz", fmt)
        self.assertIn("new Intl.DateTimeFormat(I18N.locale(), opts)", fmt)
        self.assertIn('Intl.supportedValuesOf("timeZone")', self.js)

    def test_permission_based_chrome_keeps_role_fallback(self):
        self.assertIn("Array.isArray(me.permissions) ? me.permissions : (FALLBACK_PERMISSIONS[me.role] || [])", self.js)
        for role in ("platform_admin", "admin", "agent", "integration", "requester"):
            self.assertRegex(self.js, r"\n    %s: " % role)
        self.assertIn('each("[data-perm]"', self.js)
        for perm in ("users.manage", "teams.manage", "fields.manage", "webhooks.manage", "tenants.manage",
                     "reports.view"):
            self.assertIn('data-perm="%s"' % perm, self.html)


if __name__ == "__main__":
    unittest.main()
