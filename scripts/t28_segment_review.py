"""Describe frozen T28 saved accounts by year and causal market phase; no replay."""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import gzip
import hashlib
import importlib.util
import json
import math
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAY, STEP = 86_400_000, 900_000
DEFAULT_PROTOCOL = ROOT / "research_projects/t28_segment_review_20261025/protocol.json"
ATTRIBUTOR = ROOT / "reports/t28_independent_20261025/cross_period/audit/independent_attribution.py"
SYMBOLS = ("BTCUSDT", "ETHUSDT")


def read(path):
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def stamp(value):
    return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def build_phase_labels(close_ends, start, end):
    """A close keyed D is the completed daily close ending just before D."""
    result = []
    for day in range(start, end, DAY):
        required = [day - offset * DAY for offset in range(61)]
        if any(key not in close_ends for key in required):
            raise ValueError(f"missing completed daily close at {day}")
        latest, prior = Decimal(str(close_ends[day])), Decimal(str(close_ends[day - 60 * DAY]))
        if not latest.is_finite() or not prior.is_finite() or min(latest, prior) <= 0:
            raise ValueError("daily close must be finite and positive")
        phase = "up" if latest > prior * Decimal("1.10") else "down" if latest < prior * Decimal("0.90") else "sideways"
        result.append(dict(day_start=day, day_end=day + DAY, phase=phase,
                           btc_60d_return=float(latest / prior - 1),
                           available_close_end=day, lookback_close_end=day - 60 * DAY))
    return result


def _index(path):
    if isinstance(path, dict) and "rows" in path:
        return path
    times = [row["time"] for row in path]
    if not times or any(left >= right for left, right in zip(times, times[1:])):
        raise ValueError("marked path must be strictly ordered")
    peak, historical = path[0]["equity"], []
    for row in path:
        peak = max(peak, row["equity"])
        historical.append(1 - row["equity"] / peak)
    return dict(rows=path, times=times, historical=historical)


def segment_metrics(path, start, end):
    indexed = _index(path)
    times, rows = indexed["times"], indexed["rows"]
    left, right = bisect.bisect_left(times, start), bisect.bisect_left(times, end)
    if left >= len(times) or right >= len(times) or times[left] != start or times[right] != end or right <= left:
        raise ValueError("segment requires exact marked start and end boundaries")
    first, last = rows[left], rows[right]
    initial, peak, worst = first["equity"], first["equity"], 0.0
    peak_time = dd_peak_time = dd_trough_time = start
    for row in rows[left:right + 1]:
        if row["equity"] > peak:
            peak, peak_time = row["equity"], row["time"]
        drawdown = 1 - row["equity"] / peak
        if drawdown > worst:
            worst, dd_peak_time, dd_trough_time = drawdown, peak_time, row["time"]
    net = {symbol: last["symbol_net"][symbol] - first["symbol_net"][symbol] for symbol in SYMBOLS}
    fees = {symbol: last["fees"][symbol] - first["fees"][symbol] for symbol in SYMBOLS}
    funding = {symbol: last["funding"][symbol] - first["funding"][symbol] for symbol in SYMBOLS}
    total, absolute = math.fsum(net.values()), math.fsum(abs(value) for value in net.values())
    without = [dict(time=row["time"], equity=row["equity"] - row["symbol_net"]["ETHUSDT"]) for row in rows[left:right + 1]]
    without_peak, without_dd = without[0]["equity"], 0.0
    for row in without:
        without_peak = max(without_peak, row["equity"])
        without_dd = max(without_dd, 1 - row["equity"] / without_peak)
    return dict(start_ms=start, end_ms=end, start_equity=initial, end_equity=last["equity"],
                net_return=last["equity"] / initial - 1, net_pnl_usdt=last["equity"] - initial,
                local_max_close_drawdown=worst, local_dd_peak_time=dd_peak_time, local_dd_trough_time=dd_trough_time,
                historical_highwater_max_drawdown_in_segment=max(indexed["historical"][left:right + 1]),
                symbol_net_pnl=net, symbol_fees=fees, symbol_funding=funding,
                symbol_marked_gross_pnl_before_fees_funding={symbol: net[symbol] + fees[symbol] - funding[symbol] for symbol in SYMBOLS},
                opening_marked_notional=first.get("marked_notional"), closing_marked_notional=last.get("marked_notional"),
                fees_usdt=math.fsum(fees.values()), funding_usdt=math.fsum(funding.values()),
                ETH_net_profit_share=net["ETHUSDT"] / total if total > 0 else None,
                ETH_absolute_net_contribution_share=abs(net["ETHUSDT"]) / absolute if absolute else None,
                signed_ETH_net_contribution_share=net["ETHUSDT"] / total if total != 0 else None,
                dependence_classification="ETH_DEPENDENT_OFFSETTING_NONPOSITIVE_BTC" if total > 0 and net["BTCUSDT"] <= 0 else "BOTH_SYMBOLS_POSITIVE" if all(value > 0 for value in net.values()) else "ACCOUNT_NOT_PROFITABLE" if total <= 0 else "OTHER_SIGN_CONFIGURATION",
                same_path_without_ETH_accounting=dict(start_equity=without[0]["equity"], end_equity=without[-1]["equity"],
                    net_return=without[-1]["equity"] / without[0]["equity"] - 1,
                    local_max_close_drawdown=without_dd, scope="Accounting subtraction on original executed path; no BTC-only replay"),
                reconciliation_error=abs(total - (last["equity"] - initial)))


def _pair(own, hold, start, end):
    left, right = segment_metrics(own, start, end), segment_metrics(hold, start, end)
    ratio = (1 + left["net_return"]) / (1 + right["net_return"])
    return dict(start_ms=start, end_ms=end, T28=left, hold=right,
                paired_relative_return=ratio - 1,
                net_return_difference=left["net_return"] - right["net_return"],
                paired_log_growth_difference=math.log(ratio))


def phase_metrics(labels, own, hold):
    own, hold = _index(own), _index(hold)
    daily = [dict(**_pair(own, hold, row["day_start"], row["day_end"]), phase=row["phase"]) for row in labels]
    episodes = []
    for row in labels:
        if episodes and episodes[-1]["phase"] == row["phase"] and episodes[-1]["end_ms"] == row["day_start"]:
            episodes[-1]["end_ms"] = row["day_end"]
        else:
            episodes.append(dict(phase=row["phase"], start_ms=row["day_start"], end_ms=row["day_end"]))
    episodes = [dict(**_pair(own, hold, row["start_ms"], row["end_ms"]), phase=row["phase"],
                     days=(row["end_ms"] - row["start_ms"]) // DAY) for row in episodes]
    summary = {}
    for phase in ("up", "down", "sideways"):
        days, spells = [row for row in daily if row["phase"] == phase], [row for row in episodes if row["phase"] == phase]
        item = dict(active_days=len(days), contiguous_episodes=len(spells), scope="CONDITIONAL_DESCRIPTIVE_NOT_A_STITCHED_ACCOUNT",
                    T28_conditional_growth=math.expm1(math.fsum(math.log1p(row["T28"]["net_return"]) for row in days)) if days else None,
                    hold_conditional_growth=math.expm1(math.fsum(math.log1p(row["hold"]["net_return"]) for row in days)) if days else None,
                    sum_paired_log_growth_difference=math.fsum(row["paired_log_growth_difference"] for row in days) if days else None,
                    conditional_relative_growth=math.expm1(math.fsum(row["paired_log_growth_difference"] for row in days)) if days else None,
                    noncontiguous_phase_drawdown=None,
                    worst_contiguous_episode_T28_drawdown=max((row["T28"]["local_max_close_drawdown"] for row in spells), default=None),
                    worst_contiguous_episode_hold_drawdown=max((row["hold"]["local_max_close_drawdown"] for row in spells), default=None))
        for key in ("T28", "hold"):
            net = {symbol: math.fsum(row[key]["symbol_net_pnl"][symbol] for row in days) for symbol in SYMBOLS}
            total, absolute = math.fsum(net.values()), math.fsum(abs(value) for value in net.values())
            item[key] = dict(symbol_net_pnl=net if days else None,
                fees_usdt=math.fsum(row[key]["fees_usdt"] for row in days) if days else None,
                funding_usdt=math.fsum(row[key]["funding_usdt"] for row in days) if days else None,
                ETH_net_profit_share=net["ETHUSDT"] / total if days and total > 0 else None,
                ETH_absolute_net_contribution_share=abs(net["ETHUSDT"]) / absolute if days and absolute else None)
        summary[phase] = item
    return dict(phase_summary=summary, episodes=episodes, daily_pairs=daily)


def _verify(bindings):
    for filename, expected in bindings.items():
        if sha(filename) != expected:
            raise ValueError(f"source hash drift: {filename}")


def _raw_path(manifest_path, symbol):
    manifest = read(manifest_path)
    item = manifest["symbols"][symbol]["raw15m"]
    path = Path(manifest_path).parent / item["path"]
    if sha(path) != item["sha256"]:
        raise ValueError(f"raw price hash drift: {path}")
    return path


def _daily_closes(raw):
    by_day = {}
    for row in raw:
        by_day.setdefault(int(row[0]) // DAY * DAY, []).append(row)
    result = {}
    for day, rows in by_day.items():
        if [int(row[0]) for row in rows] != list(range(day, day + DAY, STEP)):
            raise ValueError(f"incomplete daily close price grid at {day}")
        result[day + DAY] = rows[-1][4]
    return result


def _saved_attribution(dataset, strategy, cost_id):
    # Cross-period independent attribution was saved before this new grouping.
    path = Path(dataset["account_directory"]).parent.parent / "audit" / "attribution" / f"{strategy}__{cost_id}.json.gz"
    return path if path.exists() else None


def prepare(protocol_path, output):
    """Freeze labels and bytes only: never deserialize account/PnL files here."""
    protocol_path, output = Path(protocol_path).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError(output)
    protocol = read(protocol_path)
    rule = protocol["phase_rule"]
    if protocol["scope"] != "DESCRIPTIVE_ALREADY_EXPOSED_FIXED_ACCOUNT_ATTRIBUTION" or protocol["strategy_replays_allowed"] is not False:
        raise ValueError("incorrect saved-account attribution protocol scope")
    if (rule["lookback_days"], rule["upper_return"], rule["lower_return"]) != (60, "0.10", "-0.10"):
        raise ValueError("phase definition differs from frozen descriptive rule")
    bindings = dict(protocol["source_bindings"])
    bindings[str(protocol_path)] = sha(protocol_path)
    for path in (Path(__file__).resolve(), ROOT / "tests/test_t28_segment_review.py", ATTRIBUTOR):
        bindings[str(path)] = sha(path)
    _verify(bindings)
    labels = {}
    for dataset in protocol["datasets"]:
        start, end = stamp(dataset["start"]), stamp(dataset["end"])
        if start % DAY or end % DAY or start >= end:
            raise ValueError("dataset requires ordered whole UTC day boundaries")
        raw_path = _raw_path(dataset["input_manifest"], "BTCUSDT")
        bindings[str(raw_path)] = sha(raw_path)
        labels[dataset["id"]] = build_phase_labels(_daily_closes(read(raw_path)), start, end)
        for cost in protocol["costs"]:
            for strategy in protocol["strategies"]:
                cost_id = dataset["cost_mapping"][cost]
                account = Path(dataset["account_directory"]) / f"{strategy}__{cost_id}.json.gz"
                if str(account) not in bindings:
                    raise ValueError(f"account is not protocol-bound: {account}")
                attribution = _saved_attribution(dataset, strategy, cost_id)
                if attribution:
                    bindings[str(attribution)] = sha(attribution)
    _verify(bindings)
    save(output / "phase_labels.json", labels)
    result = dict(status="LABELS_FROZEN_BEFORE_GROUPED_PNL", protocol_path=str(protocol_path),
                  protocol_sha256=sha(protocol_path), source_sha256=bindings,
                  labels_sha256=sha(output / "phase_labels.json"),
                  label_counts={key: {phase: sum(row["phase"] == phase for row in rows) for phase in ("up", "down", "sideways")} for key, rows in labels.items()},
                  strategy_replayed=False, grouped_pnl_computed=False)
    save(output / "pre_pnl_manifest.json", result)
    return result


def _load_curve(dataset, strategy, cost_id, prices, helper):
    account_path = Path(dataset["account_directory"]) / f"{strategy}__{cost_id}.json.gz"
    saved = _saved_attribution(dataset, strategy, cost_id)
    if saved:
        attribution = read(saved)
        initial = attribution["path"][0]["equity"] - math.fsum(attribution["path"][0]["symbol_net"].values())
    else:
        account = read(account_path)
        initial = account["initial_equity"]
        attribution = helper.rebuild_attribution(account, prices)
    if attribution["max_equity_reconciliation_error"] > 1e-6:
        raise ValueError(f"saved-ledger equity reconciliation failed: {account_path}")
    zero = dict.fromkeys(SYMBOLS, 0.0)
    path = [dict(time=stamp(dataset["start"]), equity=initial, symbol_net=zero,
                 fees=zero, funding=zero, marked_notional=zero)] + attribution["path"]
    if path[-1]["time"] != stamp(dataset["end"]):
        raise ValueError("saved account does not cover dataset end")
    return _index(path)


def _years(start, end):
    boundaries = [start]
    year = dt.datetime.fromtimestamp(start / 1000, dt.timezone.utc).year
    while True:
        boundary = int(dt.datetime(year + 1, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
        if boundary >= end:
            break
        boundaries.append(boundary)
        year += 1
    boundaries.append(end)
    return [(dt.datetime.fromtimestamp(left / 1000, dt.timezone.utc).year, left, right)
            for left, right in zip(boundaries, boundaries[1:])]


def execute(prepared, output):
    """Read saved marks/fills, attribute them, and group. Never execute strategy."""
    prepared, output = Path(prepared).resolve(), Path(output).resolve()
    manifest = read(prepared / "pre_pnl_manifest.json")
    _verify(manifest["source_sha256"])
    if sha(prepared / "phase_labels.json") != manifest["labels_sha256"]:
        raise ValueError("phase labels hash drift")
    if output.exists():
        raise FileExistsError(output)
    protocol = read(manifest["protocol_path"])
    labels = read(prepared / "phase_labels.json")
    spec = importlib.util.spec_from_file_location("frozen_cashflow_attributor", ATTRIBUTOR)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    summaries = []
    for dataset in protocol["datasets"]:
        start, end = stamp(dataset["start"]), stamp(dataset["end"])
        prices = {symbol: {int(row[0]): dict(close=float(row[4])) for row in read(_raw_path(dataset["input_manifest"], symbol))} for symbol in SYMBOLS}
        for cost in protocol["costs"]:
            cost_id = dataset["cost_mapping"][cost]
            own = _load_curve(dataset, "T28_long_cash", cost_id, prices, helper)
            hold = _load_curve(dataset, "vol_scaled_hold", cost_id, prices, helper)
            phases = phase_metrics(labels[dataset["id"]], own, hold)
            years = [dict(**_pair(own, hold, left, right), year=year,
                          partial_year=left != stamp(f"{year}-01-01T00:00:00Z") or right != stamp(f"{year + 1}-01-01T00:00:00Z")) for year, left, right in _years(start, end)]
            full = _pair(own, hold, start, end)
            day_net = math.fsum(row["T28"]["net_pnl_usdt"] for row in phases["daily_pairs"])
            if abs(day_net - full["T28"]["net_pnl_usdt"]) > 1e-6:
                raise ValueError("daily segment attribution does not reconcile to full account")
            case = dict(dataset=dataset["id"], cost=cost, original_cost_id=cost_id, scope=dataset.get("scope"),
                        full=full, year_pairs=years, phase_summary=phases["phase_summary"],
                        dataset_totals_reconciliation=dict(T28_day_net_error=abs(day_net - full["T28"]["net_pnl_usdt"]),
                            hold_day_net_error=abs(math.fsum(row["hold"]["net_pnl_usdt"] for row in phases["daily_pairs"]) - full["hold"]["net_pnl_usdt"])))
            name = f"{dataset['id']}__{cost}.json"
            save(output / "pairs" / name, case)
            save(output / "daily" / name, phases["daily_pairs"])
            save(output / "episodes" / name, phases["episodes"])
            summaries.append(case)
            print(json.dumps(dict(dataset=dataset["id"], cost=cost, status="SAVED_ACCOUNT_ATTRIBUTION_COMPLETE")), flush=True)
            del own, hold, phases
    _verify(manifest["source_sha256"])
    result = dict(status="DESCRIPTIVE_SAVED_ACCOUNT_ATTRIBUTION", protocol_sha256=manifest["protocol_sha256"],
                  prepared_manifest_sha256=sha(prepared / "pre_pnl_manifest.json"), cases=summaries,
                  strategy_replayed=False, strict_oos=False, promotion=False,
                  interpretation="Previously exposed historical accounts. Phase products are conditional descriptions; no stitched phase drawdown or new strategy filter.")
    save(output / "summary.json", result)
    save(output / "run_manifest.json", dict(source_sha256=manifest["source_sha256"],
         artifact_sha256={str(path.relative_to(output)): sha(path) for path in sorted(output.rglob("*.json"))}))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "execute"))
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--prepared", type=Path, default=ROOT / "reports/t28_segment_review_20261025/prepared")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        print(json.dumps(prepare(args.protocol, args.output or args.prepared), ensure_ascii=False))
    else:
        execute(args.prepared, args.output or ROOT / "reports/t28_segment_review_20261025/run")


if __name__ == "__main__":
    main()
