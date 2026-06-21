from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import okx_market_cap_bot as bot


class PositionSizingTest(unittest.TestCase):
    def test_startup_reconciliation_accepts_matching_positions(self) -> None:
        args = argparse.Namespace(place_order=True, okx_instrument_type="SWAP")
        state = {
            "positions": [
                {"symbol": "BTCUSDT", "inst_id": "BTC-USDT-SWAP", "direction": "long", "size": "2", "status": "open"}
            ]
        }
        exchange = [{"instId": "BTC-USDT-SWAP", "posSide": "long", "pos": "2"}]

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(bot, "EVENT_LOG_FILE", Path(tmpdir) / "events.jsonl"), patch.object(
            bot, "fetch_okx_demo_positions", return_value=exchange
        ):
            bot.reconcile_startup_positions(args, state)

    def test_startup_reconciliation_blocks_untracked_exchange_position(self) -> None:
        args = argparse.Namespace(place_order=True, okx_instrument_type="SWAP")
        exchange = [{"instId": "ETH-USDT-SWAP", "posSide": "short", "pos": "1"}]

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(bot, "EVENT_LOG_FILE", Path(tmpdir) / "events.jsonl"), patch.object(
            bot, "fetch_okx_demo_positions", return_value=exchange
        ), patch.object(bot, "send_discord"):
            with self.assertRaisesRegex(RuntimeError, "startup reconciliation failed"):
                bot.reconcile_startup_positions(args, {"positions": []})

    def test_extract_okx_equity_prefers_total_equity(self) -> None:
        balance = {"totalEq": "123.45", "details": [{"ccy": "USDT", "availEq": "99"}]}

        self.assertEqual(bot.extract_okx_equity_usdt(balance), Decimal("123.45"))

    def test_extract_okx_equity_falls_back_to_usdt_detail(self) -> None:
        balance = {"details": [{"ccy": "BTC", "eqUsd": "10"}, {"ccy": "USDT", "availEq": "88.5"}]}

        self.assertEqual(bot.extract_okx_equity_usdt(balance), Decimal("88.5"))

    def test_risk_position_size_caps_notional_and_floors_to_lot(self) -> None:
        size, sizing = bot.calculate_risk_position_size(
            equity=Decimal("1000"),
            risk_per_trade_pct=Decimal("0.005"),
            max_position_notional_usdt=Decimal("50"),
            entry_price=Decimal("5.58"),
            stop_loss=Decimal("5.473090670800161"),
            instrument={"ctVal": "0.1", "ctValCcy": "INJ", "lotSz": "1", "minSz": "1"},
            instrument_type="SWAP",
        )

        self.assertEqual(size, Decimal("89"))
        self.assertEqual(sizing["mode"], "risk")
        self.assertEqual(sizing["notional_usdt"], "49.662")

    def test_risk_position_size_uses_risk_budget_when_smaller_than_cap(self) -> None:
        size, sizing = bot.calculate_risk_position_size(
            equity=Decimal("1000"),
            risk_per_trade_pct=Decimal("0.005"),
            max_position_notional_usdt=Decimal("500"),
            entry_price=Decimal("0.28608"),
            stop_loss=Decimal("0.363249560994888"),
            instrument={"ctVal": "100", "ctValCcy": "H", "lotSz": "0.1", "minSz": "0.1"},
            instrument_type="SWAP",
        )

        self.assertEqual(size, Decimal("0.6"))
        self.assertEqual(sizing["risk_budget_usdt"], "5")

    def test_risk_position_size_rejects_orders_below_minimum(self) -> None:
        with self.assertRaisesRegex(ValueError, "below OKX minimum"):
            bot.calculate_risk_position_size(
                equity=Decimal("10"),
                risk_per_trade_pct=Decimal("0.001"),
                max_position_notional_usdt=Decimal("0"),
                entry_price=Decimal("100"),
                stop_loss=Decimal("99"),
                instrument={"ctVal": "1", "lotSz": "1", "minSz": "1"},
                instrument_type="SWAP",
            )

    def test_position_size_for_signal_keeps_fixed_mode(self) -> None:
        args = argparse.Namespace(position_sizing="fixed", size="3")

        size, sizing = bot.position_size_for_signal(args, "BTC-USDT-SWAP", {"price": "1", "stop_loss": "0.9"})

        self.assertEqual(size, "3")
        self.assertEqual(sizing, {"mode": "fixed"})

    def test_position_size_for_signal_uses_configured_equity_without_balance_call(self) -> None:
        args = argparse.Namespace(
            position_sizing="risk",
            size="1",
            okx_instrument_type="SWAP",
            risk_per_trade_pct=0.005,
            max_position_notional_usdt=50,
            sizing_equity_usdt=1000,
        )
        signal = {"price": "2.316", "stop_loss": "2.2774037762538573"}

        with patch.object(bot, "fetch_okx_instrument", return_value={"ctVal": "10", "lotSz": "0.1", "minSz": "0.1"}), patch.object(
            bot, "fetch_okx_demo_balance"
        ) as fetch_balance:
            size, sizing = bot.position_size_for_signal(args, "NEAR-USDT-SWAP", signal)

        fetch_balance.assert_not_called()
        self.assertEqual(size, "2.1")
        self.assertEqual(sizing["mode"], "risk")

    def test_position_size_for_signal_uses_paper_equity_without_place_order(self) -> None:
        args = argparse.Namespace(
            position_sizing="risk",
            size="1",
            okx_instrument_type="SWAP",
            risk_per_trade_pct=0.005,
            max_position_notional_usdt=50,
            sizing_equity_usdt=0,
            place_order=False,
        )
        signal = {"price": "2.316", "stop_loss": "2.2774037762538573"}

        with patch.object(bot, "fetch_okx_instrument", return_value={"ctVal": "10", "lotSz": "0.1", "minSz": "0.1"}), patch.object(
            bot, "fetch_okx_demo_balance"
        ) as fetch_balance:
            size, sizing = bot.position_size_for_signal(args, "NEAR-USDT-SWAP", signal)

        fetch_balance.assert_not_called()
        self.assertEqual(size, "2.1")
        self.assertEqual(sizing["equity_usdt"], "1000")

    def test_load_state_migrates_legacy_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            legacy_state = base / "okx_market_cap_bot_state.json"
            new_state = base / "runtime" / "okx_market_cap_bot_state.json"
            event_log = base / "runtime" / "okx_market_cap_bot_events.jsonl"
            legacy_state.write_text(json.dumps({"positions": [{"symbol": "HUSDT", "status": "open"}]}), encoding="utf-8")

            with patch.object(bot, "LEGACY_STATE_FILE", legacy_state), patch.object(bot, "STATE_FILE", new_state), patch.object(
                bot, "EVENT_LOG_FILE", event_log
            ):
                state = bot.load_state()

            self.assertEqual(state["positions"][0]["symbol"], "HUSDT")
            self.assertTrue(new_state.exists())
            self.assertIn("state_migrated", event_log.read_text(encoding="utf-8"))

    def test_configured_order_symbol_blocklist_normalizes_symbols(self) -> None:
        with patch.object(bot, "CONFIG", {"bot": {"okx_order_symbol_blocklist": ["tao-usdt-swap", " BTC-USDT-SWAP "]}}), patch.dict(
            os.environ, {}, clear=False
        ):
            os.environ.pop("OKX_ORDER_SYMBOL_BLOCKLIST", None)
            self.assertEqual(bot.configured_order_symbol_blocklist(), {"TAO-USDT-SWAP", "BTC-USDT-SWAP"})

    def test_configured_order_symbol_blocklist_prefers_non_empty_env(self) -> None:
        with patch.object(bot, "CONFIG", {"bot": {"okx_order_symbol_blocklist": ["BTC-USDT-SWAP"]}}), patch.dict(
            os.environ, {"OKX_ORDER_SYMBOL_BLOCKLIST": "tao-usdt-swap, eth-usdt-swap"}
        ):
            self.assertEqual(bot.configured_order_symbol_blocklist(), {"TAO-USDT-SWAP", "ETH-USDT-SWAP"})

    def test_scan_once_skips_blocklisted_order_symbol_in_place_order_mode(self) -> None:
        args = argparse.Namespace(
            top_n=100,
            quote_asset="USDT",
            min_quote_volume=10_000_000,
            okx_instrument_type="SWAP",
            place_order=True,
            interval="15m",
            limit=1000,
            max_open_positions=5,
        )
        state = {"positions": []}

        with tempfile.TemporaryDirectory() as tmpdir:
            event_log = Path(tmpdir) / "events.jsonl"
            state_file = Path(tmpdir) / "state.json"
            with patch.object(bot, "EVENT_LOG_FILE", event_log), patch.object(bot, "STATE_FILE", state_file), patch.object(
                bot,
                "build_okx_market_cap_universe",
                return_value=[{"symbol": "TAOUSDT", "inst_id": "TAO-USDT-SWAP"}],
            ), patch.object(bot, "configured_order_symbol_blocklist", return_value={"TAO-USDT-SWAP"}), patch.object(
                bot, "latest_signal"
            ) as latest_signal:
                bot.scan_once(args, state)
                scan_event = json.loads(event_log.read_text(encoding="utf-8").splitlines()[-1])

        latest_signal.assert_not_called()
        self.assertEqual(scan_event["skipped"], {"order_symbol_blocklisted": 1})

    def test_scan_once_records_exclusive_signal_reason_and_state(self) -> None:
        args = argparse.Namespace(
            top_n=200,
            quote_asset="USDT",
            min_quote_volume=10_000_000,
            okx_instrument_type="SWAP",
            place_order=True,
            interval="15m",
            limit=1000,
            max_open_positions=5,
            min_signal_score=0,
            min_structure_score=0,
            debug_signals=False,
        )
        state = {"positions": []}
        signal = {
            "signal": "filtered_buy",
            "signal_name": "底背驰被共振/量价/震荡过滤",
            "filter": "✗ 4h未多头共振",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            event_log = Path(tmpdir) / "events.jsonl"
            state_file = Path(tmpdir) / "state.json"
            with patch.object(bot, "EVENT_LOG_FILE", event_log), patch.object(bot, "STATE_FILE", state_file), patch.object(
                bot, "build_okx_market_cap_universe", return_value=[{"symbol": "BTCUSDT", "inst_id": "BTC-USDT-SWAP"}]
            ), patch.object(bot, "configured_order_symbol_blocklist", return_value=set()), patch.object(
                bot, "configured_scan_symbol_blocklist", return_value=set()
            ), patch.object(bot, "latest_signal", return_value=(signal, [])):
                bot.scan_once(args, state)
                scan_event = json.loads(event_log.read_text(encoding="utf-8").splitlines()[-1])

        self.assertEqual(scan_event["signal_reasons"], {"higher_timeframe_misaligned": 1})
        self.assertEqual(scan_event["signal_states"], {"底背驰被共振/量价/震荡过滤": 1})

    def test_scan_once_skips_market_data_blocklist_before_signal_fetch(self) -> None:
        args = argparse.Namespace(
            top_n=200,
            quote_asset="USDT",
            min_quote_volume=10_000_000,
            okx_instrument_type="SWAP",
            place_order=True,
            interval="15m",
            limit=1000,
            max_open_positions=5,
        )
        state = {"positions": []}

        with tempfile.TemporaryDirectory() as tmpdir:
            event_log = Path(tmpdir) / "events.jsonl"
            state_file = Path(tmpdir) / "state.json"
            with patch.object(bot, "EVENT_LOG_FILE", event_log), patch.object(bot, "STATE_FILE", state_file), patch.object(
                bot, "build_okx_market_cap_universe", return_value=[{"symbol": "GRAMUSDT", "inst_id": "GRAM-USDT-SWAP"}]
            ), patch.object(bot, "configured_order_symbol_blocklist", return_value=set()), patch.object(
                bot, "configured_scan_symbol_blocklist", return_value={"GRAMUSDT"}
            ), patch.object(bot, "latest_signal") as latest_signal:
                bot.scan_once(args, state)
                scan_event = json.loads(event_log.read_text(encoding="utf-8").splitlines()[-1])

        latest_signal.assert_not_called()
        self.assertEqual(scan_event["skipped"], {"scan_symbol_blocklisted": 1})


if __name__ == "__main__":
    unittest.main()
