/* MyDemoTicket web UI - vanilla JS single page app, no dependencies.
 * All DOM is built with createElement/textContent (never innerHTML with data). */
(function () {
  "use strict";

  var STATES = ["new", "in_progress", "on_hold", "resolved", "closed", "cancelled"];
  var TRANSITIONS = {
    new: ["in_progress", "on_hold", "resolved", "cancelled"],
    in_progress: ["on_hold", "resolved", "cancelled"],
    on_hold: ["in_progress", "resolved", "cancelled"],
    resolved: ["in_progress", "closed"],
    closed: [],
    cancelled: []
  };
  var STATE_LABEL = {
    new: "New", in_progress: "In Progress", on_hold: "On Hold",
    resolved: "Resolved", closed: "Closed", cancelled: "Cancelled"
  };
  var ACTION_LABEL = {
    in_progress: "Start work", on_hold: "Put on hold", resolved: "Resolve",
    closed: "Close", cancelled: "Cancel ticket"
  };
  var PRIORITIES = { 1: "1 - Critical", 2: "2 - High", 3: "3 - Moderate", 4: "4 - Low" };
  var EVENTS = ["ticket.created", "ticket.updated", "ticket.state_changed", "ticket.commented"];

  var session = { me: null, csrf: null, fields: [], teams: [], users: [] };
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
    "tenant mismatch": "This request names a tenant other than your own. You can only work in your own tenant.",
    "tenant inactive": "This tenant is deactivated. Please contact a platform administrator.",
    "tenant not found": "This tenant does not exist. Please choose another tenant."
  };

  function errorText(err) {
    var message = (err && err.message) || String(err);
    if (TENANT_ERRORS[message]) return TENANT_ERRORS[message] + " (" + err.status + " " + message + ")";
    if (err && err.status === 403) return "Not allowed: " + message;
    if (err && err.status === 404) return "Not found (it may belong to another tenant): " + message;
    return message;
  }

  function fail(err) {
    if (err && err.status === 401) { session.me = null; showLogin(); return; }
    if (err && err.status === 403 && err.message === "tenant inactive" && session.me && !isPlatform()) {
      session.me = null; showLogin(errorText(err)); return;
    }
    toast(errorText(err), true);
  }

  function isPlatform() { return !!session.me && session.me.role === "platform_admin"; }
  function isAdmin() { return !!session.me && (session.me.role === "admin" || isPlatform()); }
  function canSeeReports() {
    return !!session.me && ["admin", "agent", "platform_admin"].indexOf(session.me.role) >= 0;
  }

  function fmtTime(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString();
  }

  function stateBadge(state) {
    return h("span", { class: "badge state-" + state, text: STATE_LABEL[state] || state });
  }

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
      var label = Array.isArray(opt) ? opt[1] : opt;
      sel.appendChild(h("option", { value: String(value), selected: String(value) === String(current) }, label));
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
    if (isAdmin() || me.role === "integration") return true;
    if (me.role !== "agent") return false;
    if (!ticket.assignment_group && !ticket.assigned_to) return true;
    if (ticket.assigned_to === me.username) return true;
    return !!ticket.assignment_group && me.teams.indexOf(ticket.assignment_group) >= 0;
  }

  // ------------------------------------------------------------ bootstrap
  function loadReference() {
    var jobs = [api("GET", "/custom-fields").then(function (f) { session.fields = f; })];
    if (session.me.role !== "requester") {
      jobs.push(api("GET", "/teams").then(function (t) { session.teams = t; }));
      jobs.push(api("GET", "/users").then(function (u) { session.users = u; }));
    }
    return Promise.all(jobs);
  }

  function setChrome(loggedIn) {
    document.getElementById("sidebar").hidden = !loggedIn;
    document.getElementById("userbox").hidden = !loggedIn;
    document.getElementById("global-search").hidden = !loggedIn;
    function show(selector, visible) {
      Array.prototype.forEach.call(document.querySelectorAll(selector), function (el) { el.hidden = !visible; });
    }
    show(".admin-only", loggedIn && isAdmin());
    show(".platform-only", loggedIn && isPlatform());
    show(".reports-only", loggedIn && canSeeReports());
    if (loggedIn) {
      document.getElementById("user-name").textContent =
        session.me.display_name + " (" + session.me.role + ")";
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
    (me.tenants || []).forEach(function (t) {
      sel.appendChild(h("option", { value: t.slug, selected: t.slug === active.slug },
        t.name + (t.active ? "" : " (inactive)")));
    });
  }

  document.getElementById("tenant-switch").addEventListener("change", function (ev) {
    var slug = ev.target.value;
    api("POST", "/auth/tenant", { tenant: slug }).then(function (me) {
      return applyMe(me).then(function () {
        toast("Switched to tenant " + ((me.active_tenant && me.active_tenant.name) || slug));
        // a ticket of the old tenant is a 404 in the new one: go back to the list
        if (/^\/tickets\/./.test(parseHash().path)) location.hash = "#/tickets";
        else route();
      });
    }).catch(function (err) { drawTenantBox(); fail(err); });
  });

  // Stores the /me object and (re)loads the tenant's reference data and the header.
  function applyMe(me) {
    session.me = me;
    session.csrf = me.csrf_token || session.csrf;
    return loadReference().then(function () { setChrome(true); });
  }

  function refreshMe() {
    return api("GET", "/me").then(applyMe).catch(fail);
  }

  function start() {
    api("GET", "/me").then(function (me) {
      return applyMe(me).then(route);
    }).catch(function (err) {
      showLogin(err && err.status === 403 ? errorText(err) : "");
    });
  }

  function showLogin(message) {
    setChrome(false);
    var user = h("input", { id: "login-user", autocomplete: "username", required: true });
    var pass = h("input", { id: "login-pass", type: "password", autocomplete: "current-password", required: true });
    var error = h("div", { class: "error", role: "alert", text: typeof message === "string" ? message : "" });
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
        field("User name", user), field("Password", pass), error,
        h("button", { class: "primary", type: "submit" }, "Log in")));
    render(h("div", { class: "panel login" },
      h("div", { class: "panel-head" }, h("h1", { text: "Log in to MyDemoTicket" })), form));
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
  function route() {
    if (!session.me) { showLogin(); return; }
    var r = parseHash();
    var nav = "tickets";
    var m;
    if (r.path === "/new") { nav = "new"; viewNew(); }
    else if ((m = r.path.match(/^\/tickets\/([^/]+)$/))) { nav = ""; viewDetail(decodeURIComponent(m[1])); }
    else if (r.path === "/admin/users") { nav = "users"; viewUsers(); }
    else if (r.path === "/admin/teams") { nav = "teams"; viewTeams(); }
    else if (r.path === "/admin/custom-fields") { nav = "custom-fields"; viewFields(); }
    else if (r.path === "/admin/webhooks") { nav = "webhooks"; viewWebhooks(); }
    else if (r.path === "/admin/audit") { nav = "audit"; viewAudit(); }
    else if (r.path === "/admin/tenants") { nav = "tenants"; viewTenants(); }
    else if (r.path === "/reports") { nav = "reports"; viewReports(r.params); }
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

    var title = params.mine ? "Assigned to me" : params.unassigned ? "Unassigned tickets"
      : params.all ? "All tickets" : "Open tickets";
    var stateSel = select("f-state", STATES.map(function (s) { return [s, STATE_LABEL[s]]; }),
      params.state || "", "Any open state");
    var groupSel = select("f-group", session.teams.map(function (t) { return t.name; }),
      params.assignment_group || "", "Any group");
    var qInput = h("input", { id: "f-q", type: "search", value: params.q || "", placeholder: "Number or text" });
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
      field("State", stateSel),
      session.me.role !== "requester" ? field("Assignment group", groupSel) : null,
      field("Search", qInput),
      h("div", null, h("button", { class: "primary", type: "submit" }, "Apply")));

    var tbody = h("tbody", null, h("tr", null, h("td", { colspan: 7, class: "muted", text: "Loading..." })));
    var pager = h("div", { class: "pager" });
    render(h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h1", { text: title }), h("span", { class: "spacer" }),
        h("a", { class: "btn", href: "#/new" }, "New")),
      filters,
      h("table", { class: "list" },
        h("thead", null, h("tr", null, ["Number", "Short description", "State", "Priority",
          "Assignment group", "Assigned to", "Updated"].map(function (c) { return h("th", { text: c }); }))),
        tbody),
      pager));

    var qs = Object.keys(query).map(function (k) { return k + "=" + encodeURIComponent(query[k]); }).join("&");
    api("GET", "/tickets?" + qs).then(function (data) {
      clear(tbody);
      if (!data.items.length) {
        tbody.appendChild(h("tr", null, h("td", { colspan: 7, class: "muted", text: "No tickets found." })));
      }
      data.items.forEach(function (t) {
        tbody.appendChild(h("tr", null,
          h("td", null, h("a", { href: "#/tickets/" + encodeURIComponent(t.number), text: t.number })),
          h("td", { text: t.short_description }),
          h("td", null, stateBadge(t.state)),
          h("td", { class: "prio-" + t.priority, text: PRIORITIES[t.priority] }),
          h("td", { text: t.assignment_group || "" }),
          h("td", { text: t.assigned_to || "" }),
          h("td", { text: fmtTime(t.updated_at) })));
      });
      var from = data.total ? query.offset + 1 : 0;
      var to = Math.min(query.offset + query.limit, data.total);
      pager.appendChild(h("span", { text: from + "-" + to + " of " + data.total }));
      function page(delta) {
        var p = Object.assign({}, params, { offset: Math.max(0, query.offset + delta) });
        location.hash = "#/tickets?" + Object.keys(p).map(function (k) {
          return k + "=" + encodeURIComponent(p[k]);
        }).join("&");
      }
      pager.appendChild(h("button", { disabled: query.offset === 0, onclick: function () { page(-query.limit); } }, "Previous"));
      pager.appendChild(h("button", { disabled: to >= data.total, onclick: function () { page(query.limit); } }, "Next"));
    }).catch(fail);
  }

  // ------------------------------------------------------ custom field input
  function customInput(def, value) {
    var id = "cf-" + def.name;
    if (def.type === "boolean") {
      return select(id, [["true", "Yes"], ["false", "No"]],
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
    var me = session.me;
    var isRequester = me.role === "requester";
    var short = h("input", { id: "n-short", required: true, maxlength: 500 });
    var desc = h("textarea", { id: "n-desc" });
    var prio = select("n-prio", Object.keys(PRIORITIES).map(function (k) { return [k, PRIORITIES[k]]; }), "3");
    var cat = h("input", { id: "n-cat", placeholder: "e.g. security" });
    var group = select("n-group", session.teams.map(function (t) { return t.name; }), "", "-- none --");
    var assignee = select("n-assignee", session.users.map(function (u) { return [u.username, u.display_name]; }),
      "", "-- none --");
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
        if (!isRequester) {
          body.assignment_group = group.value || null;
          body.assigned_to = assignee.value || null;
        }
        var cf = {};
        session.fields.forEach(function (def) {
          var v = readCustom(def, customEls[def.name]);
          if (v !== null) cf[def.name] = v;
        });
        body.custom_fields = cf;
        api("POST", "/tickets", body).then(function (t) {
          toast("Created " + t.number);
          location.hash = "#/tickets/" + encodeURIComponent(t.number);
        }).catch(fail);
      }
    },
      h("div", { class: "panel-body form-grid" },
        field("Short description", short, { required: true, wide: true }),
        field("Description", desc, { wide: true }),
        field("Priority", prio), field("Category", cat),
        isRequester ? null : field("Assignment group", group),
        isRequester ? null : field("Assigned to", assignee),
        customs),
      h("div", { class: "panel-body" }, h("button", { class: "primary", type: "submit" }, "Submit")));
    render(h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h1", { text: "New ticket" })), form));
    short.focus();
  }

  // --------------------------------------------------------- ticket detail
  function viewDetail(number) {
    render(h("p", { class: "muted", text: "Loading " + number + "..." }));
    Promise.all([
      api("GET", "/tickets/" + encodeURIComponent(number)),
      api("GET", "/tickets/" + encodeURIComponent(number) + "/journal")
    ]).then(function (res) { drawDetail(res[0], res[1]); }).catch(fail);
  }

  function drawDetail(t, journal) {
    var me = session.me;
    var isRequester = me.role === "requester";
    var editable = !isRequester && canModify(t);
    var path = "/tickets/" + encodeURIComponent(t.number);

    function ro(el) { if (!editable) el.disabled = true; return el; }
    var short = ro(h("input", { id: "d-short", value: t.short_description }));
    var desc = ro(h("textarea", { id: "d-desc", value: t.description }));
    var prio = ro(select("d-prio", Object.keys(PRIORITIES).map(function (k) { return [k, PRIORITIES[k]]; }), t.priority));
    var cat = ro(h("input", { id: "d-cat", value: t.category }));
    var groupNames = session.teams.map(function (x) { return x.name; });
    if (t.assignment_group && groupNames.indexOf(t.assignment_group) < 0) groupNames.push(t.assignment_group);
    var group = ro(select("d-group", groupNames, t.assignment_group || "", "-- none --"));
    var userOpts = session.users.map(function (u) { return [u.username, u.display_name]; });
    if (t.assigned_to && !session.users.some(function (u) { return u.username === t.assigned_to; })) {
      userOpts.push([t.assigned_to, t.assigned_to]);
    }
    var assignee = ro(select("d-assignee", userOpts, t.assigned_to || "", "-- none --"));
    var closeNotes = ro(h("textarea", { id: "d-close", value: t.close_notes }));
    var customEls = {};
    var customs = session.fields.map(function (def) {
      customEls[def.name] = ro(customInput(def, t.custom_fields[def.name]));
      return field(def.label, customEls[def.name], { required: def.required });
    });

    function collectChanges() {
      var body = {};
      if (short.value !== t.short_description) body.short_description = short.value;
      if (desc.value !== t.description) body.description = desc.value;
      if (parseInt(prio.value, 10) !== t.priority) body.priority = parseInt(prio.value, 10);
      if (cat.value !== t.category) body.category = cat.value;
      if ((group.value || null) !== t.assignment_group) body.assignment_group = group.value || null;
      if ((assignee.value || null) !== t.assigned_to) body.assigned_to = assignee.value || null;
      if (closeNotes.value !== t.close_notes) body.close_notes = closeNotes.value;
      var cf = {};
      session.fields.forEach(function (def) {
        var v = readCustom(def, customEls[def.name]);
        var old = t.custom_fields[def.name];
        if (v !== (old === undefined ? null : old)) cf[def.name] = v;
      });
      if (Object.keys(cf).length) body.custom_fields = cf;
      return body;
    }

    function patch(body, message) {
      api("PATCH", path, body).then(function () {
        toast(message);
        viewDetail(t.number);
      }).catch(fail);
    }

    var actions = h("div", { class: "actions" });
    (TRANSITIONS[t.state] || []).forEach(function (target) {
      var allowed = editable || (isRequester && target === "cancelled" && t.caller === me.username);
      if (!allowed) return;
      actions.appendChild(h("button", {
        class: target === "resolved" ? "primary" : target === "cancelled" ? "danger" : null,
        onclick: function () {
          var body = isRequester ? {} : collectChanges();
          body.state = target;
          if (target === "resolved" && !closeNotes.value.trim()) {
            toast("Please enter close notes before resolving.", true);
            closeNotes.focus();
            return;
          }
          patch(body, "State changed to " + STATE_LABEL[target]);
        }
      }, ACTION_LABEL[target]));
    });
    if (editable) {
      actions.appendChild(h("button", {
        class: "primary", onclick: function () {
          var body = collectChanges();
          if (!Object.keys(body).length) { toast("Nothing changed."); return; }
          patch(body, "Saved");
        }
      }, "Save"));
    }

    var refs = Object.keys(t.external_refs || {});
    var header = h("div", { class: "panel-head" },
      h("h1", null, t.number + " "), stateBadge(t.state), h("span", { class: "spacer" }), actions);

    var formBody = h("div", { class: "panel-body form-grid" },
      field("Number", h("input", { id: "d-number", value: t.number, disabled: true })),
      field("Caller", h("input", { id: "d-caller", value: t.caller, disabled: true })),
      field("Short description", short, { required: true, wide: true }),
      field("Description", desc, { wide: true }),
      field("Priority", prio), field("Category", cat),
      field("Assignment group", group), field("Assigned to", assignee),
      customs,
      field("Close notes", closeNotes, { wide: true }),
      h("div", { class: "wide muted" },
        "Created " + fmtTime(t.created_at) + " - Updated " + fmtTime(t.updated_at) +
        (t.resolved_at ? " - Resolved " + fmtTime(t.resolved_at) : "")),
      refs.length ? h("div", { class: "wide" }, h("label", { text: "External references" }),
        h("div", { class: "mono" }, refs.map(function (k) { return k + ": " + t.external_refs[k]; }).join("  |  ")))
        : null);

    render(h("div", { class: "panel" }, header, formBody), drawJournal(t, journal, editable));
  }

  // Journal tabs of the ticket detail page (WAI-ARIA tab pattern). The selected tab lives in the
  // hash, e.g. #/tickets/TKT0001001?tab=worknotes, and is updated with replaceState (no reload).
  var JOURNAL_LABEL = { work_note: "Work note", comment: "Comment", summary: "Summary", audit: "Field change" };

  function journalWho(e) {
    return e.author ? e.author + " (via " + e.actor + ")" : e.actor;
  }

  function journalItem(e) {
    var meta = h("div", { class: "meta" }, (JOURNAL_LABEL[e.type] || e.type) + " - " + journalWho(e) + " - " +
      fmtTime(e.at) + (e.source ? " - via " + e.source : ""));
    var body;
    if (e.type === "audit") {
      body = h("div", { class: "body" }, e.field === "created" ? "Ticket created"
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

  function journalForm(t, kind, title, placeholder) {
    var id = "j-" + kind;
    var text = h("textarea", { id: id, placeholder: placeholder });
    return h("form", {
      class: "journal-form",
      onsubmit: function (ev) {
        ev.preventDefault();
        if (!text.value.trim()) return;
        api("POST", "/tickets/" + encodeURIComponent(t.number) + "/comments", { body: text.value, type: kind })
          .then(function () { toast("Posted"); viewDetail(t.number); }).catch(fail);
      }
    }, h("label", { for: id, text: title }), text,
      h("div", null, h("button", { class: "primary", type: "submit" }, title)));
  }

  function summaryBlock(e) {
    return h("div", { class: "summary-entry" },
      h("div", { class: "meta" }, journalWho(e) + " - " + fmtTime(e.at)),
      h("div", { class: "body", text: e.body }));
  }

  function resolutionReport(t, summaries) {
    var latest = summaries[summaries.length - 1];
    var earlier = summaries.slice(0, -1).reverse();
    return h("div", { class: "report" },
      h("h3", { text: "Close notes" }),
      t.close_notes ? h("div", { class: "body", text: t.close_notes })
        : h("p", { class: "muted", text: "Not resolved yet." }),
      h("p", { class: "muted" }, t.resolved_at ? "Resolved " + fmtTime(t.resolved_at) : "Resolved: -"),
      h("h3", { text: "Summary" }),
      latest ? summaryBlock(latest) : h("p", { class: "muted", text: "No summary yet." }),
      earlier.length ? h("details", { class: "earlier" },
        h("summary", { text: "Earlier summaries (" + earlier.length + ")" }),
        earlier.map(summaryBlock)) : null);
  }

  function auditTrail(entries) {
    if (!entries.length) return h("p", { class: "muted", text: "No field changes yet." });
    return table(["Time", "Field", "Old → New", "Actor", "Source"], entries.slice().reverse().map(function (e) {
      return h("tr", null,
        h("td", { text: fmtTime(e.at) }),
        h("td", { class: "mono", text: e.field }),
        h("td", { class: "pre-wrap" }, e.field === "created" ? "Ticket created" : fmtValue(e.old) + " → " + fmtValue(e.new)),
        h("td", { text: journalWho(e) }),
        h("td", { text: e.source || "" }));
    }));
  }

  function drawJournal(t, journal, editable) {
    var me = session.me;
    var isRequester = me.role === "requester";
    var canComment = isRequester ? t.caller === me.username : editable;
    function ofType(type) { return journal.filter(function (e) { return e.type === type; }); }
    var notes = ofType("work_note"), comments = ofType("comment"), summaries = ofType("summary");
    var audits = ofType("audit");

    var defs = [];
    if (!isRequester) {
      defs.push({ id: "worknotes", title: "Work notes (" + notes.length + ")", content: [
        canComment ? journalForm(t, "work_note", "Add work note", "Add a work note (internal)") : null,
        journalList(notes, "No work notes yet.")] });
    }
    defs.push({ id: "comments", title: "Comments (" + comments.length + ")", content: [
      canComment ? journalForm(t, "comment", "Add comment", "Add a comment (visible to caller)") : null,
      journalList(comments, "No comments yet.")] });
    defs.push({ id: "resolution", title: "Resolution report", content: [resolutionReport(t, summaries)] });
    defs.push({ id: "audit", title: "Audit trail (" + audits.length + ")", content: [auditTrail(audits)] });
    defs.push({ id: "history", title: "History (" + journal.length + ")", content: [
      journalList(journal, "No activity yet.")] });

    var wanted = parseHash().params.tab;
    var current = defs.some(function (d) { return d.id === wanted; }) ? wanted : defs[0].id;
    var tablist = h("div", { class: "tablist", role: "tablist", "aria-label": "Ticket activity" });
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
      var url = "#/tickets/" + encodeURIComponent(t.number) + "?tab=" + id;
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
    if (v === null || v === undefined || v === "") return "(empty)";
    if (typeof v === "object") return JSON.stringify(v);
    return String(v);
  }

  // ----------------------------------------------------------------- admin
  function adminPanel(title, formEl, tableEl, extra) {
    return h("div", null,
      h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h1", { text: title })), tableEl),
      formEl ? h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", { text: "Add" })), formEl) : null,
      extra || null);
  }

  function table(headers, rows) {
    return h("table", { class: "list" },
      h("thead", null, h("tr", null, headers.map(function (x) { return h("th", { text: x }); }))),
      h("tbody", null, rows));
  }

  function refreshReference() { return loadReference().catch(function () {}); }

  function viewUsers() {
    api("GET", "/users").then(function (users) {
      session.users = users;
      var secretBox = h("div");
      var platform = isPlatform();
      var rows = users.map(function (u) {
        // changing a role to or from platform_admin is not possible (422), so that select is read-only
        var isPa = u.role === "platform_admin";
        var role = select("role-" + u.username,
          isPa ? ["platform_admin"] : ["admin", "agent", "requester", "integration"], u.role);
        role.setAttribute("aria-label", "Role of " + u.username);
        if (isPa) role.disabled = true;
        role.addEventListener("change", function () {
          api("PATCH", "/users/" + encodeURIComponent(u.username), { role: role.value })
            .then(function () { toast("Role updated"); }).catch(function (e) { fail(e); viewUsers(); });
        });
        return h("tr", null,
          h("td", { text: u.username }), h("td", { text: u.display_name }), h("td", null, role),
          platform ? h("td", { text: u.tenant || "(platform)" }) : null,
          h("td", { text: u.teams.join(", ") }), h("td", { text: u.active ? "active" : "inactive" }),
          h("td", { class: "actions" },
            h("button", {
              onclick: function () {
                api("POST", "/users/" + encodeURIComponent(u.username) + "/tokens", { name: "ui" }).then(function (tok) {
                  clear(secretBox);
                  secretBox.appendChild(h("div", { class: "secret-box" },
                    h("strong", { text: "New API token for " + u.username + " (shown only once):" }),
                    h("div", { class: "mono", text: tok.token })));
                }).catch(fail);
              }
            }, "New token"),
            h("button", {
              onclick: function () {
                api("PATCH", "/users/" + encodeURIComponent(u.username), { active: !u.active })
                  .then(viewUsers).catch(fail);
              }
            }, u.active ? "Deactivate" : "Activate"),
            h("button", {
              onclick: function () {
                var pw = window.prompt("New password for " + u.username + " (min. 8 characters)");
                if (!pw) return;
                api("PATCH", "/users/" + encodeURIComponent(u.username), { password: pw })
                  .then(function () { toast("Password set"); }).catch(fail);
              }
            }, "Set password")));
      });
      var name = h("input", { id: "u-name", required: true });
      var display = h("input", { id: "u-display" });
      var roleSel = select("u-role", ["agent", "requester", "integration", "admin"]
        .concat(platform ? ["platform_admin"] : []), "agent");
      var pw = h("input", { id: "u-pw", type: "password", autocomplete: "new-password" });
      var teamSel = select("u-team", session.teams.map(function (t) { return t.name; }), "", "-- none --");
      var activeSlug = (session.me.active_tenant || session.me.tenant || {}).slug;
      // platform admins choose the tenant of the new user; tenant admins always create in their own tenant
      var tenantSel = platform ? select("u-tenant", (session.me.tenants || []).map(function (t) {
        return [t.slug, t.name + " (" + t.slug + ")" + (t.active ? "" : " - inactive")];
      }), activeSlug) : null;
      var tenantHint = h("div", { class: "wide muted", id: "u-tenant-hint" });
      function syncTenantFields() {
        if (!platform) return;
        var noTenant = roleSel.value === "platform_admin";
        tenantSel.disabled = noTenant;
        // teams are listed for the active tenant only
        teamSel.disabled = noTenant || tenantSel.value !== activeSlug;
        if (teamSel.disabled) teamSel.value = "";
        tenantHint.textContent = noTenant ? "Platform admins belong to no tenant and have no teams."
          : tenantSel.value !== activeSlug ? "Teams can only be chosen for the active tenant; switch the tenant to assign teams."
            : "";
      }
      if (platform) {
        roleSel.addEventListener("change", syncTenantFields);
        tenantSel.addEventListener("change", syncTenantFields);
      }
      var form = h("form", {
        class: "panel-body form-grid", onsubmit: function (ev) {
          ev.preventDefault();
          var body = { username: name.value, display_name: display.value, role: roleSel.value };
          if (pw.value) body.password = pw.value;
          if (teamSel.value && !teamSel.disabled) body.teams = [teamSel.value];
          if (platform && roleSel.value !== "platform_admin") body.tenant = tenantSel.value;
          api("POST", "/users", body).then(function (u) {
            toast(u.tenant && u.tenant !== activeSlug ? "User created in tenant " + u.tenant : "User created");
            refreshReference().then(viewUsers);
          }).catch(fail);
        }
      }, field("User name", name, { required: true }), field("Display name", display), field("Role", roleSel),
        platform ? field("Tenant", tenantSel, { required: true }) : null,
        field("Password (optional)", pw), field("Team", teamSel), tenantHint,
        h("div", { class: "wide" }, h("button", { class: "primary", type: "submit" }, "Create user")));
      syncTenantFields();
      var headers = ["User name", "Display name", "Role"].concat(platform ? ["Tenant"] : [])
        .concat(["Teams", "Status", ""]);
      render(adminPanel("Users",
        form, h("div", null, secretBox, table(headers, rows))));
    }).catch(fail);
  }

  function viewTeams() {
    api("GET", "/teams").then(function (teams) {
      session.teams = teams;
      var rows = teams.map(function (t) {
        var add = select("add-" + t.name, session.users.map(function (u) { return u.username; }), "", "add member...");
        add.addEventListener("change", function () {
          if (!add.value) return;
          api("POST", "/teams/" + encodeURIComponent(t.name) + "/members", { username: add.value })
            .then(function () { refreshReference().then(viewTeams); }).catch(fail);
        });
        return h("tr", null, h("td", { text: t.name }), h("td", { text: t.description }),
          h("td", null, t.members.map(function (m) {
            return h("span", { class: "badge" }, m + " ", h("button", {
              class: "link", title: "Remove " + m, onclick: function () {
                api("DELETE", "/teams/" + encodeURIComponent(t.name) + "/members/" + encodeURIComponent(m))
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
            .then(function () { toast("Team created"); refreshReference().then(viewTeams); }).catch(fail);
        }
      }, field("Name", name, { required: true }), field("Description", desc),
        h("div", { class: "wide" }, h("button", { class: "primary", type: "submit" }, "Create team")));
      render(adminPanel("Teams", form, table(["Name", "Description", "Members", ""], rows)));
    }).catch(fail);
  }

  function viewFields() {
    api("GET", "/custom-fields").then(function (fields) {
      session.fields = fields;
      var rows = fields.map(function (f) {
        return h("tr", null, h("td", { class: "mono", text: f.name }), h("td", { text: f.label }),
          h("td", { text: f.type }), h("td", { text: f.choices.join(", ") }),
          h("td", null, h("input", {
            type: "checkbox", checked: f.required, "aria-label": "Required", onchange: function (ev) {
              api("PATCH", "/custom-fields/" + encodeURIComponent(f.name), { required: ev.target.checked })
                .then(function () { toast("Updated"); viewFields(); }).catch(fail);
            }
          })),
          h("td", null, h("button", {
            class: "danger", onclick: function () {
              if (!window.confirm("Delete custom field " + f.name + "? Existing values stay on tickets.")) return;
              api("DELETE", "/custom-fields/" + encodeURIComponent(f.name)).then(viewFields).catch(fail);
            }
          }, "Delete")));
      });
      var name = h("input", { id: "cf-new-name", required: true, pattern: "[a-z0-9_]+", placeholder: "e.g. ticket_source" });
      var label = h("input", { id: "cf-new-label" });
      var type = select("cf-new-type", ["string", "number", "boolean", "choice", "date"], "string");
      var choices = h("input", { id: "cf-new-choices", placeholder: "comma separated (choice only)" });
      var required = h("input", { id: "cf-new-req", type: "checkbox" });
      var form = h("form", {
        class: "panel-body form-grid", onsubmit: function (ev) {
          ev.preventDefault();
          var body = { name: name.value, label: label.value, type: type.value, required: required.checked };
          if (type.value === "choice") {
            body.choices = choices.value.split(",").map(function (c) { return c.trim(); }).filter(Boolean);
          }
          api("POST", "/custom-fields", body).then(function () { toast("Field created"); viewFields(); }).catch(fail);
        }
      }, field("Name", name, { required: true }), field("Label", label), field("Type", type),
        field("Choices", choices), field("Required", required),
        h("div", { class: "wide" }, h("button", { class: "primary", type: "submit" }, "Create field")));
      render(adminPanel("Custom fields", form,
        table(["Name", "Label", "Type", "Choices", "Required", ""], rows)));
    }).catch(fail);
  }

  function viewWebhooks() {
    api("GET", "/webhooks").then(function (hooks) {
      var log = h("div");
      var rows = hooks.map(function (w) {
        return h("tr", null, h("td", { text: String(w.id) }), h("td", { text: w.name }), h("td", { text: w.type }),
          h("td", { class: "mono", text: w.url }), h("td", { text: w.events.join(", ") }),
          h("td", { text: w.active ? "active" : "inactive" }),
          h("td", { class: "actions" },
            h("button", { onclick: function () { showDeliveries(w, log); } }, "Deliveries"),
            h("button", {
              class: "danger", onclick: function () {
                if (!window.confirm("Delete webhook " + (w.name || w.id) + "?")) return;
                api("DELETE", "/webhooks/" + w.id).then(viewWebhooks).catch(fail);
              }
            }, "Delete")));
      });
      var name = h("input", { id: "w-name" });
      var type = select("w-type", [["generic", "Generic (signed JSON)"], ["soar", "Splunk SOAR (container + artifact)"]], "generic");
      var url = h("input", { id: "w-url", required: true, placeholder: "https://soar.example.invalid" });
      var secret = h("input", { id: "w-secret", type: "password", autocomplete: "off",
        placeholder: "HMAC secret or SOAR ph-auth-token" });
      var label = h("input", { id: "w-label", value: "mydemoticket" });
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
            soar_label: label.value, verify_tls: verify.checked, events: events
          }).then(function () { toast("Webhook created"); viewWebhooks(); }).catch(fail);
        }
      }, field("Name", name), field("Type", type), field("URL", url, { required: true }),
        field("Secret / ph-auth-token", secret), field("SOAR container label", label),
        field("Verify TLS", verify), h("div", { class: "wide" }, h("label", { text: "Events" }), eventBoxes),
        h("div", { class: "wide" }, h("button", { class: "primary", type: "submit" }, "Create webhook")));
      render(adminPanel("Webhooks", form,
        table(["ID", "Name", "Type", "URL", "Events", "Status", ""], rows), log));
    }).catch(fail);
  }

  function showDeliveries(w, target) {
    api("GET", "/webhooks/" + w.id + "/deliveries").then(function (items) {
      clear(target);
      target.appendChild(h("div", { class: "panel" },
        h("div", { class: "panel-head" }, h("h2", { text: "Deliveries for " + (w.name || "webhook " + w.id) }),
          h("span", { class: "spacer" }), h("button", { onclick: function () { showDeliveries(w, target); } }, "Refresh")),
        table(["Time", "Event", "Ticket", "Status", "HTTP", "Attempts", "Error"], items.map(function (d) {
          return h("tr", null, h("td", { text: fmtTime(d.created_at) }), h("td", { text: d.event }),
            h("td", { text: d.ticket_number || "" }), h("td", { class: "status-" + d.status, text: d.status }),
            h("td", { text: d.response_code == null ? "" : String(d.response_code) }),
            h("td", { text: String(d.attempts) }), h("td", { text: d.error }));
        }))));
    }).catch(fail);
  }

  // --------------------------------------------------------- tenants (platform)
  function viewTenants() {
    if (!isPlatform()) { render(h("p", { class: "error", text: "Only platform admins manage tenants." })); return; }
    api("GET", "/tenants").then(function (tenants) {
      var editBox = h("div");
      var rows = tenants.map(function (t) {
        return h("tr", null, h("td", { class: "mono", text: t.slug }), h("td", { text: t.name }),
          h("td", { text: t.active ? "active" : "inactive" }), h("td", { class: "mono", text: t.soar_url || "" }),
          h("td", { class: "mono", text: t.splunk_index || "(default)" }), h("td", { text: t.default ? "yes" : "" }),
          h("td", { class: "actions" }, h("button", {
            type: "button", "aria-label": "Edit tenant " + t.slug,
            onclick: function () { editTenant(t, editBox); }
          }, "Edit")));
      });
      var slug = h("input", { id: "tn-slug", required: true, pattern: "[a-z0-9-]{2,32}", maxlength: 32,
        placeholder: "e.g. acme", "aria-describedby": "tn-slug-hint" });
      var name = h("input", { id: "tn-name", required: true, maxlength: 200 });
      var soar = h("input", { id: "tn-soar", type: "url", placeholder: "https://soar.example.invalid" });
      var index = h("input", { id: "tn-index", placeholder: "empty = default index" });
      var seed = h("input", { id: "tn-seed", type: "checkbox", checked: true });
      var form = h("form", {
        class: "panel-body form-grid", onsubmit: function (ev) {
          ev.preventDefault();
          var body = { slug: slug.value.trim(), name: name.value.trim(), seed: seed.checked };
          if (soar.value.trim()) body.soar_url = soar.value.trim();
          if (index.value.trim()) body.splunk_index = index.value.trim();
          api("POST", "/tenants", body).then(function (t) {
            toast("Tenant " + t.slug + " created");
            return refreshMe().then(viewTenants);
          }).catch(fail);
        }
      }, field("Slug", slug, { required: true }), field("Name", name, { required: true }),
        field("SOAR URL (optional)", soar), field("Splunk index (optional)", index),
        h("div", { class: "wide" }, h("label", { for: "tn-seed" }, seed, " With standard teams and custom fields (seed)")),
        h("div", { class: "wide muted", id: "tn-slug-hint",
          text: "Slug: 2-32 characters a-z, 0-9 and '-'; it cannot be changed later." }),
        h("div", { class: "wide" }, h("button", { class: "primary", type: "submit" }, "Create tenant")));
      render(adminPanel("Tenants", form,
        table(["Slug", "Name", "Status", "SOAR URL", "Splunk index", "Default", ""], rows), editBox));
    }).catch(fail);
  }

  function editTenant(t, target) {
    var name = h("input", { id: "te-name", required: true, maxlength: 200, value: t.name });
    var active = h("input", { id: "te-active", type: "checkbox", checked: t.active, disabled: t.default,
      "aria-describedby": t.default ? "te-active-hint" : null });
    var soar = h("input", { id: "te-soar", type: "url", value: t.soar_url || "" });
    var index = h("input", { id: "te-index", value: t.splunk_index || "", placeholder: "empty = default index" });
    var form = h("form", {
      class: "panel-body form-grid", onsubmit: function (ev) {
        ev.preventDefault();
        var body = { name: name.value.trim(), soar_url: soar.value.trim() || null,
          splunk_index: index.value.trim() || null };
        if (!t.default) body.active = active.checked;
        api("PATCH", "/tenants/" + encodeURIComponent(t.slug), body).then(function () {
          toast("Tenant " + t.slug + " saved");
          return refreshMe().then(viewTenants);
        }).catch(fail);
      }
    }, field("Name", name, { required: true }),
      h("div", null, h("label", { for: "te-active" }, active, " Active")),
      field("SOAR URL", soar), field("Splunk index", index),
      t.default ? h("div", { class: "wide muted", id: "te-active-hint",
        text: "The default tenant cannot be deactivated." }) : null,
      h("div", { class: "wide actions" },
        h("button", { class: "primary", type: "submit" }, "Save"),
        h("button", { type: "button", onclick: function () { clear(target); } }, "Cancel")));
    clear(target);
    target.appendChild(h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h2", { text: "Edit tenant " + t.slug })), form));
    name.focus();
  }

  // ------------------------------------------------------------------ reports
  var REPORT_RANGES = [["7", "Last 7 days"], ["30", "Last 30 days"], ["90", "Last 90 days"], ["all", "All time"]];

  function tile(label, value, cls) {
    return h("div", { class: "tile" + (cls ? " " + cls : "") },
      h("div", { class: "tile-label", text: label }),
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
    if (!canSeeReports()) { render(h("p", { class: "error", text: "Reports are available for admins and agents." })); return; }
    var range = params.since || "30";
    var rangeSel = select("r-since", REPORT_RANGES, range);
    rangeSel.addEventListener("change", function () { location.hash = "#/reports?since=" + rangeSel.value; });
    var body = h("div", { class: "panel-body", "aria-live": "polite" }, h("p", { class: "muted", text: "Loading..." }));
    var tenant = (session.me.active_tenant || session.me.tenant || {}).name || "";
    render(h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h1", { text: "Reports" + (tenant ? " - " + tenant : "") }),
        h("span", { class: "spacer" }), h("label", { for: "r-since", text: "Period" }), rangeSel),
      body));
    var qs = "";
    var days = parseInt(range, 10);
    if (days > 0) {
      var since = new Date(Date.now() - days * 86400000).toISOString().replace(/\.\d{3}Z$/, "Z");
      qs = "?since=" + encodeURIComponent(since);
    }
    api("GET", "/reports/summary" + qs).then(function (r) {
      clear(body);
      var prio = Object.keys(r.by_priority).sort().map(function (k) { return [PRIORITIES[k] || k, r.by_priority[k]]; });
      var states = STATES.filter(function (s) { return s in r.by_state; })
        .map(function (s) { return [STATE_LABEL[s], r.by_state[s]]; });
      var teams = Object.keys(r.by_team).map(function (k) { return [k, r.by_team[k]]; });
      body.appendChild(h("div", { class: "tiles" },
        tile("Tickets total", r.tickets_total), tile("Open", r.open_count),
        tile("Created (last 7 days)", r.created_last_7d), tile("Resolved (last 7 days)", r.resolved_last_7d),
        tile("MTTR (hours)", r.mttr_hours === null || r.mttr_hours === undefined ? "-" : r.mttr_hours)));
      body.appendChild(h("p", { class: "muted",
        text: "All numbers refer to tickets created " + (days > 0 ? "in the selected period" : "at any time") +
          ". Generated " + fmtTime(r.generated_at) + "." }));
      body.appendChild(h("div", { class: "bar-groups" },
        bars("By state", states), bars("By priority", prio), bars("By team", teams)));
    }).catch(function (err) {
      clear(body);
      body.appendChild(h("p", { class: "error", text: errorText(err) }));
    });
  }

  // ----------------------------------------------------------- audit / splunk
  function viewAudit() {
    var body = h("div", { class: "panel-body", "aria-live": "polite" }, h("p", { class: "muted", text: "Loading..." }));
    render(h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h1", { text: "Audit / Splunk" }), h("span", { class: "spacer" }),
        h("button", { type: "button", onclick: viewAudit }, "Refresh")),
      body));
    api("GET", "/audit/status").then(function (s) {
      clear(body);
      function num(v) { return v === null || v === undefined ? "-" : v; }
      body.appendChild(h("div", { class: "tiles" },
        tile("Splunk HEC", s.hec_enabled ? "active" : "off", s.hec_enabled ? "ok" : null),
        tile("Sent", num(s.sent)), tile("Failed", num(s.failed), s.failed ? "bad" : null),
        tile("Dropped", num(s.dropped), s.dropped ? "bad" : null), tile("Queued", num(s.queued)),
        tile("Last success", s.last_success_at ? fmtTime(s.last_success_at) : "-"),
        tile("Last error", s.last_error || "none", s.last_error ? "bad" : null)));
      body.appendChild(h("p", { class: "muted",
        text: "Every action is also written to the local audit log" +
          (s.local_written !== undefined ? " (" + s.local_written + " events)" : "") + ". Numbers are since the server start." }));
    }).catch(function (err) {
      clear(body);
      body.appendChild(h("p", { class: "error", text: errorText(err) }));
    });
  }

  start();
})();
