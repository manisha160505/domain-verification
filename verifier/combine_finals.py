"""Combine finished final sheets (e.g. the Azure run and the GCP run) into one.

    python -m verifier.combine_finals --inputs azure_runs/full_run/final gcp_runs/gcp_run/final \
        --out final_combined

Reads each folder's final_sorted_detailed.csv (written by merge_runs), refuses a row that
appears in two inputs, re-sorts everything by the final status order, and writes the same
pair of sheets as merge_runs: final_sorted.* (the short columns + summary tab) and
final_sorted_detailed.* (every column). --simple-only rewrites just the short sheet, e.g. to
refresh one run's folder after the short columns changed.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from .master_resolver import _save_workbook, excel_cell
from .merge_runs import FINAL_ORDER, write_simple_sheet


def load_final(folder: Path) -> tuple[list[str], list[dict]]:
    with open(folder / "final_sorted_detailed.csv", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def combine(inputs: list[Path], out: Path, simple_only: bool = False) -> dict:
    csv.field_size_limit(1 << 30)
    columns: list[str] = []
    rows: list[dict] = []
    seen: dict[int, Path] = {}
    for folder in inputs:
        cols, part = load_final(folder)
        columns += [c for c in cols if c not in columns]
        for row in part:
            number = int(row["source_row_number"])
            if number in seen:
                raise ValueError(f"row {number} is in both {seen[number]} and {folder}")
            seen[number] = folder
        rows += part
        print(f"{folder}: {len(part)} rows")
    order = {name: i for i, name in enumerate(FINAL_ORDER)}
    rows.sort(key=lambda r: (order[r["Final Domain Status"]], int(r.get("priority") or 9), int(r["source_row_number"])))
    counts = {name: sum(r["Final Domain Status"] == name for r in rows) for name in FINAL_ORDER}
    out.mkdir(parents=True, exist_ok=True)
    if not simple_only:
        with open(out / "final_sorted_detailed.csv", "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        import openpyxl
        book = openpyxl.Workbook(write_only=True)
        sheet = book.create_sheet("final_sorted")
        sheet.append(columns)
        for r in rows:
            sheet.append([excel_cell(r.get(c, "")) for c in columns])
        summary = book.create_sheet("summary")
        summary.append(["Final Domain Status", "Rows"])
        for name in FINAL_ORDER:
            summary.append([name, counts[name]])
        summary.append(["TOTAL", len(rows)])
        print(f"detailed sheet saved: {_save_workbook(book, out / 'final_sorted_detailed.xlsx')}")
    print(f"simple sheet saved: {write_simple_sheet(rows, counts, out)}")
    result = {"rows": len(rows), "inputs": [str(p) for p in inputs], "final_status": counts}
    (out / "final_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Combine finished final sheets into one")
    parser.add_argument("--inputs", nargs="+", required=True, help="folders holding final_sorted_detailed.csv")
    parser.add_argument("--out", required=True)
    parser.add_argument("--simple-only", action="store_true", help="only rewrite final_sorted.csv/.xlsx")
    args = parser.parse_args()
    print(json.dumps(combine([Path(p) for p in args.inputs], Path(args.out), args.simple_only), indent=2))


if __name__ == "__main__":
    main()
