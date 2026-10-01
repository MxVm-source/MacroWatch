import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from bot.public import publisher, relevance


class LongTermRelevanceTests(unittest.TestCase):
    def setUp(self):
        relevance.STATE["observed"] = 0
        relevance.STATE["eligible"] = 0
        relevance.STATE["published"] = 0
        relevance.STATE["last_observed_utc"] = None
        relevance.STATE["previews"].clear()
        relevance.STATE["seen"].clear()

    def test_fed_event_is_candidate_only_in_preview(self):
        event = {
            "title": "CPI Release",
            "category": "CPI",
            "location": "Bureau of Labor Statistics",
            "start": datetime(2026, 10, 13, 12, 30, tzinfo=timezone.utc),
        }
        with patch.dict(os.environ, {"PUBLIC_RELEVANCE_PREVIEW": "true"}, clear=False):
            result = relevance.observe_fed_event(event=event, stage="T-24h")

        self.assertIsNotNone(result)
        self.assertTrue(result["needs_confirmation"])
        self.assertFalse(result["publication_eligible"])
        self.assertEqual(result["driver"], "inflation")
        self.assertEqual(relevance.STATE["observed"], 1)

    def test_statement_can_be_structural_but_stays_preview(self):
        ai_result = {
            "relevance_score": 9,
            "horizon": "structural",
            "driver": "trade",
            "what_changed": "A broad tariff policy was announced.",
            "why_it_matters": "Broad tariffs can affect costs, inflation and trade flows over time.",
            "passive_takeaway": "One announcement alone does not require a portfolio change.",
            "thesis_change": "watch",
            "confidence": "high",
            "needs_confirmation": False,
        }

        with patch.dict(os.environ, {"PUBLIC_RELEVANCE_PREVIEW": "true"}, clear=False):
            with patch.object(relevance, "_ai_evaluate_statement", return_value=ai_result):
                with patch.object(relevance, "send_public") as send_public:
                    result = relevance.observe_trump_event(
                        text="Policy statement",
                        url="https://example.com/policy",
                        source="Test source",
                        upstream_score=9,
                    )

        self.assertEqual(result["relevance_score"], 9)
        self.assertFalse(result["publication_eligible"])
        send_public.assert_not_called()

    def test_public_format_is_passive_investor_focused(self):
        result = {
            "relevance_score": 8,
            "horizon": "structural",
            "what_changed": "Inflation data changed the recent trend.",
            "why_it_matters": "Persistent inflation can influence the path of interest rates.",
            "passive_takeaway": "A single release is not enough to change a long-term thesis.",
        }
        msg = relevance.build_public_message(result)
        self.assertIn("LONG-TERM RELEVANCE", msg)
        self.assertIn("WHY IT MATTERS LONG TERM", msg)
        self.assertIn("PASSIVE-INVESTOR TAKEAWAY", msg)
        self.assertIn("not a trading signal", msg.lower())

    def test_relevance_policy_blocks_trading_language(self):
        valid, reason = publisher.validate_public_text(
            "Buy now and use leverage.",
            feature="relevance",
        )
        self.assertFalse(valid)
        self.assertIn("blocked", reason)


if __name__ == "__main__":
    unittest.main()
