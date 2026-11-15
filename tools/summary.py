#!/usr/bin/env python3
"""Daily summary of ~/chrome-history/YYYY-MM-DD.jsonl.

Usage: summary.py [YYYY-MM-DD ...]     (default: today)
"""
import collections
import contextlib
import datetime
import io
import json
import os
import re
import sys
import threading
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


def gap_data(events, day):
    """Coverage analysis shared by the CLI report and the web UI. None when the day has no events."""
    allpts = sorted(((parse_ts(e.get("ts")), e.get("type"), e) for e in events if parse_ts(e.get("ts"))), key=lambda x: x[0])
    pts = [x for x in allpts if x[1] not in MONITOR_TYPES]
    if not allpts:
        return None
    today = day == datetime.date.today().isoformat()
    now = datetime.datetime.now().astimezone()
    hi = max(now, allpts[-1][0]) if today else allpts[-1][0]
    tl = Timeline(allpts, hi)
    g = {"tl": tl, "today": today, "hi": hi, "markers": False, "skipped": 0, "gaps": [], "tail": None,
         "first_ts": None, "last_ts": None, "span": 0, "lost": 0}

    # Events logged before the first marker predate gap tracking (no heartbeats), so gaps there are meaningless.
    first = next((i for i, (_, k, _e) in enumerate(pts) if k in MARKERS), None)
    if first is None:
        return g
    g["markers"] = True
    g["skipped"] = first
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
    g["gaps"] = gaps
    g["first_ts"], g["last_ts"] = pts[0][0], pts[-1][0]
    g["span"] = (pts[-1][0] - pts[0][0]).total_seconds()
    g["lost"] = sum(x[2] for x in gaps)

    # Trailing state: the last thing in the log is not followed by anything, so it never shows up as a gap.
    t_last, k_last, e_last = pts[-1]
    end_name = "now" if today else "(end of log)"
    tail = (hi - t_last).total_seconds()
    monitor_tail = tl.explain(t_last, hi) if tail > GAP_SECONDS else None
    if monitor_tail:
        g["tail"] = f"  {t_last:%H:%M:%S} -> {end_name}  {fmt(tail):>7}  {monitor_tail}"
    elif k_last == "host_stop":
        g["tail"] = f"  {t_last:%H:%M:%S} -> {end_name}  {stop_label(e_last, None)}; nothing logged after this"
    elif today:
        if tail > GAP_SECONDS:
            g["tail"] = f"  {t_last:%H:%M:%S} -> now  {fmt(tail):>7}  UNEXPECTED (no host_stop and no events since)"
    else:
        g["tail"] = f"  {t_last:%H:%M:%S} -> (end of log)  log ends without host_stop (crash, or it continues in the next day's file)"
    return g


def report_gaps(events, day):
    g = gap_data(events, day)
    if g is None:
        return
    if not g["markers"]:
        print("\nLogging coverage: no heartbeat/host markers in this log (written before gap tracking)")
    else:
        span, lost, gaps = g["span"], g["lost"], g["gaps"]
        print(f"\nLogging coverage: {fmt(span - lost)} of {fmt(span)} between {g['first_ts']:%H:%M:%S} and {g['last_ts']:%H:%M:%S}")
        if g["skipped"]:
            print(f"  ({g['skipped']} earlier events ignored: logged before gap tracking started)")
        if not gaps:
            print(f"  no gaps over {fmt(GAP_SECONDS)}")
        for t1, t2, d, kind in gaps:
            print(f"  {t1:%H:%M:%S} -> {t2:%H:%M:%S}  {fmt(d):>7}  {kind}")
        if g["tail"]:
            print(g["tail"])
    if g["tl"].present:
        report_monitor(g["tl"], g["today"], g["hi"])


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


ENABLE_LABEL = {
    "browser_start": "browser started",
    "extension_reload": "extension reloaded",
    "enabled": "extension enabled",
    "install": "extension installed",
    "worker_restart": "extension worker restarted",
}


def report_logger_state(events, day):
    """When the logger (extension + native host) was enabled and disabled, from host_start/host_stop markers.
    The external monitor's extension_missing/present fill in when Chrome ran without the extension ever connecting."""
    pts = sorted(((parse_ts(e.get("ts")), e.get("type"), e) for e in events if parse_ts(e.get("ts"))), key=lambda x: x[0])
    if not pts:
        return
    host_pts = [(t, k, e) for t, k, e in pts if k in ("host_start", "host_stop")]
    trans = [(t, k == "host_start", e) for t, k, e in host_pts]  # (time, enabled?, event)
    # monitor-only transitions: skip ones a host marker already reports within 60s
    for t, k, e in pts:
        if k in ("extension_missing", "extension_present"):
            enabled = k == "extension_present"
            if not any(abs((t - ht).total_seconds()) <= 60 and (hk == "host_start") == enabled for ht, hk, _ in host_pts):
                trans.append((t, enabled, e))
    trans.sort(key=lambda x: x[0])
    # collapse repeated identical states (e.g. host_start immediately followed by extension_present)
    seq = []
    for t, en, e in trans:
        if not seq or seq[-1][1] != en:
            seq.append((t, en, e))
    if not seq:
        return

    def start_cause(t):
        for tt, k, e in pts:
            if k == "logger_start" and -5 <= (tt - t).total_seconds() <= 30:
                return e.get("cause")
        return None

    today = day == datetime.date.today().isoformat()
    now = datetime.datetime.now().astimezone()
    print("\nLogger (extension) enabled / disabled:")
    if not seq[0][1]:
        print(f"  (enabled before this log started)")
    for i, (t, en, e) in enumerate(seq):
        prev_t = seq[i - 1][0] if i else None
        dur = f"  (was {'off' if en else 'on'} {fmt((t - prev_t).total_seconds())})" if prev_t else ""
        if en:
            why = ENABLE_LABEL.get(start_cause(t) or e.get("cause"), "")
            print(f"  {t:%H:%M:%S}  enabled   {why or '(reason not recorded)'}{dur}")
        else:
            nxt = next((seq[j] for j in range(i + 1, len(seq)) if seq[j][1]), None)
            nc = start_cause(nxt[0]) if nxt else None
            why = stop_label(e, nc) if e.get("type") == "host_stop" else "Chrome running, logger extension not connected"
            print(f"  {t:%H:%M:%S}  DISABLED  {why}{dur}")
    t_last, en_last, _e = seq[-1]
    if today:
        print(f"  now: {'enabled' if en_last else 'DISABLED'} for {fmt((now - t_last).total_seconds())}")


def report_incognito(events, day):
    """Periods when the extension was NOT allowed in incognito (so private windows were not logged).
    The flag is sampled by the extension in every heartbeat (30s) and logger_start, so edges are +/-30s."""
    samples = sorted(((t, bool(e.get("incognito_allowed"))) for t, k, e in
                      ((parse_ts(e.get("ts")), e.get("type"), e) for e in events)
                      if t and k in ("heartbeat", "logger_start") and e.get("incognito_allowed") is not None),
                     key=lambda x: x[0])
    periods, start = [], None
    for t, allowed in samples:
        if not allowed and start is None:
            start = t
        elif allowed and start is not None:
            periods.append((start, t, False))
            start = None
    if start is not None:  # still disallowed at the last sample
        today = day == datetime.date.today().isoformat()
        periods.append((start, datetime.datetime.now().astimezone() if today else samples[-1][0], True))
    if not periods:
        return
    total = sum((e - s).total_seconds() for s, e, _ in periods)
    print(f"\nIncognito logging DISALLOWED: {len(periods)} period(s), {fmt(total)} in total (private windows not logged):")
    for s, e, ongoing in periods:
        end = "now" if ongoing and day == datetime.date.today().isoformat() else f"{e:%H:%M:%S}"
        tail = "  (still disallowed)" if ongoing else ""
        began = "  (already disallowed when this log began)" if s == samples[0][0] and not samples[0][1] else ""
        print(f"  {s:%H:%M:%S} -> {end}  {fmt((e - s).total_seconds()):>7}{began}{tail}")


def aggregate(events):
    """Per-day totals shared by the CLI report and the web UI (serve.py)."""
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
    return {"dom_active": dom_active, "dom_total": dom_total, "visits": visits, "clicks": clicks,
            "searches": searches, "yt": yt, "discord": discord, "posts": posts}


def summarize(day):
    events = load(day)
    print(f"\n=== {day}: {len(events)} events ===")
    if not events:
        return
    agg = aggregate(events)
    dom_active, dom_total, visits, clicks = agg["dom_active"], agg["dom_total"], agg["visits"], agg["clicks"]
    searches, yt, discord, posts = agg["searches"], agg["yt"], agg["discord"], agg["posts"]

    report_gaps(events, day)
    report_logger_state(events, day)
    report_incognito(events, day)
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
            print(f"  all watched, in order (start -> stop):")
            for e in sorted(rows, key=lambda x: (parse_ts(x.get("start") or x.get("ts")) or datetime.datetime.min.astimezone())):
                st = parse_ts(e.get("start") or e.get("ts"))
                en = parse_ts(e.get("end") or e.get("ts"))
                span = f"{st:%H:%M:%S} -> {en:%H:%M:%S}" if st and en else "?"
                length = fmt(e.get("length_s") or 0) if e.get("length_s") else "?"
                notes = [e.get("source") or "?"]
                if e.get("finished"):
                    notes.append("finished")
                if e.get("loops"):
                    notes.append(f"{e['loops']} loop{'s' if e['loops'] != 1 else ''}")
                if e.get("partial"):
                    notes.append("partial: tab closed/crashed")
                title = (e.get("title") or "(no title)").replace("\n", " ")
                print(f"    {span}  {fmt(e.get('watched_s', 0)):>7} of {length:>7}  {title!r} [{e.get('channel') or '?'}]  ({', '.join(notes)})")

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


# ---------- data API for the web UI (tools/serve.py) ----------
DAY_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
_print_lock = threading.Lock()  # health() redirects the process-wide stdout


def list_days():
    try:
        names = os.listdir(LOG_DIR)
    except OSError:
        return []
    return sorted(m.group(1) for m in map(DAY_FILE_RE.match, names) if m)


def hourly_active(events):
    """Active seconds per hour of day (a visit's active time is spread over the hours it spanned)."""
    hours = [0.0] * 24
    for e in events:
        if e.get("type") != "visit":
            continue
        active = e.get("dur_active_s") or 0
        s, t = parse_ts(e.get("start")), parse_ts(e.get("end"))
        if not active or not s:
            continue
        if not t or t <= s:
            hours[s.hour] += active
            continue
        total, cur = (t - s).total_seconds(), s
        while cur < t:
            nxt = min(t, cur.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1))
            hours[cur.hour] += active * (nxt - cur).total_seconds() / total
            cur = nxt
    return [round(h, 1) for h in hours]


def _yt_kind(rows):
    chan = collections.Counter()
    for e in rows:
        chan[e.get("channel") or "?"] += e.get("watched_s", 0)
    src = collections.Counter(e.get("source") or "?" for e in rows)
    far = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    sessions = []
    for e in sorted(rows, key=lambda x: parse_ts(x.get("start") or x.get("ts")) or far):
        sessions.append({
            "start": e.get("start") or e.get("ts"), "end": e.get("end") or e.get("ts"),
            "title": (e.get("title") or "").replace("\n", " "), "channel": e.get("channel") or "?",
            "watched_s": e.get("watched_s", 0), "length_s": e.get("length_s"), "source": e.get("source") or "?",
            "finished": bool(e.get("finished")), "loops": e.get("loops", 0), "partial": bool(e.get("partial")),
        })
    return {
        "count": len(rows), "watched_s": round(sum(e.get("watched_s", 0) for e in rows), 1),
        "finished": sum(1 for e in rows if e.get("finished")), "loops": sum(e.get("loops", 0) for e in rows),
        "sources": [{"name": k, "n": n} for k, n in src.most_common()],
        "channels": [{"name": k, "s": round(v, 1)} for k, v in chan.most_common(8)],
        "sessions": sessions,
    }


def day_data(day, events=None):
    """Everything the UI shows for one day, JSON-serializable (numbers come from aggregate(), like the CLI)."""
    events = load(day) if events is None else events
    a = aggregate(events)
    yt = list(a["yt"].values())
    return {
        "day": day, "events": len(events), "visits": a["visits"], "clicks": a["clicks"],
        "active_s": round(sum(a["dom_active"].values()), 1), "open_s": round(sum(a["dom_total"].values()), 1),
        "top_sites": [{"host": h, "active_s": round(v, 1), "open_s": round(a["dom_total"][h], 1)}
                      for h, v in a["dom_active"].most_common(12)],
        "hourly": hourly_active(events),
        "searches": [{"engine": en, "query": q} for en, q in a["searches"]],
        "youtube": {k: _yt_kind([e for e in yt if e.get("kind") == k]) for k in ("video", "short")},
        "discord": {
            "channels": [{"guild": g, "channel": c, "s": round(v[0], 1), "msgs": v[1], "posts": a["posts"].get((g, c), 0)}
                         for (g, c), v in sorted(a["discord"].items(), key=lambda kv: -kv[1][0])],
            "total_posts": sum(a["posts"].values()),
        },
    }


def health(day, events=None):
    """Logging coverage for one day: structured gaps plus the CLI's free-text sections as 'details'."""
    events = load(day) if events is None else events
    g = gap_data(events, day)
    out = {"markers": False, "gaps": [], "tail": None, "details": ""}
    if g is None:
        return out
    iso = lambda t: t.isoformat(timespec="seconds") if t else None
    out.update(markers=g["markers"], skipped=g["skipped"], first=iso(g["first_ts"]), last=iso(g["last_ts"]),
               span_s=g["span"], lost_s=g["lost"], tail=(g["tail"] or "").strip() or None,
               gaps=[{"from": iso(t1), "to": iso(t2), "secs": d, "label": label} for t1, t2, d, label in g["gaps"]])
    buf = io.StringIO()
    with _print_lock, contextlib.redirect_stdout(buf):
        report_logger_state(events, day)
        report_incognito(events, day)
        if g["tl"].present:
            report_monitor(g["tl"], g["today"], g["hi"])
    out["details"] = buf.getvalue().strip("\n")
    return out


_trend_cache = {}  # day -> ((mtime_ns, size), row)


def trend():
    """One light row per available day; a day is only re-read when its file changed."""
    rows = []
    for day in list_days():
        path = os.path.join(LOG_DIR, day + ".jsonl")
        try:
            st = os.stat(path)
        except OSError:
            continue
        key = (st.st_mtime_ns, st.st_size)
        hit = _trend_cache.get(day)
        if not hit or hit[0] != key:
            events = load(day)
            a = aggregate(events)
            yt = list(a["yt"].values())
            row = {
                "day": day, "events": len(events), "visits": a["visits"],
                "active_s": round(sum(a["dom_active"].values()), 1),
                "yt_video_s": round(sum(e.get("watched_s", 0) for e in yt if e.get("kind") == "video"), 1),
                "yt_short_s": round(sum(e.get("watched_s", 0) for e in yt if e.get("kind") == "short"), 1),
            }
            hit = _trend_cache[day] = (key, row)
        rows.append(hit[1])
    return rows


if __name__ == "__main__":
    try:
        import verify  # same directory: hash-chain + append-only check of the whole log directory
        print("Log integrity:", verify.verify(LOG_DIR).status())
    except Exception as e:  # never let the integrity check stop the summary
        print("Log integrity: could not check (%s)" % e)
    for d in (sys.argv[1:] or [datetime.date.today().isoformat()]):
        summarize(d)
