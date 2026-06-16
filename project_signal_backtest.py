#!/usr/bin/env python3
"""
项目实盘信号回测与可视化报告。

这个脚本调用 strategy.py 中的核心信号：
1. MACD 底背驰 -> 开多观察，需通过 RSI/EMA60/成交量过滤。
2. MACD 顶背驰 -> 开空观察，需等待 MA5/MA10 死叉确认，再通过过滤。
3. 止损采用项目信号里的 ATR 逻辑：多单为背驰低点 - 1.5 ATR，空单为背驰高点 + 1.5 ATR。
4. 可选备用止损：浮盈达到 1R 后启用 ATR 移动止损。

示例：
    python3 project_signal_backtest.py --symbol BTCUSDT --interval 15m --limit 1000
    python3 project_signal_backtest.py --symbol ETHUSDT --interval 1h --limit 1500 --reward-risk 1.5
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from data_client import fetch_historical_klines, fetch_okx_historical_klines, normalize_okx_symbol
from indicators import calculate_indicators
from strategy import HIGHER_TREND_INTERVAL, ProjectSignalEngine


@dataclass
class SignalTrade:
    """保存一次信号验证结果。"""

    signal: str
    stop_mode: str
    entry_time: pd.Timestamp
    entry_price: float
    stop_loss: float
    target_price: float
    exit_time: pd.Timestamp
    exit_price: float
    exit_reason: str
    outcome: str
    return_pct: float
    bars_held: int
    confirm_bars: int
    strength: float
    divergence_type: str
    trend: str
    higher_trend: str
    signal_grade: str
    signal_score: int
    signal_score_max: int
    structure_score: int
    structure_text: str
    score_text: str
    filter_text: str


def parse_args() -> argparse.Namespace:
    """解析回测参数。"""
    parser = argparse.ArgumentParser(description="项目实盘 MACD 背驰信号有效性回测")
    parser.add_argument("--symbol", default="BTCUSDT", help="交易对，例如 BTCUSDT、ETHUSDT；OKX 合约也可用 BTC-USDT-SWAP")
    parser.add_argument("--exchange", choices=["binance", "okx"], default="binance", help="行情数据源，默认 binance")
    parser.add_argument("--okx-instrument-type", choices=["SWAP", "SPOT"], default="SWAP", help="OKX 交易品种类型，默认 SWAP")
    parser.add_argument("--interval", default="15m", help="K线周期，例如 15m、1h、4h、1d")
    parser.add_argument("--limit", type=int, default=1000, help="回测 K 线数量；超过 1000 时自动分页拉取")
    parser.add_argument("--reward-risk", type=float, default=2.0, help="目标价按几倍初始风险计算，默认 2R")
    parser.add_argument("--max-hold-bars", type=int, default=96, help="最多观察多少根 K 线，15m 下 96 根约等于 1 天")
    parser.add_argument("--fee-rate", type=float, default=0.001, help="单边手续费/滑点估计，默认 0.001")
    parser.add_argument(
        "--stop-mode",
        choices=["structure_atr", "atr_trailing_after_1r", "all"],
        default="structure_atr",
        help="止损模式：结构ATR硬止损、1R后ATR移动止损，或 all 对比两者",
    )
    parser.add_argument("--min-group-trades", type=int, default=3, help="优化候选组合最少样本数，默认 3")
    parser.add_argument("--min-signal-score", type=int, default=0, help="只交易评分不低于该值的信号，默认 0 表示不过滤")
    parser.add_argument("--min-structure-score", type=int, default=0, help="只交易结构分不低于该值的信号，默认 0 表示不过滤")
    parser.add_argument(
        "--divergence-filter",
        choices=["all", "macd", "fast_macd"],
        default="all",
        help="按背驰类型过滤交易信号，默认 all",
    )
    parser.add_argument("--block-local-countertrend", action="store_true", help="阻止与本周期 EMA20/EMA60 趋势相反的交易")
    parser.add_argument("--output", default=None, help="输出 HTML 文件名；默认自动生成")
    parser.add_argument("--reports-dir", default=os.getenv("REPORTS_DIR"), help="报告输出目录；也可用 REPORTS_DIR 环境变量")
    return parser.parse_args()


def resolve_output_path(args: argparse.Namespace, default_filename: str) -> Path:
    """Resolve the report path and create the parent directory when needed."""
    if args.output:
        output_path = Path(args.output)
    elif args.reports_dir:
        output_path = Path(args.reports_dir) / default_filename
    else:
        output_path = Path(default_filename)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return output_path


def fetch_binance_klines(symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
    """获取 Binance K 线，并补充回测报告需要的时间字段。"""
    return [
        {**bar, "time": pd.to_datetime(int(bar["open_time"]), unit="ms")}
        for bar in fetch_historical_klines(symbol, interval, limit)
    ]


def fetch_exchange_klines(exchange: str, symbol: str, interval: str, limit: int, okx_instrument_type: str = "SWAP") -> list[dict[str, Any]]:
    """按数据源获取 K 线，并补充回测报告需要的时间字段。"""
    if exchange == "okx":
        return [
            {**bar, "time": pd.to_datetime(int(bar["open_time"]), unit="ms")}
            for bar in fetch_okx_historical_klines(symbol, interval, limit, okx_instrument_type)
        ]
    return fetch_binance_klines(symbol, interval, limit)


def add_higher_timeframe_context(bars: list[dict[str, Any]], higher_bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把已知的 4h 趋势状态按时间对齐到低周期 K 线，避免使用未来数据。"""
    if not bars or not higher_bars:
        return bars

    higher_index = 0
    enriched = []
    for bar in bars:
        bar_close_time = int(bar.get("close_time") or bar.get("open_time") or 0)
        while higher_index + 1 < len(higher_bars) and int(higher_bars[higher_index + 1].get("close_time") or higher_bars[higher_index + 1].get("open_time", 0)) <= bar_close_time:
            higher_index += 1
        higher_close_time = int(higher_bars[higher_index].get("close_time") or higher_bars[higher_index].get("open_time", 0))
        higher = higher_bars[higher_index] if higher_close_time <= bar_close_time else {}
        enriched.append(
            {
                **bar,
                "higher_close": higher.get("close"),
                "higher_ema20": higher.get("ema20"),
                "higher_ema60": higher.get("ema60"),
                "higher_ema200": higher.get("ema200"),
                "higher_macd": higher.get("macd"),
                "higher_trend": higher.get("trend", "unknown"),
            }
        )
    return enriched


def fetch_higher_timeframe_context(symbol: str, bars: list[dict[str, Any]], interval: str, exchange: str = "binance", okx_instrument_type: str = "SWAP") -> list[dict[str, Any]]:
    """下载并对齐 4h 上下文；4h/1d 回测直接使用本周期上下文。"""
    if not bars:
        return bars
    if interval in {HIGHER_TREND_INTERVAL, "1d"}:
        return add_higher_timeframe_context(bars, bars)
    higher_limit = max(120, min(1000, len(bars) // 8 + 120))
    higher_bars = calculate_indicators(fetch_exchange_klines(exchange, symbol, HIGHER_TREND_INTERVAL, higher_limit, okx_instrument_type))
    return add_higher_timeframe_context(bars, higher_bars)


def update_trailing_stop(
    direction: str,
    current_stop: float,
    entry_price: float,
    initial_risk: float,
    favorable_price: float,
    atr_value: Optional[float],
    fee_rate: float,
) -> float:
    """浮盈达到 1R 后，用 ATR 跟踪止损；止损只朝有利方向移动。"""
    if atr_value is None or atr_value <= 0:
        return current_stop

    if direction == "long":
        if favorable_price < entry_price + initial_risk:
            return current_stop
        trailing_stop = favorable_price - atr_value * 1.2
        breakeven_stop = entry_price * (1 + fee_rate * 2)
        return max(current_stop, trailing_stop, breakeven_stop)

    if favorable_price > entry_price - initial_risk:
        return current_stop
    trailing_stop = favorable_price + atr_value * 1.2
    breakeven_stop = entry_price * (1 - fee_rate * 2)
    return min(current_stop, trailing_stop, breakeven_stop)


def evaluate_trade(
    signal: dict[str, Any],
    bars: list[dict[str, Any]],
    entry_index: int,
    reward_risk: float,
    max_hold_bars: int,
    fee_rate: float,
    stop_mode: str = "structure_atr",
) -> Optional[SignalTrade]:
    """用后续 K 线验证信号是否有效：先止损则失败，先到目标/推保护则成功。"""
    if entry_index >= len(bars):
        return None
    direction = signal["signal"]
    entry_bar = bars[entry_index]
    entry_price = entry_bar["open"]
    stop_loss = float(signal["stop_loss"])

    if direction == "long":
        risk = entry_price - stop_loss
        if risk <= 0:
            return None
        target_price = entry_price + risk * reward_risk
    else:
        risk = stop_loss - entry_price
        if risk <= 0:
            return None
        target_price = entry_price - risk * reward_risk

    last_index = min(len(bars) - 1, entry_index + max_hold_bars)
    exit_index = last_index
    exit_bar = bars[last_index]
    exit_price = exit_bar["close"]
    outcome = "timeout"
    exit_reason = "timeout"
    active_stop = stop_loss
    highest_price = entry_price
    lowest_price = entry_price
    protection_price = entry_price + risk if direction == "long" else entry_price - risk
    protection_reached = False

    for index in range(entry_index, last_index + 1):
        bar = bars[index]
        if direction == "long":
            # 单根 K 线内无法知道先后顺序，保守按先止损处理。
            if bar["low"] <= active_stop:
                exit_index = index
                exit_bar, exit_price = bar, active_stop
                exit_reason = "trailing_stop" if active_stop > stop_loss else "stop_loss"
                break
            if bar["high"] >= target_price:
                exit_index = index
                exit_bar, exit_price, exit_reason = bar, target_price, "take_profit"
                break
            if bar["high"] >= protection_price:
                protection_reached = True
                exit_index = index
                exit_bar, exit_price, exit_reason = bar, protection_price, "protection_reached"
                break
            highest_price = max(highest_price, bar["high"])
            if stop_mode == "atr_trailing_after_1r":
                active_stop = update_trailing_stop(direction, active_stop, entry_price, risk, highest_price, bar.get("atr"), fee_rate)
        else:
            # 单根 K 线内无法知道先后顺序，保守按先止损处理。
            if bar["high"] >= active_stop:
                exit_index = index
                exit_bar, exit_price = bar, active_stop
                exit_reason = "trailing_stop" if active_stop < stop_loss else "stop_loss"
                break
            if bar["low"] <= target_price:
                exit_index = index
                exit_bar, exit_price, exit_reason = bar, target_price, "take_profit"
                break
            if bar["low"] <= protection_price:
                protection_reached = True
                exit_index = index
                exit_bar, exit_price, exit_reason = bar, protection_price, "protection_reached"
                break
            lowest_price = min(lowest_price, bar["low"])
            if stop_mode == "atr_trailing_after_1r":
                active_stop = update_trailing_stop(direction, active_stop, entry_price, risk, lowest_price, bar.get("atr"), fee_rate)

    gross_return = exit_price / entry_price - 1 if direction == "long" else entry_price / exit_price - 1
    net_return = gross_return - fee_rate * 2
    success_reasons = {"take_profit", "protection_reached", "trailing_stop"}
    outcome = "win" if exit_reason in success_reasons or protection_reached else "loss"

    return SignalTrade(
        signal=direction,
        stop_mode=stop_mode,
        entry_time=entry_bar["time"],
        entry_price=entry_price,
        stop_loss=active_stop,
        target_price=target_price,
        exit_time=exit_bar["time"],
        exit_price=exit_price,
        exit_reason=exit_reason,
        outcome=outcome,
        return_pct=net_return,
        bars_held=exit_index - entry_index,
        confirm_bars=int(signal.get("confirm_bars", 0) or 0),
        strength=float(signal.get("strength", 0)),
        divergence_type=str(signal.get("divergence_type", "macd")),
        trend=str(entry_bar.get("trend", "sideways")),
        higher_trend=str(signal.get("higher_trend", entry_bar.get("higher_trend", "unknown"))),
        signal_grade=str(signal.get("signal_grade", "weak")),
        signal_score=int(signal.get("signal_score", 0) or 0),
        signal_score_max=int(signal.get("signal_score_max", 20) or 20),
        structure_score=int(signal.get("structure_score", 0) or 0),
        structure_text=str(signal.get("structure_text", "结构因子不足")),
        score_text=str(signal.get("score_text", "")),
        filter_text=str(signal.get("filter", "")),
    )


def signal_passes_trade_filters(
    signal: dict[str, Any],
    entry_bar: dict[str, Any],
    min_signal_score: int = 0,
    min_structure_score: int = 0,
    divergence_filter: str = "all",
    block_local_countertrend: bool = False,
) -> bool:
    """优化过滤器：只决定是否实际纳入回测交易，不改变信号检测。"""
    if int(signal.get("signal_score", 0) or 0) < min_signal_score:
        return False
    if int(signal.get("structure_score", 0) or 0) < min_structure_score:
        return False
    if divergence_filter != "all" and str(signal.get("divergence_type", "macd")) != divergence_filter:
        return False
    if block_local_countertrend:
        trend = str(entry_bar.get("trend", "sideways"))
        if signal.get("signal") == "long" and trend == "down":
            return False
        if signal.get("signal") == "short" and trend == "up":
            return False
    return True


def run_backtest(
    bars: list[dict[str, Any]],
    reward_risk: float,
    max_hold_bars: int,
    fee_rate: float,
    stop_mode: str = "structure_atr",
    min_signal_score: int = 0,
    min_structure_score: int = 0,
    divergence_filter: str = "all",
    block_local_countertrend: bool = False,
) -> tuple[list[SignalTrade], pd.Series]:
    """逐根 K 线滚动生成信号并验证结果，同时生成累计收益曲线。"""
    engine = ProjectSignalEngine()
    trades: list[SignalTrade] = []
    equity = 1.0
    equity_points = []
    used_exit_until = -1
    seen_signal_keys: set[tuple[str, Any]] = set()
    ready_bars: list[dict[str, Any]] = []

    for index in range(len(bars)):
        if bars[index].get("macd") is not None:
            ready_bars.append(bars[index])
        signal = engine.detect(ready_bars)
        if signal.get("signal") not in {"long", "short"}:
            equity_points.append((bars[index]["time"], equity))
            continue

        signal_key = (str(signal["signal"]), signal.get("divergence_time"))
        if signal_key in seen_signal_keys:
            equity_points.append((bars[index]["time"], equity))
            continue
        seen_signal_keys.add(signal_key)

        # 同一持仓窗口内不重复开仓，避免一段行情里连续信号夸大交易次数。
        if index <= used_exit_until:
            equity_points.append((bars[index]["time"], equity))
            continue

        if not signal_passes_trade_filters(signal, bars[index], min_signal_score, min_structure_score, divergence_filter, block_local_countertrend):
            equity_points.append((bars[index]["time"], equity))
            continue

        trade = evaluate_trade(signal, bars, index + 1, reward_risk, max_hold_bars, fee_rate, stop_mode)
        if trade is None:
            equity_points.append((bars[index]["time"], equity))
            continue
        trades.append(trade)
        equity *= 1 + trade.return_pct
        used_exit_until = min(len(bars) - 1, index + 1 + trade.bars_held)
        equity_points.append((bars[index]["time"], equity))

    equity_curve = pd.Series([point[1] for point in equity_points], index=[point[0] for point in equity_points], name="Equity")
    return trades, equity_curve


def calculate_metrics(trades: list[SignalTrade], equity_curve: pd.Series) -> dict[str, float]:
    """计算信号有效性指标。"""
    returns = [trade.return_pct for trade in trades]
    wins = [ret for ret in returns if ret > 0]
    losses = [ret for ret in returns if ret <= 0]
    successful_trades = [trade for trade in trades if trade.outcome == "win"]
    failed_trades = [trade for trade in trades if trade.outcome == "loss"]
    stop_trades = [trade for trade in trades if trade.exit_reason == "stop_loss"]
    protected_trades = [trade for trade in trades if trade.exit_reason in {"protection_reached", "trailing_stop"}]
    bars_held = [trade.bars_held for trade in trades]
    confirm_bars = [trade.confirm_bars for trade in trades]
    running_peak = equity_curve.cummax()
    drawdown = equity_curve / running_peak - 1
    return {
        "total_trades": float(len(trades)),
        "win_rate": len(successful_trades) / len(trades) if trades else 0.0,
        "loss_rate": len(failed_trades) / len(trades) if trades else 0.0,
        "expectancy": sum(returns) / len(returns) if returns else 0.0,
        "total_return": equity_curve.iloc[-1] / equity_curve.iloc[0] - 1 if len(equity_curve) > 1 else 0.0,
        "max_drawdown": float(drawdown.min()) if not drawdown.empty else 0.0,
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "avg_bars_held": sum(bars_held) / len(bars_held) if bars_held else 0.0,
        "avg_confirm_bars": sum(confirm_bars) / len(confirm_bars) if confirm_bars else 0.0,
        "stop_trigger_rate": len(stop_trades) / len(trades) if trades else 0.0,
        "protection_rate": len(protected_trades) / len(trades) if trades else 0.0,
    }


def build_group_stats(trades: list[SignalTrade]) -> pd.DataFrame:
    """按方向、趋势、背驰类型和强度分组，暴露可优化的条件。"""
    if not trades:
        return pd.DataFrame()
    frame = pd.DataFrame([trade.__dict__ for trade in trades])
    frame["strength_bucket"] = pd.cut(frame["strength"], bins=[0, 0.4, 0.7, 1.0], labels=["低强度", "中强度", "高强度"], include_lowest=True)
    frame["score_bucket"] = pd.cut(frame["signal_score"], bins=[-1, 6, 9, 20], labels=["低分", "中分", "高分"], include_lowest=True)
    frame["structure_bucket"] = pd.cut(frame["structure_score"], bins=[-1, 0, 1, 3], labels=["无结构", "弱结构", "强结构"], include_lowest=True)
    grouped = (
        frame.groupby(["signal", "trend", "higher_trend", "signal_grade", "score_bucket", "structure_bucket", "divergence_type", "strength_bucket"], observed=True)
        .agg(
            trades=("return_pct", "count"),
            win_rate=("outcome", lambda values: (values == "win").mean()),
            avg_return=("return_pct", "mean"),
            avg_score=("signal_score", "mean"),
            avg_structure=("structure_score", "mean"),
            avg_confirm_bars=("confirm_bars", "mean"),
        )
        .reset_index()
        .sort_values(["win_rate", "avg_return", "trades"], ascending=[False, False, False])
    )
    return grouped


def build_report(
    symbol: str,
    interval: str,
    bars: list[dict[str, Any]],
    trades: list[SignalTrade],
    equity_curve: pd.Series,
    metrics: dict[str, float],
    group_stats: pd.DataFrame,
    output_path: Path,
    stop_mode: str,
    filter_label: str = "",
) -> None:
    """生成包含 K 线、信号标记、失效标记、收益曲线和优化统计的 HTML 报告。"""
    frame = pd.DataFrame(bars)
    fig = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.05,
        row_heights=[0.5, 0.14, 0.22, 0.14],
        specs=[[{"type": "xy"}], [{"type": "xy"}], [{"type": "xy"}], [{"type": "table"}]],
        subplot_titles=("K线与项目信号", "成交量", "累计收益曲线", "条件分组统计"),
    )
    fig.add_trace(go.Candlestick(x=frame["time"], open=frame["open"], high=frame["high"], low=frame["low"], close=frame["close"], name="K线"), row=1, col=1)
    fig.add_trace(go.Scatter(x=frame["time"], y=frame["ema60"], mode="lines", name="EMA60", line=dict(color="#2563eb", width=1.2)), row=1, col=1)

    long_trades = [trade for trade in trades if trade.signal == "long"]
    short_trades = [trade for trade in trades if trade.signal == "short"]
    failed_trades = [trade for trade in trades if trade.outcome == "loss"]
    fig.add_trace(go.Scatter(x=[t.entry_time for t in long_trades], y=[t.entry_price for t in long_trades], mode="markers", name="开多", marker=dict(symbol="triangle-up", color="#16a34a", size=13)), row=1, col=1)
    fig.add_trace(go.Scatter(x=[t.entry_time for t in short_trades], y=[t.entry_price for t in short_trades], mode="markers", name="开空", marker=dict(symbol="triangle-down", color="#dc2626", size=13)), row=1, col=1)
    fig.add_trace(go.Scatter(x=[t.exit_time for t in failed_trades], y=[t.exit_price for t in failed_trades], mode="markers", name="止损/失效", marker=dict(symbol="x", color="#ef4444", size=13, line=dict(width=2))), row=1, col=1)

    volume_colors = ["#86efac" if close >= open_ else "#fecaca" for close, open_ in zip(frame["close"], frame["open"])]
    fig.add_trace(go.Bar(x=frame["time"], y=frame["volume"], name="成交量", marker_color=volume_colors), row=2, col=1)
    fig.add_trace(go.Scatter(x=equity_curve.index, y=equity_curve / equity_curve.iloc[0] - 1, mode="lines", name="累计收益", line=dict(color="#111827", width=2)), row=3, col=1)

    table = group_stats.copy()
    if table.empty:
        table_values = [["无"], ["无"], ["无"], ["无"], ["无"], ["无"], ["无"], ["无"], ["0"], ["0.0"], ["0.0"], ["0.00%"], ["0.00%"], ["0.0"]]
    else:
        table_values = [
            table["signal"].astype(str).tolist(),
            table["trend"].astype(str).tolist(),
            table["higher_trend"].astype(str).tolist(),
            table["signal_grade"].astype(str).tolist(),
            table["score_bucket"].astype(str).tolist(),
            table["structure_bucket"].astype(str).tolist(),
            table["divergence_type"].astype(str).tolist(),
            table["strength_bucket"].astype(str).tolist(),
            table["trades"].astype(str).tolist(),
            table["avg_score"].map(lambda value: f"{value:.1f}").tolist(),
            table["avg_structure"].map(lambda value: f"{value:.1f}").tolist(),
            table["win_rate"].map(lambda value: f"{value:.2%}").tolist(),
            table["avg_return"].map(lambda value: f"{value:.2%}").tolist(),
            table["avg_confirm_bars"].map(lambda value: f"{value:.1f}").tolist(),
        ]
    fig.add_trace(
        go.Table(
            header=dict(values=["方向", "趋势", "高周期", "等级", "分数段", "结构", "背驰类型", "强度", "次数", "均分", "结构分", "胜率", "均值收益", "确认K"], fill_color="#f3f4f6", align="left"),
            cells=dict(values=table_values, align="left"),
        ),
        row=4,
        col=1,
    )

    summary = (
        f"交易次数: {metrics['total_trades']:.0f}<br>"
        f"胜率: {metrics['win_rate']:.2%}<br>"
        f"最大回撤: {metrics['max_drawdown']:.2%}<br>"
        f"预期收益/笔: {metrics['expectancy']:.2%}<br>"
        f"累计收益: {metrics['total_return']:.2%}<br>"
        f"止损触发占比: {metrics['stop_trigger_rate']:.2%}<br>"
        f"推保护成功占比: {metrics['protection_rate']:.2%}<br>"
        f"平均持有K数: {metrics['avg_bars_held']:.1f}<br>"
        f"平均确认K数: {metrics['avg_confirm_bars']:.1f}"
    )
    fig.add_annotation(text=summary, xref="paper", yref="paper", x=0.01, y=0.98, showarrow=False, align="left", bgcolor="rgba(255,255,255,0.92)", bordercolor="#d1d5db", borderwidth=1)
    title_suffix = f"{stop_mode}{filter_label}"
    fig.update_layout(title=f"{symbol} {interval} 项目信号有效性回测（{title_suffix}）", template="plotly_white", height=1100, hovermode="x unified", margin=dict(l=55, r=30, t=90, b=45), xaxis_rangeslider_visible=False)
    fig.update_yaxes(title_text="价格", row=1, col=1)
    fig.update_yaxes(title_text="成交量", row=2, col=1)
    fig.update_yaxes(title_text="收益率", tickformat=".0%", row=3, col=1)
    fig.write_html(output_path, include_plotlyjs="cdn")


def print_report(symbol: str, interval: str, trades: list[SignalTrade], metrics: dict[str, float], group_stats: pd.DataFrame, output_path: Path, min_group_trades: int, stop_mode: str, filter_label: str = "") -> None:
    """在控制台输出简洁报告，并提示没有交易的情况。"""
    print(f"\n{symbol} {interval} 项目信号回测完成")
    print(f"止损模式: {stop_mode}{filter_label}")
    print(f"总交易次数: {metrics['total_trades']:.0f}")
    print(f"胜率: {metrics['win_rate']:.2%}")
    print(f"平均盈利: {metrics['avg_win']:.2%}")
    print(f"平均亏损: {metrics['avg_loss']:.2%}")
    print(f"预期收益/笔: {metrics['expectancy']:.2%}")
    print(f"最大回撤: {metrics['max_drawdown']:.2%}")
    print(f"累计收益: {metrics['total_return']:.2%}")
    print(f"止损触发占比: {metrics['stop_trigger_rate']:.2%}")
    print(f"推保护成功占比: {metrics['protection_rate']:.2%}")
    print(f"平均持有K数: {metrics['avg_bars_held']:.1f}")
    print(f"平均确认K数: {metrics['avg_confirm_bars']:.1f}")
    print(f"HTML 报告: {output_path.resolve()}")
    if not trades:
        print("\n提示：当前回测区间内项目策略没有产生 long/short 实盘信号。可以增加 --limit，或测试 1h/4h 周期。")
    elif not group_stats.empty:
        reliable = group_stats[group_stats["trades"] >= min_group_trades]
        if reliable.empty:
            print(f"\n优化提示：所有条件组合样本数都少于 {min_group_trades}，暂时不要根据单个组合改策略。")
        else:
            print(f"\n表现较好的条件组合（样本数 >= {min_group_trades}）：")
            print(reliable.head(5).to_string(index=False, formatters={"win_rate": "{:.2%}".format, "avg_return": "{:.2%}".format, "avg_score": "{:.1f}".format, "avg_structure": "{:.1f}".format, "avg_confirm_bars": "{:.1f}".format}))
            weak = reliable.sort_values(["avg_return", "win_rate"], ascending=[True, True]).head(5)
            print(f"\n优先排查/过滤的弱组合（样本数 >= {min_group_trades}）：")
            print(weak.to_string(index=False, formatters={"win_rate": "{:.2%}".format, "avg_return": "{:.2%}".format, "avg_score": "{:.1f}".format, "avg_structure": "{:.1f}".format, "avg_confirm_bars": "{:.1f}".format}))


def build_stop_mode_result(
    bars: list[dict[str, Any]],
    reward_risk: float,
    max_hold_bars: int,
    fee_rate: float,
    stop_mode: str,
    min_signal_score: int = 0,
    min_structure_score: int = 0,
    divergence_filter: str = "all",
    block_local_countertrend: bool = False,
) -> dict[str, Any]:
    """运行单个止损模式并返回报告构建所需数据。"""
    trades, equity_curve = run_backtest(
        bars,
        reward_risk,
        max_hold_bars,
        fee_rate,
        stop_mode,
        min_signal_score,
        min_structure_score,
        divergence_filter,
        block_local_countertrend,
    )
    metrics = calculate_metrics(trades, equity_curve)
    group_stats = build_group_stats(trades)
    return {
        "stop_mode": stop_mode,
        "trades": trades,
        "equity_curve": equity_curve,
        "metrics": metrics,
        "group_stats": group_stats,
    }


def print_stop_mode_comparison(results: list[dict[str, Any]]) -> None:
    """输出两个备用止损信号的横向对比。"""
    rows = []
    for result in results:
        metrics = result["metrics"]
        rows.append(
            {
                "stop_mode": result["stop_mode"],
                "trades": int(metrics["total_trades"]),
                "win_rate": metrics["win_rate"],
                "expectancy": metrics["expectancy"],
                "total_return": metrics["total_return"],
                "max_drawdown": metrics["max_drawdown"],
                "stop_trigger_rate": metrics["stop_trigger_rate"],
                "protection_rate": metrics["protection_rate"],
                "avg_bars_held": metrics["avg_bars_held"],
                "avg_confirm_bars": metrics["avg_confirm_bars"],
            }
        )
    frame = pd.DataFrame(rows)
    print("\n止损模式对比：")
    print(
        frame.to_string(
            index=False,
            formatters={
                "win_rate": "{:.2%}".format,
                "expectancy": "{:.2%}".format,
                "total_return": "{:.2%}".format,
                "max_drawdown": "{:.2%}".format,
                "stop_trigger_rate": "{:.2%}".format,
                "protection_rate": "{:.2%}".format,
                "avg_bars_held": "{:.1f}".format,
                "avg_confirm_bars": "{:.1f}".format,
            },
        )
    )


def main() -> None:
    """串联数据获取、共享信号检测、有效性验证和可视化输出。"""
    args = parse_args()
    report_symbol = normalize_okx_symbol(args.symbol) if args.exchange == "okx" else args.symbol
    output_path = resolve_output_path(args, f"{report_symbol}_{args.interval}_{args.exchange}_project_signal_report.html")
    raw_bars = fetch_exchange_klines(args.exchange, args.symbol, args.interval, args.limit, args.okx_instrument_type)
    bars = fetch_higher_timeframe_context(args.symbol, calculate_indicators(raw_bars), args.interval, args.exchange, args.okx_instrument_type)
    stop_modes = ["structure_atr", "atr_trailing_after_1r"] if args.stop_mode == "all" else [args.stop_mode]
    filter_label_parts = []
    if args.min_signal_score > 0:
        filter_label_parts.append(f"score>={args.min_signal_score}")
    if args.min_structure_score > 0:
        filter_label_parts.append(f"structure>={args.min_structure_score}")
    if args.divergence_filter != "all":
        filter_label_parts.append(f"div={args.divergence_filter}")
    if args.block_local_countertrend:
        filter_label_parts.append("block_local_countertrend")
    filter_label = f"; {', '.join(filter_label_parts)}" if filter_label_parts else ""
    results = [
        build_stop_mode_result(
            bars,
            args.reward_risk,
            args.max_hold_bars,
            args.fee_rate,
            stop_mode,
            args.min_signal_score,
            args.min_structure_score,
            args.divergence_filter,
            args.block_local_countertrend,
        )
        for stop_mode in stop_modes
    ]
    primary = results[0]
    build_report(
        report_symbol,
        args.interval,
        bars,
        primary["trades"],
        primary["equity_curve"],
        primary["metrics"],
        primary["group_stats"],
        output_path,
        primary["stop_mode"],
        filter_label,
    )
    print_report(
        report_symbol,
        args.interval,
        primary["trades"],
        primary["metrics"],
        primary["group_stats"],
        output_path,
        args.min_group_trades,
        primary["stop_mode"],
        filter_label,
    )
    if len(results) > 1:
        print_stop_mode_comparison(results)


if __name__ == "__main__":
    main()
