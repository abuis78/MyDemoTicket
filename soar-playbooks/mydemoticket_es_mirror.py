"""
MyDemoTicket -> Splunk ES mirror.

Triggered by artifacts with label "mydemoticket_event" (pushed by the MyDemoTicket ticket system).
Events whose cef.source equals "soar" are ignored (loop prevention).

Target per event:
- cef.esInvestigationId set (ES 8 investigation GUID): comments/work notes become notes on the
  investigation (Enterprise Security app action "add finding or investigation note"); on a state
  change to resolved/closed/cancelled the investigation status is set with
  "update finding or investigation".
- else cef.esFindingId set: comments/work notes become notes on the finding ("add finding or
  investigation note"); on a state change (ticket.created / ticket.state_changed) the status is set
  with the Splunk app action "update event" (without comment - a comment there would show up as a
  second "Finding Update" note in ES 8).
- neither: a markdown note with the change is added to the container.

Status and notes run as separate actions with separate callbacks, so a failing note never blocks
the status update. Every failure adds a note to the container.
cef.esInvestigationId, cef.commentType and cef.actor are optional (older ticket servers lack them).
"""

import phantom.rules as phantom
import json
from datetime import datetime, timedelta

# --- configuration -------------------------------------------------------------------------
SPLUNK_ASSET = "splunk"                 # Splunk app asset ("update event")
ES_ASSET = "builtin_mc_connector"       # Enterprise Security app asset (notes, investigations)
EVENT_LABEL = "mydemoticket_event"
IGNORED_SOURCE = "soar"
MIRROR_OWNER = True                     # set False if ticket users do not exist as ES users
INVESTIGATION_CLOSE_DISPOSITION = ""    # e.g. "disposition:1"; empty = do not send a disposition

# ticketState -> Splunk ES status (integer_status of "update event", status of
# "update finding or investigation"; both are strings in the app schemas)
STATE_TO_ES_STATUS = {
    "new": 1,           # New
    "in_progress": 2,   # In Progress
    "on_hold": 3,       # Pending
    "resolved": 4,      # Resolved
    "closed": 5,        # Closed
    "cancelled": 5,     # Closed
}
STATE_EVENTS = ("ticket.created", "ticket.state_changed")
INVESTIGATION_STATUS_STATES = ("resolved", "closed", "cancelled")
COMMENT_TYPE_LABELS = {"comment": "comment", "work_note": "work note", "summary": "summary"}


def _new_mirror_events(container):
    """Return mirror-relevant artifacts of this run as list of dicts."""
    rows = phantom.collect2(
        container=container,
        datapath=[
            "artifact:*.label",
            "artifact:*.cef.ticketNumber",
            "artifact:*.cef.ticketState",
            "artifact:*.cef.assignedTo",
            "artifact:*.cef.comment",
            "artifact:*.cef.commentAuthor",
            "artifact:*.cef.commentType",
            "artifact:*.cef.actor",
            "artifact:*.cef.esFindingId",
            "artifact:*.cef.esInvestigationId",
            "artifact:*.cef.source",
            "artifact:*.cef.event",
            "artifact:*.id",
        ],
        scope="new",
    )
    events = []
    for (label, number, state, assigned_to, comment, comment_author, comment_type, actor, es_id,
         investigation_id, source, event, artifact_id) in rows:
        if label != EVENT_LABEL:
            continue
        if (source or "").strip().lower() == IGNORED_SOURCE:
            phantom.debug("ignoring artifact {} from source {}".format(artifact_id, source))
            continue
        events.append({
            "number": number, "state": state, "assigned_to": assigned_to,
            "comment": (comment or "").strip(), "comment_author": (comment_author or "").strip(),
            "comment_type": (comment_type or "").strip(), "actor": (actor or "").strip(),
            "es_finding_id": (es_id or "").strip(),
            "es_investigation_id": (investigation_id or "").strip(),
            "event": event, "artifact_id": artifact_id,
        })
    return events


def _state_changed(e):
    return e["event"] in STATE_EVENTS


def _note_content(e):
    """'MyDemoTicket <number> - <type>/<author>: <text>' (type omitted if the server does not send it)."""
    author = e["comment_author"] or e["actor"] or "unknown"
    kind = COMMENT_TYPE_LABELS.get(e["comment_type"], e["comment_type"])
    who = "{}/{}".format(kind, author) if kind else author
    return "MyDemoTicket {} – {}: {}".format(e["number"], who, e["comment"])


def _failure_note(container, title, content):
    phantom.add_note(container=container, note_type="general", title=title, content=content,
                     note_format="markdown")


@phantom.playbook_block()
def on_start(container):
    phantom.debug("on_start() called")
    route_events(container=container)
    return


@phantom.playbook_block()
def route_events(action=None, success=None, container=None, results=None, handle=None,
                 filtered_artifacts=None, filtered_results=None, custom_function=None,
                 loop_state_json=None, **kwargs):
    phantom.debug("route_events() called")
    events = _new_mirror_events(container)
    with_inv = [e for e in events if e["es_investigation_id"]]
    with_es = [e for e in events if not e["es_investigation_id"] and e["es_finding_id"]]
    without_es = [e for e in events if not e["es_investigation_id"] and not e["es_finding_id"]]
    finding_status = [e for e in with_es if _state_changed(e)]
    inv_status = [e for e in with_inv if _state_changed(e) and e["state"] in INVESTIGATION_STATUS_STATES]
    notes = [e for e in with_inv + with_es if e["comment"]]
    if finding_status:
        update_es_event(container=container, events=finding_status)
    if inv_status:
        update_es_investigation(container=container, events=inv_status)
    if notes:
        add_es_note(container=container, events=notes)
    if without_es:
        add_container_note(container=container, events=without_es)
    return


@phantom.playbook_block()
def update_es_event(action=None, success=None, container=None, results=None, handle=None,
                    filtered_artifacts=None, filtered_results=None, custom_function=None,
                    loop_state_json=None, events=None, **kwargs):
    phantom.debug("update_es_event() called")
    parameters = []
    for e in events or []:
        status = STATE_TO_ES_STATUS.get(e["state"])
        if status is None:
            continue
        param = {"event_ids": e["es_finding_id"], "integer_status": str(status),
                 "context": {"artifact_id": e["artifact_id"]}}
        if MIRROR_OWNER and e["assigned_to"]:
            param["owner"] = e["assigned_to"]
        parameters.append(param)
    if parameters:
        phantom.act("update event", parameters=parameters, assets=[SPLUNK_ASSET],
                    name="update_es_event", callback=update_es_event_done)
    return


@phantom.playbook_block()
def update_es_event_done(action=None, success=None, container=None, results=None, handle=None,
                         filtered_artifacts=None, filtered_results=None, custom_function=None,
                         loop_state_json=None, **kwargs):
    phantom.debug("update_es_event_done() called, success={}".format(success))
    if not success:
        _failure_note(container, "MyDemoTicket: ES status mirror failed",
                      "Splunk 'update event' failed. See action run 'update_es_event'.")
    return


@phantom.playbook_block()
def update_es_investigation(action=None, success=None, container=None, results=None, handle=None,
                            filtered_artifacts=None, filtered_results=None, custom_function=None,
                            loop_state_json=None, events=None, **kwargs):
    phantom.debug("update_es_investigation() called")
    parameters = []
    for e in events or []:
        param = {"id": e["es_investigation_id"], "status": str(STATE_TO_ES_STATUS[e["state"]]),
                 "context": {"artifact_id": e["artifact_id"]}}
        if INVESTIGATION_CLOSE_DISPOSITION:
            param["disposition"] = INVESTIGATION_CLOSE_DISPOSITION
        parameters.append(param)
    if parameters:
        phantom.act("update finding or investigation", parameters=parameters, assets=[ES_ASSET],
                    name="update_es_investigation", callback=update_es_investigation_done)
    return


@phantom.playbook_block()
def update_es_investigation_done(action=None, success=None, container=None, results=None,
                                 handle=None, filtered_artifacts=None, filtered_results=None,
                                 custom_function=None, loop_state_json=None, **kwargs):
    phantom.debug("update_es_investigation_done() called, success={}".format(success))
    if not success:
        _failure_note(container, "MyDemoTicket: ES investigation status failed",
                      "'update finding or investigation' failed. See action run 'update_es_investigation'.")
    return


@phantom.playbook_block()
def add_es_note(action=None, success=None, container=None, results=None, handle=None,
                filtered_artifacts=None, filtered_results=None, custom_function=None,
                loop_state_json=None, events=None, **kwargs):
    phantom.debug("add_es_note() called")
    parameters = [{"id": e["es_investigation_id"] or e["es_finding_id"], "content": _note_content(e),
                   "context": {"artifact_id": e["artifact_id"]}} for e in events or []]
    if parameters:
        phantom.act("add finding or investigation note", parameters=parameters, assets=[ES_ASSET],
                    name="add_es_note", callback=add_es_note_done)
    return


@phantom.playbook_block()
def add_es_note_done(action=None, success=None, container=None, results=None, handle=None,
                     filtered_artifacts=None, filtered_results=None, custom_function=None,
                     loop_state_json=None, **kwargs):
    phantom.debug("add_es_note_done() called, success={}".format(success))
    if not success:
        _failure_note(container, "MyDemoTicket: ES note failed",
                      "'add finding or investigation note' failed. See action run 'add_es_note'.")
    return


@phantom.playbook_block()
def add_container_note(action=None, success=None, container=None, results=None, handle=None,
                       filtered_artifacts=None, filtered_results=None, custom_function=None,
                       loop_state_json=None, events=None, **kwargs):
    phantom.debug("add_container_note() called")
    lines = ["| Ticket | Event | State | Assigned to | Comment |", "|---|---|---|---|---|"]
    for e in events or []:
        lines.append("| {} | {} | {} | {} | {} |".format(
            e["number"], e["event"], e["state"], e["assigned_to"] or "-",
            (e["comment"] or "").replace("|", "/").replace("\n", " ")))
    phantom.add_note(container=container, note_type="general",
                     title="MyDemoTicket update (no ES finding linked)",
                     content="\n".join(lines), note_format="markdown")
    return


@phantom.playbook_block()
def on_finish(container, summary):
    phantom.debug("on_finish() called")
    return
