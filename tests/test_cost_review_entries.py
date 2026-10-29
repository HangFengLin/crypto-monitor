"""Synthetic fixed-path cost checks, never evidence of market profitability."""
from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

STEP, HIGHER = 900_000, 14_400_000


class CostReviewEntriesTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("scripts.cost_review_entries"),
                             "fixed-entry cost adapter is not implemented")
        return importlib.import_module("scripts.cost_review_entries")

    def test_marked_gross_fee_and_signed_funding_bridge_is_not_a_zero_cost_backtest(self):
        module = self.module()
        account = dict(parameters=dict(initial_equity=1000.),
                       fills=[dict(gross_pnl=120., fee=10.)],
                       equity=[dict(equity=1113., unrealized_pnl=5.)])
        summary = dict(fees_usdt=10., funding_cashflow_usdt=-2., net_return=.113)
        result = module.pnl_decomposition(account, summary)
        self.assertEqual(result["gross_realized_pnl_usdt"], 120.)
        self.assertEqual(result["tail_unrealized_pnl_usdt"], 5.)
        self.assertEqual(result["gross_marked_before_fees_and_funding_usdt"], 125.)
        self.assertEqual(result["fees_usdt"], 10.)
        self.assertEqual(result["funding_cashflow_usdt"], -2.)
        self.assertEqual(result["net_marked_pnl_usdt"], 113.)
        self.assertAlmostEqual(result["reconciliation_error_usdt"], 0.)
        self.assertFalse(result["zero_cost_replay"])
        wrong = copy.deepcopy(summary)
        wrong["funding_cashflow_usdt"] = 2.
        with self.assertRaisesRegex(ValueError, "reconciliation"):
            module.pnl_decomposition(account, wrong)

    def test_cases_are_explicit_and_duplicate_entry_exit_hold_is_rejected(self):
        module = self.module()
        cases = [dict(id="D48-control", entry="D48", exit_policy="legacy1R", max_hold_bars=96),
                 dict(id="D48-reference", entry="D48", exit_policy="target3", max_hold_bars=672)]
        self.assertEqual(module.validate_cases(cases), cases)
        duplicate = copy.deepcopy(cases)
        duplicate.append({**cases[0], "id": "another-label"})
        with self.assertRaisesRegex(ValueError, "duplicate"):
            module.validate_cases(duplicate)
        unknown = copy.deepcopy(cases)
        unknown[0]["entry"] = "new-unfrozen-entry"
        with self.assertRaisesRegex(ValueError, "frozen"):
            module.validate_cases(unknown)

    def test_serial_cost_replay_keeps_events_stops_and_prior_cost_control_bytes(self):
        module = self.module()
        old = importlib.import_module("scripts.exit_horizon_screen").load_protocol()
        data = {symbol: [dict(open_time=i * STEP, close_time=(i + 1) * STEP - 1,
                              open=100., high=100.1, low=99.9, close=100., volume=100., atr=1.)
                         for i in range(8)] for symbol in module.SYMBOLS}
        data["BTCUSDT"][2].update(high=106., low=97.)
        funding = {symbol: dict(complete=True, start=0, end=8 * STEP, records=[]) for symbol in data}
        events = {entry: [dict(symbol="BTCUSDT", direction="long", setup_id=entry,
                               signal_time=2 * STEP, known_at=STEP, stop_loss=98.)]
                  for entry in module.ENTRIES}
        original_events = copy.deepcopy(events)
        cases = [dict(id="D48-control", entry="D48", exit_policy="legacy1R", max_hold_bars=96),
                 dict(id="D48-reference", entry="D48", exit_policy="target3", max_hold_bars=672)]
        scenarios = [dict(id="old_assumption_control", fee_rate=.001, slippage_bps=2, delay_bars=0,
                          account_fee_verified=False),
                     dict(id="lower_fee_test", fee_rate=.0005, slippage_bps=2, delay_bars=0,
                          account_fee_verified=False)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prior = root / "prior/accounts"
            prior.mkdir(parents=True)
            for case in cases:
                policy = next(item for item in old["exit_policies"] if item["id"] == case["exit_policy"])
                parameters = {**old["execution"], "stop_mode": policy["stop_mode"],
                              "reward_risk": policy["reward_risk"], "max_hold_bars": case["max_hold_bars"],
                              "fee_rate": .001, "slippage_bps": 2, "delay_bars": 0}
                account = module.replay_portfolio(data, events[case["entry"]], funding, **parameters,
                                                   strict_funding=True, finalize=False)
                name = module.prior_case_name(case, "base")
                (prior / f"{name}.json").write_bytes(module._bytes(account))
            replay_original, calls = module.replay_portfolio, []
            output = root / "output"

            def replay(*args, **kwargs):
                self.assertEqual(len(list((output / "accounts").glob("*.json"))), len(calls))
                calls.append(1)
                return replay_original(*args, **kwargs)

            with patch.object(module, "replay_portfolio", side_effect=replay):
                result = module.run_cases(data, events, funding, cases, scenarios, old["execution"],
                                          old, root / "prior", output, 3 * STEP)
            self.assertEqual(len(calls), 4)
            self.assertEqual(events, original_events)
            self.assertEqual(len(result["rows"]), 4)
            self.assertEqual(len(result["controls"]), 2)
            self.assertTrue(all(item["identical_bytes"] for item in result["controls"].values()))
            for row in result["rows"].values():
                self.assertIn("pnl_decomposition", row)
                self.assertIn("recent", row)
                self.assertFalse(row["account_fee_verified"])
                self.assertNotIn("equity", row)
            path = prior / f"{module.prior_case_name(cases[0], 'base')}.json"
            path.write_bytes(path.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "control"):
                module.run_cases(data, events, funding, cases, scenarios, old["execution"],
                                 old, root / "prior", root / "bad", 3 * STEP)

    def test_freeze_then_bound_full_runner_retains_legacy_cost_evidence_limits(self):
        module = self.module()
        previous_protocol = importlib.import_module("scripts.exit_horizon_screen").load_protocol()
        start, end, recent = 400 * HIGHER, 400 * HIGHER + 32 * STEP, 400 * HIGHER + 16 * STEP
        previous_protocol.update(start=start, end=end, recent_start=recent)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry_run, exit_run = root / "entry", root / "exit"
            entry_run.mkdir()
            (exit_run / "accounts").mkdir(parents=True)
            inputs = dict(schema_version=1, symbols={})
            for symbol in module.SYMBOLS:
                inputs["symbols"][symbol] = {}
                for label, step in (("raw15m", STEP), ("raw4h", HIGHER)):
                    payload = [[t, "100", "101", "99", "100", "100", t + step - 1]
                               for t in range(start - 300 * step, end // step * step, step)]
                    path = root / f"{symbol}-{label}.json"
                    path.write_bytes(module._bytes(payload))
                    inputs["symbols"][symbol][label] = dict(path=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                funding = dict(symbol=symbol, exchange="binance", market="linear_perpetual",
                               source="Binance /fapi/v1/fundingRate", complete=True, gaps=[], start=start, end=end,
                               records=[dict(time=start, rate=0., mark_price=100.)],
                               raw_records=[dict(symbol=symbol, fundingTime=start, fundingRate="0", markPrice="100")])
                path = root / f"{symbol}-funding.json"
                path.write_bytes(module._bytes(funding))
                inputs["symbols"][symbol]["funding"] = dict(path=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            source = root / "inputs.json"
            source.write_bytes(module._bytes(inputs))
            events = {entry: [] for entry in module.ENTRIES}
            (entry_run / "signals_before_pnl.json").write_bytes(module._bytes(dict(events=events, decisions=[])))
            (exit_run / "frozen_entry_events.json").write_bytes(module._bytes(events))
            (entry_run / "protocol.json").write_bytes(module._bytes(previous_protocol))
            (exit_run / "protocol.json").write_bytes(module._bytes(previous_protocol))
            (entry_run / "run_manifest.json").write_bytes(module._bytes(dict(
                start=start, end=end, recent_start=recent, input_manifest_sha256=hashlib.sha256(source.read_bytes()).hexdigest())))
            (exit_run / "run_manifest.json").write_bytes(module._bytes(dict(
                frozen_bindings={str(source): hashlib.sha256(source.read_bytes()).hexdigest()}, input_manifest=str(source))))
            (exit_run / "summary.json").write_bytes(module._bytes(dict(scope="HISTORICAL_EXPLORATORY", rows={})))
            (entry_run / "artifact_hashes.json").write_bytes(module._bytes({
                "signals_before_pnl.json": hashlib.sha256((entry_run / "signals_before_pnl.json").read_bytes()).hexdigest()}))
            data = {symbol: [dict(open_time=t, close_time=t + STEP - 1, open=100., high=101., low=99.,
                                  close=100., volume=100., atr=2.) for t in range(start, end, STEP)] for symbol in module.SYMBOLS}
            funding = {symbol: json.loads((root / f"{symbol}-funding.json").read_bytes()) for symbol in module.SYMBOLS}
            for case in module.FIXED_CASES:
                policy = next(p for p in previous_protocol["exit_policies"] if p["id"] == case["exit_policy"])
                for scenario in previous_protocol["scenarios"]:
                    parameters = {**previous_protocol["execution"], "stop_mode": policy["stop_mode"],
                                  "reward_risk": policy["reward_risk"], "max_hold_bars": case["max_hold_bars"],
                                  **{k: v for k, v in scenario.items() if k != "id"}}
                    account = module.replay_portfolio(data, [], funding, **parameters, strict_funding=True, finalize=False)
                    path = exit_run / "accounts" / f"{module.prior_case_name(case, scenario['id'])}.json"
                    path.write_bytes(module._bytes(account))
            paths = [exit_run / "frozen_entry_events.json", exit_run / "summary.json", *(exit_run / "accounts").glob("*.json")]
            (exit_run / "artifact_hashes.json").write_bytes(module._bytes({
                str(p.relative_to(exit_run)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}))
            frozen = module.freeze(entry_run, exit_run, root / "frozen")
            self.assertEqual(frozen["status"], "FROZEN_COST_PENDING")
            self.assertEqual(len(frozen["cases"]), 7)
            self.assertFalse(list((root / "frozen").glob("accounts/*.json")))
            protocol = dict(scope="HISTORICAL_EXPLORATORY", research_only=True, start=start, end=end,
                            recent_start=recent, entry_cases=module.FIXED_CASES, execution=previous_protocol["execution"])
            protocol_path = root / "protocol.json"
            protocol_path.write_bytes(module._bytes(protocol))
            costs = dict(schema_version=1, status="HYPOTHETICAL_LEGACY_CONTROLS_ONLY", actual_account_fee_verified=False,
                         slippage_verified=False, not_actual_cost_validation=True,
                         scenarios=[dict(id="legacy_base", fee_rate=.001, slippage_bps=2, delay_bars=0,
                                         account_fee_verified=False, source="synthetic test")])
            costs_path = root / "costs.json"
            costs_path.write_bytes(module._bytes(costs))
            result = module.run(root / "frozen", protocol_path, costs_path, root / "output")
            self.assertEqual(len(result["rows"]), 7)
            self.assertEqual(len(result["controls"]), 7)
            self.assertEqual(result["cost_verification"]["status"], "HYPOTHETICAL_LEGACY_CONTROLS_ONLY")
            self.assertFalse(result["cost_verification"]["actual_account_fee_verified"])
            self.assertTrue(result["cost_verification"]["not_actual_cost_validation"])
            self.assertFalse(result["promotion_eligible"])
            with self.assertRaisesRegex(ValueError, "empty"):
                module.run(root / "frozen", protocol_path, costs_path, root / "output")
            frozen_events = root / "frozen/events.json"
            frozen_events.write_bytes(module._bytes({entry: [dict(fake=True)] for entry in module.ENTRIES}))
            with self.assertRaisesRegex(ValueError, "SHA"):
                module.run(root / "frozen", protocol_path, costs_path, root / "bad-output")


if __name__ == "__main__":
    unittest.main()
