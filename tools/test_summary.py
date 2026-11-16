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
    # an audible YouTube embed on another site: its own category, never mixed into videos or Shorts
    {"ts": ts("10:50:00"), "type": "yt_session", "sid": "e1", "kind": "embed", "title": "Embed T", "channel": "C3",
     "watched_s": 40, "length_s": 300, "start": ts("10:49:00"), "end": ts("10:50:00"), "source": "embed",
     "page_url": "https://news.example/story"},
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

    def test_embeds_are_their_own_category(self):
        yt = summary.day_data(DAY)["youtube"]
        self.assertEqual((yt["embed"]["count"], yt["embed"]["watched_s"]), (1, 40))
        self.assertEqual(yt["embed"]["sessions"][0]["host"], "news.example")
        self.assertIsNone(yt["video"]["sessions"][0]["host"])
        self.assertEqual((yt["video"]["count"], yt["video"]["watched_s"]), (1, 50), "embeds must not leak into videos")
        self.assertEqual(yt["short"]["count"], 1)
        row = next(r for r in summary.trend() if r["day"] == DAY)
        self.assertEqual((row["yt_video_s"], row["yt_short_s"], row["yt_embed_s"]), (50, 20, 40))

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
        self.assertIn("YouTube embedded in other sites (audible only): 1 watched, 40s total", out)
        self.assertIn("on news.example", out)
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


class AppTimeTests(unittest.TestCase):
    D = "2026-03-05"  # a past day, so "now" never clips the last span

    def t(self, hms):
        return f"{self.D}T{hms}.000{TZ}"

    def focus(self, hms, app, bundle=None, **kw):
        return {"ts": self.t(hms), "type": "app_focus", "app": app, "bundle": bundle or ("com.x." + str(app)), **kw}

    def events(self):
        return [
            {"ts": self.t("09:00:00"), "type": "monitor_heartbeat"},
            self.focus("09:00:00", "Code"),
            self.focus("09:10:00", "Safari"),
            self.focus("09:20:00", None, None, reason="idle"),
            self.focus("09:30:00", "Code"),
            {"ts": self.t("09:40:00"), "type": "system_sleep"},     # the hour we did not see is not credited
            {"ts": self.t("09:50:00"), "type": "monitor_start"},
            self.focus("09:50:00", "Safari"),
            {"ts": self.t("10:00:00"), "type": "monitor_heartbeat"},  # last sign of life; the monitor then died
        ]

    def test_spans_idle_sleep_restart_and_dead_monitor(self):
        a = summary.app_time(self.events(), self.D)
        got = {r["app"]: r["s"] for r in a["list"]}
        self.assertEqual(got, {"Safari": 600 + 600 + 60, "Code": 600 + 600})
        self.assertEqual(a["away_s"], 600)
        self.assertEqual(a["total_s"], 2460)
        self.assertEqual(a["list"][0]["app"], "Safari")             # most time first

    def test_same_bundle_with_new_display_name_is_one_row(self):
        ev = [self.focus("09:00:00", "Old Name", "com.x.app"), self.focus("09:01:00", "New Name", "com.x.app"),
              {"ts": self.t("09:02:00"), "type": "monitor_heartbeat"}]
        rows = summary.app_time(ev, self.D)["list"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["app"], rows[0]["s"]), ("New Name", 180))

    def test_clipped_at_midnight(self):
        ev = [self.focus("23:00:00", "Code"), {"ts": f"2026-03-06T01:00:00.000{TZ}", "type": "monitor_heartbeat"}]
        self.assertEqual(summary.app_time(ev, self.D)["list"][0]["s"], 3600)

    def test_no_app_events(self):
        self.assertEqual(summary.app_time([{"ts": self.t("09:00:00"), "type": "heartbeat"}], self.D),
                         {"total_s": 0.0, "away_s": 0.0, "list": []})

    def test_day_data_and_cli_include_apps(self):
        write_day(self.D, self.events())
        self.addCleanup(os.remove, os.path.join(_tmp.name, self.D + ".jsonl"))  # shared dir: other tests list days
        d = summary.day_data(self.D)
        self.assertEqual(d["apps"]["total_s"], 2460)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            summary.summarize(self.D)
        self.assertIn("Time by application", buf.getvalue())
        self.assertIn("Safari", buf.getvalue())

    def test_app_focus_is_not_logger_activity(self):
        # the monitor's markers must not hide a gap in the extension's own log
        ev = [e for e in EVENTS if e["type"] == "heartbeat"] + [self.focus("10:02:00", "Code").copy()]
        ev[-1]["ts"] = ts("10:02:00")
        g = summary.gap_data(ev, DAY)
        self.assertTrue(any(d > 200 for _a, _b, d, _l in g["gaps"]))


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
