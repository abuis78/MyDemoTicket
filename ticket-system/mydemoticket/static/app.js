/* MyDemoTicket web UI - vanilla JS single page app, no dependencies.
 * All DOM is built with createElement/textContent (never innerHTML with data).
 * All visible texts come from i18n.js via t("key", params). */
(function () {
  "use strict";

  var I18N = window.MDT_I18N;
  function t(key, params) { return I18N.t(key, params); }
  // label of a value from a key group, falling back to the raw value (e.g. unknown states)
  function label(prefix, value) { return I18N.has(prefix + value) ? t(prefix + value) : String(value); }

  var STATES = ["new", "in_progress", "on_hold", "resolved", "closed", "cancelled"];
  var TRANSITIONS = {
    new: ["in_progress", "on_hold", "resolved", "cancelled"],
    in_progress: ["on_hold", "resolved", "cancelled"],
    on_hold: ["in_progress", "resolved", "cancelled"],
    resolved: ["in_progress", "closed"],
    closed: [],
    cancelled: []
  };
  var CLOSING_STATES = ["resolved", "closed", "cancelled"];
  var PRIORITY_KEYS = ["1", "2", "3", "4"];
  var EVENTS = ["ticket.created", "ticket.updated", "ticket.state_changed", "ticket.commented"];
  var FIELD_TYPES = ["string", "number", "boolean", "choice", "date"];

  // Permissions of the built-in roles, used only when /me has no "permissions" list (older server).
  var ALL_PERMISSIONS = ["tickets.read_all", "tickets.create", "tickets.update_own", "tickets.update_all",
    "tickets.comment", "tickets.work_note", "tickets.close", "reports.view", "users.manage", "roles.manage",
    "teams.manage", "fields.manage", "webhooks.manage", "tenants.manage", "splunk.manage"];
  var FALLBACK_PERMISSIONS = {
    platform_admin: ALL_PERMISSIONS,
    admin: ALL_PERMISSIONS.filter(function (p) { return p !== "tenants.manage" && p !== "splunk.manage"; }),
    agent: ["tickets.read_all", "tickets.create", "tickets.update_own", "tickets.comment", "tickets.work_note",
      "tickets.close", "reports.view"],
    integration: ["tickets.read_all", "tickets.create", "tickets.update_all", "tickets.comment",
      "tickets.work_note", "tickets.close", "reports.view"],
    requester: ["tickets.create", "tickets.comment"],
    analyst_l1: ["tickets.read_all", "tickets.create", "tickets.update_own", "tickets.comment", "tickets.work_note"],
    analyst_l2: ["tickets.read_all", "tickets.create", "tickets.update_all", "tickets.comment", "tickets.work_note",
      "tickets.close"],
    soc_lead: ["tickets.read_all", "tickets.create", "tickets.update_all", "tickets.comment", "tickets.work_note",
      "tickets.close", "reports.view"],
    read_only: ["tickets.read_all"]
  };

  var session = { me: null, csrf: null, fields: [], teams: [], users: [], roles: [] };
  var main = document.getElementById("main");

  // ---------------------------------------------------------------- helpers
  function h(tag, attrs) {
    var el = document.createElement(tag);
    var children = Array.prototype.slice.call(arguments, 2);
    Object.keys(attrs || {}).forEach(function (key) {
      var value = attrs[key];
      if (value === null || value === undefined || value === false) return;
      if (key === "class") el.className = value;
      else if (key === "text") el.textContent = value;
      else if (key.indexOf("on") === 0) el.addEventListener(key.slice(2), value);
      else if (key === "value") el.value = value;
      else if (key === "checked" || key === "selected" || key === "disabled") el[key] = !!value;
      else el.setAttribute(key, value === true ? "" : String(value));
    });
    children.forEach(function append(child) {
      if (child === null || child === undefined || child === false) return;
      if (Array.isArray(child)) { child.forEach(append); return; }
      el.appendChild(typeof child === "string" || typeof child === "number"
        ? document.createTextNode(String(child)) : child);
    });
    return el;
  }

  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }

  function render() {
    clear(main);
    Array.prototype.slice.call(arguments).forEach(function (c) { if (c) main.appendChild(c); });
  }

  var toastTimer = null;
  function toast(message, isError) {
    var el = document.getElementById("toast");
    el.textContent = message;
    el.className = "toast" + (isError ? " err" : "");
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, isError ? 6000 : 3000);
  }

  function api(method, path, body) {
    var headers = { "Accept": "application/json" };
    var opts = { method: method, headers: headers, credentials: "same-origin" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    if (method !== "GET" && session.csrf) headers["X-CSRF-Token"] = session.csrf;
    return fetch("/api/v1" + path, opts).then(function (resp) {
      if (resp.status === 204) return null;
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (!resp.ok) {
          var err = new Error((data && data.error) || ("HTTP " + resp.status));
          err.status = resp.status;
          throw err;
        }
        return data;
      });
    });
  }

  // Readable texts for the tenant errors of the API (see docs/api.md, "Tenant context").
  var TENANT_ERRORS = {
    "tenant mismatch": "error.tenant_mismatch",
    "tenant inactive": "error.tenant_inactive",
    "tenant not found": "error.tenant_not_found"
  };

  function errorText(err) {
    var message = (err && err.message) || String(err);
    if (TENANT_ERRORS[message]) return t(TENANT_ERRORS[message]) + " (" + err.status + " " + message + ")";
    if (err && err.status === 403) return t("error.forbidden", { message: message });
    if (err && err.status === 404) return t("error.not_found", { message: message });
    return message;
  }

  function fail(err) {
    if (err && err.status === 401) { session.me = null; showLogin(); return; }
    if (err && err.status === 403 && err.message === "tenant inactive" && session.me && !isPlatform()) {
      session.me = null; showLogin(errorText(err)); return;
    }
    toast(errorText(err), true);
  }

  // ------------------------------------------------------- roles & permissions
  function isPlatform() { return !!session.me && session.me.role === "platform_admin"; }
  function isAdmin() { return !!session.me && (session.me.role === "admin" || isPlatform()); }
  function permissions() {
    var me = session.me;
    if (!me) return [];
    return Array.isArray(me.permissions) ? me.permissions : (FALLBACK_PERMISSIONS[me.role] || []);
  }
  function can(perm) { return permissions().indexOf(perm) >= 0; }
  // requesters (and roles without tickets.read_all) only see their own tickets and no internal data
  function isRequester() { return !!session.me && (session.me.role === "requester" || !can("tickets.read_all")); }
  function canCreate() { return !!session.me && (session.me.role === "requester" || can("tickets.create")); }
  function canSeeReports() { return can("reports.view"); }

  function roleLabel(name) {
    if (I18N.has("role." + name)) return t("role." + name);
    var custom = session.roles.filter(function (r) { return r.name === name; })[0];
    if (custom && custom.label) return custom.label;
    // without roles.manage there is no /roles; /me carries the label of the own role
    var me = session.me;
    return me && me.role === name && me.role_label ? me.role_label : name;
  }

  // ----------------------------------------------------- theme, time, language
  var THEME_KEY = "mdt.theme";
  var darkQuery = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  var themePref = "system";

  function storedTheme() {
    try { return window.localStorage.getItem(THEME_KEY) || "system"; } catch (e) { return "system"; }
  }

  function applyTheme(pref) {
    themePref = pref === "light" || pref === "dark" ? pref : "system";
    try { window.localStorage.setItem(THEME_KEY, themePref); } catch (e) { /* storage blocked */ }
    var dark = themePref === "dark" || (themePref === "system" && !!darkQuery && darkQuery.matches);
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  }
  if (darkQuery) {
    var onSchemeChange = function () { if (themePref === "system") applyTheme("system"); };
    if (darkQuery.addEventListener) darkQuery.addEventListener("change", onSchemeChange);
    else if (darkQuery.addListener) darkQuery.addListener(onSchemeChange);
  }

  var timeFormat = { key: null, fmt: null };
  function formatter() {
    var tz = (session.me && session.me.timezone) || "";
    var key = I18N.locale() + "|" + tz;
    if (timeFormat.key !== key) {
      var opts = { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" };
      if (tz) opts.timeZone = tz;
      var fmt;
      try { fmt = new Intl.DateTimeFormat(I18N.locale(), opts); } catch (e) {
        delete opts.timeZone; // unknown time zone in this browser: use the browser time zone
        fmt = new Intl.DateTimeFormat(I18N.locale(), opts);
      }
      timeFormat = { key: key, fmt: fmt };
    }
    return timeFormat.fmt;
  }

  function fmtTime(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    return isNaN(d) ? iso : formatter().format(d);
  }

  function applyLanguage(lang) {
    if (lang && lang !== I18N.lang) I18N.setLang(lang);
    else I18N.applyDom(document);
  }

  function stateBadge(state) {
    return h("span", { class: "badge state-" + state, text: label("state.", state) });
  }

  function priorityOptions() { return PRIORITY_KEYS.map(function (k) { return [k, t("priority." + k)]; }); }

  function field(labelText, control, opts) {
    opts = opts || {};
    return h("div", { class: opts.wide ? "wide" : null },
      h("label", { class: opts.required ? "req" : null, for: control.id || null, text: labelText }),
      control);
  }

  function select(id, options, current, includeEmpty) {
    var sel = h("select", { id: id });
    if (includeEmpty) sel.appendChild(h("option", { value: "" }, includeEmpty));
    options.forEach(function (opt) {
      var value = Array.isArray(opt) ? opt[0] : opt;
      var text = Array.isArray(opt) ? opt[1] : opt;
      sel.appendChild(h("option", { value: String(value), selected: String(value) === String(current) }, text));
    });
    return sel;
  }

  function parseHash() {
    var raw = location.hash.replace(/^#/, "") || "/tickets";
    var parts = raw.split("?");
    var params = {};
    (parts[1] || "").split("&").forEach(function (kv) {
      if (!kv) return;
      var p = kv.split("=");
      params[decodeURIComponent(p[0])] = decodeURIComponent((p[1] || "").replace(/\+/g, " "));
    });
    return { path: parts[0], params: params };
  }

  function canModify(ticket) {
    var me = session.me;
    if (!me) return false;
    if (can("tickets.update_all")) return true;
    if (!can("tickets.update_own")) return false;
    if (!ticket.assignment_group && !ticket.assigned_to) return true;
    if (ticket.assigned_to === me.username) return true;
    return !!ticket.assignment_group && me.teams.indexOf(ticket.assignment_group) >= 0;
  }

  // ------------------------------------------------------------ bootstrap
  function loadReference() {
    var jobs = [api("GET", "/custom-fields").then(function (f) { session.fields = f; })];
    if (!isRequester()) {
      jobs.push(api("GET", "/teams").then(function (x) { session.teams = x; }));
      jobs.push(api("GET", "/users").then(function (u) { session.users = u; }));
    }
    return Promise.all(jobs);
  }

  function setChrome(loggedIn) {
    I18N.applyDom(document);
    document.getElementById("sidebar").hidden = !loggedIn;
    document.getElementById("userbox").hidden = !loggedIn;
    document.getElementById("global-search").hidden = !loggedIn;
    function each(selector, fn) { Array.prototype.forEach.call(document.querySelectorAll(selector), fn); }
    // data-perm wins; .admin-only / .platform-only without it stay role based
    each(".admin-only", function (el) { el.hidden = !(loggedIn && isAdmin()); });
    each(".platform-only", function (el) { el.hidden = !(loggedIn && isPlatform()); });
    each(".create-only", function (el) { el.hidden = !(loggedIn && canCreate()); });
    each("[data-perm]", function (el) { el.hidden = !(loggedIn && can(el.getAttribute("data-perm"))); });
    var anyAdmin = false;
    each(".sidebar a.admin-only", function (el) { if (!el.hidden) anyAdmin = true; });
    document.getElementById("nav-admin").hidden = !anyAdmin;
    if (loggedIn) {
      document.getElementById("user-name").textContent =
        session.me.display_name + " (" + roleLabel(session.me.role) + ")";
      drawTenantBox();
    }
  }

  // Header: name of the active tenant; platform admins get a switcher fed by /me.tenants.
  function drawTenantBox() {
    var me = session.me;
    var active = me.active_tenant || me.tenant || {};
    var nameEl = document.getElementById("tenant-name");
    var sel = document.getElementById("tenant-switch");
    nameEl.textContent = active.name || active.slug || "-";
    nameEl.hidden = isPlatform();
    sel.hidden = !isPlatform();
    clear(sel);
    if (!isPlatform()) return;
    (me.tenants || []).forEach(function (x) {
      sel.appendChild(h("option", { value: x.slug, selected: x.slug === active.slug },
        x.name + (x.active ? "" : t("tenant.inactive_suffix"))));
    });
  }

  document.getElementById("tenant-switch").addEventListener("change", function (ev) {
    var slug = ev.target.value;
    api("POST", "/auth/tenant", { tenant: slug }).then(function (me) {
      return applyMe(me).then(function () {
        toast(t("tenant.switched", { name: (me.active_tenant && me.active_tenant.name) || slug }));
        // a ticket of the old tenant is a 404 in the new one: go back to the list
        if (/^\/tickets\/./.test(parseHash().path)) location.hash = "#/tickets";
        else route();
      });
    }).catch(function (err) { drawTenantBox(); fail(err); });
  });

  function loadRoles() {
    if (!can("users.manage") && !can("roles.manage")) { session.roles = []; return Promise.resolve(); }
    return api("GET", "/roles").then(function (r) { session.roles = r || []; })
      .catch(function () { session.roles = []; });
  }

  // Stores the /me object, applies language, theme and time zone and (re)loads reference data and the header.
  function applyMe(me) {
    session.me = me;
    session.csrf = me.csrf_token || session.csrf;
    applyLanguage(me.language);
    applyTheme(me.theme || storedTheme());
    return Promise.all([loadReference(), loadRoles()]).then(function () { setChrome(true); });
  }

  function refreshMe() {
    return api("GET", "/me").then(applyMe).catch(fail);
  }

  function start() {
    applyTheme(storedTheme());
    api("GET", "/me").then(function (me) {
      return applyMe(me).then(route);
    }).catch(function (err) {
      showLogin(err && err.status === 403 ? errorText(err) : "");
    });
  }

  function languageSelect(id, onChange) {
    var sel = select(id, I18N.supported.map(function (l) { return [l, t("lang." + l)]; }), I18N.lang);
    sel.addEventListener("change", function () { onChange(sel.value); });
    return sel;
  }

  function showLogin(message) {
    setChrome(false);
    var user = h("input", { id: "login-user", autocomplete: "username", required: true });
    var pass = h("input", { id: "login-pass", type: "password", autocomplete: "current-password", required: true });
    var error = h("div", { class: "error", role: "alert", text: typeof message === "string" ? message : "" });
    var lang = languageSelect("login-lang", function (value) {
      var typed = user.value;
      I18N.setLang(value);
      showLogin(error.textContent);
      document.getElementById("login-user").value = typed;
    });
    var form = h("form", {
      onsubmit: function (ev) {
        ev.preventDefault();
        error.textContent = "";
        api("POST", "/auth/login", { username: user.value, password: pass.value }).then(function (login) {
          session.csrf = login.csrf_token;
          // the login response has no tenant list; /me adds active_tenant + tenants for platform admins
          return api("GET", "/me").then(applyMe).then(function () {
            if (!location.hash || location.hash === "#/login") location.hash = "#/tickets";
            else route();
          });
        }).catch(function (err) { error.textContent = errorText(err); });
      }
    },
      h("div", { class: "panel-body" },
        field(t("login.user"), user), field(t("login.password"), pass), error,
        h("button", { class: "primary", type: "submit" }, t("login.submit")),
        field(t("login.language"), lang)));
    render(h("div", { class: "panel login" },
      h("div", { class: "panel-head" }, h("h1", { text: t("login.title") })), form));
    user.focus();
  }

  document.getElementById("logout").addEventListener("click", function () {
    api("POST", "/auth/logout", {}).catch(function () {}).then(function () {
      session.me = null; session.csrf = null; showLogin();
    });
  });
  document.getElementById("global-search").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var q = document.getElementById("global-search-input").value.trim();
    location.hash = "#/tickets?all=1&q=" + encodeURIComponent(q);
  });

  // ----------------------------------------------------------------- router
  // Pages a user without the permission must not see even via a typed URL (same permission as the nav link
  // in index.html). Some of their GET requests succeed (e.g. /users with tickets.read_all), the writes do not.
  var ROUTE_PERMISSIONS = {
    "/admin/users": "users.manage", "/admin/roles": "roles.manage", "/admin/teams": "teams.manage",
    "/admin/custom-fields": "fields.manage", "/admin/webhooks": "webhooks.manage",
    "/admin/tenants": "tenants.manage", "/reports": "reports.view"
  };

  function routeAllowed(path) {
    if (path === "/new") return canCreate();
    if (path === "/admin/audit") return isAdmin();
    return !ROUTE_PERMISSIONS[path] || can(ROUTE_PERMISSIONS[path]);
  }

  function route() {
    if (!session.me) { showLogin(); return; }
    var r = parseHash();
    var nav = "tickets";
    var m;
    if (!routeAllowed(r.path)) { nav = ""; render(h("p", { class: "error", role: "alert", text: t("error.no_access") })); }
    else if (r.path === "/new") { nav = "new"; viewNew(); }
    else if ((m = r.path.match(/^\/tickets\/([^/]+)$/))) { nav = ""; viewDetail(decodeURIComponent(m[1])); }
    else if (r.path === "/admin/users") { nav = "users"; viewUsers(); }
    else if (r.path === "/admin/roles") { nav = "roles"; viewRoles(); }
    else if (r.path === "/admin/teams") { nav = "teams"; viewTeams(); }
    else if (r.path === "/admin/custom-fields") { nav = "custom-fields"; viewFields(); }
    else if (r.path === "/admin/webhooks") { nav = "webhooks"; viewWebhooks(); }
    else if (r.path === "/admin/audit") { nav = "audit"; viewAudit(); }
    else if (r.path === "/admin/tenants") { nav = "tenants"; viewTenants(); }
    else if (r.path === "/reports") { nav = "reports"; viewReports(r.params); }
    else if (r.path === "/profile") { nav = ""; viewProfile(); }
    else {
      if (r.params.mine) nav = "mine";
      else if (r.params.unassigned) nav = "unassigned";
      else if (r.params.all) nav = "all";
      viewList(r.params);
    }
    Array.prototype.forEach.call(document.querySelectorAll(".sidebar a"), function (a) {
      a.classList.toggle("active", a.getAttribute("data-nav") === nav);
    });
  }
  window.addEventListener("hashchange", route);

  // ------------------------------------------------------------ ticket list
  function viewList(params) {
    var query = { limit: 25, offset: parseInt(params.offset || "0", 10) || 0 };
    if (params.state) query.state = params.state;
    else if (!params.all) query.state = "new,in_progress,on_hold";
    if (params.q) query.q = params.q;
    if (params.assignment_group) query.assignment_group = params.assignment_group;
    if (params.mine) query.assigned_to = session.me.username;
    if (params.unassigned) query.assigned_to = "";

    var title = params.mine ? t("list.title_mine") : params.unassigned ? t("list.title_unassigned")
      : params.all ? t("list.title_all") : t("list.title_open");
    var stateSel = select("f-state", STATES.map(function (s) { return [s, label("state.", s)]; }),
      params.state || "", t("list.any_state"));
    var groupSel = select("f-group", session.teams.map(function (x) { return x.name; }),
      params.assignment_group || "", t("list.any_group"));
    var qInput = h("input", { id: "f-q", type: "search", value: params.q || "", placeholder: t("list.search_placeholder") });
    function apply(ev) {
      if (ev) ev.preventDefault();
      var next = {};
      ["mine", "unassigned", "all"].forEach(function (k) { if (params[k]) next[k] = params[k]; });
      if (stateSel.value) next.state = stateSel.value;
      if (groupSel.value) next.assignment_group = groupSel.value;
      if (qInput.value.trim()) next.q = qInput.value.trim();
      location.hash = "#/tickets?" + Object.keys(next).map(function (k) {
        return k + "=" + encodeURIComponent(next[k]);
      }).join("&");
    }
    var filters = h("form", { class: "filters panel-body", onsubmit: apply },
      field(t("list.state"), stateSel),
      !isRequester() ? field(t("list.group"), groupSel) : null,
      field(t("list.search"), qInput),
      h("div", null, h("button", { class: "primary", type: "submit" }, t("common.apply"))));

    var tbody = h("tbody", null, h("tr", null, h("td", { colspan: 7, class: "muted", text: t("common.loading") })));
    var pager = h("div", { class: "pager" });
    render(h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h1", { text: title }), h("span", { class: "spacer" }),
        canCreate() ? h("a", { class: "btn", href: "#/new" }, t("list.new")) : null),
      filters,
      h("table", { class: "list" },
        h("thead", null, h("tr", null, ["col.number", "col.short_description", "col.state", "col.priority",
          "col.group", "col.assigned_to", "col.updated"].map(function (c) { return h("th", { text: t(c) }); }))),
        tbody),
      pager));

    var qs = Object.keys(query).map(function (k) { return k + "=" + encodeURIComponent(query[k]); }).join("&");
    api("GET", "/tickets?" + qs).then(function (data) {
      clear(tbody);
      if (!data.items.length) {
        tbody.appendChild(h("tr", null, h("td", { colspan: 7, class: "muted", text: t("list.empty") })));
      }
      data.items.forEach(function (x) {
        tbody.appendChild(h("tr", null,
          h("td", null, h("a", { href: "#/tickets/" + encodeURIComponent(x.number), text: x.number })),
          h("td", { text: x.short_description }),
          h("td", null, stateBadge(x.state)),
          h("td", { class: "prio-" + x.priority, text: label("priority.", x.priority) }),
          h("td", { text: x.assignment_group || "" }),
          h("td", { text: x.assigned_to || "" }),
          h("td", { text: fmtTime(x.updated_at) })));
      });
      var from = data.total ? query.offset + 1 : 0;
      var to = Math.min(query.offset + query.limit, data.total);
      pager.appendChild(h("span", { text: t("list.range", { from: from, to: to, total: data.total }) }));
      function page(delta) {
        var p = Object.assign({}, params, { offset: Math.max(0, query.offset + delta) });
        location.hash = "#/tickets?" + Object.keys(p).map(function (k) {
          return k + "=" + encodeURIComponent(p[k]);
        }).join("&");
      }
      pager.appendChild(h("button", { disabled: query.offset === 0, onclick: function () { page(-query.limit); } }, t("common.previous")));
      pager.appendChild(h("button", { disabled: to >= data.total, onclick: function () { page(query.limit); } }, t("common.next")));
    }).catch(fail);
  }

  // ------------------------------------------------------ custom field input
  function customInput(def, value) {
    var id = "cf-" + def.name;
    if (def.type === "boolean") {
      return select(id, [["true", t("common.yes")], ["false", t("common.no")]],
        value === true ? "true" : value === false ? "false" : "", "-");
    }
    if (def.type === "choice") return select(id, def.choices, value == null ? "" : value, "-");
    var input = h("input", { id: id, value: value == null ? "" : String(value) });
    if (def.type === "number") { input.type = "number"; input.step = "any"; }
    if (def.type === "date") input.type = "date";
    return input;
  }

  function readCustom(def, el) {
    var raw = el.value;
    if (raw === "") return null;
    if (def.type === "number") return Number(raw);
    if (def.type === "boolean") return raw === "true";
    return raw;
  }

  // ----------------------------------------------------------- new ticket
  function viewNew() {
    var requester = isRequester();
    var short = h("input", { id: "n-short", required: true, maxlength: 500 });
    var desc = h("textarea", { id: "n-desc" });
    var prio = select("n-prio", priorityOptions(), "3");
    var cat = h("input", { id: "n-cat", placeholder: t("ticket.category_placeholder") });
    var group = select("n-group", session.teams.map(function (x) { return x.name; }), "", t("common.none"));
    var assignee = select("n-assignee", session.users.map(function (u) { return [u.username, u.display_name]; }),
      "", t("common.none"));
    var customEls = {};
    var customs = session.fields.map(function (def) {
      customEls[def.name] = customInput(def, null);
      return field(def.label, customEls[def.name], { required: def.required });
    });
    var form = h("form", {
      onsubmit: function (ev) {
        ev.preventDefault();
        var body = {
          short_description: short.value, description: desc.value,
          priority: parseInt(prio.value, 10), category: cat.value
        };
        if (!requester) {
          body.assignment_group = group.value || null;
          body.assigned_to = assignee.value || null;
        }
        var cf = {};
        session.fields.forEach(function (def) {
          var v = readCustom(def, customEls[def.name]);
          if (v !== null) cf[def.name] = v;
        });
        body.custom_fields = cf;
        api("POST", "/tickets", body).then(function (x) {
          toast(t("ticket.created", { number: x.number }));
          location.hash = "#/tickets/" + encodeURIComponent(x.number);
        }).catch(fail);
      }
    },
      h("div", { class: "panel-body form-grid" },
        field(t("ticket.short_description"), short, { required: true, wide: true }),
        field(t("ticket.description"), desc, { wide: true }),
        field(t("ticket.priority"), prio), field(t("ticket.category"), cat),
        requester ? null : field(t("ticket.group"), group),
        requester ? null : field(t("ticket.assigned_to"), assignee),
        customs),
      h("div", { class: "panel-body" }, h("button", { class: "primary", type: "submit" }, t("common.submit"))));
    render(h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h1", { text: t("ticket.new_title") })), form));
    short.focus();
  }

  // --------------------------------------------------------- ticket detail
  function viewDetail(number) {
    render(h("p", { class: "muted", text: t("ticket.loading", { number: number }) }));
    Promise.all([
      api("GET", "/tickets/" + encodeURIComponent(number)),
      api("GET", "/tickets/" + encodeURIComponent(number) + "/journal")
    ]).then(function (res) { drawDetail(res[0], res[1]); }).catch(fail);
  }

  function drawDetail(tk, journal) {
    var me = session.me;
    var requester = isRequester();
    var editable = !requester && canModify(tk);
    var path = "/tickets/" + encodeURIComponent(tk.number);

    function ro(el) { if (!editable) el.disabled = true; return el; }
    var short = ro(h("input", { id: "d-short", value: tk.short_description }));
    var desc = ro(h("textarea", { id: "d-desc", value: tk.description }));
    var prio = ro(select("d-prio", priorityOptions(), tk.priority));
    var cat = ro(h("input", { id: "d-cat", value: tk.category }));
    var groupNames = session.teams.map(function (x) { return x.name; });
    if (tk.assignment_group && groupNames.indexOf(tk.assignment_group) < 0) groupNames.push(tk.assignment_group);
    var group = ro(select("d-group", groupNames, tk.assignment_group || "", t("common.none")));
    var userOpts = session.users.map(function (u) { return [u.username, u.display_name]; });
    if (tk.assigned_to && !session.users.some(function (u) { return u.username === tk.assigned_to; })) {
      userOpts.push([tk.assigned_to, tk.assigned_to]);
    }
    var assignee = ro(select("d-assignee", userOpts, tk.assigned_to || "", t("common.none")));
    var closeNotes = ro(h("textarea", { id: "d-close", value: tk.close_notes }));
    var customEls = {};
    var customs = session.fields.map(function (def) {
      customEls[def.name] = ro(customInput(def, tk.custom_fields[def.name]));
      return field(def.label, customEls[def.name], { required: def.required });
    });

    function collectChanges() {
      var body = {};
      if (short.value !== tk.short_description) body.short_description = short.value;
      if (desc.value !== tk.description) body.description = desc.value;
      if (parseInt(prio.value, 10) !== tk.priority) body.priority = parseInt(prio.value, 10);
      if (cat.value !== tk.category) body.category = cat.value;
      if ((group.value || null) !== tk.assignment_group) body.assignment_group = group.value || null;
      if ((assignee.value || null) !== tk.assigned_to) body.assigned_to = assignee.value || null;
      if (closeNotes.value !== tk.close_notes) body.close_notes = closeNotes.value;
      var cf = {};
      session.fields.forEach(function (def) {
        var v = readCustom(def, customEls[def.name]);
        var old = tk.custom_fields[def.name];
        if (v !== (old === undefined ? null : old)) cf[def.name] = v;
      });
      if (Object.keys(cf).length) body.custom_fields = cf;
      return body;
    }

    function patch(body, message) {
      api("PATCH", path, body).then(function () {
        toast(message);
        viewDetail(tk.number);
      }).catch(fail);
    }

    var actions = h("div", { class: "actions" });
    (TRANSITIONS[tk.state] || []).forEach(function (target) {
      var allowed = (editable && (CLOSING_STATES.indexOf(target) < 0 || can("tickets.close"))) ||
        (requester && target === "cancelled" && tk.caller === me.username && can("tickets.create"));
      if (!allowed) return;
      actions.appendChild(h("button", {
        class: target === "resolved" ? "primary" : target === "cancelled" ? "danger" : null,
        onclick: function () {
          var body = requester ? {} : collectChanges();
          body.state = target;
          if (target === "resolved" && !closeNotes.value.trim()) {
            toast(t("ticket.close_notes_required"), true);
            closeNotes.focus();
            return;
          }
          patch(body, t("ticket.state_changed", { state: label("state.", target) }));
        }
      }, t("action." + target)));
    });
    if (editable) {
      actions.appendChild(h("button", {
        class: "primary", onclick: function () {
          var body = collectChanges();
          if (!Object.keys(body).length) { toast(t("common.nothing_changed")); return; }
          patch(body, t("common.saved"));
        }
      }, t("common.save")));
    }

    var refs = Object.keys(tk.external_refs || {});
    var header = h("div", { class: "panel-head" },
      h("h1", null, tk.number + " "), stateBadge(tk.state), h("span", { class: "spacer" }), actions);

    var times = [t("ticket.created_at", { time: fmtTime(tk.created_at) }),
      t("ticket.updated_at", { time: fmtTime(tk.updated_at) })];
    if (tk.resolved_at) times.push(t("ticket.resolved_at", { time: fmtTime(tk.resolved_at) }));

    var formBody = h("div", { class: "panel-body form-grid" },
      field(t("ticket.number"), h("input", { id: "d-number", value: tk.number, disabled: true })),
      field(t("ticket.caller"), h("input", { id: "d-caller", value: tk.caller, disabled: true })),
      field(t("ticket.short_description"), short, { required: true, wide: true }),
      field(t("ticket.description"), desc, { wide: true }),
      field(t("ticket.priority"), prio), field(t("ticket.category"), cat),
      field(t("ticket.group"), group), field(t("ticket.assigned_to"), assignee),
      customs,
      field(t("ticket.close_notes"), closeNotes, { wide: true }),
      h("div", { class: "wide muted" }, times.join(" - ")),
      refs.length ? h("div", { class: "wide" }, h("label", { text: t("ticket.external_refs") }),
        h("div", { class: "mono" }, refs.map(function (k) { return k + ": " + tk.external_refs[k]; }).join("  |  ")))
        : null);

    render(h("div", { class: "panel" }, header, formBody), drawJournal(tk, journal, editable));
  }

  // Journal tabs of the ticket detail page (WAI-ARIA tab pattern). The selected tab lives in the
  // hash, e.g. #/tickets/TKT0001001?tab=worknotes, and is updated with replaceState (no reload).
  function journalWho(e) {
    return e.author ? t("journal.via", { author: e.author, actor: e.actor }) : e.actor;
  }

  function journalItem(e) {
    var parts = [label("journal.type.", e.type), journalWho(e), fmtTime(e.at)];
    if (e.source) parts.push(t("journal.via_source", { source: e.source }));
    var meta = h("div", { class: "meta" }, parts.join(" - "));
    var body;
    if (e.type === "audit") {
      body = h("div", { class: "body" }, e.field === "created" ? t("journal.ticket_created")
        : e.field + ": " + fmtValue(e.old) + " → " + fmtValue(e.new));
    } else {
      body = h("div", { class: "body", text: e.body });
    }
    return h("li", { class: e.type }, meta, body);
  }

  function journalList(entries, emptyText) {
    if (!entries.length) return h("p", { class: "muted", text: emptyText });
    return h("ul", { class: "journal" }, entries.slice().reverse().map(journalItem));
  }

  function journalForm(tk, kind, title, placeholder) {
    var id = "j-" + kind;
    var text = h("textarea", { id: id, placeholder: placeholder });
    return h("form", {
      class: "journal-form",
      onsubmit: function (ev) {
        ev.preventDefault();
        if (!text.value.trim()) return;
        api("POST", "/tickets/" + encodeURIComponent(tk.number) + "/comments", { body: text.value, type: kind })
          .then(function () { toast(t("journal.posted")); viewDetail(tk.number); }).catch(fail);
      }
    }, h("label", { for: id, text: title }), text,
      h("div", null, h("button", { class: "primary", type: "submit" }, title)));
  }

  function summaryBlock(e) {
    return h("div", { class: "summary-entry" },
      h("div", { class: "meta" }, journalWho(e) + " - " + fmtTime(e.at)),
      h("div", { class: "body", text: e.body }));
  }

  function resolutionReport(tk, summaries) {
    var latest = summaries[summaries.length - 1];
    var earlier = summaries.slice(0, -1).reverse();
    return h("div", { class: "report" },
      h("h3", { text: t("report.close_notes") }),
      tk.close_notes ? h("div", { class: "body", text: tk.close_notes })
        : h("p", { class: "muted", text: t("report.not_resolved") }),
      h("p", { class: "muted" }, tk.resolved_at ? t("report.resolved", { time: fmtTime(tk.resolved_at) })
        : t("report.resolved_none")),
      h("h3", { text: t("report.summary") }),
      latest ? summaryBlock(latest) : h("p", { class: "muted", text: t("report.no_summary") }),
      earlier.length ? h("details", { class: "earlier" },
        h("summary", { text: t("report.earlier", { n: earlier.length }) }),
        earlier.map(summaryBlock)) : null);
  }

  var AUDIT_TRAIL_COLUMNS = ["col.time", "col.field", "col.old_new", "col.actor", "col.source"];

  function auditTrail(entries) {
    if (!entries.length) return h("p", { class: "muted", text: t("journal.no_field_changes") });
    return table(AUDIT_TRAIL_COLUMNS.map(function (c) { return t(c); }), entries.slice().reverse().map(function (e) {
      return h("tr", null,
        h("td", { text: fmtTime(e.at) }),
        h("td", { class: "mono", text: e.field }),
        h("td", { class: "pre-wrap" }, e.field === "created" ? t("journal.ticket_created")
          : fmtValue(e.old) + " → " + fmtValue(e.new)),
        h("td", { text: journalWho(e) }),
        h("td", { text: e.source || "" }));
    }));
  }

  function drawJournal(tk, journal, editable) {
    var me = session.me;
    var requester = isRequester();
    var canComment = requester ? tk.caller === me.username : editable;
    function ofType(type) { return journal.filter(function (e) { return e.type === type; }); }
    var notes = ofType("work_note"), comments = ofType("comment"), summaries = ofType("summary");
    var audits = ofType("audit");
    var canNote = canComment && can("tickets.work_note");
    // self-service users need tickets.comment too (custom roles without tickets.read_all)
    var canPublic = canComment && can("tickets.comment");

    var defs = [];
    if (!requester) {
      defs.push({ id: "worknotes", title: t("tabs.worknotes", { n: notes.length }), content: [
        canNote ? journalForm(tk, "work_note", t("journal.add_work_note"), t("journal.work_note_placeholder")) : null,
        journalList(notes, t("journal.no_work_notes"))] });
    }
    defs.push({ id: "comments", title: t("tabs.comments", { n: comments.length }), content: [
      canPublic ? journalForm(tk, "comment", t("journal.add_comment"), t("journal.comment_placeholder")) : null,
      journalList(comments, t("journal.no_comments"))] });
    defs.push({ id: "resolution", title: t("tabs.resolution"), content: [resolutionReport(tk, summaries)] });
    defs.push({ id: "audit", title: t("tabs.audit", { n: audits.length }), content: [auditTrail(audits)] });
    defs.push({ id: "history", title: t("tabs.history", { n: journal.length }), content: [
      journalList(journal, t("journal.no_activity"))] });

    var wanted = parseHash().params.tab;
    var current = defs.some(function (d) { return d.id === wanted; }) ? wanted : defs[0].id;
    var tablist = h("div", { class: "tablist", role: "tablist", "aria-label": t("tabs.label") });
    var tabs = [], panels = [];

    function selectTab(id, focus) {
      current = id;
      tabs.forEach(function (tab, i) {
        var on = defs[i].id === id;
        tab.setAttribute("aria-selected", on ? "true" : "false");
        tab.tabIndex = on ? 0 : -1;
        panels[i].hidden = !on;
        if (on && focus) tab.focus();
      });
      var url = "#/tickets/" + encodeURIComponent(tk.number) + "?tab=" + id;
      if (location.hash !== url) history.replaceState(null, "", url);
    }

    defs.forEach(function (d, i) {
      var tab = h("button", {
        type: "button", role: "tab", id: "tab-" + d.id, "aria-controls": "tabpanel-" + d.id,
        "aria-selected": "false", tabindex: "-1",
        onclick: function () { selectTab(d.id, false); },
        onkeydown: function (ev) {
          var next = null;
          if (ev.key === "ArrowRight") next = (i + 1) % defs.length;
          else if (ev.key === "ArrowLeft") next = (i - 1 + defs.length) % defs.length;
          else if (ev.key === "Home") next = 0;
          else if (ev.key === "End") next = defs.length - 1;
          if (next === null) return;
          ev.preventDefault();
          selectTab(defs[next].id, true);
        }
      }, d.title);
      tabs.push(tab);
      tablist.appendChild(tab);
      panels.push(h("div", {
        class: "panel-body tabpanel", role: "tabpanel", id: "tabpanel-" + d.id,
        "aria-labelledby": "tab-" + d.id, tabindex: "0"
      }, d.content));
    });
    selectTab(current, false);

    return h("div", { class: "panel" },
      h("div", { class: "panel-head tabs-head" }, tablist), panels);
  }

  function fmtValue(v) {
    if (v === null || v === undefined || v === "") return t("journal.empty_value");
    if (typeof v === "object") return JSON.stringify(v);
    return String(v);
  }

  // ---------------------------------------------------------------- profile
  var THEMES = ["system", "light", "dark"];
  var MIN_PASSWORD = 8;

  // Returns the i18n key of the problem with a new password (typed twice), or null when it is fine.
  function passwordProblem(first, second) {
    if (first.length < MIN_PASSWORD) return "pw.too_short";
    if (first !== second) return "pw.mismatch";
    return null;
  }

  function browserTimeZone() {
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) { return ""; }
  }

  // IANA time zone picker: a select when the browser can list the zones, else a text input. "" = browser default.
  function timeZoneControl(id, current) {
    var zones = [];
    try { if (Intl.supportedValuesOf) zones = Intl.supportedValuesOf("timeZone"); } catch (e) { zones = []; }
    if (!zones.length) {
      return h("input", { id: id, value: current, placeholder: t("profile.tz_placeholder"),
        "aria-describedby": id + "-hint" });
    }
    if (current && zones.indexOf(current) < 0) zones = [current].concat(zones);
    return select(id, zones, current, t("profile.tz_browser", { tz: browserTimeZone() || "-" }));
  }

  function viewProfile() {
    var me = session.me;
    var tenant = me.active_tenant || me.tenant || {};
    var display = h("input", { id: "p-display", value: me.display_name || "", required: true, maxlength: 200,
      autocomplete: "name" });
    var email = h("input", { id: "p-email", type: "email", value: me.email || "", autocomplete: "email" });
    var tz = timeZoneControl("p-tz", me.timezone || "");
    var theme = select("p-theme", THEMES.map(function (x) { return [x, t("theme." + x)]; }), me.theme || themePref);
    var lang = select("p-lang", I18N.supported.map(function (l) { return [l, t("lang." + l)]; }),
      me.language || I18N.lang);
    var profileForm = h("form", {
      class: "panel-body form-grid", onsubmit: function (ev) {
        ev.preventDefault();
        var body = { display_name: display.value.trim(), email: email.value.trim(), timezone: tz.value.trim(),
          theme: theme.value, language: lang.value };
        api("PATCH", "/me", body).then(function (updated) {
          return applyMe(updated).then(function () { toast(t("profile.saved")); viewProfile(); });
        }).catch(fail);
      }
    },
      field(t("profile.username"), h("input", { id: "p-user", value: me.username, disabled: true })),
      field(t("profile.role"), h("input", { id: "p-role", value: roleLabel(me.role), disabled: true })),
      field(t("profile.display_name"), display, { required: true }),
      field(t("profile.email"), email),
      field(t("profile.timezone"), tz),
      field(t("profile.theme"), theme),
      field(t("profile.language"), lang),
      tenant.name ? field(t("profile.tenant"), h("input", { id: "p-tenant", value: tenant.name, disabled: true })) : null,
      h("div", { class: "wide muted", id: "p-tz-hint", text: t("profile.tz_hint") }),
      h("div", { class: "wide" }, h("button", { class: "primary", type: "submit" }, t("profile.save"))));

    var current = h("input", { id: "pw-current", type: "password", autocomplete: "current-password", required: true });
    var next = h("input", { id: "pw-new", type: "password", autocomplete: "new-password", required: true,
      "aria-describedby": "pw-hint" });
    var repeat = h("input", { id: "pw-repeat", type: "password", autocomplete: "new-password", required: true });
    var message = h("div", { class: "wide error", role: "alert" });
    var pwForm = h("form", {
      class: "panel-body form-grid", onsubmit: function (ev) {
        ev.preventDefault();
        message.textContent = "";
        var problem = passwordProblem(next.value, repeat.value);
        if (problem) { message.textContent = t(problem); next.focus(); return; }
        api("POST", "/me/password", { current_password: current.value, new_password: next.value }).then(function () {
          current.value = next.value = repeat.value = "";
          toast(t("pw.changed"));
        }).catch(function (err) {
          if (err && err.status === 401) { fail(err); return; }
          message.textContent = err && err.status === 403 ? t("pw.wrong_current") : errorText(err);
        });
      }
    },
      field(t("pw.current"), current, { required: true, wide: true }),
      field(t("pw.new"), next, { required: true }), field(t("pw.repeat"), repeat, { required: true }),
      h("div", { class: "wide muted", id: "pw-hint", text: t("pw.hint") }),
      message,
      h("div", { class: "wide" }, h("button", { class: "primary", type: "submit" }, t("pw.change"))));

    render(
      h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h1", { text: t("profile.title") })), profileForm),
      h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", { text: t("pw.change") })), pwForm));
    display.focus();
  }

  // ----------------------------------------------------------------- admin
  function adminPanel(title, formEl, tableEl, extra) {
    return h("div", null,
      h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h1", { text: title })), tableEl),
      formEl ? h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", { text: t("common.add") })), formEl) : null,
      extra || null);
  }

  function table(headers, rows) {
    return h("table", { class: "list" },
      h("thead", null, h("tr", null, headers.map(function (x) { return h("th", { text: x }); }))),
      h("tbody", null, rows));
  }

  function refreshReference() { return loadReference().catch(function () {}); }

  function activeText(active) { return active ? t("common.active") : t("common.inactive"); }

  // Roles for the selects: GET /roles (built-in + custom of the tenant); older servers have no /roles.
  var FALLBACK_ROLES = ["admin", "agent", "requester", "integration"];

  function roleOptions(includePlatform, current) {
    var names = session.roles.length ? session.roles.map(function (r) { return r.name; }) : FALLBACK_ROLES.slice();
    names = names.filter(function (n) { return n !== "platform_admin"; });
    if (includePlatform) names.push("platform_admin");
    if (current && names.indexOf(current) < 0) names.push(current);
    return names.map(function (n) { return [n, roleLabel(n)]; });
  }

  // Modal dialog: new password twice; mismatch or < 8 characters never sends a request.
  function passwordDialog(u) {
    var first = h("input", { id: "sp-new", type: "password", autocomplete: "new-password", required: true,
      "aria-describedby": "sp-hint" });
    var second = h("input", { id: "sp-repeat", type: "password", autocomplete: "new-password", required: true });
    var message = h("div", { class: "error", role: "alert" });
    var dlg = h("dialog", { class: "modal", "aria-labelledby": "sp-title" });
    function close() { if (dlg.close) dlg.close(); else dlg.remove(); }
    dlg.addEventListener("close", function () { dlg.remove(); });
    dlg.appendChild(h("form", {
      onsubmit: function (ev) {
        ev.preventDefault();
        message.textContent = "";
        var problem = passwordProblem(first.value, second.value);
        if (problem) { message.textContent = t(problem); first.focus(); return; }
        api("PATCH", "/users/" + encodeURIComponent(u.username), { password: first.value }).then(function () {
          close();
          toast(t("users.password_set", { user: u.username }));
        }).catch(function (err) {
          if (err && err.status === 401) { close(); fail(err); return; }
          message.textContent = errorText(err);
        });
      }
    },
      h("div", { class: "panel-head" }, h("h2", { id: "sp-title", text: t("users.set_password_for", { user: u.username }) })),
      h("div", { class: "panel-body" },
        field(t("pw.new"), first, { required: true }), field(t("pw.repeat"), second, { required: true }),
        h("div", { class: "muted", id: "sp-hint", text: t("pw.hint") }), message,
        h("div", { class: "actions" },
          h("button", { class: "primary", type: "submit" }, t("users.set_password")),
          h("button", { type: "button", onclick: close }, t("common.cancel"))))));
    document.body.appendChild(dlg);
    if (dlg.showModal) dlg.showModal(); else dlg.setAttribute("open", "");
    first.focus();
  }

  function viewUsers() {
    Promise.all([api("GET", "/users"), loadRoles()]).then(function (res) {
      var users = res[0];
      session.users = users;
      var secretBox = h("div");
      var platform = isPlatform();

      function userRow(u, editing) {
        // changing a role to or from platform_admin is not possible (422), so that select is read-only
        var isPa = u.role === "platform_admin";
        var path = "/users/" + encodeURIComponent(u.username);
        var role = select("role-" + u.username, isPa ? [["platform_admin", roleLabel("platform_admin")]]
          : roleOptions(false, u.role), u.role);
        role.setAttribute("aria-label", t("users.role_of", { user: u.username }));
        if (isPa) role.disabled = true;
        role.addEventListener("change", function () {
          api("PATCH", path, { role: role.value })
            .then(function () { toast(t("users.role_updated")); }).catch(function (e) { fail(e); viewUsers(); });
        });
        var display = h("input", { id: "ue-display-" + u.username, value: u.display_name || "", maxlength: 200,
          "aria-label": t("users.display_name_of", { user: u.username }) });
        var email = h("input", { id: "ue-email-" + u.username, type: "email", value: u.email || "",
          "aria-label": t("users.email_of", { user: u.username }) });
        var row;
        function swap(edit) {
          var next = userRow(u, edit);
          row.parentNode.replaceChild(next, row);
          var focusEl = edit ? next.querySelector("input") : next.querySelector("button");
          if (focusEl) focusEl.focus();
        }
        var actions = editing ? [
          h("button", {
            type: "button", class: "primary", onclick: function () {
              var body = {};
              if (display.value.trim() !== (u.display_name || "")) body.display_name = display.value.trim();
              if (email.value.trim() !== (u.email || "")) body.email = email.value.trim();
              if (!Object.keys(body).length) { swap(false); return; }
              api("PATCH", path, body).then(function (saved) {
                Object.assign(u, saved || body);
                toast(t("users.saved", { user: u.username }));
                swap(false);
              }).catch(fail);
            }
          }, t("common.save")),
          h("button", { type: "button", onclick: function () { swap(false); } }, t("common.cancel"))
        ] : [
          h("button", {
            type: "button", "aria-label": t("users.edit_aria", { user: u.username }),
            onclick: function () { swap(true); }
          }, t("common.edit")),
          h("button", {
            type: "button", onclick: function () {
              api("POST", path + "/tokens", { name: "ui" }).then(function (tok) {
                clear(secretBox);
                secretBox.appendChild(h("div", { class: "secret-box" },
                  h("strong", { text: t("users.token_shown", { user: u.username }) }),
                  h("div", { class: "mono", text: tok.token })));
              }).catch(fail);
            }
          }, t("users.new_token")),
          h("button", {
            type: "button", onclick: function () {
              api("PATCH", path, { active: !u.active }).then(viewUsers).catch(fail);
            }
          }, u.active ? t("users.deactivate") : t("users.activate")),
          h("button", { type: "button", onclick: function () { passwordDialog(u); } }, t("users.set_password"))
        ];
        row = h("tr", null,
          h("td", { text: u.username }),
          editing ? h("td", null, display) : h("td", { text: u.display_name }),
          editing ? h("td", null, email) : h("td", { text: u.email || "" }),
          h("td", null, role),
          platform ? h("td", { text: u.tenant || t("users.platform") }) : null,
          h("td", { text: u.teams.join(", ") }), h("td", { text: activeText(u.active) }),
          h("td", { class: "actions" }, actions));
        return row;
      }

      var rows = users.map(function (u) { return userRow(u, false); });
      var name = h("input", { id: "u-name", required: true });
      var display = h("input", { id: "u-display" });
      var email = h("input", { id: "u-email", type: "email" });
      var roleSel = select("u-role", roleOptions(platform), "agent");
      var pw = h("input", { id: "u-pw", type: "password", autocomplete: "new-password", "aria-describedby": "u-pw-hint" });
      var teamSel = select("u-team", session.teams.map(function (x) { return x.name; }), "", t("common.none"));
      var activeSlug = (session.me.active_tenant || session.me.tenant || {}).slug;
      // platform admins choose the tenant of the new user; tenant admins always create in their own tenant
      var tenantSel = platform ? select("u-tenant", (session.me.tenants || []).map(function (x) {
        return [x.slug, x.name + " (" + x.slug + ")" + (x.active ? "" : t("tenant.inactive_suffix"))];
      }), activeSlug) : null;
      var tenantHint = h("div", { class: "wide muted", id: "u-tenant-hint" });
      var message = h("div", { class: "wide error", role: "alert" });
      function syncTenantFields() {
        if (!platform) return;
        var noTenant = roleSel.value === "platform_admin";
        tenantSel.disabled = noTenant;
        // teams are listed for the active tenant only
        teamSel.disabled = noTenant || tenantSel.value !== activeSlug;
        if (teamSel.disabled) teamSel.value = "";
        tenantHint.textContent = noTenant ? t("users.hint_platform_no_tenant")
          : tenantSel.value !== activeSlug ? t("users.hint_teams_active_tenant") : "";
      }
      if (platform) {
        roleSel.addEventListener("change", syncTenantFields);
        tenantSel.addEventListener("change", syncTenantFields);
      }
      var form = h("form", {
        class: "panel-body form-grid", onsubmit: function (ev) {
          ev.preventDefault();
          message.textContent = "";
          if (pw.value && pw.value.length < MIN_PASSWORD) { message.textContent = t("pw.too_short"); pw.focus(); return; }
          var body = { username: name.value.trim(), display_name: display.value.trim(), role: roleSel.value };
          if (email.value.trim()) body.email = email.value.trim();
          if (pw.value) body.password = pw.value;
          if (teamSel.value && !teamSel.disabled) body.teams = [teamSel.value];
          if (platform && roleSel.value !== "platform_admin") body.tenant = tenantSel.value;
          api("POST", "/users", body).then(function (u) {
            toast(u.tenant && u.tenant !== activeSlug ? t("users.created_in_tenant", { tenant: u.tenant })
              : t("users.created"));
            refreshReference().then(viewUsers);
          }).catch(function (err) {
            if (err && err.status === 401) { fail(err); return; }
            message.textContent = errorText(err);
          });
        }
      }, field(t("users.username"), name, { required: true }), field(t("users.display_name"), display),
        field(t("users.email"), email), field(t("users.role"), roleSel),
        platform ? field(t("users.tenant"), tenantSel, { required: true }) : null,
        field(t("users.password_optional"), pw), field(t("users.team"), teamSel), tenantHint,
        h("div", { class: "wide muted", id: "u-pw-hint", text: t("pw.hint") }), message,
        h("div", { class: "wide" }, h("button", { class: "primary", type: "submit" }, t("users.create"))));
      syncTenantFields();
      var headers = [t("users.username"), t("users.display_name"), t("users.email"), t("users.role")]
        .concat(platform ? [t("users.tenant")] : [])
        .concat([t("users.teams"), t("users.status"), ""]);
      render(adminPanel(t("users.title"),
        form, h("div", null, secretBox, table(headers, rows))));
    }).catch(fail);
  }

  // ------------------------------------------------------------------ roles
  function permLabel(p) { return label("perm.", p); }

  function roleDescription(r) {
    return r.builtin && I18N.has("roles.desc." + r.name) ? t("roles.desc." + r.name) : (r.description || "");
  }

  // Create (role = null) or edit form of a custom role; only grantable permissions get a checkbox.
  function roleForm(role, grantable, onCancel) {
    var prefix = role ? "re-" : "rn-";
    var name = h("input", { id: prefix + "name", required: true, pattern: "[a-z][a-z0-9_]{1,31}", maxlength: 32,
      value: role ? role.name : "", disabled: !!role, placeholder: t("roles.name_placeholder"),
      "aria-describedby": prefix + "name-hint" });
    var labelInput = h("input", { id: prefix + "label", required: true, maxlength: 100, value: role ? role.label || "" : "" });
    var desc = h("input", { id: prefix + "desc", maxlength: 500, value: role ? role.description || "" : "" });
    var boxes = grantable.map(function (p) {
      return h("input", { type: "checkbox", id: prefix + "perm-" + p.replace(/\./g, "-"), value: p,
        checked: !!role && role.permissions.indexOf(p) >= 0 });
    });
    var form = h("form", {
      class: "panel-body form-grid", onsubmit: function (ev) {
        ev.preventDefault();
        var perms = boxes.filter(function (b) { return b.checked; }).map(function (b) { return b.value; });
        var body = { label: labelInput.value.trim(), description: desc.value.trim(), permissions: perms };
        var req = role ? api("PATCH", "/roles/" + encodeURIComponent(role.name), body)
          : api("POST", "/roles", Object.assign({ name: name.value.trim() }, body));
        req.then(function () {
          toast(t(role ? "roles.saved" : "roles.created", { name: role ? role.name : name.value.trim() }));
          viewRoles();
        }).catch(fail);
      }
    },
      field(t("roles.name"), name, { required: true }), field(t("roles.label"), labelInput, { required: true }),
      field(t("roles.description"), desc, { wide: true }),
      h("div", { class: "wide muted", id: prefix + "name-hint", text: t("roles.name_hint") }),
      h("div", { class: "wide", role: "group", "aria-labelledby": prefix + "perms-title" },
        h("label", { id: prefix + "perms-title", text: t("roles.permissions") }),
        h("div", { class: "checks" }, boxes.map(function (b) {
          return h("label", { class: "check", for: b.id }, b, permLabel(b.value));
        })),
        h("p", { class: "muted", text: t("roles.grantable_hint") })),
      h("div", { class: "wide actions" },
        h("button", { class: "primary", type: "submit" }, role ? t("common.save") : t("roles.create")),
        onCancel ? h("button", { type: "button", onclick: onCancel }, t("common.cancel")) : null));
    return form;
  }

  function viewRoles() {
    if (!can("roles.manage")) { render(h("p", { class: "error", text: t("roles.only_admins") })); return; }
    Promise.all([api("GET", "/roles"), api("GET", "/permissions")]).then(function (res) {
      var roles = res[0] || [];
      session.roles = roles;
      var grantable = (res[1] || []).filter(function (p) { return p.grantable; }).map(function (p) { return p.name; });
      var editBox = h("div");
      function edit(r) {
        clear(editBox);
        var form = roleForm(r, grantable, function () { clear(editBox); });
        editBox.appendChild(h("div", { class: "panel" },
          h("div", { class: "panel-head" }, h("h2", { text: t("roles.edit_title", { name: r.name }) })), form));
        form.querySelector("input:not([disabled])").focus();
      }
      var rows = roles.map(function (r) {
        return h("tr", null,
          h("td", { class: "mono", text: r.name }),
          h("td", { text: r.builtin ? roleLabel(r.name) : (r.label || r.name) }),
          h("td", { text: roleDescription(r) }),
          h("td", { text: r.builtin ? t("roles.builtin") : t("roles.custom") }),
          h("td", { text: r.permissions.length ? r.permissions.map(permLabel).join(", ") : t("roles.none") }),
          h("td", { text: String(r.users == null ? "" : r.users) }),
          h("td", { class: "actions" }, r.builtin ? h("span", { class: "muted", text: t("roles.read_only") }) : [
            h("button", { type: "button", "aria-label": t("roles.edit_aria", { name: r.name }),
              onclick: function () { edit(r); } }, t("common.edit")),
            h("button", {
              type: "button", class: "danger", "aria-label": t("roles.delete_aria", { name: r.name }),
              onclick: function () {
                if (!window.confirm(t("roles.delete_confirm", { name: r.name }))) return;
                api("DELETE", "/roles/" + encodeURIComponent(r.name)).then(function () {
                  toast(t("roles.deleted", { name: r.name }));
                  viewRoles();
                }).catch(function (err) {
                  if (err && err.status === 409) toast(t("roles.in_use", { name: r.name }), true);
                  else fail(err);
                });
              }
            }, t("common.delete"))]));
      });
      render(adminPanel(t("roles.title"), roleForm(null, grantable, null),
        h("div", null, h("p", { class: "panel-body muted", text: t("roles.read_only_hint") }),
          table([t("roles.name"), t("roles.label"), t("roles.description"), t("roles.type"), t("roles.permissions"),
            t("roles.users"), ""], rows)), editBox));
    }).catch(fail);
  }

  function viewTeams() {
    api("GET", "/teams").then(function (teams) {
      session.teams = teams;
      var rows = teams.map(function (tm) {
        var add = select("add-" + tm.name, session.users.map(function (u) { return u.username; }), "",
          t("teams.add_member"));
        add.addEventListener("change", function () {
          if (!add.value) return;
          api("POST", "/teams/" + encodeURIComponent(tm.name) + "/members", { username: add.value })
            .then(function () { refreshReference().then(viewTeams); }).catch(fail);
        });
        return h("tr", null, h("td", { text: tm.name }), h("td", { text: tm.description }),
          h("td", null, tm.members.map(function (m) {
            return h("span", { class: "badge" }, m + " ", h("button", {
              class: "link", title: t("teams.remove", { member: m }), "aria-label": t("teams.remove", { member: m }),
              onclick: function () {
                api("DELETE", "/teams/" + encodeURIComponent(tm.name) + "/members/" + encodeURIComponent(m))
                  .then(function () { refreshReference().then(viewTeams); }).catch(fail);
              }
            }, "x"));
          })), h("td", null, add));
      });
      var name = h("input", { id: "t-name", required: true });
      var desc = h("input", { id: "t-desc" });
      var form = h("form", {
        class: "panel-body form-grid", onsubmit: function (ev) {
          ev.preventDefault();
          api("POST", "/teams", { name: name.value, description: desc.value })
            .then(function () { toast(t("teams.created")); refreshReference().then(viewTeams); }).catch(fail);
        }
      }, field(t("teams.name"), name, { required: true }), field(t("teams.description"), desc),
        h("div", { class: "wide" }, h("button", { class: "primary", type: "submit" }, t("teams.create"))));
      render(adminPanel(t("teams.title"), form,
        table([t("teams.name"), t("teams.description"), t("teams.members"), ""], rows)));
    }).catch(fail);
  }

  function viewFields() {
    api("GET", "/custom-fields").then(function (fields) {
      session.fields = fields;
      var rows = fields.map(function (f) {
        return h("tr", null, h("td", { class: "mono", text: f.name }), h("td", { text: f.label }),
          h("td", { text: label("fields.type.", f.type) }), h("td", { text: f.choices.join(", ") }),
          h("td", null, h("input", {
            type: "checkbox", checked: f.required, "aria-label": t("fields.required"), onchange: function (ev) {
              api("PATCH", "/custom-fields/" + encodeURIComponent(f.name), { required: ev.target.checked })
                .then(function () { toast(t("common.updated")); viewFields(); }).catch(fail);
            }
          })),
          h("td", null, h("button", {
            class: "danger", onclick: function () {
              if (!window.confirm(t("fields.delete_confirm", { name: f.name }))) return;
              api("DELETE", "/custom-fields/" + encodeURIComponent(f.name)).then(viewFields).catch(fail);
            }
          }, t("common.delete"))));
      });
      var name = h("input", { id: "cf-new-name", required: true, pattern: "[a-z0-9_]+",
        placeholder: t("fields.name_placeholder") });
      var labelInput = h("input", { id: "cf-new-label" });
      var type = select("cf-new-type", FIELD_TYPES.map(function (x) { return [x, t("fields.type." + x)]; }), "string");
      var choices = h("input", { id: "cf-new-choices", placeholder: t("fields.choices_placeholder") });
      var required = h("input", { id: "cf-new-req", type: "checkbox" });
      var form = h("form", {
        class: "panel-body form-grid", onsubmit: function (ev) {
          ev.preventDefault();
          var body = { name: name.value, label: labelInput.value, type: type.value, required: required.checked };
          if (type.value === "choice") {
            body.choices = choices.value.split(",").map(function (c) { return c.trim(); }).filter(Boolean);
          }
          api("POST", "/custom-fields", body).then(function () { toast(t("fields.created")); viewFields(); }).catch(fail);
        }
      }, field(t("fields.name"), name, { required: true }), field(t("fields.label"), labelInput),
        field(t("fields.type"), type), field(t("fields.choices"), choices), field(t("fields.required"), required),
        h("div", { class: "wide" }, h("button", { class: "primary", type: "submit" }, t("fields.create"))));
      render(adminPanel(t("fields.title"), form,
        table([t("fields.name"), t("fields.label"), t("fields.type"), t("fields.choices"), t("fields.required"), ""],
          rows)));
    }).catch(fail);
  }

  function viewWebhooks() {
    api("GET", "/webhooks").then(function (hooks) {
      var log = h("div");
      var rows = hooks.map(function (w) {
        return h("tr", null, h("td", { text: String(w.id) }), h("td", { text: w.name }),
          h("td", { text: label("webhooks.type.", w.type) }),
          h("td", { class: "mono", text: w.url }), h("td", { text: w.events.join(", ") }),
          h("td", { text: activeText(w.active) }),
          h("td", { class: "actions" },
            h("button", { onclick: function () { showDeliveries(w, log); } }, t("webhooks.deliveries")),
            h("button", {
              class: "danger", onclick: function () {
                if (!window.confirm(t("webhooks.delete_confirm", { name: w.name || w.id }))) return;
                api("DELETE", "/webhooks/" + w.id).then(viewWebhooks).catch(fail);
              }
            }, t("common.delete"))));
      });
      var name = h("input", { id: "w-name" });
      var type = select("w-type", [["generic", t("webhooks.type.generic")], ["soar", t("webhooks.type.soar")]], "generic");
      var url = h("input", { id: "w-url", required: true, placeholder: "https://soar.example.invalid" });
      var secret = h("input", { id: "w-secret", type: "password", autocomplete: "off",
        placeholder: t("webhooks.secret_placeholder") });
      var soarLabel = h("input", { id: "w-label", value: "mydemoticket" });
      var verify = h("input", { id: "w-verify", type: "checkbox", checked: true });
      var eventBoxes = EVENTS.map(function (e) {
        return h("label", null, h("input", { type: "checkbox", checked: true, value: e }), " " + e);
      });
      var form = h("form", {
        class: "panel-body form-grid", onsubmit: function (ev) {
          ev.preventDefault();
          var events = eventBoxes.map(function (l) { return l.firstChild; })
            .filter(function (c) { return c.checked; }).map(function (c) { return c.value; });
          api("POST", "/webhooks", {
            name: name.value, type: type.value, url: url.value, secret: secret.value,
            soar_label: soarLabel.value, verify_tls: verify.checked, events: events
          }).then(function () { toast(t("webhooks.created")); viewWebhooks(); }).catch(fail);
        }
      }, field(t("webhooks.name"), name), field(t("webhooks.type"), type),
        field(t("webhooks.url"), url, { required: true }),
        field(t("webhooks.secret"), secret), field(t("webhooks.label"), soarLabel),
        field(t("webhooks.verify"), verify),
        h("div", { class: "wide" }, h("label", { text: t("webhooks.events") }), eventBoxes),
        h("div", { class: "wide" }, h("button", { class: "primary", type: "submit" }, t("webhooks.create"))));
      render(adminPanel(t("webhooks.title"), form,
        table([t("webhooks.id"), t("webhooks.name"), t("webhooks.type"), t("webhooks.url"), t("webhooks.events"),
          t("webhooks.status"), ""], rows), log));
    }).catch(fail);
  }

  function showDeliveries(w, target) {
    api("GET", "/webhooks/" + w.id + "/deliveries").then(function (items) {
      clear(target);
      target.appendChild(h("div", { class: "panel" },
        h("div", { class: "panel-head" },
          h("h2", { text: t("webhooks.deliveries_for", { name: w.name || t("webhooks.webhook_n", { id: w.id }) }) }),
          h("span", { class: "spacer" }),
          h("button", { onclick: function () { showDeliveries(w, target); } }, t("common.refresh"))),
        table([t("col.time"), t("webhooks.col.event"), t("webhooks.col.ticket"), t("webhooks.status"),
          t("webhooks.col.http"), t("webhooks.col.attempts"), t("webhooks.col.error")], items.map(function (d) {
          return h("tr", null, h("td", { text: fmtTime(d.created_at) }), h("td", { text: d.event }),
            h("td", { text: d.ticket_number || "" }),
            h("td", { class: "status-" + d.status, text: label("webhooks.delivery.", d.status) }),
            h("td", { text: d.response_code == null ? "" : String(d.response_code) }),
            h("td", { text: String(d.attempts) }), h("td", { text: d.error }));
        }))));
    }).catch(fail);
  }

  // --------------------------------------------------------- tenants (platform)
  function viewTenants() {
    if (!isPlatform()) { render(h("p", { class: "error", text: t("tenants.only_platform") })); return; }
    api("GET", "/tenants").then(function (tenants) {
      var editBox = h("div");
      var rows = tenants.map(function (tn) {
        return h("tr", null, h("td", { class: "mono", text: tn.slug }), h("td", { text: tn.name }),
          h("td", { text: activeText(tn.active) }), h("td", { class: "mono", text: tn.soar_url || "" }),
          h("td", { class: "mono", text: tn.splunk_index || t("tenants.default_index") }),
          h("td", { text: tn.default ? t("common.yes") : "" }),
          h("td", { class: "actions" }, h("button", {
            type: "button", "aria-label": t("tenants.edit_aria", { slug: tn.slug }),
            onclick: function () { editTenant(tn, editBox); }
          }, t("common.edit"))));
      });
      var slug = h("input", { id: "tn-slug", required: true, pattern: "[a-z0-9-]{2,32}", maxlength: 32,
        placeholder: t("tenants.slug_placeholder"), "aria-describedby": "tn-slug-hint" });
      var name = h("input", { id: "tn-name", required: true, maxlength: 200 });
      var soar = h("input", { id: "tn-soar", type: "url", placeholder: "https://soar.example.invalid" });
      var index = h("input", { id: "tn-index", placeholder: t("tenants.index_placeholder") });
      var seed = h("input", { id: "tn-seed", type: "checkbox", checked: true });
      var form = h("form", {
        class: "panel-body form-grid", onsubmit: function (ev) {
          ev.preventDefault();
          var body = { slug: slug.value.trim(), name: name.value.trim(), seed: seed.checked };
          if (soar.value.trim()) body.soar_url = soar.value.trim();
          if (index.value.trim()) body.splunk_index = index.value.trim();
          api("POST", "/tenants", body).then(function (tn) {
            toast(t("tenants.created", { slug: tn.slug }));
            return refreshMe().then(viewTenants);
          }).catch(fail);
        }
      }, field(t("tenants.slug"), slug, { required: true }), field(t("tenants.name"), name, { required: true }),
        field(t("tenants.soar_optional"), soar), field(t("tenants.index_optional"), index),
        h("div", { class: "wide" }, h("label", { for: "tn-seed" }, seed, t("tenants.seed"))),
        h("div", { class: "wide muted", id: "tn-slug-hint", text: t("tenants.slug_hint") }),
        h("div", { class: "wide" }, h("button", { class: "primary", type: "submit" }, t("tenants.create"))));
      render(adminPanel(t("tenants.title"), form,
        table([t("tenants.slug"), t("tenants.name"), t("tenants.status"), t("tenants.soar_url"), t("tenants.index"),
          t("tenants.default"), ""], rows), editBox));
    }).catch(fail);
  }

  function editTenant(tn, target) {
    var name = h("input", { id: "te-name", required: true, maxlength: 200, value: tn.name });
    var active = h("input", { id: "te-active", type: "checkbox", checked: tn.active, disabled: tn.default,
      "aria-describedby": tn.default ? "te-active-hint" : null });
    var soar = h("input", { id: "te-soar", type: "url", value: tn.soar_url || "" });
    var index = h("input", { id: "te-index", value: tn.splunk_index || "", placeholder: t("tenants.index_placeholder") });
    var form = h("form", {
      class: "panel-body form-grid", onsubmit: function (ev) {
        ev.preventDefault();
        var body = { name: name.value.trim(), soar_url: soar.value.trim() || null,
          splunk_index: index.value.trim() || null };
        if (!tn.default) body.active = active.checked;
        api("PATCH", "/tenants/" + encodeURIComponent(tn.slug), body).then(function () {
          toast(t("tenants.saved", { slug: tn.slug }));
          return refreshMe().then(viewTenants);
        }).catch(fail);
      }
    }, field(t("tenants.name"), name, { required: true }),
      h("div", null, h("label", { for: "te-active" }, active, t("tenants.active"))),
      field(t("tenants.soar_url"), soar), field(t("tenants.index"), index),
      tn.default ? h("div", { class: "wide muted", id: "te-active-hint", text: t("tenants.default_hint") }) : null,
      h("div", { class: "wide actions" },
        h("button", { class: "primary", type: "submit" }, t("common.save")),
        h("button", { type: "button", onclick: function () { clear(target); } }, t("common.cancel"))));
    clear(target);
    target.appendChild(h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h2", { text: t("tenants.edit_title", { slug: tn.slug }) })), form));
    name.focus();
  }

  // ------------------------------------------------------------------ reports
  var REPORT_RANGES = ["7", "30", "90", "all"];

  function tile(labelText, value, cls) {
    return h("div", { class: "tile" + (cls ? " " + cls : "") },
      h("div", { class: "tile-label", text: labelText }),
      h("div", { class: "tile-value" + (String(value).length > 14 ? " small" : ""), text: String(value) }));
  }

  // One bar list; widths are set through the CSSOM (allowed by the CSP, unlike style attributes).
  function bars(title, entries) {
    var max = entries.reduce(function (m, e) { return Math.max(m, e[1]); }, 0);
    return h("div", null, h("h3", { text: title }),
      h("ul", { class: "bars", "aria-label": title }, entries.map(function (e) {
        var fill = h("div", { class: "bar-fill" });
        fill.style.width = (max ? Math.round(e[1] * 100 / max) : 0) + "%";
        return h("li", null, h("span", { class: "bar-label", title: e[0], text: e[0] }),
          h("div", { class: "bar-track", "aria-hidden": "true" }, fill),
          h("span", { class: "bar-count", text: String(e[1]) }));
      })));
  }

  function viewReports(params) {
    if (!canSeeReports()) { render(h("p", { class: "error", text: t("reports.only") })); return; }
    var range = params.since || "30";
    var rangeSel = select("r-since", REPORT_RANGES.map(function (r) { return [r, t("reports.range." + r)]; }), range);
    rangeSel.addEventListener("change", function () { location.hash = "#/reports?since=" + rangeSel.value; });
    var body = h("div", { class: "panel-body", "aria-live": "polite" }, h("p", { class: "muted", text: t("common.loading") }));
    var tenant = (session.me.active_tenant || session.me.tenant || {}).name || "";
    render(h("div", { class: "panel" },
      h("div", { class: "panel-head" },
        h("h1", { text: tenant ? t("reports.title_tenant", { tenant: tenant }) : t("reports.title") }),
        h("span", { class: "spacer" }), h("label", { for: "r-since", text: t("reports.period") }), rangeSel),
      body));
    var qs = "";
    var days = parseInt(range, 10);
    if (days > 0) {
      var since = new Date(Date.now() - days * 86400000).toISOString().replace(/\.\d{3}Z$/, "Z");
      qs = "?since=" + encodeURIComponent(since);
    }
    api("GET", "/reports/summary" + qs).then(function (r) {
      clear(body);
      var prio = Object.keys(r.by_priority).sort().map(function (k) { return [label("priority.", k), r.by_priority[k]]; });
      var states = STATES.filter(function (s) { return s in r.by_state; })
        .map(function (s) { return [label("state.", s), r.by_state[s]]; });
      var teams = Object.keys(r.by_team).map(function (k) { return [k, r.by_team[k]]; });
      body.appendChild(h("div", { class: "tiles" },
        tile(t("reports.total"), r.tickets_total), tile(t("reports.open"), r.open_count),
        tile(t("reports.created_7d"), r.created_last_7d), tile(t("reports.resolved_7d"), r.resolved_last_7d),
        tile(t("reports.mttr"), r.mttr_hours === null || r.mttr_hours === undefined ? "-" : r.mttr_hours)));
      body.appendChild(h("p", { class: "muted",
        text: t(days > 0 ? "reports.note_period" : "reports.note_all", { time: fmtTime(r.generated_at) }) }));
      body.appendChild(h("div", { class: "bar-groups" },
        bars(t("reports.by_state"), states), bars(t("reports.by_priority"), prio), bars(t("reports.by_team"), teams)));
    }).catch(function (err) {
      clear(body);
      body.appendChild(h("p", { class: "error", text: errorText(err) }));
    });
  }

  // ----------------------------------------------------------- audit / splunk
  // Splunk HEC connection (platform admins, splunk.manage). The token is write-only: the API never returns it.
  function splunkPanel() {
    var box = h("div", { class: "panel-body" }, h("p", { class: "muted", text: t("common.loading") }));
    api("GET", "/settings/splunk").then(function (s) {
      clear(box);
      box.appendChild(splunkForm(s || {}));
    }).catch(function (err) {
      clear(box);
      box.appendChild(h("p", { class: "error", text: errorText(err) }));
    });
    return h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h2", { text: t("splunk.title") })), box);
  }

  function checkField(input, text) {
    return h("div", null, h("label", { class: "check", for: input.id }, input, text));
  }

  function splunkForm(s) {
    var url = h("input", { id: "sp-url", type: "url", value: s.hec_url || "", placeholder: t("splunk.url_placeholder") });
    var index = h("input", { id: "sp-index", value: s.index || "", placeholder: t("splunk.index_placeholder") });
    var token = h("input", { id: "sp-token", type: "password", autocomplete: "new-password",
      placeholder: s.token_set ? t("splunk.token_keep") : t("splunk.token_empty") });
    var clearToken = h("input", { id: "sp-clear", type: "checkbox", disabled: !s.token_set });
    clearToken.addEventListener("change", function () {
      token.disabled = clearToken.checked;
      if (clearToken.checked) token.value = "";
    });
    var verify = h("input", { id: "sp-verify", type: "checkbox", checked: s.verify_tls !== false });
    var enabled = h("input", { id: "sp-enabled", type: "checkbox", checked: !!s.enabled });
    var result = h("div", { class: "wide", role: "status", "aria-live": "polite" });
    var testBtn = h("button", {
      type: "button", onclick: function () {
        testBtn.disabled = true;
        result.className = "wide muted";
        result.textContent = t("splunk.testing");
        api("POST", "/settings/splunk/test", {}).then(function (r) {
          result.className = "wide " + (r && r.ok ? "ok-text" : "error");
          result.textContent = t(r && r.ok ? "splunk.test_ok" : "splunk.test_failed") +
            (r && r.message ? ": " + r.message : "");
        }).catch(function (err) {
          result.className = "wide error";
          result.textContent = t("splunk.test_failed") + ": " + errorText(err);
        }).then(function () { testBtn.disabled = false; });
      }
    }, t("splunk.test"));
    var source = s.source || "none";
    return h("form", {
      class: "form-grid", onsubmit: function (ev) {
        ev.preventDefault();
        var body = { hec_url: url.value.trim(), index: index.value.trim(), verify_tls: verify.checked,
          enabled: enabled.checked };
        if (clearToken.checked) body.token = "";
        else if (token.value) body.token = token.value; // empty field = keep the stored token
        api("PUT", "/settings/splunk", body).then(function () {
          toast(t("splunk.saved"));
          viewAudit();
        }).catch(fail);
      }
    },
      h("p", { class: "wide muted", text: t("splunk.precedence_hint") }),
      field(t("splunk.url"), url), field(t("splunk.index"), index),
      field(t("splunk.token"), token),
      h("div", null, h("label", { text: t("splunk.state") }),
        h("div", { id: "sp-token-state", text: t("splunk.token_status", {
          state: s.token_set ? t("splunk.token_set") : t("splunk.token_not_set") }) }),
        h("div", { id: "sp-source", class: "muted", text: t("splunk.source_label", { source: label("splunk.source.", source) }) })),
      checkField(clearToken, t("splunk.clear_token")),
      checkField(verify, t("splunk.verify")),
      checkField(enabled, t("splunk.enabled")),
      h("div", { class: "wide actions" },
        h("button", { class: "primary", type: "submit" }, t("splunk.save")), testBtn),
      h("p", { class: "wide muted", text: t("splunk.test_hint") }),
      result);
  }

  function viewAudit() {
    var body = h("div", { class: "panel-body", "aria-live": "polite" }, h("p", { class: "muted", text: t("common.loading") }));
    render(h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h1", { text: t("audit.title") }), h("span", { class: "spacer" }),
        h("button", { type: "button", onclick: viewAudit }, t("common.refresh"))),
      body), can("splunk.manage") ? splunkPanel() : null);
    api("GET", "/audit/status").then(function (s) {
      clear(body);
      function num(v) { return v === null || v === undefined ? "-" : v; }
      body.appendChild(h("div", { class: "tiles" },
        tile(t("audit.hec"), s.hec_enabled ? t("audit.active") : t("audit.off"), s.hec_enabled ? "ok" : null),
        tile(t("audit.sent"), num(s.sent)), tile(t("audit.failed"), num(s.failed), s.failed ? "bad" : null),
        tile(t("audit.dropped"), num(s.dropped), s.dropped ? "bad" : null), tile(t("audit.queued"), num(s.queued)),
        tile(t("audit.last_success"), s.last_success_at ? fmtTime(s.last_success_at) : "-"),
        tile(t("audit.last_error"), s.last_error || t("audit.none"), s.last_error ? "bad" : null)));
      body.appendChild(h("p", { class: "muted",
        text: s.local_written !== undefined ? t("audit.local_note_count", { n: s.local_written }) : t("audit.local_note") }));
    }).catch(function (err) {
      clear(body);
      body.appendChild(h("p", { class: "error", text: errorText(err) }));
    });
  }

  start();
})();
