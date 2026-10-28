"""Page reader (review #10) and parked-page detection (review #6)."""
import unittest

from verifier.extractor import extract_page, relevant_links

FOOTER_PAGE = """<html><head><title>Acme</title></head><body>
<p>Founded 1998. Prices from 2500 to 1000 units.</p>
<div class="site-footer">
  <a href="mailto:info@acme.com.sg?subject=hello">Contact us</a> Tel: +65 6123 4567
  <p>© 2024 Acme Singapore Pte. Ltd. All rights reserved</p>
</div></body></html>"""


class ExtractorTests(unittest.TestCase):
    def test_reads_mailto_footer_class_and_copyright(self):
        page = extract_page("https://acme.com.sg/", FOOTER_PAGE, 200)
        self.assertEqual(page["emails"], ["info@acme.com.sg"])
        self.assertIn("Acme Singapore Pte. Ltd.", page["footer"])
        self.assertEqual(page["copyright"], ["© 2024 Acme Singapore Pte. Ltd."])

    def test_years_and_prices_are_not_phone_numbers(self):
        page = extract_page("https://acme.com.sg/", FOOTER_PAGE, 200)
        self.assertEqual(page["phones"], ["+65 6123 4567"])

    def test_parked_pages_detected(self):
        for html in ("<title>example.com is for sale</title><p>Buy this domain today</p>",
                     "<title>Welcome to nginx!</title>",
                     "<p>This domain may be for sale. Make an offer on this domain.</p>"):
            self.assertTrue(extract_page("https://x.example/", html, 200)["parked"], html)
        self.assertFalse(extract_page("https://x.example/", FOOTER_PAGE, 200)["parked"])

    def test_sister_site_about_pages_followed(self):
        html = ('<a href="https://corporate.acme.com/about">About us</a>'
                '<a href="https://other.com/about">About</a><a href="/news">News</a>')
        page = extract_page("https://www.acme.com/", html, 200)
        self.assertEqual(relevant_links(page, 5, sister_domain="acme.com"), ["https://corporate.acme.com/about"])


if __name__ == "__main__":
    unittest.main()
