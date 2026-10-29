import asyncio
import copy
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app


def sample_trade():
    return {
        "id": "trade-1", "symbol": "BTCUSDT", "interval": "15m", "direction": "short",
        "status": "open", "entry_price": 100.0, "opened_at": 100, "closed_at": None,
        "return_model": "linear_usdm_v1", "return_pct": None, "unrealized_pct": 0.0,
        "fee_rate": 0.0, "config_revision": "config-1", "execution_revision": "execution-1",
        "exit_price": None, "exit_reason": "open", "stop_loss": 105.0, "initial_stop_loss": 105.0,
        "active_stop": 105.0, "protected_stop_price": 0.0, "protection_activated": False,
        "target_price": 90.0, "last_evaluated_close_time": 123,
        "runtime_config": {"values": {"paper": {"fee_rate": 0.0}}, "large": "x" * 4000},
        "decision_trace": {"bars": [1, 2, 3]}, "paper_universe": {"snapshot_id": "pool-1"},
        "future_accounting_field": {"unknown": None, "zero": 0, "empty": []},
    }


def sample_state():
    trade = sample_trade()
    return {
        "updated_at": 100, "watchlist": [{"symbol": "BTCUSDT"}], "last_error": None,
        "prices": {"BTCUSDT": {"lastPrice": 0.0, "priceChange": None}},
        "signals": {"BTCUSDT:15m": {"signal": "wait", "td_summary": "1m:-", "runtime_config": trade["runtime_config"]}},
        "events": [{"type": "chanlun", "symbol": "BTCUSDT", "runtime_config": trade["runtime_config"]}],
        "strategy_trades": [trade], "strategy_stats": {"win_rate": None, "expectancy": 0.0},
        "signal_tracking": {"mode": "paper", "positions": [copy.deepcopy(trade)], "updated_at": 100},
        "runtime_settings": {"revision": "current", "values": {"paper": {"fee_rate": 0.0}}},
        "paper_data_fault": {"active": False, "details": {}},
    }


class PayloadEndpointTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app.create_app())

    def test_compact_state_omits_large_record_detail_and_preserves_market_accounting_and_exit_fields(self):
        # Losing nulls/zeroes or exit fields changes display/accounting meaning;
        # retaining the repeated runtime configuration defeats the smaller wire format.
        source = sample_state()
        original = copy.deepcopy(source)
        with patch.object(app, "snapshot", return_value=source):
            result = self.client.get("/api/state?detail=compact").json()
        self.assertNotIn("runtime_config", result["strategy_trades"][0])
        self.assertNotIn("decision_trace", result["strategy_trades"][0])
        self.assertNotIn("runtime_config", result["signal_tracking"]["positions"][0])
        self.assertNotIn("runtime_config", result["signals"]["BTCUSDT:15m"])
        self.assertNotIn("runtime_config", result["events"][0])
        expected_trade = {key: value for key, value in original["strategy_trades"][0].items()
                          if key not in {"runtime_config", "decision_trace"}}
        self.assertEqual(result["strategy_trades"][0], expected_trade)
        self.assertEqual(result["signal_tracking"]["positions"][0], expected_trade)
        for key in ("prices", "strategy_stats", "runtime_settings", "paper_data_fault"):
            self.assertEqual(result[key], original[key])
        self.assertEqual(source, original)

    def test_default_and_explicit_full_state_keep_complete_historical_detail(self):
        source = sample_state()
        with patch.object(app, "snapshot", return_value=source):
            for url in ("/api/state", "/api/state?detail=full"):
                with self.subTest(url=url):
                    self.assertEqual(self.client.get(url).json(), source)

    def test_paper_compact_has_same_accounting_and_universe_as_full_without_repeated_configuration(self):
        state = app.MonitorState(strategy_trades=[sample_trade()])
        with patch.object(app, "state", state), \
             patch.object(app, "paper_universe_snapshot", return_value={"ready": True, "members": [], "scan": {"errors": {}}}):
            full = self.client.get("/api/paper-trades").json()
            compact = self.client.get("/api/paper-trades?detail=compact").json()
        self.assertIn("runtime_config", full["trades"][0])
        self.assertNotIn("runtime_config", compact["trades"][0])
        self.assertEqual(compact["stats"], full["stats"])
        self.assertEqual(compact["comparison_status"], full["comparison_status"])
        self.assertEqual(compact["universe"], full["universe"])
        self.assertIsNone(compact["trades"][0]["return_pct"])
        self.assertEqual(compact["trades"][0]["protected_stop_price"], 0.0)
        self.assertIn("runtime_config", state.strategy_trades[0])

    def test_unsupported_detail_is_rejected_instead_of_silently_returning_a_different_contract(self):
        for path in ("/api/state", "/api/paper-trades"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path + "?detail=typo").status_code, 422)

    def test_compact_event_stream_matches_the_compact_state_contract(self):
        endpoint = next(route.endpoint for route in self.client.app.routes if getattr(route, "path", None) == "/api/events")

        async def first_frame():
            try:
                response = await endpoint(detail="compact")
            except TypeError as error:
                self.fail(f"The SSE endpoint does not support the compact payload contract: {error}")
            frame = await anext(response.body_iterator)
            await response.body_iterator.aclose()
            return json.loads(frame.removeprefix("data: "))

        with patch.object(app, "snapshot", return_value=sample_state()):
            result = asyncio.run(first_frame())
        self.assertNotIn("runtime_config", result["strategy_trades"][0])
        self.assertEqual(result["signal_tracking"]["positions"][0]["exit_price"], None)

    def test_projection_results_are_independent_of_live_state_in_both_details(self):
        project = getattr(app, "project_state", None)
        self.assertTrue(callable(project), "State responses need an independent payload projection")
        for detail in ("full", "compact"):
            with self.subTest(detail=detail):
                source = sample_state()
                result = project(source, detail)
                result["signal_tracking"]["positions"][0]["future_accounting_field"]["empty"].append("new")
                result["runtime_settings"]["values"]["paper"]["fee_rate"] = 1
                self.assertEqual(source["signal_tracking"]["positions"][0]["future_accounting_field"]["empty"], [])
                self.assertEqual(source["runtime_settings"]["values"]["paper"]["fee_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
