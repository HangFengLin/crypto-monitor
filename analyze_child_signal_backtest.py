#!/usr/bin/env python3
"""Build a decision report for a pre-filtered universe child-signal run."""

from __future__ import annotations

import argparse
import ast
import hashlib
import html
import json
import math
import os
import textwrap
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backtest_statistics import weekly_block_bootstrap_summary, wilson_interval

SEED = 20260622
ITERATIONS = 2000
TOKENS = {
    "surface": "#FCFCFD",
    "panel": "#FFFFFF",
    "ink": "#1F2430",
    "muted": "#6F768A",
    "grid": "#E6E8F0",
    "axis": "#D7DBE7",
}
COLORS = {
    "blue": {"xlight": "#EAF1FE", "light": "#CEDFFE", "base": "#A3BEFA", "dark": "#2E4780"},
    "gold": {"xlight": "#FFF4C2", "light": "#FFEA8F", "base": "#FFE15B", "dark": "#736422"},
    "orange": {"xlight": "#FFEDDE", "light": "#FFBDA1", "base": "#F0986E", "dark": "#804126"},
    "olive": {"xlight": "#D8ECBD", "light": "#BEEB96", "base": "#A3D576", "dark": "#386411"},
    "pink": {"xlight": "#FCDAD6", "light": "#F5BACC", "base": "#F390CA", "dark": "#8A3A6F"},
}


def json_default(value: Any) -> Any:
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="分析独立子信号 universe 回测和止盈路径")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--baseline-trades", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--bootstrap-iterations", type=int, default=ITERATIONS)
    parser.add_argument("--seed", type=int, default=SEED)
    return parser.parse_args()


def sample_metrics(
    frame: pd.DataFrame,
    label: str,
    *,
    fee_rate: float,
    iterations: int,
    seed: int,
) -> dict[str, Any]:
    returns = pd.to_numeric(frame.get("return_pct"), errors="coerce").dropna()
    wins = int((returns > 0).sum())
    low, high = wilson_interval(wins, len(returns))
    bootstrap = weekly_block_bootstrap_summary(
        frame.to_dict("records"),
        value_key="return_pct",
        time_key="entry_time",
        iterations=iterations,
        seed=seed,
    )
    gross_profit = float(returns[returns > 0].sum())
    gross_loss = abs(float(returns[returns <= 0].sum()))
    return {
        "sample": label,
        "trades": int(len(returns)),
        "win_rate": wins / len(returns) if len(returns) else 0.0,
        "win_rate_ci_low": low,
        "win_rate_ci_high": high,
        "expectancy": float(returns.mean()) if len(returns) else 0.0,
        "expectancy_ci_low": bootstrap["ci_low"],
        "expectancy_ci_high": bootstrap["ci_high"],
        "p_nonpositive": bootstrap["p_nonpositive"],
        "profit_factor": gross_profit / gross_loss if gross_loss else float("inf"),
        "double_cost_expectancy": float((returns - fee_rate * 2).mean()) if len(returns) else 0.0,
    }


def numeric_bar(value: Any) -> int | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return int(value)


def as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


def bool_series(series: pd.Series) -> pd.Series:
    return series.map(as_bool)


def counterfactual_returns(frame: pd.DataFrame, reward_risk: float, fee_rate: float) -> pd.DataFrame:
    result = frame.copy()
    target_column = {1.0: "first_1r_bar", 1.5: "first_1_5r_bar", 2.0: "first_2r_bar"}[reward_risk]
    returns: list[float] = []
    reasons: list[str] = []
    for row in result.to_dict("records"):
        entry = float(row["entry_price"])
        risk = float(row["initial_risk"])
        stop = numeric_bar(row.get("first_initial_stop_bar"))
        target = numeric_bar(row.get(target_column))
        if stop is not None and (target is None or stop <= target):
            exit_price = float(row["initial_stop_loss"])
            reason = "stop_loss"
        elif target is not None:
            exit_price = entry + risk * reward_risk
            reason = "target"
        else:
            exit_price = float(row["horizon_close_price"])
            reason = "timeout"
        returns.append(exit_price / entry - 1 - fee_rate * 2)
        reasons.append(reason)
    result["counterfactual_return"] = returns
    result["counterfactual_reason"] = reasons
    return result


def counterfactual_table(
    frame: pd.DataFrame,
    *,
    fee_rate: float,
    iterations: int,
    seed: int,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for index, multiple in enumerate((1.0, 1.5, 2.0)):
        simulated = counterfactual_returns(frame, multiple, fee_rate)
        stats = weekly_block_bootstrap_summary(
            simulated.rename(columns={"counterfactual_return": "value"}).to_dict("records"),
            value_key="value",
            time_key="entry_time",
            iterations=iterations,
            seed=seed + index,
        )
        reasons = simulated["counterfactual_reason"].value_counts()
        rows.append(
            {
                "reward_risk": multiple,
                "trades": len(simulated),
                "win_rate": float((simulated["counterfactual_return"] > 0).mean()),
                "expectancy": stats["mean"],
                "expectancy_ci_low": stats["ci_low"],
                "expectancy_ci_high": stats["ci_high"],
                "p_nonpositive": stats["p_nonpositive"],
                "target_rate": float(reasons.get("target", 0) / len(simulated)) if len(simulated) else 0.0,
                "stop_rate": float(reasons.get("stop_loss", 0) / len(simulated)) if len(simulated) else 0.0,
                "timeout_rate": float(reasons.get("timeout", 0) / len(simulated)) if len(simulated) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def parse_r_path(value: Any) -> list[float]:
    if isinstance(value, list):
        return [float(item) for item in value]
    if value in (None, "") or (isinstance(value, float) and math.isnan(value)):
        return []
    try:
        parsed = ast.literal_eval(str(value))
    except (ValueError, SyntaxError):
        return []
    return [float(item) for item in parsed] if isinstance(parsed, list) else []


def add_chart_header(fig: Any, ax: Any, title: str, subtitle: str) -> None:
    title = textwrap.fill(title, width=78, break_long_words=False)
    subtitle = textwrap.fill(subtitle, width=112, break_long_words=False)
    fig.subplots_adjust(top=0.80)
    left = ax.get_position().x0
    fig.text(left, 0.98, title, ha="left", va="top", fontsize=14, fontweight="semibold", color=TOKENS["ink"])
    fig.text(left, 0.925, subtitle, ha="left", va="top", fontsize=9, color=TOKENS["muted"])


def chart_theme() -> None:
    import seaborn as sns

    sns.set_theme(
        style="whitegrid",
        rc={
            "figure.facecolor": TOKENS["surface"],
            "axes.facecolor": TOKENS["panel"],
            "axes.edgecolor": TOKENS["axis"],
            "axes.labelcolor": TOKENS["ink"],
            "grid.color": TOKENS["grid"],
            "grid.linewidth": 0.8,
            "axes.spines.top": False,
            "axes.spines.right": False,
        },
    )


def save_figure(fig: Any, path: Path) -> None:
    fig.savefig(path, dpi=180, bbox_inches="tight", facecolor=TOKENS["surface"])
    fig.clf()


def render_charts(
    output_dir: Path,
    trades: pd.DataFrame,
    metrics: pd.DataFrame,
    counterfactual: pd.DataFrame,
    old_exact_count: int | None,
) -> list[dict[str, Any]]:
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib-child-signal")
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    import seaborn as sns

    chart_theme()
    chart_map: list[dict[str, Any]] = []

    sample_rows = []
    if old_exact_count is not None:
        sample_rows.append({"cohort": "旧混合回测\n事后 exact", "trades": old_exact_count})
    for sample in ("fit", "holdout"):
        count = int(metrics.loc[metrics["sample"] == sample, "trades"].iloc[0])
        sample_rows.append({"cohort": f"独立回测\n{sample}", "trades": count})
    sample_rows.append({"cohort": "独立回测\n总样本", "trades": len(trades)})
    sample_frame = pd.DataFrame(sample_rows)
    fig, ax = plt.subplots(figsize=(9.5, 4.8))
    palette = [COLORS["orange"]["light"]] + [COLORS["blue"]["light"], COLORS["blue"]["base"], COLORS["blue"]["dark"]]
    palette = palette[-len(sample_frame) :]
    sns.barplot(data=sample_frame, x="cohort", y="trades", hue="cohort", palette=palette, legend=False, ax=ax, edgecolor=TOKENS["ink"], linewidth=1)
    for patch, value in zip(ax.patches, sample_frame["trades"]):
        ax.text(patch.get_x() + patch.get_width() / 2, patch.get_height() + max(sample_frame["trades"]) * 0.025, f"{value}", ha="center", va="bottom", fontsize=9)
    ax.set(xlabel="", ylabel="交易笔数")
    add_chart_header(fig, ax, "子信号样本量", "exact 底分型确认 long；独立运行先过滤再建仓，fit 与 holdout 互斥")
    save_figure(fig, output_dir / "sample_size.png")
    chart_map.append({"section": "sample", "type": "bar", "file": "sample_size.png", "takeaway": "独立运行扩大 exact 子信号样本"})

    exits = trades["exit_reason"].value_counts().rename_axis("exit_reason").reset_index(name="trades")
    exits = exits.sort_values("trades")
    fig, ax = plt.subplots(figsize=(9.5, 4.8))
    tones = ["xlight", "light", "base", "dark"]
    exit_palette = {
        name: COLORS["orange"][tones[index % len(tones)]]
        for index, name in enumerate(exits["exit_reason"])
    }
    sns.barplot(data=exits, x="trades", y="exit_reason", hue="exit_reason", palette=exit_palette, legend=False, ax=ax, edgecolor=TOKENS["ink"], linewidth=1)
    ax.set(xlabel="交易笔数", ylabel="")
    add_chart_header(fig, ax, "实际退出原因", f"n={len(trades)}；共享状态机依次检查止损、2R、1R protection")
    save_figure(fig, output_dir / "exit_reasons.png")
    chart_map.append({"section": "exit", "type": "ranked bar", "file": "exit_reasons.png", "takeaway": "算法赢单是否由 protection 退出主导"})

    reach = pd.DataFrame(
        {
            "target": ["1R", "1.5R", "2R"],
            "rate": [
                bool_series(trades["hit_1r_before_initial_stop"]).mean(),
                bool_series(trades["hit_1_5r_before_initial_stop"]).mean(),
                bool_series(trades["hit_2r_before_initial_stop"]).mean(),
            ],
        }
    )
    fig, ax = plt.subplots(figsize=(9.5, 4.8))
    sns.barplot(data=reach, x="target", y="rate", hue="target", palette={"1R": COLORS["blue"]["base"], "1.5R": COLORS["blue"]["light"], "2R": COLORS["blue"]["xlight"]}, legend=False, ax=ax, edgecolor=COLORS["blue"]["dark"], linewidth=1)
    ax.yaxis.set_major_formatter(mticker.PercentFormatter(1.0))
    ax.set(xlabel="固定目标", ylabel="先于初始止损触达的交易占比", ylim=(0, min(1.0, reach["rate"].max() * 1.25 + 0.05)))
    for patch, value in zip(ax.patches, reach["rate"]):
        ax.text(patch.get_x() + patch.get_width() / 2, patch.get_height() + 0.015, f"{value:.1%}", ha="center", va="bottom", fontsize=9)
    add_chart_header(fig, ax, "固定目标触达率", "同一批信号、96 根 15m 窗口、同根 K 线按初始止损优先")
    save_figure(fig, output_dir / "target_reach.png")
    chart_map.append({"section": "target", "type": "bar", "file": "target_reach.png", "takeaway": "区分目标过远与进场质量不足"})

    fig, ax = plt.subplots(figsize=(9.5, 4.8))
    ax.axvline(0, color=TOKENS["ink"], linestyle=":", linewidth=1)
    labels = [f"{value:g}R" for value in counterfactual["reward_risk"]]
    x = counterfactual["expectancy"].to_numpy()
    xerr = np.vstack([x - counterfactual["expectancy_ci_low"].to_numpy(), counterfactual["expectancy_ci_high"].to_numpy() - x])
    ax.errorbar(x, labels, xerr=xerr, fmt="o", color=COLORS["gold"]["base"], markeredgecolor=COLORS["gold"]["dark"], capsize=4, linewidth=1)
    ax.xaxis.set_major_formatter(mticker.PercentFormatter(1.0))
    ax.set(xlabel="每笔净期望收益（周分块 bootstrap 95% 区间）", ylabel="")
    add_chart_header(fig, ax, "固定止损/目标的路径反事实", "保持信号入场不变；到目标或初始止损，否则在 96 根结束收盘，已扣双边成本")
    save_figure(fig, output_dir / "counterfactual_rr.png")
    chart_map.append({"section": "counterfactual", "type": "dot interval", "file": "counterfactual_rr.png", "takeaway": "判断 1.5R/2R 延长持有是否改善期望"})

    protection = trades[trades["exit_reason"] == "protection_reached"].copy()
    protection = protection.sort_values("horizon_mfe_r", ascending=False).head(5)
    fig, ax = plt.subplots(figsize=(10.5, 5.8))
    families = [COLORS[name] for name in ("blue", "orange", "olive", "gold", "pink")]
    for (_, row), family in zip(protection.iterrows(), families):
        path = parse_r_path(row.get("horizon_close_r_path"))
        if not path:
            continue
        exit_bar = max(0, min(int(row["bars_held"]), len(path) - 1))
        label = f"{row['symbol']} / {row['sample']}"
        ax.plot(range(exit_bar + 1), path[: exit_bar + 1], color=family["base"], linewidth=1, label=label)
        if exit_bar + 1 < len(path):
            ax.plot(range(exit_bar, len(path)), path[exit_bar:], color=family["base"], linewidth=1, linestyle="--")
        ax.scatter([exit_bar], [1.0], color=family["dark"], s=24, zorder=4)
    for level in (0, 1, 1.5, 2):
        ax.axhline(level, color=TOKENS["ink"] if level == 0 else TOKENS["grid"], linestyle=":" if level else "-", linewidth=1)
    ax.set(xlabel="入场后 15m K 线数", ylabel="收盘价相对入场的 R")
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.01), frameon=False, ncol=3, borderaxespad=0)
    add_chart_header(fig, ax, "典型 protection 赢单的价格路径", "实线为实际持仓期，深色点为 1R 强制退出；虚线为平仓后至 96 根结束的真实市场路径")
    save_figure(fig, output_dir / "winner_paths.png")
    chart_map.append({"section": "paths", "type": "multi-series line", "file": "winner_paths.png", "takeaway": "观察 1R 平仓后是否继续扩展到 1.5R/2R"})
    return chart_map


def html_table(frame: pd.DataFrame, columns: list[str], formatters: dict[str, Any]) -> str:
    rows = []
    for record in frame[columns].to_dict("records"):
        cells = []
        for column in columns:
            value = record[column]
            if column in formatters:
                value = formatters[column](value)
            cells.append(f"<td>{html.escape(str(value))}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    header = "".join(f"<th>{html.escape(column)}</th>" for column in columns)
    return f"<table><thead><tr>{header}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def pct(value: Any) -> str:
    return f"{float(value):.2%}"


def build_report(
    output_dir: Path,
    trades: pd.DataFrame,
    metrics: pd.DataFrame,
    counterfactual: pd.DataFrame,
    old_exact_count: int | None,
    gates: dict[str, bool],
    symbol_breadth: float,
    concentration: float,
) -> str:
    holdout = metrics.set_index("sample").loc["holdout"]
    exit_counts = trades["exit_reason"].value_counts()
    protection_count = int(exit_counts.get("protection_reached", 0))
    tp_count = int(exit_counts.get("take_profit", 0))
    protected = trades[trades["exit_reason"] == "protection_reached"].copy()
    protected_2r = protected[
        protected.apply(
            lambda row: numeric_bar(row.get("first_2r_bar")) is not None
            and (numeric_bar(row.get("first_initial_stop_bar")) is None or numeric_bar(row.get("first_2r_bar")) < numeric_bar(row.get("first_initial_stop_bar"))),
            axis=1,
        )
    ]
    reliable = all(gates.values())
    cf = counterfactual.set_index("reward_risk")
    if cf.loc[1.0, "expectancy"] <= 0:
        tp_diagnosis = "即使 1R 固定目标也没有正期望，首要问题更像是进场质量，而不是单纯把 2R 调低。"
    elif cf.loc[1.5, "expectancy"] > cf.loc[1.0, "expectancy"] and cf.loc[1.5, "expectancy_ci_low"] > 0:
        tp_diagnosis = "1.5R 路径期望优于 1R 且区间为正，现有 1R 立即平仓过早；应先研究真正的保护止损状态机。"
    else:
        tp_diagnosis = "1R 之后的延长持有没有稳定改善期望，说明该子信号的趋势延续弱；直接把 TP 改成 1.5R/2R 不能解决问题。"

    gate_items = "".join(
        f"<li><span class=\"badge {'pass' if passed else 'fail'}\">{'通过' if passed else '失败'}</span> {html.escape(name)}</li>"
        for name, passed in gates.items()
    )
    sample_table = html_table(
        metrics,
        ["sample", "trades", "win_rate", "win_rate_ci_low", "win_rate_ci_high", "expectancy", "expectancy_ci_low", "expectancy_ci_high", "double_cost_expectancy"],
        {name: pct for name in ["win_rate", "win_rate_ci_low", "win_rate_ci_high", "expectancy", "expectancy_ci_low", "expectancy_ci_high", "double_cost_expectancy"]},
    )
    counter_table = html_table(
        counterfactual,
        ["reward_risk", "win_rate", "expectancy", "expectancy_ci_low", "expectancy_ci_high", "target_rate", "stop_rate", "timeout_rate"],
        {name: pct for name in ["win_rate", "expectancy", "expectancy_ci_low", "expectancy_ci_high", "target_rate", "stop_rate", "timeout_rate"]},
    )
    typical = protected.sort_values("horizon_mfe_r", ascending=False).head(10).copy()
    if not typical.empty:
        typical["2R_before_stop"] = typical["hit_2r_before_initial_stop"].map(lambda value: "是" if as_bool(value) else "否")
        typical_table = html_table(
            typical,
            ["symbol", "sample", "bars_held", "pre_exit_mfe_r", "horizon_mfe_r", "horizon_close_return_pct", "2R_before_stop"],
            {"pre_exit_mfe_r": lambda value: f"{float(value):.2f}R", "horizon_mfe_r": lambda value: f"{float(value):.2f}R", "horizon_close_return_pct": pct},
        )
    else:
        typical_table = "<p>没有 protection_reached 交易。</p>"

    title = "底分型确认 Long：全市场可靠性与止盈审查"
    baseline_text = f"旧混合回测 exact 子组为 {old_exact_count} 笔；" if old_exact_count is not None else ""
    decision = "可以继续进入下一层样本外验证" if reliable else "不能晋级，保留基线且不改生产参数"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root{{--ink:#1F2430;--muted:#6F768A;--surface:#FCFCFD;--panel:#fff;--line:#E6E8F0;--blue:#A3BEFA;--orange:#F0986E}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--surface);color:var(--ink);font-family:Inter,"PingFang SC",system-ui,sans-serif}}
main{{max-width:980px;margin:0 auto;padding:44px 22px 70px}} header,section{{margin-bottom:34px}} h1{{font-size:34px;line-height:1.15;margin:0}} h2{{font-size:23px;margin:0 0 12px}} h3{{font-size:17px;margin:22px 0 8px}} p,li{{line-height:1.65}} .summary{{padding:22px 24px;background:#EAF1FE;border:1px solid #CEDFFE;border-radius:18px}} .summary ul{{margin:0;padding-left:22px}} .summary li+li{{margin-top:9px}} figure{{margin:20px 0 26px}} img{{width:100%;display:block;border:1px solid var(--line);border-radius:12px;background:#fff}} figcaption{{font-size:13px;color:var(--muted);margin-top:8px}} table{{width:100%;border-collapse:collapse;background:#fff;font-size:13px;overflow:auto}} th,td{{padding:9px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}} th:first-child,td:first-child{{text-align:left}} .callout{{padding:16px 18px;background:#FFF4C2;border:1px solid #FFEA8F;border-radius:12px}} .badge{{display:inline-block;padding:2px 8px;border-radius:99px;font-size:12px;margin-right:7px}} .pass{{background:#D8ECBD;color:#386411}} .fail{{background:#FFEDDE;color:#804126}} .gate-list{{padding-left:0;list-style:none}} .gate-list li+li{{margin-top:7px}} .mono{{font-family:"SFMono-Regular",Consolas,monospace}} @media(max-width:640px){{h1{{font-size:28px}} main{{padding:28px 14px 50px}} .table-wrap{{overflow:auto}}}}
</style></head><body><main data-report-audience="product stakeholders">
<header data-contract-section="title"><h1>{title}</h1></header>
<section class="summary" data-contract-section="executive-summary"><h2>Executive Summary</h2><ul>
<li><strong>统计结论：{decision}。</strong> {baseline_text}独立运行得到 {len(trades)} 笔，其中 holdout {int(holdout['trades'])} 笔；holdout 每笔期望 {pct(holdout['expectancy'])}，95% 区间 {pct(holdout['expectancy_ci_low'])}–{pct(holdout['expectancy_ci_high'])}。</li>
<li><strong>TP 触达率低是代码语义造成的结构性结果。</strong> 当前 {protection_count}/{len(trades)} 笔在 1R 以 <span class="mono">protection_reached</span> 立即平仓，真正 2R TP 只有 {tp_count} 笔；2R 只能在首次越过 1R 的同一根 K 线内抢先触发。</li>
<li><strong>路径审查：{html.escape(tp_diagnosis)}</strong> protection 赢单中有 {len(protected_2r)}/{max(1, protection_count)} 笔在后续 96 根内、初始止损之前达到过 2R。</li>
</ul></section>

<section data-contract-section="key-findings"><h2>独立运行扩大样本，但可靠性由留出集决定</h2>
<p>exact 子信号在信号生成后立即过滤，再独立执行持仓互斥，因此不会再被其他结构组合的持仓占位。样本变多只解决置信区间过宽的问题；是否可靠仍取决于 holdout 期望、成本压力、跨币种宽度与盈利集中度。</p>
<figure><img src="sample_size.png" alt="子信号样本量"><figcaption>fit 与 holdout 按时间切分并留有 96 根 15m 隔离。</figcaption></figure>
<div class="table-wrap">{sample_table}</div>
<h3>晋级门槛</h3><ul class="gate-list">{gate_items}</ul>
<p>正期望币种占比为 {symbol_breadth:.1%}；单一币种占正盈利的最高比例为 {concentration:.1%}。这两个数字用来防止“总体好看，其实只靠一两个币”。</p>
</section>

<section data-contract-section="exit-mechanism"><h2>1R 不是保护位，而是立即止盈</h2>
<p>共享 <span class="mono">position_manager.py</span> 的顺序是：同根 K 线先止损，再检查 2R target，最后检查 1R protection；一旦达到 protection，所有机器人和回测都会调用平仓。它没有把止损上移到盈亏平衡，也没有继续等待 TP。</p>
<figure><img src="exit_reasons.png" alt="实际退出原因"><figcaption>退出原因直接来自逐笔记录，不是根据盈亏反推。</figcaption></figure>
<figure><img src="winner_paths.png" alt="典型赢单持仓路径"><figcaption>虚线部分是实际平仓后的市场路径，只用于反事实审查，不计入当前策略收益。</figcaption></figure>
<div class="table-wrap">{typical_table}</div>
</section>

<section data-contract-section="target-diagnostics"><h2>目标倍数与进场质量必须分开判断</h2>
<p>{html.escape(tp_diagnosis)} 下图先看每个目标在初始止损前的触达率，再用同一批入场做固定目标反事实；这样不会把信号数量变化误当成止盈改善。</p>
<figure><img src="target_reach.png" alt="固定目标触达率"><figcaption>同一根 K 线同时触及止损和目标时，保守按止损优先。</figcaption></figure>
<figure><img src="counterfactual_rr.png" alt="固定止损目标反事实"><figcaption>周分块 bootstrap 2,000 次，固定种子；区间跨 0 表示无法排除无优势。</figcaption></figure>
<div class="table-wrap">{counter_table}</div>
</section>

<section data-contract-section="recommended-next-steps"><h2>Recommended Next Steps</h2><ol>
<li>不要修改生产 RSI/ADX/ATR 阈值；若子信号未通过全部门槛，直接保留基线。</li>
<li>把退出语义拆成三个明确模式做独立候选：1R 固定止盈、1R 后移至保本并继续等 2R、1R 后 ATR trailing。不要继续把立即平仓命名为“推保护”。</li>
<li>所有退出候选必须复用同一信号快照做路径级比较，再用独立回测处理不同持仓时长造成的信号互斥变化。</li>
</ol></section>

<section data-contract-section="further-questions"><h2>Further Questions</h2><p>下一步最值得回答的是：保本止损和 ATR trailing 在永久留出集上是否能提高净期望，同时不把回撤和持有时间拉高到不可接受。若 1R、1.5R、2R 的路径期望都不为正，则无需再研究退出，先回到进场质量。</p></section>

<section data-contract-section="caveats-and-assumptions"><h2>Caveats and Assumptions</h2><div class="callout"><p>这是 OKX SWAP top200 市值/流动性池的历史回测；手续费按单边 0.1%，未建模逐笔盘口冲击。路径反事实固定了原始入场集合，因此适合诊断 TP，不等同于完整组合回测。周 bootstrap 处理时间相关性，但不能消除跨币种共同市场因子的相关性。报告没有修改生产配置，也没有触发订单。</p></div></section>
</main></body></html>"""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    args = parse_args()
    run_dir = args.run_dir.expanduser().resolve()
    output_dir = (args.output_dir or run_dir / "child_signal_analysis").expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    trades = pd.read_csv(run_dir / "trades.csv")
    if trades.empty:
        raise ValueError("run has no trades")
    fee_rate = float(manifest["args"]["fee_rate"])
    metrics = pd.DataFrame(
        [
            sample_metrics(trades, "all", fee_rate=fee_rate, iterations=args.bootstrap_iterations, seed=args.seed),
            sample_metrics(trades[trades["sample"] == "fit"], "fit", fee_rate=fee_rate, iterations=args.bootstrap_iterations, seed=args.seed + 1),
            sample_metrics(trades[trades["sample"] == "holdout"], "holdout", fee_rate=fee_rate, iterations=args.bootstrap_iterations, seed=args.seed + 2),
        ]
    )
    counterfactual = counterfactual_table(trades, fee_rate=fee_rate, iterations=args.bootstrap_iterations, seed=args.seed + 10)
    symbol_returns = trades.groupby("symbol", sort=True)["return_pct"].agg(["count", "sum", "mean"]).reset_index()
    symbol_breadth = float((symbol_returns["mean"] >= 0).mean()) if len(symbol_returns) else 0.0
    positive = symbol_returns[symbol_returns["sum"] > 0]["sum"]
    concentration = float(positive.max() / positive.sum()) if len(positive) and positive.sum() > 0 else 1.0
    indexed = metrics.set_index("sample")
    gates = {
        "总样本至少 100 笔": int(indexed.loc["all", "trades"]) >= 100,
        "holdout 至少 30 笔": int(indexed.loc["holdout", "trades"]) >= 30,
        "fit 与 holdout 期望同为正": indexed.loc["fit", "expectancy"] > 0 and indexed.loc["holdout", "expectancy"] > 0,
        "holdout 周 bootstrap 95% 下界高于 0": indexed.loc["holdout", "expectancy_ci_low"] > 0,
        "双倍成本后总体仍为正期望": indexed.loc["all", "double_cost_expectancy"] > 0,
        "至少 50% 有效币种不亏": symbol_breadth > 0.5,
        "单一币种正盈利贡献不超过 25%": concentration <= 0.25,
    }

    old_exact_count: int | None = None
    if args.baseline_trades and args.baseline_trades.exists():
        baseline = pd.read_csv(args.baseline_trades)
        old_exact_count = int(
            (
                baseline["signal"].eq("long")
                & baseline["structure_text"].fillna("").eq("底分型确认")
            ).sum()
        )

    chart_map = render_charts(output_dir, trades, metrics, counterfactual, old_exact_count)
    metrics.to_csv(output_dir / "sample_metrics.csv", index=False)
    counterfactual.to_csv(output_dir / "counterfactual_rr.csv", index=False)
    symbol_returns.to_csv(output_dir / "symbol_metrics.csv", index=False)
    protection = trades[trades["exit_reason"] == "protection_reached"].sort_values("horizon_mfe_r", ascending=False).head(20)
    protection.to_csv(output_dir / "typical_winners.csv", index=False)
    analysis = {
        "reliable": all(gates.values()),
        "gates": gates,
        "old_exact_count": old_exact_count,
        "new_total_trades": len(trades),
        "symbol_breadth": symbol_breadth,
        "profit_concentration": concentration,
        "exit_reasons": trades["exit_reason"].value_counts().to_dict(),
        "bootstrap_iterations": args.bootstrap_iterations,
        "seed": args.seed,
    }
    (output_dir / "analysis_metrics.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2, default=json_default),
        encoding="utf-8",
    )
    source_notes = {
        "report_mode": "html",
        "audience": "product stakeholders",
        "source_run": str(run_dir),
        "source_manifest_sha256": sha256(run_dir / "manifest.json"),
        "source_trades_sha256": sha256(run_dir / "trades.csv"),
        "chart_map": chart_map,
        "method": "prespecified exact child signal; weekly block bootstrap; Wilson interval; fixed-entry path counterfactual",
        "omissions": {"cross_exchange": "not requested for this focused child-signal run", "production_change": "intentionally not performed"},
        "required_structure_map": {
            "title": "title",
            "executive_summary": "executive-summary",
            "key_findings": ["key-findings", "exit-mechanism", "target-diagnostics"],
            "recommended_next_steps": "recommended-next-steps",
            "further_questions": "further-questions",
            "caveats": "caveats-and-assumptions",
        },
    }
    (output_dir / "source_notes.json").write_text(
        json.dumps(source_notes, ensure_ascii=False, indent=2, default=json_default),
        encoding="utf-8",
    )
    report = build_report(output_dir, trades, metrics, counterfactual, old_exact_count, gates, symbol_breadth, concentration)
    (output_dir / "report.html").write_text(report, encoding="utf-8")
    artifact_names = [
        "report.html",
        "analysis_metrics.json",
        "source_notes.json",
        "sample_metrics.csv",
        "counterfactual_rr.csv",
        "symbol_metrics.csv",
        "typical_winners.csv",
        "sample_size.png",
        "exit_reasons.png",
        "target_reach.png",
        "counterfactual_rr.png",
        "winner_paths.png",
    ]
    hashes = {name: sha256(output_dir / name) for name in artifact_names}
    (output_dir / "artifact_hashes.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")
    print(output_dir / "report.html")


if __name__ == "__main__":
    main()
