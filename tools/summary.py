#!/usr/bin/env python3
"""Daily summary of ~/chrome-history/YYYY-MM-DD.jsonl.

Usage: summary.py [YYYY-MM-DD ...]     (default: today)
"""
import collections
import datetime
import json
import os
import sys
from urllib.parse import urlparse

LOG_DIR = os.environ.get("CHROME_HISTORY_DIR") or os.path.expanduser("~/chrome-history")


def fmt(sec):
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{s:02d}s" if m else f"{s}s")


def load(day):
    path = os.path.join(LOG_DIR, day + ".jsonl")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


GAP_SECONDS = int(os.environ.get("GAP_SECONDS", "60"))  # silence longer than this counts as a gap (heartbeat is every 30s)


def parse_ts(ts):
    try:
        return datetime.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


MARKERS = ("host_start", "host_stop", "logger_start", "heartbeat")

# what the extension reports as the reason for its next start
START_CAUSE = {
    "browser_start": "browser closed",
    "extension_reload": "extension reloaded",
    "install": "extension (re)installed",
    "enabled": "extension disabled, then re-enabled",
    "worker_restart": "extension worker restarted",
}


def stop_label(stop, next_cause):
    """Explain a host_stop. Prefer the host's own diagnosis; refine it with how the extension next started."""
    cause = (stop or {}).get("cause")
    if next_cause == "browser_start":
        return "browser closed"
    if cause == "browser_exit":
        return "browser closed (quit or crash)"
    if cause == "extension_stopped" or (cause is None and next_cause in START_CAUSE):
        detail = START_CAUSE.get(next_cause)
        return "extension stopped, browser still running" + (f" ({detail})" if detail else " (disabled, reloaded or removed)")
    return "browser closed or extension stopped (cause not recorded)"


# Written by monitor/monitor.py (outside Chrome). They explain gaps; they must NOT count as logging activity,
# otherwise the monitor's own heartbeats would hide exactly the periods the extension was silent.
MONITOR_TYPES = ("monitor_start", "monitor_heartbeat", "browser_start", "browser_stop",
                 "extension_missing", "extension_present", "system_sleep", "system_wake")
MONITOR_GAP_SECONDS = 150  # monitor heartbeats every 60s


def overlap(t1, t2, intervals):
    return sum(max(0.0, (min(t2, e) - max(t1, s)).total_seconds()) for s, e in intervals)


def pair_intervals(mev, open_kinds, close_kinds, lo, hi):
    """[(start, end)] from monitor events; a close with no open starts at lo, an unclosed open ends at hi."""
    out, opened = [], None
    for t, k, _e in mev:
        if k in open_kinds and opened is None:
            opened = t
        elif k in close_kinds:
            if opened is not None:
                out.append((opened, t))
            elif k == close_kinds[0]:
                out.append((lo, t))
            opened = None
    if opened is not None:
        out.append((opened, hi))
    return out


def intersect(a, b):
    out = []
    for s1, e1 in a:
        for s2, e2 in b:
            lo, hi = max(s1, s2), min(e1, e2)
            if hi > lo:
                out.append((lo, hi))
    return out


class Timeline:
    """What the external monitor saw: when Chrome ran, when the Mac slept, when the extension was not connected."""

    def __init__(self, pts, hi):
        self.mev = [x for x in pts if x[1] in MONITOR_TYPES]
        self.present = bool(self.mev)
        if not self.present:
            return
        self.lo = pts[0][0]
        self.hi = hi
        beats = [e for _t, k, e in self.mev if k == "monitor_heartbeat"]
        if any(k in ("browser_start", "browser_stop") for _t, k, _e in self.mev):
            self.running = pair_intervals(self.mev, ("browser_start",), ("browser_stop",), self.lo, hi)
        else:  # no transitions today: rely on the heartbeats' chrome_running flag
            self.running = [(self.lo, hi)] if any(e.get("chrome_running") for e in beats) else []
        self.asleep = pair_intervals(self.mev, ("system_sleep",), ("system_wake",), self.lo, hi)
        # The monitor only knows about time it was actually running: from its first marker on, minus any
        # silence in its own heartbeats (sleep is expected silence). Outside that, it must not explain anything.
        self.down = []
        for (t1, k1, _e1), (t2, _k2, _e2) in zip(self.mev, self.mev[1:]):
            if (t2 - t1).total_seconds() > MONITOR_GAP_SECONDS and k1 != "system_sleep":
                self.down.append((t1, t2))
        self.known, cursor = [], self.mev[0][0]
        for t1, t2 in self.down:
            if t1 > cursor:
                self.known.append((cursor, t1))
            cursor = t2
        if hi > cursor:
            self.known.append((cursor, hi))
        self.missing = pair_intervals(self.mev, ("extension_missing",), ("extension_present", "browser_stop"), self.lo, hi)

    def explain(self, t1, t2):
        """Label for the silence t1..t2 when one cause covers at least half of it, else None."""
        if not self.present:
            return None
        d = (t2 - t1).total_seconds()
        if d <= 0:
            return None
        known = self.known
        asleep = overlap(t1, t2, self.asleep)
        closed = overlap(t1, t2, known) - overlap(t1, t2, intersect(self.running, known))
        missing = overlap(t1, t2, intersect(self.missing, known))
        best = max((asleep, "machine asleep"), (closed, "browser closed (no Chrome process)"),
                   (missing, "Chrome running, logger extension not connected (disabled or removed)"))
        return best[1] if best[0] >= d * 0.5 else None


def report_gaps(events, day):
    allpts = sorted(((parse_ts(e.get("ts")), e.get("type"), e) for e in events if parse_ts(e.get("ts"))), key=lambda x: x[0])
    pts = [x for x in allpts if x[1] not in MONITOR_TYPES]
    if not allpts:
        return
    today = day == datetime.date.today().isoformat()
    now = datetime.datetime.now().astimezone()
    hi = max(now, allpts[-1][0]) if today else allpts[-1][0]
    tl = Timeline(allpts, hi)

    # Events logged before the first marker predate gap tracking (no heartbeats), so gaps there are meaningless.
    first = next((i for i, (_, k, _e) in enumerate(pts) if k in MARKERS), None)
    if first is None:
        print("\nLogging coverage: no heartbeat/host markers in this log (written before gap tracking)")
        if tl.present:
            report_monitor(tl, today, hi)
        return
    skipped = first
    pts = pts[first:]

    def next_start_cause(i):
        """cause of the first logger_start shortly after pts[i] (the extension's own reason for restarting)"""
        t0 = pts[i][0]
        for t, k, e in pts[i:]:
            if (t - t0).total_seconds() > 30:
                break
            if k == "logger_start":
                return e.get("cause")
        return None

    def label(t1, k1, e1, t2, nc):
        monitor_says = tl.explain(t1, t2)
        if monitor_says:
            return monitor_says
        if k1 == "host_stop":
            return stop_label(e1, nc)
        if nc == "browser_start":
            return "browser closed uncleanly (crash, kill, power loss or sleep: no host_stop)"
        return "UNEXPECTED (no host_stop: machine sleep, or extension/host died)"

    gaps = []
    for i in range(len(pts) - 1):
        (t1, k1, e1), (t2, _k2, _e2) = pts[i], pts[i + 1]
        d = (t2 - t1).total_seconds()
        if d > GAP_SECONDS:
            gaps.append((t1, t2, d, label(t1, k1, e1, t2, next_start_cause(i + 1))))
    span = (pts[-1][0] - pts[0][0]).total_seconds()
    lost = sum(g[2] for g in gaps)
    print(f"\nLogging coverage: {fmt(span - lost)} of {fmt(span)} between {pts[0][0]:%H:%M:%S} and {pts[-1][0]:%H:%M:%S}")
    if skipped:
        print(f"  ({skipped} earlier events ignored: logged before gap tracking started)")
    if not gaps:
        print(f"  no gaps over {fmt(GAP_SECONDS)}")
    for t1, t2, d, kind in gaps:
        print(f"  {t1:%H:%M:%S} -> {t2:%H:%M:%S}  {fmt(d):>7}  {kind}")

    # Trailing state: the last thing in the log is not followed by anything, so it never shows up as a gap.
    t_last, k_last, e_last = pts[-1]
    end_name = "now" if today else "(end of log)"
    tail = (hi - t_last).total_seconds()
    monitor_tail = tl.explain(t_last, hi) if tail > GAP_SECONDS else None
    if monitor_tail:
        print(f"  {t_last:%H:%M:%S} -> {end_name}  {fmt(tail):>7}  {monitor_tail}")
    elif k_last == "host_stop":
        print(f"  {t_last:%H:%M:%S} -> {end_name}  {stop_label(e_last, None)}; nothing logged after this")
    elif today:
        if tail > GAP_SECONDS:
            print(f"  {t_last:%H:%M:%S} -> now  {fmt(tail):>7}  UNEXPECTED (no host_stop and no events since)")
    else:
        print(f"  {t_last:%H:%M:%S} -> (end of log)  log ends without host_stop (crash, or it continues in the next day's file)")

    if tl.present:
        report_monitor(tl, today, hi)


def report_monitor(tl, today, hi):
    """Browser sessions and 'open but not logged' time, from the external monitor."""
    print("\nBrowser sessions (external monitor):")
    if not tl.running:
        print("  Chrome was not running")
    for s, e in tl.running:
        end = "now" if today and e == hi else f"{e:%H:%M:%S}"
        print(f"  {s:%H:%M:%S} -> {end}  {fmt((e - s).total_seconds()):>7}")
    missing = overlap(tl.lo, hi, tl.missing)
    asleep = overlap(tl.lo, hi, tl.asleep)
    print(f"  Chrome open but logger extension not connected: {fmt(missing)}")
    if asleep:
        print(f"  Mac asleep: {fmt(asleep)}")
    for t1, t2 in tl.down:
        print(f"  monitor not running {t1:%H:%M:%S} -> {t2:%H:%M:%S} ({fmt((t2 - t1).total_seconds())}): "
              "logger state unknown for that period")


def summarize(day):
    events = load(day)
    print(f"\n=== {day}: {len(events)} events ===")
    if not events:
        return
    dom_active = collections.Counter()
    dom_total = collections.Counter()
    visits = 0
    clicks = 0
    searches = []
    yt = {}
    discord = collections.defaultdict(lambda: [0.0, 0])
    posts = collections.Counter()
    for e in events:
        t = e.get("type")
        if t == "visit":
            visits += 1
            host = urlparse(e.get("url", "")).hostname or "?"
            dom_active[host] += e.get("dur_active_s", 0)
            dom_total[host] += e.get("dur_total_s", 0)
        elif t == "click":
            clicks += 1
        elif t == "search":
            searches.append((e.get("engine"), e.get("query")))
        elif t == "yt_session":
            # keep the fullest record per session id (partial snapshots can precede the final one)
            key = e.get("sid") or id(e)
            if key not in yt or not e.get("partial"):
                yt[key] = e
        elif t == "discord_channel":
            k = (e.get("guild_name") or e.get("guild_id"), e.get("channel_name") or e.get("channel_id"))
            discord[k][0] += e.get("dur_active_s", 0)
            discord[k][1] += e.get("messages_seen", 0)
        elif t == "discord_post":
            posts[(e.get("guild_name") or e.get("guild_id"), e.get("channel_name") or e.get("channel_id"))] += 1

    report_gaps(events, day)
    print(f"\nvisits: {visits}   link clicks: {clicks}   searches: {len(searches)}")
    print("\nTop sites by active time:")
    for host, a in dom_active.most_common(10):
        print(f"  {fmt(a):>8} active / {fmt(dom_total[host]):>8} open   {host}")

    if yt:
        for kind, label in (("video", "YouTube videos"), ("short", "YouTube Shorts")):
            rows = [e for e in yt.values() if e.get("kind") == kind]
            if not rows:
                print(f"\n{label}: none")
                continue
            watched = sum(e.get("watched_s", 0) for e in rows)
            finished = sum(1 for e in rows if e.get("finished"))
            line = f"\n{label}: {len(rows)} watched, {fmt(watched)} total, {finished} finished"
            if kind == "short":
                line += f", {sum(e.get('loops', 0) for e in rows)} loops"
            print(line)
            src = collections.Counter(e.get("source") or "?" for e in rows)
            print("  found via: " + ", ".join(f"{k} {n}" for k, n in src.most_common()))
            chan = collections.Counter()
            for e in rows:
                chan[e.get("channel") or "?"] += e.get("watched_s", 0)
            print("  top channels:")
            for c, sec in chan.most_common(5):
                print(f"    {fmt(sec):>8}  {c}")
            print("  longest watched:")
            for e in sorted(rows, key=lambda x: -x.get("watched_s", 0))[:5]:
                length = fmt(e.get("length_s") or 0)
                print(f"    {fmt(e.get('watched_s', 0)):>7} / {length:>7}  {e.get('title')!r} [{e.get('channel') or '?'}] ({e.get('source')})")

    if discord:
        print("\nDiscord channels read:")
        for (g, c), (s, n) in sorted(discord.items(), key=lambda kv: -kv[1][0])[:10]:
            print(f"  {fmt(s):>8}  {n:>4} msgs seen  {g} / {c}   posted: {posts.get((g, c), 0)}")
        if posts:
            print(f"  total posts: {sum(posts.values())}")

    if searches:
        print("\nSearches:")
        for eng, q in searches[:15]:
            print(f"  [{eng}] {q}")


if __name__ == "__main__":
    try:
        import verify  # same directory: hash-chain + append-only check of the whole log directory
        print("Log integrity:", verify.verify(LOG_DIR).status())
    except Exception as e:  # never let the integrity check stop the summary
        print("Log integrity: could not check (%s)" % e)
    for d in (sys.argv[1:] or [datetime.date.today().isoformat()]):
        summarize(d)
