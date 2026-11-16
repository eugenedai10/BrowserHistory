#!/usr/bin/env python3
"""External monitor, run by launchd (NOT by Chrome).

Watches Chrome from the outside and writes ground-truth markers into the same
~/chrome-history/YYYY-MM-DD.jsonl that the extension's native host writes:

  browser_start / browser_stop     Chrome main process appeared / disappeared
  extension_missing / _present     Chrome is running but the logger's native host is not (extension
                                   disabled/removed), and when it comes back
  system_sleep / system_wake       the Mac slept (inferred from a wall-clock vs uptime-clock jump)
  monitor_start / monitor_heartbeat  liveness of this monitor itself
  app_focus                        the frontmost application changed (app, bundle); app=null with
                                   reason idle|locked when nobody is at the machine

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

# Foreground-app tracking (app name + bundle id only; no window titles, so no Accessibility permission).
TRACK_APPS = os.environ.get("TRACK_APPS", "1") != "0"   # TRACK_APPS=0 turns it off
IDLE_S = 300            # no input (and no video/call holding the display awake) this long: "away"
RESUME_GAP_S = 10       # ticks further apart than this mean the Mac slept (or we stalled)
LOCK_BUNDLE = "com.apple.loginwindow"

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


class Focus:
    """Foreground-application tracker. tick() is a pure function of (frontmost app, idle seconds, wall clock) plus
    its own state; it emits app_focus only when the effective foreground changes. app=None means nobody is at
    the machine (reason 'idle': no input for IDLE_S, back-dated to when input stopped; or 'locked')."""

    def __init__(self, emit):
        self.emit = emit
        self.cur = None        # last emitted (app, bundle, reason)
        self.last_wall = None
        self.last_ts = 0.0     # ts of the last emitted event: back-dating must never go before it
        self.last_held = None  # last time something was holding the display awake (video, call): counts as use
        self.day = None

    def tick(self, front, idle_s, wall, held=None):
        """held: optional callable -> True/False/None (None = probe failed). Only asked once input has been
        idle for IDLE_S, because watching a video or being on a call involves no input but is not "away"."""
        floor = self.last_ts
        if self.last_wall is not None and wall - self.last_wall > RESUME_GAP_S:
            self.cur, floor = None, wall   # slept or stalled: start a fresh span, don't back-date into the gap
        self.last_wall = wall
        today = datetime.date.fromtimestamp(wall)
        if self.day is not None and today != self.day:
            self.cur = None                # midnight: each day's file must be self-contained
        self.day = today
        if front is None:
            return
        app, bundle = front
        if bundle != LOCK_BUNDLE and idle_s >= IDLE_S and held is not None:
            h = held()
            if h is None:
                return                     # failed probe: skip the tick rather than guess
            if h:
                self.last_held = wall
        if self.last_held is not None:     # activity = input OR display held awake, whichever is more recent
            idle_s = min(idle_s, wall - self.last_held)
        if bundle == LOCK_BUNDLE:
            eff, ts = (None, None, "locked"), wall
        elif idle_s >= IDLE_S:
            eff, ts = (None, None, "idle"), max(wall - idle_s, floor)
        else:
            eff, ts = (app, bundle, None), wall
        if eff == self.cur:
            return
        self.cur, self.last_ts = eff, ts
        fields = {"app": eff[0], "bundle": eff[1]}
        if eff[2]:
            fields["reason"] = eff[2]
        self.emit("app_focus", ts, **fields)


def parse_front(text):
    """(name, bundle id) from `lsappinfo info -only name -only bundleid` output, or None."""
    name = re.search(r'"LSDisplayName"="([^"]*)"', text)
    bundle = re.search(r'"CFBundleIdentifier"="([^"]*)"', text)
    return (name.group(1), bundle.group(1)) if name and bundle else None


def parse_idle(text):
    """Seconds since the last keyboard/mouse input from `ioreg -c IOHIDSystem -d 1 -r` output, or None."""
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', text)
    return int(m.group(1)) / 1e9 if m else None


def _run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def front_app():
    asn = (_run(["lsappinfo", "front"]) or "").strip()
    out = _run(["lsappinfo", "info", "-only", "name", "-only", "bundleid", asn]) if asn else None
    return parse_front(out) if out else None


def parse_display_held(text):
    """True when some process is preventing display sleep (video playback, calls, presentations), from
    `pmset -g assertions`. Not PreventUserIdleSystemSleep: powerd holds that whenever the display is on."""
    m = re.search(r"^\s*PreventUserIdleDisplaySleep\s+(\d+)", text, re.M)
    return int(m.group(1)) > 0 if m else None


def display_held():
    out = _run(["pmset", "-g", "assertions"])
    return parse_display_held(out) if out else None


def idle_seconds():
    out = _run(["ioreg", "-c", "IOHIDSystem", "-d", "1", "-r"])
    return parse_idle(out) if out else None


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
    focus = Focus(emit_to_log) if TRACK_APPS else None
    uptime = time.monotonic()
    emit_to_log("monitor_start", time.time(), cause="login" if uptime < 600 else "restart", system_uptime_s=round(uptime))
    while True:
        procs = snapshot()
        if procs is not None:
            m.tick(procs, time.time(), time.monotonic())
        if focus:
            try:
                front, idle = front_app(), idle_seconds()
                if front is not None and idle is not None:  # a failed probe skips the tick, never logs "no app"
                    focus.tick(front, idle, time.time(), display_held)
            except Exception as e:  # app tracking must never take the monitor down
                print("focus error: %s" % e, file=sys.stderr)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
