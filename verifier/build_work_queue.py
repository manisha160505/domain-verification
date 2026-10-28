"""
Build the domain re-verification work queue from the final super merged master:
1. Appends registry status columns from the raw super merged master (rows align 1:1 by position).
2. Moves VERIFIED_ACTIVE rows to a separate reference sheet.
3. Orders the remaining rows by priority:
   UNVERIFIED_INACTIVE -> NOT_FOUND -> REPLACED_INACTIVE -> PRE_EXISTING_ACTIVE.
4. Writes work_queue_master.xlsx (work_queue + verified_active_reference sheets) and work_queue_master.csv.
With --include-verified, VERIFIED_ACTIVE rows stay in the queue (priority 5) and only
work_queue_all.csv is written (for re-checking every row, e.g. the GCP run).
Source CSVs are never modified.
"""
from __future__ import annotations

import argparse
import csv
import logging
from collections import Counter
from datetime import datetime
from pathlib import Path

import openpyxl

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("build_work_queue")

FINAL_CSV = Path("SUPER_MERGED_MASTER_FINAL_POPULATED.xlsx - final super merged master.csv")
RAW_CSV = Path("SUPER_MERGED_MASTER_FINAL_POPULATED.xlsx - raw Super Merged Master .csv")
OUT_XLSX = Path("work_queue_master.xlsx")
OUT_CSV = Path("work_queue_master.csv")
ALL_CSV = Path("work_queue_all.csv")

RAW_COLUMNS = {"status_normalised": "registry_status_normalised", "status": "registry_status",
               "registered_address": "registry_address"}
PRIORITY = {"UNVERIFIED_INACTIVE": 1, "NOT_FOUND": 2, "REPLACED_INACTIVE": 3, "PRE_EXISTING_ACTIVE": 4}
SKIP_STATUS = "VERIFIED_ACTIVE"
VERIFIED_PRIORITY = 5


def load(path: Path) -> tuple[list[str], list[dict]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--include-verified", action="store_true",
                        help="keep VERIFIED_ACTIVE rows in the queue and write work_queue_all.csv only")
    args = parser.parse_args()
    csv.field_size_limit(1 << 30)
    final_cols, final_rows = load(FINAL_CSV)
    _, raw_rows = load(RAW_CSV)
    LOG.info("Loaded %d final rows and %d raw rows", len(final_rows), len(raw_rows))

    if len(final_rows) != len(raw_rows):
        raise ValueError(f"Row count mismatch: final={len(final_rows)} raw={len(raw_rows)}")
    misaligned = [i for i, (f, r) in enumerate(zip(final_rows, raw_rows), 2)
                  if (f["original_company_name"] or "").strip() != (r["original_company_name"] or "").strip()]
    if misaligned:
        raise ValueError(f"{len(misaligned)} rows are misaligned on original_company_name, e.g. CSV rows {misaligned[:5]}")

    unknown = Counter(f["Domain Status"] for f in final_rows if f["Domain Status"] not in PRIORITY and f["Domain Status"] != SKIP_STATUS)
    if unknown:
        raise ValueError(f"Unexpected Domain Status values: {dict(unknown)}")

    work, verified = [], []
    for row_number, (f, r) in enumerate(zip(final_rows, raw_rows), 2):
        out = {"source_row_number": row_number, **f}
        for src, dst in RAW_COLUMNS.items():
            out[dst] = r.get(src, "")
        status = f["Domain Status"]
        if status == SKIP_STATUS and args.include_verified:
            out["priority"] = VERIFIED_PRIORITY
            work.append(out)
        elif status == SKIP_STATUS:
            verified.append(out)
        else:
            out["priority"] = PRIORITY[status]
            work.append(out)

    work.sort(key=lambda x: x["priority"])  # stable: original order kept within each group

    work_cols = ["priority", "source_row_number", *final_cols, *RAW_COLUMNS.values()]
    ref_cols = ["source_row_number", *final_cols, *RAW_COLUMNS.values()]

    out_csv = ALL_CSV if args.include_verified else OUT_CSV
    LOG.info("Writing %s...", out_csv)
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=work_cols)
        writer.writeheader()
        writer.writerows(work)
    if args.include_verified:
        LOG.info("Queue rows: %d %s", len(work), dict(Counter(r["Domain Status"] for r in work)))
        return

    LOG.info("Writing %s...", OUT_XLSX)
    wb = openpyxl.Workbook(write_only=True)
    for title, cols, rows in (("work_queue", work_cols, work), ("verified_active_reference", ref_cols, verified)):
        ws = wb.create_sheet(title)
        ws.append(cols)
        for row in rows:
            ws.append([row.get(c, "") for c in cols])
    try:
        wb.save(OUT_XLSX)
    except PermissionError:  # open in Excel
        fallback = OUT_XLSX.with_name(f"{OUT_XLSX.stem}_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
        wb.save(fallback)
        LOG.warning("%s is open in another program; saved %s instead", OUT_XLSX, fallback)

    LOG.info("Work queue rows: %d | Verified-active reference rows: %d", len(work), len(verified))
    LOG.info("Per-priority counts: %s", dict(Counter(r["Domain Status"] for r in work)))


if __name__ == "__main__":
    main()
