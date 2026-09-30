import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from bot.public import publisher
from bot.modules import weeklybrief


class PublicPublisherTests(unittest.TestCase):
    def test_global_kill_switch_blocks_weekly(self):
        env = {
            "PUBLIC_ENABLED": "false",
            "PUBLIC_ENABLE_WEEKLY": "true",
            "TELEGRAM_TOKEN": "test-token",
            "PUBLIC_CHAT_ID": "@test",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch.object(publisher.requests, "post") as post:
                self.assertFalse(publisher.send_public("hello", feature="weekly"))
                post.assert_not_called()

    def test_strategy_is_off_by_default(self):
        keys = ["PUBLIC_ENABLE_STRATEGY"]
        saved = {k: os.environ.get(k) for k in keys}
        try:
            for key in keys:
                os.environ.pop(key, None)
            with patch.dict(os.environ, {"PUBLIC_ENABLED": "true"}, clear=False):
                self.assertFalse(publisher.feature_enabled("strategy"))
                self.assertTrue(publisher.feature_enabled("weekly"))
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


    def test_weekly_policy_blocks_legacy_content(self):
        env = {
            "PUBLIC_ENABLED": "true",
            "PUBLIC_ENABLE_WEEKLY": "true",
            "TELEGRAM_TOKEN": "test-token",
            "PUBLIC_CHAT_ID": "@test",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch.object(publisher.requests, "post") as post:
                self.assertFalse(
                    publisher.send_public(
                        "Confluence copy trading update",
                        feature="weekly",
                    )
                )
                post.assert_not_called()


class WeeklyPublicBriefTests(unittest.TestCase):
    def test_public_brief_has_no_legacy_strategy_marketing(self):
        event = {
            "title": "CPI Release",
            "category": "CPI",
            "start": datetime.now(timezone.utc) + timedelta(days=2),
        }

        patches = [
            patch.object(weeklybrief, "_fetch_asset_weekly_snapshot",
                         side_effect=[
                             {"price": 65000.0, "chg_7d": 3.2},
                             {"price": 2200.0, "chg_7d": -1.1},
                         ]),
            patch.object(weeklybrief, "_fetch_equity_weekly",
                         return_value={"sp500": 1.25, "nasdaq": 1.9, "aapl": 0.2}),
            patch.object(weeklybrief, "_fetch_macro_assets_weekly",
                         return_value={"gold": 0.7, "dxy": -0.8}),
            patch.object(weeklybrief, "_fetch_fear_greed",
                         return_value={"value": 42, "label": "Fear"}),
            patch.object(weeklybrief, "_regime_simple", return_value="CHOP"),
            patch.object(weeklybrief, "_generate_public_narrative",
                         return_value="Markets were mixed. Cross-asset moves remain context, not a forecast."),
            patch.object(weeklybrief, "_fetch_upcoming_macro", return_value=[event]),
        ]

        for mocked in patches:
            mocked.start()
            self.addCleanup(mocked.stop)

        msg = weeklybrief.build_weekly_pulse({})
        self.assertIn("WEEKLY MARKET BRIEF", msg)
        self.assertIn("ONE THING TO KNOW", msg)
        self.assertIn("CPI Release", msg)
        self.assertNotIn("ATRb", msg)
        self.assertNotIn("Confluence", msg)
        self.assertNotIn("Copy trading", msg)


if __name__ == "__main__":
    unittest.main()