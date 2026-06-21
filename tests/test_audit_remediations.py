from __future__ import annotations

import json
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

import app
import config
import okx_demo_bot


class AuditRemediationTest(unittest.TestCase):
    def test_legacy_okx_bot_is_signal_only_by_default(self) -> None:
        with patch.object(sys, "argv", ["okx_demo_bot.py"]):
            args = okx_demo_bot.parse_args()
        self.assertFalse(args.place_order)

    def test_load_config_uses_real_yaml_features(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.yaml"
            path.write_text(
                """
app:
  default_signal_interval: 15m
  enabled_intervals:
    - 15m
    - 1h
strategy:
  thresholds:
    buy: 40
    sell: 60
""".strip(),
                encoding="utf-8",
            )

            loaded = config.load_config(path)

        self.assertEqual(loaded["app"]["enabled_intervals"], ["15m", "1h"])
        self.assertEqual(loaded["strategy"]["thresholds"]["buy"], 40)

    def test_save_watchlist_writes_valid_json_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "runtime" / "watchlist.json"
            with patch.object(app, "STATE_FILE", state_file):
                app.save_watchlist([{"symbol": "btcusdt", "signal": True, "interval": "15m"}])

            payload = json.loads(state_file.read_text(encoding="utf-8"))

        self.assertEqual(payload[0]["symbol"], "BTCUSDT")
        self.assertFalse(list(state_file.parent.glob("*.tmp")))

    def test_create_app_creates_reports_directory_before_static_mount(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            reports_dir = Path(tmpdir) / "not-created-yet" / "reports"
            with patch.object(app, "REPORTS_DIR", reports_dir):
                created = app.create_app()
            self.assertEqual(created.title, "Chanlun Crypto Monitor")
            self.assertTrue(reports_dir.is_dir())

    def test_monitor_task_closes_its_event_loop_http_session(self) -> None:
        async def run() -> None:
            await app.run_monitor_loop(asyncio.Event())

        with patch.object(app, "monitor_loop_async", new=AsyncMock()), patch.object(
            app, "close_http_session", new=AsyncMock()
        ) as close_session:
            asyncio.run(run())
        close_session.assert_awaited_once()

    def test_get_td_signals_returns_neutral_for_empty_klines(self) -> None:
        with patch.object(app, "fetch_klines", return_value=[]):
            signals = app.get_td_signals("BTCUSDT", 1234567890)

        self.assertEqual(signals, {"1m": 0, "3m": 0, "5m": 0, "15m": 0, "30m": 0})

    def test_nearest_index_rejects_empty_bars(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            app.nearest_index_by_close_time([], 1234567890)

    def test_okx_order_bot_requires_explicit_trading_profile(self) -> None:
        compose_path = Path(__file__).resolve().parents[1] / "docker-compose.yml"
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

        services = compose["services"]
        self.assertNotIn("profiles", services["crypto-project"])
        self.assertEqual(services["okx-strategy-bot"]["profiles"], ["trading"])
        self.assertNotIn("--place-order", services["okx-strategy-bot"]["command"])
        self.assertIn("healthcheck", services["crypto-project"])
        self.assertIn("mem_limit", services["crypto-project"])

        order_override = yaml.safe_load((compose_path.parent / "docker-compose.order.yml").read_text(encoding="utf-8"))
        self.assertIn("--place-order", order_override["services"]["okx-strategy-bot"]["command"])

    def test_frequency_tuning_preserves_portfolio_risk_guards(self) -> None:
        project_config = config.load_config()
        bot = project_config["bot"]
        strategy = project_config["strategy"]

        self.assertEqual(bot["universe_top_n"], 200)
        self.assertEqual(bot["min_quote_volume"], 5_000_000)
        self.assertEqual(bot["kline_limit"], 300)
        self.assertEqual(bot["max_open_positions"], 5)
        self.assertEqual(bot["risk_per_trade_pct"], 0.005)
        self.assertEqual(bot["max_position_notional_usdt"], 50)
        self.assertEqual(strategy["min_divergence_strength"], 0.20)
        self.assertEqual(strategy["max_extreme_lag_bars"], 4)
        self.assertTrue(strategy["require_higher_trend_alignment"])


if __name__ == "__main__":
    unittest.main()
