/* MyDemoTicket web UI - admin page #/admin/pairing (ES + SOAR pairing wizard).
 * Loaded before app.js; app.js calls window.MDT_PAIRING.view(kit) with its helpers
 * (h, t, api, render, clear, toast, fail, field, errorText, fmtTime, checkField, table).
 * All visible texts come from i18n.js via t(); the SOAR token is only sent, never shown or stored. */
(function () {
  "use strict";

  var STEP_KEYS = ["user", "webhook", "label", "app", "asset", "playbook_es_mirror", "playbook_ticket_from_es"];
  var CHECK_KEYS = ["insecure_http", "connection", "splunk_asset", "es_asset", "app", "label"];

  function view(kit) {
    var h = kit.h, t = kit.t;
    function txt(prefix, value) {
      var key = prefix + value;
      var text = t(key);
      return text === key ? String(value) : text;
    }
    function statusClass(status) {
      if (status === "failed") return "error";
      if (status === "created" || status === "updated" || status === "ok") return "ok-text";
      return "muted";
    }

    var statusBox = h("div", { class: "panel-body", "aria-live": "polite" },
      h("p", { class: "muted", text: t("common.loading") }));
    var formBox = h("div", { class: "panel-body" });
    var resultBox = h("div", { class: "panel-body", role: "status", "aria-live": "polite" },
      h("p", { class: "muted", text: t("pairing.result_empty") }));

    kit.render(
      h("div", { class: "panel" },
        h("div", { class: "panel-head" }, h("h1", { text: t("pairing.title") }), h("span", { class: "spacer" }),
          h("button", { type: "button", onclick: function () { load(); } }, t("common.refresh"))),
        h("div", { class: "panel-body" }, h("p", { class: "muted", text: t("pairing.intro") })),
        statusBox),
      h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", { text: t("pairing.setup") })), formBox),
      h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", { text: t("pairing.result") })),
        resultBox));

    function drawStatus(p) {
      kit.clear(statusBox);
      var last = p.last_apply;
      var user = p.integration_user || {};
      var rows = [
        [t("pairing.state"), p.configured ? (last && last.ok ? t("pairing.state_ok") : t("pairing.state_partial"))
          : t("pairing.state_none")],
        [t("pairing.last_apply"), last ? t("pairing.by_at", { user: last.by, at: kit.fmtTime(last.at) }) : "-"],
        [t("pairing.soar_url"), (p.settings && p.settings.soar_url) || "-"],
        [t("pairing.soar_version"), (p.settings && p.settings.soar_version) || "-"],
        [t("pairing.integration_user"), user.username + " - " + (user.exists
          ? (user.token_active ? t("pairing.user_token_ok") : t("pairing.user_no_token")) : t("pairing.user_missing"))],
        [t("pairing.webhook"), p.webhook ? t("pairing.webhook_ok", { id: p.webhook.id, label: p.webhook.soar_label })
          : t("pairing.webhook_missing")],
        [t("pairing.bundle"), p.bundle && p.bundle.available
          ? t("pairing.bundle_ok", { version: p.bundle.app_version }) : t("pairing.bundle_missing")]
      ];
      statusBox.appendChild(kit.table([t("pairing.col.item"), t("pairing.col.value")], rows.map(function (r) {
        return h("tr", null, h("td", { text: r[0] }), h("td", { text: String(r[1]) }));
      })));
      if (last && last.steps) {
        statusBox.appendChild(h("h3", { text: t("pairing.last_steps") }));
        statusBox.appendChild(stepTable(last.steps, "pairing.step.", "step"));
      }
    }

    function stepTable(items, prefix, nameKey) {
      return kit.table([t("pairing.col.step"), t("pairing.col.status"), t("pairing.col.detail")],
        items.map(function (s) {
          return h("tr", null, h("td", { text: txt(prefix, s[nameKey]) }),
            h("td", { class: statusClass(s.status), text: txt("pairing.status.", s.status) }),
            h("td", { class: "mono", text: s.detail || "" }));
        }));
    }

    function drawForm(p) {
      kit.clear(formBox);
      var s = p.settings || {};
      var d = p.defaults || {};
      function input(id, value, type) {
        return h("input", { id: id, type: type || "text", value: value === null || value === undefined ? "" : value });
      }
      var url = input("pa-url", s.soar_url || "", "url");
      url.setAttribute("placeholder", "https://soar.example.invalid");
      url.required = true;
      var token = h("input", { id: "pa-token", type: "password", autocomplete: "new-password", required: true,
        placeholder: t("pairing.token_placeholder") });
      var verify = h("input", { id: "pa-verify", type: "checkbox", checked: s.verify_tls !== false });
      var lbl = input("pa-label", s.label || d.label);
      var splunk = input("pa-splunk", s.splunk_asset || d.splunk_asset);
      var es = input("pa-es", s.es_asset || d.es_asset);
      var appAsset = input("pa-asset", s.app_asset || d.app_asset);
      var base = input("pa-base", s.ticket_base_url || d.ticket_base_url, "url");
      var baseVerify = h("input", { id: "pa-base-verify", type: "checkbox", checked: s.ticket_verify_tls !== false });

      function payload(apply) {
        var body = { soar_url: url.value.trim(), soar_token: token.value, verify_tls: verify.checked,
          label: lbl.value.trim(), splunk_asset: splunk.value.trim(), es_asset: es.value.trim(),
          app_asset: appAsset.value.trim() };
        if (apply) { body.ticket_base_url = base.value.trim(); body.ticket_verify_tls = baseVerify.checked; }
        return body;
      }
      function valid() {
        if (!url.value.trim() || !token.value) { kit.toast(t("pairing.need_url_token"), true); return false; }
        return true;
      }
      function busy(on) { checkBtn.disabled = on; applyBtn.disabled = on; }
      function showError(err) {
        kit.clear(resultBox);
        resultBox.appendChild(h("p", { class: "error", text: kit.errorText(err) }));
      }

      var checkBtn = h("button", {
        type: "button", onclick: function () {
          if (!valid()) return;
          busy(true);
          kit.clear(resultBox);
          resultBox.appendChild(h("p", { class: "muted", text: t("pairing.checking") }));
          kit.api("POST", "/pairing/check", payload(false)).then(function (r) {
            kit.clear(resultBox);
            resultBox.appendChild(h("p", { class: r.ok ? "ok-text" : "error",
              text: r.ok ? t("pairing.check_ok", { version: r.soar_version || "-" }) : t("pairing.check_failed") }));
            resultBox.appendChild(stepTable(r.checks || [], "pairing.check.", "check"));
          }).catch(showError).then(function () { busy(false); });
        }
      }, t("pairing.check"));

      var applyBtn = h("button", { class: "primary", type: "submit" }, t("pairing.apply"));
      var form = h("form", {
        class: "form-grid", onsubmit: function (ev) {
          ev.preventDefault();
          if (!valid()) return;
          busy(true);
          kit.clear(resultBox);
          resultBox.appendChild(h("p", { class: "muted", text: t("pairing.applying") }));
          kit.api("POST", "/pairing/apply", payload(true)).then(function (r) {
            token.value = "";
            kit.clear(resultBox);
            var conn = r.connection || {};
            resultBox.appendChild(h("p", { class: r.ok ? "ok-text" : "error",
              text: r.ok ? t("pairing.apply_ok") : (conn.status === "failed"
                ? t("pairing.apply_no_connection", { detail: conn.detail || "" }) : t("pairing.apply_failed")) }));
            resultBox.appendChild(stepTable(r.steps || [], "pairing.step.", "step"));
            kit.toast(r.ok ? t("pairing.apply_ok") : t("pairing.apply_failed"), !r.ok);
            return kit.api("GET", "/pairing").then(drawStatus);
          }).catch(showError).then(function () { busy(false); });
        }
      },
        h("h3", { class: "wide", text: t("pairing.section_soar") }),
        kit.field(t("pairing.soar_url"), url, { required: true }),
        kit.field(t("pairing.token"), token, { required: true }),
        kit.checkField(verify, " " + t("pairing.verify_tls")),
        h("p", { class: "wide muted", text: t("pairing.token_hint") }),
        h("h3", { class: "wide", text: t("pairing.section_names") }),
        kit.field(t("pairing.label"), lbl),
        kit.field(t("pairing.app_asset"), appAsset),
        kit.field(t("pairing.splunk_asset"), splunk),
        kit.field(t("pairing.es_asset"), es),
        h("h3", { class: "wide", text: t("pairing.section_ticket") }),
        kit.field(t("pairing.ticket_base_url"), base),
        kit.checkField(baseVerify, " " + t("pairing.ticket_verify_tls")),
        h("p", { class: "wide muted", text: t("pairing.steps_hint") }),
        h("div", { class: "wide actions" }, checkBtn, " ", applyBtn));
      formBox.appendChild(form);
    }

    function load() {
      kit.api("GET", "/pairing").then(function (p) {
        drawStatus(p);
        if (!formBox.firstChild) drawForm(p);
      }).catch(function (err) {
        kit.clear(statusBox);
        statusBox.appendChild(h("p", { class: "error", text: kit.errorText(err) }));
        kit.fail(err);
      });
    }
    load();
  }

  window.MDT_PAIRING = { view: view, steps: STEP_KEYS, checks: CHECK_KEYS };
})();
