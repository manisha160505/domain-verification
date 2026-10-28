"""Parallel workers: unique row split, per-row saving, resume after a crash, live sheet."""
import argparse
import asyncio
import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from verifier import master_resolver
from verifier.master_resolver import StateStore, load_queue, partition, updated_status

STATUSES = ["UNVERIFIED_INACTIVE", "NOT_FOUND", "REPLACED_INACTIVE", "PRE_EXISTING_ACTIVE"]


def make_queue(path: Path, n: int = 40) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["priority", "source_row_number", "original_company_name",
                                               "country code", "Sales Territory Name", "Domain URL", "Domain Status"])
        writer.writeheader()
        for i in range(n):
            status = STATUSES[i % 4]
            writer.writerow({"priority": STATUSES.index(status) + 1, "source_row_number": 100 + i,
                             "original_company_name": f"COMPANY {i} LTD", "country code": "JP",
                             "Sales Territory Name": "", "Domain URL": f"c{i}.co.jp", "Domain Status": status})


def args_for(directory: Path, worker_id=0, num_workers=1, limit=None):
    return argparse.Namespace(input=str(directory / "queue.csv"), output_dir=str(directory / "out"), statuses=None,
                              overrides=None, calibration=None, export_only=False, retry_unreachable=False, limit=limit,
                              row_concurrency=3, fetch_concurrency=5, search_concurrency=1, timeout=5, max_pages=0,
                              no_wayback=True, no_registers=True, max_searches_per_minute=20,
                              max_register_lookups_per_minute=60, id_reference=None, num_workers=num_workers, worker_id=worker_id)


class Crash(Exception):
    pass


class ParallelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self.tmp.name)
        make_queue(self.dir / "queue.csv")
        self.calls: list[int] = []
        self.crash_after = None

        async def fake_resolve(row, inspect, discoverer, version, registers=None):
            number = int(row["source_row_number"])
            if self.crash_after is not None and len(self.calls) >= self.crash_after:
                raise Crash("simulated power cut")
            self.calls.append(number)
            await asyncio.sleep(0)
            return {"source_row_number": number, "decision": "ACCEPT", "outcome": "VERIFIED",
                    "new_domain_url": f"https://{row['Domain URL']}", "domain_changed": False, "proof_level": "STRONG"}

        self.patch = patch.object(master_resolver, "resolve_row", fake_resolve)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def run_worker(self, **kwargs):
        try:
            asyncio.run(master_resolver.run(args_for(self.dir, **kwargs)))
        except Crash:
            pass

    def saved(self) -> dict:
        state = StateStore(self.dir / "out" / "resolver_state.sqlite")
        try:
            return state.all()
        finally:
            state.close()

    def test_rows_split_uniquely_and_completely(self):
        queue = load_queue(self.dir / "queue.csv", None)
        for n in (1, 2, 3, 4, 7):
            parts = [partition(queue, n, k) for k in range(n)]
            numbers = [int(r["source_row_number"]) for part in parts for r in part]
            self.assertEqual(len(numbers), len(set(numbers)), f"overlap with {n} workers")
            self.assertEqual(sorted(numbers), sorted(int(r["source_row_number"]) for r in queue))
            for part in parts:  # each worker keeps priority order
                priorities = [int(r["priority"]) for r in part]
                self.assertEqual(priorities, sorted(priorities))

    def test_sharded_input_keeps_every_worker_busy(self):
        # A shard made with % 4 holds only multiples of 4; plain % 10 would idle half the workers.
        shard = [{"source_row_number": str(n), "priority": "1"} for n in range(0, 40000, 4)]
        sizes = [len(partition(shard, 10, k)) for k in range(10)]
        self.assertEqual(sum(sizes), len(shard))
        self.assertTrue(all(800 <= size <= 1200 for size in sizes), sizes)

    def test_workers_never_process_the_same_row(self):
        for k in range(3):
            self.run_worker(worker_id=k, num_workers=3)
        self.assertEqual(len(self.calls), 40)
        self.assertEqual(len(set(self.calls)), 40)
        saved = self.saved()
        self.assertEqual(len(saved), 40)
        from verifier.master_resolver import worker_of
        self.assertTrue(all(saved[n]["worker_id"] == worker_of(n, 3) for n in saved))

    def test_crash_then_resume_from_first_unsaved_row(self):
        self.crash_after = 6
        self.run_worker(worker_id=1, num_workers=2)
        first_run = list(self.calls)
        self.assertEqual(len(self.saved()), 6)  # rows finished before the crash are saved
        self.crash_after = None
        self.calls.clear()
        self.run_worker(worker_id=1, num_workers=2)
        mine = [int(r["source_row_number"]) for r in partition(load_queue(self.dir / "queue.csv", None), 2, 1)]
        self.assertFalse(set(first_run) & set(self.calls), "a saved row was processed again")
        self.assertEqual(self.calls[0], next(n for n in mine if n not in first_run))  # resumes at first unsaved row
        self.assertEqual(sorted(first_run + self.calls), sorted(mine))

    def test_progress_and_live_sheet(self):
        self.run_worker(worker_id=0, num_workers=2, limit=4)
        state = StateStore(self.dir / "out" / "resolver_state.sqlite")
        progress = state.progress()
        state.close()
        owned = len(partition(load_queue(self.dir / "queue.csv", None), 2, 0))
        self.assertEqual((progress[0]["worker_id"], progress[0]["done"], progress[0]["assigned"]), (0, 4, owned))
        self.assertEqual(progress[0]["status"], "PARTIAL")
        queue = load_queue(self.dir / "queue.csv", None)
        summary = master_resolver.write_outputs(queue, self.saved(), {}, self.dir / "out", excel=False)
        self.assertEqual(summary["updated_domain_status"], {"VERIFIED_ACTIVE": 4, "PENDING": 36})
        with open(self.dir / "out" / "resolved_master.csv", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 40)
        self.assertIn("Updated Domain Status", rows[0])

    def test_updated_status_values(self):
        self.assertEqual(updated_status(None), "PENDING")
        self.assertEqual(updated_status({"decision": "ACCEPT", "outcome": "REPLACED", "domain_changed": True}), "REPLACED_VERIFIED")
        self.assertEqual(updated_status({"decision": "ACCEPT", "outcome": "FOUND", "domain_changed": True}), "FOUND_VERIFIED")
        self.assertEqual(updated_status({"decision": "REVIEW", "outcome": "TEMP_UNREACHABLE"}), "RETRY_UNREACHABLE")
        self.assertEqual(updated_status({"decision": "REJECT", "outcome": "COMPANY_CLOSED"}), "COMPANY_CLOSED")


if __name__ == "__main__":
    unittest.main()
