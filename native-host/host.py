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
import struct
import sys

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


def append_lines(day, lines):
    fd = os.open(day_path(day), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, ("\n".join(lines) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def handle_events(events):
    by_day = {}
    today = datetime.date.today().isoformat()
    for ev in events:
        ts = ev.get("ts") if isinstance(ev, dict) else None
        day = ts[:10] if isinstance(ts, str) and DAY_RE.match(ts) else today
        by_day.setdefault(day, []).append(json.dumps(ev, ensure_ascii=False, separators=(",", ":")))
    for day, lines in by_day.items():
        append_lines(day, lines)
    return {"ok": True, "n": len(events)}


def count_lines(path):
    try:
        with open(path, "rb") as f:
            return sum(chunk.count(b"\n") for chunk in iter(lambda: f.read(1 << 20), b""))
    except OSError:
        return 0


def handle_ping():
    today = datetime.date.today().isoformat()
    path = day_path(today)
    return {"ok": True, "dir": LOG_DIR, "file": path, "events_today": count_lines(path)}


def main():
    ensure_dir()
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
        except Exception as e:
            send_message({"ok": False, "error": str(e)})


if __name__ == "__main__":
    main()
