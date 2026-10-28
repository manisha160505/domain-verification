"""Rate limit shared by all worker processes on one machine.

Every worker records its requests in the same SQLite file, so the limit holds for the whole
machine (one IP address) however many workers run: e.g. at most 20 searches per minute in total.

The limit adapts: when a search engine blocks any worker, slow_down() halves the machine's
current limit (never below `minimum`), at most once per `debounce_seconds` so that several
workers hitting the same block count once; after each `recovery_seconds` without a block it
rises by `recovery_step` per minute, back up to the configured maximum.
"""
from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
import time

LOG = logging.getLogger(__name__)


class SharedRateLimiter:
    def __init__(self, path: str, bucket: str, per_minute: float, window: float = 60.0,
                 minimum: float = 10.0, recovery_step: float = 2.0, recovery_seconds: float = 300.0,
                 debounce_seconds: float = 180.0):
        self.bucket = bucket
        self.per_minute = per_minute
        self.window = window
        self.minimum = min(minimum, per_minute) if per_minute else minimum
        self.recovery_step = recovery_step
        self.recovery_seconds = recovery_seconds
        self.debounce_seconds = debounce_seconds
        self.db = sqlite3.connect(path, timeout=120.0, isolation_level=None)  # explicit transactions
        self.db.execute("PRAGMA busy_timeout=120000")
        self.db.execute("CREATE TABLE IF NOT EXISTS rate_events (bucket TEXT, ts REAL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS rate_events_idx ON rate_events (bucket, ts)")
        self.db.execute("CREATE TABLE IF NOT EXISTS rate_state (bucket TEXT PRIMARY KEY, current REAL, "
                        "last_change REAL, last_slowdown REAL DEFAULT 0)")
        if "last_slowdown" not in {r[1] for r in self.db.execute("PRAGMA table_info(rate_state)")}:
            self.db.execute("ALTER TABLE rate_state ADD COLUMN last_slowdown REAL DEFAULT 0")  # older runs

    def _current(self, now: float) -> float:
        """Current machine-wide limit, raised by recovery_step after each quiet period.
        Must be called inside a transaction."""
        row = self.db.execute("SELECT current, last_change FROM rate_state WHERE bucket = ?", (self.bucket,)).fetchone()
        if row is None or row[0] > self.per_minute:  # first use, or the configured maximum was lowered
            self.db.execute("INSERT OR REPLACE INTO rate_state (bucket, current, last_change, last_slowdown) "
                            "VALUES (?, ?, ?, 0)", (self.bucket, self.per_minute, now))
            return self.per_minute
        current, last_change = row
        if current < self.minimum:  # floor raised since an older run lowered it
            current = self.minimum
            self.db.execute("UPDATE rate_state SET current = ? WHERE bucket = ?", (current, self.bucket))
        if current < self.per_minute and now - last_change >= self.recovery_seconds:
            current = min(self.per_minute, current + self.recovery_step)
            self.db.execute("UPDATE rate_state SET current = ?, last_change = ? WHERE bucket = ?", (current, now, self.bucket))
            LOG.warning("%s rate limit raised to %.0f/min after a quiet period", self.bucket, current)
        return current

    def current_limit(self, now: float | None = None) -> float:
        if not self.per_minute or self.per_minute <= 0:
            return 0.0
        self.db.execute("BEGIN IMMEDIATE")
        try:
            value = self._current(time.time() if now is None else now)
            self.db.execute("COMMIT")
            return value
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def slow_down(self, now: float | None = None) -> float:
        """A block happened on this machine: halve the limit for every worker."""
        if not self.per_minute or self.per_minute <= 0:
            return 0.0
        now = time.time() if now is None else now
        self.db.execute("BEGIN IMMEDIATE")
        try:
            current = self._current(now)
            last = self.db.execute("SELECT last_slowdown FROM rate_state WHERE bucket = ?", (self.bucket,)).fetchone()[0] or 0
            if now - last < self.debounce_seconds:  # same block wave, already slowed down
                self.db.execute("COMMIT")
                return current
            lowered = max(self.minimum, current / 2)
            self.db.execute("UPDATE rate_state SET current = ?, last_change = ?, last_slowdown = ? WHERE bucket = ?",
                            (lowered, now, now, self.bucket))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        if lowered < current:
            LOG.warning("%s blocked: rate limit lowered from %.0f to %.0f/min for the whole machine",
                        self.bucket, current, lowered)
        return lowered

    def try_acquire(self, now: float | None = None) -> float:
        """Take a slot if one is free (returns 0), else return the seconds until one frees up."""
        if not self.per_minute or self.per_minute <= 0:
            return 0.0
        now = time.time() if now is None else now
        self.db.execute("BEGIN IMMEDIATE")  # one worker at a time decides
        try:
            limit = max(1, int(self._current(now) * self.window / 60.0))
            self.db.execute("DELETE FROM rate_events WHERE bucket = ? AND ts <= ?", (self.bucket, now - self.window))
            used, oldest = self.db.execute("SELECT COUNT(*), MIN(ts) FROM rate_events WHERE bucket = ?",
                                           (self.bucket,)).fetchone()
            if used < limit:
                self.db.execute("INSERT INTO rate_events VALUES (?, ?)", (self.bucket, now))
                self.db.execute("COMMIT")
                return 0.0
            self.db.execute("COMMIT")
            if oldest is None:
                return 1.0
            # With a lowered limit, wait for enough old events to expire.
            events = [r[0] for r in self.db.execute(
                "SELECT ts FROM rate_events WHERE bucket = ? ORDER BY ts LIMIT ?", (self.bucket, used - limit + 1))]
            return max(0.05, events[-1] + self.window - now)
        except Exception:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    async def acquire(self) -> None:
        while True:
            wait = self.try_acquire()
            if wait <= 0:
                return
            await asyncio.sleep(wait + random.uniform(0, 1.0))  # jitter so workers don't wake together

    def close(self) -> None:
        self.db.close()
