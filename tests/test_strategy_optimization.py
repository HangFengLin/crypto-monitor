import unittest

from strategy import DEFAULT_CONFIG
from strategy_optimization import causal_events, correlation_exposure, paired_weekly_test, portfolio_metrics, variants


class FakeEngine:
    def __init__(self, config):
        self.windows = []

    def detect(self, bars, clean=False):
        self.windows.append(len(bars))
        bar = bars[-1]
        return {"signal": "long", "divergence_time": 10 if bar["open_time"] < 30 else 20,
                "stop_loss": 90, "score": 0}


class StrategyOptimizationTest(unittest.TestCase):
    def test_complete_events_do_not_use_lifecycle_or_skip_occupied_signals(self):
        bars = [{"open_time": t, "close_time": t + 9, "macd": 1} for t in (0, 10, 20, 30, 40)]
        events = causal_events("TEST", bars, DEFAULT_CONFIG, 10, 40, engine_factory=FakeEngine)
        self.assertEqual([e["signal_time"] for e in events], [10])
        # A different setup survives regardless of a hypothetical existing trade.
        events = causal_events("TEST", bars, DEFAULT_CONFIG, 10, 50, engine_factory=FakeEngine)
        self.assertEqual([e["signal_time"] for e in events], [10, 40])
        self.assertEqual(events[1]["known_at"], 39)
        self.assertNotIn("exit_time", events[1])

    def test_causal_extraction_prefix_does_not_read_future_prices(self):
        prefix = [{"open_time": t, "close_time": t + 9, "macd": 1} for t in (0, 10, 20, 30)]
        future = {"open_time": 40, "close_time": 49, "macd": 1, "close": 999999}
        before = causal_events("TEST", prefix, DEFAULT_CONFIG, 10, 40, engine_factory=FakeEngine)
        after = causal_events("TEST", prefix + [future], DEFAULT_CONFIG, 10, 40, engine_factory=FakeEngine)
        self.assertEqual(before, after)

    def test_metrics_are_money_equity_not_sum_of_trade_percentages(self):
        result = {"equity": [{"equity": 9000, "positions": 2, "gross_notional": 9000,
                              "committed_risk": 200, "short_risk": 100},
                             {"equity": 9900, "positions": 1, "gross_notional": 4000,
                              "committed_risk": 50, "short_risk": 0}],
                  "trades": [{"net_pnl": -100, "net_return": -.1, "net_r": -1,
                              "funding_cashflow": -2, "total_fees": 4}],
                  "rejections": [], "open_positions": [],
                  "parameters": {"initial_equity": 10000}}
        metrics = portfolio_metrics(result)
        self.assertAlmostEqual(metrics["total_return"], -.01)
        self.assertAlmostEqual(metrics["max_drawdown"], .1)
        self.assertEqual(metrics["max_positions"], 2)

    def test_two_frozen_candidates_and_paired_weekly_constant_delta(self):
        experiments = variants()
        self.assertEqual(len(experiments), 6)
        self.assertEqual({v["signal_variant"] for v in experiments.values()}, {"baseline", "V1"})
        self.assertEqual(experiments["risk_only"]["direction_risk_fraction"], {"long": .025, "short": .01})
        result = paired_weekly_test([.001] * 20, [0] * 20, iterations=100, seed=7)
        self.assertAlmostEqual(result["delta"], .001)
        self.assertAlmostEqual(result["ci_low"], .001)
        self.assertLess(result["p_value"], .05)

    def test_correlation_proxy_uses_remaining_quantity_and_preceding_closes(self):
        day = 86_400_000
        prices, price = [], 100
        for i in range(35):
            price *= 1 + ((i % 5) + 1) / 1000
            prices.append(dict(open_time=i * day, close_time=(i + 1) * day - 1, close=price))
        data = {"A": prices, "B": prices}
        result = {"trades": [{"symbol": s, "setup_id": s, "direction": "long", "quantity": 2,
                               "entry_time": 31 * day, "exit_time": 34 * day} for s in data],
                  "fills": [{"symbol": "A", "setup_id": "A", "kind": "exit", "quantity": 1, "time": 32 * day}],
                  "equity": [{"time": 33 * day, "equity": 1000}]}
        proxy = correlation_exposure(data, result)
        self.assertEqual(proxy["observations"], 1)
        self.assertAlmostEqual(proxy["peak_correlated_gross_equity"], prices[32]["close"] * 3 / 1000)
        # Alter future closes: the observed boundary's attribution is identical.
        extended = {s: [dict(b) for b in rows] for s, rows in data.items()}
        for rows in extended.values():
            rows[-1]["close"] = 9999
        self.assertEqual(proxy, correlation_exposure(extended, result))

    def test_prior_close_proxy_does_not_consume_next_open_fills_at_same_time(self):
        day = 86_400_000
        price, bars = 100, []
        for i in range(35):
            price *= 1 + ((i % 5) + 1) / 1000
            bars.append(dict(open_time=i * day, close_time=(i + 1) * day - 1, close=price))
        time = 33 * day
        result = {"trades": [{"symbol": s, "setup_id": s, "direction": "long", "quantity": 2,
                               "entry_time": 31 * day, "exit_time": time, "exit_phase": "open"} for s in ("A", "B")],
                  "fills": [{"symbol": s, "setup_id": s, "kind": "exit", "quantity": 2,
                              "time": time, "phase": "open"} for s in ("A", "B")],
                  "equity": [{"time": time, "equity": 1000}]}
        proxy = correlation_exposure({"A": bars, "B": bars}, result)
        self.assertAlmostEqual(proxy["peak_correlated_gross_equity"], bars[32]["close"] * 4 / 1000)

    def test_weekly_blocks_do_not_cross_unobserved_calendar_gaps(self):
        result = paired_weekly_test([.01, .02, .03, .04], [0] * 4,
                                    iterations=100, seed=1, segment_lengths=[2, 2])
        self.assertEqual(result["block_count"], 2)
        self.assertEqual(result["segment_lengths"], [2, 2])


if __name__ == "__main__":
    unittest.main()
