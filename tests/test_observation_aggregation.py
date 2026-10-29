"""Observation grouping at a real scan boundary, without external delivery."""

import asyncio
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app
from automatic_notifications import Outbox


class ObservationAggregationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = self.root / "events.jsonl"
        for replacement in (
            patch.object(app, "state", app.MonitorState()),
            patch.object(app, "EVENT_LOG_FILE", self.log),
            patch.object(app, "STRATEGY_TRADES_FILE", self.root / "trades.json"),
            patch.object(app, "STATE_FILE", self.root / "watchlist.json"),
            patch.object(app, "PUBLIC_DIR", self.root / "public"),
            patch.object(app, "REPORTS_DIR", self.root / "reports"),
            patch.object(app, "FAULT_TRACKER", None),
            patch.object(app, "list_report_files", return_value=[]),
            patch.object(app, "site_monitor_snapshot", return_value={"enabled": False}),
            patch.object(app, "send_discord_alert", side_effect=lambda alert: None),
            patch.object(app, "SUPPORT_INTERVALS", ("1h", "4h")),
            patch.object(app, "INDICATOR_INTERVALS", ("15m",)),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        app.state.watchlist = [{"symbol": "ETHUSDT"}]

    def records(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def level(self, symbol="ETHUSDT", interval="1h", *, price=100.2, support=100, close=1000):
        return {"type": "support", "symbol": symbol, "interval": interval, "price": price,
                "support": support, "tolerance_pct": .4, "distance_pct": (price / support - 1) * 100,
                "touched": price <= support * 1.004, "kline_close_time": close, "created_at": 100}

    def indicator(self, symbol, interval, close=1000):
        return {"type": "indicator", "symbol": symbol, "interval": interval, "price": 100.2,
                "kline_close_time": close, "created_at": 101,
                "ma": {"direction": "up", "label": "MA5/10上穿"},
                "macd": {"direction": None}}

    def run_round(self, *, indicator=None, strategy_error=None):
        async def run():
            stop = asyncio.Event()

            async def stop_after_round():
                stop.set()

            async def support(symbol, interval, price):
                return self.level(symbol, interval, price=price)

            async def default_indicator(symbol, interval):
                return self.indicator(symbol, interval)

            with ExitStack() as patches:
                for name, replacement in (
                    ("async_fetch_tickers", AsyncMock(return_value={
                        item["symbol"]: {"lastPrice": 100.2} for item in app.state.watchlist})),
                    ("update_strategy_trades_with_prices_async", AsyncMock()),
                    ("detect_support_level_async", support),
                    ("detect_indicator_signal_async", indicator or default_indicator),
                    ("evaluate_chanlun_signals_async", AsyncMock(side_effect=strategy_error)),
                    ("send_hourly_market_summary_async", AsyncMock()),
                    ("maybe_run_site_monitor_async", stop_after_round),
                ):
                    patches.enter_context(patch.object(app, name, new=replacement))
                await app.monitor_loop_async(stop)

        asyncio.run(run())

    def test_same_round_waits_for_indicators_while_open_and_fault_remain_immediate(self):
        box = Outbox(self.root / "outbox.json")
        box.observe(*app.automatic_notification_inputs())
        inspected = False

        async def inspect_mid_round(symbol, interval):
            nonlocal inspected
            app.state.strategy_trades = [{"id": "new-trade", "symbol": "ETHUSDT", "status": "open"}]
            app.state.published_fault = "技术指标"
            box.observe(*app.automatic_notification_inputs())
            self.assertEqual(len(box.data["pending"]), 2)
            self.assertEqual(box.data["pending"][0]["key"], "new-trade:open")
            self.assertEqual([item["channel"] for item in box.data["pending"]], ["signal", "system"])
            self.assertIn("技术指标", box.data["pending"][1]["body"])
            self.assertEqual(len([event for event in self.records() if event.get("type") == "support"]), 2)
            inspected = True
            return self.indicator(symbol, interval)

        self.run_round(indicator=inspect_mid_round)
        self.assertTrue(inspected)
        box.observe(*app.automatic_notification_inputs())
        observations = [m for m in box.data["pending"] if "观察提醒" in m["title"]]
        self.assertEqual(len(observations), 1)
        self.assertIn("15m", observations[0]["body"])
        self.assertIn("1h", observations[0]["body"])
        self.assertIn("4h", observations[0]["body"])
        self.assertIn("MA5/10上穿", observations[0]["body"])

    def test_different_symbols_get_separate_groups_and_raw_records_are_preserved(self):
        app.state.watchlist.append({"symbol": "BTCUSDT"})
        self.run_round()
        records = self.records()
        raw = [event for event in records if event.get("type") in {"support", "indicator"}]
        batches = [event for event in records if event.get("type") == "observation_batch"]
        self.assertEqual(len(raw), 6)
        self.assertEqual(len(batches), 1)
        self.assertEqual({alert["title"] for alert in batches[0]["alerts"]},
                         {"ETHUSDT 观察提醒", "BTCUSDT 观察提醒"})
        self.assertTrue(all(event.get("observation_round_id") for event in raw))

    def test_large_round_is_not_limited_to_the_fifty_event_display_ring(self):
        with patch.object(app, "INDICATOR_INTERVALS", tuple(f"{number}m" for number in range(60))), \
             patch.object(app, "SUPPORT_INTERVALS", ()):
            self.run_round()
        records = self.records()
        self.assertEqual(len([e for e in records if e.get("type") == "indicator"]), 60)
        batches = [e for e in records if e.get("type") == "observation_batch"]
        self.assertEqual(len(batches), 1)
        alert = batches[0]["alerts"][0]
        self.assertEqual(len(alert["source_event_keys"]), 60)
        self.assertIn("0m", alert["body"])
        self.assertIn("59m", alert["body"])
        self.assertEqual(len(app.state.events), 50)

    def test_aborted_round_publishes_only_the_observations_already_obtained(self):
        self.run_round(strategy_error=RuntimeError("injected scan failure"))
        batches = [event for event in self.records() if event.get("type") == "observation_batch"]
        self.assertEqual(len(batches), 1)
        self.assertFalse(batches[0]["complete"])
        self.assertIn("未完成", batches[0]["alerts"][0]["body"])
        self.assertNotIn("MA5/10", batches[0]["alerts"][0]["body"])

    def test_persistent_near_support_does_not_repeat_on_new_bars_or_restart(self):
        with patch.object(app, "SUPPORT_INTERVALS", ("1h",)):
            for close in (1000, 2000):
                with patch.object(app, "detect_support_level_async", new=AsyncMock(
                    return_value=self.level(close=close),
                )):
                    asyncio.run(app.evaluate_support_alerts_async(app.state.watchlist, {}))
            self.assertEqual(len([e for e in self.records() if e.get("type") == "support"]), 1)
            app.state = app.MonitorState()
            app.initialize_state()
            with patch.object(app, "detect_support_level_async", new=AsyncMock(
                return_value=self.level(close=3000),
            )):
                asyncio.run(app.evaluate_support_alerts_async([{"symbol": "ETHUSDT"}], {}))
            self.assertEqual(len([e for e in self.records() if e.get("type") == "support"]), 1)

    def test_departure_and_return_rearm_support_even_within_the_same_bar(self):
        with patch.object(app, "SUPPORT_INTERVALS", ("1h",)):
            for price in (100.2, 101.0, 100.2):
                with patch.object(app, "detect_support_level_async", new=AsyncMock(
                    return_value=self.level(price=price),
                )):
                    asyncio.run(app.evaluate_support_alerts_async(app.state.watchlist, {}))
        events = [e for e in self.records() if e.get("type") == "support"]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1]["support_transition"], "retest")
        self.assertNotEqual(events[0]["event_key"], events[1]["event_key"])

    def test_new_cross_is_not_suppressed_by_the_previous_cross_direction(self):
        with patch.object(app, "SUPPORT_INTERVALS", ()), patch.object(app, "INDICATOR_INTERVALS", ("15m",)):
            for close in (1000, 1000, 2000):
                with patch.object(app, "detect_indicator_signal_async", new=AsyncMock(
                    return_value=self.indicator("ETHUSDT", "15m", close),
                )):
                    asyncio.run(app.evaluate_indicator_signals_async(app.state.watchlist))
        self.assertEqual(len([e for e in self.records() if e.get("type") == "indicator"]), 2)

    def test_restart_merges_raw_journal_tail_missing_from_the_round_checkpoint(self):
        support = {**self.level(), "event_key": "support:saved", "observation_round_id": "interrupted"}
        indicator = {**self.indicator("ETHUSDT", "15m"), "event_key": "indicator:tail",
                     "observation_round_id": "interrupted", "indicator_label": "MA5/10上穿", "indicator": "ma"}
        self.log.write_text("\n".join(json.dumps(event) for event in [support, indicator]) + "\n")
        checkpoint = {"support_states": {}, "indicator_keys": {}, "pending_rounds": [],
                      "active_round": {"id": "interrupted", "created_at": 100, "events": [support]}}
        self.log.with_name(self.log.name + ".observations.json").write_text(json.dumps(checkpoint))
        app.initialize_state()
        app.commit_observation_rounds()
        groups = [event for event in self.records() if event.get("type") == "observation_batch"]
        self.assertEqual(groups[-1]["alerts"][0]["source_event_keys"], ["support:saved", "indicator:tail"])
        self.assertIn("MA5/10上穿", groups[-1]["alerts"][0]["body"])

    def test_recovered_pending_round_is_committed_after_the_first_outbox_baseline(self):
        support = {**self.level(), "event_key": "support:saved", "observation_round_id": "interrupted"}
        self.log.write_text(json.dumps(support) + "\n")
        checkpoint = {"support_states": {}, "indicator_keys": {}, "pending_rounds": [],
                      "active_round": {"id": "interrupted", "created_at": 100, "events": [support]}}
        self.log.with_name(self.log.name + ".observations.json").write_text(json.dumps(checkpoint))
        app.initialize_state()
        box = Outbox(self.root / "outbox.json")
        box.observe(*app.automatic_notification_inputs())
        app.commit_observation_rounds()
        box.observe(*app.automatic_notification_inputs())
        self.assertEqual([item["title"] for item in box.data["pending"]], ["ETHUSDT 观察提醒"])

    def test_corrupt_observation_checkpoint_is_preserved_and_does_not_block_trade_notices(self):
        checkpoint = self.log.with_name(self.log.name + ".observations.json")
        checkpoint.write_text("{broken")
        app.initialize_state()
        box = Outbox(self.root / "outbox.json")
        box.observe(*app.automatic_notification_inputs())
        self.run_round()
        app.state.strategy_trades = [{"id": "safe-trade", "status": "open", "symbol": "ETHUSDT"}]
        box.observe(*app.automatic_notification_inputs())
        self.assertIn("safe-trade:open", [item["key"] for item in box.data["pending"]])
        self.assertEqual(checkpoint.read_text(), "{broken")
        self.assertIn("暂停", app.state.module_errors["observations"])
        self.assertFalse(any(event.get("type") == "observation_batch" for event in self.records()))
