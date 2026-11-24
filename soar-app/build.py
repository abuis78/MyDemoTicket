#!/usr/bin/env python3
"""Package the MyDemoTicket SOAR app as dist/mydemoticket.tgz (stdlib tarfile only).

The archive root is ``mydemoticket/`` as required by SOAR. Usage: python3 build.py
"""

import os
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(HERE, "mydemoticket")
DIST_DIR = os.path.join(HERE, "dist")
ARCHIVE = os.path.join(DIST_DIR, "mydemoticket.tgz")

APP_FILES = (
    "__init__.py",
    "mydemoticket.json",
    "mydemoticket_connector.py",
    "mydemoticket_consts.py",
    "mydemoticket.svg",
    "mydemoticket_dark.svg",
    "README.md",
)


def _filter(info):
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    return info


def build(archive=ARCHIVE):
    missing = [f for f in APP_FILES if not os.path.isfile(os.path.join(APP_DIR, f))]
    if missing:
        raise SystemExit("Missing app files: {}".format(", ".join(missing)))
    os.makedirs(os.path.dirname(archive), exist_ok=True)
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(APP_DIR, arcname="mydemoticket", recursive=False, filter=_filter)
        for name in APP_FILES:
            tar.add(os.path.join(APP_DIR, name), arcname="mydemoticket/" + name, filter=_filter)
    return archive


if __name__ == "__main__":
    path = build(sys.argv[1] if len(sys.argv) > 1 else ARCHIVE)
    print("Built {}".format(path))
