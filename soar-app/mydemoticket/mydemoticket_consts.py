"""Constants for the MyDemoTicket SOAR app."""

API_PREFIX = "/api/v1"
SOURCE_HEADER = "X-MyDemoTicket-Source"
DEFAULT_TIMEOUT = 30
DEFAULT_SOURCE_TAG = "soar"
DEFAULT_INGEST_LABEL = "mydemoticket"
SNAPSHOT_ARTIFACT_NAME = "ticket snapshot"
SNAPSHOT_ARTIFACT_LABEL = "mydemoticket_snapshot"

VALID_STATES = ("new", "in_progress", "on_hold", "resolved", "closed", "cancelled")
VALID_COMMENT_TYPES = ("comment", "work_note")
VALID_PRIORITIES = (1, 2, 3, 4)
ALLOWED_ROLES = ("integration", "agent", "admin")

# Ticket priority -> SOAR container severity (same mapping as the ticket system's SOAR webhook,
# docs/api.md, so polled and pushed containers of one ticket agree)
PRIORITY_TO_SEVERITY = {1: "high", 2: "high", 3: "medium", 4: "low"}

STATE_LAST_POLL = "last_poll"
POLL_PAGE_SIZE = 100
DEFAULT_POLL_NOW_COUNT = 10
DEFAULT_LIST_LIMIT = 100
ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Fields the PATCH /tickets/{number} endpoint accepts as plain values
UPDATABLE_FIELDS = (
    "short_description",
    "description",
    "state",
    "priority",
    "assignment_group",
    "assigned_to",
    "category",
    "close_notes",
)
