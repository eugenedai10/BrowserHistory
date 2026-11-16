#!/usr/bin/env python3
"""Local web UI for the browsing summary: browse ~/chrome-history day by day.

Usage: serve.py [--port 9898] [--open] [--lan [--allow-host NAME ...]]

Read-only and bound to 127.0.0.1 by default. The logs hold full URLs and your own Discord posts, so requests whose
Host header is not an expected name are refused (DNS-rebinding guard) and no CORS headers are sent.

--lan binds 0.0.0.0 so other machines on the local network can reach it, with no authentication: anyone on the
network can read the logs. Traffic is plain HTTP, so only use this on a network you trust.
"""
import argparse
import ipaddress
import json
import os
import re
import socket
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


def lan_ip():
    """Best-guess LAN address of this machine (UDP connect sends no packets)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


def host_names():
    """Names (other than IP literals) this machine may legitimately be reached by."""
    names = {"localhost"}
    h = socket.gethostname().lower().rstrip(".")
    if h:
        names.add(h)
        short = h.split(".")[0]
        names.update({short, short + ".local"})
    return names


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
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.server.server_address[1]
        if not self.server.lan:
            return host in ("127.0.0.1:%d" % port, "localhost:%d" % port)
        name, sep, p = host.rpartition(":")
        if not sep or p != str(port):
            return False
        name = name.strip("[]").rstrip(".")
        try:
            ipaddress.ip_address(name)  # IP literals can't be DNS-rebound
            return True
        except ValueError:
            return name in self.server.allowed_names

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


def make_server(port=9898, lan=False, allow_hosts=()):
    srv = ThreadingHTTPServer(("0.0.0.0" if lan else "127.0.0.1", port), Handler)
    srv.lan = lan
    srv.allowed_names = host_names() | {h.lower().rstrip(".") for h in allow_hosts}
    return srv


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=9898)
    ap.add_argument("--open", action="store_true", help="open the page in the default browser")
    ap.add_argument("--lan", action="store_true", help="listen on all interfaces (no authentication)")
    ap.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                    help="extra hostname clients may use with --lan (repeatable)")
    args = ap.parse_args()
    try:
        srv = make_server(args.port, lan=args.lan, allow_hosts=args.allow_host)
    except OSError as e:
        sys.exit("cannot listen on port %d (%s); try --port" % (args.port, e))
    port = srv.server_address[1]
    local_url = "http://127.0.0.1:%d/" % port
    if args.lan:
        ip = lan_ip()
        print("Summary UI (LAN, no auth, logs: %s, Ctrl-C to stop)" % summary.LOG_DIR)
        print("  this machine: %s" % local_url)
        if ip:
            print("  network:      http://%s:%d/" % (ip, port))
        print("  also via:     %s" % ", ".join("%s:%d" % (n, port) for n in sorted(srv.allowed_names)))
    else:
        print("Summary UI: %s   (logs: %s, Ctrl-C to stop)" % (local_url, summary.LOG_DIR))
    if args.open:
        threading.Timer(0.3, webbrowser.open, args=(local_url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
