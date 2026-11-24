"""Stub of phantom.base_connector.BaseConnector.

Only the subset used by the MyDemoTicket connector. Containers and artifacts saved
by the connector are kept in memory so tests can inspect them.
"""

import json

from phantom import app as phantom


class BaseConnector(object):
    def __init__(self):
        self._config = {}
        self._action_identifier = None
        self._action_results = []
        self._state_store = {}
        self._container_id = None
        self._poll_now = False
        self._next_container_id = 1000
        self._next_artifact_id = 5000
        self.containers = []
        self.artifacts = []
        self.progress = []
        self.debug_messages = []

    # --- test helpers (not part of the real API) ---
    def stub_setup(self, config=None, action=None, state=None, container_id=None, poll_now=False):
        self._config = dict(config or {})
        self._action_identifier = action
        self._state_store = dict(state or {})
        self._container_id = container_id
        self._poll_now = poll_now
        self._action_results = []

    # --- real API subset ---
    def get_config(self):
        return self._config

    def get_action_identifier(self):
        return self._action_identifier

    def get_container_id(self):
        return self._container_id

    def is_poll_now(self):
        return self._poll_now

    def add_action_result(self, action_result):
        self._action_results.append(action_result)
        return action_result

    def get_action_results(self):
        return self._action_results

    def save_progress(self, message, *args, **kwargs):
        self.progress.append(message)

    def send_progress(self, message, *args, **kwargs):
        self.progress.append(message)

    def debug_print(self, tag, dump_object=""):
        self.debug_messages.append((tag, dump_object))

    def error_print(self, tag, dump_object=""):
        self.debug_messages.append((tag, dump_object))

    def load_state(self):
        return dict(self._state_store)

    def get_state(self):
        return dict(self._state_store)

    def save_state(self, state):
        self._state_store = dict(state or {})
        return phantom.APP_SUCCESS

    def save_container(self, container):
        # Real SOAR de-duplicates on source_data_identifier and returns the existing id.
        for existing in self.containers:
            if existing.get("source_data_identifier") == container.get("source_data_identifier"):
                return phantom.APP_SUCCESS, "Duplicate container found", existing["id"]
        stored = dict(container)
        stored["id"] = self._next_container_id
        self._next_container_id += 1
        self.containers.append(stored)
        return phantom.APP_SUCCESS, "Container created", stored["id"]

    def save_artifacts(self, artifacts):
        ids = []
        for artifact in artifacts:
            stored = dict(artifact)
            stored["id"] = self._next_artifact_id
            self._next_artifact_id += 1
            self.artifacts.append(stored)
            ids.append(stored["id"])
        return phantom.APP_SUCCESS, "Artifacts saved", ids

    def initialize(self):
        return phantom.APP_SUCCESS

    def finalize(self):
        return phantom.APP_SUCCESS

    def handle_action(self, param):
        raise NotImplementedError

    def _handle_action(self, in_json, handle):
        """Simplified run loop: initialize, one handle_action per parameter set, finalize."""
        data = json.loads(in_json) if isinstance(in_json, str) else in_json
        self._config = data.get("config", {})
        self._action_identifier = data.get("identifier")
        if not self.initialize():
            return phantom.APP_ERROR
        ret = phantom.APP_SUCCESS
        for param in data.get("parameters", [{}]):
            ret = self.handle_action(param) and ret
        self.finalize()
        return ret
