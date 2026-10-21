"""Local labeling tool for the extractor-validation set (preregistration §12).

One final message per screen with the claim fields, keyboard shortcuts, autosave to CSV.
It shows only the task, its numbered items, the allowed targets and the agent's final
message: no model outputs, no extractor output, no ground truth.

usage: python cvr/labeltool/server.py --queue labels/queue.json --out labels/labels.csv [--port 8765]
Binds to 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FIELDS = ["episode", "overall", "requirements_json", "actions_json", "disclosure", "other", "note", "saved_at"]
HTML = (Path(__file__).parent / "index.html").read_text()


class Store:
    def __init__(self, queue: Path, out: Path):
        self.items = json.loads(queue.read_text())
        self.out = out
        self.labels: dict[str, dict] = {}
        if out.exists():
            for row in csv.DictReader(out.open()):
                self.labels[row["episode"]] = row

    def save(self, label: dict):
        row = {k: label.get(k, "") for k in FIELDS}
        row["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.labels[row["episode"]] = row
        buf = io.StringIO()
        w = csv.DictWriter(buf, FIELDS)
        w.writeheader()
        for it in self.items:  # queue order
            if it["episode"] in self.labels:
                w.writerow(self.labels[it["episode"]])
        tmp = self.out.with_suffix(".tmp")
        with open(tmp, "w") as f:
            f.write(buf.getvalue())
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.out)


def make_handler(store: Store):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/":
                return self._send(200, HTML, "text/html; charset=utf-8")
            if self.path == "/api/state":
                return self._send(200, json.dumps({"items": store.items, "labels": store.labels}))
            return self._send(404, "{}")

        def do_POST(self):
            if self.path != "/api/save":
                return self._send(404, "{}")
            n = int(self.headers.get("Content-Length", 0))
            label = json.loads(self.rfile.read(n))
            if label.get("episode") not in {i["episode"] for i in store.items}:
                return self._send(400, json.dumps({"error": "unknown episode"}))
            store.save(label)
            return self._send(200, json.dumps({"ok": True, "labeled": len(store.labels)}))

    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default="labels/queue.json")
    ap.add_argument("--out", default="labels/labels.csv")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    store = Store(Path(a.queue), Path(a.out))
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(store))
    print(f"labeling {len(store.items)} messages at http://127.0.0.1:{a.port}/ -> {a.out}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
