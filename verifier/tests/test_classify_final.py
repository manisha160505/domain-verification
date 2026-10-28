"""Final RESOLVED / UNRESOLVED classification."""
import unittest

from verifier import classify_final as cf

COLUMNS = ["original_company_name", "Old URL Provided", "Final Domain Status", "Confidence", "Final Domain URL",
           "Suggested Domain"]


def row(status, suggested="", final="", old="old.jp", name="X"):
    return {"original_company_name": name, "Old URL Provided": old, "Final Domain Status": status,
            "Confidence": "LOW CONFIDENCE" if status == "NEEDS_REVIEW" else "", "Final Domain URL": final,
            "Suggested Domain": suggested}


class ClassifyFinal(unittest.TestCase):
    def test_every_status_maps_to_a_class(self):
        cases = {"VERIFIED_ACTIVE": ("RESOLVED", "VERIFIED_ACTIVE"), "REPLACED_ACTIVE": ("RESOLVED", "REPLACED_ACTIVE"),
                 "COMPANY_CLOSED": ("RESOLVED", "COMPANY_CLOSED"), "MERGED_COMPANY": ("RESOLVED", "COMPANY_MERGED"),
                 "NOT_FOUND": ("RESOLVED", "NOT_FOUND_IN_SEARCH"), "BLOCKED": ("UNRESOLVED", "BLOCKED")}
        for status, expected in cases.items():
            self.assertEqual(cf.classify(row(status)), expected, status)
        with self.assertRaises(ValueError):
            cf.classify(row("SOMETHING_ELSE"))

    def test_needs_review_splits_on_suggested_domain(self):
        promoted = cf.classified_row(row("NEEDS_REVIEW", suggested="www.acme.co.jp/en"))
        self.assertEqual((promoted["Super Class"], promoted["Final Domain Status"], promoted["Final Domain URL"],
                          promoted["Suggested Domain"]),
                         ("RESOLVED", "REPLACED_ACTIVE", "https://acme.co.jp", ""))  # one domain: the final one
        low = cf.classified_row(row("NEEDS_REVIEW"))
        self.assertEqual((low["Super Class"], low["Final Domain Status"], low["Final Domain URL"], low["Suggested Domain"],
                          low["Old URL Provided"]),
                         ("UNRESOLVED", cf.LOW_EVIDENCE, "", "same as the old url provided", "old.jp"))
        self.assertNotIn("Confidence", promoted)
        self.assertNotIn("Confidence", low)

    def test_suggested_column_only_marks_low_evidence_rows(self):
        kept = cf.classified_row(row("REPLACED_ACTIVE", final="https://new.jp"))
        self.assertEqual((kept["Final Domain URL"], kept["Suggested Domain"]), ("https://new.jp", ""))
        for status in ("NOT_FOUND", "BLOCKED", "VERIFIED_ACTIVE"):  # old weak leads are cleared
            self.assertEqual(cf.classified_row(row(status, suggested="lead.jp"))["Suggested Domain"], "", status)

    def test_columns_and_sort_order(self):
        rows = [row("BLOCKED", name="b"), row("NEEDS_REVIEW", name="low"), row("NOT_FOUND", name="nf"),
                row("NEEDS_REVIEW", suggested="s.jp", name="sugg"), row("VERIFIED_ACTIVE", name="v"),
                row("MERGED_COMPANY", name="m"), row("REPLACED_ACTIVE", final="https://r.jp", name="r"),
                row("COMPANY_CLOSED", name="c")]
        columns, out = cf.build(rows, COLUMNS)
        self.assertEqual(columns, ["original_company_name", "Old URL Provided", "Super Class", "Final Domain Status",
                                   "Final Domain URL", "Suggested Domain"])  # no Confidence column
        self.assertEqual([r["original_company_name"] for r in out], ["v", "sugg", "r", "c", "m", "nf", "b", "low"])
        self.assertEqual([r["Super Class"] for r in out], ["RESOLVED"] * 6 + ["UNRESOLVED"] * 2)


if __name__ == "__main__":
    unittest.main()
