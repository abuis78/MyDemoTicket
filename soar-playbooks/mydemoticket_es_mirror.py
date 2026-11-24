"""
MyDemoTicket -> Splunk ES mirror.

Triggered by artifacts with label "mydemoticket_event" (pushed by the MyDemoTicket ticket system).
If the artifact carries cef.esFindingId, the ticket state, comment and assignee are mirrored to the
Splunk ES notable/finding via the Splunk app action "update event". Otherwise a note is added to
the container. Events whose cef.source equals "soar" are ignored (loop prevention).

NOT tested against a real SOAR or Splunk ES instance (syntax checked with py_compile only).
"""

import phantom.rules as phantom
import json
from datetime import datetime, timedelta

# --- configuration -------------------------------------------------------------------------
SPLUNK_ASSET = "splunk"             # name of the Splunk app asset in SOAR
EVENT_LABEL = "mydemoticket_event"
IGNORED_SOURCE = "soar"
MIRROR_OWNER = True                 # set False if ticket users do not exist as ES users

# ticketState -> Splunk ES status (integer_status of the Splunk app "update event" action)
STATE_TO_ES_STATUS = {
    "new": 1,           # New
    "in_progress": 2,   # In Progress
    "on_hold": 3,       # Pending
    "resolved": 4,      # Resolved
    "closed": 5,        # Closed
    "cancelled": 5,     # Closed
}


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
            "artifact:*.cef.esFindingId",
            "artifact:*.cef.source",
            "artifact:*.cef.event",
            "artifact:*.id",
        ],
        scope="new",
    )
    events = []
    for label, number, state, assigned_to, comment, es_id, source, event, artifact_id in rows:
        if label != EVENT_LABEL:
            continue
        if (source or "").strip().lower() == IGNORED_SOURCE:
            phantom.debug("ignoring artifact {} from source {}".format(artifact_id, source))
            continue
        events.append({
            "number": number, "state": state, "assigned_to": assigned_to, "comment": comment,
            "es_finding_id": (es_id or "").strip(), "event": event, "artifact_id": artifact_id,
        })
    return events


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
    with_es = [e for e in events if e["es_finding_id"]]
    without_es = [e for e in events if not e["es_finding_id"]]
    if with_es:
        update_es_event(container=container, events=with_es)
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
        param = {"event_ids": e["es_finding_id"], "context": {"artifact_id": e["artifact_id"]}}
        status = STATE_TO_ES_STATUS.get(e["state"])
        if status is not None:
            param["integer_status"] = str(status)  # Splunk app declares integer_status as string
        comment = e["comment"] or ""
        prefix = "MyDemoTicket {} ({}, state {})".format(e["number"], e["event"], e["state"])
        param["comment"] = "{}: {}".format(prefix, comment) if comment else prefix
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
        phantom.add_note(container=container, note_type="general",
                         title="MyDemoTicket: ES mirror failed",
                         content="Splunk ES 'update event' failed. See action run 'update_es_event'.",
                         note_format="markdown")
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
