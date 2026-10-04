import unittest

from strategy_optimization_report import event_concurrency


class EventConcurrencyTest(unittest.TestCase):
    def test_intrabar_roundtrip_counts_and_partial_does_not_free_slot(self):
        fills = [
            dict(symbol="A", setup_id="a", direction="long", kind="entry", quantity=1),
            dict(symbol="B", setup_id="b", direction="short", kind="entry", quantity=2),
            dict(symbol="A", setup_id="a", direction="long", kind="exit", quantity=1),
            dict(symbol="B", setup_id="b", direction="short", kind="exit", quantity=1),
            dict(symbol="C", setup_id="c", direction="short", kind="entry", quantity=1),
            dict(symbol="B", setup_id="b", direction="short", kind="exit", quantity=1),
            dict(symbol="C", setup_id="c", direction="short", kind="exit", quantity=1),
        ]
        self.assertEqual(event_concurrency(fills), {"max_positions_event_order": 2,
                                                  "max_long_positions_event_order": 1,
                                                  "max_short_positions_event_order": 2})

    def test_fractional_quantity_is_not_lost_to_absolute_counting_tolerance(self):
        fills = [dict(symbol="A", setup_id="a", direction="short", kind="entry", quantity=1e-13),
                 dict(symbol="A", setup_id="a", direction="short", kind="exit", quantity=5e-14),
                 dict(symbol="A", setup_id="a", direction="short", kind="exit", quantity=5e-14)]
        self.assertEqual(event_concurrency(fills)["max_short_positions_event_order"], 1)


if __name__ == "__main__":
    unittest.main()
