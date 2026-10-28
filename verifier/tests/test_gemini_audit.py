"""Gemini accuracy audit: comparison, scoring, tier rules and statistics (offline, fake agent)."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from verifier import gemini_audit as ga

ROW = {"source_row_number": "7", "country code": "JP", "Organization Name": "ACME CO., LTD.",
       "original_company_name": "ACME CO., LTD.", "Sales Territory Name": "ACME GROUP - JP",
       "registration_number": "1234567890123", "tax_id": "1234567890123", "registry_address": "Tokyo",
       "suggested_domain": "https://www.acme.co.jp/en/", "Final Domain Status": "NEEDS_REVIEW"}


class FakeAgent:
    def __init__(self, answers):
        self.answers = answers  # variant -> answer dict
        self.calls = []

    async def ask(self, ctx, variant="standard"):
        self.calls.append(variant)
        answer = dict(self.answers.get(variant, {"domain": "", "evidence": [], "criteria": [], "local_name": ""}))
        answer.setdefault("local_name", "")
        return {**answer, "model_version": "fake-1", "attempts": 1}


class FakeChecker:
    def __init__(self, sites, pages=None, redirects=None):
        self.sites, self.pages, self.redirects = sites, pages or {}, redirects or {}

    async def resolve(self, url):
        return url

    async def page(self, url):
        return url, self.pages.get(url, "")

    async def site_text(self, domain):
        return self.redirects.get(domain, domain), self.sites.get(domain, "")


def judge(agent, checker):
    return asyncio.run(ga.judge_row(ROW, agent, checker, 1))


class CompareAndEvidence(unittest.TestCase):
    def test_registrable_domain_comparison(self):
        self.assertEqual(ga.reg_domain("https://www.acme.co.jp/en/index.html"), "acme.co.jp")
        self.assertEqual(ga.compare("shop.acme.co.jp", "https://www.acme.co.jp/"), "MATCH")
        self.assertEqual(ga.compare("acme.com", "acme.co.jp"), "MISMATCH")
        self.assertEqual(ga.compare("acme.com", "acme.co.jp", {"acme.co.jp": "acme.com"}), "MATCH")  # redirect
        self.assertEqual(ga.compare("", "acme.co.jp"), "MISMATCH")

    def test_quotes_and_ids_are_checked_on_the_page(self):
        page = "Welcome to  ACME Co., Ltd.\nCorporate number 1234-5678-90123 Tokyo"
        self.assertTrue(ga.quote_on_page("welcome to acme co., ltd.", page))
        self.assertFalse(ga.quote_on_page("made-up quote", page))
        self.assertTrue(ga.id_on_page("1234567890123", page))
        self.assertFalse(ga.id_on_page("1234567890124", page))
        self.assertFalse(ga.id_on_page("12345", page))  # too short to be an identifier

    def test_parse_agent_json(self):
        text = 'Here: ```json\n{"domain": "acme.co.jp", "evidence": [{"url": "https://acme.co.jp", "quote": "ACME"}], "criteria_met": ["country_matches"]}\n```'
        parsed = ga.parse_agent_json(text)
        self.assertEqual((parsed["domain"], len(parsed["evidence"]), parsed["criteria"]), ("acme.co.jp", 1, ["country_matches"]))
        self.assertEqual(ga.parse_agent_json("no json here")["domain"], "")
        self.assertEqual(ga.parse_agent_json('{"domain": "empty"}')["domain"], "")


class ContextAndScore(unittest.TestCase):
    def test_context_never_contains_the_mapped_domain(self):
        ctx = ga.build_context(ROW)
        self.assertEqual((ctx["name_source"], ctx["country"]), ("Organization Name + Original Company Name", "Japan"))
        self.assertNotIn("acme.co.jp", json.dumps(ctx) + ga.make_prompt(ctx))
        fallback = ga.build_context({**ROW, "Organization Name": ""})
        self.assertEqual((fallback["name_source"], fallback["names"][0]), ("Sales Territory Name", "ACME GROUP"))

    def test_score_points(self):
        ctx = ga.build_context(ROW)
        site = "ACME Co., Ltd. Corporate number 1234567890123 Tokyo, Japan"
        independent = [{"url": "https://www.houjin-bangou.nta.go.jp/x", "quote": "acme.co.jp", "verified": True,
                        "text": "ACME Co., Ltd. website acme.co.jp"}]
        score, items = ga.score_evidence(ctx, "acme.co.jp", site, independent, [])
        self.assertEqual(score, 1.0)
        self.assertTrue(all(items[k] for k in ("legal_name_or_id", "country", "independent_source", "unique")))
        score, _ = ga.score_evidence(ctx, "acme.com", "Some other company", [], [])
        self.assertEqual(score, 0.0)
        self.assertEqual([ga.tier(s) for s in (0.8, 0.6, 0.4, 0.2)], ["HIGH", "MEDIUM", "LOW", "NONE"])


class TierRules(unittest.TestCase):
    STRONG = "ACME Co., Ltd. corporate number 1234567890123, Tokyo Japan"

    def test_high_score_is_judged_match(self):
        agent = FakeAgent({"standard": {"domain": "acme.co.jp", "evidence": [], "criteria": []}})
        record = judge(agent, FakeChecker({"acme.co.jp": self.STRONG}))
        self.assertEqual((record["final_state"], record["confidence_tier"], agent.calls), ("JUDGED_MATCH", "HIGH", ["standard"]))

    def test_high_score_other_domain_is_mismatch(self):
        agent = FakeAgent({"standard": {"domain": "acme-global.com", "evidence": [], "criteria": []}})
        record = judge(agent, FakeChecker({"acme-global.com": self.STRONG}))
        self.assertEqual((record["final_state"], record["found_domain"]), ("JUDGED_MISMATCH", "acme-global.com"))

    def test_medium_needs_the_same_domain_twice(self):
        medium = "ACME Co., Ltd. Tokyo Japan"  # legal name + country = 0.60, no ID, no independent source
        agree = FakeAgent({"standard": {"domain": "acme.co.jp", "evidence": [], "criteria": []},
                           "alt": {"domain": "acme.co.jp", "evidence": [], "criteria": []}})
        record = judge(agree, FakeChecker({"acme.co.jp": medium}))
        self.assertEqual((record["final_state"], agree.calls), ("JUDGED_MATCH", ["standard", "alt"]))
        differ = FakeAgent({"standard": {"domain": "acme.co.jp", "evidence": [], "criteria": []},
                            "alt": {"domain": "other.jp", "evidence": [], "criteria": []}})
        record = judge(differ, FakeChecker({"acme.co.jp": medium}))
        self.assertEqual((record["final_state"], record["replaced_reason"]), ("REPLACED", "MEDIUM_NOT_CONFIRMED"))

    def test_no_domain_gets_one_deep_retry_then_replaced(self):
        agent = FakeAgent({})
        record = judge(agent, FakeChecker({}))
        self.assertEqual((record["final_state"], record["replaced_reason"], agent.calls),
                         ("REPLACED", "NO_DOMAIN_FOUND", ["standard", "deep"]))


class AllReviewPopulation(unittest.TestCase):
    def test_suggestion_first_then_old_url(self):
        with_suggestion = {**ROW, "suggested_domain": "acme.co.jp", "Domain URL": "old-acme.jp"}
        without = {**ROW, "suggested_domain": "", "Domain URL": "https://www.old-acme.jp/home"}
        self.assertEqual((ga.mapped_domain(with_suggestion), ga.mapped_source(with_suggestion)), ("acme.co.jp", "suggested"))
        self.assertEqual((ga.mapped_domain(without), ga.mapped_source(without)), ("old-acme.jp", "old_url"))
        record = judge(FakeAgent({"standard": {"domain": "old-acme.jp", "evidence": [], "criteria": []}}),
                       FakeChecker({"old-acme.jp": TierRules.STRONG}))
        self.assertEqual(record["mapped_source"], "suggested")  # ROW itself has a suggestion

    def test_population_kinds(self):
        rows = [{**ROW, "source_row_number": "1", "Domain URL": "a-old.jp"}, {**ROW, "source_row_number": "2", "suggested_domain": "", "Domain URL": "b.jp"},
                {**ROW, "source_row_number": "3", "suggested_domain": "", "Domain URL": ""},
                {**ROW, "source_row_number": "4", "Final Domain Status": "NOT_FOUND"}]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            path = Path(d) / "pop.csv"
            import csv
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
            self.assertEqual([r["source_row_number"] for r in ga.load_population(path, "suggested")], ["1"])
            self.assertEqual([r["source_row_number"] for r in ga.load_population(path, "all_review")], ["1", "2"])

    def test_reuse_needs_the_same_mapped_domain(self):
        earlier = {7: {"row_id": 7, "mapped_domain": "acme.co.jp", "final_state": "JUDGED_MATCH", "seed": 1}}
        reused = ga.reused_record(ROW, earlier, 2)
        self.assertEqual((reused["final_state"], reused["reused_from"], reused["seed"], reused["mapped_source"]),
                         ("JUDGED_MATCH", 1, 2, "suggested"))
        self.assertIsNone(ga.reused_record({**ROW, "suggested_domain": "other.jp"}, earlier, 2))
        failed = {7: {**earlier[7], "final_state": "REPLACED", "replaced_reason": "TECHNICAL_FAILURE"}}
        self.assertIsNone(ga.reused_record(ROW, failed, 2))  # technical failures are searched again

    def test_report_splits_by_source(self):
        records = ([{"final_state": "JUDGED_MATCH", "country": "JP", "mapped_domain": "a.jp", "mapped_source": "suggested"}] * 3
                   + [{"final_state": "JUDGED_MISMATCH", "country": "JP", "mapped_domain": "b.jp", "mapped_source": "old_url"}]
                   + [{"final_state": "REPLACED", "country": "JP", "mapped_source": "old_url", "replaced_reason": "LOW_CONFIDENCE"}])
        report = ga.build_report(records, [{"country code": "JP"}] * 50, 1)
        self.assertEqual(report["by_mapped_source"]["suggested"]["accuracy"], 1.0)
        self.assertEqual((report["by_mapped_source"]["old_url"]["judged"], report["by_mapped_source"]["old_url"]["replacement_rate"]),
                         (1, 0.5))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            ga.write_report(report, records, Path(d), "All NEEDS_REVIEW")
            text = (Path(d) / "report.md").read_text(encoding="utf-8")
            self.assertIn("Old URL provided (no suggestion)", text)
            self.assertTrue(text.startswith("# All NEEDS_REVIEW"))


class AbsenceMode(unittest.TestCase):
    """NOT_FOUND audit: POSITIVE = absence confirmed, NEGATIVE = a clear site (confidence >= 0.70) exists."""

    def absent(self, answers, sites=None):
        agent = FakeAgent(answers)
        record = asyncio.run(ga.judge_absence_row({**ROW, "Final Domain Status": "NOT_FOUND"}, agent,
                                                  FakeChecker(sites or {}), 1))
        return record, agent.calls

    def test_outcome_rules(self):
        self.assertEqual(ga.absence_outcome("", [], 0.0), "NO_DOMAIN")
        self.assertEqual(ga.absence_outcome("acme.co.jp", ["acme-group.com"], 1.0), "AMBIGUOUS")
        self.assertEqual(ga.absence_outcome("acme.co.jp", ["shop.acme.co.jp"], 0.8), "FOUND")  # same site
        self.assertEqual(ga.absence_outcome("acme.co.jp", [], 0.6), "LOW_CONFIDENCE")
        self.assertEqual(ga.absence_outcome("", ["acme.co.jp"], 0.0), "LOW_CONFIDENCE")

    def test_nothing_found_after_two_searches_is_positive(self):
        record, calls = self.absent({})
        self.assertEqual((record["final_state"], record["reason"], calls), ("JUDGED_POSITIVE", "NO_DOMAIN", ["standard", "deep"]))

    def test_clear_site_is_negative_and_skips_deep_search(self):
        record, calls = self.absent({"standard": {"domain": "acme.co.jp", "evidence": [], "criteria": []}},
                                    {"acme.co.jp": TierRules.STRONG})
        self.assertEqual((record["final_state"], record["found_domain"], calls), ("JUDGED_NEGATIVE", "acme.co.jp", ["standard"]))

    def test_weak_or_mixed_answers_stay_positive(self):
        weak, _ = self.absent({"standard": {"domain": "acme.co.jp", "evidence": [], "criteria": []},
                               "deep": {"domain": "acme.co.jp", "evidence": [], "criteria": []}},
                              {"acme.co.jp": "ACME Co., Ltd. Tokyo Japan"})  # 0.60 < 0.70
        self.assertEqual((weak["final_state"], weak["reason"]), ("JUDGED_POSITIVE", "LOW_CONFIDENCE"))
        mixed, _ = self.absent({"standard": {"domain": "", "candidates": ["acme.co.jp", "acme-tokyo.jp"], "evidence": [], "criteria": []}})
        self.assertEqual((mixed["final_state"], mixed["reason"]), ("JUDGED_POSITIVE", "AMBIGUOUS"))

    def test_deep_search_can_find_a_site(self):
        record, calls = self.absent({"deep": {"domain": "acme.co.jp", "evidence": [], "criteria": []}},
                                    {"acme.co.jp": TierRules.STRONG})
        self.assertEqual((record["final_state"], calls), ("JUDGED_NEGATIVE", ["standard", "deep"]))

    def test_absence_report(self):
        records = ([{"final_state": "JUDGED_POSITIVE", "reason": "NO_DOMAIN", "country": "JP", "old_url_provided": ""}] * 3
                   + [{"final_state": "JUDGED_NEGATIVE", "reason": "FOUND", "country": "AU", "old_url_provided": "x.au",
                       "row_id": 9, "company": "X", "found_domain": "x.com.au", "confidence_score": 0.8}])
        report = ga.build_absence_report(records, [{}] * 100, 1)
        self.assertEqual((report["n_judged"], report["confirmed_rate"], report["positive_reasons"], len(report["sites_found"])),
                         (4, 0.75, {"NO_DOMAIN": 3}, 1))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            ga.write_absence_report(report, records, Path(d))
            self.assertIn("NOT_FOUND confirmed: 75.0%", (Path(d) / "report.md").read_text(encoding="utf-8"))


class Statistics(unittest.TestCase):
    def test_wilson_with_finite_population(self):
        low, high = ga.wilson_fpc(300, 376, 16509)
        self.assertTrue(0.75 < low < 0.80 < high < 0.84)
        self.assertEqual(ga.wilson_fpc(0, 0, 100), (0.0, 1.0))

    def test_draw_order_is_reproducible(self):
        rows = [{"source_row_number": str(i)} for i in range(50)]
        self.assertEqual(ga.draw_order(rows, 5), ga.draw_order(list(reversed(rows)), 5))
        self.assertNotEqual(ga.draw_order(rows, 5), ga.draw_order(rows, 6))

    def test_report_bounds_and_replacement_rate(self):
        population = [{"country code": "JP"}] * 100
        records = ([{"final_state": "JUDGED_MATCH", "country": "JP", "mapped_domain": f"m{i}.jp"} for i in range(6)]
                   + [{"final_state": "JUDGED_MISMATCH", "country": "JP", "mapped_domain": "x.jp"}] * 2
                   + [{"final_state": "REPLACED", "country": "JP", "replaced_reason": "LOW_CONFIDENCE"}] * 2)
        report = ga.build_report(records, population, 1)
        self.assertEqual((report["n_judged"], report["raw_accuracy"], report["replacement_rate"], report["bounds"]),
                         (8, 0.75, 0.2, [0.6, 0.8]))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            ga.write_report(report, records, Path(d))
            self.assertIn("75.0%", (Path(d) / "report.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
