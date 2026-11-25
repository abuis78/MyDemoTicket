"""Web UI guard rails for the multi-select list filters and bulk editing (static checks of app.js)."""

import re
import unittest

from test_i18n import parse_dict, read, strip_comments
from test_ui_profile_roles import function_body


class UiBulkFiltersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = strip_comments(read("app.js"))
        cls.css = read("app.css")
        i18n = read("i18n.js")
        cls.de = parse_dict(i18n, "de")
        cls.en = parse_dict(i18n, "en")

    def test_filters_are_keyboard_accessible_multi_selects(self):
        body = function_body(self.js, "multiSelect")
        for attr in ('"aria-haspopup": "true"', '"aria-expanded": "false"', '"aria-controls": id + "-panel"',
                     'role: "group"', 'type: "checkbox"', 'ev.key === "Escape"', "api_.close(true)",
                     'button.setAttribute("aria-expanded", "true")', "first.focus()"):
            self.assertIn(attr, body)
        view = function_body(self.js, "viewList")
        for fid in ('"f-state"', '"f-priority"', '"f-group"', '"f-assignee"'):
            self.assertIn("multiSelect(" + fid, view)
        self.assertNotIn('select("f-state"', view)  # the old single selects are gone
        self.assertIn(".multi-panel", self.css)

    def test_filters_live_in_the_hash_and_go_to_the_api_as_repeated_params(self):
        view = function_body(self.js, "viewList")
        for key in ("state", "priority", "assignment_group", "assigned_to"):
            self.assertIn('hashList(raw, "%s")' % key, view)
        self.assertIn('location.hash = "#/tickets?" + hashQuery(next)', view)
        self.assertIn('parts.push(key + "=" + encodeURIComponent(v))', view)
        self.assertIn('var NONE = "-";', self.js)  # API value for "unassigned" / "no group"
        self.assertIn("map(encodeURIComponent).join(\",\")", function_body(self.js, "hashQuery"))

    def test_row_selection_and_bulk_request(self):
        view = function_body(self.js, "viewList")
        self.assertIn('id: "bulk-all", "aria-label": t("bulk.select_all")', view)
        self.assertIn('"aria-label": t("bulk.select_row", { number: x.number })', view)
        self.assertIn("allBox.indeterminate", view)
        bar = function_body(self.js, "bulkBar")
        self.assertIn('api("POST", "/tickets/bulk", { numbers: numbers, changes: changes })', bar)
        self.assertIn("form.hidden = n === 0", bar)  # bar only with >= 1 selected
        self.assertIn("pendingBulkSummary = bulkSummary(res.results || [])", bar)
        self.assertIn("route()", bar)  # list reloads after the bulk change
        # close notes required before any request when resolving
        pos_check = bar.index('changes.state === "resolved" && !changes.close_notes')
        self.assertLess(pos_check, bar.index('api("POST", "/tickets/bulk"'))
        summary = function_body(self.js, "bulkSummary")
        for key in ('t("bulk.result"', 't("bulk.failed_count"', 't("bulk.failure"', 'role: "status"', "r.number"):
            self.assertIn(key, summary)

    def test_bulk_actions_follow_permissions(self):
        allowed = function_body(self.js, "bulkAllowed")
        for perm in ('can("tickets.update_all")', 'can("tickets.update_own")', 'can("tickets.create")'):
            self.assertIn(perm, allowed)
        bar = function_body(self.js, "bulkBar")
        self.assertIn('var mayClose = can("tickets.close");', bar)
        self.assertIn("mayClose || CLOSING_STATES.indexOf(s) < 0", bar)
        self.assertIn('if (requester) return s === "cancelled";', bar)
        self.assertIn("var selectable = bulkAllowed();", function_body(self.js, "viewList"))

    def test_bulk_and_filter_texts_exist_in_both_languages(self):
        keys = set(re.findall(r'\bt\("((?:bulk|filter)\.[a-z0-9_.]+)"', self.js))
        self.assertGreaterEqual(len(keys), 20)
        for key in sorted(keys | {"list.priority", "list.assigned_to"}):
            self.assertIn(key, self.de, key)
            self.assertIn(key, self.en, key)
            self.assertNotEqual(self.de[key], self.en[key], key)


if __name__ == "__main__":
    unittest.main()
