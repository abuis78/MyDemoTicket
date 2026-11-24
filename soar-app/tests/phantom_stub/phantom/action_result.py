"""Stub of phantom.action_result.ActionResult."""


class ActionResult(object):
    def __init__(self, param=None):
        self._param = dict(param or {})
        self._data = []
        self._summary = {}
        self._status = False
        self._message = ""

    def add_data(self, data):
        self._data.append(data)
        return data

    def get_data(self):
        return self._data

    def update_summary(self, summary):
        self._summary.update(summary)
        return self._summary

    def get_summary(self):
        return self._summary

    def set_status(self, status, message="", exception=None):
        self._status = status
        self._message = message
        return status

    def get_status(self):
        return self._status

    def get_message(self):
        return self._message

    def get_param(self):
        return self._param
