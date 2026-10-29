"""Saved-fill cashflow attribution; no strategy, target or replay imports."""
from __future__ import annotations

import datetime as dt
import math

STEP = 900_000


def curve_metrics(path, initial, start):
    peak, peak_time = initial, start
    worst = dict(max_close_drawdown=0.0, peak_time=start, trough_time=start,
                 peak_equity=initial, trough_equity=initial)
    for row in path:
        if row["equity"] > peak:
            peak, peak_time = row["equity"], row["time"]
        drawdown = 1 - row["equity"] / peak
        if drawdown > worst["max_close_drawdown"]:
            worst = dict(max_close_drawdown=drawdown, peak_time=peak_time,
                         trough_time=row["time"], peak_equity=peak,
                         trough_equity=row["equity"])
    worst["net_return"] = path[-1]["equity"] / initial - 1
    worst["final_equity"] = path[-1]["equity"]
    return worst


def rebuild_attribution(account, source_prices):
    """Use signed principal cashflows + held notional, not producer WAC PnL."""
    symbols = sorted(source_prices)
    initial = account["initial_equity"]
    start = account["equity"][0]["time"] - STEP
    events = [(row["time"], 1, index, "fill", row)
              for index, row in enumerate(account["fills"])]
    events += [(row["time"], 0, index, "funding", row)
               for index, row in enumerate(account["funding"])]
    events.sort(key=lambda row: row[:3])
    quantity = dict.fromkeys(symbols, 0.0)
    cashflows = dict.fromkeys(symbols, 0.0)
    fees = dict.fromkeys(symbols, 0.0)
    funding = dict.fromkeys(symbols, 0.0)
    path, without_eth = [], []
    pointer, max_error, max_quantity_error = 0, 0.0, 0.0
    for point in account["equity"]:
        while pointer < len(events) and events[pointer][0] < point["time"]:
            _stamp, _phase, _index, kind, row = events[pointer]
            symbol = row["symbol"]
            if kind == "fill":
                signed = row["signed_quantity"]
                quantity[symbol] += signed
                if row["quantity_after"] == 0 and abs(quantity[symbol]) < 1e-10:
                    quantity[symbol] = 0.0
                cashflows[symbol] -= signed * row["price"] + row["fee"]
                fees[symbol] += row["fee"]
            else:
                cashflows[symbol] += row["cashflow"]
                funding[symbol] += row["cashflow"]
            pointer += 1
        marked = {symbol: quantity[symbol] * source_prices[symbol][point["time"] - STEP]["close"] for symbol in symbols}
        contribution = {symbol: cashflows[symbol] + marked[symbol] for symbol in symbols}
        expected_equity = initial + math.fsum(contribution.values())
        max_error = max(max_error, abs(expected_equity - point["equity"]))
        max_quantity_error = max(max_quantity_error, *(abs(quantity[symbol] - point["quantities"][symbol]) for symbol in symbols))
        path.append(dict(time=point["time"], equity=expected_equity,
                         symbol_net=contribution, marked_notional=marked,
                         fees=dict(fees), funding=dict(funding),
                         components={symbol: dict(
                             trade_cashflow_before_cost=cashflows[symbol] + fees[symbol] - funding[symbol],
                             fees=fees[symbol], funding=funding[symbol],
                             held_notional=marked[symbol], net_contribution=contribution[symbol])
                             for symbol in symbols}))
        without_eth.append(dict(time=point["time"], equity=initial + math.fsum(value for symbol, value in contribution.items() if symbol != "ETHUSDT")))
    metrics = curve_metrics(path, initial, start)
    without_metrics = curve_metrics(without_eth, initial, start)
    by_time = {row["time"]: row for row in path}
    zero = dict.fromkeys(symbols, 0.0)
    peak_contribution = by_time.get(metrics["peak_time"], {}).get("symbol_net", zero)
    trough_contribution = by_time.get(metrics["trough_time"], {}).get("symbol_net", zero)
    dd_deltas = {symbol: trough_contribution[symbol] - peak_contribution[symbol] for symbol in symbols}
    dd_error = abs(math.fsum(dd_deltas.values()) - (metrics["trough_equity"] - metrics["peak_equity"]))
    final_net = path[-1]["symbol_net"]
    total = math.fsum(final_net.values())
    absolute = math.fsum(abs(value) for value in final_net.values())
    positive = math.fsum(max(value, 0.0) for value in final_net.values())
    eth = final_net.get("ETHUSDT", 0.0)
    exposure = {symbol: dict(mean_weight=math.fsum(row["marked_notional"][symbol] / row["equity"] for row in path) / len(path),
                             max_weight=max(row["marked_notional"][symbol] / row["equity"] for row in path),
                             bars_with_positive_quantity=sum(row["marked_notional"][symbol] > 0 for row in path)) for symbol in symbols}
    full_components = {}
    for symbol in symbols:
        realized = math.fsum(row["realized_gross_pnl"] for row in account["fills"] if row["symbol"] == symbol)
        tail = final_net[symbol] - realized + fees[symbol] - funding[symbol]
        full_components[symbol] = dict(realized_gross_pnl=realized, fees=fees[symbol],
                                       funding=funding[symbol], tail_unrealized_pnl=tail,
                                       net_contribution=final_net[symbol],
                                       final_quantity=quantity[symbol],
                                       final_held_notional=path[-1]["marked_notional"][symbol])
    years = {}
    prior = dict(time=start, equity=initial, symbol_net=zero,
                 fees=zero, funding=zero)
    prior_without = initial
    for year in sorted({dt.datetime.fromtimestamp((row["time"] - 1) / 1000, dt.timezone.utc).year for row in path}):
        segment = [row for row in path if dt.datetime.fromtimestamp((row["time"] - 1) / 1000, dt.timezone.utc).year == year]
        ending = segment[-1]
        without_segment = [dict(time=row["time"], equity=initial + sum(v for s, v in row["symbol_net"].items() if s != "ETHUSDT")) for row in segment]
        years[str(year)] = dict(start_ms=prior["time"], end_ms=ending["time"],
                               net_return=ending["equity"] / prior["equity"] - 1,
                               account_close_drawdown=curve_metrics(segment, prior["equity"], prior["time"])["max_close_drawdown"],
                               symbol_net={symbol: ending["symbol_net"][symbol] - prior["symbol_net"][symbol] for symbol in symbols},
                               symbol_fees={symbol: ending["fees"][symbol] - prior["fees"][symbol] for symbol in symbols},
                               symbol_funding={symbol: ending["funding"][symbol] - prior["funding"][symbol] for symbol in symbols},
                               without_ETH_accounting=curve_metrics(without_segment, prior_without, prior["time"]))
        prior = ending
        prior_without = without_segment[-1]["equity"]
    positive_account = total > 0
    dependence = ("PROFIT_REQUIRES_ETH_OFFSETTING_NONPOSITIVE_BTC"
                  if positive_account and final_net.get("BTCUSDT", 0.0) <= 0
                  else "BOTH_SYMBOLS_POSITIVE" if all(v > 0 for v in final_net.values())
                  else "ACCOUNT_NOT_PROFITABLE" if not positive_account
                  else "POSITIVE_ACCOUNT_WITH_OTHER_SIGN_CONFIGURATION")
    return dict(path=path, without_ETH_accounting_curve=without_eth,
                full=metrics, without_ETH_full=without_metrics,
                full_symbol_components=full_components, year_segments=years,
                symbol_net=final_net, peak_to_trough_symbol_delta=dd_deltas,
                max_equity_reconciliation_error=max_error,
                max_quantity_error=max_quantity_error,
                drawdown_attribution_error=dd_error,
                exposure=exposure,
                dependence_classification=dependence,
                eth_shares=dict(signed_ETH_net_share=eth / total if total != 0 else None,
                                absolute_ETH_net_contribution_share=abs(eth) / absolute if absolute else None,
                                positive_profit_share=max(eth, 0) / positive if positive else None),
                scope="Same executed path accounting subtraction only; not a tradable BTC-only strategy")
