"""Split the work queue into N shard files for separate machines.

    python -m verifier.make_shards --shards 4

Keeps UNVERIFIED_INACTIVE, NOT_FOUND and REPLACED_INACTIVE rows (PRE_EXISTING_ACTIVE is not
processed) by default; --statuses picks others, e.g. the GCP run:
    python -m verifier.make_shards --input work_queue_all.csv --out-dir shards_gcp --shards 13 \
        --statuses VERIFIED_ACTIVE,PRE_EXISTING_ACTIVE --done
It drops rows already resolved (--done: one or more resolver_state.sqlite files), and
puts row r into shard (source_row_number % N) + 1. Every row lands in exactly one shard, each
shard gets the same mix of priorities and keeps the queue's priority order.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
from collections import Counter
from pathlib import Path

from .master_resolver import PRIORITY

DEFAULT_STATUSES = ("UNVERIFIED_INACTIVE", "NOT_FOUND", "REPLACED_INACTIVE")


def done_rows(paths: list[str]) -> set[int]:
    rows: set[int] = set()
    for path in paths:
        db = sqlite3.connect(path)
        try:
            rows |= {r[0] for r in db.execute("SELECT source_row_number FROM results")}
        finally:
            db.close()
    return rows


def make_shards(queue: Path, out_dir: Path, shards: int, statuses: tuple[str, ...], done: set[int]) -> list[Path]:
    with open(queue, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        columns = list(reader.fieldnames or [])
        rows = [r for r in reader if r["Domain Status"] in statuses and int(r["source_row_number"]) not in done]
    rows.sort(key=lambda r: (PRIORITY.get(r["Domain Status"], 9), int(r["source_row_number"])))
    parts: list[list[dict]] = [[] for _ in range(shards)]
    for row in rows:
        parts[int(row["source_row_number"]) % shards].append(row)
    numbers = [int(r["source_row_number"]) for part in parts for r in part]
    assert len(numbers) == len(set(numbers)) == len(rows), "a row landed in two shards"
    assert not set(numbers) & done, "an already-resolved row is in a shard"
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, part in enumerate(parts, 1):
        path = out_dir / f"shard_{index}.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(part)
        paths.append(path)
        print(f"{path}: {len(part)} rows {dict(Counter(r['Domain Status'] for r in part))}")
    print(f"total {len(rows)} rows in {shards} shards; {len(done)} already-resolved rows excluded")
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Split the work queue into shard files")
    parser.add_argument("--input", default="work_queue_master.csv")
    parser.add_argument("--out-dir", default="shards")
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument("--statuses", default=",".join(DEFAULT_STATUSES))
    parser.add_argument("--done", nargs="*", default=["azure_runs/test_80rows_rejudged_with_fixes/resolver_state.sqlite"],
                        help="resolver_state.sqlite files whose rows are already resolved")
    args = parser.parse_args()
    make_shards(Path(args.input), Path(args.out_dir), args.shards, tuple(args.statuses.split(",")), done_rows(args.done))


if __name__ == "__main__":
    main()
