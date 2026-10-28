"""Run the resolver as N parallel worker processes over disjoint rows.

    python -m verifier.run_parallel --workers 6                  # whole queue
    python -m verifier.run_parallel --workers 2 --limit-per-worker 3   # small test (6 rows)
    python -m verifier.run_parallel --status                     # show progress only

How the work is split and saved:
  * Worker k owns the rows with source_row_number % N == k. Every row has exactly one owner,
    no two workers ever process the same row, and each worker goes through its rows in the
    queue's priority order (UNVERIFIED_INACTIVE -> NOT_FOUND -> REPLACED_INACTIVE -> PRE_EXISTING_ACTIVE).
  * A row's result is saved to resolver_output/resolver_state.sqlite the moment it finishes.
    If a worker stops at its row m-10 (crash, Ctrl+C, power cut), rows before it are already
    saved; on the next start it skips them and continues from its first unsaved row.
  * A worker that exits with an error is restarted automatically (up to --max-restarts) and
    resumes the same way.
  * Every --export-every seconds the sheet resolver_output/resolved_master.csv is rewritten with
    each row's `Updated Domain Status` (PENDING until processed). Excel is written at the end.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .master_resolver import StateStore, load_calibration, load_overrides, load_queue, write_outputs

LOG = logging.getLogger("run_parallel")
PASS_THROUGH = ("input", "output_dir", "statuses", "overrides", "row_concurrency", "fetch_concurrency",
                "search_concurrency", "timeout", "max_pages", "max_searches_per_minute",
                "max_register_lookups_per_minute", "id_reference")


def worker_command(args, worker_id: int) -> list[str]:
    cmd = [sys.executable, "-m", "verifier.master_resolver", "--num-workers", str(args.workers),
           "--worker-id", str(worker_id)]
    for name in PASS_THROUGH:
        value = getattr(args, name)
        if value not in (None, ""):
            cmd += ["--" + name.replace("_", "-"), str(value)]
    if args.limit_per_worker:
        cmd += ["--limit", str(args.limit_per_worker)]
    if args.no_wayback:
        cmd.append("--no-wayback")
    if args.retry_unreachable:
        cmd.append("--retry-unreachable")
    if args.no_registers:
        cmd.append("--no-registers")
    return cmd


def print_status(out: Path) -> None:
    state = StateStore(out / "resolver_state.sqlite")
    try:
        rows = state.progress()
    finally:
        state.close()
    if not rows:
        print("No worker progress recorded yet.")
        return
    total_assigned = sum(r["assigned"] or 0 for r in rows)
    total_done = sum(r["done"] or 0 for r in rows)
    print(f"{'worker':>6} {'status':>9} {'saved':>8} {'owned':>8} {'pct':>6}  last row   updated")
    for r in rows:
        pct = 100 * (r["done"] or 0) / max(1, r["assigned"] or 0)
        print(f"{r['worker_id']:>6} {r['status']:>9} {r['done'] or 0:>8} {r['assigned'] or 0:>8} {pct:>5.1f}%  "
              f"{str(r['last_row'] or '-'):>8}   {(r['updated_at'] or '')[:19]}")
    print(f"{'total':>6} {'':>9} {total_done:>8} {total_assigned:>8} {100 * total_done / max(1, total_assigned):>5.1f}%")


def search_health(out: Path, seen: dict) -> str:
    """One line for the main log: current machine-wide search limit and search blocks in the
    worker logs since the last call (worker logs are not readable while a job runs)."""
    import sqlite3
    limit = "?"
    try:
        db = sqlite3.connect(out / "rate_limits.sqlite", timeout=30)
        row = db.execute("SELECT current FROM rate_state WHERE bucket = 'search'").fetchone()
        db.close()
        limit = f"{row[0]:.0f}" if row else "max"
    except Exception:
        pass
    total = 0
    for log in (out / "logs").glob("worker_*.log"):
        try:
            total += log.read_text(encoding="utf-8", errors="replace").count("blocked/failed")
        except OSError:
            pass
    new = total - seen.get("blocks", 0)
    seen["blocks"] = total
    return f"search health: limit {limit}/min, blocks since last check {new}, total blocks {total}"


def export(args, excel: bool) -> dict:
    out = Path(args.output_dir)
    statuses = set(args.statuses.split(",")) if args.statuses else None
    queue = load_queue(Path(args.input), statuses)
    state = StateStore(out / "resolver_state.sqlite")
    try:
        results = state.all()
    finally:
        state.close()
    return write_outputs(queue, results, load_overrides(args.overrides), out, input_path=Path(args.input), excel=excel,
                         calibration=load_calibration(args.calibration))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the domain resolver with N parallel workers")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--input", default="work_queue_master.csv")
    parser.add_argument("--output-dir", default="resolver_output")
    parser.add_argument("--statuses", help="comma-separated Domain Status values to include")
    parser.add_argument("--overrides", help="CSV of reviewer decisions")
    parser.add_argument("--id-reference", help="full queue file for shared tax-ID detection (when --input is a shard)")
    parser.add_argument("--calibration", help="accuracy_report.json from verifier.accuracy score (accuracy gate)")
    parser.add_argument("--limit-per-worker", type=int, help="each worker resolves at most N new rows (tests)")
    parser.add_argument("--row-concurrency", type=int, default=4, help="rows in flight inside each worker")
    parser.add_argument("--fetch-concurrency", type=int, default=20)
    parser.add_argument("--search-concurrency", type=int, default=1, help="searches in flight per worker")
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--max-pages", type=int, default=4)
    parser.add_argument("--no-wayback", action="store_true")
    parser.add_argument("--retry-unreachable", action="store_true")
    parser.add_argument("--no-registers", action="store_true", help="skip official register lookups (JP/AU)")
    parser.add_argument("--max-searches-per-minute", type=float, default=20,
                        help="search-engine requests per minute, all workers together (0 = no limit)")
    parser.add_argument("--max-register-lookups-per-minute", type=float, default=60,
                        help="register page requests per minute, all workers together (0 = no limit)")
    parser.add_argument("--export-every", type=int, default=300, help="seconds between sheet refreshes")
    parser.add_argument("--max-restarts", type=int, default=5, help="automatic restarts per crashed worker")
    parser.add_argument("--status", action="store_true", help="print worker progress and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    if args.status:
        print_status(out)
        return
    if args.workers < 1:
        raise SystemExit("--workers must be at least 1")

    logs = out / "logs"
    logs.mkdir(exist_ok=True)
    procs: dict[int, subprocess.Popen] = {}
    restarts = {k: 0 for k in range(args.workers)}
    handles = {}

    def start(k: int) -> None:
        handles[k] = open(logs / f"worker_{k}.log", "a", encoding="utf-8")
        handles[k].write(f"\n===== start {datetime.now():%Y-%m-%d %H:%M:%S} (restart {restarts[k]}) =====\n")
        handles[k].flush()
        procs[k] = subprocess.Popen(worker_command(args, k), stdout=handles[k], stderr=subprocess.STDOUT)
        LOG.info("worker %d started (pid %d), log %s", k, procs[k].pid, logs / f"worker_{k}.log")

    # Create the shared databases once, before any worker opens them, then start workers a few
    # seconds apart so they never race to set up the same files.
    from .cache import SQLiteCache
    from .discovery import SearchCache
    from .registers import RegisterClient
    StateStore(out / "resolver_state.sqlite").close()
    SQLiteCache(out / "domain_cache.sqlite", 86400).close()
    SearchCache(str(out / "search_cache.sqlite")).close()
    RegisterClient(str(out / "registers.sqlite")).db.close()
    from .ratelimit import SharedRateLimiter
    SharedRateLimiter(str(out / "rate_limits.sqlite"), "search", 1).close()
    for k in range(args.workers):
        start(k)
        time.sleep(3)
    last_export = time.time()
    health: dict = {}
    try:
        while procs:
            time.sleep(10)
            for k, proc in list(procs.items()):
                code = proc.poll()
                if code is None:
                    continue
                handles[k].close()
                del procs[k]
                if code == 0:
                    LOG.info("worker %d finished", k)
                elif restarts[k] < args.max_restarts:
                    restarts[k] += 1
                    LOG.warning("worker %d exited with code %s; restarting (%d/%d), it resumes from its first unsaved row",
                                k, code, restarts[k], args.max_restarts)
                    start(k)
                else:
                    LOG.error("worker %d failed %d times; leaving it stopped (its saved rows are kept)", k, restarts[k] + 1)
            if time.time() - last_export >= args.export_every:
                summary = export(args, excel=False)
                LOG.info("sheet refreshed: %s", json.dumps(summary.get("updated_domain_status", {})))
                LOG.info(search_health(out, health))
                print_status(out)
                sys.stdout.flush()
                last_export = time.time()
    except KeyboardInterrupt:
        LOG.warning("Stopping workers; finished rows are saved and the next run resumes where each worker stopped")
        for proc in procs.values():
            proc.terminate()
        for proc in procs.values():
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
    finally:
        for handle in handles.values():
            if not handle.closed:
                handle.close()
    summary = export(args, excel=True)
    print_status(out)
    print(json.dumps({k: summary.get(k) for k in ("rows_resolved", "rows_pending", "updated_domain_status")}, indent=2))


if __name__ == "__main__":
    main()
