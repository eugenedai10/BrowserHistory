#!/usr/bin/env python3
"""Native messaging host: appends browsing events to ~/chrome-history/YYYY-MM-DD.jsonl.

Protocol (Chrome native messaging): each message is a 4-byte native-endian length
followed by UTF-8 JSON. Requests:
    {"events": [ {...}, ... ]}   -> appends one JSON line per event
    {"cmd": "ping"}              -> returns status info
Responses: {"ok": true, ...} or {"ok": false, "error": "..."}.

Never print to stdout: stdout is the protocol channel.
"""
import datetime
import json
import os
import re
import signal
import struct
import sys
import time

import chainlog

LOG_DIR = os.environ.get("CHROME_HISTORY_DIR") or os.path.expanduser("~/chrome-history")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def read_message():
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    (length,) = struct.unpack("=I", raw)
    data = sys.stdin.buffer.read(length)
    if len(data) < length:
        return None
    return json.loads(data.decode("utf-8"))


def send_message(obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("=I", len(data)) + data)
    sys.stdout.buffer.flush()


def ensure_dir():
    os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(LOG_DIR, 0o700)
    except OSError:
        pass


def day_path(day):
    return os.path.join(LOG_DIR, day + ".jsonl")


def append_events(items):
    """items: [(day, event dict)]. All writes go through chainlog: hash-chained and append-only."""
    return chainlog.append(LOG_DIR, items)


def handle_events(events):
    today = datetime.date.today().isoformat()
    items = []
    for ev in events:
        if not isinstance(ev, dict):
            ev = {"ts": now_iso(), "type": "invalid_event", "raw": str(ev)[:500]}
        ts = ev.get("ts")
        day = ts[:10] if isinstance(ts, str) and DAY_RE.match(ts) else today
        items.append((day, ev))
    append_events(items)
    return {"ok": True, "n": len(events)}


def count_lines(path):
    try:
        with open(path, "rb") as f:
            return sum(chunk.count(b"\n") for chunk in iter(lambda: f.read(1 << 20), b""))
    except OSError:
        return 0


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")


def write_marker(kind, ts=None, **extra):
    """Host-written marker. Because the host (not the extension) writes it, it shows up even when
    the extension has been disabled/removed, which is what lets summary.py explain gaps."""
    ev = {"ts": ts or now_iso(), "type": kind, "pid": os.getpid()}
    ev.update(extra)
    try:
        ensure_dir()
        append_events([(ev["ts"][:10], ev)])
    except Exception:
        pass


def handle_ping():
    today = datetime.date.today().isoformat()
    path = day_path(today)
    return {"ok": True, "dir": LOG_DIR, "file": path, "events_today": count_lines(path)}


def classify_stop(parent_pid, wait=5.0):
    """Why did the connection close? Chrome launches the host as its child. If that parent process
    goes away shortly after our stdin closes, the browser exited (quit or crash); if it is still alive,
    only the extension went away (reloaded, disabled, removed, or its worker restarted)."""
    deadline = time.time() + wait
    while time.time() < deadline:
        if os.getppid() != parent_pid:  # reparented to launchd => parent died
            return "browser_exit"
        time.sleep(0.1)
    return "extension_stopped"


def main():
    parent_pid = os.getppid()
    ensure_dir()
    write_marker("host_start", ppid=parent_pid)
    reason = "eof"
    try:
        loop()
    except SystemExit:
        reason = "terminated"
    except Exception as e:  # never die silently: record why
        reason = "error: %s" % e
    signal.signal(signal.SIGTERM, signal.SIG_IGN)  # finish classifying and write the marker
    stop_ts = now_iso()  # time of the disconnect, not of the end of the classification wait
    write_marker("host_stop", ts=stop_ts, reason=reason, cause=classify_stop(parent_pid), ppid=parent_pid)


def loop():
    while True:
        try:
            msg = read_message()
        except Exception as e:  # malformed input
            send_message({"ok": False, "error": "bad message: %s" % e})
            return
        if msg is None:
            return
        try:
            if isinstance(msg, dict) and msg.get("cmd") == "ping":
                send_message(handle_ping())
            elif isinstance(msg, dict) and isinstance(msg.get("events"), list):
                send_message(handle_events(msg["events"]))
            else:
                send_message({"ok": False, "error": "unknown request"})
        except BrokenPipeError:
            return
        except Exception as e:
            send_message({"ok": False, "error": str(e)})


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    main()
