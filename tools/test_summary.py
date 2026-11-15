#!/usr/bin/env python3
"""Tests for the summary data layer (summary.py) and the local web server (serve.py).

Run: python3 tools/test_summary.py
"""
import contextlib
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DAY = "2026-03-01"
TZ = "-08:00"


def ts(hms):
    return f"{DAY}T{hms}.000{TZ}"


EVENTS = [
    {"ts": ts("10:00:00"), "type": "heartbeat", "incognito_allowed": True},
    {"ts": ts("10:00:30"), "type": "heartbeat", "incognito_allowed": True},
    # 270s of silence with no host_stop: an unexplained gap
    {"ts": ts("10:05:00"), "type": "heartbeat", "incognito_allowed": True},
    {"ts": ts("11:00:00"), "type": "visit", "url": "https://a.example/x", "start": ts("10:00:00"), "end": ts("11:00:00"),
     "dur_active_s": 600, "dur_total_s": 900},
    {"ts": ts("10:31:00"), "type": "visit", "url": "https://b.example/", "start": ts("10:30:00"), "end": ts("10:31:00"),
     "dur_active_s": 60, "dur_total_s": 60},
    {"ts": ts("10:10:00"), "type": "click"},
    {"ts": ts("10:11:00"), "type": "search", "engine": "google", "query": "<b>hello</b>"},
    # a partial snapshot followed by the final record of the same session: must count once
    {"ts": ts("10:20:00"), "type": "yt_session", "sid": "s1", "kind": "video", "title": "T1", "channel": "C1",
     "watched_s": 5, "length_s": 100, "start": ts("10:19:00"), "end": ts("10:20:00"), "source": "home", "partial": True},
    {"ts": ts("10:21:00"), "type": "yt_session", "sid": "s1", "kind": "video", "title": "T1", "channel": "C1",
     "watched_s": 50, "length_s": 100, "start": ts("10:19:00"), "end": ts("10:21:00"), "source": "home", "finished": True},
    {"ts": ts("10:22:00"), "type": "yt_session", "sid": "s2", "kind": "short", "title": "S", "channel": "C2",
     "watched_s": 20, "length_s": 15, "start": ts("10:21:30"), "end": ts("10:22:00"), "source": "shorts_swipe", "loops": 2},
    {"ts": ts("10:40:00"), "type": "discord_channel", "guild_name": "G", "channel_name": "general", "dur_active_s": 30, "messages_seen": 4},
    {"ts": ts("10:41:00"), "type": "discord_post", "guild_name": "G", "channel_name": "general"},
]

_tmp = tempfile.TemporaryDirectory()
os.environ["CHROME_HISTORY_DIR"] = _tmp.name  # must be set before importing summary / serve / verify
import summary  # noqa: E402
import serve  # noqa: E402


def write_day(day, events):
    with open(os.path.join(_tmp.name, day + ".jsonl"), "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


class DataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        write_day(DAY, EVENTS)
        write_day("2026-03-02", [{"ts": "2026-03-02T09:00:00.000" + TZ, "type": "heartbeat"}])
        open(os.path.join(_tmp.name, "notes.txt"), "w").close()  # not a day file

    def test_list_days_ignores_other_files(self):
        self.assertEqual(summary.list_days(), [DAY, "2026-03-02"])

    def test_day_data_totals(self):
        d = summary.day_data(DAY)
        self.assertEqual((d["visits"], d["clicks"]), (2, 1))
        self.assertEqual(d["active_s"], 660)
        self.assertEqual(d["top_sites"][0], {"host": "a.example", "active_s": 600, "open_s": 900})
        self.assertEqual(d["searches"], [{"engine": "google", "query": "<b>hello</b>"}])
        json.dumps(d)  # must be serializable

    def test_youtube_partial_is_deduped_by_session(self):
        yt = summary.day_data(DAY)["youtube"]
        self.assertEqual(yt["video"]["count"], 1)
        self.assertEqual(yt["video"]["watched_s"], 50)
        self.assertEqual(yt["video"]["finished"], 1)
        self.assertFalse(yt["video"]["sessions"][0]["partial"])
        self.assertEqual((yt["short"]["count"], yt["short"]["loops"]), (1, 2))
        self.assertEqual(yt["video"]["channels"], [{"name": "C1", "s": 50}])

    def test_discord(self):
        dc = summary.day_data(DAY)["discord"]
        self.assertEqual(dc["total_posts"], 1)
        self.assertEqual(dc["channels"][0]["posts"], 1)

    def test_hourly_active_spreads_visits(self):
        hours = summary.day_data(DAY)["hourly"]
        self.assertEqual(len(hours), 24)
        self.assertAlmostEqual(hours[10], 660, places=0)
        self.assertEqual(sum(hours) - hours[10], 0)

    def test_health_reports_the_gap(self):
        hl = summary.health(DAY)
        self.assertTrue(hl["markers"])
        # every logged event counts as activity, so the sparse fixture has several gaps; find the planted one
        planted = [g for g in hl["gaps"] if abs(g["secs"] - 270) < 1 and g["from"].endswith("10:00:30" + TZ)]
        self.assertEqual(len(planted), 1)
        self.assertTrue(planted[0]["label"].startswith("UNEXPECTED"))
        self.assertGreater(hl["lost_s"], 270)
        json.dumps(hl)

    def test_empty_day(self):
        d = summary.day_data("2026-01-01")
        self.assertEqual(d["events"], 0)
        self.assertEqual(d["youtube"]["video"]["count"], 0)
        self.assertFalse(summary.health("2026-01-01")["markers"])

    def test_cli_report_still_prints_the_same_facts(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            summary.summarize(DAY)
        out = buf.getvalue()
        self.assertIn("visits: 2   link clicks: 1   searches: 1", out)
        self.assertIn("YouTube videos: 1 watched, 50s total, 1 finished", out)
        self.assertIn("Logging coverage:", out)
        self.assertIn("UNEXPECTED", out)

    def test_trend_only_rereads_changed_days(self):
        summary._trend_cache.clear()
        calls = []
        real = summary.load
        summary.load = lambda day: (calls.append(day), real(day))[1]
        try:
            rows = summary.trend()
            self.assertEqual([r["day"] for r in rows], [DAY, "2026-03-02"])
            self.assertEqual(rows[0]["yt_video_s"], 50)
            self.assertEqual(len(calls), 2)
            summary.trend()
            self.assertEqual(len(calls), 2, "unchanged files must come from the cache")
            with open(os.path.join(_tmp.name, "2026-03-02.jsonl"), "a") as f:
                f.write(json.dumps({"ts": "2026-03-02T09:01:00.000" + TZ, "type": "click"}) + "\n")
            summary.trend()
            self.assertEqual(calls[2:], ["2026-03-02"], "only the changed day is re-read")
        finally:
            summary.load = real


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        write_day(DAY, EVENTS)
        cls.srv = serve.make_server(0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def req(self, path, method="GET", host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, headers={"Host": host or f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    def test_page(self):
        code, body = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Browsing summary", body)

    def test_days_api(self):
        code, body = self.req("/api/days")
        j = json.loads(body)
        self.assertEqual(code, 200)
        self.assertIn(DAY, j["days"])
        self.assertIn("status", j["integrity"])

    def test_day_api(self):
        code, body = self.req("/api/day/" + DAY)
        j = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(j["data"]["visits"], 2)
        self.assertTrue(j["health"]["markers"])
        self.assertGreaterEqual(len(j["health"]["gaps"]), 1)

    def test_unknown_day_is_empty_not_an_error(self):
        code, body = self.req("/api/day/2001-01-01")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["data"]["events"], 0)

    def test_bad_dates_are_rejected(self):
        for p in ("/api/day/nope", "/api/day/..%2F..%2Fetc%2Fpasswd", "/api/day/2026-03-01.jsonl", "/api/day/"):
            self.assertEqual(self.req(p)[0], 400, p)

    def test_foreign_host_header_is_refused(self):
        for host in ("evil.com", f"evil.com:{self.port}", "127.0.0.1", "localhost"):
            self.assertEqual(self.req("/api/days", host=host)[0], 403, host)
        self.assertEqual(self.req("/api/days", host=f"localhost:{self.port}")[0], 200)

    def test_read_only(self):
        for m in ("POST", "PUT", "DELETE"):
            self.assertEqual(self.req("/api/days", method=m)[0], 405, m)

    def test_no_cors_headers(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/api/days", headers={"Host": f"127.0.0.1:{self.port}", "Origin": "http://evil.com"})
        r = c.getresponse()
        r.read()
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))
        c.close()

    def test_unknown_route(self):
        self.assertEqual(self.req("/nope")[0], 404)


if __name__ == "__main__":
    unittest.main()
