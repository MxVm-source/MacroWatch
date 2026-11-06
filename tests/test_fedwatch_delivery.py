import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from bot.modules import fedwatch


class FedDeliveryTests(unittest.TestCase):
    def test_legacy_alert_no_longer_crashes_or_generates_consensus(self):
        ev = dict(title='CPI Release', start=datetime.now(timezone.utc) + timedelta(days=1), category='CPI')
        alert = dict(event=ev, event_id='test-cpi', label='T-24h')
        fedwatch.STATE['fired_alerts'].discard(('test-cpi', 'T-24h'))
        with patch.object(fedwatch, 'send_text', return_value=True) as send:
            with patch.object(fedwatch, '_ai_pre_event_brief') as ai:
                fedwatch._send_alert(alert)
                ai.assert_not_called()
                self.assertIn('indisponible', send.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
