"""Machine-wide rate limit shared by all worker processes."""
import multiprocessing
import tempfile
import time
import unittest
from pathlib import Path

from verifier.ratelimit import SharedRateLimiter


def _grab(path: str, count: int, queue) -> None:
    limiter = SharedRateLimiter(path, "search", 10)
    granted = sum(limiter.try_acquire() == 0 for _ in range(count))
    limiter.close()
    queue.put(granted)


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = str(Path(self.tmp.name) / "rate.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_workers_share_one_budget(self):
        a, b = SharedRateLimiter(self.path, "search", 5), SharedRateLimiter(self.path, "search", 5)
        now = 1000.0
        granted = [lim.try_acquire(now) == 0 for lim in (a, b, a, b, a, b, a)]
        self.assertEqual(granted.count(True), 5)  # 7 requests from two "workers", only 5 allowed per minute
        self.assertGreater(a.try_acquire(now + 1), 0)
        self.assertEqual(b.try_acquire(now + 61), 0)  # the window moved on: a slot is free again
        a.close(), b.close()

    def test_buckets_are_independent_and_zero_means_unlimited(self):
        search, register = SharedRateLimiter(self.path, "search", 1), SharedRateLimiter(self.path, "register", 1)
        self.assertEqual(search.try_acquire(10.0), 0)
        self.assertEqual(register.try_acquire(10.0), 0)
        self.assertGreater(search.try_acquire(11.0), 0)
        unlimited = SharedRateLimiter(self.path, "x", 0)
        self.assertTrue(all(unlimited.try_acquire(12.0) == 0 for _ in range(100)))
        search.close(), register.close(), unlimited.close()

    def test_block_halves_limit_and_quiet_period_restores_it(self):
        a, b = SharedRateLimiter(self.path, "search", 20), SharedRateLimiter(self.path, "search", 20)
        self.assertEqual(a.current_limit(1000.0), 20)
        self.assertEqual(a.slow_down(1000.0), 10)          # worker a was blocked...
        self.assertEqual(b.current_limit(1001.0), 10)      # ...so worker b also slows down
        self.assertEqual(b.slow_down(1002.0), 10)          # same block wave: counted once (debounce)
        self.assertEqual(a.slow_down(1000.0 + 181), 10)    # a later block cannot go below the floor
        self.assertEqual(a.current_limit(1181.0 + 299), 10)  # still within the quiet period
        self.assertEqual(a.current_limit(1181.0 + 300), 12)  # +2 after 5 quiet minutes
        self.assertEqual(a.current_limit(1181.0 + 600), 14)
        granted = sum(b.try_acquire(5000.0) == 0 for _ in range(30))
        self.assertLessEqual(granted, 20)
        a.close(), b.close()

    def test_old_state_without_debounce_column_is_upgraded(self):
        import sqlite3
        db = sqlite3.connect(self.path)
        db.execute("CREATE TABLE rate_state (bucket TEXT PRIMARY KEY, current REAL, last_change REAL)")
        db.execute("INSERT INTO rate_state VALUES ('search', 5, 0)")  # an older run left it at 5/min
        db.commit(), db.close()
        lim = SharedRateLimiter(self.path, "search", 20)
        self.assertEqual(lim.current_limit(10.0), 10)  # raised to the new floor
        lim.close()

    def test_lowered_limit_is_enforced(self):
        lim = SharedRateLimiter(self.path, "search", 20)
        self.assertEqual(sum(lim.try_acquire(100.0) == 0 for _ in range(30)), 20)
        lim.slow_down(101.0)  # limit 10 (the floor), but 20 events already in the window
        self.assertGreater(lim.try_acquire(102.0), 0)
        self.assertEqual(lim.try_acquire(161.0), 0)  # old events expired
        lim.close()

    def test_real_processes_never_exceed_the_limit(self):
        SharedRateLimiter(self.path, "search", 10).close()
        queue = multiprocessing.get_context("spawn").Queue()
        procs = [multiprocessing.get_context("spawn").Process(target=_grab, args=(self.path, 8, queue)) for _ in range(4)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
        total = sum(queue.get(timeout=10) for _ in procs)
        self.assertEqual(total, 10)  # 4 processes asked for 32 slots within a minute; exactly 10 granted


if __name__ == "__main__":
    unittest.main()
