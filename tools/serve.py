#!/usr/bin/env python3
"""Local web UI for the browsing summary: browse ~/chrome-history day by day.

Usage: serve.py [--port 9898] [--open]

Read-only and bound to 127.0.0.1. The logs hold full URLs and your own Discord posts, so requests whose Host
header is not localhost are refused (DNS-rebinding guard) and no CORS headers are sent.
"""
import argparse
import json
import os
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import summary  # noqa: E402
import verify  # noqa: E402

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UI_FILE = os.path.join(HERE, "ui", "index.html")

_integrity = {"value": None}
_integrity_lock = threading.Lock()


def integrity(refresh=False):
    """Whole-directory hash-chain check: slow-ish, so cached until the UI asks for a refresh."""
    with _integrity_lock:
        if refresh or _integrity["value"] is None:
            try:
                r = verify.verify(summary.LOG_DIR)
                _integrity["value"] = {"ok": r.ok, "status": r.status(), "errors": r.errors[:5],
                                       "warnings": r.warnings[:5], "lines": r.lines, "files": r.files}
            except Exception as e:  # never let the integrity check break the UI
                _integrity["value"] = {"ok": False, "status": "could not check (%s)" % e,
                                       "errors": [], "warnings": [], "lines": 0, "files": 0}
        return _integrity["value"]


class Handler(BaseHTTPRequestHandler):
    server_version = "SummaryUI"

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _host_ok(self):
        port = self.server.server_address[1]
        return (self.headers.get("Host") or "") in ("127.0.0.1:%d" % port, "localhost:%d" % port)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "forbidden host"}, 403)
        path, _, query = self.path.partition("?")
        try:
            if path in ("/", "/index.html"):
                with open(UI_FILE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/days":
                return self._json({"days": summary.list_days(), "trend": summary.trend(),
                                   "integrity": integrity(), "log_dir": summary.LOG_DIR})
            if path == "/api/integrity":
                return self._json(integrity(refresh="refresh=1" in query))
            if path.startswith("/api/day/"):
                day = path[len("/api/day/"):]
                if not DAY_RE.match(day):
                    return self._json({"error": "bad date, expected YYYY-MM-DD"}, 400)
                events = summary.load(day)
                return self._json({"data": summary.day_data(day, events), "health": summary.health(day, events)})
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def do_POST(self):
        self._json({"error": "read-only"}, 405)

    do_PUT = do_DELETE = do_PATCH = do_POST


def make_server(port=9898):
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=9898)
    ap.add_argument("--open", action="store_true", help="open the page in the default browser")
    args = ap.parse_args()
    try:
        srv = make_server(args.port)
    except OSError as e:
        sys.exit("cannot listen on 127.0.0.1:%d (%s); try --port" % (args.port, e))
    port = srv.server_address[1]
    url = "http://127.0.0.1:%d/" % port
    print("Summary UI: %s   (logs: %s, Ctrl-C to stop)" % (url, summary.LOG_DIR))
    if args.open:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
