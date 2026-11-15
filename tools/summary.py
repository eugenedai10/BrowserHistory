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

    print(f"visits: {visits}   link clicks: {clicks}   searches: {len(searches)}")
    print("\nTop sites by active time:")
    for host, a in dom_active.most_common(10):
        print(f"  {fmt(a):>8} active / {fmt(dom_total[host]):>8} open   {host}")

    if yt:
        vids = [e for e in yt.values() if e.get("kind") == "video"]
        shorts = [e for e in yt.values() if e.get("kind") == "short"]
        print(f"\nYouTube: {len(vids)} videos ({fmt(sum(e.get('watched_s', 0) for e in vids))}), "
              f"{len(shorts)} shorts ({fmt(sum(e.get('watched_s', 0) for e in shorts))})")
        chan = collections.Counter()
        for e in yt.values():
            chan[e.get("channel") or "?"] += e.get("watched_s", 0)
        for c, s in chan.most_common(5):
            print(f"  {fmt(s):>8}  {c}")
        print("  longest watched:")
        for e in sorted(yt.values(), key=lambda x: -x.get("watched_s", 0))[:5]:
            print(f"    {fmt(e.get('watched_s', 0)):>7} / {fmt(e.get('length_s') or 0):>7}  {e.get('title')!r} ({e.get('source')})")

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
