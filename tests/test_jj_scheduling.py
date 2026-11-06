import unittest
from unittest.mock import patch
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timezone
from bot import main


class SchedulingTests(unittest.TestCase):
    def test_jj_job_registered_and_legacy_fed_alerts_replaced(self):
        scheduler = BackgroundScheduler(timezone='Europe/Brussels')
        with patch.object(main, 'SCHED', scheduler), patch.object(scheduler, 'start'):
            main.start_scheduler()
            self.assertIsNotNone(scheduler.get_job('jjwatch'))
            self.assertIsNone(scheduler.get_job('fedwatch'))

    def test_no_hourly_false_us_open_message(self):
        scheduler = BackgroundScheduler(timezone='Europe/Brussels')
        with patch.object(main, 'SCHED', scheduler), patch.object(scheduler, 'start'):
            main.start_scheduler()
            self.assertIsNone(scheduler.get_job('market_open'))

    def test_fed_rates_no_longer_use_unverified_probabilities(self):
        from bot.modules import fedwatch
        with patch.object(fedwatch, '_compute_rate_probability') as compute:
            line = fedwatch._rate_prob_line()
            compute.assert_not_called()
            self.assertIn('indisponible', line)


if __name__ == '__main__':
    unittest.main()
