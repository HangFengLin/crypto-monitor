#!/usr/bin/env python3
"""
单标的自动化回测与可视化脚本。

依赖：
    pip install yfinance pandas numpy plotly

示例：
    python3 backtest_visual_report.py --symbol BTC-USD --period 2y
    python3 backtest_visual_report.py --symbol AAPL --start 2022-01-01 --end 2025-12-31
"""

from __future__ import annotations

import argparse
import os
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# macOS 系统 Python 可能使用 LibreSSL，urllib3 会给出兼容性警告；该警告不影响 yfinance 下载。
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL.*")

try:
    import pandas as pd
    import plotly.graph_objects as go
    import yfinance as yf
    from plotly.subplots import make_subplots
except ModuleNotFoundError as exc:
    missing_package = exc.name or "unknown"
    raise SystemExit(
        f"缺少依赖包: {missing_package}\n"
        "请先运行: python3 -m pip install -r requirements-backtest.txt"
    ) from exc


@dataclass
class Trade:
    """保存单笔交易的完整信息，便于统计和图表标记。"""

    entry_date: pd.Timestamp
    entry_price: float
    exit_date: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    exit_reason: str = "open"

    @property
    def return_pct(self) -> Optional[float]:
        """计算单笔交易收益率；未平仓交易没有最终收益率。"""
        if self.exit_price is None:
            return None
        return self.exit_price / self.entry_price - 1.0


def parse_args() -> argparse.Namespace:
    """解析命令行参数，让脚本可复用到不同标的和不同周期。"""
    parser = argparse.ArgumentParser(description="K线策略回测与可视化报告生成器")
    parser.add_argument("--symbol", default="BTC-USD", help="yfinance 标的代码，例如 BTC-USD 或 AAPL")
    parser.add_argument("--period", default="2y", help="下载区间，例如 6mo、1y、2y、5y；设置 start/end 时会被忽略")
    parser.add_argument("--start", default=None, help="开始日期，例如 2022-01-01")
    parser.add_argument("--end", default=None, help="结束日期，例如 2025-12-31")
    parser.add_argument("--ma-window", type=int, default=20, help="均线窗口，默认 20 日")
    parser.add_argument("--volume-window", type=int, default=20, help="成交量均线窗口，默认 20 日")
    parser.add_argument("--volume-multiplier", type=float, default=1.05, help="成交量放大倍数，默认大于 20 日均量 1.05 倍")
    parser.add_argument("--stop-loss", type=float, default=0.03, help="止损比例，默认 0.03")
    parser.add_argument("--take-profit", type=float, default=0.06, help="止盈比例，默认 0.06")
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


def fetch_daily_data(symbol: str, period: str, start: Optional[str], end: Optional[str]) -> pd.DataFrame:
    """使用 yfinance 获取日线 OHLCV 数据，并统一字段格式。"""
    if start or end:
        data = yf.download(symbol, start=start, end=end, interval="1d", auto_adjust=False, progress=False)
    else:
        data = yf.download(symbol, period=period, interval="1d", auto_adjust=False, progress=False)

    if data.empty:
        raise RuntimeError(f"没有获取到 {symbol} 的日线数据，请检查标的代码或日期范围。")

    # yfinance 有时会返回多级列索引，这里压平为 Open/High/Low/Close/Volume。
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)

    required_columns = ["Open", "High", "Low", "Close", "Volume"]
    data = data[required_columns].dropna().copy()
    data.index = pd.to_datetime(data.index)
    data.sort_index(inplace=True)
    return data


def add_indicators(data: pd.DataFrame, ma_window: int, volume_window: int) -> pd.DataFrame:
    """添加策略所需的均线、成交量均线和买入触发条件。"""
    df = data.copy()
    df["MA"] = df["Close"].rolling(ma_window).mean()
    df["Volume_MA"] = df["Volume"].rolling(volume_window).mean()
    df["Prev_Close"] = df["Close"].shift(1)
    df["Prev_MA"] = df["MA"].shift(1)
    return df


def run_backtest(
    data: pd.DataFrame,
    volume_multiplier: float,
    stop_loss: float,
    take_profit: float,
) -> tuple[list[Trade], pd.Series]:
    """执行长仓回测，并生成每日权益曲线。"""
    trades: list[Trade] = []
    equity_values: list[float] = []
    equity_dates: list[pd.Timestamp] = []

    cash_equity = 1.0
    shares = 0.0
    current_trade: Optional[Trade] = None

    for date, row in data.iterrows():
        close_price = float(row["Close"])
        high_price = float(row["High"])
        low_price = float(row["Low"])

        if current_trade is not None:
            stop_price = current_trade.entry_price * (1.0 - stop_loss)
            target_price = current_trade.entry_price * (1.0 + take_profit)

            # 保守处理：同一天同时触及止损和止盈时，优先认定止损发生。
            if low_price <= stop_price:
                cash_equity = shares * stop_price
                current_trade.exit_date = date
                current_trade.exit_price = stop_price
                current_trade.exit_reason = "stop_loss"
                shares = 0.0
                current_trade = None
            elif high_price >= target_price:
                cash_equity = shares * target_price
                current_trade.exit_date = date
                current_trade.exit_price = target_price
                current_trade.exit_reason = "take_profit"
                shares = 0.0
                current_trade = None
            elif pd.notna(row["MA"]) and close_price < float(row["MA"]):
                cash_equity = shares * close_price
                current_trade.exit_date = date
                current_trade.exit_price = close_price
                current_trade.exit_reason = "ma_break"
                shares = 0.0
                current_trade = None

        if current_trade is None:
            crossed_above_ma = (
                pd.notna(row["MA"])
                and pd.notna(row["Prev_MA"])
                and float(row["Prev_Close"]) <= float(row["Prev_MA"])
                and close_price > float(row["MA"])
            )
            volume_expanded = pd.notna(row["Volume_MA"]) and float(row["Volume"]) > float(row["Volume_MA"]) * volume_multiplier

            if crossed_above_ma and volume_expanded:
                current_trade = Trade(entry_date=date, entry_price=close_price)
                trades.append(current_trade)
                shares = cash_equity / close_price

        # 每天记录一次权益：持仓时按收盘价盯市，空仓时维持现金权益。
        marked_equity = shares * close_price if current_trade is not None else cash_equity
        equity_dates.append(date)
        equity_values.append(marked_equity)

    # 如果最后一天仍持仓，用最后收盘价做期末平仓统计，避免报告缺失收益。
    if current_trade is not None:
        last_date = data.index[-1]
        last_close = float(data.iloc[-1]["Close"])
        cash_equity = shares * last_close
        current_trade.exit_date = last_date
        current_trade.exit_price = last_close
        current_trade.exit_reason = "end_of_data"
        equity_values[-1] = cash_equity

    equity_curve = pd.Series(equity_values, index=equity_dates, name="Equity")
    return trades, equity_curve


def calculate_metrics(trades: list[Trade], equity_curve: pd.Series) -> dict[str, float]:
    """计算总交易次数、胜率、最大回撤和预期收益率等核心指标。"""
    closed_returns = [trade.return_pct for trade in trades if trade.return_pct is not None]
    wins = [ret for ret in closed_returns if ret > 0]
    losses = [ret for ret in closed_returns if ret <= 0]

    total_trades = len(closed_returns)
    win_rate = len(wins) / total_trades if total_trades else 0.0
    expectancy = float(sum(closed_returns) / len(closed_returns)) if closed_returns else 0.0
    total_return = float(equity_curve.iloc[-1] / equity_curve.iloc[0] - 1.0) if len(equity_curve) > 1 else 0.0

    running_peak = equity_curve.cummax()
    drawdown = equity_curve / running_peak - 1.0
    max_drawdown = float(drawdown.min()) if not drawdown.empty else 0.0

    return {
        "total_trades": float(total_trades),
        "win_rate": win_rate,
        "expectancy": expectancy,
        "total_return": total_return,
        "max_drawdown": max_drawdown,
        "avg_win": float(sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss": float(sum(losses) / len(losses)) if losses else 0.0,
    }


def build_report(
    symbol: str,
    data: pd.DataFrame,
    trades: list[Trade],
    equity_curve: pd.Series,
    metrics: dict[str, float],
    output_path: Path,
) -> None:
    """使用 Plotly 绘制 K 线、交易标记、成交量和累计收益曲线，并保存 HTML 报告。"""
    entry_dates = [trade.entry_date for trade in trades]
    entry_prices = [data.loc[trade.entry_date, "Low"] * 0.985 for trade in trades]

    stop_trades = [trade for trade in trades if trade.exit_reason == "stop_loss"]
    stop_dates = [trade.exit_date for trade in stop_trades if trade.exit_date is not None]
    stop_prices = [data.loc[trade.exit_date, "High"] * 1.015 for trade in stop_trades if trade.exit_date is not None]

    exit_trades = [trade for trade in trades if trade.exit_reason in {"take_profit", "ma_break", "end_of_data"}]
    exit_dates = [trade.exit_date for trade in exit_trades if trade.exit_date is not None]
    exit_prices = [data.loc[trade.exit_date, "High"] * 1.015 for trade in exit_trades if trade.exit_date is not None]

    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.58, 0.16, 0.26],
        subplot_titles=("K线与交易标记", "成交量", "累计收益曲线"),
    )

    fig.add_trace(
        go.Candlestick(
            x=data.index,
            open=data["Open"],
            high=data["High"],
            low=data["Low"],
            close=data["Close"],
            name="K线",
            increasing_line_color="#16a34a",
            decreasing_line_color="#dc2626",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(go.Scatter(x=data.index, y=data["MA"], mode="lines", name="20日均线", line=dict(color="#2563eb", width=1.5)), row=1, col=1)
    fig.add_trace(
        go.Scatter(
            x=entry_dates,
            y=entry_prices,
            mode="markers",
            name="买入/开仓",
            marker=dict(symbol="triangle-up", color="#22c55e", size=13, line=dict(color="#14532d", width=1)),
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=stop_dates,
            y=stop_prices,
            mode="markers",
            name="止损/失效",
            marker=dict(symbol="x", color="#ef4444", size=13, line=dict(width=2)),
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=exit_dates,
            y=exit_prices,
            mode="markers",
            name="其他退出",
            marker=dict(symbol="triangle-down", color="#f97316", size=11),
        ),
        row=1,
        col=1,
    )

    volume_colors = ["#86efac" if close >= open_ else "#fecaca" for close, open_ in zip(data["Close"], data["Open"])]
    fig.add_trace(go.Bar(x=data.index, y=data["Volume"], name="成交量", marker_color=volume_colors), row=2, col=1)

    cumulative_return = equity_curve / equity_curve.iloc[0] - 1.0
    fig.add_trace(
        go.Scatter(
            x=cumulative_return.index,
            y=cumulative_return,
            mode="lines",
            name="累计收益",
            line=dict(color="#111827", width=2),
        ),
        row=3,
        col=1,
    )

    report_text = (
        f"交易次数: {metrics['total_trades']:.0f}<br>"
        f"胜率: {metrics['win_rate']:.2%}<br>"
        f"最大回撤: {metrics['max_drawdown']:.2%}<br>"
        f"预期收益率/笔: {metrics['expectancy']:.2%}<br>"
        f"累计收益: {metrics['total_return']:.2%}"
    )
    fig.add_annotation(
        text=report_text,
        xref="paper",
        yref="paper",
        x=0.01,
        y=0.98,
        showarrow=False,
        align="left",
        bgcolor="rgba(255,255,255,0.9)",
        bordercolor="#d1d5db",
        borderwidth=1,
        font=dict(size=12, color="#111827"),
    )

    fig.update_layout(
        title=f"{symbol} 策略回测报告",
        template="plotly_white",
        height=980,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=55, r=30, t=90, b=50),
        xaxis_rangeslider_visible=False,
    )
    fig.update_yaxes(title_text="价格", row=1, col=1)
    fig.update_yaxes(title_text="成交量", row=2, col=1)
    fig.update_yaxes(title_text="收益率", tickformat=".0%", row=3, col=1)
    fig.write_html(output_path, include_plotlyjs="cdn")


def print_console_report(symbol: str, trades: list[Trade], metrics: dict[str, float], output_path: Path) -> None:
    """在控制台输出一份简洁的性能分析摘要。"""
    print(f"\n{symbol} 回测完成")
    print(f"总交易次数: {metrics['total_trades']:.0f}")
    print(f"胜率: {metrics['win_rate']:.2%}")
    print(f"平均盈利: {metrics['avg_win']:.2%}")
    print(f"平均亏损: {metrics['avg_loss']:.2%}")
    print(f"预期收益率/笔: {metrics['expectancy']:.2%}")
    print(f"最大回撤: {metrics['max_drawdown']:.2%}")
    print(f"累计收益: {metrics['total_return']:.2%}")
    print(f"HTML 报告: {output_path.resolve()}")

    if not trades:
        print("\n提示：当前回测区间内策略没有产生任何交易，请尝试放宽成交量条件或调整日期范围。")


def main() -> None:
    """串联数据获取、策略回测、指标统计和可视化输出。"""
    args = parse_args()
    output_path = resolve_output_path(args, f"{args.symbol.replace('-', '_')}_backtest_report.html")

    data = fetch_daily_data(args.symbol, args.period, args.start, args.end)
    data = add_indicators(data, args.ma_window, args.volume_window)
    trades, equity_curve = run_backtest(data, args.volume_multiplier, args.stop_loss, args.take_profit)
    metrics = calculate_metrics(trades, equity_curve)

    build_report(args.symbol, data, trades, equity_curve, metrics, output_path)
    print_console_report(args.symbol, trades, metrics, output_path)


if __name__ == "__main__":
    main()
