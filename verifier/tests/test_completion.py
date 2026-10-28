"""Items completed after the first pass: official registers (framework 2.4, #9, #14), local-language
names (code #10, framework 2.8), address check (code #5, framework #9), accuracy gate (framework
#16, 2.2), English-page fallback."""
import unittest

from verifier.decision import decide
from verifier.discovery import build_queries, relevance_terms, is_relevant
from verifier.extractor import english_link, extract_page, latin_share
from verifier.master_resolver import apply_calibration
from verifier.matching import address_evidence, untruncate
from verifier.models import DomainRecord
from verifier.normalization import normalize_domain, registered_domain
from verifier.registers import local_core_name, parse_au, parse_jp, register_key

JP_PAGE = """<dl><dt>法人番号</dt><dd>6010401035489</dd><dt>商号又は名称</dt><dd>ノバルティスホールディングジャパン株式会社</dd>
<dt>商号又は名称（フリガナ）</dt><dd>ノバルティスホールディングジャパン</dd><dt>本店又は主たる事務所の所在地</dt><dd>東京都港区虎ノ門１丁目２３番１号</dd>
<dt>状態</dt><dd>登記記録の閉鎖等（合併による解散等） 事由発生年月日：令和2年9月3日 令和２年９月１日東京都港区虎ノ門一丁目２３番１号ノバルティスファーマ株式会社（4010401011491）に合併し解散</dd></dl>"""
AU_PAGE = """<table><tr><th>Entity name:</th><td>WESFARMERS HOLDINGS PTY LTD</td></tr><tr><th>ABN status:</th><td>Active from 18 Oct 2000</td></tr>
<tr><th>Entity type:</th><td>Australian Private Company</td></tr><tr><th>Goods &amp; Services Tax (GST):</th><td>Not registered</td></tr>
<tr><th>Main business location:</th><td>WA 6000</td></tr></table>"""
FILLER = "<p>" + "We deliver quality products and services to our customers every day. " * 6 + "</p>"
JP_FILLER = "<p>" + "私たちはお客様に高品質な製品とサービスをお届けします。" * 8 + "</p>"


def site(domain, html):
    page = extract_page(f"https://{domain}/", html, 200)
    return DomainRecord(domain, registered_domain(domain), f"https://{domain}/", final_url=f"https://{domain}/",
                        final_registered_domain=registered_domain(domain), http_status=200, dns_status="RESOLVED", pages=[page])


class RegisterTests(unittest.TestCase):
    def test_register_keys(self):
        self.assertEqual(register_key("JP", "9011101026296"), ("JP", "9011101026296"))
        self.assertEqual(register_key("AU", "23 094 800 665"), ("AU", "23094800665"))
        self.assertIsNone(register_key("SG", "201912345K"))

    def test_parse_japan_closed_with_successor(self):
        record = parse_jp(JP_PAGE)
        self.assertEqual(record["local_name"], "ノバルティスホールディングジャパン株式会社")
        self.assertEqual(record["local_core_name"], "ノバルティスホールディングジャパン")
        self.assertEqual(record["status"], "CLOSED")
        self.assertEqual((record["successor_name"], record["successor_number"]), ("ノバルティスファーマ株式会社", "4010401011491"))
        self.assertEqual(local_core_name("(株)日立製作所"), "日立製作所")

    def test_parse_australia(self):
        record = parse_au(AU_PAGE)
        self.assertEqual((record["legal_name"], record["status"], record["address"]), ("WESFARMERS HOLDINGS PTY LTD", "ACTIVE", "WA 6000"))


class LocalNameAndAddressTests(unittest.TestCase):
    def test_japanese_site_proven_by_register_name(self):
        register = {"local_name": "アディダスジャパン株式会社", "local_core_name": "アディダスジャパン", "legal_name": "", "status": "ACTIVE"}
        row = {"original_company_name": "ADIDAS JAPAN K.K.", "country code": "JP", "_register": register}
        html = "<title>アディダス公式オンラインストア</title>" + JP_FILLER + "<footer>アディダスジャパン株式会社</footer>"
        result = decide(row, normalize_domain("adidas.jp"), site("adidas.jp", html))
        self.assertEqual((result["decision"], result["proof_level"]), ("ACCEPT", "STRONG"))
        self.assertEqual(result["checks"]["register_name"], "YES")
        without = decide({**row, "_register": {}}, normalize_domain("adidas.jp"), site("adidas.jp", html))
        self.assertNotEqual(without["decision"], "ACCEPT")  # same page cannot be proven from the English name alone

    def test_japanese_footer_listing_many_companies_is_not_strong(self):
        register = {"local_name": "近畿労働金庫", "local_core_name": "近畿労働金庫", "legal_name": "", "status": "ACTIVE"}
        row = {"original_company_name": "KINKI LABOR CREDIT ASSOCIATION", "country code": "JP", "_register": register}
        footer = "<footer>北海道労働金庫 東北労働金庫 中央労働金庫 近畿労働金庫 九州労働金庫</footer>"
        result = decide(row, normalize_domain("rokin.or.jp"), site("rokin.or.jp", "<title>全国労働金庫協会</title>" + JP_FILLER + footer))
        self.assertNotEqual(result["proof_level"], "STRONG")
        self.assertNotEqual(result["decision"], "ACCEPT")

    def test_footer_naming_two_companies_is_not_strong(self):
        row = {"original_company_name": "AIR WATER MEDICAL INC.", "country code": "JP"}
        footer = "<footer>Air Water Medical Inc. / Air Water Service K.K. Tokyo</footer>"
        result = decide(row, normalize_domain("awb.co.jp"), site("awb.co.jp", "<title>AWB</title>" + FILLER + footer))
        self.assertNotEqual(result["proof_level"], "STRONG")
        single = decide(row, normalize_domain("awm.co.jp"), site("awm.co.jp", "<title>AWM</title>" + FILLER + "<footer>© Air Water Medical Inc.</footer>"))
        self.assertEqual(single["proof_level"], "STRONG")

    def test_successor_name_without_address(self):
        page = JP_PAGE.replace("東京都港区虎ノ門一丁目２３番１号ノバルティスファーマ株式会社", "東京都中央区八丁堀二丁目５川浅ビル東日本エア・ウォーター物流株式会社")
        self.assertEqual(parse_jp(page)["successor_name"], "東日本エア・ウォーター物流株式会社")

    def test_address_check(self):
        self.assertEqual(address_evidence("Perth WA 6000", ["WA 6000"]), "WA 6000")
        self.assertEqual(address_evidence("established 2000", ["NSW 2000"]), "")
        self.assertEqual(address_evidence("〒105-0001 東京都港区虎ノ門", ["東京都, 港区, 虎ノ門, 105-0001"]), "〒105-0001")
        row = {"original_company_name": "Apex Pty Ltd", "country code": "AU", "registry_address": "PARTIAL_STATE_POSTCODE_ONLY: VIC 3000"}
        result = decide(row, normalize_domain("apex.com.au"), site("apex.com.au", "<title>Apex</title>" + FILLER + "<p>Melbourne VIC 3000</p>"))
        self.assertEqual(result["checks"]["address"], "YES")

    def test_common_name_needs_address_not_just_country(self):
        row = {"original_company_name": "Apex Pty Ltd", "country code": "AU"}
        result = decide(row, normalize_domain("apex.com.au"), site("apex.com.au", "<title>Apex</title>" + FILLER + "<p>Sydney, Australia</p>"))
        self.assertNotEqual(result["decision"], "ACCEPT")
        row["registry_address"] = "NSW 2000"
        result = decide(row, normalize_domain("apex.com.au"), site("apex.com.au", "<title>Apex</title>" + FILLER + "<p>Sydney NSW 2000</p>"))
        self.assertEqual(result["decision"], "ACCEPT")

    def test_truncated_names(self):
        self.assertEqual(untruncate("KYUSHU ELECTRIC POWER COMPANY, INCO"), ("KYUSHU ELECTRIC POWER COMPANY", True))
        self.assertEqual(untruncate("XYZ CORPORATE"), ("XYZ CORPORATE", False))

    def test_english_page_link_for_local_language_sites(self):
        page = extract_page("https://www.example.co.jp/", "<title>例</title>" + JP_FILLER +
                            '<a href="/en/">English</a><a href="https://other.com/en/">EN</a>', 200)
        self.assertLess(latin_share(page["visible_text"]), 0.3)
        self.assertEqual(english_link(page, "example.co.jp"), "https://www.example.co.jp/en/")

    def test_search_uses_register_names(self):
        ctx = {"name": "ADIDAS JAPAN K.K.", "country": "JP",
               "register": {"local_name": "アディダスジャパン株式会社", "local_core_name": "アディダスジャパン", "legal_name": ""}}
        self.assertEqual(build_queries(ctx)[0], '"アディダスジャパン株式会社"')
        self.assertTrue(is_relevant({"title": "アディダスジャパン株式会社 | 会社概要"}, "adidas-group.jp", relevance_terms(ctx)))


class CalibrationTests(unittest.TestCase):
    def test_only_proven_rules_auto_accept(self):
        accepted = {"decision": "ACCEPT", "outcome": "VERIFIED", "accept_rule": "brand_in_site_identity+country_match",
                    "new_domain_url": "https://acme.com", "reason": "ok"}
        self.assertEqual(apply_calibration(accepted, None), accepted)  # no hand-check yet: unchanged
        self.assertEqual(apply_calibration(accepted, {"brand_in_site_identity+country_match"})["decision"], "ACCEPT")
        gated = apply_calibration(accepted, {"legal_name_footer"})
        self.assertEqual((gated["decision"], gated["outcome"], gated["suggested_domain"]), ("REVIEW", "UNCALIBRATED_ACCEPT", "acme.com"))


if __name__ == "__main__":
    unittest.main()
