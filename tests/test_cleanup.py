import os
import sqlite3
import threading
import time
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from apscheduler.schedulers.background import BackgroundScheduler
from bot import main
from bot.modules import jj_sources as src, jjwatch, trumpwatch_live
from bot.public import publisher


class CleanupTests(unittest.TestCase):
    def test_one_time_cleanup_markers_survive_alert_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = jjwatch.Engine(directory, sender=lambda _: True)
            now = datetime(2026, 11, 6, tzinfo=timezone.utc)
            old = (now - timedelta(days=31)).isoformat()
            keys = ['description:cleanup-v2:private', 'activation:cleanup-v2', 'reminder:old']
            with sqlite3.connect(worker.db) as conn:
                conn.executemany('INSERT INTO sent VALUES (?, ?)', [(key, old) for key in keys])
            with patch.object(worker, '_source', return_value=([], None)):
                worker.refresh(now)
            with sqlite3.connect(worker.db) as conn:
                remaining = {row[0] for row in conn.execute('SELECT key FROM sent')}
            self.assertEqual(remaining, set(keys[:2]))

    def test_only_macro_jobs_remain_even_with_old_crypto_flags(self):
        scheduler = BackgroundScheduler(timezone='Europe/Brussels')
        with patch.object(main, 'SCHED', scheduler), patch.object(scheduler, 'start'):
            with patch.dict(os.environ, {'ENABLE_ELITE_POSITIONWATCH': 'true', 'ETHERSCAN_API_KEY': 'fake'}):
                main.start_scheduler()
        self.assertEqual({job.id for job in scheduler.get_jobs()}, {'jjwatch', 'trumpwatch', 'weekly_brief'})

    def test_old_commands_cannot_generate_crypto_reports(self):
        with patch.object(main, 'send_private', return_value=True) as send:
            for cmd in ['/status', '/bot_challenge', '/live_challenge', '/report', '/intel', '/monthly_update test']:
                main._handle_command(cmd, cmd)
        self.assertTrue(all('pause' in call.args[0].lower() for call in send.call_args_list))
        self.assertEqual(send.call_count, 6)

    def test_public_automation_is_paused_by_default(self):
        with patch.dict(os.environ, {'PUBLIC_ENABLED': 'true', 'PUBLIC_ENABLE_WEEKLY': 'true'}):
            self.assertFalse(publisher.feature_enabled('weekly'))

    def test_crypto_only_trump_post_rejected_and_macro_retained(self):
        self.assertFalse(trumpwatch_live._passes_gate('Bitcoin and crypto markets are booming!'))
        self.assertFalse(trumpwatch_live._passes_gate('Stablecoins: new deregulation deal and agreement to ban competitors.'))
        self.assertFalse(trumpwatch_live._passes_gate('Crypto holders are spoiled by our new deregulation deal and agreement.'))
        self.assertTrue(trumpwatch_live._passes_gate('New tariffs on semiconductor imports affect Nasdaq stocks.'))

    def test_failed_macro_alert_not_marked_as_sent(self):
        tw = trumpwatch_live
        item = dict(text='New tariffs on Nasdaq stocks', url='https://example.org/123456',
                    source='test', ts='2026-11-06T12:00:00Z')
        with patch.object(tw, '_fetch_rss', return_value=[item]), \
             patch.object(tw, '_fetch_cnn', return_value=[]), \
             patch.object(tw, '_dedup_check', return_value=True), \
             patch.object(tw, '_is_too_old', return_value=False), \
             patch.object(tw, '_is_endorsement', return_value=False), \
             patch.object(tw, '_ai_score', return_value={'score': 9, 'sentiment': 'neutral'}), \
             patch.object(tw, 'send_text', return_value=False), \
             patch.object(tw, '_dedup_mark') as mark:
            tw.poll_once()
            mark.assert_not_called()

    def test_manual_and_scheduled_macro_poll_cannot_double_send(self):
        tw = trumpwatch_live
        seen, delivered = set(), []
        item = dict(text='New tariffs on Nasdaq stocks', url='https://example.org/123456',
                    source='test', ts='2026-11-06T12:00:00Z')
        def sender(*args, **kwargs):
            delivered.append(1)
            time.sleep(0.03)
            return True
        with patch.object(tw, '_fetch_rss', return_value=[item]), \
             patch.object(tw, '_fetch_cnn', return_value=[]), \
             patch.object(tw, '_dedup_check', side_effect=lambda key: key not in seen), \
             patch.object(tw, '_dedup_mark', side_effect=seen.add), \
             patch.object(tw, '_is_too_old', return_value=False), \
             patch.object(tw, '_is_endorsement', return_value=False), \
             patch.object(tw, '_ai_score', return_value={'score': 9, 'sentiment': 'neutral'}), \
             patch.object(tw, 'send_text', side_effect=sender):
            threads = [threading.Thread(target=tw.poll_once) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)
        self.assertEqual(len(delivered), 1)

    def test_close_handles_dst_and_early_sessions(self):
        self.assertEqual(src.session_close('2026-11-06').astimezone(src.BRUSSELS).hour, 22)
        self.assertEqual(src.session_close('2026-10-28').astimezone(src.BRUSSELS).hour, 21)
        self.assertEqual(src.session_close('2026-11-27').astimezone(src.BRUSSELS).hour, 19)
        self.assertIsNone(src.session_close('2026-11-26'))

    def test_close_recap_runs_once_after_actual_close(self):
        with tempfile.TemporaryDirectory() as directory:
            messages = []
            worker = jjwatch.Engine(directory, sender=lambda msg: messages.append(msg) or True)
            before = datetime(2026, 11, 27, 18, 9, tzinfo=timezone.utc)
            with patch('bot.modules.marketbrief.build_close', return_value='Bilan US'):
                worker.tick(before)
                self.assertEqual(messages, [])
                worker.tick(before + timedelta(minutes=1))
                worker.tick(before + timedelta(minutes=2))
            self.assertEqual(messages, ['Bilan US'])


if __name__ == '__main__':
    unittest.main()
