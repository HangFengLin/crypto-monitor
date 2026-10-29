"""Synthetic exit-study checks; no fixture is evidence of strategy performance."""
from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

STEP, HIGHER = 900_000, 14_400_000


class ExitHorizonScreenTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("scripts.exit_horizon_screen"),
                             "exit/horizon runner is not implemented")
        return importlib.import_module("scripts.exit_horizon_screen")

    def protocol(self, module):
        protocol = copy.deepcopy(importlib.import_module("scripts.entry_screen").load_protocol())
        protocol["entry_ids"] = protocol.pop("candidate_ids")
        protocol["exit_policies"] = [
            dict(id="legacy1R", stop_mode="structure_atr", reward_risk=2),
            dict(id="target2", stop_mode="structure_atr_target_only", reward_risk=2),
            dict(id="target3", stop_mode="structure_atr_target_only", reward_risk=3),
            dict(id="BE_then2", stop_mode="structure_atr_breakeven_after_1r", reward_risk=2),
        ]
        protocol["hold_bars"] = [96, 288, 672]
        return protocol

    def trade(self, name, signal=0, value=.02, risk=.2):
        return dict(symbol="BTCUSDT", direction="long", setup_id=name, signal_time=signal,
                    entry_time=signal, entry_price=100., initial_stop=98., net_return=value,
                    net_r=risk, net_pnl=value * 100, holding_bars=2, exit_reason="take_profit")

    def test_common_cohort_is_mature_and_compares_quantity_free_trade_results(self):
        module = self.module()
        baseline = [self.trade("a"), self.trade("b", STEP, -.01, -.1), self.trade("c", 2 * STEP)]
        variant = [self.trade("a", value=.03, risk=.3), self.trade("b", STEP, -.03, -.3),
                   self.trade("d", 2 * STEP), self.trade("late", 4 * STEP)]
        result = module.paired_closed_trades(baseline, variant, 2 * STEP)
        self.assertEqual(result["common_count"], 2)
        self.assertEqual((result["baseline_only_count"], result["variant_only_count"]), (1, 1))
        self.assertAlmostEqual(result["mean_net_return_difference"], -.005)
        self.assertAlmostEqual(result["mean_net_r_difference"], -.05)
        self.assertEqual((result["improved_count"], result["worsened_count"], result["unchanged_count"]), (1, 1, 0))
        self.assertAlmostEqual(result["median_net_return_difference"], -.005)
        self.assertAlmostEqual(result["mean_positive_return_difference"], .005)
        self.assertAlmostEqual(result["mean_negative_return_difference_magnitude"], .01)
        self.assertEqual(len(result["common"]), 2)
        self.assertEqual(result["maturity_cutoff"], 2 * STEP)
        for field in ("entry_time", "entry_price", "initial_stop"):
            broken = copy.deepcopy(variant)
            broken[0][field] += 1
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "entry"):
                module.paired_closed_trades(baseline, broken, 2 * STEP)

    def test_control_gate_requires_identical_bytes_not_similar_json(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            a, b = Path(directory) / "a.json", Path(directory) / "b.json"
            a.write_bytes(b'{"equity":100.0}')
            b.write_bytes(a.read_bytes())
            self.assertTrue(module.verify_control(a, b)["identical_bytes"])
            b.write_bytes(b'{"equity": 100.0}\n')
            self.assertEqual(json.loads(a.read_bytes()), json.loads(b.read_bytes()))
            with self.assertRaisesRegex(ValueError, "control"):
                module.verify_control(a, b)

    def test_account_deltas_keep_continuous_recent_equity_and_risk_separate(self):
        module = self.module()
        baseline = dict(net_return=.02, max_close_drawdown=-.05, fees_usdt=20., funding_cashflow_usdt=-2.,
                        closed_trades=5, trade_diagnostics=dict(net_win_rate=.4, payoff_ratio=1.2, profit_factor=1.1, mean_net_r=.1),
                        recent=dict(net_return=-.01, max_close_drawdown=-.03))
        variant = copy.deepcopy(baseline)
        variant.update(net_return=.05, max_close_drawdown=-.03, fees_usdt=15., closed_trades=3)
        variant["recent"]["net_return"] = .01
        delta = module.account_deltas(baseline, variant)
        self.assertAlmostEqual(delta["net_return"], .03)
        self.assertAlmostEqual(delta["recent_net_return"], .02)
        self.assertAlmostEqual(delta["max_close_drawdown"], .02)
        self.assertEqual(delta["fees_usdt"], -5.)
        self.assertEqual(delta["closed_trades"], -2)

    def test_serial_matrix_preserves_controls_and_writes_all_144_accounts(self):
        module = self.module()
        data = {s: [dict(open_time=i * STEP, close_time=(i + 1) * STEP - 1,
                         open=100., high=100.1, low=99.9, close=100., volume=100., atr=1.)
                    for i in range(8)] for s in module.SYMBOLS}
        data["BTCUSDT"][2].update(high=106., low=97.)
        funding = {s: dict(complete=True, start=0, end=8 * STEP,
                           records=[dict(time=2 * STEP + 100, rate=.001, mark_price=100.)]) for s in data}
        events = {v: [dict(symbol="BTCUSDT", direction="long", setup_id=v,
                          signal_time=2 * STEP, known_at=STEP, stop_loss=98.)]
                  for v in module.ENTRIES}
        protocol = self.protocol(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference/accounts"
            reference.mkdir(parents=True)
            for entry in module.ENTRIES:
                for scenario in protocol["scenarios"]:
                    params = {k: v for k, v in scenario.items() if k != "id"}
                    account = module.replay_portfolio(data, events[entry], funding, **protocol["execution"], **params,
                                                       strict_funding=True, finalize=False)
                    (reference / f"{entry}-{scenario['id']}.json").write_bytes(module._bytes(account))
            original, calls = module.replay_portfolio, []
            output = root / "run"

            def replay(*args, **kwargs):
                self.assertEqual(len(list((output / "accounts").glob("*.json"))), len(calls))
                calls.append(1)
                return original(*args, **kwargs)

            with patch.object(module, "replay_portfolio", side_effect=replay):
                result = module.run_matrix(data, events, funding, protocol, output, root / "reference", 3 * STEP, 8 * STEP)
            self.assertEqual(len(calls), 144)
            self.assertEqual(len(result["rows"]), 144)
            self.assertEqual(len(result["controls"]), 12)
            self.assertTrue(all(item["identical_bytes"] for item in result["controls"].values()))
            self.assertEqual(len(list((output / "accounts").glob("*.json"))), 144)
            self.assertEqual(len(list((output / "paired").glob("*.json"))), 144)
            for row in result["rows"].values():
                self.assertNotIn("equity", row)
                self.assertIn("delta_vs_legacy_24h", row)
                self.assertIn("delta_vs_same_policy_24h", row)
                self.assertIn("delta_vs_legacy_same_hold", row)
                self.assertIn("paired_vs_legacy_24h", row)
                self.assertIn("paired_vs_legacy_same_hold", row)
                self.assertIn("exit_reason_counts", row)
                self.assertEqual(row["maturity_cutoff"], 8 * STEP - 673 * STEP)
            bad = reference / "D48-base.json"
            bad.write_bytes(bad.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "control"):
                module.run_matrix(data, events, funding, protocol, root / "bad", root / "reference", 3 * STEP, 8 * STEP)

    def test_runner_binds_old_frozen_events_without_regenerating_an_entry(self):
        module = self.module()
        entry_module = importlib.import_module("scripts.entry_screen")
        start, end = 400 * HIGHER, 400 * HIGHER + 32 * STEP
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_protocol = copy.deepcopy(entry_module.load_protocol())
            old_protocol.update(start=start, end=end, recent_start=start + 16 * STEP)
            protocol = self.protocol(module)
            protocol.update(start=start, end=end, recent_start=start + 16 * STEP)
            protocol_path = root / "protocol.json"
            protocol_path.write_text(json.dumps(protocol))
            manifest = dict(schema_version=1, scope="HISTORICAL_EXPLORATORY", symbols={})
            for symbol in module.SYMBOLS:
                manifest["symbols"][symbol] = {}
                for label, step in (("raw15m", STEP), ("raw4h", HIGHER)):
                    payload = [[t, "100", "101", "99", "100", "100", t + step - 1]
                               for t in range(start - 300 * step, end // step * step, step)]
                    path = root / f"{symbol}-{label}.json"
                    path.write_text(json.dumps(payload))
                    manifest["symbols"][symbol][label] = dict(path=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                evidence = dict(symbol=symbol, exchange="binance", market="linear_perpetual",
                                source="Binance /fapi/v1/fundingRate", complete=True, gaps=[], start=start, end=end,
                                records=[dict(time=start, rate=0., mark_price=100.)],
                                raw_records=[dict(symbol=symbol, fundingTime=start, fundingRate="0", markPrice="100")])
                path = root / f"{symbol}-funding.json"
                path.write_text(json.dumps(evidence))
                manifest["symbols"][symbol]["funding"] = dict(path=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            source = root / "input_manifest.json"
            source.write_text(json.dumps(manifest))
            old = root / "entry-run"
            old.mkdir()
            (old / "protocol.json").write_text(json.dumps(old_protocol))
            events = dict(events={entry: [] for entry in (*module.ENTRIES, "baseline")}, decisions=[])
            (old / "signals_before_pnl.json").write_bytes(module._bytes(events))
            frozen = dict(scope="HISTORICAL_EXPLORATORY", start=start, end=end,
                          recent_start=start + 16 * STEP, baseline_strategy=asdict(entry_module.DEFAULT_CONFIG),
                          source_sha256=entry_module._source_bindings(), input_manifest_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                          protocol_sha256=hashlib.sha256((old / "protocol.json").read_bytes()).hexdigest())
            (old / "run_manifest.json").write_bytes(module._bytes(frozen))
            data = {s: [dict(open_time=t, close_time=t + STEP - 1, open=100., high=101., low=99.,
                             close=100., volume=100., atr=1.) for t in range(start, end, STEP)] for s in module.SYMBOLS}
            funding = {s: json.loads((root / f"{s}-funding.json").read_bytes()) for s in module.SYMBOLS}
            (old / "accounts").mkdir()
            for entry in module.ENTRIES:
                for scenario in old_protocol["scenarios"]:
                    parameters = {k: v for k, v in scenario.items() if k != "id"}
                    account = module.replay_portfolio(data, [], funding, **old_protocol["execution"], **parameters,
                                                       strict_funding=True, finalize=False)
                    (old / "accounts" / f"{entry}-{scenario['id']}.json").write_bytes(module._bytes(account))
            bindings = {str(p.relative_to(old)): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in [old / "signals_before_pnl.json", *(old / "accounts").glob("*.json")]}
            (old / "artifact_hashes.json").write_bytes(module._bytes(bindings))
            with patch.object(entry_module, "baseline_events", side_effect=AssertionError("must not regenerate signals")):
                result = module.run(old, source, root / "run", protocol_path=protocol_path)
            self.assertEqual(result["scope"], "HISTORICAL_EXPLORATORY")
            self.assertEqual(len(result["rows"]), 144)
            self.assertEqual(result["frozen_event_counts"], dict.fromkeys(module.ENTRIES, 0))
            self.assertFalse(result["account_verified"])
            self.assertFalse(result["statistically_verified"])
            self.assertFalse(result["promotion_eligible"])
            with self.assertRaisesRegex(ValueError, "empty"):
                module.run(old, source, root / "run", protocol_path=protocol_path)
            (old / "signals_before_pnl.json").write_bytes(module._bytes(dict(events={v: [] for v in module.ENTRIES})))
            with self.assertRaisesRegex(ValueError, "binding|SHA"):
                module.run(old, source, root / "bad-run", protocol_path=protocol_path)

    def test_real_shared_lifecycle_distinguishes_targets_breakeven_and_holding_limits(self):
        module = self.module()
        protocol = self.protocol(module)
        data = {s: [dict(open_time=i * STEP, close_time=(i + 1) * STEP - 1,
                         open=100., high=100.1, low=99.9, close=100., volume=100., atr=1.)
                    for i in range(704)] for s in module.SYMBOLS}
        data["BTCUSDT"][2].update(high=102.1)
        data["BTCUSDT"][3].update(open=100.3, high=100.4, low=99.7)
        data["BTCUSDT"][110].update(high=104.2)
        data["BTCUSDT"][120].update(high=106.5)
        funding = {s: dict(complete=True, start=0, end=704 * STEP, records=[]) for s in data}
        events = {entry: [dict(symbol="BTCUSDT", direction="long", setup_id=entry + "-long",
                               signal_time=2 * STEP, known_at=STEP, stop_loss=98.),
                         dict(symbol="BTCUSDT", direction="short", setup_id=entry + "-short",
                               signal_time=200 * STEP, known_at=199 * STEP, stop_loss=145.)]
                  for entry in module.ENTRIES}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference/accounts"
            reference.mkdir(parents=True)
            for entry in module.ENTRIES:
                for scenario in protocol["scenarios"]:
                    parameters = {k: v for k, v in scenario.items() if k != "id"}
                    account = module.replay_portfolio(data, events[entry], funding, **protocol["execution"], **parameters,
                                                       strict_funding=True, finalize=False)
                    (reference / f"{entry}-{scenario['id']}.json").write_bytes(module._bytes(account))
            result = module.run_matrix(data, events, funding, protocol, root / "run", root / "reference", 300 * STEP, 704 * STEP)
            rows = result["rows"]
            self.assertEqual(rows["D48-BE_then2-h96-base"]["exit_reason_counts"].get("protected_stop"), 1)
            self.assertEqual(rows["D48-target2-h96-base"]["exit_reason_counts"].get("timeout"), 2)
            self.assertEqual(rows["D48-target2-h288-base"]["exit_reason_counts"].get("take_profit"), 1)
            self.assertGreater(rows["D48-target2-h288-base"]["delta_vs_same_policy_24h"]["net_return"], 0.)
            self.assertEqual(rows["D48-target3-h96-base"]["rejection_reason_counts"], {"invalid_entry_open": 1})
            self.assertEqual(rows["D48-legacy1R-h96-base"]["average_holding_bars"], 48.5)
            for policy, hold, expected_exit in (("legacy1R", 96, 3 * STEP),
                                                 ("target2", 288, 111 * STEP),
                                                 ("target3", 288, 121 * STEP),
                                                 ("BE_then2", 96, 4 * STEP)):
                account = json.loads((root / "run/accounts" / f"D48-{policy}-h{hold}-base.json").read_bytes())
                self.assertEqual(account["trades"][0]["exit_time"], expected_exit)

    def test_flat_timeout_horizons_use_entry_bar_one_and_ignore_post_timeout_prices(self):
        module = self.module()
        protocol = self.protocol(module)
        data = {s: [dict(open_time=i * STEP, close_time=(i + 1) * STEP - 1,
                         open=100., high=100.1, low=99.9, close=100., volume=100., atr=1.)
                    for i in range(704)] for s in module.SYMBOLS}
        funding = {s: dict(complete=True, start=0, end=704 * STEP, records=[]) for s in data}
        events = {entry: [dict(symbol="BTCUSDT", direction="long", setup_id=entry,
                               signal_time=2 * STEP, known_at=STEP, stop_loss=98.)]
                  for entry in module.ENTRIES}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference/accounts"
            reference.mkdir(parents=True)
            for entry in module.ENTRIES:
                for scenario in protocol["scenarios"]:
                    parameters = {k: v for k, v in scenario.items() if k != "id"}
                    account = module.replay_portfolio(data, events[entry], funding, **protocol["execution"], **parameters,
                                                       strict_funding=True, finalize=False)
                    (reference / f"{entry}-{scenario['id']}.json").write_bytes(module._bytes(account))
            result = module.run_matrix(data, events, funding, protocol, root / "run", root / "reference", 300 * STEP, 704 * STEP)
            for policy in module.POLICIES:
                for hold, hours in ((96, 24), (288, 72), (672, 168)):
                    name = f"D48-{policy}-h{hold}-base"
                    summary = result["rows"][name]
                    self.assertEqual(summary["exit_reason_counts"], {"timeout": 1})
                    self.assertEqual(summary["average_hold_hours"], hours)
                    self.assertEqual(summary["average_holding_bars"], hold)
                    account = json.loads((root / "run/accounts" / f"{name}.json").read_bytes())
                    trade = account["trades"][0]
                    self.assertEqual(trade["exit_time"], (2 + hold) * STEP)
            for hold in (96, 288, 672):
                previous = json.loads((root / "run/accounts" / f"D48-target2-h{hold}-base.json").read_bytes())
                future_changed = copy.deepcopy(data)
                future_changed["BTCUSDT"][2 + hold].update(high=106., low=97.)
                parameters = {**protocol["execution"], "stop_mode": "structure_atr_target_only", "max_hold_bars": hold,
                              **{k: v for k, v in protocol["scenarios"][0].items() if k != "id"}}
                replayed = module.replay_portfolio(future_changed, events["D48"], funding, **parameters,
                                                    strict_funding=True, finalize=False)
                self.assertEqual(previous["trades"], replayed["trades"])


if __name__ == "__main__":
    unittest.main()
