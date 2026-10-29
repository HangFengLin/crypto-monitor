"""Synthetic masking and real target-account tests, without market PnL."""
from __future__ import annotations

import copy
import importlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

STEP = 900_000


class T28PortfolioCompareTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("scripts.t28_portfolio_compare"),
                             "portfolio compare wrapper is not implemented")
        return importlib.import_module("scripts.t28_portfolio_compare")

    def fixture(self):
        data = {}
        for symbol, prices in (("BTCUSDT", [100, 100, 110, 120, 125, 130, 135, 140]),
                               ("ETHUSDT", [100, 100, 150, 200, 180, 210, 220, 230])):
            data[symbol] = [dict(open_time=i * STEP, close_time=(i + 1) * STEP - 1,
                open=float(price), high=float(price), low=float(price), close=float(price), volume=1.)
                for i, price in enumerate(prices)]
        funding = {symbol: dict(start=0, end=8 * STEP, complete=True,
            records=[dict(time=4 * STEP + 3, rate=.001, mark_price=100.)]) for symbol in data}
        protocol = dict(symbols=list(data), accounting=dict(initial_equity_usdt=10000),
            scoring=dict(start_inclusive_utc="1970-01-01T00:00:00Z", end_exclusive_utc="1970-01-01T02:00:00Z"))
        scenario = dict(id="legacy_base", fee_rate_per_fill=.001, adverse_slippage_bps=2., extra_delay_minutes=0)
        targets = [dict(decision_time_ms=i * STEP, known_at=i * STEP,
            weights=dict(BTCUSDT=.4, ETHUSDT=.35), features=dict(BTCUSDT={"weight": .4}, ETHUSDT={"weight": .35}),
            strategy_id="T28_long_cash", decision_kind="weekly_closed_daily") for i in (0, 2, 5)]
        return data, funding, protocol, scenario, targets

    def test_mask_changes_only_excluded_weights_and_never_reallocates_cash(self):
        module = self.module()
        _d, _f, _p, _s, targets = self.fixture()
        original = copy.deepcopy(targets)
        self.assertEqual(module.mask_targets(targets, "both"), targets)
        for portfolio, active, excluded in (("btc_only", "BTCUSDT", "ETHUSDT"), ("eth_only", "ETHUSDT", "BTCUSDT")):
            masked = module.mask_targets(targets, portfolio)
            for before, after in zip(targets, masked):
                self.assertEqual(after["weights"][active], before["weights"][active])
                self.assertEqual(after["weights"][excluded], 0.)
                expected = copy.deepcopy(before)
                expected["weights"][excluded] = 0.
                self.assertEqual(after, expected)
        self.assertEqual(targets, original)
        with self.assertRaises(ValueError):
            module.mask_targets(targets, "normalized")

    def test_real_replays_leave_excluded_symbol_flat_and_are_not_contribution_subtraction(self):
        module = self.module()
        data, funding, protocol, scenario, targets = self.fixture()
        both = module.replay_case(data, funding, targets, protocol, scenario, "both")
        btc = module.replay_case(data, funding, module.mask_targets(targets, "btc_only"), protocol, scenario, "btc_only")
        for portfolio, excluded in (("btc_only", "ETHUSDT"), ("eth_only", "BTCUSDT")):
            result = module.replay_case(data, funding, module.mask_targets(targets, portfolio), protocol, scenario, portfolio)
            self.assertFalse(any(row["symbol"] == excluded for row in result["fills"]))
            self.assertTrue(all(row["quantities"][excluded] == 0 for row in result["equity"]))
            self.assertTrue(all(row["held_quantity"] == row["cashflow"] == 0 for row in result["funding"] if row["symbol"] == excluded))
            self.assertEqual(result["by_symbol"][excluded]["fees"], 0.)
            self.assertEqual(result["by_symbol"][excluded]["funding"], 0.)
        self.assertNotAlmostEqual(btc["final_equity"], 10000 + both["by_symbol"]["BTCUSDT"]["net_pnl"], places=4)
        self.assertNotEqual(btc["final_positions"]["BTCUSDT"]["quantity"], both["final_positions"]["BTCUSDT"]["quantity"])

    def test_both_control_uses_exact_original_account_bytes_and_mismatch_blocks(self):
        module = self.module()
        data, funding, protocol, scenario, targets = self.fixture()
        result = module.replay_case(data, funding, targets, protocol, scenario, "both")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, fresh = root / "original.json.gz", root / "fresh.json.gz"
            module.write(original, result)
            digest = module.write_account(fresh, result, original)
            self.assertEqual(digest, module.sha(original))
            self.assertEqual(fresh.read_bytes(), original.read_bytes())
            changed = copy.deepcopy(result)
            changed["portfolio_label"] = "both"
            with self.assertRaisesRegex(ValueError, "control|exact|byte"):
                module.write_account(root / "changed.json.gz", changed, original)

    def test_prepare_freezes_all_masks_before_real_synthetic_matrix_and_drift_blocks(self):
        module = self.module()
        self.assertTrue(hasattr(module, "prepare"), "prepare and execute are not implemented")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old"
            old.mkdir()
            start, end = 67 * 86_400_000, 69 * 86_400_000
            effective = dict(symbols=["BTCUSDT", "ETHUSDT"], candidates=[dict(id="T28_long_cash", lookback_days=28)],
                accounting=dict(initial_equity_usdt=10000), scoring=dict(start_inclusive_utc="1970-03-09T00:00:00Z",
                end_exclusive_utc="1970-03-11T00:00:00Z", predeclared_segment_boundaries_utc=["1970-03-09T00:00:00Z", "1970-03-11T00:00:00Z"]))
            source = dict(symbols={})
            for symbol in effective["symbols"]:
                source["symbols"][symbol] = {}
                raw = [[time, str(100 + time // 86_400_000), str(101 + time // 86_400_000), "99", str(100 + time // 86_400_000), "1", time + STEP - 1]
                       for time in range(0, end, STEP)]
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
            module.write(old / "effective_protocol.json", effective)
            temporary = dict(input_manifest=str(input_manifest), end=effective["scoring"]["end_exclusive_utc"])
            data, funding = module.load_data(temporary)
            targets = module.build_weekly_targets(data, effective, "T28_long_cash")
            module.write(old / "targets_before_pnl.json", {"T28_long_cash": targets})
            costs = [dict(id=name, fee_rate=.001 * multiple, slippage_bps=2 * multiple, delay_bars=delay)
                     for name, multiple, delay in (("base", 1, 0), ("double_cost", 2, 0), ("delay15m", 1, 1))]
            costs_path = root / "costs.json"
            costs_path.write_text(json.dumps(dict(scenarios=costs)))
            accounts = root / "accounts"
            accounts.mkdir()
            for scenario in costs:
                account = module.replay_target_weights(data, funding, targets, effective, module.target_cost(scenario))
                module.write(accounts / f"T28_long_cash__{scenario['id']}.json.gz", account)
            datasets = [dict(id=name, start=effective["scoring"]["start_inclusive_utc"], end=effective["scoring"]["end_exclusive_utc"],
                input_manifest=str(input_manifest), prepared_directory=str(old), account_directory=str(accounts),
                cost_mapping={cost["id"]: cost["id"] for cost in costs}, scope="SYNTHETIC") for name in ("main", "cross")]
            protocol = root / "protocol.json"
            protocol.write_text(json.dumps(dict(datasets=datasets, costs_path=str(costs_path), cost_scenarios=costs,
                source_bindings={})))
            frozen = module.prepare(protocol, root / "prepared")
            self.assertFalse(frozen["pnl_computed"])
            self.assertFalse((root / "prepared" / "accounts").exists())
            all_targets = module.read(root / "prepared" / "targets_before_pnl.json")
            self.assertEqual(set(all_targets), {"main", "cross"})
            self.assertEqual(set(all_targets["main"]), {"both", "btc_only", "eth_only"})
            result = module.execute(root / "prepared", root / "run")
            self.assertEqual(len(result["cases"]), 18)
            self.assertEqual(len(result["both_control_sha256"]), 6)
            self.assertTrue(all(row["both_control_exact"] for row in result["cases"] if row["portfolio"] == "both"))
            self.assertEqual([row["portfolio"] for row in result["cases"][:6]], ["both"] * 6)
            path = root / "prepared" / "targets_before_pnl.json"
            path.write_bytes(path.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "drift|hash"):
                module.execute(root / "prepared", root / "drift_run")
            self.assertFalse((root / "drift_run").exists())
            document = module.read(protocol)
            document["source_bindings"] = {str(module.ROOT / "research_projects/trend_portfolio_v1/target_weight_adapter.py"): "old-expected-hash"}
            protocol.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "drift|hash"):
                module.prepare(protocol, root / "bad_source_prepare")
            self.assertFalse((root / "bad_source_prepare").exists())


if __name__ == "__main__":
    unittest.main()
