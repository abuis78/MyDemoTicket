"""In-process fake of the Splunk SOAR REST endpoints used by the pairing wizard (127.0.0.1 only).

Models just enough behaviour to make the wizard's idempotency observable: apps by appid/version,
assets by name (GET masks api_token and returns product_name null, as SOAR 8.x does), labels
(listed by GET /rest/container_options; GET /rest/system_settings/events answers 404 as on SOAR 8.x),
playbooks by name with draft mode when the coa metadata is missing.
``fail[(method, path_prefix)] = (status, message)`` injects errors; ``requests`` records every call.
``redirect_to = "<url>"`` answers every request with ``302 Location: <url><path>`` (redirect mode).
"""

import base64
import io
import json
import tarfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REQUIRED_COA = {"python_version": "3.13", "schema": "5.0.9", "version": "8.6.0.530"}


class FakeSoar:
    def __init__(self, token, version="8.8.0.123", labels=("events",), assets=("splunk", "builtin_mc_connector")):
        self.token = token
        self.version = version
        self.lock = threading.Lock()
        self.requests = []
        self.fail = {}
        self.redirect_to = None     # redirect mode: every request answers 302 to this base URL
        self.bool_as_text = False   # True: asset GET returns booleans as "true"/"false" strings
        self.labels = list(labels)
        self.apps = {}          # appid -> {"id", "appid", "app_version", "product_name", "product_vendor"}
        self.assets = {}        # id -> asset dict
        self.playbooks = {}     # id -> {"id", "name", "active", "draft_mode", "labels", "py", "json"}
        self.next_id = 100
        for name in assets:
            self._add_asset({"name": name, "product_name": "Splunk" if name == "splunk" else "Enterprise Security",
                             "product_vendor": "Splunk Inc.", "configuration": {}})
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _do(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                parsed = urllib.parse.urlsplit(self.path)
                query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    body = None
                with fake.lock:
                    fake.requests.append({"method": self.command, "path": parsed.path, "query": query,
                                          "body": body, "raw": raw,
                                          "headers": {k.lower(): v for k, v in self.headers.items()}})
                    redirect = fake.redirect_to
                    if redirect:
                        status, payload = 302, {"redirect": True}
                    else:
                        status, payload = fake.handle(self.command, parsed.path, query, body,
                                                      self.headers.get("ph-auth-token"))
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                if redirect:
                    self.send_header("Location", redirect.rstrip("/") + self.path)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _do

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # ------------------------------------------------------------------ helpers
    def _id(self):
        self.next_id += 1
        return self.next_id

    def _add_asset(self, asset):
        asset = dict(asset, id=self._id())
        self.assets[asset["id"]] = asset
        return asset

    @staticmethod
    def _filtered(rows, query):
        for key, value in query.items():
            if key.startswith("_filter_"):
                field, wanted = key[len("_filter_"):], json.loads(value)
                rows = [r for r in rows if r.get(field) == wanted]
        return {"count": len(rows), "num_pages": 1, "data": rows}

    def calls(self, method=None, path=None):
        with self.lock:
            return [r for r in self.requests if (method is None or r["method"] == method)
                    and (path is None or r["path"].startswith(path))]

    def writes(self):
        return [r for r in self.calls() if r["method"] == "POST"]

    # ------------------------------------------------------------------ routes
    def handle(self, method, path, query, body, token):
        for (m, prefix), answer in self.fail.items():
            if m == method and path.startswith(prefix):
                return answer[0], {"failed": True, "message": answer[1]}
        if token != self.token:
            return 401, {"failed": True, "message": "Invalid token"}
        if method == "GET" and path == "/rest/version":
            return 200, {"version": self.version}
        if method == "GET" and path == "/rest/container_options":
            return 200, {"label": list(self.labels), "sensitivity": [], "severity": [], "severity_colors": {},
                         "status": [], "tags": []}
        if path == "/rest/system_settings/events":
            if method == "GET":   # real SOAR 8.x has no GET here
                return 404, {"failed": True, "message": "Requested item not found"}
            if body and body.get("add_label"):
                if body.get("label_name") in self.labels:
                    return 400, {"failed": True, "message": "Label already exists"}
                self.labels.append(body["label_name"])
                return 200, {"success": True}
        if path == "/rest/app":
            if method == "GET":
                return 200, self._filtered(list(self.apps.values()), query)
            return self._install_app(body)
        if path == "/rest/asset":
            if method == "GET":
                rows = [self._public_asset(a) for a in self.assets.values()]
                return 200, self._filtered(rows, query)
            return self._create_asset(body)
        if method == "POST" and path.startswith("/rest/asset/"):
            asset = self.assets.get(int(path.rsplit("/", 1)[1]))
            if asset is None:
                return 404, {"failed": True, "message": "asset not found"}
            asset["configuration"] = dict((body or {}).get("configuration") or {})
            return 200, {"success": True, "id": asset["id"]}
        if path == "/rest/playbook" and method == "GET":
            rows = [{k: v for k, v in p.items() if k not in ("py", "json")} for p in self.playbooks.values()]
            return 200, self._filtered(rows, query)
        if method == "POST" and path == "/rest/import_playbook":
            return self._import_playbook(body)
        if method == "POST" and path.startswith("/rest/playbook/"):
            pb = self.playbooks.get(int(path.rsplit("/", 1)[1]))
            if pb is None:
                return 404, {"failed": True, "message": "playbook not found"}
            if body.get("active") and pb["draft_mode"]:
                return 400, {"failed": True, "message": "draft playbooks cannot be activated"}
            pb["active"] = bool(body.get("active"))
            return 200, {"success": True}
        return 404, {"failed": True, "message": "not found: %s %s" % (method, path)}

    def _public_asset(self, asset):
        row = dict(asset)
        config = dict(row.get("configuration") or {})
        if "api_token" in config:
            config["api_token"] = "*" * 8   # SOAR never returns password fields in clear text
        if self.bool_as_text and isinstance(config.get("verify_server_cert"), bool):
            config["verify_server_cert"] = str(config["verify_server_cert"]).lower()  # "true"/"false"
        row["configuration"] = config
        row["product_name"] = None          # real SOAR 8.x returns product_name null in the asset detail
        return row

    def _install_app(self, body):
        archive = base64.b64decode(body["app"])
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            names = tar.getnames()
            if "mydemoticket/mydemoticket.json" not in names:
                return 400, {"failed": True, "message": "invalid app archive"}
            manifest = json.load(tar.extractfile("mydemoticket/mydemoticket.json"))
        existing = self.apps.get(manifest["appid"])
        app = {"id": existing["id"] if existing else self._id(), "appid": manifest["appid"],
               "app_version": manifest["app_version"], "product_name": manifest["product_name"],
               "product_vendor": manifest["product_vendor"], "name": manifest["name"], "files": names}
        self.apps[manifest["appid"]] = app
        return 200, {"success": True, "message": "App successfully imported.", "id": app["id"]}

    def _create_asset(self, body):
        if any(a["name"] == body.get("name") for a in self.assets.values()):
            return 400, {"failed": True, "message": "asset with this name already exists"}
        if not any(a["product_name"] == body.get("product_name") and a["product_vendor"] == body.get("product_vendor")
                   for a in self.apps.values()):
            return 400, {"failed": True, "message": "no app installed for this product"}
        asset = self._add_asset({"name": body["name"], "product_name": body["product_name"],
                                 "product_vendor": body["product_vendor"],
                                 "configuration": dict(body.get("configuration") or {})})
        return 200, {"success": True, "id": asset["id"]}

    def _import_playbook(self, body):
        archive = base64.b64decode(body["playbook"])
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            names = tar.getnames()
            jsons = [n for n in names if n.endswith(".json")]
            if len(jsons) != 1 or jsons[0][:-5] + ".py" not in names:
                return 400, {"failed": True, "message": "archive needs <name>.json and <name>.py"}
            name = jsons[0][:-5]
            meta = json.load(tar.extractfile(jsons[0]))
            py = tar.extractfile(name + ".py").read().decode("utf-8")
        coa = meta.get("coa") or {}
        draft = any(coa.get(k) != v for k, v in REQUIRED_COA.items())
        existing = [p for p in self.playbooks.values() if p["name"] == name]
        if existing and not body.get("force"):
            return 400, {"failed": True, "message": "playbook exists (use force)"}
        if existing:
            pb = existing[0]
            pb.update(py=py, json=meta, draft_mode=draft, labels=meta.get("labels"))
        else:
            pb = {"id": self._id(), "name": name, "active": False, "draft_mode": draft, "labels": meta.get("labels"),
                  "py": py, "json": meta}
            self.playbooks[pb["id"]] = pb
        return 200, {"success": True, "message": "Playbook \"%s\" imported." % name, "id": pb["id"]}
