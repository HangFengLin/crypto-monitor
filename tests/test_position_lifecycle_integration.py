from __future__ import annotations

import argparse
import unittest
from unittest.mock import patch

import app
import binance_strategy_bot as binance_bot
import okx_demo_bot as demo_bot
import okx_market_cap_bot as okx_bot


class PositionLifecycleIntegrationTest(unittest.TestCase):
    def test_web_ledger_uses_stop_first_for_same_bar(self) -> None:
        trade = {
            "status": "open",
            "direction": "long",
            "entry_price": 100,
            "stop_loss": 95,
            "target_price": 110,
            "protection_price": 105,
            "opened_kline_close_time": 1,
        }
        closed = app.evaluate_open_strategy_trade_with_bars(
            trade,
            [{"close_time": 2, "low": 94, "high": 111, "close": 100}],
            100,
        )
        self.assertTrue(closed)
        self.assertEqual((trade["exit_reason"], trade["exit_price"]), ("stop_loss", 95))

    def test_web_strategy_stats_counts_breakeven_without_inflating_wins(self) -> None:
        stats = app.calculate_strategy_stats(
            [
                {"status": "closed", "outcome": "win", "return_pct": 0.02, "exit_reason": "take_profit"},
                {"status": "closed", "outcome": "breakeven", "return_pct": 0.0, "exit_reason": "protected_stop"},
                {"status": "closed", "outcome": "loss", "return_pct": -0.01, "exit_reason": "stop_loss"},
            ]
        )

        self.assertEqual((stats["wins"], stats["breakevens"], stats["losses"]), (1, 1, 1))
        self.assertAlmostEqual(stats["win_rate"], 1 / 3)

    def test_binance_paper_bot_uses_shared_stop_first_rule(self) -> None:
        position = {
            "status": "open",
            "direction": "short",
            "stop_loss": 105,
            "target_price": 90,
            "protection_price": 95,
            "opened_kline_close_time": 1,
        }
        self.assertTrue(binance_bot.evaluate_position(position, [{"close_time": 2, "low": 89, "high": 106}]))
        self.assertEqual((position["exit_reason"], position["exit_price"]), ("stop_loss", 105))

    def test_binance_paper_bot_checks_all_bars_after_open(self) -> None:
        position = {
            "status": "open",
            "direction": "long",
            "entry_price": 100,
            "initial_stop_loss": 95,
            "active_stop": 95,
            "stop_loss": 95,
            "target_price": 110,
            "protection_price": 105,
            "opened_kline_close_time": 1,
        }
        bars = [
            {"close_time": 2, "low": 94, "high": 101, "close": 99},
            {"close_time": 3, "low": 99, "high": 101, "close": 100},
            {"close_time": 4, "low": 99, "high": 101, "close": 100},
            {"close_time": 5, "low": 99, "high": 101, "close": 100},
        ]

        self.assertTrue(binance_bot.evaluate_position(position, bars))
        self.assertEqual((position["exit_reason"], position["exit_price"]), ("stop_loss", 95))

    def test_okx_multi_bot_delegates_shared_exit_decision_to_executor(self) -> None:
        args = argparse.Namespace(position_stop_mode="structure_atr", fee_rate=0.001)
        position = {
            "status": "open",
            "direction": "long",
            "entry_price": 100,
            "stop_loss": 95,
            "target_price": 110,
            "protection_price": 105,
        }
        with patch.object(okx_bot, "close_position") as close:
            self.assertTrue(okx_bot.evaluate_position(args, position, [{"low": 94, "high": 111, "close": 100}]))
        close.assert_called_once_with(args, position, 95, "stop_loss")

    def test_okx_multi_bot_checks_all_bars_after_open(self) -> None:
        args = argparse.Namespace(position_stop_mode="structure_atr", fee_rate=0.001)
        position = {
            "status": "open",
            "direction": "long",
            "entry_price": 100,
            "initial_stop_loss": 95,
            "active_stop": 95,
            "stop_loss": 95,
            "target_price": 110,
            "protection_price": 105,
            "opened_kline_close_time": 1,
        }
        bars = [
            {"close_time": 2, "low": 94, "high": 101, "close": 99},
            {"close_time": 3, "low": 99, "high": 101, "close": 100},
            {"close_time": 4, "low": 99, "high": 101, "close": 100},
        ]

        with patch.object(okx_bot, "close_position") as close:
            self.assertTrue(okx_bot.evaluate_position(args, position, bars))

        close.assert_called_once_with(args, position, 95, "stop_loss")

    def test_okx_close_position_records_fee_adjusted_return(self) -> None:
        args = argparse.Namespace(
            place_order=False,
            okx_instrument_type="SWAP",
            trade_mode="cross",
            size="1",
            fee_rate=0.001,
        )
        position = {
            "symbol": "BTCUSDT",
            "inst_id": "BTC-USDT-SWAP",
            "interval": "15m",
            "direction": "long",
            "size": "1",
            "entry_price": 100,
            "position_sizing": {"mode": "fixed"},
        }

        with patch.object(okx_bot, "send_discord"), patch.object(okx_bot, "append_event"):
            okx_bot.close_position(args, position, 101, "take_profit")

        self.assertAlmostEqual(position["return_pct"], 0.008)

    def test_okx_single_bot_delegates_shared_exit_decision_to_executor(self) -> None:
        args = argparse.Namespace()
        position = demo_bot.OpenPosition(
            symbol="BTCUSDT",
            interval="15m",
            direction="short",
            size="1",
            entry_price=100,
            stop_loss=105,
            target_price=90,
            protection_price=95,
            opened_at=1,
            opened_close_time=1,
            signal_score=10,
            signal_grade="A",
            order_id="test",
            highest_price=100,
            lowest_price=100,
        )
        with patch.object(demo_bot, "save_position"), patch.object(demo_bot, "close_position") as close:
            result = demo_bot.evaluate_position(args, position, [{"low": 89, "high": 106, "close": 100}], True)
        self.assertIsNone(result)
        close.assert_called_once_with(args, position, 105, "stop_loss", True)

    def test_okx_single_bot_close_position_reports_fee_adjusted_return(self) -> None:
        args = argparse.Namespace(fee_rate=0.001)
        position = demo_bot.OpenPosition(
            symbol="BTCUSDT",
            interval="15m",
            direction="long",
            size="1",
            entry_price=100,
            stop_loss=95,
            target_price=110,
            protection_price=105,
            opened_at=1,
            opened_close_time=1,
            signal_score=10,
            signal_grade="A",
            order_id="test",
            highest_price=100,
            lowest_price=100,
        )

        with patch.object(demo_bot, "save_position"), patch.object(demo_bot, "send_discord") as send:
            demo_bot.close_position(args, position, 101, "take_profit", True)

        message = send.call_args.args[0]
        self.assertIn("return=0.80%", message)

    def test_okx_pair_protection_blocks_recent_low_profit_symbol(self) -> None:
        args = argparse.Namespace(
            low_profit_pair_min_trades=2,
            low_profit_pair_lookback=3,
            low_profit_pair_threshold=0.0,
            low_profit_pair_cooldown_hours=0,
        )
        state = {
            "positions": [
                {"symbol": "BTCUSDT", "status": "closed", "return_pct": -0.01, "closed_at": 2},
                {"symbol": "BTCUSDT", "status": "closed", "return_pct": -0.02, "closed_at": 3},
                {"symbol": "ETHUSDT", "status": "closed", "return_pct": -0.10, "closed_at": 4},
            ]
        }
        blocked, details = okx_bot.low_profit_pair_blocked(args, state, "BTCUSDT")
        self.assertTrue(blocked)
        self.assertEqual(details["closed_trades"], 2)

    def test_okx_pair_protection_requires_minimum_trade_sample(self) -> None:
        args = argparse.Namespace(
            low_profit_pair_min_trades=3,
            low_profit_pair_lookback=3,
            low_profit_pair_threshold=0.0,
            low_profit_pair_cooldown_hours=0,
        )
        state = {"positions": [{"symbol": "BTCUSDT", "status": "closed", "return_pct": -0.01, "closed_at": 2}]}
        blocked, details = okx_bot.low_profit_pair_blocked(args, state, "BTCUSDT")
        self.assertFalse(blocked)
        self.assertEqual(details["closed_trades"], 1)

    def test_okx_stoploss_guard_blocks_after_recent_stop_losses(self) -> None:
        args = argparse.Namespace(
            stoploss_guard_min_trades=2,
            stoploss_guard_lookback=3,
            stoploss_guard_cooldown_hours=0,
        )
        state = {
            "positions": [
                {"symbol": "BTCUSDT", "status": "closed", "exit_reason": "stop_loss", "closed_at": 3},
                {"symbol": "ETHUSDT", "status": "closed", "exit_reason": "take_profit", "closed_at": 2},
                {"symbol": "SOLUSDT", "status": "closed", "exit_reason": "stop_loss", "closed_at": 1},
            ]
        }
        blocked, details = okx_bot.stoploss_guard_blocked(args, state)
        self.assertTrue(blocked)
        self.assertEqual(details["stop_losses"], 2)

    def test_okx_stoploss_guard_requires_recent_sample(self) -> None:
        args = argparse.Namespace(
            stoploss_guard_min_trades=2,
            stoploss_guard_lookback=3,
            stoploss_guard_cooldown_hours=0,
        )
        state = {"positions": [{"symbol": "BTCUSDT", "status": "closed", "exit_reason": "stop_loss", "closed_at": 3}]}
        blocked, details = okx_bot.stoploss_guard_blocked(args, state)
        self.assertFalse(blocked)
        self.assertEqual(details["stop_losses"], 1)

    def test_okx_entry_signal_filter_matches_candidate_long_macd_structure(self) -> None:
        args = argparse.Namespace(
            signal_direction="long",
            divergence_filter="macd",
            structure_text_exact="底分型确认，下跌笔力度衰竭",
            structure_text_contains=None,
        )
        good = {"signal": "long", "divergence_type": "macd", "structure_text": "底分型确认，下跌笔力度衰竭"}
        self.assertIsNone(okx_bot.entry_signal_filter_reason(args, good))
        self.assertEqual(okx_bot.entry_signal_filter_reason(args, {**good, "signal": "short"}), "signal_direction")
        self.assertEqual(okx_bot.entry_signal_filter_reason(args, {**good, "divergence_type": "fast_macd"}), "divergence_filter")
        self.assertEqual(okx_bot.entry_signal_filter_reason(args, {**good, "structure_text": "底分型确认"}), "structure_text_exact")


if __name__ == "__main__":
    unittest.main()
