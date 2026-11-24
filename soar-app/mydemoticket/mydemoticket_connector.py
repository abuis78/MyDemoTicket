"""Splunk SOAR connector for the MyDemoTicket ticket system.

Standard library only (urllib for HTTP) plus the phantom modules of the SOAR platform.
Python 3.9 compatible.
"""

import json
import socket
import ssl
import sys
from datetime import datetime, timezone
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

import phantom.app as phantom
from phantom.action_result import ActionResult
from phantom.base_connector import BaseConnector

try:
    from mydemoticket_consts import *  # noqa: F401,F403  (SOAR loads the app dir flat)
except ImportError:  # pragma: no cover - package-style import (tests)
    from .mydemoticket_consts import *  # noqa: F401,F403


class MyDemoTicketConnector(BaseConnector):

    def __init__(self):
        super(MyDemoTicketConnector, self).__init__()
        self._state = None
        self._base_url = None
        self._api_token = None
        self._verify = True
        self._source_tag = DEFAULT_SOURCE_TAG
        self._ingest_label = DEFAULT_INGEST_LABEL
        self._poll_state_filter = None

    # ------------------------------------------------------------------ lifecycle
    def initialize(self):
        config = self.get_config()
        self._state = self.load_state() or {}
        self._base_url = str(config.get("base_url", "")).rstrip("/")
        self._api_token = config.get("api_token")
        self._verify = bool(config.get("verify_server_cert", True))
        self._source_tag = config.get("source_tag") or DEFAULT_SOURCE_TAG
        self._ingest_label = config.get("ingest_label") or DEFAULT_INGEST_LABEL
        self._poll_state_filter = config.get("poll_state_filter") or None
        if not self._base_url.startswith(("http://", "https://")):
            self.save_progress("Asset config base_url must start with http:// or https://")
            return phantom.APP_ERROR
        return phantom.APP_SUCCESS

    def finalize(self):
        self.save_state(self._state)
        return phantom.APP_SUCCESS

    # ------------------------------------------------------------------ helpers
    def _ssl_context(self):
        if self._verify:
            return ssl.create_default_context()
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _make_rest_call(self, endpoint, action_result, method="GET", params=None, body=None):
        """Call the ticket API. Returns (status, parsed_json_or_None)."""
        url = "{}{}{}".format(self._base_url, API_PREFIX, endpoint)
        if params:
            clean = {k: v for k, v in params.items() if v is not None and v != ""}
            if clean:
                url = "{}?{}".format(url, urlparse.urlencode(clean))

        headers = {
            "Accept": "application/json",
            "Authorization": "Bearer {}".format(self._api_token or ""),
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if method in ("POST", "PATCH", "PUT", "DELETE"):
            headers[SOURCE_HEADER] = self._source_tag

        req = urlrequest.Request(url, data=data, headers=headers, method=method)
        self.debug_print("MyDemoTicket request", "{} {}".format(method, url))
        try:
            kwargs = {"timeout": DEFAULT_TIMEOUT}
            if url.startswith("https://"):
                kwargs["context"] = self._ssl_context()
            with urlrequest.urlopen(req, **kwargs) as resp:
                raw = resp.read()
        except urlerror.HTTPError as e:
            return action_result.set_status(phantom.APP_ERROR, self._http_error_message(e)), None
        except (socket.timeout, TimeoutError):
            return action_result.set_status(
                phantom.APP_ERROR, "Timeout after {} s calling {} {}".format(DEFAULT_TIMEOUT, method, endpoint)), None
        except urlerror.URLError as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                return action_result.set_status(
                    phantom.APP_ERROR, "Timeout after {} s calling {} {}".format(DEFAULT_TIMEOUT, method, endpoint)), None
            return action_result.set_status(phantom.APP_ERROR, "Connection error: {}".format(reason)), None
        except Exception as e:  # noqa: BLE001 - report every transport failure as APP_ERROR
            return action_result.set_status(phantom.APP_ERROR, "Request failed: {}".format(e)), None

        if not raw:
            return phantom.APP_SUCCESS, {}
        try:
            return phantom.APP_SUCCESS, json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            return action_result.set_status(
                phantom.APP_ERROR, "Invalid JSON in response from {}: {}".format(endpoint, e)), None

    @staticmethod
    def _http_error_message(err):
        text = ""
        try:
            raw = err.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(raw)
                text = parsed.get("error") if isinstance(parsed, dict) else raw
            except ValueError:
                text = raw
        except Exception:  # noqa: BLE001
            text = ""
        finally:
            try:
                err.close()
            except Exception:  # noqa: BLE001
                pass
        return "HTTP {}: {}".format(err.code, (text or err.reason or "").strip())

    @staticmethod
    def _parse_json_object(value, name, action_result):
        """Parse an optional JSON-object string parameter. Returns (status, dict_or_None)."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return phantom.APP_SUCCESS, None
        if isinstance(value, dict):
            return phantom.APP_SUCCESS, value
        try:
            parsed = json.loads(value)
        except ValueError as e:
            return action_result.set_status(phantom.APP_ERROR, "Parameter {} is not valid JSON: {}".format(name, e)), None
        if not isinstance(parsed, dict):
            return action_result.set_status(phantom.APP_ERROR, "Parameter {} must be a JSON object".format(name)), None
        return phantom.APP_SUCCESS, parsed

    @staticmethod
    def _parse_priority(value, action_result):
        if value is None or value == "":
            return phantom.APP_SUCCESS, None
        try:
            prio = int(value)
        except (TypeError, ValueError):
            prio = None
        if prio not in VALID_PRIORITIES:
            return action_result.set_status(phantom.APP_ERROR, "Parameter priority must be 1, 2, 3 or 4"), None
        return phantom.APP_SUCCESS, prio

    @staticmethod
    def _items(response):
        if isinstance(response, list):
            return response
        if isinstance(response, dict):
            return response.get("items", [])
        return []

    @staticmethod
    def _ticket_path(number):
        return "/tickets/{}".format(urlparse.quote(str(number).strip(), safe=""))

    # ------------------------------------------------------------------ actions
    def _handle_test_connectivity(self, param):
        action_result = self.add_action_result(ActionResult(dict(param)))
        self.save_progress("Connecting to {}".format(self._base_url))
        ret, health = self._make_rest_call("/health", action_result)
        if phantom.is_fail(ret):
            self.save_progress("Test Connectivity Failed")
            return action_result.get_status()
        if not isinstance(health, dict) or health.get("status") != "ok":
            self.save_progress("Test Connectivity Failed")
            return action_result.set_status(phantom.APP_ERROR, "Health check did not return status ok")
        ret, me = self._make_rest_call("/me", action_result)
        if phantom.is_fail(ret):
            self.save_progress("Test Connectivity Failed")
            return action_result.get_status()
        role = me.get("role") if isinstance(me, dict) else None
        if role not in ALLOWED_ROLES:
            self.save_progress("Test Connectivity Failed")
            return action_result.set_status(
                phantom.APP_ERROR,
                "API token role '{}' is not allowed (needs one of {})".format(role, ", ".join(ALLOWED_ROLES)))
        self.save_progress("Test Connectivity Passed")
        action_result.add_data({"health": health, "me": me})
        action_result.update_summary({"version": health.get("version"), "role": role})
        return action_result.set_status(phantom.APP_SUCCESS, "Test Connectivity Passed")

    @staticmethod
    def _external_ref_params(param):
        return {key: param[key] for key in EXTERNAL_REF_PARAMS if param.get(key)}

    def _handle_create_ticket(self, param):
        action_result = self.add_action_result(ActionResult(dict(param)))
        body = {"short_description": param.get("short_description")}
        for key in ("description", "assignment_group", "assigned_to", "category"):
            if param.get(key):
                body[key] = param[key]
        ret, prio = self._parse_priority(param.get("priority"), action_result)
        if phantom.is_fail(ret):
            return ret
        if prio is not None:
            body["priority"] = prio
        ret, custom = self._parse_json_object(param.get("custom_fields"), "custom_fields", action_result)
        if phantom.is_fail(ret):
            return ret
        if custom:
            body["custom_fields"] = custom
        refs = {}
        container_id = self.get_container_id()
        if container_id:
            refs["soar_container_id"] = container_id
        refs.update(self._external_ref_params(param))
        if refs:
            body["external_refs"] = refs

        ret, ticket = self._make_rest_call("/tickets", action_result, method="POST", body=body)
        if phantom.is_fail(ret):
            return action_result.get_status()
        action_result.add_data(ticket)
        action_result.update_summary({"ticket_number": ticket.get("number")})
        return action_result.set_status(phantom.APP_SUCCESS, "Created ticket {}".format(ticket.get("number")))

    def _handle_get_ticket(self, param):
        action_result = self.add_action_result(ActionResult(dict(param)))
        ret, ticket = self._make_rest_call(self._ticket_path(param["number"]), action_result)
        if phantom.is_fail(ret):
            return action_result.get_status()
        action_result.add_data(ticket)
        action_result.update_summary({"ticket_number": ticket.get("number"), "state": ticket.get("state")})
        return action_result.set_status(phantom.APP_SUCCESS)

    def _patch_ticket(self, action_result, number, body, message):
        ret, ticket = self._make_rest_call(self._ticket_path(number), action_result, method="PATCH", body=body)
        if phantom.is_fail(ret):
            return action_result.get_status()
        action_result.add_data(ticket)
        action_result.update_summary({"ticket_number": ticket.get("number"), "state": ticket.get("state")})
        return action_result.set_status(phantom.APP_SUCCESS, message.format(ticket.get("number")))

    def _handle_update_ticket(self, param):
        action_result = self.add_action_result(ActionResult(dict(param)))
        body = {}
        for key in UPDATABLE_FIELDS:
            if key == "priority":
                continue
            if param.get(key) not in (None, ""):
                body[key] = param[key]
        if "state" in body and body["state"] not in VALID_STATES:
            return action_result.set_status(
                phantom.APP_ERROR, "Parameter state must be one of {}".format(", ".join(VALID_STATES)))
        ret, prio = self._parse_priority(param.get("priority"), action_result)
        if phantom.is_fail(ret):
            return ret
        if prio is not None:
            body["priority"] = prio
        ret, custom = self._parse_json_object(param.get("custom_fields"), "custom_fields", action_result)
        if phantom.is_fail(ret):
            return ret
        if custom:
            body["custom_fields"] = custom
        refs = self._external_ref_params(param)
        if refs:
            body["external_refs"] = refs
        if not body:
            return action_result.set_status(phantom.APP_ERROR, "No fields to update were given")
        return self._patch_ticket(action_result, param["number"], body, "Updated ticket {}")

    def _handle_set_state(self, param):
        action_result = self.add_action_result(ActionResult(dict(param)))
        state = param.get("state")
        if state not in VALID_STATES:
            return action_result.set_status(
                phantom.APP_ERROR, "Parameter state must be one of {}".format(", ".join(VALID_STATES)))
        body = {"state": state}
        if param.get("close_notes"):
            body["close_notes"] = param["close_notes"]
        return self._patch_ticket(action_result, param["number"], body, "Ticket {} state set")

    def _handle_add_comment(self, param):
        action_result = self.add_action_result(ActionResult(dict(param)))
        ctype = param.get("type") or "comment"
        if ctype not in VALID_COMMENT_TYPES:
            return action_result.set_status(
                phantom.APP_ERROR, "Parameter type must be one of {}".format(", ".join(VALID_COMMENT_TYPES)))
        body = {"body": param.get("body"), "type": ctype}
        author = param.get("author")
        if author not in (None, ""):
            body["author"] = author
        endpoint = "{}/comments".format(self._ticket_path(param["number"]))
        ret, entry = self._make_rest_call(endpoint, action_result, method="POST", body=body)
        if phantom.is_fail(ret):
            return action_result.get_status()
        action_result.add_data(entry)
        action_result.update_summary({"ticket_number": param["number"], "type": ctype})
        return action_result.set_status(phantom.APP_SUCCESS, "Added {} to {}".format(ctype, param["number"]))

    def _handle_list_tickets(self, param):
        action_result = self.add_action_result(ActionResult(dict(param)))
        try:
            limit = int(param.get("limit") or DEFAULT_LIST_LIMIT)
            offset = int(param.get("offset") or 0)
        except (TypeError, ValueError):
            return action_result.set_status(phantom.APP_ERROR, "Parameters limit and offset must be integers")
        if limit <= 0 or offset < 0:
            return action_result.set_status(phantom.APP_ERROR, "limit must be > 0 and offset >= 0")
        params = {k: param.get(k) for k in ("state", "assignment_group", "assigned_to", "updated_since", "q")}
        ref_key = str(param.get("external_ref_key") or "").strip()
        ref_value = param.get("external_ref_value")
        ref_value = "" if ref_value is None else str(ref_value)
        if bool(ref_key) != bool(ref_value):
            return action_result.set_status(
                phantom.APP_ERROR, "Parameters external_ref_key and external_ref_value must be given together")
        if ref_key:
            if not EXTERNAL_REF_KEY_RE.match(ref_key):
                return action_result.set_status(
                    phantom.APP_ERROR, "Parameter external_ref_key must match [a-z0-9_]{1,64}")
            params[EXTERNAL_REF_PREFIX + ref_key] = ref_value
        params.update({"limit": limit, "offset": offset})
        ret, resp = self._make_rest_call("/tickets", action_result, params=params)
        if phantom.is_fail(ret):
            return action_result.get_status()
        items = self._items(resp)
        for item in items:
            action_result.add_data(item)
        total = resp.get("total", len(items)) if isinstance(resp, dict) else len(items)
        action_result.update_summary({"total": total, "returned": len(items)})
        return action_result.set_status(phantom.APP_SUCCESS)

    def _handle_get_journal(self, param):
        action_result = self.add_action_result(ActionResult(dict(param)))
        endpoint = "{}/journal".format(self._ticket_path(param["number"]))
        ret, resp = self._make_rest_call(endpoint, action_result)
        if phantom.is_fail(ret):
            return action_result.get_status()
        entries = self._items(resp)
        for entry in entries:
            action_result.add_data(entry)
        action_result.update_summary({"ticket_number": param["number"], "entries": len(entries)})
        return action_result.set_status(phantom.APP_SUCCESS)

    def _handle_list_custom_fields(self, param):
        action_result = self.add_action_result(ActionResult(dict(param)))
        ret, resp = self._make_rest_call("/custom-fields", action_result)
        if phantom.is_fail(ret):
            return action_result.get_status()
        fields = self._items(resp)
        for field in fields:
            action_result.add_data(field)
        action_result.update_summary({"total": len(fields)})
        return action_result.set_status(phantom.APP_SUCCESS)

    # ------------------------------------------------------------------ on poll
    def _last_change_from_soar(self, number):
        """True only if the newest journal entry clearly carries our source tag.

        If the journal cannot be read or entries have no ``source`` field, the change
        is NOT ignored (ingest everything rather than lose updates).
        """
        probe = ActionResult()
        ret, resp = self._make_rest_call("{}/journal".format(self._ticket_path(number)), probe)
        if phantom.is_fail(ret):
            return False
        entries = [e for e in self._items(resp) if isinstance(e, dict)]
        if not entries:
            return False
        # The API returns the journal oldest-first (ORDER BY id); sort by timestamp only if all have one.
        for ts_key in ("at", "created_at"):
            if all(ts_key in e for e in entries):
                entries = sorted(entries, key=lambda e, k=ts_key: str(e.get(k)))
                break
        source = entries[-1].get("source")
        return source is not None and source == self._source_tag

    def _ticket_cef(self, ticket):
        refs = ticket.get("external_refs") or {}
        return {
            "ticketNumber": ticket.get("number"),
            "ticketState": ticket.get("state"),
            "ticketPriority": ticket.get("priority"),
            "shortDescription": ticket.get("short_description"),
            "assignmentGroup": ticket.get("assignment_group"),
            "assignedTo": ticket.get("assigned_to"),
            "caller": ticket.get("caller"),
            "category": ticket.get("category"),
            "updatedAt": ticket.get("updated_at"),
            "customFields": json.dumps(ticket.get("custom_fields") or {}, sort_keys=True),
            "esFindingId": refs.get("es_finding_id"),
            "esInvestigationId": refs.get("es_investigation_id"),
        }

    def _ingest_ticket(self, ticket):
        number = ticket.get("number")
        container = {
            "name": "{}: {}".format(number, ticket.get("short_description") or ""),
            "source_data_identifier": number,
            "label": self._ingest_label,
            "description": ticket.get("description") or "",
            "severity": PRIORITY_TO_SEVERITY.get(ticket.get("priority"), "medium"),
            "data": ticket,
        }
        ret, message, container_id = self.save_container(container)
        if phantom.is_fail(ret) or not container_id:
            self.debug_print("save_container failed for {}".format(number), message)
            return False
        artifact = {
            "container_id": container_id,
            "name": SNAPSHOT_ARTIFACT_NAME,
            "label": SNAPSHOT_ARTIFACT_LABEL,
            "source_data_identifier": "{}:{}".format(number, ticket.get("updated_at")),
            "cef": self._ticket_cef(ticket),
            "run_automation": False,
        }
        ret, message, _ids = self.save_artifacts([artifact])
        if phantom.is_fail(ret):
            self.debug_print("save_artifacts failed for {}".format(number), message)
            return False
        return True

    def _handle_on_poll(self, param):
        action_result = self.add_action_result(ActionResult(dict(param)))
        poll_now = self.is_poll_now()
        poll_started = datetime.now(timezone.utc).strftime(ISO_FORMAT)
        if poll_now:
            try:
                max_count = int(param.get("container_count") or DEFAULT_POLL_NOW_COUNT)
            except (TypeError, ValueError):
                max_count = DEFAULT_POLL_NOW_COUNT
            updated_since = None
        else:
            max_count = None
            updated_since = self._state.get(STATE_LAST_POLL)

        tickets, offset = [], 0
        while True:
            page = POLL_PAGE_SIZE if max_count is None else min(POLL_PAGE_SIZE, max_count - len(tickets))
            params = {"limit": page, "offset": offset, "updated_since": updated_since,
                      "state": self._poll_state_filter}
            ret, resp = self._make_rest_call("/tickets", action_result, params=params)
            if phantom.is_fail(ret):
                return action_result.get_status()
            items = self._items(resp)
            tickets.extend(items)
            offset += len(items)
            total = resp.get("total", 0) if isinstance(resp, dict) else 0
            if not items or offset >= total or (max_count is not None and len(tickets) >= max_count):
                break

        ingested, skipped, failed = 0, 0, 0
        for ticket in tickets:
            if not poll_now and updated_since and self._last_change_from_soar(ticket.get("number")):
                skipped += 1
                continue
            if self._ingest_ticket(ticket):
                ingested += 1
            else:
                failed += 1

        if not poll_now:
            self._state[STATE_LAST_POLL] = poll_started
        self.save_progress("Ingested {} ticket(s), skipped {} SOAR-originated change(s)".format(ingested, skipped))
        action_result.update_summary({"ingested": ingested, "skipped": skipped, "failed": failed})
        if failed:
            return action_result.set_status(phantom.APP_ERROR, "{} ticket(s) could not be saved".format(failed))
        return action_result.set_status(phantom.APP_SUCCESS)

    # ------------------------------------------------------------------ dispatch
    def handle_action(self, param):
        handlers = {
            "test_connectivity": self._handle_test_connectivity,
            "create_ticket": self._handle_create_ticket,
            "get_ticket": self._handle_get_ticket,
            "update_ticket": self._handle_update_ticket,
            "set_state": self._handle_set_state,
            "add_comment": self._handle_add_comment,
            "list_tickets": self._handle_list_tickets,
            "get_journal": self._handle_get_journal,
            "list_custom_fields": self._handle_list_custom_fields,
            "on_poll": self._handle_on_poll,
        }
        action_id = self.get_action_identifier()
        self.debug_print("action_id", action_id)
        handler = handlers.get(action_id)
        if handler is None:
            action_result = self.add_action_result(ActionResult(dict(param)))
            return action_result.set_status(phantom.APP_ERROR, "Unsupported action: {}".format(action_id))
        return handler(param)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 mydemoticket_connector.py <test.json>")
        sys.exit(2)
    with open(sys.argv[1]) as f:
        in_json = f.read()
    connector = MyDemoTicketConnector()
    connector.print_progress_message = True
    ret_val = connector._handle_action(in_json, None)
    print(json.dumps(json.loads(ret_val), indent=4) if isinstance(ret_val, str) else ret_val)
    sys.exit(0)


if __name__ == "__main__":
    main()
