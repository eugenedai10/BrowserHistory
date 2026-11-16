#!/usr/bin/env python3
"""Tests for the monitor state machine (no Chrome needed): python3 monitor/test_monitor.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import monitor as M  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HELPER = CHROME + " Helper (Renderer).app/Contents/MacOS/Google Chrome Helper (Renderer) --type=renderer"
HOST = "/opt/homebrew/bin/python3 /Users/x/chrome_history/native-host/host.py"


def chrome(pid=100, started=1000.0):
    return M.Proc(pid, 1, started, CHROME)


def hostp(ppid=100, pid=200):
    return M.Proc(pid, ppid, 1001.0, HOST)


class Rec:
    def __init__(self):
        self.ev = []

    def __call__(self, kind, epoch, **f):
        self.ev.append((kind, epoch, f))

    def kinds(self):
        return [k for k, _, _ in self.ev if k != "monitor_heartbeat"]


class Clock:
    """Wall and uptime clocks; sleep() advances wall only, like a real suspend."""
    def __init__(self, t=1000.0):
        self.wall, self.mono = t, 50.0

    def step(self, s=2.0):
        self.wall += s
        self.mono += s
        return self.wall, self.mono

    def sleep(self, s):
        self.wall += s


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.rec = Rec()
        self.m = M.Monitor(self.rec)
        self.c = Clock()

    def tick(self, procs, step=2.0):
        w, m = self.c.step(step)
        self.m.tick(procs, w, m)

    def test_parse_ps_and_match(self):
        text = (" 5310     1 Wed Nov  4 08:03:39 2026     " + CHROME + "\n"
                " 9000  5310 Sun Nov 15 00:43:37 2026     " + HOST + "\n"
                " 9001  5310 Sun Nov 15 00:43:37 2026     " + HELPER + "\n")
        procs = M.parse_ps(text)
        self.assertEqual(len(procs), 3)
        self.assertEqual(M.find_chrome(procs).pid, 5310)       # helper is not the main process
        self.assertTrue(M.has_host(procs, 5310))
        self.assertFalse(M.has_host(procs, 1))

    def test_start_stop(self):
        self.tick([])
        self.tick([chrome(started=self.c.wall)])
        self.tick([chrome(started=self.c.wall - 4), hostp()])
        self.tick([])
        self.assertEqual(self.rec.kinds(), ["browser_start", "browser_stop"])
        start = next(e for e in self.rec.ev if e[0] == "browser_start")
        self.assertFalse(start[2]["observed_late"])

    def test_observed_late_uses_now(self):
        self.tick([chrome(started=10.0), hostp()])
        k, ts, f = self.rec.ev[0]
        self.assertEqual(k, "browser_start")
        self.assertTrue(f["observed_late"])
        self.assertEqual(ts, self.c.wall)          # not the (possibly days-old) process start time

    def test_host_late_no_false_missing_within_grace(self):
        self.tick([chrome(started=self.c.wall)])
        for _ in range(5):                         # 10s without a host: under the 15s grace
            self.tick([chrome(started=self.c.wall - 2)])
        self.tick([chrome(started=1000.0), hostp()])
        self.assertEqual(self.rec.kinds(), ["browser_start"])

    def test_extension_missing_and_present(self):
        p = [chrome(started=self.c.wall + 2)]
        self.tick(p)
        for _ in range(12):                        # 24s with no host
            self.tick(p)
        self.assertIn("extension_missing", self.rec.kinds())
        miss = next(e for e in self.rec.ev if e[0] == "extension_missing")
        self.assertLess(miss[1], self.c.wall - 14)  # backdated to when the host vanished, not detection time
        self.tick(p + [hostp()])
        self.assertEqual(self.rec.kinds()[-1], "extension_present")
        self.assertGreater(next(e for e in self.rec.ev if e[0] == "extension_present")[2]["missing_s"], 20)
        n = len(self.rec.kinds())
        self.tick(p + [hostp()])
        self.assertEqual(len(self.rec.kinds()), n)  # no repeats

    def test_host_disappears_while_chrome_runs(self):
        p = [chrome(started=1.0), hostp()]
        self.tick(p)
        for _ in range(3):
            self.tick([chrome(started=1.0)])        # extension reloading: short outage
        self.tick(p)
        self.assertNotIn("extension_missing", self.rec.kinds())
        for _ in range(10):
            self.tick([chrome(started=1.0)])        # 20s outage
        self.assertIn("extension_missing", self.rec.kinds())

    def test_sleep_detected(self):
        self.tick([chrome(started=1.0), hostp()])
        before = self.c.wall
        self.c.sleep(300)                           # Mac asleep for 5 minutes
        self.tick([chrome(started=1.0), hostp()])
        kinds = self.rec.kinds()
        self.assertEqual(kinds[-2:], ["system_sleep", "system_wake"])
        sleep = next(e for e in self.rec.ev if e[0] == "system_sleep")
        wake = next(e for e in self.rec.ev if e[0] == "system_wake")
        self.assertEqual(sleep[1], before)          # sleep starts at the last tick before the jump
        self.assertAlmostEqual(wake[2]["asleep_s"], 300, delta=1)

    def test_no_false_sleep_on_normal_ticks(self):
        for _ in range(30):
            self.tick([])
        self.assertNotIn("system_sleep", self.rec.kinds())

    def test_heartbeat_cadence(self):
        for _ in range(65):                         # 130s
            self.tick([chrome(started=1.0), hostp()])
        hb = [e for e in self.rec.ev if e[0] == "monitor_heartbeat"]
        self.assertEqual(len(hb), 3)
        self.assertTrue(hb[-1][2]["chrome_running"])
        self.assertTrue(hb[-1][2]["extension_connected"])

    def test_chrome_restart_between_ticks(self):
        self.tick([chrome(pid=100, started=1.0), hostp()])
        self.tick([chrome(pid=300, started=self.c.wall), hostp(ppid=300)])
        self.assertEqual(self.rec.kinds(), ["browser_start", "browser_stop", "browser_start"])


CODE = ("Code", "com.microsoft.VSCode")
SAFARI = ("Safari", "com.apple.Safari")
LOGIN = ("loginwindow", "com.apple.loginwindow")


class FocusTests(unittest.TestCase):
    def setUp(self):
        self.rec = Rec()
        self.f = M.Focus(self.rec)
        self.t = 1_800_000_000.0

    def tick(self, front, idle=0.0, step=2.0, held=None):
        self.t += step
        self.f.tick(front, idle, self.t, held)

    def apps(self):
        return [(e[2]["app"], e[2].get("reason")) for e in self.rec.ev]

    def test_emits_first_then_only_changes(self):
        for _ in range(5):
            self.tick(CODE)
        self.tick(SAFARI)
        self.tick(SAFARI)
        self.tick(CODE)
        self.assertEqual(self.apps(), [("Code", None), ("Safari", None), ("Code", None)])
        self.assertEqual(self.rec.ev[0][2]["bundle"], "com.microsoft.VSCode")

    def test_no_front_emits_nothing(self):
        self.tick(None)
        self.assertEqual(self.rec.ev, [])

    def test_idle_is_backdated_and_resumes(self):
        self.tick(CODE)
        stop = self.t                                # the user stops typing here
        for i in range(1, 160):                      # 2 s polls; HIDIdleTime grows with them
            self.tick(CODE, idle=2.0 * i)
            if len(self.rec.ev) > 1:
                break
        idle = self.rec.ev[-1]
        self.assertEqual(idle[2], {"app": None, "bundle": None, "reason": "idle"})
        self.assertAlmostEqual(idle[1], stop + 2, delta=3)   # ~ when input stopped, not when we noticed
        n = len(self.rec.ev)
        self.tick(CODE, idle=320)                    # still idle: no repeat
        self.assertEqual(len(self.rec.ev), n)
        self.tick(CODE, idle=0)                      # input again
        self.assertEqual(self.apps()[-1], ("Code", None))

    def test_below_idle_threshold_counts_as_use(self):
        self.tick(CODE, idle=299)
        self.assertEqual(self.apps(), [("Code", None)])

    def test_backdate_never_precedes_previous_event(self):
        self.tick(CODE)
        self.tick(SAFARI)                            # switched with no input (an app popped to the front)
        switch_ts = self.rec.ev[-1][1]
        self.tick(SAFARI, idle=3000)                 # idle claims to have started long before the switch
        self.assertGreaterEqual(self.rec.ev[-1][1], switch_ts)

    def test_locked_screen_is_away(self):
        self.tick(CODE)
        self.tick(LOGIN)
        self.assertEqual(self.apps()[-1], (None, "locked"))
        self.tick(LOGIN)
        self.assertEqual(len(self.rec.ev), 2)
        self.tick(CODE)
        self.assertEqual(self.apps()[-1], ("Code", None))

    def test_sleep_gap_restarts_span_without_backdating_into_it(self):
        self.tick(CODE)
        self.tick(CODE, step=3600)                   # Mac slept an hour
        self.assertEqual(len(self.rec.ev), 2)
        self.assertEqual(self.rec.ev[-1][1], self.t)
        self.tick(CODE, idle=3600, step=3600)        # woke with a huge HIDIdleTime
        self.assertEqual(self.rec.ev[-1][2]["reason"], "idle")
        self.assertEqual(self.rec.ev[-1][1], self.t)  # clamped to the wake time, not an hour earlier

    def test_midnight_reemits_so_each_day_is_self_contained(self):
        self.t = 1_800_000_000.0
        d = M.datetime.date.fromtimestamp(self.t)
        midnight = M.datetime.datetime.combine(d + M.datetime.timedelta(days=1), M.datetime.time()).timestamp()
        self.t = midnight - 4
        self.f.tick(CODE, 0, self.t)
        self.f.tick(CODE, 0, self.t + 2)
        self.assertEqual(len(self.rec.ev), 1)
        self.f.tick(CODE, 0, midnight + 1)
        self.assertEqual(len(self.rec.ev), 2)

    def test_video_playing_without_input_is_not_away(self):
        self.tick(CODE)
        asked = []
        held = lambda: asked.append(1) or True
        for i in range(1, 400):                      # 800 s of video, no input at all
            self.tick(CODE, idle=2.0 * i, held=held)
        self.assertEqual(self.apps(), [("Code", None)])
        self.assertGreater(len(asked), 0)

    def test_held_is_only_probed_after_idle_threshold(self):
        calls = []
        self.tick(CODE, idle=10, held=lambda: calls.append(1) or False)
        self.assertEqual(calls, [])

    def test_idle_starts_when_the_video_stops_not_at_last_input(self):
        self.tick(CODE)
        playing = True
        for i in range(1, 300):                      # 600 s watching, then the video ends
            if i == 300 - 1:
                playing = False
            self.tick(CODE, idle=2.0 * i, held=lambda: playing)
            if not playing:
                break
        stopped = self.t
        self.assertEqual(len(self.rec.ev), 1)        # not idle yet: 5 min must pass after the video ends
        for i in range(160):
            self.tick(CODE, idle=2.0 * (300 + i), held=lambda: False)
            if len(self.rec.ev) > 1:
                break
        idle = self.rec.ev[-1]
        self.assertEqual(idle[2]["reason"], "idle")
        self.assertAlmostEqual(idle[1], stopped, delta=4)  # back-dated to the end of the video, not to the last keypress

    def test_failed_held_probe_skips_the_tick(self):
        self.tick(CODE)
        self.tick(CODE, idle=400, held=lambda: None)
        self.assertEqual(len(self.rec.ev), 1)

    def test_locked_screen_ignores_held(self):
        self.tick(CODE)
        self.tick(LOGIN, idle=0, held=lambda: True)
        self.assertEqual(self.apps()[-1], (None, "locked"))

    def test_parse_display_held(self):
        held = "Assertion status system-wide:\n   PreventUserIdleDisplaySleep    1\n   PreventUserIdleSystemSleep     1\n"
        free = "Assertion status system-wide:\n   PreventUserIdleDisplaySleep    0\n   PreventUserIdleSystemSleep     1\n"
        self.assertTrue(M.parse_display_held(held))
        self.assertFalse(M.parse_display_held(free))     # system-sleep assertions alone (powerd, caffeinate -i) don't count
        self.assertIsNone(M.parse_display_held("garbage"))

    def test_parsers(self):
        self.assertEqual(M.parse_front('"LSDisplayName"="Code"\n"CFBundleIdentifier"="com.microsoft.VSCode"\n'), CODE)
        self.assertIsNone(M.parse_front(""))
        self.assertIsNone(M.parse_front('"LSDisplayName"="X"\n'))   # no bundle id: ignore rather than guess
        self.assertAlmostEqual(M.parse_idle('    "HIDIdleTime" = 46887358666\n'), 46.887358666)
        self.assertIsNone(M.parse_idle("nothing here"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
