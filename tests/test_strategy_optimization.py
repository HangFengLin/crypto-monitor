import gzip
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from strategy import DEFAULT_CONFIG
from strategy_optimization import (
    _symbol_job,
    causal_events,
    classify_coverage,
    compute_signal_binding_sha256,
    correlation_exposure,
    inspect_signal_cache_coverage,
    paired_weekly_test,
    portfolio_metrics,
    run_replays,
    sha256,
    signal_binding_material,
    signal_cache_reusable,
    variants,
)


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


STEP = 900_000


def _write_prepared(path, symbol, n=40, start=1_700_000_000_000, coverage=None, price_base=100):
    bars = []
    for i in range(n):
        open_time = start + i * STEP
        close_time = open_time + STEP - 1
        price = price_base + i * 0.01
        bars.append({
            "time": "2024-01-01T00:00:00+00:00",
            "open_time": open_time,
            "close_time": close_time,
            "open": price,
            "high": price + 1,
            "low": price - 1,
            "close": price,
            "volume": 10,
            "atr": 1,
            "macd": 1,
        })
    if coverage is None:
        coverage = {"symbol": symbol, "exchange": "binance", "expected_bars": n,
                    "missing_bars": 0, "kline_coverage": 1.0}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump({"bars": bars, "coverage": coverage}, handle)
    return path, bars, coverage


def _fold(name="fold1", start=1_700_000_000_000, bars=40):
    return dict(name=name, start=start, end=start + bars * STEP, exposure="DEVELOPMENT_EXPOSED")


def _fake_events(symbol, bars, config, entry_start, entry_end, **kwargs):
    marker = getattr(config, "ma_fast_period", None)
    return [dict(symbol=symbol, direction="long", stop_loss=1.0,
                 setup_id=f"{symbol}:{marker}", signal_time=entry_start,
                 known_at=entry_start, score=marker, structure_score=0,
                 rule_marker=marker)]


def _load_cache(path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


class StrategyOptimizationMinAcceptTest(unittest.TestCase):
    def test_signal_cache_reuse_requires_rule_parameter_binding(self):
        window = _fold()
        current = compute_signal_binding_sha256()
        matching = dict(symbol="AAA", window=window, source_sha256="abc",
                        events={"baseline": [{"setup_id": "stale"}]}, binding_sha256=current)
        self.assertTrue(signal_cache_reusable(matching, symbol="AAA", source_hash="abc",
                                              window=window, binding_sha256=current))
        changed = compute_signal_binding_sha256(signal_binding_material(
            replace(DEFAULT_CONFIG, ma_fast_period=DEFAULT_CONFIG.ma_fast_period + 1)))
        self.assertNotEqual(current, changed)
        self.assertFalse(signal_cache_reusable(matching, symbol="AAA", source_hash="abc",
                                               window=window, binding_sha256=changed))
        missing = dict(symbol="AAA", window=window, source_sha256="abc", events={})
        self.assertFalse(signal_cache_reusable(missing, symbol="AAA", source_hash="abc",
                                               window=window, binding_sha256=current))

    def test_rule_or_parameter_change_refuses_stale_signal_cache_reuse(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source, _, _ = _write_prepared(root / "AAA.json.gz", "AAA", price_base=100)
            window = _fold()
            cache_path = root / "out" / "signal_cache" / window["name"] / "AAA.json.gz"
            task = ("AAA", str(source), [window], str(root / "out"))
            with patch("strategy_optimization.causal_events", side_effect=_fake_events):
                first = _symbol_job(task)
            original = _load_cache(cache_path)
            binding = original["binding_sha256"]
            self.assertEqual(binding, compute_signal_binding_sha256())
            self.assertEqual(original["source_sha256"], sha256(source))
            self.assertEqual(original["events"]["baseline"][0]["rule_marker"], DEFAULT_CONFIG.ma_fast_period)
            poisoned = dict(original)
            poisoned["events"] = {"baseline": [{"setup_id": "poison", "symbol": "AAA"}],
                                  "V1": [{"setup_id": "poison", "symbol": "AAA"}]}
            with gzip.open(cache_path, "wt", encoding="utf-8") as handle:
                json.dump(poisoned, handle)
            with patch("strategy_optimization.causal_events", side_effect=_fake_events):
                _symbol_job(task)
            reused = _load_cache(cache_path)
            self.assertEqual(reused["events"]["baseline"][0]["setup_id"], "poison")
            changed_config = replace(DEFAULT_CONFIG, ma_fast_period=DEFAULT_CONFIG.ma_fast_period + 3)
            with patch("strategy_optimization.DEFAULT_CONFIG", changed_config), \
                    patch("strategy_optimization.causal_events", side_effect=_fake_events):
                _symbol_job(task)
                new_binding = compute_signal_binding_sha256()
            refreshed = _load_cache(cache_path)
            self.assertNotEqual(new_binding, binding)
            self.assertEqual(refreshed["binding_sha256"], new_binding)
            self.assertNotEqual(refreshed["events"]["baseline"][0].get("setup_id"), "poison")
            self.assertEqual(refreshed["events"]["baseline"][0]["rule_marker"], changed_config.ma_fast_period)
            self.assertEqual(refreshed["source_sha256"], original["source_sha256"])
            self.assertEqual(refreshed["window"], window)
            self.assertEqual(first["symbol"], "AAA")

    def test_two_symbol_signal_caches_do_not_cross_contaminate(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_a, _, _ = _write_prepared(root / "AAA.json.gz", "AAA", price_base=100)
            source_b, _, _ = _write_prepared(root / "BBB.json.gz", "BBB", price_base=500)
            window = _fold()
            out = root / "out"
            with patch("strategy_optimization.causal_events", side_effect=_fake_events):
                _symbol_job(("AAA", str(source_a), [window], str(out)))
                _symbol_job(("BBB", str(source_b), [window], str(out)))
            path_a = out / "signal_cache" / window["name"] / "AAA.json.gz"
            path_b = out / "signal_cache" / window["name"] / "BBB.json.gz"
            self.assertNotEqual(path_a, path_b)
            cache_a = _load_cache(path_a)
            cache_b = _load_cache(path_b)
            self.assertEqual(cache_a["symbol"], "AAA")
            self.assertEqual(cache_b["symbol"], "BBB")
            self.assertNotEqual(cache_a["source_sha256"], cache_b["source_sha256"])
            self.assertTrue(all(bar["close"] < 200 for bar in cache_a["bars"]))
            self.assertTrue(all(bar["close"] > 400 for bar in cache_b["bars"]))
            self.assertTrue(all(event["symbol"] == "AAA" for events in cache_a["events"].values() for event in events))
            self.assertTrue(all(event["symbol"] == "BBB" for events in cache_b["events"].values() for event in events))
            path_b.write_bytes(path_a.read_bytes())
            planted = _load_cache(path_b)
            self.assertEqual(planted["symbol"], "AAA")
            with patch("strategy_optimization.causal_events", side_effect=_fake_events):
                _symbol_job(("BBB", str(source_b), [window], str(out)))
            repaired = _load_cache(path_b)
            self.assertEqual(repaired["symbol"], "BBB")
            self.assertEqual(repaired["source_sha256"], sha256(source_b))
            self.assertTrue(all(bar["close"] > 400 for bar in repaired["bars"]))
            self.assertTrue(all(event["symbol"] == "BBB" for events in repaired["events"].values() for event in events))

    def test_classify_coverage_rejects_missing_and_invalid(self):
        self.assertEqual(classify_coverage(None)["reason"], "missing_coverage")
        self.assertEqual(classify_coverage({"symbol": "AAA"})["reason"], "incomplete_coverage_fields")
        self.assertEqual(classify_coverage({"expected_bars": 10, "missing_bars": 0, "error": "no_klines"})["reason"],
                         "coverage_error")
        self.assertEqual(classify_coverage({"expected_bars": 10, "missing_bars": 2, "kline_coverage": 0.8})["reason"],
                         "missing_bars")
        self.assertEqual(classify_coverage({"expected_bars": 10, "missing_bars": 0, "kline_coverage": 0.5})["reason"],
                         "incomplete_kline_coverage")
        self.assertTrue(classify_coverage({"expected_bars": 10, "missing_bars": 0, "kline_coverage": 1.0})["valid"])

    def test_missing_or_invalid_coverage_is_not_a_valid_economic_summary(self):
        cases = [
            ("missing", None, "missing_coverage"),
            ("error", {"symbol": "AAA", "expected_bars": 40, "missing_bars": 0, "error": "no_klines"}, "coverage_error"),
            ("gaps", {"symbol": "AAA", "expected_bars": 40, "missing_bars": 5, "kline_coverage": 0.875}, "missing_bars"),
        ]
        repo = Path(__file__).resolve().parents[1]
        for label, coverage, reason in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                if coverage is None:
                    source, _, _ = _write_prepared(root / "AAA.json.gz", "AAA")
                    window = _fold()
                    cache_path = root / "out" / "signal_cache" / window["name"] / "AAA.json.gz"
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    payload = dict(symbol="AAA", window=window, bars=[], events={},
                                   source_sha256=sha256(source),
                                   binding_sha256=compute_signal_binding_sha256())
                    with gzip.open(cache_path, "wt", encoding="utf-8") as handle:
                        json.dump(payload, handle)
                else:
                    source, _, _ = _write_prepared(root / "AAA.json.gz", "AAA", coverage=coverage)
                    window = _fold()
                    with patch("strategy_optimization.causal_events", side_effect=_fake_events):
                        _symbol_job(("AAA", str(source), [window], str(root / "out")))
                registry = {
                    "code_hashes": {"strategy.py": sha256(repo / "strategy.py")},
                    "folds": [window],
                    "input_sources": {"AAA": {"path": str(source), "sha256": sha256(source)}},
                    "signal_binding_sha256": compute_signal_binding_sha256(),
                }
                output = root / "out"
                with self.assertRaisesRegex(ValueError, "missing or invalid coverage cannot be a valid economic summary"):
                    run_replays(registry, output)
                comparison = json.loads((output / "comparison.json").read_text())
                metrics = json.loads((output / "fold_metrics.json").read_text())
                self.assertEqual(comparison["status"], "COVERAGE_INVALID")
                self.assertIs(comparison["economic_summary_valid"], False)
                self.assertIs(comparison["promotion_eligible"], False)
                self.assertIsNone(comparison["summary"])
                self.assertIsNone(comparison["comparisons"])
                self.assertEqual(comparison["diagnostic_positive"], [])
                self.assertNotIn("total_return", comparison)
                self.assertNotIn("linked_return", comparison)
                self.assertIs(metrics["economic_summary_valid"], False)
                self.assertEqual(metrics["rows"], [])
                self.assertTrue(any(item["reason"] == reason for item in comparison["invalid_coverage"]))
                self.assertFalse((output / "portfolio_replays").exists())
                self.assertFalse((output / "execution_snapshot.json").exists())
                issues = inspect_signal_cache_coverage(registry, output)
                self.assertTrue(issues)
                self.assertTrue(any(item["reason"] == reason for item in issues))


if __name__ == "__main__":
    unittest.main()
