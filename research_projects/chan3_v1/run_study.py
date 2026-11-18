"""One-shot CHAN3_V1 study.

The script refuses to run unless FREEZE.json matches the current protocol,
implementation and data manifest, and refuses to run twice.  It computes all
results in memory, then writes the report and artifacts in one final step.

Run from the repository root:
    .venv/bin/python research_projects/chan3_v1/run_study.py
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

from chan3 import (  # noqa: E402
    Bars,
    Signal,
    Trade,
    baseline_returns,
    block_bootstrap_difference,
    block_bootstrap_mean,
    build_strokes_and_signals,
    find_fractals,
    signal_entry_status,
    simulate_trade,
    week_key,
    wilder_atr,
)

PROTOCOL = HERE / "protocol.json"
DATA = HERE / "data"
RESULTS = HERE / "results"
FREEZE = HERE / "FREEZE.json"
FEE = 0.001
SLIP = 0.0002
HORIZON = 96
MIN_TRADES = 30


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def load_bars(symbol: str) -> Bars:
    path = DATA / f"{symbol}_15m.csv"
    with path.open(encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise RuntimeError(f"empty bars: {path}")
    bars = Bars(
        open_time=[int(row["open_time"]) for row in rows],
        open=[float(row["open"]) for row in rows],
        high=[float(row["high"]) for row in rows],
        low=[float(row["low"]) for row in rows],
        close=[float(row["close"]) for row in rows],
        close_time=[int(row["close_time"]) for row in rows],
    )
    if any(b <= a for a, b in zip(bars.open_time, bars.open_time[1:])):
        raise RuntimeError(f"bars not strictly ordered: {symbol}")
    for a, b in zip(bars.open_time, bars.open_time[1:]):
        if b - a != 900_000:
            raise RuntimeError(f"bar gap in frozen data: {symbol} {a} -> {b}")
    return bars


def load_funding(symbol: str) -> tuple[list[int], list[float]]:
    path = DATA / f"{symbol}_funding.csv"
    with path.open(encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    times = [int(row["timestamp"]) for row in rows]
    rates = [float(row["funding_rate"]) for row in rows]
    if not times or any(b <= a for a, b in zip(times, times[1:])):
        raise RuntimeError(f"funding not strictly ordered: {symbol}")
    return times, rates


def pct(value: float | None) -> float | None:
    return None if value is None or not math.isfinite(value) else value * 100.0


def stats(nets: list[float]) -> dict:
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    mean = sum(nets) / len(nets) if nets else float("nan")
    return {
        "trades": len(nets),
        "mean_net_return_pct": pct(mean),
        "median_net_return_pct": pct(statistics.median(nets)) if nets else None,
        "stdev_net_return_pct": pct(statistics.stdev(nets)) if len(nets) > 1 else None,
        "win_rate_pct": 100.0 * len(wins) / len(nets) if nets else None,
        "avg_win_pct": pct(gross_win / len(wins)) if wins else None,
        "avg_loss_pct": pct(-gross_loss / len(losses)) if losses else None,
        "payoff_ratio": (gross_win / len(wins)) / (gross_loss / len(losses)) if wins and losses else None,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else (999.0 if gross_win > 0 else None),
        "best_trade_pct": pct(max(nets)) if nets else None,
        "worst_trade_pct": pct(min(nets)) if nets else None,
    }


def simple_equity(trades: list[Trade]) -> tuple[list[dict], float]:
    """Fixed-risk, non-compounded, trade-close equity (not a live portfolio)."""
    equity = 10_000.0
    peak = equity
    max_dd = 0.0
    rows: list[dict] = []
    for trade in sorted((t for t in trades if not t.excluded), key=lambda t: t.exit_time):
        risk_usdt = 10_000.0 * 0.005
        if trade.entry <= trade.stop:
            continue
        qty = risk_usdt / (trade.entry - trade.stop)
        notional = qty * trade.entry
        if notional > 10_000.0:
            qty = 10_000.0 / trade.entry
            notional = 10_000.0
        pnl = notional * trade.net_pct
        equity += pnl
        peak = max(peak, equity)
        drawdown = equity / peak - 1.0
        max_dd = min(max_dd, drawdown)
        rows.append(
            {
                "exit_time": trade.exit_time,
                "exit_utc": utc_iso(trade.exit_time),
                "symbol": trade.symbol,
                "notional": notional,
                "pnl_usdt": pnl,
                "equity": equity,
                "drawdown_pct": drawdown * 100.0,
            }
        )
    return rows, abs(max_dd) * 100.0


def verify_freeze() -> dict:
    if not FREEZE.exists():
        raise SystemExit("FREEZE.json missing; freeze the protocol before running")
    if (RESULTS / "summary.json").exists():
        raise SystemExit("results/summary.json already exists; this is a one-shot study")
    expected = json.loads(FREEZE.read_text(encoding="utf-8"))
    targets = {
        "protocol.json": PROTOCOL,
        "chan3.py": HERE / "chan3.py",
        "test_chan3.py": HERE / "test_chan3.py",
        "prepare_data.py": HERE / "prepare_data.py",
        "run_study.py": HERE / "run_study.py",
        "data_manifest.json": HERE / "data_manifest.json",
    }
    for name, path in targets.items():
        actual = sha256(path)
        if expected.get("hashes", {}).get(name) != actual:
            raise SystemExit(f"freeze hash mismatch: {name}")
    return expected


def format_pct(value: float | None, digits: int = 3) -> str:
    # All *_pct values in this study are already percentage points.
    return "—" if value is None or not math.isfinite(value) else f"{value:+.{digits}f}%"


def format_num(value: float | None, digits: int = 2) -> str:
    return "—" if value is None or not math.isfinite(value) else f"{value:.{digits}f}"


def main() -> int:
    freeze = verify_freeze()
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    if protocol.get("status") != "FROZEN_BEFORE_RESULTS" or not protocol.get("one_shot"):
        raise SystemExit("protocol is not a frozen one-shot protocol")
    data_manifest = json.loads((HERE / "data_manifest.json").read_text(encoding="utf-8"))

    all_signals: list[dict] = []
    all_trades: list[Trade] = []
    baseline_by_week: dict[tuple[int, int], list[float]] = {}
    baseline_by_year: dict[int, list[float]] = {}
    baseline_by_symbol: dict[str, list[float]] = {}
    signal_rows: list[dict] = []
    trade_rows: list[dict] = []

    for symbol in protocol["scope"]["symbols"]:
        bars = load_bars(symbol)
        funding_times, funding_rates = load_funding(symbol)
        atr = wilder_atr(bars.high, bars.low, bars.close, period=14)
        fractals = find_fractals(bars.high, bars.low, wing=2)
        strokes, signals = build_strokes_and_signals(fractals, atr, symbol=symbol)
        print(
            f"[run] {symbol}: {len(bars)} bars, {len(fractals)} fractals, "
            f"{len(strokes)} strokes, {len(signals)} signals",
            flush=True,
        )
        for signal in signals:
            entry_raw = float(bars.open[signal.entry_index]) if signal.entry_index < len(bars) else None
            entry_with_slip = entry_raw * (1.0 + SLIP) if entry_raw is not None else None
            status = signal_entry_status(signal, bars, horizon_bars=HORIZON, slippage=SLIP)
            signal_rows.append(
                {
                    "symbol": symbol,
                    "signal_index": signal.signal_index,
                    "signal_utc": utc_iso(bars.close_time[signal.signal_index]),
                    "entry_index": signal.entry_index,
                    "entry_utc": utc_iso(bars.open_time[signal.entry_index])
                    if signal.entry_index < len(bars)
                    else "",
                    "zone_zg": signal.zone_zg,
                    "zone_zd": signal.zone_zd,
                    "departure_high": signal.departure_high,
                    "pullback_low": signal.pullback_low,
                    "stop": signal.stop,
                    "atr": signal.atr,
                    "entry_raw": entry_raw,
                    "entry_with_slippage": entry_with_slip,
                    "status": status,
                }
            )
            all_signals.append(
                {
                    "symbol": symbol,
                    "signal_index": signal.signal_index,
                    "signal_utc": utc_iso(bars.close_time[signal.signal_index]),
                    "status": status,
                }
            )
            if status != "candidate":
                continue
            trade = simulate_trade(
                bars,
                signal,
                funding_times,
                funding_rates,
                fee_rate=FEE,
                slippage=SLIP,
                max_hold_bars=HORIZON,
            )
            if trade is None:
                continue
            all_trades.append(trade)
            trade_rows.append(
                {
                    "symbol": trade.symbol,
                    "signal_utc": utc_iso(bars.close_time[trade.signal_index]),
                    "entry_utc": utc_iso(trade.entry_time),
                    "exit_utc": utc_iso(trade.exit_time),
                    "entry": trade.entry,
                    "stop": trade.stop,
                    "target": trade.target,
                    "exit": trade.exit,
                    "reason": trade.reason,
                    "gross_pct": trade.gross_pct * 100.0,
                    "fee_pct": trade.fee_pct * 100.0,
                    "funding_pct": trade.funding_pct * 100.0,
                    "net_pct": trade.net_pct * 100.0,
                    "risk_pct": trade.risk_pct * 100.0,
                    "r_multiple": trade.r_multiple,
                    "funding_missing": trade.funding_missing,
                    "excluded": trade.excluded,
                }
            )

        baseline = baseline_returns(
            bars,
            funding_times,
            funding_rates,
            horizon_bars=HORIZON,
            fee_rate=FEE,
            slippage=SLIP,
        )
        for row in baseline:
            if row["funding_missing"]:
                continue
            value = float(row["net_pct"])
            week = week_key(int(row["entry_time"]))
            baseline_by_week.setdefault(week, []).append(value)
            year = datetime.fromtimestamp(int(row["entry_time"]) / 1000, tz=timezone.utc).year
            baseline_by_year.setdefault(year, []).append(value)
            baseline_by_symbol.setdefault(symbol, []).append(value)

    included = [t for t in all_trades if not t.excluded]
    excluded = [t for t in all_trades if t.excluded]
    nets = [t.net_pct for t in included]
    primary = stats(nets)
    primary["gross_mean_pct"] = pct(sum(t.gross_pct for t in included) / len(included)) if included else None
    primary["fee_mean_pct"] = pct(sum(t.fee_pct for t in included) / len(included)) if included else None
    primary["funding_mean_pct"] = pct(sum(t.funding_pct for t in included) / len(included)) if included else None
    primary["mean_r_multiple"] = (
        sum(t.r_multiple for t in included) / len(included) if included else None
    )
    primary["excluded_missing_funding"] = len(excluded)
    primary["candidate_signals"] = len(signal_rows)
    primary["skipped_signals"] = sum(1 for row in signal_rows if row["status"] != "candidate")

    signal_by_week: dict[tuple[int, int], list[float]] = {}
    for trade in included:
        signal_by_week.setdefault(week_key(trade.entry_time), []).append(trade.net_pct)
    baseline_week_means = {
        key: [sum(values) / len(values)] for key, values in baseline_by_week.items() if values
    }
    mean_ci = block_bootstrap_mean(
        signal_by_week,
        iterations=protocol["bootstrap"]["iterations"],
        seed=protocol["bootstrap"]["seed"],
    )
    baseline_ci = block_bootstrap_mean(
        baseline_week_means,
        iterations=protocol["bootstrap"]["iterations"],
        seed=protocol["bootstrap"]["seed"],
    )
    diff_ci = block_bootstrap_difference(
        signal_by_week,
        baseline_week_means,
        iterations=protocol["bootstrap"]["iterations"],
        seed=protocol["bootstrap"]["seed"],
    )

    mean_ci_pct = {key: value * 100.0 for key, value in mean_ci.items()}
    baseline_ci_pct = {key: value * 100.0 for key, value in baseline_ci.items()}
    diff_ci_pct = {key: value * 100.0 for key, value in diff_ci.items()}

    baseline_all = [value for values in baseline_by_week.values() for value in values]
    baseline_stats = stats(baseline_all)
    baseline_stats["symbols"] = {
        symbol: stats(values) for symbol, values in baseline_by_symbol.items()
    }
    baseline_stats["years"] = {
        str(year): stats(values) for year, values in sorted(baseline_by_year.items())
    }

    yearly: dict[int, list[float]] = {}
    yearly_all: dict[int, list[Trade]] = {}
    for trade in included:
        year = datetime.fromtimestamp(trade.entry_time / 1000, tz=timezone.utc).year
        yearly.setdefault(year, []).append(trade.net_pct)
        yearly_all.setdefault(year, []).append(trade)
    yearly_stats = {str(year): stats(values) for year, values in sorted(yearly.items())}

    def grouped(key) -> dict[str, dict]:
        groups: dict[str, list[float]] = {}
        for trade in included:
            groups.setdefault(key(trade), []).append(trade.net_pct)
        return {name: stats(values) for name, values in sorted(groups.items())}

    by_symbol = grouped(lambda t: t.symbol)
    by_reason = grouped(lambda t: t.reason)

    equity_rows, max_dd = simple_equity(included)
    conditions = {
        "trades_ge_30": primary["trades"] >= MIN_TRADES,
        "mean_net_positive": (primary["mean_net_return_pct"] or 0) > 0,
        "mean_ci_low_positive": mean_ci["low"] > 0,
        "excess_over_baseline_positive": diff_ci["mean"] > 0,
        "excess_ci_low_positive": diff_ci["low"] > 0,
        "profit_factor_gt_1": (primary["profit_factor"] or 0) > 1,
    }
    if all(conditions.values()):
        verdict = "PASS_TO_NEXT_STAGE"
    elif primary["trades"] < MIN_TRADES:
        verdict = "INCONCLUSIVE"
    elif (primary["mean_net_return_pct"] or 0) <= 0 or (primary["profit_factor"] or 0) <= 1:
        verdict = "REJECT"
    else:
        verdict = "INCONCLUSIVE"

    summary = {
        "project": "CHAN3_V1",
        "verdict": verdict,
        "verdict_conditions": conditions,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "data": {
            "start_utc": protocol["data"]["start_utc"],
            "end_exclusive_utc": protocol["data"]["end_exclusive_utc"],
            "symbols": protocol["scope"]["symbols"],
            "data_manifest_sha256": sha256(HERE / "data_manifest.json"),
            "coverage": {
                symbol: data_manifest["symbols"][symbol]["bars"] for symbol in protocol["scope"]["symbols"]
            },
        },
        "primary": primary,
        "bootstrap": {
            "signal_mean_pct": mean_ci_pct,
            "baseline_mean_pct": baseline_ci_pct,
            "signal_minus_baseline_pct": diff_ci_pct,
            "unit": protocol["bootstrap"]["unit"],
            "iterations": protocol["bootstrap"]["iterations"],
            "seed": protocol["bootstrap"]["seed"],
        },
        "baseline": baseline_stats,
        "yearly": yearly_stats,
        "by_symbol": by_symbol,
        "by_exit_reason": by_reason,
        "simple_equity": {
            "initial_equity_usdt": 10_000.0,
            "risk_per_trade_pct": 0.5,
            "max_drawdown_pct": max_dd,
            "note": "fixed-risk, non-compounded, trade-close equity; not a mark-to-market portfolio",
        },
        "signals": {
            "candidate": len(signal_rows),
            "skipped": primary["skipped_signals"],
            "excluded_missing_funding": len(excluded),
        },
        "freeze": {
            "created_at_utc": freeze.get("created_at_utc"),
            "hashes": freeze.get("hashes"),
            "git_commit": freeze.get("git_commit"),
        },
        "runtime": {
            "python": sys.version,
            "platform": platform.platform(),
        },
    }

    # Build the human report before writing anything.
    failed = [name for name, ok in conditions.items() if not ok]
    lines = [
        "# CHAN3_V1：15m 三买单次检验结果",
        "",
        f"**结论：{verdict}。**",
        "",
        "这是规则、数据边界和代码哈希预先冻结后的单次检验；没有在看到结果后追加过滤、改参数或换区间。",
        "",
        "## 一句话假设",
        "",
        "BTCUSDT / ETHUSDT 15m 永续中，因果三买（向上离开有效中枢后，回抽低点仍严格高于 ZG）"
        "在扣除 0.1%/边手续费、2bps/边滑点和真实资金费后，具有正期望，并优于同品种同区间的无条件 24 小时多头。",
        "",
        "## 数据",
        "",
        f"- 区间：{protocol['data']['start_utc']} 至 {protocol['data']['end_exclusive_utc']}（右端不含）",
        f"- 品种：{', '.join(protocol['scope']['symbols'])}；周期：15m；只做多。",
        "- 来源：Binance 官方 USD-M 月度归档，逐文件 CHECKSUM；详见 `DATA_AUDIT.md`。",
        "- 该窗口在 2023-09-30 结束，早于仓库内可检索到的 2023-10 之后使用记录。",
        "",
        "## 样本",
        "",
        f"- 候选三买信号：**{len(signal_rows)}**",
        f"- 可执行并完成 96 根窗口的交易：**{primary['trades']}**",
        f"- 因资金费缺失排除：**{len(excluded)}**",
        f"- 因无前向窗口/入场越止损/回到中枢跳过：**{primary['skipped_signals']}**",
        "",
        "## 主结果",
        "",
        "| 指标 | 三买 | 无条件 24h 多头基线 |",
        "|---|---:|---:|",
        f"| 交易/观测数 | {primary['trades']} | {baseline_stats['trades']} |",
        f"| 平均每笔净收益 | {format_pct(primary['mean_net_return_pct'])} | {format_pct(baseline_stats['mean_net_return_pct'])} |",
        f"| 中位数净收益 | {format_pct(primary['median_net_return_pct'])} | {format_pct(baseline_stats['median_net_return_pct'])} |",
        f"| 净胜率 | {format_num(primary['win_rate_pct'])}% | {format_num(baseline_stats['win_rate_pct'])}% |",
        f"| 平均盈利 / 平均亏损 | {format_pct(primary['avg_win_pct'])} / {format_pct(primary['avg_loss_pct'])} | {format_pct(baseline_stats['avg_win_pct'])} / {format_pct(baseline_stats['avg_loss_pct'])} |",
        f"| 实际盈亏比 | {format_num(primary['payoff_ratio'])} | {format_num(baseline_stats['payoff_ratio'])} |",
        f"| 利润因子 | {format_num(primary['profit_factor'])} | {format_num(baseline_stats['profit_factor'])} |",
        f"| 最差单笔 | {format_pct(primary['worst_trade_pct'])} | {format_pct(baseline_stats['worst_trade_pct'])} |",
        "",
        "### 置信区间（按入场 UTC 周分块，2000 次）",
        "",
        "| 统计量 | 点估计 | 95% 下限 | 95% 上限 |",
        "|---|---:|---:|---:|",
        f"| 三买平均净收益 | {format_pct(mean_ci_pct['mean'])} | {format_pct(mean_ci_pct['low'])} | {format_pct(mean_ci_pct['high'])} |",
        f"| 基线平均净收益 | {format_pct(baseline_ci_pct['mean'])} | {format_pct(baseline_ci_pct['low'])} | {format_pct(baseline_ci_pct['high'])} |",
        f"| 三买减基线 | {format_pct(diff_ci_pct['mean'])} | {format_pct(diff_ci_pct['low'])} | {format_pct(diff_ci_pct['high'])} |",
        "",
        "## 通过条件",
        "",
        "| 条件 | 结果 |",
        "|---|---|",
    ]
    for name, ok in conditions.items():
        lines.append(f"| {name} | {'PASS' if ok else 'FAIL'} |")
    lines += [
        "",
        f"未满足：{', '.join(failed) if failed else '无'}。",
        "",
        "## 成本与退出",
        "",
        f"- 平均毛收益（含滑点）：{format_pct(primary['gross_mean_pct'])}",
        f"- 平均手续费：{format_pct(primary['fee_mean_pct'])}",
        f"- 平均资金费：{format_pct(primary['funding_mean_pct'])}",
        f"- 平均净收益：{format_pct(primary['mean_net_return_pct'])}",
        f"- 平均 R 倍数：{format_num(primary['mean_r_multiple'], 3)}",
        f"- 简单逐笔权益最大回撤（固定 0.5% 风险、非复利、非盘中盯市）：**{format_num(max_dd)}%**",
        "",
        "### 退出原因",
        "",
        "| 原因 | 笔数 | 平均净收益 | 净胜率 |",
        "|---|---:|---:|---:|",
    ]
    for reason, item in by_reason.items():
        lines.append(
            f"| {reason} | {item['trades']} | {format_pct(item['mean_net_return_pct'])} | "
            f"{format_num(item['win_rate_pct'])}% |"
        )
    lines += [
        "",
        "## 年度拆分（描述性，不用于选参数）",
        "",
        "| 年份 | 三买笔数 | 三买平均净收益 | 三买净胜率 | 基线平均净收益 |",
        "|---|---:|---:|---:|---:|",
    ]
    for year, item in yearly_stats.items():
        base = baseline_stats["years"].get(year, {})
        lines.append(
            f"| {year} | {item['trades']} | {format_pct(item['mean_net_return_pct'])} | "
            f"{format_num(item['win_rate_pct'])}% | {format_pct(base.get('mean_net_return_pct'))} |"
        )
    lines += [
        "",
        "## 怎么读这个结果",
        "",
        "- 这是**一个**可执行三买定义，不是完整缠论。原始 K 线分型、未做包含合并；结论不能写成“缠论无效”。",
        "- 这是 **2020-2023、BTC/ETH、15m、只做多** 的单个样本；不能直接推广到其他品种、周期或空头。",
        "- 0.1%/边手续费和 2bps/边滑点是研究假设，不是已核实账户费率；没有盘口、排队、部分成交模型。",
        "- 简单权益曲线是逐笔收盘、固定风险、非复利，不是完整组合盯市回撤。",
        "- 即使 PASS，也只代表值得进入下一阶段验证，不代表可实盘或具有统计保证。",
        "",
        "## 复现",
        "",
        "```bash",
        ".venv/bin/python research_projects/chan3_v1/prepare_data.py   # 数据已缓存时可复用",
        ".venv/bin/python research_projects/chan3_v1/freeze.py",
        ".venv/bin/python research_projects/chan3_v1/run_study.py",
        "```",
        "",
        "运行前 `run_study.py` 会校验 `FREEZE.json` 中的协议、代码和数据清单哈希；结果目录已有 `summary.json` 时拒绝二次运行。",
        "",
    ]
    report = "\n".join(lines)

    RESULTS.mkdir(parents=True, exist_ok=True)
    with (RESULTS / "signals.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(signal_rows[0].keys()) if signal_rows else ["symbol"])
        writer.writeheader()
        writer.writerows(signal_rows)
    with (RESULTS / "trades.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(trade_rows[0].keys()) if trade_rows else ["symbol"])
        writer.writeheader()
        writer.writerows(trade_rows)
    with (RESULTS / "equity.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(equity_rows[0].keys()) if equity_rows else ["exit_time"])
        writer.writeheader()
        writer.writerows(equity_rows)
    (RESULTS / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (RESULTS / "bootstrap.json").write_text(
        json.dumps(summary["bootstrap"], indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (HERE / "REPORT.md").write_text(report, encoding="utf-8")
    run_manifest = {
        "project": "CHAN3_V1",
        "created_at_utc": summary["created_at_utc"],
        "freeze": freeze,
        "data_manifest_sha256": sha256(HERE / "data_manifest.json"),
        "result_hashes": {
            name: sha256(RESULTS / name)
            for name in ("signals.csv", "trades.csv", "equity.csv", "summary.json", "bootstrap.json")
        },
        "report_sha256": sha256(HERE / "REPORT.md"),
        "runtime": summary["runtime"],
    }
    (HERE / "RUN_MANIFEST.json").write_text(
        json.dumps(run_manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[run] verdict={verdict}", flush=True)
    print(
        f"[run] trades={primary['trades']} mean={format_pct(primary['mean_net_return_pct'])} "
        f"baseline={format_pct(baseline_stats['mean_net_return_pct'])} "
        f"excess={format_pct(diff_ci_pct['mean'])}",
        flush=True,
    )
    print(f"[run] wrote {HERE / 'REPORT.md'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
