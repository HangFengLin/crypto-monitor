"""Synthetic cross-period adapter checks; no market PnL is run by these tests."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

STEP = 900_000


class T28CrossPeriodTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("scripts.t28_cross_period"),
                             "cross-period adapter is not implemented")
        return importlib.import_module("scripts.t28_cross_period")

    def fixture(self, module, root):
        protocol = json.loads(module.PROTOCOL.read_bytes())
        start, end, warmup = (module.utc_ms(protocol[key]) for key in ("start", "end", "warmup_start"))
        manifest = dict(schema_version=1, exposure_label=module.EXPOSURE, data_authority="TEST", symbols={})
        for symbol in protocol["symbols"]:
            manifest["symbols"][symbol] = {}
            raw = [[t, "100", "101", "99", "100", "100", t + STEP - 1]
                   for t in range(warmup, end, STEP)]
            funding = dict(symbol=symbol, exchange="binance", market="linear_perpetual",
                           source="Binance /fapi/v1/fundingRate", complete=True, gaps=[], start=start, end=end,
                           records=[dict(time=t + 3, rate=.0001, mark_price=100.) for t in range(start, end, 28_800_000)],
                           raw_records=[dict(symbol=symbol, fundingTime=t + 3, fundingRate="0.0001", markPrice="100")
                                        for t in range(start, end, 28_800_000)])
            for key, payload in (("raw15m", raw), ("funding", funding)):
                path = root / f"{symbol}-{key}.json"
                path.write_text(json.dumps(payload))
                manifest["symbols"][symbol][key] = dict(path=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        source = root / "inputs.json"
        source.write_text(json.dumps(manifest))
        return source, start, end

    def test_prepare_has_no_pnl_and_execute_preserves_six_accounts_and_exposure_label(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, start, end = self.fixture(module, root)
            with patch.object(module, "replay_target_weights", side_effect=AssertionError("PnL during prepare")):
                frozen = module.prepare(source, module.PROTOCOL, root / "prepared")
            self.assertEqual(frozen["status"], "TARGETS_AND_SOURCES_FROZEN_NO_PNL")
            self.assertFalse(list((root / "prepared").glob("accounts/*")))
            targets = json.loads((root / "prepared/targets_before_pnl.json").read_bytes())
            self.assertEqual(set(targets), {"T28_long_cash", "vol_scaled_hold"})
            self.assertEqual(targets["T28_long_cash"][0]["decision_time_ms"], start)
            result = module.execute(root / "prepared", root / "run")
            self.assertEqual(result["scope"], module.EXPOSURE)
            self.assertEqual(len(list((root / "run/accounts").glob("*.json.gz"))), 6)
            self.assertFalse(result["statistically_verified"])
            self.assertFalse(result["promotion_eligible"])
            row = result["summaries"]["T28_long_cash"]["base"]
            self.assertEqual(row["calendar"]["segments"][0]["start_ms"], start)
            self.assertEqual(row["calendar"]["segments"][0]["end_ms"], end)
            self.assertEqual(row["complete_weeks"], 16)
            self.assertIn("episode_diagnostics", row)
            self.assertIn("cash_profit_factor", row)
            self.assertEqual(result["cash_benchmark"]["net_return"], 0.)
            with self.assertRaisesRegex((ValueError, FileExistsError), "new|empty"):
                module.execute(root / "prepared", root / "run")

    def test_input_or_target_drift_blocks_execution_before_account_replay(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _start, _end = self.fixture(module, root)
            module.prepare(source, module.PROTOCOL, root / "prepared")
            path = root / "prepared/targets_before_pnl.json"
            path.write_bytes(path.read_bytes() + b"\n")
            with patch.object(module, "replay_target_weights", side_effect=AssertionError("must reject first")):
                with self.assertRaisesRegex(ValueError, "drift|SHA"):
                    module.execute(root / "prepared", root / "run")
            self.assertFalse((root / "run").exists())

    def test_original_four_economic_conditions_remain_separate_from_confirmation(self):
        module = self.module()
        rows = {"T28_long_cash": {"base": dict(net_return=.1, max_close_drawdown=.03),
                                  "double_cost": dict(net_return=.05, max_close_drawdown=.04)},
                "vol_scaled_hold": {"base": dict(net_return=.08, max_close_drawdown=.05)}}
        result = module.economic_screen(rows)
        self.assertEqual(result["status"], "CROSS_PERIOD_DIAGNOSTIC_SUPPORT_ONLY")
        self.assertFalse(result["strategy_confirmed"])
        rows["T28_long_cash"]["double_cost"]["net_return"] = -.01
        self.assertEqual(module.economic_screen(rows)["status"], "ECONOMIC_SCREEN_NOT_REPLICATED")


if __name__ == "__main__":
    unittest.main()
