from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

import app
import config


class AuditRemediationTest(unittest.TestCase):
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
        self.assertIn("--place-order", services["okx-strategy-bot"]["command"])


if __name__ == "__main__":
    unittest.main()
