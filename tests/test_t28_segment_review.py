"""Synthetic saved-ledger attribution checks, without any strategy replay."""
from __future__ import annotations

import gzip
import importlib
import importlib.util
import json
import math
import tempfile
import unittest
from pathlib import Path

DAY = 86_400_000


class T28SegmentReviewTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("scripts.t28_segment_review"),
                             "segment postprocessor is not implemented")
        return importlib.import_module("scripts.t28_segment_review")

    def path(self, values):
        return [dict(time=i * DAY, equity=value,
                     symbol_net=dict(BTCUSDT=(value - 100.) * .4, ETHUSDT=(value - 100.) * .6),
                     fees=dict(BTCUSDT=i, ETHUSDT=2 * i), funding=dict(BTCUSDT=0., ETHUSDT=-i))
                for i, value in enumerate(values)]

    def test_phase_uses_only_pre_day_closes_and_exact_ten_percent_is_sideways(self):
        module = self.module()
        closes = {i * DAY: "100" for i in range(64)}
        closes.update({60 * DAY: "110", 61 * DAY: "111", 62 * DAY: "89"})
        result = module.build_phase_labels(closes, 60 * DAY, 63 * DAY)
        self.assertEqual([row["phase"] for row in result], ["sideways", "up", "down"])
        self.assertEqual(result[0]["available_close_end"], 60 * DAY)
        self.assertEqual(result[0]["lookback_close_end"], 0)
        closes[63 * DAY] = "1000000"
        self.assertEqual(result, module.build_phase_labels(closes, 60 * DAY, 63 * DAY))
        closes[60 * DAY] = "90"
        self.assertEqual(module.build_phase_labels(closes, 60 * DAY, 61 * DAY)[0]["phase"], "sideways")
        del closes[0]
        with self.assertRaisesRegex(ValueError, "close"):
            module.build_phase_labels(closes, 60 * DAY, 61 * DAY)

    def test_local_segment_drawdown_does_not_reset_wealth_or_hide_prior_highwater(self):
        module = self.module()
        path = self.path([100., 110., 99., 108.])
        result = module.segment_metrics(path, 2 * DAY, 3 * DAY)
        self.assertEqual(result["start_equity"], 99.)
        self.assertAlmostEqual(result["net_return"], 108 / 99 - 1)
        self.assertEqual(result["local_max_close_drawdown"], 0.)
        self.assertAlmostEqual(result["historical_highwater_max_drawdown_in_segment"], .1)
        self.assertAlmostEqual(result["symbol_net_pnl"]["BTCUSDT"], 3.6)
        self.assertAlmostEqual(result["symbol_net_pnl"]["ETHUSDT"], 5.4)
        self.assertEqual(result["fees_usdt"], 3.)
        self.assertEqual(result["funding_usdt"], -1.)
        loss = module.segment_metrics(path, DAY, 2 * DAY)
        self.assertIsNone(loss["ETH_net_profit_share"])

    def test_noncontiguous_phase_days_have_conditional_growth_but_separate_spells(self):
        module = self.module()
        own, hold = self.path([100., 110., 55., 60.5]), self.path([100., 105., 52.5, 55.125])
        labels = [dict(day_start=i * DAY, day_end=(i + 1) * DAY, phase=phase)
                  for i, phase in enumerate(("up", "down", "up"))]
        result = module.phase_metrics(labels, own, hold)
        up = result["phase_summary"]["up"]
        self.assertEqual(up["active_days"], 2)
        self.assertEqual(up["contiguous_episodes"], 2)
        self.assertAlmostEqual(up["T28_conditional_growth"], .21)
        self.assertAlmostEqual(up["hold_conditional_growth"], .1025)
        self.assertAlmostEqual(up["sum_paired_log_growth_difference"], 2 * math.log(1.1 / 1.05))
        self.assertEqual(up["worst_contiguous_episode_T28_drawdown"], 0.)
        self.assertNotIn("stitched_drawdown", up)
        self.assertEqual([row["phase"] for row in result["episodes"]], ["up", "down", "up"])
        self.assertAlmostEqual(result["episodes"][1]["T28"]["local_max_close_drawdown"], .5)
        self.assertAlmostEqual(result["daily_pairs"][2]["T28"]["net_return"], .1)

    def test_prepare_hashes_unreadable_accounts_without_reading_pnl_and_drift_blocks_execute(self):
        module = self.module()
        self.assertTrue(hasattr(module, "prepare"), "prepare stage is not implemented")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "BTC.json"
            raw.write_text(json.dumps([[i * 900_000, "100", "100", "100", "100", "1"]
                                       for i in range(62 * 96)]))
            input_manifest = root / "input.json"
            input_manifest.write_text(json.dumps(dict(symbols={symbol: dict(raw15m=dict(path=raw.name, sha256=module.sha(raw)))
                                                              for symbol in ("BTCUSDT", "ETHUSDT")})))
            accounts = root / "data" / "run" / "accounts"
            accounts.mkdir(parents=True)
            attrs = root / "data" / "audit" / "attribution"
            attrs.mkdir(parents=True)
            for strategy in ("T28_long_cash", "vol_scaled_hold"):
                (accounts / f"{strategy}__base.json.gz").write_bytes(b"not-gzip-do-not-read")
                path = []
                for i in range(1, 97):
                    gain = i / 96 * (10 if strategy == "T28_long_cash" else 5)
                    path.append(dict(time=61 * DAY + i * 900_000, equity=100 + gain,
                                     symbol_net=dict(BTCUSDT=gain * .4, ETHUSDT=gain * .6),
                                     fees=dict(BTCUSDT=0., ETHUSDT=0.), funding=dict(BTCUSDT=0., ETHUSDT=0.)))
                with gzip.open(attrs / f"{strategy}__base.json.gz", "wt") as handle:
                    json.dump(dict(path=path, max_equity_reconciliation_error=0.), handle)
            bindings = {str(raw): module.sha(raw), str(input_manifest): module.sha(input_manifest)}
            bindings.update({str(path): module.sha(path) for path in accounts.iterdir()})
            protocol = root / "protocol.json"
            protocol.write_text(json.dumps(dict(scope="DESCRIPTIVE_ALREADY_EXPOSED_FIXED_ACCOUNT_ATTRIBUTION",
                strategy_replays_allowed=False, strategies=["T28_long_cash", "vol_scaled_hold"], costs=["base"],
                phase_rule=dict(lookback_days=60, upper_return="0.10", lower_return="-0.10"), source_bindings=bindings,
                datasets=[dict(id="test", start="1970-03-03T00:00:00Z", end="1970-03-04T00:00:00Z",
                               input_manifest=str(input_manifest), account_directory=str(accounts), cost_mapping=dict(base="base"))])))
            result = module.prepare(protocol, root / "prepared")
            self.assertEqual(result["status"], "LABELS_FROZEN_BEFORE_GROUPED_PNL")
            frozen = module.read(root / "prepared" / "phase_labels.json")
            self.assertEqual(frozen["test"][0]["phase"], "sideways")
            self.assertFalse((root / "prepared" / "summary.json").exists())
            replay = module.execute(root / "prepared", root / "synthetic_run")
            self.assertFalse(replay["strategy_replayed"])
            case = replay["cases"][0]
            self.assertAlmostEqual(case["full"]["T28"]["net_return"], .1)
            self.assertAlmostEqual(case["full"]["paired_relative_return"], 1.1 / 1.05 - 1)
            self.assertIsNone(case["phase_summary"]["down"]["T28_conditional_growth"])
            self.assertEqual(case["phase_summary"]["down"]["active_days"], 0)
            self.assertTrue(case["year_pairs"][0]["partial_year"])
            raw.write_text("changed")
            with self.assertRaisesRegex(ValueError, "hash|drift"):
                module.execute(root / "prepared", root / "run")
            self.assertFalse((root / "run").exists())


if __name__ == "__main__":
    unittest.main()
