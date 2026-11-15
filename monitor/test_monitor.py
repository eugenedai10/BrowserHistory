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


if __name__ == "__main__":
    unittest.main(verbosity=2)
