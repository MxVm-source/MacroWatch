import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from bot.modules import jjwatch


NOW = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)


class OfficialCacheTests(unittest.TestCase):
    def test_successful_local_cache_returns_parsed_data_without_second_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = jjwatch.Engine(directory, sender=lambda _: True)
            with patch.object(jjwatch.requests, 'get', return_value=Mock(text='[1]')) as get:
                engine._source('calendar', 'https://example.org', json.loads, NOW, timedelta(hours=1))
                self.assertEqual(engine._source('calendar', 'https://example.org', json.loads,
                                                NOW + timedelta(seconds=30), timedelta(hours=1)), ([1], NOW))
                self.assertEqual(get.call_count, 1)

    def test_dated_official_cache_used_only_when_direct_fetch_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = jjwatch.Engine(directory, sender=lambda _: True)
            bad = Mock()
            bad.raise_for_status.side_effect = RuntimeError('unreachable')
            cached = Mock(text=json.dumps({'sources': {'BLS CPI': {
                'url': 'https://www.bls.gov/feed/cpi.rss', 'fetched_at': NOW.isoformat(), 'body': '[1]'}}}))
            with patch.object(jjwatch.requests, 'get', side_effect=[bad, cached]):
                items, fetched = engine._source('BLS CPI', 'https://www.bls.gov/feed/cpi.rss',
                                                json.loads, NOW, timedelta(minutes=1))
            self.assertEqual(items, [1])
            self.assertEqual(fetched, NOW)
            self.assertTrue(engine.health['BLS CPI'])

    def test_stale_or_wrong_origin_cache_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = jjwatch.Engine(directory, sender=lambda _: True)
            for url, age in [('https://example.org/feed', 0), ('https://www.bls.gov/feed/cpi.rss', 3)]:
                payload = {'sources': {'BLS CPI': {'url': url,
                    'fetched_at': (NOW - timedelta(hours=age)).isoformat(), 'body': '[1]'}}}
                with patch.object(jjwatch.requests, 'get', return_value=Mock(text=json.dumps(payload))):
                    result = engine._official_cache('BLS CPI', 'https://www.bls.gov/feed/cpi.rss',
                                                     json.loads, NOW, timedelta(hours=2))
                self.assertIsNone(result)


if __name__ == '__main__':
    unittest.main()
