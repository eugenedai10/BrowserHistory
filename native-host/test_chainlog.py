#!/usr/bin/env python3
"""Tests for the hash chain + append-only protection: python3 native-host/test_chainlog.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import chainlog  # noqa: E402
import verify as V  # noqa: E402


def ev(n, t="click", day="2026-11-15"):
    return {"ts": f"{day}T10:00:{n % 60:02d}.000-08:00", "type": t, "n": n}


def unflag(path):
    """What an admin would do first; tests use it to simulate tampering."""
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                os.chflags(os.path.join(root, f), 0)
            except OSError:
                pass


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))

    def tearDown(self):
        unflag(self.d)
        shutil.rmtree(self.d, ignore_errors=True)

    def path(self, day="2026-11-15"):
        return os.path.join(self.d, day + ".jsonl")

    def lines(self, day="2026-11-15"):
        with open(self.path(day), "rb") as f:
            return f.read().splitlines(keepends=True)

    def rewrite(self, day, lines):
        unflag(self.d)
        with open(self.path(day), "wb") as f:
            f.writelines(lines)

    def seed(self, n=6):
        chainlog.append(self.d, [("2026-11-14", ev(0, day="2026-11-14")), ("2026-11-14", ev(1, day="2026-11-14"))])
        for i in range(2, n):
            chainlog.append(self.d, [("2026-11-15", ev(i))])
        self.assertTrue(V.verify(self.d).ok, V.verify(self.d).errors)

    def find_start(self):
        """(day, lineno) of the chain_start line; it lands in the real current day's file."""
        for name in chainlog.day_files(self.d):
            with open(os.path.join(self.d, name), "rb") as f:
                for i, raw in enumerate(f, 1):
                    if json.loads(raw).get("type") == "chain_start":
                        return name[:10], i
        raise AssertionError("no chain_start")

    def errors(self):
        return V.verify(self.d).errors

    def assertTampered(self, *needles):
        errs = " | ".join(self.errors())
        self.assertTrue(errs, "tampering not detected")
        for n in needles:
            self.assertIn(n, errs)


class ChainTests(Base):
    def test_basic_chain_and_flags(self):
        self.seed()
        r = V.verify(self.d)
        self.assertTrue(r.ok)
        self.assertEqual(r.files, 2)
        self.assertTrue(chainlog.is_append_only(self.path()) and chainlog.is_append_only(self.path("2026-11-14")))
        day, i = self.find_start()
        first = json.loads(self.lines(day)[i - 1])
        self.assertEqual((first["type"], first["seq"], first["prev"]), ("chain_start", 1, chainlog.GENESIS))

    def test_late_event_in_earlier_day_file_is_fine(self):
        self.seed()
        chainlog.append(self.d, [("2026-11-14", ev(99, day="2026-11-14"))])  # orphan event landing in yesterday's file
        self.assertTrue(V.verify(self.d).ok, V.verify(self.d).errors)

    def test_edit_line(self):
        self.seed()
        ls = self.lines()
        ls[2] = ls[2].replace(b'"n":3', b'"n":33')
        self.rewrite("2026-11-15", ls)
        self.assertTampered("edited")

    def test_delete_middle_line(self):
        self.seed()
        ls = self.lines()
        del ls[1]
        self.rewrite("2026-11-15", ls)
        self.assertTampered("missing")

    def test_delete_first_line(self):
        self.seed()
        day, i = self.find_start()
        ls = self.lines(day)
        del ls[i - 1]                                  # remove the very first line of the chain
        self.rewrite(day, ls)
        self.assertTampered("does not start")

    def test_reorder(self):
        self.seed()
        ls = self.lines()
        ls[0], ls[1] = ls[1], ls[0]
        self.rewrite("2026-11-15", ls)
        self.assertTampered("out of order")

    def test_truncate_last_line(self):
        self.seed()
        ls = self.lines()
        self.rewrite("2026-11-15", ls[:-1])
        self.assertTampered("tail removed")

    def test_delete_last_day_file(self):
        self.seed()
        unflag(self.d)
        os.remove(self.path())
        self.assertTampered("tail removed")

    def test_delete_middle_day_file(self):
        self.seed()
        chainlog.append(self.d, [("2026-11-16", ev(50, day="2026-11-16"))])
        unflag(self.d)
        os.remove(self.path("2026-11-15"))
        self.assertTampered("missing")

    def test_delete_all_logs(self):
        self.seed()
        unflag(self.d)
        for f in chainlog.day_files(self.d):
            os.remove(os.path.join(self.d, f))
        self.assertTampered("all logs deleted")

    def test_strip_hash_fields_from_a_line(self):
        self.seed()
        ls = self.lines()
        d = json.loads(ls[2])
        for k in ("seq", "prev", "h"):
            d.pop(k)
        ls[2] = (json.dumps(d) + "\n").encode()
        self.rewrite("2026-11-15", ls)
        self.assertTampered("unchained")

    def test_head_deleted_is_recovered_and_reported(self):
        self.seed()
        os.remove(os.path.join(self.d, ".head"))
        r = V.verify(self.d)
        self.assertTrue(any("head file" in w for w in r.warnings))
        chainlog.append(self.d, [("2026-11-15", ev(70))])
        r = V.verify(self.d)
        self.assertEqual(r.errors, [])
        self.assertTrue(any("chain_recovered" in w for w in r.warnings))

    def test_flag_removed_is_a_warning(self):
        self.seed()
        unflag(self.d)
        r = V.verify(self.d)
        self.assertEqual(r.errors, [])
        self.assertTrue(any("append-only flag" in w for w in r.warnings))

    def test_torn_write_is_confined_to_one_line(self):
        self.seed()
        unflag(self.d)
        with open(self.path(), "ab") as f:
            f.write(b'{"ts":"torn')           # crash mid-write, no newline
        chainlog.append(self.d, [("2026-11-15", ev(80))])
        errs = self.errors()
        self.assertEqual(len(errs), 1)
        self.assertIn("malformed", errs[0])   # only the torn line; the chain after it is intact


class MigrationTests(Base):
    def legacy(self):
        for day, n in (("2026-11-13", 3), ("2026-11-14", 4)):
            with open(self.path(day), "w") as f:
                for i in range(n):
                    f.write(json.dumps(ev(i, day=day)) + "\n")

    def test_migration_covers_and_seals_legacy_files(self):
        self.legacy()
        chainlog.append(self.d, [("2026-11-14", ev(10, day="2026-11-14"))])   # appends into a legacy file
        chainlog.append(self.d, [("2026-11-15", ev(11))])
        r = V.verify(self.d)
        self.assertTrue(r.ok, r.errors + r.warnings)
        self.assertTrue(all(chainlog.is_append_only(self.path(d)) for d in ("2026-11-13", "2026-11-14")))
        day, i = self.find_start()
        start = json.loads(self.lines(day)[i - 1])
        self.assertEqual(start["type"], "chain_start")
        self.assertEqual({x["file"] for x in start["legacy"]}, {"2026-11-13.jsonl", "2026-11-14.jsonl"})

    def test_tamper_legacy_bytes(self):
        self.legacy()
        chainlog.append(self.d, [("2026-11-15", ev(1))])
        ls = self.lines("2026-11-13")
        ls[1] = ls[1].replace(b'"n": 1', b'"n": 9')
        self.assertNotEqual(ls[1], self.lines("2026-11-13")[1])   # make sure the edit really changed something
        self.rewrite("2026-11-13", ls)
        self.assertTampered("legacy content changed")

    def test_delete_legacy_file(self):
        self.legacy()
        chainlog.append(self.d, [("2026-11-15", ev(1))])
        unflag(self.d)
        os.remove(self.path("2026-11-13"))
        self.assertTampered("legacy file is missing")

    def test_unchained_line_injected_after_migration(self):
        self.legacy()
        chainlog.append(self.d, [("2026-11-14", ev(10, day="2026-11-14"))])
        unflag(self.d)
        with open(self.path("2026-11-14"), "ab") as f:
            f.write((json.dumps(ev(77, day="2026-11-14")) + "\n").encode())
        self.assertTampered("unchained")


class ProtectionTests(Base):
    def test_flagged_file_refuses_tampering_but_accepts_appends(self):
        self.seed()
        p = self.path()
        size = os.stat(p).st_size
        for cmd in (f"echo x > '{p}'", f"rm -f '{p}'", f"mv '{p}' '{p}.bak'", f"sed -i '' s/click/CLICK/ '{p}'",
                    f"truncate -s 0 '{p}'"):
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            self.assertNotEqual(res.returncode, 0, cmd)
            self.assertIn("not permitted", (res.stderr or "").lower(), cmd)
        self.assertEqual(os.stat(p).st_size, size)
        chainlog.append(self.d, [("2026-11-15", ev(5))])             # the writer can still append
        self.assertGreater(os.stat(p).st_size, size)
        self.assertTrue(V.verify(self.d).ok)

    def test_two_processes_appending_concurrently(self):
        code = ("import sys; sys.path.insert(0, %r); import chainlog\n"
                "for i in range(40): chainlog.append(%r, [('2026-11-15', {'ts':'2026-11-15T10:00:00.000-08:00','type':'w','who':sys.argv[1],'i':i})])\n"
                % (HERE, self.d))
        procs = [subprocess.Popen([sys.executable, "-c", code, name]) for name in ("host", "monitor")]
        for p in procs:
            self.assertEqual(p.wait(timeout=60), 0)
        r = V.verify(self.d)
        self.assertTrue(r.ok, r.errors + r.warnings)
        self.assertEqual(r.lines, 81)                                # 80 events + chain_start, contiguous seq


if __name__ == "__main__":
    unittest.main(verbosity=1)
