import asyncio
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import app
import project_signal_backtest as backtest
from research_archive import ResearchArchive


def flat_bars():
    return [{"time": pd.Timestamp("2025-01-01", tz="UTC") + pd.Timedelta(minutes=15 * i),
             "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10}
            for i in range(4)]


class ExecutionMetadataTest(unittest.TestCase):
    def test_stop_mode_result_discloses_accounting_even_with_no_trades(self):
        for model, legacy, convention, observed in [
            ("legacy_ratio_v1", True, "legacy_inclusive_end", 97),
            ("linear_usdm_v1", False, "entry_bar_is_first", 96),
        ]:
            with self.subTest(model=model):
                result = backtest.build_stop_mode_result(
                    [], 2, 96, .001, "structure_atr", return_model=model, legacy_holding_limit=legacy,
                )
                metadata = result.get("execution_metadata", {})
                self.assertEqual(metadata.get("return_model"), model)
                self.assertEqual(metadata.get("holding_limit_convention"), convention)
                self.assertEqual(metadata.get("max_observed_bars"), observed)
                self.assertEqual(metadata.get("timestamp_boundary"), "bar_open_label_not_execution_time")
                self.assertFalse(metadata.get("funding_included", True))

    def test_persisted_web_research_retains_model_and_holding_convention(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = ResearchArchive(Path(directory))
            with patch.object(app, "RESEARCH_ARCHIVE", archive), \
                 patch.object(backtest, "fetch_exchange_klines", return_value=flat_bars()), \
                 patch.object(backtest, "fetch_higher_timeframe_context", side_effect=lambda symbol, bars, interval, **kwargs: bars):
                result = app.run_signal_backtest_summary("BTCUSDT", "15m", 4, 2, 96, .001, "structure_atr")
            restored = archive.get(result["run_id"])
            self.assertEqual(restored["params"].get("return_model"), "linear_usdm_v1")
            self.assertEqual(restored["params"].get("holding_limit_convention"), "entry_bar_is_first")
            self.assertEqual(restored["params"].get("max_observed_bars"), 96)
            self.assertEqual(restored["stop_mode_results"][0].get("execution_metadata"),
                             restored.get("execution_metadata"))
            self.assertEqual(archive.list()[0]["params"], restored["params"])

    def test_cli_writes_execution_manifest_and_human_readable_model_label(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "fixture.html"
            with patch("sys.argv", ["project_signal_backtest.py", "--output", str(output)]), \
                 patch.object(backtest, "fetch_exchange_klines", return_value=flat_bars()), \
                 patch.object(backtest, "fetch_higher_timeframe_context", side_effect=lambda symbol, bars, *args: bars), \
                 contextlib.redirect_stdout(io.StringIO()):
                backtest.main()
            manifest_path = output.with_name("fixture_manifest.json")
            self.assertTrue(manifest_path.exists(), "CLI execution metadata must accompany its report")
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest["execution_metadata"]["return_model"], "linear_usdm_v1")
            self.assertEqual(manifest["execution_metadata"]["holding_limit_convention"], "entry_bar_is_first")
            self.assertIn("linear_usdm_v1", output.read_text())

    def test_cli_can_explicitly_restore_legacy_holding_window(self):
        with patch("sys.argv", ["project_signal_backtest.py", "--return-model", "legacy_ratio_v1",
                                "--holding-limit-convention", "legacy_inclusive_end"]):
            try:
                args = backtest.parse_args()
            except SystemExit as exc:
                self.fail(f"Explicit legacy holding-window replay is unavailable: {exc}")
        self.assertEqual(args.holding_limit_convention, "legacy_inclusive_end")

    def test_mixed_paper_api_discloses_comparability_without_rewriting_records(self):
        records = [{"id": "old", "status": "closed", "outcome": "win", "return_pct": .1},
                   {"id": "new", "status": "closed", "outcome": "loss", "return_pct": -.02,
                    "return_model": "linear_usdm_v1"}]
        before = copy.deepcopy(records)
        application = app.create_app()
        endpoint = next(route.endpoint for route in application.routes if getattr(route, "path", None) == "/api/paper-trades")
        with patch.object(app.state, "strategy_trades", records), \
             patch.object(app, "paper_universe_snapshot", return_value={}):
            result = asyncio.run(endpoint())
        self.assertEqual(result.get("comparison_status"), "MIXED_RETURN_MODELS")
        self.assertIn("按收益版本", result.get("comparability_note", ""))
        self.assertEqual(result["stats"].get("comparability_note"), result["comparability_note"])
        self.assertIn("不同", result.get("retention_note", ""))
        self.assertEqual(records, before)
        self.assertEqual(result["stats"]["total_return"], .08)


if __name__ == "__main__":
    unittest.main()
