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


def report_gaps(events):
    pts = sorted(((parse_ts(e.get("ts")), e.get("type")) for e in events if parse_ts(e.get("ts"))), key=lambda x: x[0])
    if len(pts) < 2:
        return
    span = (pts[-1][0] - pts[0][0]).total_seconds()
    gaps = []
    for (t1, k1), (t2, k2) in zip(pts, pts[1:]):
        d = (t2 - t1).total_seconds()
        if d > GAP_SECONDS:
            kind = "browser closed" if k1 == "host_stop" else "UNEXPECTED (no host_stop: crash, sleep, or extension stopped)"
            gaps.append((t1, t2, d, kind))
    lost = sum(g[2] for g in gaps)
    print(f"\nLogging coverage: {fmt(span - lost)} of {fmt(span)} between first and last event")
    if not gaps:
        print(f"  no gaps over {fmt(GAP_SECONDS)}")
    for t1, t2, d, kind in gaps:
        print(f"  {t1:%H:%M:%S} -> {t2:%H:%M:%S}  {fmt(d):>7}  {kind}")


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

    report_gaps(events)
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
    for d in (sys.argv[1:] or [datetime.date.today().isoformat()]):
        summarize(d)
