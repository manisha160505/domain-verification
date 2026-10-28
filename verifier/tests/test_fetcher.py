import asyncio
import sys
import types
import unittest
from unittest.mock import patch

from verifier.config import Config
from verifier.fetcher import TieredFetcher


class _Response:
    def __init__(self, status, url='https://example.test/', body=b'<h1>Example</h1>'):
        self.status = status
        self.url = url
        self.body = body
        self.encoding = 'utf-8'
        self.history = []


class FetcherTests(unittest.TestCase):
    def _module(self, statuses):
        calls = []

        class AsyncFetcher:
            @staticmethod
            async def get(url, **kwargs):
                calls.append(('http', url))
                return _Response(statuses.pop(0), url)

        class DynamicFetcher:
            @staticmethod
            async def async_fetch(url, **kwargs):
                calls.append(('dynamic', url))
                return _Response(200, url, b'<h1>Rendered</h1>')

        class StealthyFetcher:
            @staticmethod
            async def async_fetch(url, **kwargs):
                calls.append(('stealth', url))
                return _Response(200, url, b'<h1>Protected</h1>')

        return types.SimpleNamespace(AsyncFetcher=AsyncFetcher, DynamicFetcher=DynamicFetcher,
                                     StealthyFetcher=StealthyFetcher), calls

    def test_429_retries_but_is_bounded(self):
        module, calls = self._module([429, 200])
        cfg = Config(per_host_delay_seconds=0, max_http_attempts=2, respect_robots=False)
        with patch.dict(sys.modules, {'scrapling': types.ModuleType('scrapling'),
                                      'scrapling.fetchers': module}):
            result = asyncio.run(TieredFetcher(cfg).fetch('https://example.test/'))
        self.assertEqual(result.status, 200)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(calls), 2)

    def test_browser_only_on_explicit_escalation(self):
        module, calls = self._module([200, 200])
        cfg = Config(per_host_delay_seconds=0, max_http_attempts=1,
                     respect_robots=False, use_dynamic=True)
        with patch.dict(sys.modules, {'scrapling': types.ModuleType('scrapling'),
                                      'scrapling.fetchers': module}):
            fetcher = TieredFetcher(cfg)
            normal = asyncio.run(fetcher.fetch('https://example.test/'))
            rendered = asyncio.run(fetcher.fetch('https://example.test/', allow_browser=True,
                                                 force_browser=True))
        self.assertEqual(normal.method, 'HTTP')
        self.assertEqual(rendered.method, 'DYNAMIC')
        self.assertEqual([kind for kind, _ in calls], ['http', 'http', 'dynamic'])


    def test_consistent_identity_without_fake_referer(self):
        seen = {}

        class AsyncFetcher:
            @staticmethod
            async def get(url, **kwargs):
                seen.update(kwargs)
                return _Response(200, url)

        module = types.SimpleNamespace(AsyncFetcher=AsyncFetcher)
        cfg = Config(per_host_delay_seconds=0, respect_robots=False)
        with patch.dict(sys.modules, {'scrapling': types.ModuleType('scrapling'), 'scrapling.fetchers': module}):
            asyncio.run(TieredFetcher(cfg).fetch('https://example.test/'))
        self.assertFalse(seen['stealthy_headers'])
        self.assertIn('Chrome/', seen['headers']['User-Agent'])
        self.assertTrue(seen['verify'])

    def test_bad_certificate_is_recorded_not_hidden(self):
        class AsyncFetcher:
            @staticmethod
            async def get(url, **kwargs):
                if kwargs['verify']:
                    raise RuntimeError('curl: (60) SSL certificate problem')
                return _Response(200, url)

        module = types.SimpleNamespace(AsyncFetcher=AsyncFetcher)
        cfg = Config(per_host_delay_seconds=0, respect_robots=False, max_http_attempts=1)
        with patch.dict(sys.modules, {'scrapling': types.ModuleType('scrapling'), 'scrapling.fetchers': module}):
            result = asyncio.run(TieredFetcher(cfg).fetch('https://example.test/'))
        self.assertEqual(result.status, 200)
        self.assertIs(result.tls_valid, False)


class InvestigateTests(unittest.TestCase):
    def test_www_tried_before_calling_domain_dead(self):
        from verifier import pipeline
        from verifier.models import FetchRecord

        async def fake_dns(host):
            return 'RESOLVED' if host.startswith('www.') else 'NO_ADDRESS'

        class Fetcher:
            urls = []

            async def fetch(self, url, **kwargs):
                self.urls.append(url)
                return FetchRecord(url, final_url=url, status=200, html='<title>Acme</title>' + '<p>text</p>' * 50)

        fetcher = Fetcher()
        with patch.object(pipeline, 'dns_status', fake_dns):
            record = asyncio.run(pipeline.investigate_domain('acme.test', Config(max_evidence_pages=0), fetcher))
        self.assertEqual(record.dns_status, 'RESOLVED')
        self.assertEqual(record.live_host, 'www.acme.test')
        self.assertEqual(fetcher.urls[0], 'https://www.acme.test/')

    def test_dead_only_when_both_variants_do_not_exist(self):
        from verifier import pipeline

        async def all_failed(host):
            return 'FAILED'

        async def temporary(host):
            return 'TEMPORARY'

        with patch.object(pipeline, 'dns_status', all_failed):
            dead = asyncio.run(pipeline.investigate_domain('acme.test', Config(), None))
        with patch.object(pipeline, 'dns_status', temporary):
            temp = asyncio.run(pipeline.investigate_domain('acme.test', Config(), None))
        self.assertEqual(dead.dns_status, 'FAILED')
        self.assertEqual(temp.dns_status, 'TEMPORARY')


if __name__ == '__main__':
    unittest.main()
