#!/usr/bin/python3
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ram_reclaim as reclaim


class ReclaimTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "pressure").mkdir()
        (self.root / "sys/vm").mkdir(parents=True)
        self.stat([0] * 16, initial=True)
        self.put("loadavg", "0.1 0.1 0.1 1/100 1\n")
        self.psi()
        self.memory()
        self.writes = []

    def put(self, path, text):
        (self.root / path).write_text(text)

    def stat(self, busy, initial=False, iowait=False, guest=False):
        rows = []
        for value in busy:
            ticks = [100, 0, 0, 900, 0, 0, 0, 0]
            if not initial:
                ticks[4 if iowait else 0] += value
                ticks[3] += 100 - value
            if guest:
                ticks += [999999 if not initial else 0, 0]
            rows.append(ticks)
        aggregate = [sum(row[index] for row in rows) for index in range(len(rows[0]))]
        text = "cpu " + " ".join(map(str, aggregate)) + "\n"
        text += "".join(f"cpu{index} " + " ".join(map(str, row)) + "\n"
                        for index, row in enumerate(rows))
        self.put("stat", text)

    def psi(self, some=0, full=0):
        self.put("pressure/memory", f"some avg10={some} avg60=0 avg300=0 total=0\n"
                 f"full avg10={full} avg60=0 avg300=0 total=0\n")

    def memory(self, cached=2048, buffers=0, shmem=0, dirty=0, writeback=0):
        self.put("meminfo", "".join(f"{name}: {value * 1024} kB\n" for name, value in (
            ("Cached", cached), ("Buffers", buffers), ("Shmem", shmem),
            ("Dirty", dirty), ("Writeback", writeback))))

    def execute(self, after=None, dry_run=True, uid=1000):
        def sleeper(seconds):
            self.assertEqual(seconds, 10)
            self.stat([0] * 16)
            if after:
                after()
        times = iter((0, 10))
        return reclaim.run(reclaim.Config(dry_run=dry_run), self.root, sleeper,
                           lambda: next(times), self.writes.append, lambda: uid)

    def test_one_saturated_core_on_sixteen_skips(self):
        result = self.execute(lambda: self.stat([100] + [0] * 15))
        self.assertIn("skip: cpu0 activity 100.0%", result)
        self.assertEqual(self.writes, [])

    def test_iowait_counts_as_busy(self):
        result = self.execute(lambda: self.stat([100] + [0] * 15, iowait=True))
        self.assertIn("skip: cpu0 activity", result)

    def test_guest_fields_not_counted_twice(self):
        self.stat([0] * 16, initial=True, guest=True)
        result = self.execute(lambda: self.stat([0] * 16, guest=True))
        self.assertTrue(result.startswith("would-drop:"))

    def test_load_is_checked_after_sample(self):
        result = self.execute(lambda: self.put("loadavg", "0.6 0 0 1/2 1\n"))
        self.assertIn("skip: load", result)

    def test_load_is_checked_before_sample(self):
        self.put("loadavg", "0.6 0 0 1/2 1\n")
        with patch.object(reclaim.time, "sleep", side_effect=AssertionError):
            result = reclaim.run(reclaim.Config(), self.root,
                                 sleeper=lambda _: self.fail("must not sample"))
        self.assertIn("skip: load", result)

    def test_psi_guards(self):
        for some, full in ((0.51, 0), (0, 0.11)):
            with self.subTest(some=some, full=full):
                self.stat([0] * 16, initial=True)
                self.psi()
                result = self.execute(lambda: self.psi(some, full))
                self.assertIn("skip: memory pressure", result)

    def test_dirty_and_writeback_guard(self):
        result = self.execute(lambda: self.memory(dirty=9, writeback=8))
        self.assertIn("skip: dirty/writeback", result)

    def test_tmpfs_is_excluded(self):
        result = self.execute(lambda: self.memory(cached=2048, shmem=1500))
        self.assertIn("skip: disposable cache 548.0 MiB", result)

    def test_negative_cache_estimate_clamped(self):
        result = self.execute(lambda: self.memory(cached=10, shmem=20))
        self.assertIn("skip: disposable cache 0.0 MiB", result)

    def test_dry_run_does_not_need_root_or_write(self):
        self.assertTrue(self.execute().startswith("would-drop:"))
        self.assertEqual(self.writes, [])

    def test_actual_write_is_only_drop_caches(self):
        def sleeper(_):
            self.stat([0] * 16)
        times = iter((0, 10))
        result = reclaim.run(reclaim.Config(), self.root, sleeper,
                             lambda: next(times), geteuid=lambda: 0)
        self.assertTrue(result.startswith("dropped:"))
        self.assertEqual((self.root / "sys/vm/drop_caches").read_text(), "1\n")
        self.assertEqual(list((self.root / "sys/vm").iterdir()),
                         [self.root / "sys/vm/drop_caches"])

    def test_actual_write_requires_root(self):
        with self.assertRaisesRegex(reclaim.ReclaimError, "root is required"):
            self.execute(dry_run=False)
        self.assertEqual(self.writes, [])

    def test_final_activity_is_checked(self):
        original = reclaim.read_cpu
        reads = 0
        def read(root):
            nonlocal reads
            reads += 1
            if reads == 3:
                # New work begins after the measured idle interval.
                snapshot = original(root)
                rows = {name: list(ticks) for name, ticks in snapshot.items()}
                rows["cpu"][0] += 100
                rows["cpu0"][0] += 100
                return rows
            return original(root)
        with patch.object(reclaim, "read_cpu", side_effect=read):
            self.assertIn("skip: cpu activity", self.execute())

    def test_final_memory_is_fresh(self):
        original = reclaim.read_guards
        reads = 0
        def read(root, config):
            nonlocal reads
            reads += 1
            if reads == 3:
                self.memory(cached=500)
            return original(root, config)
        with patch.object(reclaim, "read_guards", side_effect=read):
            self.assertIn("skip: disposable cache 500.0 MiB", self.execute())

    def test_missing_or_malformed_inputs_fail_closed(self):
        cases = (("stat", "cpu 1 2\n"), ("loadavg", "nan 0 0\n"),
                 ("pressure/memory", "some avg10=nan\nfull avg10=0\n"),
                 ("meminfo", "Cached: 100 kB\n"))
        for path, contents in cases:
            with self.subTest(path=path):
                original = (self.root / path).read_text()
                self.put(path, contents)
                with self.assertRaises(reclaim.ReclaimError):
                    self.execute()
                self.put(path, original)
                self.stat([0] * 16, initial=True)
        (self.root / "pressure/memory").unlink()
        with self.assertRaises(reclaim.ReclaimError):
            self.execute()
        self.assertEqual(self.writes, [])

    def test_bad_configuration(self):
        for name in ("MIN_CACHE_MB", "MAX_CPU_PCT", "MAX_CORE_CPU_PCT",
                     "MAX_LOAD_AVG", "SAMPLE_SEC"):
            for value in ("bad", "nan", "inf", "0", "-1"):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(reclaim.ReclaimError):
                        reclaim.Config.from_env({name: value})
        for env in ({"MAX_CPU_PCT": "101"}, {"MAX_CORE_CPU_PCT": "101"},
                    {"SAMPLE_SEC": "3601"}, {"DRY_RUN": "yes"}):
            with self.assertRaises(reclaim.ReclaimError):
                reclaim.Config.from_env(env)
        self.assertTrue(reclaim.Config.from_env({"DRY_RUN": "1"}).dry_run)
        self.assertTrue(reclaim.Config.from_env({}, dry_run=True).dry_run)

    def test_cli_error_returns_nonzero(self):
        with patch.dict(reclaim.os.environ, {"MIN_CACHE_MB": "nan"}), \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(reclaim.main([]), 1)
        self.assertIn("invalid MIN_CACHE_MB", error.getvalue())


if __name__ == "__main__":
    unittest.main()
