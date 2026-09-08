import tempfile
import unittest
from pathlib import Path


class DemoBehaviorTest(unittest.TestCase):
    def setUp(self):
        import binance_demo

        self.m = binance_demo

    def test_quantity_respects_risk_cap_and_exchange_step(self):
        info = {
            "filters": [
                {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.1", "minQty": "0.1", "maxQty": "1000"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ]
        }
        self.assertEqual(self.m.order_quantity("1000", "5.58", "5.47", info, ".005", "50"), "8.9")
        with self.assertRaises(ValueError):
            self.m.order_quantity("1", "5.58", "5.47", info, ".001", "50")
        with self.assertRaises(ValueError):
            self.m.order_quantity("NaN", "5.58", "5.47", info, ".001", "50")

    def test_live_host_cannot_receive_authenticated_request(self):
        with self.assertRaises(ValueError):
            self.m.DemoClient("key", "secret", base_url="https://fapi.binance.com")

    def test_candles_exclude_unfinished_and_invalid_history(self):
        rows = [[0, "1", "2", ".5", "1.5", "10", 59999], [60000, "1", "2", ".5", "1.5", "10", 119999]]
        self.assertEqual(len(self.m.closed_candles(rows, 90000)), 1)
        rows[0][4] = "NaN"
        with self.assertRaises(ValueError):
            self.m.closed_candles(rows, 90000)

    def test_timeout_is_persisted_and_reconciled_without_resubmit(self):
        class Exchange:
            posts = 0

            def request(self, method, path, params=None):
                if method == "POST":
                    self.posts += 1
                    raise TimeoutError()
                return {"orderId": 123, "status": "FILLED", "executedQty": "1", "avgPrice": "10"}

        exchange = Exchange()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            book = self.m.DemoBook(path, exchange)
            result = book.order("signal-a", {"symbol": "TESTUSDT", "side": "BUY", "type": "MARKET", "quantity": "1"})
            self.assertEqual(result["status"], "FILLED")
            restarted = self.m.DemoBook(path, exchange)
            self.assertEqual(
                restarted.order("signal-a", {"symbol": "TESTUSDT", "side": "BUY", "type": "MARKET", "quantity": "1"})[
                    "orderId"
                ],
                123,
            )
            self.assertEqual(exchange.posts, 1)

    def test_unknown_order_stops_duplicate_submission_after_restart(self):
        class Exchange:
            posts = 0

            def request(self, method, path, params=None):
                if method == "POST":
                    self.posts += 1
                raise TimeoutError()

        exchange = Exchange()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    self.m.DemoBook(path, exchange).order(
                        "signal-a", {"symbol": "TESTUSDT", "side": "BUY", "type": "MARKET", "quantity": "1"}
                    )
            self.assertEqual(exchange.posts, 1)

    def test_order_identity_cannot_be_reused_with_different_quantity(self):
        class Exchange:
            def request(self, method, path, params=None):
                return {"orderId": 1, "status": "FILLED", "executedQty": "1", "avgPrice": "10"}

        with tempfile.TemporaryDirectory() as d:
            book = self.m.DemoBook(Path(d) / "state.json", Exchange())
            book.order("same", {"symbol": "XUSDT", "side": "BUY", "quantity": "1"})
            with self.assertRaises(ValueError):
                book.order("same", {"symbol": "XUSDT", "side": "BUY", "quantity": "2"})

    def test_corrupt_ledger_does_not_reset_positions(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            path.write_text("{broken")
            with self.assertRaises(ValueError):
                self.m.DemoBook(path, None)


class FakeDemoExchange:
    def __init__(self):
        self.posts = []
        self.amount = "0"
        self.partial = False
        self.fail_protection = False

    def request(self, method, path, params=None):
        params = params or {}
        if path.endswith("positionSide/dual"):
            return {"dualSidePosition": False}
        if path.endswith("account"):
            return {"availableBalance": "1000", "totalWalletBalance": "1000"}
        if path.endswith("positionRisk"):
            return [{"symbol": "TESTUSDT", "positionAmt": self.amount, "positionSide": "BOTH"}]
        if path.endswith("openOrders") or path.endswith("openAlgoOrders"):
            return []
        if path.endswith("userTrades"):
            return [{"id": 1, "orderId": 1, "commission": ".01", "commissionAsset": "USDT", "realizedPnl": "0"}]
        if path.endswith("algoOrder"):
            if self.fail_protection:
                raise RuntimeError("protection unavailable")
            if method == "POST":
                self.posts.append((path, dict(params)))
            return {"algoId": 9, "algoStatus": "NEW"}
        if path.endswith("/order"):
            if method == "POST":
                self.posts.append((path, dict(params)))
                self.amount = (
                    "0" if params.get("reduceOnly") == "true" else (".5" if self.partial else params["quantity"])
                )
            return {
                "orderId": 1,
                "status": "PARTIALLY_FILLED" if self.partial and method != "DELETE" else "FILLED",
                "executedQty": ".5" if self.partial else "1",
                "avgPrice": "10",
            }
        raise AssertionError((method, path))


class DemoEngineTest(unittest.TestCase):
    def setUp(self):
        import binance_demo as m

        self.m = m
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = FakeDemoExchange()
        self.book = m.DemoBook(Path(self.tmp.name) / "state.json", self.client)
        self.signal = dict(
            symbol="TESTUSDT",
            interval="15m",
            signal="long",
            price=10,
            stop_loss=9,
            kline_close_time=900000,
            divergence_time=1,
        )

    def test_entry_records_exchange_fill_and_installs_close_all_stop(self):
        engine = self.m.DemoEngine(self.book, "version-a")
        position = engine.enter(self.signal, "1", "0.1")
        self.assertEqual(position["entry_price"], 10.0)
        self.assertEqual(position["executed_qty"], "1")
        self.assertEqual(position["strategy_version"], "version-a")
        self.assertEqual(position["status"], "open")
        self.assertEqual(self.client.posts[-1][1]["closePosition"], "true")
        engine.enter(self.signal, "1", "0.1")
        self.assertEqual(len(self.client.posts), 2)

    def test_untracked_position_blocks_new_entry(self):
        self.client.amount = "2"
        with self.assertRaisesRegex(RuntimeError, "untracked"):
            self.m.DemoEngine(self.book, "a").reconcile()

    def test_partial_fill_does_not_become_requested_size(self):
        self.client.partial = True
        position = self.m.DemoEngine(self.book, "a").enter(self.signal, "1", "0.1")
        self.assertEqual(position["executed_qty"], "0.5")
        self.assertEqual(position["status"], "open")

    def test_missing_protection_attempts_reduce_only_exit_and_blocks(self):
        self.client.fail_protection = True
        with self.assertRaises(RuntimeError):
            self.m.DemoEngine(self.book, "a").enter(self.signal, "1", "0.1")
        self.assertEqual(self.client.posts[-1][1].get("reduceOnly"), "true")
        self.assertTrue(self.book.data.get("fault"))


class DemoRunnerTest(unittest.TestCase):
    def test_runner_is_disabled_without_credentials_or_network(self):
        import os
        import subprocess
        import sys

        env = dict(os.environ, BINANCE_DEMO_API_KEY="", BINANCE_DEMO_SECRET_KEY="")
        result = subprocess.run(
            [sys.executable, "binance_demo_bot.py", "--check"], env=env, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("extension_disabled", result.stdout)

    def test_strategy_version_changes_with_config(self):
        import binance_demo_bot as runner

        self.assertNotEqual(
            runner.strategy_version({"bot": {"fee_rate": 0.001}}), runner.strategy_version({"bot": {"fee_rate": 0.002}})
        )


class FuturesDataRoutingTest(unittest.TestCase):
    def test_futures_mode_never_falls_back_to_gate_or_spot(self):
        import os
        from unittest.mock import patch

        import data_client

        with patch.dict(os.environ, {"MARKET_DATA_SOURCE": "binance_usdm"}):
            self.assertEqual(data_client.market_source_order(), ["binance_usdm"])
            urls = []

            def read(url, **kwargs):
                urls.append(url)
                return [[0, "1", "2", ".5", "1", "10", 59999]]

            with patch.object(data_client, "read_json_url", side_effect=read):
                self.assertEqual(data_client.fetch_klines("BTCUSDT", "1m")[0]["close"], 1)
            self.assertTrue(urls[0].startswith("https://fapi.binance.com/fapi/v1/klines?"))

    def test_futures_tickers_filter_requested_symbols(self):
        import os
        from unittest.mock import patch

        import data_client

        with (
            patch.dict(os.environ, {"MARKET_DATA_SOURCE": "binance_usdm"}),
            patch.object(data_client, "read_json_url", return_value=[{"symbol": "BTCUSDT"}, {"symbol": "ETHUSDT"}]),
        ):
            self.assertEqual(set(data_client.fetch_tickers(["BTCUSDT"])), {"BTCUSDT"})


class DemoStatusTest(unittest.TestCase):
    def test_missing_ledger_is_not_reported_as_running(self):
        import binance_demo

        with tempfile.TemporaryDirectory() as d:
            result = binance_demo.read_status(Path(d) / "state.json")
            self.assertEqual(result["status"], "not_started")

    def test_api_exposes_demo_separately_from_paper_tracking(self):
        import asyncio
        import os
        from unittest.mock import patch

        import app

        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"BINANCE_DEMO_DIR": d}):
            routes = {r.path: r for r in app.create_app().routes}
            self.assertIn("/api/binance-demo/status", routes)
            result = asyncio.run(routes["/api/binance-demo/status"].endpoint())
            self.assertEqual(result["status"], "extension_disabled")


class ReconciliationEvidenceTest(unittest.TestCase):
    def test_flat_position_without_exit_fill_is_not_reported_closed(self):
        import binance_demo as m

        with tempfile.TemporaryDirectory() as d:
            client = FakeDemoExchange()
            book = m.DemoBook(Path(d) / "state.json", client)
            engine = m.DemoEngine(book, "a")
            p = engine.enter(
                dict(symbol="TESTUSDT", interval="15m", signal="long", price=10, stop_loss=9, kline_close_time=900000),
                "1",
                ".1",
            )
            client.amount = "0"
            with self.assertRaisesRegex(RuntimeError, "exit fill"):
                engine.reconcile()
            self.assertNotEqual(p["status"], "closed")

    def test_authentication_is_only_sent_to_demo_and_signature_is_valid(self):
        import hashlib
        import hmac
        import urllib.parse
        from unittest.mock import patch

        import binance_demo as m

        requests = []
        with patch.object(m, "http_json", side_effect=lambda request: requests.append(request) or {}):
            m.DemoClient("test-key", "test-secret").request("GET", "/fapi/v3/account")
        request = requests[0]
        self.assertEqual(urllib.parse.urlsplit(request.full_url).netloc, "demo-fapi.binance.com")
        query = urllib.parse.urlsplit(request.full_url).query
        payload, signature = query.rsplit("&signature=", 1)
        self.assertEqual(signature, hmac.new(b"test-secret", payload.encode(), hashlib.sha256).hexdigest())


class DemoNotificationTest(unittest.TestCase):
    def test_demo_open_notification_is_distinguishable_from_paper_signal(self):
        from automatic_notifications import Outbox

        with tempfile.TemporaryDirectory() as d:
            box = Outbox(Path(d) / "outbox.json")
            box.observe([], [], None)
            box.observe(
                [
                    dict(
                        id="demo:a",
                        status="open",
                        symbol="BTCUSDT",
                        execution_mode="binance_demo",
                        strategy_version="v1",
                    )
                ],
                [],
                None,
            )
            self.assertIn("Binance", box.data["pending"][0]["title"])
            self.assertIn("v1", box.data["pending"][0]["body"])


class FuturesWatchlistTest(unittest.TestCase):
    def test_futures_only_symbol_is_validated_against_contracts(self):
        import os
        from unittest.mock import patch

        import app

        with (
            patch.dict(os.environ, {"MARKET_DATA_SOURCE": "binance_usdm", "WATCHLIST_VALIDATE_SYMBOLS": "true"}),
            patch(
                "binance_demo.DemoClient.public",
                return_value={
                    "symbols": [
                        {
                            "symbol": "1000PEPEUSDT",
                            "status": "TRADING",
                            "contractType": "PERPETUAL",
                            "quoteAsset": "USDT",
                        }
                    ]
                },
            ),
            patch.object(app, "fetch_binance_spot_symbols", return_value={"BTCUSDT"}),
        ):
            self.assertEqual(app.invalid_watchlist_symbols([{"symbol": "1000PEPEUSDT"}]), [])


if __name__ == "__main__":
    unittest.main()


class DemoPriceRiskTest(unittest.TestCase):
    def test_short_size_uses_worse_stop_distance_between_reference_and_demo(self):
        import binance_demo as m

        filters = {
            "filters": [
                {"filterType": "MARKET_LOT_SIZE", "stepSize": ".01", "minQty": ".01", "maxQty": "1000"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ]
        }
        self.assertEqual(m.order_quantity("1000", "100", "110", filters, ".005", "1000", execution_price="99"), "0.45")


class DemoHeartbeatTest(unittest.TestCase):
    def test_active_long_scan_is_not_mislabeled_stale(self):
        import time

        import binance_demo as m

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            book = m.DemoBook(path, None)
            book.data.update(last_scan_at=time.time() - 600, scan_in_progress=True)
            book.save()
            self.assertEqual(m.read_status(path)["status"], "running")


class DemoEventHistoryTest(unittest.TestCase):
    def test_recent_events_are_bounded_but_archive_retains_all(self):
        import binance_demo as m

        with tempfile.TemporaryDirectory() as d:
            book = m.DemoBook(Path(d) / "state.json", None)
            for index in range(205):
                book.event("demo_entry", position_id=str(index))
            self.assertEqual(len(book.data["events"]), 200)
            self.assertEqual(len((Path(d) / "events.jsonl").read_text().splitlines()), 205)


class PublicTransportTest(unittest.TestCase):
    def test_public_read_recovers_from_truncated_response(self):
        import http.client
        from unittest.mock import patch

        import binance_demo as m

        class Response:
            calls = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, *args):
                self.calls += 1
                if self.calls == 1:
                    raise http.client.IncompleteRead(b"partial", 20)
                return b'{"serverTime":123}'

        response = Response()

        class Opener:
            def open(self, *args, **kwargs):
                return response

        with patch.object(m.urllib.request, "build_opener", return_value=Opener()), patch.object(m.time, "sleep"):
            self.assertEqual(m.DemoClient().public("/fapi/v1/time")["serverTime"], 123)
            self.assertEqual(response.calls, 2)
