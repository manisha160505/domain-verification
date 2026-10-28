"""Merging several runs into one final sheet."""
import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from verifier.master_resolver import load_queue, write_outputs
from verifier.merge_runs import load_states


def state(path: Path, results: dict) -> str:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE results (source_row_number INTEGER PRIMARY KEY, payload TEXT, finished_at TEXT, worker_id INTEGER)")
    for number, result in results.items():
        db.execute("INSERT INTO results VALUES (?,?,?,?)", (number, json.dumps(result), "", 0))
    db.commit()
    db.close()
    return str(path)


class MergeTests(unittest.TestCase):
    def test_merge_marks_unprocessed_groups_and_rejects_conflicts(self):
        accepted = {"decision": "ACCEPT", "outcome": "REPLACED", "new_domain_url": "https://a.jp", "domain_changed": True,
                    "proof_level": "STRONG"}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            d = Path(d)
            with open(d / "queue.csv", "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=["priority", "source_row_number", "original_company_name", "Domain Status"])
                w.writeheader()
                w.writerows([{"priority": 1, "source_row_number": 10, "original_company_name": "A", "Domain Status": "NOT_FOUND"},
                             {"priority": 2, "source_row_number": 11, "original_company_name": "B", "Domain Status": "NOT_FOUND"},
                             {"priority": 4, "source_row_number": 12, "original_company_name": "C", "Domain Status": "PRE_EXISTING_ACTIVE"}])
            s1 = state(d / "s1.sqlite", {10: accepted})
            s2 = state(d / "s2.sqlite", {11: {**accepted, "decision": "REVIEW", "outcome": "NOT_FOUND", "new_domain_url": ""}})
            results = load_states([s1, s2])
            summary = write_outputs(load_queue(d / "queue.csv", None), results, {}, d, excel=False,
                                    not_processed={"PRE_EXISTING_ACTIVE"})
            self.assertEqual(summary["updated_domain_status"], {"REPLACED_VERIFIED": 1, "NOT_FOUND": 1, "NOT_PROCESSED": 1})
            s3 = state(d / "s3.sqlite", {10: {**accepted, "new_domain_url": "https://other.jp"}})
            with self.assertRaises(SystemExit):
                load_states([s1, s3])


class FinalSheetTests(unittest.TestCase):
    def test_status_names_and_sort_order(self):
        from verifier.merge_runs import final_status, write_final_sorted
        cases = {
            1: ({"decision": "ACCEPT", "outcome": "VERIFIED", "domain_changed": False, "new_domain_url": "https://a.jp"}, "VERIFIED_ACTIVE"),
            2: ({"decision": "ACCEPT", "outcome": "FOUND", "domain_changed": True, "new_domain_url": "https://b.jp"}, "VERIFIED_ACTIVE"),
            3: ({"decision": "ACCEPT", "outcome": "REPLACED", "domain_changed": True, "new_domain_url": "https://c.jp"}, "REPLACED_ACTIVE"),
            4: ({"decision": "ACCEPT", "outcome": "MERGED_INTO", "domain_changed": True, "successor": "X"}, "MERGED_COMPANY"),
            5: ({"decision": "REJECT", "outcome": "COMPANY_CLOSED"}, "COMPANY_CLOSED"),
            6: ({"decision": "REVIEW", "outcome": "REPLACEMENT_SUGGESTED"}, "NEEDS_REVIEW"),
            7: ({"decision": "REVIEW", "outcome": "NOT_FOUND"}, "NOT_FOUND"),
            8: ({"decision": "REVIEW", "outcome": "TEMP_UNREACHABLE"}, "BLOCKED"),
        }
        for number, (result, expected) in cases.items():
            self.assertEqual(final_status(result)[0], expected, number)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            d = Path(d)
            queue = [{"priority": "1", "source_row_number": str(n), "original_company_name": f"C{n}", "Domain URL": ""}
                     for n in (8, 7, 6, 5, 4, 3, 2, 1)]
            summary = write_final_sorted(queue, {n: r for n, (r, _) in cases.items()}, d)
            self.assertEqual(summary["rows"], 8)
            with open(d / "final_sorted.csv", encoding="utf-8-sig") as f:
                sheet = list(csv.DictReader(f))
            order = [r["Final Domain Status"] for r in sheet]
            self.assertEqual(order, ["VERIFIED_ACTIVE", "VERIFIED_ACTIVE", "REPLACED_ACTIVE", "MERGED_COMPANY",
                                     "COMPANY_CLOSED", "NEEDS_REVIEW", "NOT_FOUND", "BLOCKED"])
            # only NEEDS_REVIEW rows are flagged as low confidence; the column sits right after the status
            self.assertEqual([r["Confidence"] for r in sheet], ["", "", "", "", "", "LOW CONFIDENCE", "", ""])
            columns = list(sheet[0].keys())
            self.assertEqual(columns.index("Confidence"), columns.index("Final Domain Status") + 1)

    def test_excel_cell_removes_control_characters(self):
        from verifier.master_resolver import excel_cell
        dirty = "黒崎播磨" + chr(3) + "株式会社" + chr(31)
        self.assertEqual(excel_cell(dirty), "黒崎播磨株式会社")
        kept = "tab" + chr(9) + "and" + chr(10) + "newline"
        self.assertEqual(excel_cell(kept), kept)
        self.assertEqual(excel_cell(["a", "b"]), "['a', 'b']")
        self.assertEqual(excel_cell(5), 5)

    def test_retry_results_replace_earlier_ones(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            d = Path(d)
            first = state(d / "a.sqlite", {5: {"decision": "REVIEW", "outcome": "TEMP_UNREACHABLE"}})
            retry = state(d / "r.sqlite", {5: {"decision": "ACCEPT", "outcome": "VERIFIED"}})
            self.assertEqual(load_states([first], [retry])[5]["outcome"], "VERIFIED")
            still = state(d / "s.sqlite", {5: {"decision": "REVIEW", "outcome": "BLOCKED"}})
            self.assertEqual(load_states([first], [retry, still])[5]["outcome"], "VERIFIED")  # later failure doesn't undo it

    def test_combine_runs_into_one_sorted_sheet_with_original_url(self):
        from verifier.combine_finals import combine
        from verifier.merge_runs import write_final_sorted
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            d = Path(d)
            run_a = [{"priority": "1", "source_row_number": "1", "original_company_name": "A", "Domain URL": "a-old.jp"}]
            run_b = [{"priority": "4", "source_row_number": "2", "original_company_name": "B", "Domain URL": "b.jp"}]
            write_final_sorted(run_a, {1: {"decision": "REVIEW", "outcome": "NO_PROOF"}}, d / "a")
            write_final_sorted(run_b, {2: {"decision": "ACCEPT", "outcome": "VERIFIED", "new_domain_url": "https://b.jp"}}, d / "b")
            summary = combine([d / "a", d / "b"], d / "all")
            self.assertEqual(summary["rows"], 2)
            with open(d / "all" / "final_sorted.csv", encoding="utf-8-sig") as f:
                sheet = list(csv.DictReader(f))
            # sorted by status across both runs; the supplied URL is kept even when nothing was proven
            self.assertEqual([(r["original_company_name"], r["Final Domain Status"], r["Confidence"], r["Old URL Provided"])
                              for r in sheet],
                             [("B", "VERIFIED_ACTIVE", "", "b.jp"), ("A", "NEEDS_REVIEW", "LOW CONFIDENCE", "a-old.jp")])
            with self.assertRaises(ValueError):  # the same row in two runs is refused
                combine([d / "a", d / "a"], d / "dup")


if __name__ == "__main__":
    unittest.main()
