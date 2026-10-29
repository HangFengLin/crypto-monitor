"""Offline adapter tests; synthetic prices are never new market evidence."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

STEP, HIGHER = 900_000, 14_400_000


class HistoricalAdapterTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("scripts.trend_pullback_backtest"))
        return importlib.import_module("scripts.trend_pullback_backtest")

    def test_context_only_uses_closed_four_hour_bars(self):
        module = self.module()
        start, end = 400 * HIGHER, 400 * HIGHER + 20 * STEP
        def rows(step, beginning, ending):
            return [[t, "100", "101", "99", str(100 + t / HIGHER * .001), "100", t + step - 1]
                    for t in range(beginning, ending, step)]
        lower = rows(STEP, start - 300 * STEP, end)
        higher = rows(HIGHER, start - 300 * HIGHER, end // HIGHER * HIGHER)
        prepared = module.prepare_inputs(lower, higher, start, end)
        for row in prepared:
            if row["open_time"] >= start:
                self.assertLessEqual(row["higher_close_time"], row["close_time"])
                self.assertEqual(row["higher_close_time"], ((row["close_time"] + 1) // HIGHER) * HIGHER - 1)

    def test_events_use_one_full_bar_after_the_decision_boundary(self):
        module = self.module()
        event = module.historical_event("BTCUSDT", "TPR1", "setup", 99 * STEP - 1, 98.)
        self.assertEqual(event["known_at"], 99 * STEP)
        self.assertEqual(event["signal_time"], 100 * STEP)

    def test_six_accounts_preserve_stop_first_funding_and_cash_equity_identity(self):
        module = self.module()
        bars = [dict(open_time=i * STEP, close_time=(i + 1) * STEP - 1,
                     open=100., high=100.1, low=99.9, close=100., volume=100., is_closed=True)
                for i in range(8)]
        bars[2].update(high=106., low=97.)
        data = {s: bars for s in ("BTCUSDT", "ETHUSDT")}
        funding = {s: dict(complete=True, start=0, end=8 * STEP,
                           records=[dict(time=2 * STEP + 100, rate=.001, mark_price=100.)]) for s in data}
        events = {v: [dict(symbol="BTCUSDT", direction="long", setup_id=v,
                          signal_time=2 * STEP, known_at=STEP, stop_loss=98.)] for v in ("baseline", "TPR1")}
        protocol = module.load_protocol()
        accounts, summaries = module.calculate_accounts(data, events, funding, protocol)
        self.assertEqual(len(accounts), 6)
        self.assertEqual(accounts["TPR1-base"]["trades"][0]["exit_reason"], "stop_loss")
        self.assertLess(summaries["TPR1-base"]["funding_cashflow_usdt"], 0)
        for name, summary in summaries.items():
            self.assertAlmostEqual(summary["cash_equity_reconciliation_error"], 0., places=8)
            self.assertAlmostEqual(summary["net_return"], accounts[name]["equity"][-1]["equity"] / 10_000 - 1)

    def test_sha_bound_fixture_runs_end_to_end_without_a_forward_oos_claim(self):
        module = self.module()
        start, end = 400 * HIGHER, 400 * HIGHER + 20 * STEP
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = dict(schema_version=1, symbols={})
            for symbol in ("BTCUSDT", "ETHUSDT"):
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
            result = module.run(source, root / "output", start, end)
            self.assertEqual(result["scope"], "HISTORICAL_DIAGNOSTIC")
            self.assertEqual(len(list((root / "output/accounts").glob("*.json"))), 6)
            self.assertFalse(result["account_verified"])
            self.assertFalse(result["statistically_verified"])
            with self.assertRaisesRegex(ValueError, "empty"):
                module.run(source, root / "output", start, end)


if __name__ == "__main__":
    unittest.main()
