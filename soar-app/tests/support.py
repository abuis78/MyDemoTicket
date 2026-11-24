"""Shared test setup: puts the phantom stub and the app directory on sys.path."""

import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SOAR_APP_DIR = os.path.dirname(TESTS_DIR)
APP_DIR = os.path.join(SOAR_APP_DIR, "mydemoticket")
REPO_DIR = os.path.dirname(SOAR_APP_DIR)
PLAYBOOK_DIR = os.path.join(REPO_DIR, "soar-playbooks")

for path in (os.path.join(TESTS_DIR, "phantom_stub"), APP_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)
