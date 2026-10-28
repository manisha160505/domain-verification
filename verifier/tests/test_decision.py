"""Proof-ladder decision engine: one test per gap in the code and framework reviews."""
import unittest

from verifier.decision import decide
from verifier.extractor import extract_page
from verifier.models import DomainRecord, Evidence
from verifier.normalization import normalize_domain, registered_domain

FILLER = "<p>" + "We deliver quality products and services to our customers every day. " * 6 + "</p>"


def row(name, country="JP", territory="", **extra):
    return {"original_company_name": name, "country code": country, "Sales Territory Name": territory, **extra}


def site(domain, html, status=200, final_url=None, dns="RESOLVED", parked=None):
    final = final_url or f"https://{domain}/"
    page = extract_page(final, html, status)
    record = DomainRecord(domain, registered_domain(domain), f"https://{domain}/", final_url=final,
                          final_registered_domain=registered_domain(final.split("/")[2]),
                          http_status=status, dns_status=dns, domain_active=True, pages=[page])
    if parked is not None:
        record.parked = parked
    return record


def run(r, domain, record, **kwargs):
    return decide(r, normalize_domain(domain), record, **kwargs)


class DecisionTests(unittest.TestCase):
    # Strong proof
    def test_legal_name_in_footer_is_strong_proof(self):
        result = run(row("WATAKYU SEIMOA CO.,LTD."), "watakyu.co.jp",
                     site("watakyu.co.jp", "<title>Home</title>" + FILLER + "<footer>© 2023 Watakyu Seimoa Co., Ltd.</footer>"))
        self.assertEqual((result["decision"], result["proof_level"], result["answer_type"]), ("ACCEPT", "STRONG", "OWN_SITE"))
        self.assertEqual(result["checks"]["legal_name"], "YES")

    def test_owner_statement_with_other_legal_ending_counts(self):
        result = run(row("WATAKYU SEIMOA CO.,LTD."), "watakyu.co.jp",
                     site("watakyu.co.jp", "<title>ワタキューセイモア株式会社</title>" + FILLER + "<footer>© Watakyu seimoa Corporation.</footer>"))
        self.assertEqual(result["decision"], "ACCEPT")
        self.assertIn("legal_name_owner_variant", [p["type"] for p in result["proofs"]])

    def test_tax_id_on_site_is_strong_proof(self):
        result = run(row("Some Co Pty Ltd", "AU", tax_id="68706814312"), "someco.com.au",
                     site("someco.com.au", "<title>Welcome</title>" + FILLER + "<p>ABN 68 706 814 312</p>"))
        self.assertEqual(result["decision"], "ACCEPT")
        self.assertEqual(result["checks"]["registration_number"], "YES")

    def test_footer_listing_many_companies_is_not_strong(self):
        footer = ("<footer>Group companies: Acme Life Insurance Co., Ltd. | Acme Direct Co., Ltd. | "
                  "Acme General Co., Ltd. | Acme Asset Co., Ltd.</footer>")
        result = run(row("Acme Life Insurance Co., Ltd."), "acme-direct.co.jp",
                     site("acme-direct.co.jp", "<title>Direct</title>" + FILLER + footer))
        self.assertNotEqual(result["proof_level"], "STRONG")

    # Weak evidence is not enough (review #1/#2, framework #2)
    def test_lookalike_domain_alone_is_not_accepted(self):
        result = run(row("Fabcorp Industries Pty Ltd", "AU"), "abc.com", site("abc.com", "<title>ABC Shop</title>" + FILLER))
        self.assertNotEqual(result["decision"], "ACCEPT")

    def test_contact_email_on_own_domain_is_hint_only(self):
        result = run(row("Acme Pte Ltd", "SG"), "acme.sg",
                     site("acme.sg", "<title>Home</title>" + FILLER + '<a href="mailto:info@acme.sg">mail</a> Singapore'))
        self.assertEqual(result["decision"], "REVIEW")

    def test_territory_column_alone_does_not_prove_group(self):
        result = run(row("PHILIP MORRIS (PAKISTAN) LIMITED", "PK", "PMI - PK"), "pmi.com",
                     site("pmi.com", "<title>PMI | Smoke-free future</title>" + FILLER))
        self.assertNotEqual(result["decision"], "ACCEPT")

    def test_group_site_accepted_when_it_names_the_subsidiary(self):
        result = run(row("ABB AUTOMATION AND ELECTRIFICATION (VIETNAM) COMPANY LIMITED", "VN", "ABB LTD - VN"), "abb.com",
                     site("abb.com", "<title>ABB Group</title>" + FILLER +
                          "<p>ABB Automation and Electrification (Vietnam) Company Limited, Hanoi</p>"))
        self.assertEqual(result["decision"], "ACCEPT")
        self.assertEqual(result["answer_type"], "GROUP_SITE")

    def test_brand_needs_two_distinctive_words(self):
        result = run(row("BOSTON CONSULTING JAPAN K.K."), "boston-hotel.jp",
                     site("boston-hotel.jp", "<title>Boston Hotel Tokyo</title>" + FILLER))
        self.assertNotIn("brand_in_site_identity", [p["type"] for p in result["proofs"]])

    # Unreadable sites are never judged (review #2, framework #5)
    def test_unreadable_sites_are_not_valid(self):
        cases = [(404, "<title>Not found</title>" + FILLER, "TEMP_UNREACHABLE"),
                 (503, "<title>Error</title>" + FILLER, "TEMP_UNREACHABLE"),
                 (403, "<title>Access denied</title>" + FILLER, "BLOCKED"),
                 (200, "<title>Loading...</title>", "UNREADABLE")]
        for status, html, outcome in cases:
            result = run(row("Acme Pte Ltd", "SG"), "acme.sg", site("acme.sg", html, status=status))
            self.assertEqual((result["decision"], result["outcome"]), ("REVIEW", outcome), status)

    def test_dead_domain_outcomes_are_separate(self):
        dead = DomainRecord("acme.sg", "acme.sg", "https://acme.sg/", dns_status="FAILED")
        temp = DomainRecord("acme.sg", "acme.sg", "https://acme.sg/", dns_status="TEMPORARY")
        self.assertEqual(run(row("Acme Pte Ltd", "SG"), "acme.sg", dead)["outcome"], "DOMAIN_NOT_EXIST")
        self.assertEqual(run(row("Acme Pte Ltd", "SG"), "acme.sg", temp)["outcome"], "TEMP_UNREACHABLE")
        self.assertEqual(run(row("Acme Pte Ltd", "SG"), "acme.sg", dead, network_healthy=False)["decision"], "REVIEW")
        parked = site("acme.sg", "<p>This domain may be for sale</p>", parked=True)
        self.assertEqual(run(row("Acme Pte Ltd", "SG"), "acme.sg", parked)["outcome"], "PARKED")

    def test_blank_domain_is_no_domain_supplied(self):
        result = decide(row("Acme Pte Ltd", "SG"), normalize_domain(""), None)
        self.assertEqual((result["decision"], result["outcome"]), ("REJECT", "NO_DOMAIN_SUPPLIED"))

    # Mismatch rules (review #4, framework #6)
    def test_title_first_word_is_not_a_different_owner(self):
        result = run(row("Acme Pte Ltd", "SG"), "acme.sg", site("acme.sg", "<title>Welcome to our site</title>" + FILLER))
        self.assertNotEqual(result["outcome"], "MISMATCH")

    def test_clearly_named_other_owner_is_mismatch(self):
        result = run(row("Acme Pte Ltd", "SG"), "sunflower.com",
                     site("sunflower.com", "<title>Sunflower</title>" + FILLER + "<footer>© 2024 Sunflower Bakery LLC</footer>"))
        self.assertEqual((result["decision"], result["outcome"]), ("REJECT", "MISMATCH"))

    def test_government_site_goes_to_review(self):
        result = run(row("Acme Medical Pty Ltd", "AU"), "health.gov.au",
                     site("health.gov.au", "<title>Department of Health</title>" + FILLER + " Canberra"))
        self.assertEqual((result["decision"], result["outcome"]), ("REVIEW", "GOVERNMENT_SITE"))

    def test_keyboard_is_not_a_government_body(self):
        result = run(row("Keyboard Ltd", "AU"), "keyboard.com.au",
                     site("keyboard.com.au", "<title>Keyboard Ltd | Sydney</title>" + FILLER))
        self.assertEqual(result["decision"], "ACCEPT")

    # Redirects are judged at the destination (review #9, framework #7)
    def test_redirect_to_company_site_is_accepted(self):
        result = run(row("ALPS ALPINE CO LTD"), "alps.co.jp",
                     site("alps.co.jp", "<title>ALPS ALPINE</title>" + FILLER + "<footer>© ALPS ALPINE CO., LTD.</footer>",
                          final_url="https://www.alpsalpine.com/"))
        self.assertEqual((result["decision"], result["found_via"]), ("ACCEPT", "REDIRECT"))
        self.assertEqual(result["current_website"], "https://www.alpsalpine.com")

    # Search-found candidates need a higher bar (framework #11)
    def test_search_bar_is_higher_than_supplied(self):
        html = "<title>Nissan recalls cars - Motor News</title>" + FILLER + " Tokyo"
        record = site("motornews.jp", html)
        supplied = run(row("NISSAN MOTOR CO LTD"), "motornews.jp", record)
        searched = run(row("NISSAN MOTOR CO LTD"), "motornews.jp", record, found_via="SEARCH")
        self.assertEqual(supplied["decision"], "ACCEPT")  # brand in identity + country: enough for a supplied domain
        self.assertNotEqual(searched["decision"], "ACCEPT")  # not enough for a search result

    def test_checks_not_run_say_not_checked(self):
        result = run(row("Acme Pte Ltd", "SG"), "acme.sg", site("acme.sg", "<title>Acme</title>" + FILLER + " Singapore"))
        self.assertEqual(result["checks"]["registration_number"], "NOT_CHECKED")
        self.assertIn("http_status", result)
        self.assertEqual(result["http_status"], 200)

    def test_reviewed_external_evidence_is_used(self):
        evidence = [Evidence("https://registry.example/acme", "government_registry", "acme.sg registered to Acme Pte Ltd",
                             "exact_entity", "VERY_STRONG", source_type="external")]
        result = run(row("Acme Pte Ltd", "SG"), "acme.sg", site("acme.sg", "<title>Home</title>" + FILLER), external=evidence)
        self.assertEqual(result["decision"], "ACCEPT")


if __name__ == "__main__":
    unittest.main()
