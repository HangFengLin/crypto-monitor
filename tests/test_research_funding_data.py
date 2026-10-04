import unittest

from research_funding_data import collect_funding


class ResearchFundingDataTest(unittest.TestCase):
    def test_paginated_settlements_retain_actual_mark_and_exclude_right_edge(self):
        calls = []
        def provider(symbol, start, end, limit):
            calls.append(start)
            return ([{"fundingTime": 100, "fundingRate": ".001", "markPrice": "101"},
                     {"fundingTime": 200, "fundingRate": "-.002", "markPrice": "98"}]
                    if start == 100 else
                    [{"fundingTime": 300, "fundingRate": ".001", "markPrice": "99"}])
        result = collect_funding("BTCUSDT", 100, 300, provider=provider, limit=2)
        self.assertEqual(calls, [100, 201])
        self.assertTrue(result["complete"])
        self.assertEqual(result["records"], [{"time": 100, "rate": .001, "mark_price": 101.0},
                                             {"time": 200, "rate": -.002, "mark_price": 98.0}])

    def test_missing_mark_price_is_retained_as_gap_and_cannot_be_called_complete(self):
        result = collect_funding("BTCUSDT", 100, 300, provider=lambda *a:
                                 [{"fundingTime": 100, "fundingRate": ".001"}])
        self.assertFalse(result["complete"])
        self.assertEqual(result["gaps"], [{"time": 100, "reason": "invalid_settlement"}])

    def test_nonadvancing_duplicate_page_fails_closed(self):
        def provider(*args):
            return [{"fundingTime": 100, "fundingRate": ".001", "markPrice": "101"}]
        with self.assertRaisesRegex(ValueError, "advance"):
            collect_funding("BTCUSDT", 100, 300, provider=provider, limit=1)

    def test_page_budget_does_not_label_truncated_history_complete(self):
        result = collect_funding("BTCUSDT", 100, 300, limit=1, max_pages=1,
                                 provider=lambda *a: [{"fundingTime": 100, "fundingRate": ".001", "markPrice": "101"}])
        self.assertFalse(result["complete"])
        self.assertEqual(result["incomplete_reason"], "page_budget_exhausted")


if __name__ == "__main__":
    unittest.main()
