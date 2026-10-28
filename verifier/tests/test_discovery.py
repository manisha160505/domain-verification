"""Discovery (review #11-#17, framework #11-#14): offline, with fake search and fake sites."""
import asyncio
import base64
import unittest

from verifier.discovery import (
    Discoverer, SearchCache, WebSearch, candidate_hosts, decode_bing_url, parse_bing_serp,
    parse_ddg_html, parse_yahoo_serp, relevance_terms, successor_from_text,
)
from verifier.extractor import extract_page
from verifier.models import DomainRecord
from verifier.normalization import registered_domain

FILLER = "<p>" + "We deliver quality products and services to our customers every day. " * 6 + "</p>"
SITES = {
    "acme-industries.co.jp": "<title>Acme Industries</title>" + FILLER + "<footer>© Acme Industries Co., Ltd. Tokyo</footer>",
    "acme-fan-blog.com": "<title>I love Acme</title>" + FILLER,
    "acmesister.co.jp": "<title>Acme Sister</title>" + FILLER + "<footer>© Acme Sister Co., Ltd.</footer>",
}


def fake_inspect(calls):
    async def inspect(host):
        calls.append(host)
        html = SITES.get(host, "<title>Unrelated</title>" + FILLER)
        return DomainRecord(host, registered_domain(host), f"https://{host}/", final_url=f"https://{host}/",
                            final_registered_domain=registered_domain(host), http_status=200,
                            dns_status="RESOLVED", pages=[extract_page(f"https://{host}/", html, 200)])
    return inspect


class FakeSearch:
    def __init__(self, results):
        self.results = results
        self.queries = []

    async def search(self, query):
        self.queries.append(query)
        return self.results


class DiscoveryTests(unittest.TestCase):
    def test_parsers(self):
        target = base64.urlsafe_b64encode(b"https://www.acme.co.jp/").decode().rstrip("=")
        bing = f'<ol id="b_results"><li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&amp;u=a1{target}&amp;ntb=1">Acme</a></h2><p>Snippet</p></li></ol>'
        self.assertEqual(parse_bing_serp(bing)[0][0]["url"], "https://www.acme.co.jp/")
        yahoo = ('<div class="compTitle options-toggle"><a href="https://r.search.yahoo.com/_ylt=x/RV=2/RU=https%3a%2f%2fwww.acme.co.jp%2f/RK=2/RS=y">'
                 '<h3>Acme</h3></a></div><div class="compText"><p>About Acme</p></div>')
        self.assertEqual(parse_yahoo_serp(yahoo)[0][0]["url"], "https://www.acme.co.jp/")
        ddg = '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Facme.co.jp%2F&rut=x">Acme</a><a class="result__snippet">Acme Co</a>'
        self.assertEqual(parse_ddg_html(ddg)[0][0]["url"], "https://acme.co.jp/")
        self.assertFalse(parse_ddg_html("<html>anomaly detected</html>")[1])
        self.assertEqual(decode_bing_url("https://example.com/"), "https://example.com/")

    def test_skip_list_and_relevance_filter(self):
        terms = relevance_terms({"name": "ACME INDUSTRIES CO., LTD."})
        results = [{"url": "https://www.linkedin.com/company/acme", "title": "Acme | LinkedIn"},
                   {"url": "https://www.fedex.com/", "title": "FedEx shipping"},
                   {"url": "https://acme-industries.co.jp/", "title": "Acme Industries"},
                   {"url": "https://apple.com/", "title": "Apple"}]
        self.assertEqual(candidate_hosts(results, set(), terms), ["acme-industries.co.jp"])

    def test_failed_searches_are_not_cached(self):
        class Response:
            status_code, text = 202, "<html>anomaly</html>"

        class Session:
            async def get(self, *a, **k):
                return Response()

            async def post(self, *a, **k):
                return Response()

            async def close(self):
                pass

        cache = SearchCache(":memory:")
        search = WebSearch(cache)
        search._session = Session()
        search._cooldown = {}

        async def go():
            import verifier.discovery as d
            original = d.asyncio.sleep
            d.asyncio.sleep = lambda *_: original(0)
            try:
                return await search.search("acme")
            finally:
                d.asyncio.sleep = original
        self.assertEqual(asyncio.run(go()), [])
        self.assertIsNone(cache.get("acme"))
        cache.close()

    def test_checks_several_results_and_needs_proof(self):
        calls = []
        search = FakeSearch([{"url": "https://acme-fan-blog.com/", "title": "Acme fan blog"},
                             {"url": "https://acme-industries.co.jp/", "title": "Acme Industries official"}])
        found = asyncio.run(Discoverer(search, fake_inspect(calls), use_wayback=False).discover(
            {"original_company_name": "ACME INDUSTRIES CO., LTD.", "country code": "JP"}))
        self.assertEqual(found["decision"], "ACCEPT")
        self.assertEqual(found["new_domain"], "acme-industries.co.jp")
        self.assertIn("acme-fan-blog.com", calls)  # the first result was judged and not accepted

    def test_each_row_judged_on_its_own_name(self):
        search = FakeSearch([{"url": "https://acme-industries.co.jp/", "title": "Acme Industries"},
                             {"url": "https://acmesister.co.jp/", "title": "Acme Sister"}])
        discoverer = Discoverer(search, fake_inspect([]), use_wayback=False)
        first = asyncio.run(discoverer.discover({"original_company_name": "ACME INDUSTRIES CO., LTD.", "country code": "JP"}))
        second = asyncio.run(discoverer.discover({"original_company_name": "ACME SISTER CO., LTD.", "country code": "JP"}))
        self.assertEqual(first["new_domain"], "acme-industries.co.jp")
        self.assertEqual(second["new_domain"], "acmesister.co.jp")

    def test_excluded_hosts_never_proposed(self):
        search = FakeSearch([{"url": "https://acme-industries.co.jp/", "title": "Acme Industries"}])
        found = asyncio.run(Discoverer(search, fake_inspect([]), use_wayback=False).discover(
            {"original_company_name": "ACME INDUSTRIES CO., LTD.", "country code": "JP"},
            exclude_hosts=("acme-industries.co.jp",)))
        self.assertNotEqual(found["decision"], "ACCEPT")

    def test_closed_company_outcome(self):
        found = asyncio.run(Discoverer(FakeSearch([]), fake_inspect([]), use_wayback=False).discover(
            {"original_company_name": "OLD ACME CO., LTD.", "country code": "JP"}, registry_status="CEASED"))
        self.assertEqual((found["decision"], found["outcome"]), ("REJECT", "COMPANY_CLOSED"))

    def test_successor_detected_from_text(self):
        self.assertEqual(successor_from_text(["Old Acme was acquired by Mega Holdings Inc. in 2019."], "Old Acme Co., Ltd."),
                         "Mega Holdings Inc")


if __name__ == "__main__":
    unittest.main()
