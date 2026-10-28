"""Whole-word matching (review #3, #14; framework #3)."""
import unittest

from verifier.matching import (
    core_name, country_evidence, distinctive_tokens, host_matches, is_government_host,
    is_government_name, legal_name_in, name_in, word_in,
)


class MatchingTests(unittest.TestCase):
    def test_no_fragment_matches(self):
        self.assertFalse(word_in("Welcome to Starlight Hotels", "star"))
        self.assertTrue(word_in("Star Industries Ltd", "star"))
        self.assertFalse(name_in("Fabcorp Industries", "abc"))

    def test_country_never_from_two_letter_codes(self):
        self.assertEqual(country_evidence("Our office in Singapore", "IN"), "")
        self.assertEqual(country_evidence("Thank you for visiting", "TH"), "")
        self.assertEqual(country_evidence("Head office: Tokyo", "JP"), "tokyo")
        self.assertEqual(country_evidence("Tel +81-3-1234-5678", "JP"), "+81")

    def test_government_terms_whole_words(self):
        self.assertFalse(is_government_name("Keyboard Ltd"))
        self.assertTrue(is_government_name("Ministry of Defense"))
        self.assertTrue(is_government_host("parlinfo.aph.gov.au"))
        self.assertFalse(is_government_host("govtech.com"))

    def test_skip_list_matches_whole_host_or_parent(self):
        self.assertFalse(host_matches("fedex.com", ["x.com"]))
        self.assertFalse(host_matches("netflix.com", ["x.com"]))
        self.assertFalse(host_matches("pineapple.com.au", ["apple.com"]))
        self.assertTrue(host_matches("www.x.com", ["x.com"]))
        self.assertTrue(host_matches("en.wikipedia.org", ["wikipedia.org"]))

    def test_legal_name_ignores_punctuation_and_suffix_spelling(self):
        self.assertTrue(legal_name_in("© 2023 Watakyu Seimoa Co., Ltd.", "WATAKYU SEIMOA CO.,LTD. - JP"))
        self.assertTrue(legal_name_in("The Bank of Kyoto, Ltd.", "BANK OF KYOTO,LTD., THE"))
        self.assertTrue(legal_name_in("Acme Singapore Pte. Limited", "Acme Singapore Pte Ltd"))
        self.assertFalse(legal_name_in("Bacme Singapore Pte Ltd", "Acme Singapore Pte Ltd"))

    def test_core_name_strips_legal_forms_and_territory_tags(self):
        self.assertEqual(core_name("(KK) NTT DATA CCS"), "NTT DATA CCS")
        self.assertEqual(core_name("A S N, K.K."), "A S N")
        self.assertEqual(core_name("WATAKYU SEIMOA CO.,LTD. - JP"), "WATAKYU SEIMOA")
        self.assertEqual(distinctive_tokens("NISSAN MOTOR ASIA PACIFIC COMPANY LIMITED"), ["nissan", "motor"])

    def test_local_script_names_match(self):
        self.assertTrue(word_in("株式会社日本電気のサイト", "日本電気"))


if __name__ == "__main__":
    unittest.main()
