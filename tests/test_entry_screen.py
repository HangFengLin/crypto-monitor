"""Synthetic adapter checks; fixtures are never historical market evidence."""
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


class EntryScreenTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("scripts.entry_screen"),
                             "entry screen runner is not implemented")
        return importlib.import_module("scripts.entry_screen")

    def test_trade_diagnostics_keep_cash_profit_factor_and_return_payoff_distinct(self):
        result = self.module().trade_diagnostics([
            dict(net_return=.01, net_pnl=100., net_r=1.),
            dict(net_return=-.02, net_pnl=-50., net_r=-1.),
            dict(net_return=1e-13, net_pnl=0., net_r=0.),
        ])
        self.assertEqual(result["closed_trades"], 3)
        self.assertAlmostEqual(result["net_win_rate"], 1 / 3)
        self.assertAlmostEqual(result["payoff_ratio"], .5)
        self.assertAlmostEqual(result["profit_factor"], 2.)
        self.assertEqual(result["mean_net_r"], 0.)
        self.assertEqual(result["average_loss_return"], -.02)
        self.assertIsNone(self.module().trade_diagnostics([])["net_win_rate"])
        self.assertIsNone(self.module().trade_diagnostics([
            dict(net_return=.01, net_pnl=1., net_r=1.)])["profit_factor"])

    def test_recent_segment_uses_continuous_boundary_equity_and_phase_aware_costs(self):
        account = dict(parameters=dict(initial_equity=100.),
                       equity=[dict(time=STEP, equity=100., cash=100.),
                               dict(time=2 * STEP, equity=120., cash=120.),
                               dict(time=3 * STEP, equity=90., cash=100.)],
                       fills=[dict(time=2 * STEP, phase="bar_end", fee=5., gross_pnl=25.),
                              dict(time=2 * STEP, phase="open", fee=2., gross_pnl=0.),
                              dict(time=3 * STEP, phase="bar_end", fee=3., gross_pnl=-15.)],
                       trades=[])
        result = self.module().recent_segment(account, 2 * STEP)
        self.assertEqual(result["initial_marked_equity"], 120.)
        self.assertAlmostEqual(result["net_return"], -.25)
        self.assertAlmostEqual(result["max_close_drawdown"], -.25)
        self.assertEqual(result["fees_usdt"], 5.)
        self.assertEqual(result["funding_cashflow_usdt"], 0.)
        self.assertFalse(result["capital_reset"])
        with self.assertRaisesRegex(ValueError, "boundary"):
            self.module().recent_segment(account, STEP + 1)

    def test_baseline_is_recomputed_in_both_directions_and_deduplicated(self):
        module = self.module()
        prepared = {s: [dict(open_time=i * STEP, close_time=(i + 1) * STEP - 1)
                        for i in range(104)] for s in module.SYMBOLS}

        class Engine:
            def detect(self, rows, clean=False):
                count = len(rows)
                if count in (100, 101):
                    return dict(signal="long", divergence_time=50, stop_loss=98.)
                if count in (102, 103):
                    return dict(signal="short", divergence_time=50, stop_loss=102.)
                return dict(signal="wait")

        with patch.object(module, "ProjectSignalEngine", Engine):
            events, decisions = module.baseline_events(prepared, 99 * STEP, 104 * STEP)
        self.assertEqual(len(events), 4)
        for symbol in module.SYMBOLS:
            self.assertEqual([event["direction"] for event in events if event["symbol"] == symbol],
                             ["long", "short"])
        self.assertEqual(events[0]["known_at"], 100 * STEP)
        self.assertEqual(events[0]["signal_time"], 101 * STEP)
        self.assertEqual(len(decisions), 10)

    def test_continuation_screen_shows_all_candidates_without_a_promotion_claim(self):
        module = self.module()
        summaries = {}
        for variant in module.CANDIDATES:
            for scenario in ("base", "double_cost", "delay15m"):
                summaries[f"{variant}-{scenario}"] = dict(
                    net_return=.1, closed_trades=2,
                    recent=dict(net_return=.02),
                    per_symbol_marked_pnl_usdt=dict(BTCUSDT=50., ETHUSDT=50.),
                    trade_diagnostics=dict(profit_factor=1.5))
        summaries["B20-double_cost"]["recent"]["net_return"] = -.01
        summaries["S24-base"]["trade_diagnostics"]["profit_factor"] = None
        screens = module.continuation_screen(summaries)
        self.assertEqual(set(screens), set(module.CANDIDATES))
        self.assertEqual(screens["D48"]["status"], "WORTH_FURTHER_VALIDATION")
        self.assertEqual(screens["B20"]["status"], "NOT_MET")
        self.assertEqual(screens["S24"]["status"], "INSUFFICIENT_EVIDENCE")
        self.assertFalse(screens["D48"]["promotion_eligible"])
        self.assertFalse(screens["D48"]["statistically_verified"])

    def test_serial_replay_saves_each_account_before_starting_the_next(self):
        module = self.module()
        data = {s: [dict(open_time=i * STEP, close_time=(i + 1) * STEP - 1,
                         open=100., high=100.1, low=99.9, close=100., volume=100.)
                    for i in range(8)] for s in module.SYMBOLS}
        data["BTCUSDT"][2].update(high=106., low=97.)
        funding = {s: dict(complete=True, start=0, end=8 * STEP,
                           records=[dict(time=2 * STEP + 100, rate=.001, mark_price=100.)]) for s in data}
        events = {v: [dict(symbol="BTCUSDT", direction="long", setup_id=v,
                          signal_time=2 * STEP, known_at=STEP, stop_loss=98.)]
                  for v in (*module.CANDIDATES, "baseline")}
        protocol = module.load_protocol()
        original = module.replay_portfolio
        started = []
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)

            def replay(*args, **kwargs):
                self.assertEqual(len(list((output / "accounts").glob("*.json"))), len(started))
                started.append(1)
                return original(*args, **kwargs)

            with patch.object(module, "replay_portfolio", side_effect=replay):
                summaries = module.run_accounts_serial(data, events, funding, protocol, output, 3 * STEP)
            self.assertEqual(len(started), 15)
            self.assertEqual(len(summaries), 15)
            self.assertTrue(all("equity" not in row for row in summaries.values()))
            account = json.loads((output / "accounts/D48-base.json").read_text())
            self.assertEqual(account["trades"][0]["exit_reason"], "stop_loss")
            self.assertLess(summaries["D48-base"]["funding_cashflow_usdt"], 0)

    def test_bound_fixture_freezes_signals_before_any_pnl_and_writes_fifteen_accounts(self):
        module = self.module()
        start, end = 400 * HIGHER, 400 * HIGHER + 32 * STEP
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protocol = copy.deepcopy(module.load_protocol())
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
                funding = dict(symbol=symbol, exchange="binance", market="linear_perpetual",
                               source="Binance /fapi/v1/fundingRate", complete=True, gaps=[], start=start, end=end,
                               records=[dict(time=start, rate=0., mark_price=100.)],
                               raw_records=[dict(symbol=symbol, fundingTime=start, fundingRate="0", markPrice="100")])
                path = root / f"{symbol}-funding.json"
                path.write_text(json.dumps(funding))
                manifest["symbols"][symbol]["funding"] = dict(path=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            source = root / "input_manifest.json"
            source.write_text(json.dumps(manifest))
            output = root / "output"
            original = module.replay_portfolio

            def replay(*args, **kwargs):
                self.assertTrue((output / "signals_before_pnl.json").is_file())
                return original(*args, **kwargs)

            with patch.object(module, "replay_portfolio", side_effect=replay):
                result = module.run(source, output, protocol_path=protocol_path)
            self.assertEqual(result["scope"], "HISTORICAL_EXPLORATORY")
            self.assertEqual(len(result["summaries"]), 15)
            self.assertEqual(len(list((output / "accounts").glob("*.json"))), 15)
            self.assertFalse(result["account_verified"])
            self.assertFalse(result["statistically_verified"])
            self.assertFalse(result["promotion_eligible"])
            self.assertTrue(all(row["status"] != "WORTH_FURTHER_VALIDATION"
                                for row in result["continuation_screen"].values()))
            frozen = json.loads((output / "run_manifest.json").read_text())
            self.assertEqual(frozen["protocol_sha256"], hashlib.sha256(protocol_path.read_bytes()).hexdigest())
            self.assertIn("baseline_strategy", frozen)
            with self.assertRaisesRegex(ValueError, "empty"):
                module.run(source, output, protocol_path=protocol_path)
            bad = json.loads(source.read_text())
            bad["symbols"]["BTCUSDT"]["raw15m"]["sha256"] = "0" * 64
            source.write_text(json.dumps(bad))
            with self.assertRaisesRegex(ValueError, "SHA"):
                module.run(source, root / "bad-output", protocol_path=protocol_path)
            source.write_text(json.dumps(manifest))
            touched = False

            def change_input_during_replay(*args, **kwargs):
                nonlocal touched
                if not touched:
                    changed = root / manifest["symbols"]["BTCUSDT"]["raw15m"]["path"]
                    changed.write_bytes(changed.read_bytes() + b"\n")
                    touched = True
                return original(*args, **kwargs)

            with patch.object(module, "replay_portfolio", side_effect=change_input_during_replay):
                with self.assertRaisesRegex(ValueError, "SHA"):
                    module.run(source, root / "input-drift", protocol_path=protocol_path)
            self.assertFalse((root / "input-drift/summary.json").exists())


if __name__ == "__main__":
    unittest.main()
