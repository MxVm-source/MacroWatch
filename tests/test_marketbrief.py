import unittest
from datetime import datetime, timezone
from unittest.mock import patch, Mock
from bot.modules import marketbrief


NOW = datetime(2026, 11, 6, 21, 10, tzinfo=timezone.utc)


class CloseBriefTests(unittest.TestCase):
    def payload(self, day='2026-11-06', close=102):
        previous = int(datetime(2026, 11, 5, 14, 30, tzinfo=timezone.utc).timestamp())
        timestamp = int(datetime.fromisoformat(day + 'T14:30:00+00:00').timestamp())
        return {'chart': {'result': [{'timestamp': [previous, timestamp],
            'indicators': {'quote': [{'close': [100, close]}]}}], 'error': None}}

    def test_daily_close_comparison_requires_target_session(self):
        self.assertEqual(marketbrief.parse_close(self.payload(), NOW.date(), NOW), (102, 2.0))
        self.assertIsNone(marketbrief.parse_close(self.payload('2026-11-05'), NOW.date(), NOW))
        self.assertIsNone(marketbrief.parse_close(self.payload(close=None), NOW.date(), NOW))

    def test_nonfinite_values_rejected(self):
        self.assertIsNone(marketbrief.parse_close(self.payload(close=float('nan')), NOW.date(), NOW))

    def test_no_close_before_market_closes(self):
        early = datetime(2026, 11, 6, 18, tzinfo=timezone.utc)
        self.assertIsNone(marketbrief.parse_close(self.payload(), early.date(), early))

    def test_outage_is_explicit_without_invented_cause(self):
        worker = Mock(events=[], releases=[], health={})
        with patch.object(marketbrief, 'fetch_close', return_value=None):
            text = marketbrief.build_close(worker, NOW.date(), NOW)
        self.assertIn('Nasdaq Composite : indisponible', text)
        self.assertIn('S&P 500 : indisponible', text)
        self.assertIn('Aucun lien de causalité', text)
        self.assertNotIn('BTC', text)


if __name__ == '__main__':
    unittest.main()
