"""Binance USD-M public market data and demo-only durable execution.

No live authenticated transport or automatic promotion exists in this module.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

LIVE_MARKET_URL = "https://fapi.binance.com"
DEMO_URL = "https://demo-fapi.binance.com"
TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite numeric value")
    return result


def order_quantity(equity, price, stop, instrument, risk_fraction, notional_cap, *, execution_price=None):
    equity, price, stop, risk_fraction, cap = map(number, (equity, price, stop, risk_fraction, notional_cap))
    execution_price = price if execution_price is None else number(execution_price)
    if min(equity, price, execution_price, stop, risk_fraction, cap) <= 0 or risk_fraction > 1 or price == stop:
        raise ValueError("Invalid sizing inputs")
    filters = {f["filterType"]: f for f in instrument["filters"]}
    lot = filters.get("MARKET_LOT_SIZE", filters.get("LOT_SIZE"))
    if not lot or number(lot["stepSize"]) <= 0:
        raise ValueError("Missing market lot filter")
    step = number(lot["stepSize"])
    distance = max(abs(price - stop), abs(execution_price - stop))
    qty = min(equity * risk_fraction / distance, cap / max(price, execution_price), number(lot["maxQty"]))
    qty = (qty / step).to_integral_value(rounding=ROUND_FLOOR) * step
    minimum = number(filters.get("MIN_NOTIONAL", {}).get("notional", "0"))
    if qty <= 0 or qty < number(lot["minQty"]) or qty * min(price, execution_price) < minimum:
        raise ValueError("Order below Binance minimum; risk cap will not be increased")
    return format(qty, "f")


def closed_candles(rows, now_ms):
    bars = []
    for row in rows:
        if int(row[6]) >= now_ms:
            continue
        values = [float(number(v)) for v in row[1:6]]
        o, h, low, c, volume = values
        if min(o, h, low, c) <= 0 or volume < 0 or h < max(o, c, low) or low > min(o, c):
            raise ValueError("Invalid OHLCV")
        if bars and int(row[0]) <= bars[-1]["open_time"]:
            raise ValueError("Non-increasing candles")
        bars.append(
            dict(open_time=int(row[0]), close_time=int(row[6]), open=o, high=h, low=low, close=c, volume=volume)
        )
    return bars


class ApiError(RuntimeError):
    def __init__(self, status, code=None):
        self.status, self.code = status, code
        super().__init__(f"Binance API HTTP {status}, code={code}")


class TransportError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiError(code)


def http_json(request):
    # Never follow redirects carrying an API key. Never surface URLs/signatures.
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=12) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        code = None
        try:
            code = json.loads(exc.read()).get("code")
        except (ValueError, AttributeError):
            pass
        raise ApiError(exc.code, code) from None
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise TransportError(f"Binance transport failure ({type(exc).__name__})") from None


class DemoClient:
    def __init__(self, key="", secret="", *, base_url=DEMO_URL):
        if base_url != DEMO_URL:
            raise ValueError("Only Binance Demo endpoint is permitted")
        self.key, self.secret = key, secret
        self.time_offset = 0

    def public(self, path, params=None, *, demo=False):
        if path not in {
            "/fapi/v1/time",
            "/fapi/v1/exchangeInfo",
            "/fapi/v1/klines",
            "/fapi/v1/ticker/24hr",
            "/fapi/v1/premiumIndex",
        }:
            raise ValueError("Unsupported public endpoint")
        url = (DEMO_URL if demo else LIVE_MARKET_URL) + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in range(3):
            try:
                return http_json(urllib.request.Request(url))
            except TransportError:
                if attempt == 2:
                    raise
                time.sleep(0.2 * 2**attempt)

    def sync_time(self):
        before = int(time.time() * 1000)
        server = self.public("/fapi/v1/time", demo=True)["serverTime"]
        self.time_offset = int(server) - (before + int(time.time() * 1000)) // 2

    def request(self, method, path, params=None):
        allowed = {
            ("GET", "/fapi/v3/account"),
            ("GET", "/fapi/v3/positionRisk"),
            ("GET", "/fapi/v1/positionSide/dual"),
            ("GET", "/fapi/v1/openOrders"),
            ("GET", "/fapi/v1/openAlgoOrders"),
            ("GET", "/fapi/v1/userTrades"),
            ("GET", "/fapi/v1/income"),
            ("GET", "/fapi/v1/order"),
            ("POST", "/fapi/v1/order"),
            ("DELETE", "/fapi/v1/order"),
            ("GET", "/fapi/v1/algoOrder"),
            ("POST", "/fapi/v1/algoOrder"),
            ("DELETE", "/fapi/v1/algoOrder"),
        }
        if (method, path) not in allowed:
            raise ValueError("Unsupported demo operation")
        if not self.key or not self.secret:
            raise RuntimeError("Binance Demo credentials missing")
        data = dict(params or {})
        data.update(timestamp=int(time.time() * 1000) + self.time_offset, recvWindow=5000)
        query = urllib.parse.urlencode(data)
        signature = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        encoded = (query + "&signature=" + signature).encode()
        url = DEMO_URL + path
        body = None
        if method == "GET":
            url += "?" + encoded.decode()
        else:
            body = encoded
        return http_json(
            urllib.request.Request(
                url,
                data=body,
                method=method,
                headers={"X-MBX-APIKEY": self.key, "Content-Type": "application/x-www-form-urlencoded"},
            )
        )

    def bars(self, symbol, interval, limit=300):
        now = self.public("/fapi/v1/time")["serverTime"]
        rows = self.public("/fapi/v1/klines", dict(symbol=symbol, interval=interval, limit=limit))
        return closed_candles(rows, now)


def persist(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(data, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class DemoBook:
    def __init__(self, path, client):
        self.path, self.client = Path(path), client
        self.data = (
            json.loads(self.path.read_text())
            if self.path.exists()
            else {"execution_mode": "binance_demo", "orders": {}, "positions": [], "events": [], "fault": None}
        )
        if self.data.get("execution_mode") != "binance_demo" or not isinstance(self.data.get("orders"), dict):
            raise ValueError("Invalid Demo ledger; refusing to reset")

    def save(self):
        self.data["updated_at"] = time.time()
        persist(self.path, self.data)

    def event(self, kind, **fields):
        event = dict(type=kind, created_at=time.time(), **fields)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with (self.path.parent / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.data["events"].append(event)
        self.data["events"] = self.data["events"][-200:]
        self.save()

    def order(self, identity, params, *, algo=False):
        key = ("algo:" if algo else "order:") + identity
        endpoint = "/fapi/v1/algoOrder" if algo else "/fapi/v1/order"
        record = self.data["orders"].get(key)
        if record and record["params"] != params:
            raise ValueError("Order identity reused with different parameters")
        if not record:
            record = dict(
                params=dict(params), client_id="lq-" + hashlib.sha256(key.encode()).hexdigest()[:28], response=None
            )
            self.data["orders"][key] = record
            # Commit intent BEFORE network access. Even a crash must not resubmit.
            self.save()
            body = dict(params)
            body["clientAlgoId" if algo else "newClientOrderId"] = record["client_id"]
            try:
                record["response"] = self.client.request("POST", endpoint, body)
                self.save()
            except (RuntimeError, OSError):
                pass
        response = record.get("response") or {}
        status = response.get("algoStatus" if algo else "status")
        if (algo and response.get("algoId")) or status in TERMINAL:
            return response
        query = (
            {"clientAlgoId": record["client_id"]}
            if algo
            else {"symbol": params["symbol"], "origClientOrderId": record["client_id"]}
        )
        try:
            record["response"] = self.client.request("GET", endpoint, query)
            self.save()
            return record["response"]
        except (RuntimeError, OSError):
            self.event("order_unknown", identity=identity)
            raise RuntimeError("Order status unknown; entries blocked until reconciliation") from None

    def snapshot(self):
        return dict(
            execution_mode="binance_demo",
            market_data_source="binance_usdm_live",
            updated_at=self.data.get("updated_at"),
            fault=self.data.get("fault"),
            positions=self.data["positions"],
            events=self.data["events"][-30:],
        )


class DemoEngine:
    """One-way account, durable intents, actual fills, exchange-side initial stop."""

    def __init__(self, book, version, reward_risk=2):
        self.book, self.client, self.version = book, book.client, version
        self.reward_risk = reward_risk

    def enter(self, signal, quantity, tick_size):
        from binance_strategy_bot import open_position

        identity = f"{signal['symbol']}:{signal['interval']}:{signal['signal']}:{signal.get('divergence_time') or signal['kline_close_time']}"
        positions = self.book.data["positions"]
        position = next((p for p in positions if p["id"] == identity), None)
        if position and position["status"] != "entry_pending":
            return position
        if not position:
            if self.book.data.get("fault"):
                raise RuntimeError("Demo entries blocked by fault")
            position = open_position(signal["symbol"], signal["interval"], signal, self.reward_risk)
            if not position:
                raise ValueError("Invalid signal or stop")
            position.update(
                id=identity,
                status="entry_pending",
                requested_qty=quantity,
                tick_size=tick_size,
                strategy_version=self.version,
                reference_price=signal["price"],
                signal=dict(signal),
            )
            positions.append(position)
            self.book.save()
        params = dict(
            symbol=position["symbol"],
            side="BUY" if position["direction"] == "long" else "SELL",
            type="MARKET",
            quantity=position["requested_qty"],
            newOrderRespType="RESULT",
        )
        response = self.book.order(identity, params)
        if response.get("status") not in TERMINAL:
            # Cancel remainder; acknowledge only after a terminal response is observed.
            response = self.client.request(
                "DELETE", "/fapi/v1/order", dict(symbol=position["symbol"], orderId=response["orderId"])
            )
            if response.get("status") not in TERMINAL:
                raise RuntimeError("Entry remainder unresolved")
            self.book.data["orders"]["order:" + identity]["response"] = response
            self.book.save()
        qty = number(response.get("executedQty", 0))
        if qty <= 0:
            position.update(status="rejected", entry_order=response)
            self.book.save()
            return position
        price = number(response.get("avgPrice", 0))
        if price <= 0:
            raise RuntimeError("Entry average fill price missing")
        position.update(
            executed_qty=format(qty, "f"),
            entry_price=float(price),
            entry_order=response,
            status="protection_pending",
            slippage_bps=float((price / number(position["reference_price"]) - 1) * 10000)
            * (1 if position["direction"] == "long" else -1),
        )
        self.book.save()
        self.protect(position)
        return position

    def protect(self, position):
        step = number(position["tick_size"])
        if step <= 0:
            raise ValueError("Invalid tick size")
        # Round towards entry so the initial stop never increases reference risk.
        from decimal import ROUND_CEILING

        rounding = ROUND_CEILING if position["direction"] == "long" else ROUND_FLOOR
        trigger = (number(position["initial_stop_loss"]) / step).to_integral_value(rounding=rounding) * step
        try:
            response = self.book.order(
                position["id"] + ":stop",
                dict(
                    symbol=position["symbol"],
                    side="SELL" if position["direction"] == "long" else "BUY",
                    type="STOP_MARKET",
                    algoType="CONDITIONAL",
                    triggerPrice=format(trigger, "f"),
                    closePosition="true",
                    workingType="CONTRACT_PRICE",
                ),
                algo=True,
            )
            if not response.get("algoId") or response.get("algoStatus") not in {"NEW", "WORKING"}:
                raise RuntimeError("Initial stop not active")
            position.update(stop_algo_id=response["algoId"], status="open")
            self.book.event(
                "demo_entry",
                symbol=position["symbol"],
                position_id=position["id"],
                strategy_version=position["strategy_version"],
            )
        except (RuntimeError, OSError):
            self.book.data["fault"] = "Protection unavailable; emergency reduce-only exit required"
            self.book.save()
            self.close(position, "protection_failure")
            raise RuntimeError("Protection failed; entries blocked") from None

    def close(self, position, reason):
        position.update(status="exit_pending", exit_reason=reason)
        self.book.save()
        response = self.book.order(
            position["id"] + ":exit",
            dict(
                symbol=position["symbol"],
                side="SELL" if position["direction"] == "long" else "BUY",
                type="MARKET",
                quantity=position["executed_qty"],
                reduceOnly="true",
                newOrderRespType="RESULT",
            ),
        )
        position["exit_order"] = response
        self.book.save()
        if response.get("status") != "FILLED":
            raise RuntimeError("Exit not fully filled; reconciliation required")
        # Exchange position reconciliation (not this response) establishes closure.

    def reconcile(self):
        if self.client.request("GET", "/fapi/v1/positionSide/dual").get("dualSidePosition") is not False:
            raise RuntimeError("Demo account must use one-way position mode")
        for p in self.book.data["positions"]:
            if p["status"] == "entry_pending":
                self.enter(p["signal"], p["requested_qty"], p["tick_size"])
            if p["status"] == "protection_pending":
                self.protect(p)
        rows = self.client.request("GET", "/fapi/v3/positionRisk")
        actual = {r["symbol"]: number(r["positionAmt"]) for r in rows if number(r["positionAmt"]) != 0}
        tracked = {p["symbol"]: p for p in self.book.data["positions"] if p["status"] in {"open", "exit_pending"}}
        if set(actual) - set(tracked):
            raise RuntimeError("Demo account has untracked positions")
        for symbol, p in tracked.items():
            amount = actual.get(symbol, Decimal(0))
            expected = number(p["executed_qty"]) * (1 if p["direction"] == "long" else -1)
            if amount == 0:
                # Cancel this strategy's stop only. An unresolved cancel blocks reuse.
                if p.get("stop_algo_id"):
                    stop = self.client.request("GET", "/fapi/v1/algoOrder", {"algoId": p["stop_algo_id"]})
                    if not p.get("exit_order") and str(stop.get("actualOrderId", "0")) not in {"", "0", "None"}:
                        p["exit_order"] = self.client.request(
                            "GET", "/fapi/v1/order", dict(symbol=symbol, orderId=stop["actualOrderId"])
                        )
                        p["exit_reason"] = "exchange_initial_stop"
                    self.book.save()
                    if p.get("exit_order", {}).get("status") != "FILLED":
                        raise RuntimeError("Flat position without verified exit fill; reconciliation required")
                    if stop.get("algoStatus") in {"NEW", "WORKING"}:
                        self.client.request("DELETE", "/fapi/v1/algoOrder", {"algoId": p["stop_algo_id"]})
                if p.get("exit_order", {}).get("status") != "FILLED":
                    raise RuntimeError("Flat position without verified exit fill; reconciliation required")
                p["exit_price"] = float(number(p["exit_order"]["avgPrice"]))
                p.update(status="closed", closed_at=time.time())
                self.book.event(
                    "demo_closed", symbol=symbol, position_id=p["id"], strategy_version=p["strategy_version"]
                )
            elif amount != expected:
                raise RuntimeError("Position quantity mismatch; entries blocked")
            elif p["status"] == "exit_pending":
                self.close(p, p["exit_reason"])
                raise RuntimeError("Waiting for exchange position closure")
            else:
                stop = self.client.request("GET", "/fapi/v1/algoOrder", {"algoId": p["stop_algo_id"]})
                if stop.get("algoStatus") not in {"NEW", "WORKING"}:
                    self.book.data["fault"] = "Initial stop inactive"
                    self.book.save()
                    self.close(p, "stop_inactive")
                    raise RuntimeError("Initial stop inactive; entries blocked")
        # Any foreign pending order could create exposure outside the ledger.
        if self.client.request("GET", "/fapi/v1/openOrders"):
            raise RuntimeError("Unresolved open orders; entries blocked")
        owned_stops = {p.get("stop_algo_id") for p in tracked.values()}
        for order in self.client.request("GET", "/fapi/v1/openAlgoOrders"):
            if order.get("algoId") not in owned_stops:
                raise RuntimeError("Untracked conditional orders; entries blocked")
        self.book.save()


def read_status(path):
    base = dict(execution_mode="binance_demo", market_data_source="binance_usdm_live", positions=[], events=[])
    if not Path(path).exists():
        return dict(base, status="not_started")
    try:
        book = DemoBook(path, None)
        result = book.snapshot()
        result.update(scan_error=book.data.get("scan_error"), last_scan_at=book.data.get("last_scan_at"))
        result["status"] = "error" if result["fault"] or result["scan_error"] else "running"
        heartbeat = book.data.get("updated_at") if book.data.get("scan_in_progress") else result.get("last_scan_at")
        if not heartbeat or time.time() - heartbeat > 180:
            result["status"] = "stale" if result["status"] != "error" else "error"
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return dict(base, status="error", fault="Demo ledger unreadable")
