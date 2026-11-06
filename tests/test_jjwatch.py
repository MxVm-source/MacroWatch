import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from unittest.mock import Mock

from bot.modules import jjwatch as jj
from bot.modules import jj_sources as src


NOW = datetime(2026, 11, 6, 13, 0, tzinfo=timezone.utc)


def event():
    return src.normalize_calendar([dict(title='Non-Farm Employment Change', country='USD',
        impact='High', date='2026-11-06T08:30:00-05:00', forecast='150K', previous='100K')])[0]


class JJTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.messages = []
        self.engine = jj.Engine(self.tmp.name, sender=lambda text: self.messages.append(text) or True)
        self.engine.events = [event()]
        self.engine.calendar_checked = NOW
        self.engine.calendar_success = NOW
        self.engine.coverage = (NOW.date() - timedelta(days=5), NOW.date() + timedelta(days=1))
        self.engine.health = {'calendar': True, 'BLS schedule': True}

    def test_reminder_once_and_survives_restart(self):
        self.engine.tick(NOW)
        self.engine.tick(NOW + timedelta(seconds=30))
        other = jj.Engine(self.tmp.name, sender=lambda text: self.messages.append(text) or True)
        other.events = self.engine.events
        other.tick(NOW + timedelta(seconds=45))
        self.assertEqual(len(self.messages), 1)
        self.assertIn('30 min', self.messages[0])
        self.assertIn('150K', self.messages[0])

    def test_failed_delivery_retries(self):
        self.engine.sender = lambda text: False
        self.engine.tick(NOW)
        self.engine.sender = lambda text: self.messages.append(text) or True
        self.engine.tick(NOW + timedelta(seconds=30))
        self.assertEqual(len(self.messages), 1)

    def test_successful_release_sources_refresh_each_minute(self):
        response = Mock(text='[]')
        with patch.object(jj.requests, 'get', return_value=response) as get:
            self.engine._source('release', 'https://www.bls.gov/test', __import__('json').loads,
                                NOW, timedelta(minutes=1))
            self.engine._source('release', 'https://www.bls.gov/test', __import__('json').loads,
                                NOW + timedelta(minutes=1), timedelta(minutes=1))
            self.assertEqual(get.call_count, 2)

    def test_startup_does_not_replay_old_reminder(self):
        self.engine.tick(NOW + timedelta(minutes=10))
        self.assertEqual(self.messages, [])

    def test_startup_does_not_replay_historical_release(self):
        self.engine.releases = [dict(title='Payroll employment rises by 165,000',
            body='Total nonfarm payroll employment rose by 165,000 in October.',
            url='https://www.bls.gov/news.release/archives/empsit_11062026.htm',
            source='BLS employment', published=NOW)]
        self.engine.tick(NOW + timedelta(minutes=70))
        self.assertEqual(self.messages, [])

    def test_no_result_until_official_confirmation_and_release_time(self):
        self.engine.tick(NOW + timedelta(minutes=30))
        self.assertEqual(self.messages, [])
        self.engine.releases = [dict(title='Payroll employment rises by 165,000',
            body='Total nonfarm payroll employment rose by 165,000 in October.',
            url='https://www.bls.gov/news.release/archives/empsit_11062026.htm',
            source='BLS employment', published=NOW)]
        self.engine.tick(NOW + timedelta(minutes=29))
        self.assertEqual(self.messages, [])
        self.engine.tick(NOW + timedelta(minutes=31))
        self.engine.tick(NOW + timedelta(minutes=32))
        self.assertEqual(len(self.messages), 1)
        self.assertIn('165K', self.messages[0])
        self.assertIn('+15K', self.messages[0])
        self.assertIn('révisions', self.messages[0])

    def test_outage_never_becomes_quiet_day_claim(self):
        self.engine.calendar_success = None
        msg = self.engine.brief(NOW.date(), 'Préouverture', NOW)
        self.assertIn('incomplète', msg)
        self.assertNotIn('Aucune annonce majeure', msg)

    def test_coverage_does_not_extend_to_next_week(self):
        msg = self.engine.brief(NOW.date() + timedelta(days=3), 'Veille', NOW)
        self.assertIn('incomplète', msg)

    def test_no_open_alert_on_holiday(self):
        self.engine.events = []
        self.engine.tick(datetime(2026, 11, 26, 14, 15, tzinfo=timezone.utc))
        self.assertEqual(self.messages, [])

    def test_preopen_and_evening_fixed_to_their_timezones(self):
        self.engine.tick(datetime(2026, 11, 6, 14, 15, tzinfo=timezone.utc))
        self.assertIn('Préouverture', self.messages[0])
        self.engine.tick(datetime(2026, 11, 8, 18, 0, tzinfo=timezone.utc))
        self.assertIn('Veille', self.messages[1])
        self.assertIn('2026-11-09', self.messages[1])

    def test_private_sender_blocks_public_destination(self):
        with patch.dict(os.environ, {'CHAT_ID': '@same', 'PUBLIC_CHAT_ID': '@same'}):
            with patch('bot.modules.jjwatch.send_text') as send:
                self.assertFalse(jj.send_private('test'))
                send.assert_not_called()

    def test_news_ignores_undated_old_unrelated_and_no_startup_flood(self):
        self.engine.releases = [dict(title='Minutes of discount rate meetings', body='',
            url='https://www.federalreserve.gov/a', source='Fed', published=NOW)]
        self.engine.tick(NOW + timedelta(minutes=90))
        self.assertFalse(any('Actualité' in text for text in self.messages))

    def test_fed_news_polled_at_least_each_five_minutes_before_open(self):
        now = NOW - timedelta(hours=1)
        with patch.object(self.engine, '_source', return_value=([], None)) as source:
            self.engine.refresh(now)
            fed_call = next(call for call in source.call_args_list if call.args[0] == 'Fed')
            self.assertLessEqual(fed_call.args[4], timedelta(minutes=5))

    def test_stale_forecast_not_presented_as_current_expectation(self):
        self.engine.calendar_success = NOW - timedelta(hours=8)
        self.engine.tick(NOW)
        self.assertNotIn('150K', self.messages[0])
        self.assertIn('consensus indisponible', self.messages[0])

    def test_activation_message_delivered_once(self):
        with patch.object(self.engine, 'refresh'):
            self.engine.poll(NOW + timedelta(minutes=10))
            self.engine.poll(NOW + timedelta(minutes=11))
        self.assertEqual(len(self.messages), 1)
        self.assertIn('activé', self.messages[0])

    def test_existing_briefs_receive_the_sourced_calendar(self):
        from bot.modules import fedwatch
        with patch.object(self.engine, '_source', side_effect=[([event()], NOW), ([], None)] + [([], None)] * 5):
            self.engine.refresh(NOW)
        self.assertTrue(any(e['title'] == event()['title'] and e['category'] == 'NFP'
                            for e in fedwatch.STATE['events']))


if __name__ == '__main__':
    unittest.main()
