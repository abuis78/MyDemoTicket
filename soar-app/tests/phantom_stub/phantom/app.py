"""Stub of phantom.app: status constants and helpers."""

APP_SUCCESS = True
APP_ERROR = False


def is_success(status):
    return bool(status)


def is_fail(status):
    return not status
