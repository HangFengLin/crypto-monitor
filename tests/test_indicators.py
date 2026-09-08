from __future__ import annotations

import unittest

from indicators import calculate_indicators


class IndicatorsTest(unittest.TestCase):
    def test_short_history_with_missing_long_ema_does_not_crash_trend_assignment(self) -> None:
        bars = [
            {
                "open_time": index,
                "open": 100.0 + index,
                "high": 101.0 + index,
                "low": 99.0 + index,
                "close": 100.5 + index,
                "volume": 1000.0 + index,
            }
            for index in range(22)
        ]

        enriched = calculate_indicators(bars)

        self.assertEqual(len(enriched), 22)
        self.assertEqual(enriched[-1]["trend"], "sideways")
        self.assertIsNone(enriched[-1]["ema60"])


if __name__ == "__main__":
    unittest.main()
