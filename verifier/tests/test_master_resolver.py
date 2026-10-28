"""Master resolver: never counts unproven rows as active, never auto-replaces a live unproven
site, writes new files only (review #19, #22; framework #12, #15, 2.5, 2.6, 2.7)."""
import asyncio
import csv
import tempfile
import unittest
from pathlib import Path

from verifier.extractor import extract_page
from verifier.master_resolver import consistency_report, resolve_row, same_site, write_outputs
from verifier.models import DomainRecord
from verifier.normalization import registered_domain

FILLER = "<p>" + "We deliver quality products and services to our customers every day. " * 6 + "</p>"


def record(host, html=None, dns="RESOLVED", status=200, live_host=None):
    pages = [extract_page(f"https://{host}/", html, status)] if html else []
    return DomainRecord(host, registered_domain(host), f"https://{host}/", final_url=f"https://{host}/" if html else "",
                        final_registered_domain=registered_domain(host) if html else "", http_status=status if html else 0,
                        dns_status=dns, pages=pages, live_host=live_host or host)


class FakeDiscoverer:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def discover(self, row, **kwargs):
        self.calls.append(kwargs)
        return self.result


ACCEPTED = {"decision": "ACCEPT", "outcome": "VERIFIED", "reason": "Site proves the company.", "answer_type": "OWN_SITE",
            "found_via": "SEARCH", "proof_level": "STRONG", "new_domain": "acme-new.co.jp",
            "proofs": [{"type": "legal_name_footer", "level": "STRONG", "text": "Acme Co., Ltd.", "url": "https://acme-new.co.jp/",
                        "is_name": True, "is_country": False}], "candidates_judged": ["acme-new.co.jp:VERIFIED"]}


def queue_row(domain, status="UNVERIFIED_INACTIVE", number=10, legacy=""):
    return {"priority": "1", "source_row_number": str(number), "original_company_name": "ACME CO., LTD.",
            "country code": "JP", "Sales Territory Name": "ACME CO., LTD. - JP", "Domain URL": domain,
            "Original Legacy Domain": legacy, "Domain Status": status, "registry_status_normalised": "ACTIVE"}


class UntrustedIdTests(unittest.TestCase):
    def test_shared_and_placeholder_ids_are_not_used(self):
        from verifier.master_resolver import mark_untrusted_ids, untrusted_ids
        from verifier.decision import row_context
        rows = [{"tax_id": "1234567890123", "country code": "JP", "original_company_name": "AIR WATER MEDICAL INC."},
                {"tax_id": "1234567890123", "country code": "JP", "original_company_name": "AIR WATER SERVICE K.K."},
                {"tax_id": "9876543210123", "country code": "JP", "original_company_name": "ACME CO LTD"},
                {"tax_id": "9876543210123", "country code": "JP", "original_company_name": "ACME CO., LTD."},
                {"tax_id": "0000000000", "country code": "KR", "original_company_name": "X CORP"}]
        self.assertEqual(mark_untrusted_ids(rows, untrusted_ids(rows)), 3)
        self.assertEqual(row_context(rows[0])["tax_id"], "")           # parent's number on a subsidiary
        self.assertEqual(row_context(rows[2])["tax_id"], "9876543210123")  # same company, two spellings
        self.assertEqual(row_context(rows[4])["tax_id"], "")           # placeholder


class ResolverTests(unittest.TestCase):
    def resolve(self, row, inspect_map, found=ACCEPTED):
        async def inspect(host):
            return inspect_map[host]
        discoverer = FakeDiscoverer(found)
        return asyncio.run(resolve_row(row, inspect, discoverer, "test")), discoverer

    def test_dead_domain_replaced_only_with_proven_site(self):
        result, discoverer = self.resolve(queue_row("acme-old.co.jp"), {"acme-old.co.jp": record("acme-old.co.jp", dns="FAILED")})
        self.assertEqual((result["decision"], result["outcome"]), ("ACCEPT", "REPLACED"))
        self.assertEqual(result["new_domain_url"], "https://acme-new.co.jp")
        self.assertTrue(result["domain_changed"])
        self.assertTrue(discoverer.calls[0]["wayback"])
        self.assertEqual(discoverer.calls[0]["exclude_hosts"], ("acme-old.co.jp",))

    def test_verified_supplied_domain_is_kept_without_search(self):
        html = "<title>Acme</title>" + FILLER + "<footer>© Acme Co., Ltd.</footer>"
        result, discoverer = self.resolve(queue_row("acme.co.jp", "PRE_EXISTING_ACTIVE"), {"acme.co.jp": record("acme.co.jp", html)})
        self.assertEqual((result["decision"], result["new_domain_url"], result["domain_changed"]), ("ACCEPT", "https://acme.co.jp", False))
        self.assertEqual(discoverer.calls, [])

    def test_geo_redirect_to_own_subdomain_keeps_supplied_domain(self):
        # mizuno.com answering from usa.mizuno.com is the same company domain, not a replacement
        html = "<title>Acme</title>" + FILLER + "<footer>© Acme Co., Ltd.</footer>"
        live = record("usa.acme.co.jp", html)
        result, _ = self.resolve(queue_row("acme.co.jp", "PRE_EXISTING_ACTIVE"), {"acme.co.jp": live})
        self.assertEqual((result["decision"], result["new_domain_url"], result["domain_changed"]),
                         ("ACCEPT", "https://acme.co.jp", False))

    def test_same_site(self):
        self.assertTrue(same_site("usa.mizuno.com", "mizuno.com"))
        self.assertTrue(same_site("jr-central.co.jp", "global.jr-central.co.jp"))
        self.assertTrue(same_site("www.acme.com", "acme.com"))
        self.assertFalse(same_site("a.wixsite.com", "b.wixsite.com"))
        self.assertFalse(same_site("notmizuno.com", "mizuno.com"))
        self.assertFalse(same_site("", "mizuno.com"))

    def test_live_unproven_site_gets_suggestion_not_replacement(self):
        html = "<title>アクメ株式会社</title>" + FILLER
        result, _ = self.resolve(queue_row("acme.co.jp", "PRE_EXISTING_ACTIVE"), {"acme.co.jp": record("acme.co.jp", html)})
        self.assertEqual((result["decision"], result["outcome"]), ("REVIEW", "REPLACEMENT_SUGGESTED"))
        self.assertEqual(result["suggested_domain"], "acme-new.co.jp")
        self.assertEqual(result["new_domain_url"], "")

    def test_www_variant_still_needs_ownership_proof(self):
        rec = record("acme.co.jp", "<title>Parked placeholder</title>" + FILLER, live_host="www.acme.co.jp")
        not_found = {"decision": "REVIEW", "outcome": "NOT_FOUND", "reason": "No candidate.", "candidates_judged": []}
        result, _ = self.resolve(queue_row("acme.co.jp"), {"acme.co.jp": rec}, found=not_found)
        self.assertNotEqual(result["decision"], "ACCEPT")

    def test_blank_domain_goes_to_search(self):
        result, discoverer = self.resolve(queue_row("", "NOT_FOUND"), {})
        self.assertEqual((result["decision"], result["outcome"]), ("ACCEPT", "FOUND"))
        self.assertEqual(result["supplied_domain_outcome"], "NO_DOMAIN_SUPPLIED")
        self.assertEqual(len(discoverer.calls), 1)
        self.assertEqual(discoverer.calls[0]["legacy_domain"], "")  # nothing to look up in the archive

    def test_outputs_overrides_and_consistency(self):
        rows = [queue_row("acme.co.jp", number=n) for n in (10, 11)]
        rows[1]["original_company_name"] = "ACME CO., LTD."
        results = {10: {"source_row_number": 10, "decision": "ACCEPT", "outcome": "VERIFIED", "new_domain_url": "https://acme.co.jp",
                        "domain_changed": False, "proof_level": "STRONG", "answer_type": "OWN_SITE", "found_via": "SUPPLIED"},
                   11: {"source_row_number": 11, "decision": "ACCEPT", "outcome": "REPLACED", "new_domain_url": "https://acme2.co.jp",
                        "domain_changed": True, "proof_level": "STRONG", "answer_type": "OWN_SITE", "found_via": "SEARCH"}}
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            summary = write_outputs(rows, results, {11: {"source_row_number": "11", "decision": "REJECT", "domain": "", "note": "wrong"}}, out)
            with open(out / "resolved_master.csv", encoding="utf-8-sig") as f:
                written = list(csv.DictReader(f))
            self.assertEqual(written[1]["outcome"], "REVIEWER_OVERRIDE")
            self.assertEqual(summary["reviewer_overrides_applied"], 1)
            self.assertTrue((out / "review_queue.csv").exists())
        flags = consistency_report([{**rows[0], **results[10]}, {**rows[1], **results[11]}])
        self.assertEqual(flags[0]["issue"], "SAME_COMPANY_DIFFERENT_SITES")
        a = {**queue_row("x", number=20), "original_company_name": "AIR WATER MEDICAL INC.", **results[10], "new_domain_url": "https://awb.co.jp"}
        b = {**queue_row("x", number=21), "original_company_name": "AIR WATER SERVICE K.K.", **results[10], "new_domain_url": "https://awb.co.jp"}
        self.assertIn("ONE_SITE_OWN_SITE_FOR_SEVERAL_COMPANIES", [f["issue"] for f in consistency_report([a, b])])


if __name__ == "__main__":
    unittest.main()
