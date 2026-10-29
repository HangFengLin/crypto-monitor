"""Causal covariance and shared-risk target invariants on synthetic prices."""
from __future__ import annotations

import copy
import importlib
import importlib.util
import json
import math
import statistics
import tempfile
import unittest
from pathlib import Path

from research_projects.trend_portfolio_v1.run_study import write

DAY, STEP = 86_400_000, 900_000


class T28EqualRiskTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("scripts.t28_equal_risk"),
                             "equal risk wrapper is not implemented")
        return importlib.import_module("scripts.t28_equal_risk")

    def event(self, covariance, weights=(.25, .2)):
        return dict(decision_time_ms=67 * DAY, known_at=67 * DAY,
                    weights=dict(zip(("BTCUSDT", "ETHUSDT"), weights)),
                    features={symbol: dict(weight=weights[i], annualized_volatility=math.sqrt(covariance[i][i]), momentum=.1 if weights[i] else -.1)
                              for i, symbol in enumerate(("BTCUSDT", "ETHUSDT"))},
                    strategy_id="T28_long_cash", decision_kind="weekly_closed_daily")

    def test_covariance_uses_sixty_aligned_log_returns_sample_ddof_one_and_matches_original_vol(self):
        module = self.module()
        closes = {symbol: [100.] for symbol in ("BTCUSDT", "ETHUSDT")}
        for i in range(60):
            closes["BTCUSDT"].append(closes["BTCUSDT"][-1] * math.exp(.01 if i % 2 else .03))
            closes["ETHUSDT"].append(closes["ETHUSDT"][-1] * math.exp(.04 if i % 3 else -.01))
        actual = module.estimate_covariance(closes)
        logs = {s: [math.log(b / a) for a, b in zip(values, values[1:])] for s, values in closes.items()}
        self.assertAlmostEqual(actual[0][0], statistics.variance(logs["BTCUSDT"]) * 365, places=14)
        self.assertAlmostEqual(actual[1][1], statistics.variance(logs["ETHUSDT"]) * 365, places=14)
        self.assertAlmostEqual(actual[0][1], statistics.covariance(logs["BTCUSDT"], logs["ETHUSDT"]) * 365, places=14)
        self.assertEqual(actual[0][1], actual[1][0])
        with self.assertRaises(ValueError):
            module.estimate_covariance({**closes, "BTCUSDT": closes["BTCUSDT"][:-1]})

    def test_common_feasible_budget_keeps_direction_and_matches_risk_under_half_gross(self):
        module = self.module()
        covariance = [[.01, .01], [.01, .04]]
        original = self.event(covariance, (.5, .5))
        before = copy.deepcopy(original)
        targets, sidecar = module.allocate_event(original, covariance)
        self.assertAlmostEqual(sidecar["common_target_risk"], .05)
        self.assertEqual(set(sidecar["A_eligible_portfolios"]), {"both", "btc_only", "eth_only"})
        self.assertEqual(targets["btc_only"]["weights"], dict(BTCUSDT=.5, ETHUSDT=0.))
        self.assertEqual(targets["eth_only"]["weights"], dict(BTCUSDT=0., ETHUSDT=.25))
        self.assertAlmostEqual(targets["both"]["weights"]["BTCUSDT"], targets["both"]["weights"]["ETHUSDT"])
        for portfolio, event in targets.items():
            self.assertLessEqual(sum(event["weights"].values()), .5 + 1e-12)
            self.assertAlmostEqual(sidecar["portfolios"][portfolio]["forecast_risk"], .05)
            self.assertEqual({k: v for k, v in event.items() if k != "weights"}, {k: v for k, v in original.items() if k != "weights"})
        self.assertEqual(original, before)

    def test_off_group_does_not_zero_active_budget_and_degenerate_or_invalid_estimation_blocks(self):
        module = self.module()
        covariance = [[.16, .06], [.06, .36]]
        targets, sidecar = module.allocate_event(self.event(covariance, (.25, 0.)), covariance)
        self.assertAlmostEqual(sidecar["common_target_risk"], .1)
        self.assertEqual(sidecar["A_eligible_portfolios"], ["both", "btc_only"])
        self.assertEqual(targets["eth_only"]["weights"], dict(BTCUSDT=0., ETHUSDT=0.))
        self.assertEqual(targets["both"], targets["btc_only"])
        with self.assertRaisesRegex(ValueError, "ZERO_FORECAST_RISK"):
            module.allocate_event(self.event([[0., 0.], [0., 0.]], (.5, .5)), [[0., 0.], [0., 0.]])
        tiny = math.ulp(0.)
        with self.assertRaisesRegex(ValueError, "ZERO_FORECAST_RISK"):
            module.allocate_event(self.event([[tiny, 0.], [0., tiny]], (.5, 0.)), [[tiny, 0.], [0., tiny]])
        cash, features = module.allocate_event(self.event([[0., 0.], [0., 0.]], (0., 0.)), [[0., 0.], [0., 0.]])
        self.assertEqual(features["common_target_risk"], 0.)
        self.assertTrue(all(sum(event["weights"].values()) == 0 for event in cash.values()))
        with self.assertRaisesRegex(ValueError, "PSD|covariance"):
            module.allocate_event(self.event([[1., 2.], [2., 1.]]), [[1., 2.], [2., 1.]])
        with self.assertRaisesRegex(ValueError, "finite|covariance"):
            module.allocate_event(self.event([[1., 0.], [0., 1.]]), [[1., math.nan], [math.nan, 1.]])
        module.allocate_event(self.event([[1., 1.], [1., 1.]]), [[1., 1.], [1., 1.]])
        bad = self.event(covariance)
        bad["features"]["BTCUSDT"]["annualized_volatility"] = .7
        with self.assertRaisesRegex(ValueError, "diagonal|volatility"):
            module.allocate_event(bad, covariance)

    def test_joint_reuses_a_exactly_and_appended_or_shocked_future_never_changes_old_prefix(self):
        module = self.module()
        data = {s: [] for s in ("BTCUSDT", "ETHUSDT")}
        prices = dict.fromkeys(data, 100.)
        for day in range(85):
            for symbol in data:
                prices[symbol] *= math.exp((.003 if day % 2 else .012) if symbol == "BTCUSDT" else (.02 if day % 3 else -.002))
                for offset in range(96):
                    time = day * DAY + offset * STEP
                    data[symbol].append(dict(open_time=time, close_time=time + STEP - 1,
                        open=prices[symbol], high=prices[symbol], low=prices[symbol], close=prices[symbol], volume=1.))
        protocol = dict(symbols=list(data), candidates=[dict(id="T28_long_cash", lookback_days=28)],
            scoring=dict(start_inclusive_utc="1970-03-09T00:00:00Z", end_exclusive_utc="1970-03-27T00:00:00Z"))
        original = module.build_weekly_targets(data, protocol, "T28_long_cash")
        targets, risk = module.build_risk_targets(data, original)
        for portfolio in targets["budget_all_signals"]:
            self.assertEqual(targets["budget_all_signals"][portfolio], targets["matched_joint_signals"][portfolio])
        self.assertEqual(risk[0]["close_end_ms"], list(range(original[0]["decision_time_ms"] - 60 * DAY, original[0]["decision_time_ms"] + DAY, DAY)))
        future = copy.deepcopy(data)
        for symbol in future:
            for row in future[symbol]:
                if row["open_time"] >= original[0]["decision_time_ms"]:
                    row.update(open=1e8, high=1e8, low=1e8, close=1e8)
            for t in range(85 * DAY, 86 * DAY, STEP):
                future[symbol].append(dict(open_time=t, close_time=t + STEP - 1, open=1e8, high=1e8, low=1e8, close=1e8, volume=1.))
        prefix_targets, prefix_risk = module.build_risk_targets(future, original[:1])
        for mode in targets:
            for portfolio in targets[mode]:
                self.assertEqual(prefix_targets[mode][portfolio][0], targets[mode][portfolio][0])
        self.assertEqual(prefix_risk, risk[:1])

    def test_prepare_sidecar_then_synthetic_36_replays_and_risk_hash_drift_blocks(self):
        module = self.module()
        self.assertTrue(hasattr(module, "prepare"), "prepare and execute are not implemented")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old"
            old.mkdir()
            effective = dict(symbols=["BTCUSDT", "ETHUSDT"], candidates=[dict(id="T28_long_cash", lookback_days=28)],
                accounting=dict(initial_equity_usdt=10000), scoring=dict(start_inclusive_utc="1970-03-09T00:00:00Z",
                end_exclusive_utc="1970-03-11T00:00:00Z", predeclared_segment_boundaries_utc=["1970-03-09T00:00:00Z", "1970-03-11T00:00:00Z"]))
            source = dict(symbols={})
            start, end = 67 * DAY, 69 * DAY
            for symbol in effective["symbols"]:
                source["symbols"][symbol] = {}
                price, raw = 100., []
                for day in range(69):
                    price *= math.exp((.003 if day % 2 else .012) if symbol == "BTCUSDT" else (.02 if day % 3 else -.002))
                    for time in range(day * DAY, (day + 1) * DAY, STEP):
                        raw.append([time, str(price), str(price * 1.01), str(price * .99), str(price), "1", time + STEP - 1])
                funding = dict(symbol=symbol, exchange="binance", market="linear_perpetual", source="Binance /fapi/v1/fundingRate",
                    complete=True, gaps=[], start=start, end=end,
                    records=[dict(time=t + 3, rate=.0001, mark_price=100.) for t in range(start, end, 28_800_000)],
                    raw_records=[dict(symbol=symbol, fundingTime=t + 3, fundingRate="0.0001", markPrice="100") for t in range(start, end, 28_800_000)])
                for key, payload in (("raw15m", raw), ("funding", funding)):
                    path = root / f"{symbol}-{key}.json"
                    path.write_text(json.dumps(payload))
                    source["symbols"][symbol][key] = dict(path=path.name, sha256=module.sha(path))
            source_path = root / "inputs.json"
            source_path.write_text(json.dumps(source))
            write(old / "effective_protocol.json", effective)
            data, _funding = module.load_data(dict(input_manifest=str(source_path), end=effective["scoring"]["end_exclusive_utc"]))
            original = module.build_weekly_targets(data, effective, "T28_long_cash")
            write(old / "targets_before_pnl.json", {"T28_long_cash": original})
            document = module.read(module.PROTOCOL)
            document["source_bindings"] = {}
            document["datasets"] = [dict(id=name, start=effective["scoring"]["start_inclusive_utc"], end=effective["scoring"]["end_exclusive_utc"],
                input_manifest=str(source_path), prepared_directory=str(old),
                cost_mapping={cost["id"]: cost["id"] for cost in document["cost_scenarios"]}, scope="SYNTHETIC") for name in ("main", "cross")]
            protocol = root / "protocol.json"
            protocol.write_text(json.dumps(document))
            frozen = module.prepare(protocol, root / "prepared")
            self.assertFalse(frozen["pnl_computed"])
            self.assertFalse((root / "prepared" / "accounts").exists())
            features = module.read(root / "prepared" / "risk_features_before_pnl.json")
            self.assertEqual(features["main"][0]["n_returns"], 60)
            result = module.execute(root / "prepared", root / "run")
            self.assertEqual(len(result["cases"]), 36)
            self.assertTrue(all(row["risk_audit"]["max_postfill_forecast_risk_error"] <= 1e-10 for row in result["cases"]))
            self.assertTrue(all(row["risk_audit"]["max_postfill_asset_weight_error"] <= 1e-10 for row in result["cases"]))
            self.assertFalse(result["promotion_eligible"])
            path = root / "prepared" / "risk_features_before_pnl.json"
            path.write_bytes(path.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "drift|hash"):
                module.execute(root / "prepared", root / "drift")
            self.assertFalse((root / "drift").exists())
            document["source_bindings"] = {str(module.ROOT / "research_projects/trend_portfolio_v1/target_weight_adapter.py"): "bad-original"}
            protocol.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "drift|hash"):
                module.prepare(protocol, root / "bad_prepare")
            self.assertFalse((root / "bad_prepare").exists())


if __name__ == "__main__":
    unittest.main()
