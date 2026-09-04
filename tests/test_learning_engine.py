import tempfile
import unittest
from pathlib import Path

import learning_engine as le


class LearningEngineTest(unittest.TestCase):
    def point(self, minute, price=100, bid=1000, ask=500, trades=20, score=10):
        return {"snapshot_epoch": minute * 60, "price": price, "bid_trade_value_15m": bid,
                "ask_trade_value_15m": ask, "trade_count_15m": trades, "launch_score": score,
                "slope15": 1, "gap120": 5, "higher_low": True}

    def test_future_labels_include_failure_controls(self):
        old_horizon = le.HORIZON_MINUTES
        le.HORIZON_MINUTES = 60
        try:
            markets = {"KRW-UP": [self.point(0), self.point(30, 106), self.point(60, 111)],
                       "KRW-FLAT": [self.point(0), self.point(30, 100), self.point(60, 99)]}
            examples, _ = le.build_examples(markets, 7200)
            labels = {(e["market"], e["snapshot_epoch"]): e["labels"] for e in examples}
            self.assertTrue(labels[("KRW-UP", 0)]["hit_10"])
            self.assertFalse(labels[("KRW-FLAT", 0)]["hit_5"])
        finally:
            le.HORIZON_MINUTES = old_horizon

    def test_probability_is_suppressed_for_small_sample(self):
        estimate = le.empirical_prediction({}, [])
        self.assertEqual("표본 부족", estimate["status"])
        self.assertIsNone(estimate["p10"])

    def test_intraday_excludes_no_trade_ma_setup(self):
        previous = self.point(0)
        current = self.point(15, trades=0, bid=0, ask=0, score=14)
        self.assertEqual("제외", le.transition(current, previous))


if __name__ == "__main__":
    unittest.main()
