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

    def test_okx_multi_bot_delegates_shared_exit_decision_to_executor(self) -> None:
        args = argparse.Namespace()
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


if __name__ == "__main__":
    unittest.main()
