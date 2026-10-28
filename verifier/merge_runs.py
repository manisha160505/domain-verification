"""Merge the results of several runs (e.g. one per machine/shard) into one final sheet.

    python -m verifier.merge_runs --states azure_runs/full_run/shard_*/resolver_state.sqlite \
        azure_runs/test_80rows_rejudged_with_fixes/resolver_state.sqlite --out azure_runs/full_run/final

Every row of the full queue appears once. Rows from groups that were not processed
(--not-processed, default PRE_EXISTING_ACTIVE) keep their domain and are marked NOT_PROCESSED.
A row resolved in two different runs is an error unless both results are identical.
"""
from __future__ import annotations

import argparse
import glob
import json
import sqlite3
from pathlib import Path

import csv

from .master_resolver import OUTPUT_COLUMNS, _save_workbook, excel_cell, apply_override, load_calibration, load_overrides, load_queue, write_outputs

# Final status names and their order in the sorted sheet.
FINAL_ORDER = ["VERIFIED_ACTIVE", "REPLACED_ACTIVE", "MERGED_COMPANY", "COMPANY_CLOSED", "NEEDS_REVIEW",
               "NOT_FOUND", "BLOCKED"]
UNREACHABLE_OUTCOMES = {"TEMP_UNREACHABLE", "BLOCKED", "UNREADABLE"}
BLOCKED_DETAIL = {"BLOCKED": "access denied to automated visitors (HTTP 403/429)",
                  "TEMP_UNREACHABLE": "timeout / no connection", "UNREADABLE": "page loads but is nearly empty"}


def final_status(result: dict) -> tuple[str, str]:
    """(final status, detail) for one resolved row."""
    decision, outcome = result.get("decision"), result.get("outcome")
    if outcome in UNREACHABLE_OUTCOMES:
        return "BLOCKED", BLOCKED_DETAIL[outcome]
    if decision == "ACCEPT":
        if outcome == "MERGED_INTO":
            return "MERGED_COMPANY", f"merged into {result.get('successor', '')}".strip()
        if outcome == "FOUND":
            return "VERIFIED_ACTIVE", "found (no domain before)"
        if result.get("domain_changed"):
            return "REPLACED_ACTIVE", "replaced (old domain dead or wrong)"
        return "VERIFIED_ACTIVE", "existing domain confirmed"
    if outcome == "COMPANY_CLOSED":
        return "COMPANY_CLOSED", "official register: closed/merged"
    if outcome == "NOT_FOUND" or decision == "REJECT":
        return "NOT_FOUND", outcome or ""
    return "NEEDS_REVIEW", outcome or ""


def confidence(status: str) -> str:
    """'LOW CONFIDENCE' for rows a person still has to check (NEEDS_REVIEW); blank otherwise."""
    return "LOW CONFIDENCE" if status == "NEEDS_REVIEW" else ""


# Columns of the simple final sheet (source column -> sheet heading).
SIMPLE_COLUMNS = [("Country (Group)", "Country (Group)"), ("country code", "country code"),
                  ("Sales Territory ID", "Sales Territory ID"), ("Sales Territory Name", "Sales Territory Name"),
                  ("Organization ID ", "Organization ID"), ("Organization Name", "Organization Name"),
                  ("original_company_name", "original_company_name"), ("registration_number", "registration_number"),
                  ("tax_id", "tax_id"), ("Domain URL", "Old URL Provided"),
                  ("Final Domain Status", "Final Domain Status"), ("Confidence", "Confidence"),
                  ("Final Domain URL", "Final Domain URL"), ("suggested_domain", "Suggested Domain")]


def write_simple_sheet(rows: list[dict], counts: dict, out: Path) -> Path:
    """final_sorted.xlsx/.csv with only the columns people need; the full detail stays in
    final_sorted_detailed.*"""
    headings = [h for _, h in SIMPLE_COLUMNS]
    simple = [{h: r.get(src, "") for src, h in SIMPLE_COLUMNS} for r in rows]
    with open(out / "final_sorted.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=headings)
        writer.writeheader()
        writer.writerows(simple)
    import openpyxl
    book = openpyxl.Workbook(write_only=True)
    sheet = book.create_sheet("final_sorted")
    sheet.append(headings)
    for r in simple:
        sheet.append([excel_cell(r[h]) for h in headings])
    summary = book.create_sheet("summary")
    summary.append(["Final Domain Status", "Rows"])
    for name in FINAL_ORDER:
        summary.append([name, counts[name]])
    summary.append(["TOTAL", len(rows)])
    return _save_workbook(book, out / "final_sorted.xlsx")


def write_final_sorted(queue: list[dict], results: dict[int, dict], out: Path, overrides: dict | None = None) -> dict:
    """Only the processed rows, with Final Domain Status / Final Domain URL, sorted by FINAL_ORDER."""
    by_number = {int(r["source_row_number"]): r for r in queue}
    rows = []
    for number, result in results.items():
        row = by_number.get(number)
        if row is None:
            continue
        if overrides and number in overrides:
            result = apply_override(result, overrides[number])
        status, detail = final_status(result)
        final_url = result.get("new_domain_url", "") if result.get("decision") == "ACCEPT" else ""
        rows.append({**row, "Final Domain Status": status, "Confidence": confidence(status),
                     "Status Detail": detail, "Final Domain URL": final_url,
                     **{k: result.get(k, "") for k in OUTPUT_COLUMNS + ["suggested_domain"]}})
    order = {name: i for i, name in enumerate(FINAL_ORDER)}
    rows.sort(key=lambda r: (order[r["Final Domain Status"]], int(r.get("priority") or 9), int(r["source_row_number"])))
    # new_domain_url is identical to Final Domain URL, so it is left out of the final sheet.
    columns = list(queue[0].keys()) + ["Final Domain Status", "Confidence", "Status Detail", "Final Domain URL"] +         [c for c in OUTPUT_COLUMNS + ["suggested_domain"] if c not in queue[0] and c != "new_domain_url"]
    out.mkdir(parents=True, exist_ok=True)
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
    counts = {name: sum(r["Final Domain Status"] == name for r in rows) for name in FINAL_ORDER}
    summary_sheet = book.create_sheet("summary")
    summary_sheet.append(["Final Domain Status", "Rows"])
    for name in FINAL_ORDER:
        summary_sheet.append([name, counts[name]])
    summary_sheet.append(["TOTAL", len(rows)])
    saved = _save_workbook(book, out / "final_sorted_detailed.xlsx")  # dated copy if the file is open in Excel
    print(f"detailed sheet saved: {saved}")
    print(f"simple sheet saved: {write_simple_sheet(rows, counts, out)}")
    (out / "final_summary.json").write_text(json.dumps({"rows": len(rows), "final_status": counts}, indent=2), encoding="utf-8")
    return {"rows": len(rows), "final_status": counts}


def load_states(paths: list[str], override_paths: list[str] | None = None) -> dict[int, dict]:
    """Results from several runs. Rows from override_paths (e.g. a retry pass) replace earlier ones."""
    merged = _load(paths)
    for path in override_paths or []:
        db = sqlite3.connect(path)
        try:
            for number, payload in db.execute("SELECT source_row_number, payload FROM results"):
                new = json.loads(payload)
                old = merged.get(number)
                # A retry that is still unreachable never replaces a result that got through.
                if old is not None and new.get("outcome") in UNREACHABLE_OUTCOMES                         and old.get("outcome") not in UNREACHABLE_OUTCOMES:
                    continue
                merged[number] = new
        finally:
            db.close()
        print(f"{path}: applied as override")
    return merged


def _load(paths: list[str]) -> dict[int, dict]:
    merged: dict[int, dict] = {}
    sources: dict[int, str] = {}
    conflicts = []
    for path in paths:
        db = sqlite3.connect(path)
        try:
            for number, payload in db.execute("SELECT source_row_number, payload FROM results"):
                result = json.loads(payload)
                if number in merged and merged[number] != result:
                    conflicts.append((number, sources[number], path))
                merged[number] = result
                sources[number] = path
        finally:
            db.close()
        print(f"{path}: loaded")
    if conflicts:
        raise SystemExit(f"{len(conflicts)} rows were resolved differently in two runs, e.g. {conflicts[:3]}")
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge several resolver runs into one sheet")
    parser.add_argument("--states", nargs="+", required=True, help="resolver_state.sqlite files (globs allowed)")
    parser.add_argument("--input", default="work_queue_master.csv", help="the full queue")
    parser.add_argument("--out", default="azure_runs/full_run/final")
    parser.add_argument("--not-processed", default="PRE_EXISTING_ACTIVE")
    parser.add_argument("--retry-states", nargs="*", default=[],
                        help="state files of a retry pass; their rows replace the earlier results")
    parser.add_argument("--overrides")
    parser.add_argument("--calibration")
    args = parser.parse_args()
    paths = sorted({p for pattern in args.states for p in (glob.glob(pattern) or [pattern])})
    retry = sorted({p for pattern in args.retry_states for p in (glob.glob(pattern) or [pattern])})
    results = load_states(paths, retry)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary = write_outputs(load_queue(Path(args.input), None), results, load_overrides(args.overrides), out,
                            input_path=Path(args.input), calibration=load_calibration(args.calibration),
                            not_processed=set(filter(None, args.not_processed.split(","))))
    print(json.dumps({k: summary.get(k) for k in ("rows_in_queue", "rows_resolved", "updated_domain_status")}, indent=2))
    final = write_final_sorted(load_queue(Path(args.input), None), results, out, load_overrides(args.overrides))
    print("final sorted sheet:", json.dumps(final, indent=2))


if __name__ == "__main__":
    main()
