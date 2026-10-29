"""Synthetic equal-budget target and account invariants; no market PnL."""
from __future__ import annotations

import copy
import importlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from research_projects.trend_portfolio_v1.run_study import write
from research_projects.trend_portfolio_v1.test_target_weight_adapter import fixture as adapter_fixture


class T28EqualExposureTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("scripts.t28_equal_exposure"),
                             "equal exposure wrapper is not implemented")
        return importlib.import_module("scripts.t28_equal_exposure")

    def targets(self):
        return [dict(decision_time_ms=index * 900_000, known_at=index * 900_000,
                     weights=dict(BTCUSDT=btc, ETHUSDT=eth),
                     features=dict(BTCUSDT=dict(weight=btc, momentum=1 if btc else -1),
                                   ETHUSDT=dict(weight=eth, momentum=1 if eth else -1)),
                     strategy_id="T28_long_cash", decision_kind="weekly_closed_daily")
                for index, (btc, eth) in enumerate(((.5, .25), (.3, 0.), (0., .2), (0., 0.)))]

    def test_a_preserves_each_groups_original_signal_coverage_with_exact_half_budget(self):
        module = self.module()
        original = self.targets()
        before = copy.deepcopy(original)
        expected = {"both": [.5, .5, .5, 0.], "btc_only": [.5, .5, 0., 0.], "eth_only": [.5, 0., .5, 0.]}
        for portfolio in expected:
            result = module.transform_targets(original, "budget_all_signals", portfolio)
            self.assertEqual([sum(row["weights"].values()) for row in result], expected[portfolio])
            for previous, event in zip(original, result):
                self.assertEqual({k: v for k, v in event.items() if k != "weights"},
                                 {k: v for k, v in previous.items() if k != "weights"})
                self.assertTrue(all(0 <= weight <= .5 for weight in event["weights"].values()))
        both = module.transform_targets(original, "budget_all_signals", "both")
        self.assertAlmostEqual(both[0]["weights"]["BTCUSDT"] / both[0]["weights"]["ETHUSDT"], 2.)
        self.assertEqual(original, before)

    def test_b_shared_gate_keeps_all_events_and_zeroes_every_group_when_either_signal_is_off(self):
        module = self.module()
        original = self.targets()
        for portfolio in ("both", "btc_only", "eth_only"):
            events = module.transform_targets(original, "matched_joint_signals", portfolio)
            self.assertEqual(len(events), len(original))
            self.assertEqual([sum(event["weights"].values()) for event in events], [.5, 0., 0., 0.])
            if portfolio == "btc_only":
                self.assertEqual(events[0]["weights"], dict(BTCUSDT=.5, ETHUSDT=0.))
            if portfolio == "eth_only":
                self.assertEqual(events[0]["weights"], dict(BTCUSDT=0., ETHUSDT=.5))
        with self.assertRaises(ValueError):
            module.transform_targets(original, "lookahead_mean", "both")

    def test_original_solver_matches_half_target_after_costs_and_joint_exit_with_same_clocks(self):
        module = self.module()
        data, funding, protocol, scenario, _targets = adapter_fixture(count=4)
        data = {"BTCUSDT": data["A"], "ETHUSDT": data["B"]}
        funding = {"BTCUSDT": funding["A"], "ETHUSDT": funding["B"]}
        protocol["symbols"] = list(data)
        scenario.update(fee_rate_per_fill=.001, adverse_slippage_bps=2.)
        original = self.targets()[:2]
        for portfolio in ("both", "btc_only", "eth_only"):
            targets = module.transform_targets(original, "matched_joint_signals", portfolio)
            account = module.replay_case(data, funding, targets, protocol, scenario, portfolio)
            self.assertEqual([row["time"] for row in account["rebalances"]], [900_000, 1_800_000])
            self.assertAlmostEqual(account["rebalances"][0]["post_gross_notional"] / account["rebalances"][0]["post_equity"], .5)
            self.assertEqual(account["rebalances"][1]["post_gross_notional"], 0.)
            self.assertTrue(all(position["quantity"] == 0 for position in account["final_positions"].values()))
            self.assertLessEqual(module.exposure_audit(account, targets)["post_rebalance_max_abs_error"], 1e-10)

    def test_prepare_freezes_every_mode_then_synthetic_36_accounts_and_hash_drift_blocks(self):
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
            start, end, step = 67 * 86_400_000, 69 * 86_400_000, 900_000
            for symbol in effective["symbols"]:
                source["symbols"][symbol] = {}
                raw = [[time, str(100 + time // 86_400_000), str(101 + time // 86_400_000), "99", str(100 + time // 86_400_000), "1", time + step - 1]
                       for time in range(0, end, step)]
                funding = dict(symbol=symbol, exchange="binance", market="linear_perpetual", source="Binance /fapi/v1/fundingRate",
                    complete=True, gaps=[], start=start, end=end,
                    records=[dict(time=t + 3, rate=.0001, mark_price=100.) for t in range(start, end, 28_800_000)],
                    raw_records=[dict(symbol=symbol, fundingTime=t + 3, fundingRate="0.0001", markPrice="100") for t in range(start, end, 28_800_000)])
                for key, payload in (("raw15m", raw), ("funding", funding)):
                    path = root / f"{symbol}-{key}.json"
                    path.write_text(json.dumps(payload))
                    source["symbols"][symbol][key] = dict(path=path.name, sha256=module.sha(path))
            input_manifest = root / "inputs.json"
            input_manifest.write_text(json.dumps(source))
            write(old / "effective_protocol.json", effective)
            temporary = dict(input_manifest=str(input_manifest), end=effective["scoring"]["end_exclusive_utc"])
            data, _funding = module.load_data(temporary)
            original = module.build_weekly_targets(data, effective, "T28_long_cash")
            write(old / "targets_before_pnl.json", {"T28_long_cash": original})
            costs = [dict(id=name, fee_rate=.001 * multiple, slippage_bps=2 * multiple, delay_bars=delay)
                     for name, multiple, delay in (("base", 1, 0), ("double_cost", 2, 0), ("delay15m", 1, 1))]
            costs_path = root / "costs.json"
            costs_path.write_text(json.dumps(dict(scenarios=costs)))
            datasets = [dict(id=name, start=effective["scoring"]["start_inclusive_utc"], end=effective["scoring"]["end_exclusive_utc"],
                input_manifest=str(input_manifest), prepared_directory=str(old),
                cost_mapping={cost["id"]: cost["id"] for cost in costs}, scope="SYNTHETIC") for name in ("main", "cross")]
            protocol = root / "protocol.json"
            document = dict(datasets=datasets, costs_path=str(costs_path), cost_scenarios=costs, source_bindings={},
                modes=["budget_all_signals", "matched_joint_signals"], gross_budget=.5,
                portfolios={"both": ["BTCUSDT", "ETHUSDT"], "btc_only": ["BTCUSDT"], "eth_only": ["ETHUSDT"]})
            protocol.write_text(json.dumps(document))
            frozen = module.prepare(protocol, root / "prepared")
            self.assertFalse(frozen["pnl_computed"])
            targets = module.read(root / "prepared" / "targets_before_pnl.json")
            self.assertEqual(set(targets["main"]), {"budget_all_signals", "matched_joint_signals"})
            self.assertFalse((root / "prepared" / "accounts").exists())
            result = module.execute(root / "prepared", root / "run")
            self.assertEqual(len(result["cases"]), 36)
            self.assertTrue(all(case["exposure_audit"]["post_rebalance_max_abs_error"] <= 1e-10 for case in result["cases"]))
            self.assertFalse(result["promotion_eligible"])
            path = root / "prepared" / "targets_before_pnl.json"
            path.write_bytes(path.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "drift|hash"):
                module.execute(root / "prepared", root / "drift")
            self.assertFalse((root / "drift").exists())
            document["source_bindings"] = {str(module.ROOT / "research_projects/trend_portfolio_v1/target_weight_adapter.py"): "bad-old-expected"}
            protocol.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "drift|hash"):
                module.prepare(protocol, root / "bad_prepare")


if __name__ == "__main__":
    unittest.main()
