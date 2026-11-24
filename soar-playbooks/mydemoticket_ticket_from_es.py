"""
Splunk ES notable -> MyDemoTicket ticket.

Runs on a container created from a Splunk ES notable/finding (Splunk app ingest). Takes the first
new artifact with cef.event_id and creates a ticket with the MyDemoTicket app action
"create ticket". The app stores the current container ID in external_refs.soar_container_id;
this playbook passes event_id as es_finding_id. A note with the ticket number is added.

NOT tested against a real SOAR or Splunk ES instance (syntax checked with py_compile only).
"""

import phantom.rules as phantom
import json
from datetime import datetime, timedelta

# --- configuration -------------------------------------------------------------------------
MYDEMOTICKET_ASSET = "mydemoticket"     # name of the MyDemoTicket asset in SOAR
ASSIGNMENT_GROUP = "SOC L1"
CATEGORY = "security_incident"

URGENCY_TO_PRIORITY = {"critical": 1, "high": 2, "medium": 3, "low": 4, "informational": 4}


@phantom.playbook_block()
def on_start(container):
    phantom.debug("on_start() called")
    create_ticket_1(container=container)
    return


@phantom.playbook_block()
def create_ticket_1(action=None, success=None, container=None, results=None, handle=None,
                    filtered_artifacts=None, filtered_results=None, custom_function=None,
                    loop_state_json=None, **kwargs):
    phantom.debug("create_ticket_1() called")
    rows = phantom.collect2(
        container=container,
        datapath=[
            "artifact:*.cef.event_id",
            "artifact:*.cef.rule_name",
            "artifact:*.cef.urgency",
            "artifact:*.cef.rule_description",
            "artifact:*.id",
        ],
        scope="new",
    )
    for event_id, rule_name, urgency, rule_description, artifact_id in rows:
        if not event_id:
            continue
        title = rule_name or container.get("name") or "Splunk ES finding"
        description = "{}\n\nSplunk ES event_id: {}\nSOAR container: {}".format(
            rule_description or container.get("description") or "", event_id, container.get("id"))
        parameters = [{
            "short_description": title[:160],
            "description": description,
            "priority": URGENCY_TO_PRIORITY.get(str(urgency or "").lower(), 3),
            "assignment_group": ASSIGNMENT_GROUP,
            "category": CATEGORY,
            "custom_fields": json.dumps({"es_urgency": urgency or ""}),
            "es_finding_id": event_id,
            "context": {"artifact_id": artifact_id},
        }]
        phantom.act("create ticket", parameters=parameters, assets=[MYDEMOTICKET_ASSET],
                    name="create_ticket_1", callback=add_ticket_note)
        return  # one ticket per notable container
    phantom.debug("no artifact with cef.event_id found - no ticket created")
    return


@phantom.playbook_block()
def add_ticket_note(action=None, success=None, container=None, results=None, handle=None,
                    filtered_artifacts=None, filtered_results=None, custom_function=None,
                    loop_state_json=None, **kwargs):
    phantom.debug("add_ticket_note() called")
    rows = phantom.collect2(container=container,
                            datapath=["create_ticket_1:action_result.summary.ticket_number",
                                      "create_ticket_1:action_result.message"],
                            action_results=results)
    number = rows[0][0] if rows else None
    message = rows[0][1] if rows else ""
    if success and number:
        title, content = "MyDemoTicket {} created".format(number), "Ticket **{}** was created.".format(number)
    else:
        title, content = "MyDemoTicket: ticket creation failed", "create ticket failed: {}".format(message)
    phantom.add_note(container=container, note_type="general", title=title, content=content,
                     note_format="markdown")
    return


@phantom.playbook_block()
def on_finish(container, summary):
    phantom.debug("on_finish() called")
    return
