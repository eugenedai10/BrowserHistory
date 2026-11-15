#!/usr/bin/env python3
"""External monitor, run by launchd (NOT by Chrome).

Watches Chrome from the outside and writes ground-truth markers into the same
~/chrome-history/YYYY-MM-DD.jsonl that the extension's native host writes:

  browser_start / browser_stop     Chrome main process appeared / disappeared
  extension_missing / _present     Chrome is running but the logger's native host is not (extension
                                   disabled/removed), and when it comes back
  system_sleep / system_wake       the Mac slept (inferred from a wall-clock vs uptime-clock jump)
  monitor_start / monitor_heartbeat  liveness of this monitor itself

tools/summary.py uses these to say exactly why the log has a gap.
"""
import datetime
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "native-host"))
import host  # noqa: E402  (reuse write_marker/LOG_DIR so both writers append identically)

POLL_S = 2
GRACE_S = 15            # host must be absent this long (with Chrome running) before it counts as missing
SLEEP_THRESHOLD_S = 5   # wall-clock minus uptime-clock jump that means "the Mac slept"
HEARTBEAT_S = 60

# Chrome's main process; helpers are "Google Chrome Helper (...)" and do not match.
CHROME_RE = re.compile(r"^(?:.*/)?Google Chrome\.app/Contents/MacOS/Google Chrome(?:\s|$)")
HOST_MARKERS = ("native-host/host.py", "native-host/host_launcher.sh")


class Proc:
    __slots__ = ("pid", "ppid", "started", "command")

    def __init__(self, pid, ppid, started, command):
        self.pid, self.ppid, self.started, self.command = pid, ppid, started, command


def parse_ps(text):
    """Parse `ps -axo pid=,ppid=,lstart=,command=` output. lstart is 5 tokens: 'Wed Nov  4 08:03:39 2026'."""
    out = []
    for line in text.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        try:
            started = datetime.datetime.strptime(" ".join(parts[2:7]), "%a %b %d %H:%M:%S %Y").timestamp()
            out.append(Proc(int(parts[0]), int(parts[1]), started, parts[7]))
        except ValueError:
            continue
    return out


def find_chrome(procs):
    return next((p for p in procs if CHROME_RE.match(p.command)), None)


def has_host(procs, chrome_pid):
    return any(p.ppid == chrome_pid and any(m in p.command for m in HOST_MARKERS) for p in procs)


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="milliseconds")


class Monitor:
    """State machine. tick() is a pure function of (snapshot, wall clock, uptime clock) plus its own
    state; all output goes through emit(kind, epoch, **fields), so it is testable without Chrome."""

    def __init__(self, emit):
        self.emit = emit
        self.last_wall = None
        self.last_mono = None
        self.last_heartbeat = None
        self.chrome = None  # {pid, started, seen, ext, absent_since, missing_since}

    def tick(self, procs, wall, mono):
        slept = 0.0
        if self.last_wall is not None:
            slept = (wall - self.last_wall) - (mono - self.last_mono)
            if slept > SLEEP_THRESHOLD_S:
                self.emit("system_sleep", self.last_wall, inferred=True)
                self.emit("system_wake", wall, asleep_s=round(slept, 1))
        self.last_wall, self.last_mono = wall, mono
        extra = {"during_sleep": True} if slept > SLEEP_THRESHOLD_S else {}

        proc = find_chrome(procs)
        cur = self.chrome

        if cur and (proc is None or proc.pid != cur["pid"]):
            self.emit("browser_stop", wall, chrome_pid=cur["pid"], ran_s=round(wall - cur["started"]), **extra)
            self.chrome = cur = None

        if proc and not cur:
            late = (wall - proc.started) > 10  # we noticed Chrome well after it launched
            self.emit("browser_start", wall if late else proc.started, chrome_pid=proc.pid,
                      started_at=iso(proc.started), observed_late=late, **extra)
            cur = self.chrome = {"pid": proc.pid, "started": proc.started, "seen": wall,
                                 "ext": "unknown", "absent_since": None, "missing_since": None}

        if cur:
            if has_host(procs, cur["pid"]):
                if cur["ext"] == "missing":
                    self.emit("extension_present", wall, chrome_pid=cur["pid"],
                              missing_s=round(wall - cur["missing_since"]), **extra)
                cur["ext"], cur["absent_since"], cur["missing_since"] = "present", None, None
            else:
                if cur["absent_since"] is None:
                    cur["absent_since"] = max(cur["started"], cur["seen"]) if cur["ext"] == "unknown" else wall
                if cur["ext"] != "missing" and wall - cur["absent_since"] >= GRACE_S:
                    cur["ext"], cur["missing_since"] = "missing", cur["absent_since"]
                    self.emit("extension_missing", cur["absent_since"], chrome_pid=cur["pid"], **extra)

        if self.last_heartbeat is None or wall - self.last_heartbeat >= HEARTBEAT_S:
            self.last_heartbeat = wall
            self.emit("monitor_heartbeat", wall, chrome_running=bool(cur),
                      chrome_pid=cur["pid"] if cur else None,
                      extension_connected=(cur["ext"] == "present") if cur else None)


def snapshot():
    try:
        r = subprocess.run(["ps", "-axo", "pid=,ppid=,lstart=,command="], capture_output=True, text=True, timeout=10)
        return parse_ps(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None  # skip this tick; never treat a failed ps as "Chrome quit"


def emit_to_log(kind, epoch, **fields):
    host.write_marker(kind, ts=iso(epoch), **fields)


def main():
    m = Monitor(emit_to_log)
    uptime = time.monotonic()
    emit_to_log("monitor_start", time.time(), cause="login" if uptime < 600 else "restart", system_uptime_s=round(uptime))
    while True:
        procs = snapshot()
        if procs is not None:
            m.tick(procs, time.time(), time.monotonic())
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
