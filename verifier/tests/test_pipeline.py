import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from verifier.cache import SQLiteCache
from verifier.config import Config
from verifier.export import write_outputs
from verifier.extractor import extract_page
from verifier.models import DomainRecord
from verifier.pipeline import process_records


class PipelineTests(unittest.TestCase):
    def test_shared_domain_fetched_once_and_rows_preserved(self):
        html = ('<script type="application/ld+json">{"@type":"Organization","legalName":"Acme Ltd"}</script>'
                '<p>' + 'Acme makes industrial equipment for customers around the world. ' * 5 + '</p>')
        calls = []

        async def fake_investigate(domain, config, fetcher):
            calls.append(domain)
            return DomainRecord(domain, 'acme.com', 'https://acme.com/',
                                final_url='https://acme.com/', final_registered_domain='acme.com',
                                http_status=200, dns_status='RESOLVED', domain_active=True,
                                checked_at='2026-09-29T00:00:00Z',
                                pages=[extract_page('https://acme.com/', html, 200)])

        records = [
            (2, {'Organization Name': 'Acme Ltd', 'Domain Name': 'acme.com', 'Custom': 'A'},
             {'Organization Name': 'Acme Ltd', 'Domain Name': 'acme.com'}),
            (3, {'Organization Name': 'Acme Subsidiary Ltd', 'Domain Name': 'acme.com', 'Custom': 'B'},
             {'Organization Name': 'Acme Subsidiary Ltd', 'Domain Name': 'acme.com'}),
        ]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            cache = SQLiteCache(Path(directory) / 'cache.sqlite', 86400)
            self.addCleanup(cache.close)  # an assertion failure must not leave the file locked (Windows)
            with patch('verifier.pipeline.investigate_domain', fake_investigate):
                output, info = asyncio.run(process_records(records, Config(), cache, network_healthy=True))
                second, _ = asyncio.run(process_records(records, Config(), cache, network_healthy=True))
            self.assertEqual(calls, ['acme.com'])
            self.assertEqual(len(output), 2)
            self.assertEqual([r['Custom'] for r in output], ['A', 'B'])
            self.assertEqual((output[0]['decision'], output[0]['proof_level']), ('ACCEPT', 'STRONG'))
            self.assertEqual(output[1]['decision'], 'REVIEW')  # site names Acme Ltd, not the subsidiary
            self.assertEqual(output, second)
            summary = write_outputs(output, Path(directory) / 'out.csv', Path(directory) / 'review.csv',
                                    Path(directory) / 'evidence.jsonl', Path(directory) / 'summary.json',
                                    info['unique_domains'])
            self.assertEqual(summary['total_rows'], 2)
            self.assertEqual(summary['unique_domains'], 1)
            self.assertEqual(summary['domains_successfully_fetched'], 1)
            cache.close()


if __name__ == '__main__':
    unittest.main()
