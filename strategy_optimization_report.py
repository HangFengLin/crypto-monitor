"""Audit-only postprocessing for the archived 2026-10-03 optimization run.

The narrative includes fixed findings from that archive. Use this script only
with its original inputs; other experiments require their own report narrative.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
from collections import defaultdict
from pathlib import Path


def event_concurrency(fills):
    active, maximum, long_max, short_max = {}, 0, 0, 0
    for fill in fills:
        key = (fill["symbol"], fill["setup_id"])
        quantity = fill["quantity"]
        if fill["kind"] == "entry":
            if key in active:
                raise ValueError("duplicate active setup")
            active[key] = [quantity, fill["direction"], quantity]
        else:
            if key not in active or quantity - active[key][0] > active[key][2] * 1e-10:
                raise ValueError("exit without sufficient remaining quantity")
            active[key][0] -= quantity
            if abs(active[key][0]) <= active[key][2] * 1e-10:
                del active[key]
        maximum = max(maximum, len(active))
        long_max = max(long_max, sum(value[1] == "long" for value in active.values()))
        short_max = max(short_max, sum(value[1] == "short" for value in active.values()))
    if active:
        raise ValueError("unclosed quantities in completed fold")
    return dict(max_positions_event_order=maximum, max_long_positions_event_order=long_max,
                max_short_positions_event_order=short_max)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    directory = args.output
    comparison = json.loads((directory / "comparison.json").read_text())
    json.loads((directory / "experiment_registry.json").read_text())
    json.loads((directory / "fold_metrics.json").read_text())
    accounting = json.loads((directory / "accounting_summary.json").read_text())
    supplemental, totals = [], defaultdict(list)
    for path in sorted((directory / "portfolio_replays").rglob("*.json.gz")):
        with gzip.open(path, "rt") as handle:
            result = json.load(handle)
        name = path.name.removesuffix(".json.gz")
        metrics = event_concurrency(result["fills"])
        entry_cash = result["parameters"]["initial_equity"]
        net_pnl = sum(t["net_pnl"] for t in result["trades"])
        reconciled = math.isclose(result["equity"][-1]["equity"] - entry_cash, net_pnl, abs_tol=1e-7)
        if not reconciled:
            raise ValueError("closed-trade/equity reconciliation failed")
        supplemental.append(dict(fold=path.parent.name, replay=name, accounting_reconciled=reconciled, **metrics))
        totals[name].append(metrics)
    if len(supplemental) != 108:
        raise ValueError("all 108 registered replays required")
    audit = dict(status="PASS_ENGINEERING_RECONCILIATION_ONLY", replays=len(supplemental),
                 sampling_boundary="fills append order for concurrency; bar-close samples for drawdown/gross/risk; daily samples for correlation",
                 input_binding_boundary="post-run file bindings; input price hashes frozen in registry and currently rechecked; funding hashes crosschecked before PnL",
                 rows=supplemental)
    (directory / "execution_reconciliation.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n")
    labels = {"baseline": "工程基线", "risk_only": "仅加风险限制", "V1_entry_only": "V1 仅改入场",
              "V1_with_risk": "V1 入场加风险", "X1_exit_only": "X1 仅改退出", "X1_with_risk": "X1 退出加风险"}
    base = {r["variant"]: r for r in comparison["summary"] if r["stress"] == "base"}
    lines = ["# 策略优化研究：2026-10-03", "", "**结论：本地工程修复和完整诊断已完成，策略选择为 `NO_SELECTION / RESEARCH_ONLY`。**",
             "风险限制降低了这份固定样本的回撤，但没有改变亏损结论；X1 部分退出整体更差。V1 只有 15 笔，主要通过减少参与改善相对亏损基线，缺少自身盈利优势的证据。两个候选都未默认启用，未部署、重启服务或下单。", "",
             "## 已完成的工程改动", "",
             "新纸面记录固定 `linear_usdm_v1`，收益以入场名义本金计量，开仓费与实际平仓名义本金费用分别扣除；旧缺少版本的记录继续使用 `legacy_ratio_v1`。胜负分类、浮盈和费后保本价统一到各记录版本。混合账本在 API 和页面明确提示，并分别显示闭仓数和单笔期望。历史账本没有重写。",
             f"本轮截取的 202 笔保留记录固定原入/出价和费率后，旧单笔均值 {accounting['mean_legacy_return']:.4%}，线性重算均值 {accounting['mean_linear_return']:.4%}。这只是会计差异，未加入资金费、滑点、杠杆或组合权益。逐笔见 [accounting_difference.csv](accounting_difference.csv)。",
             "修复同一顶背驰反复重置 `pending_sell` 等待时钟的问题。V1 的有序量价候选与 X1 的半仓退出均用独立测试验证，组合回放复用生产信号引擎与共享持仓生命周期。", "",
             "## 相近策略与固定候选", "",
             "参考了 CZSC 顺序结构/量价事件、Jesse 的 MACD+EMA 过滤、RSI 背离和 Freqtrade 的部分退出数量管理。参考机制不提供盈利证据；具体代码版本、链接与差异见 [参考审查](../../docs/STRATEGY_REFERENCE_REVIEW_20261003.md)。",
             "V1：已知背驰后，先锁定严格 RVOL20≤0.75 的反向回撤，再在 1–3 根内等待 RVOL20≥1.5、实体方向与收盘突破；原过滤仍须通过，失败/超时/结构破坏取消，B 收盘后下一根开盘成交。没有扩大阈值或为补成交放宽条件。",
             "X1：第一次 1R 退出初始数量一半，余仓收紧到线性费后保本、保留 2R 和 96 根期限。未知盘中先后用止损优先；开盘已知触及先记成交，开盘减仓后的保护可以用于后续区间。盘中首次触及的新止损仅下一根生效。有利跳空不赚额外价差，不利止损跳空按更差开盘执行。", "",
             "## 输入、执行与证据边界", "",
             "冻结 2026-06-20 的 44 币 Binance 池。所有键都保留，缺少上市前/末尾观测明确审计，无价格填补；当前池含历史幸存者/上市偏差，不能当 PIT 历史选币。",
             "5 个独立 60 天验证窗采用原 240 天训练期架构、250 根预热及 96 根边界隔离，规则没有在训练期拟合；另单列旧最终尾部。它们全是已曝光诊断样本，旧尾部标 `HOLDOUT_EXPOSED`，不是新盲测。",
             "基线与 X1 共享同一完整入场事件流；V1 完整重新生成。先保存信号，再处理容量/持仓，不复用旧闭仓子集。验证首根开盘保留实际前一根已收盘 bar 作为依据。",
             "共同假设：初始研究权益 10000，每笔初始费后止损风险 0.5%，一倍总敞口上限，单边手续费 0.1%，每次不利滑点 2bps，持仓最多 96 根。宽松基线允许同币重叠、没有仓数上限；风险方案最多 5 仓、同币不重叠、总风险 2.5%、多头 2.5%、空头 1%。这些是研究资金假设，不能复原缺少数量/权益账本的线上纸面账户。",
             "**空头 1% 是风险金额上限，不是最多两仓。** 初始风险减小后可容纳更多剩余仓位。跳空、费用和权益变化也会使事后比率偏离入场上限，不能解释为损失保证。",
             "44 币共 107,363 条新旧资金费时间/费率逐值一致；新资料保留实际 markPrice，旧缓存没有 markPrice 对照。整点结算只计已有仓；盘中退出时先后未知，支出按较大已持数量、收入仅计确定存续数量。OI 来源的 available_at 链不完整，且旧覆盖按基线事件挑取；本轮不依赖 score 排序/最低分入场，仍保留 PIT 缺口。", "",
             "## 基础成本结果", "",
             "下表的串接净收益是 6 个独立重置资金窗口净收益的乘积；间隔不持仓，不能称用户真实账户收益。回撤是各窗收盘权益的最差观测。并发按逐次成交事件顺序重建，包含同一 bar 内开平仓。", "",
             "| 方案 | 闭仓笔数 | 正收益窗 | 诊断串接净收益 | 最差窗收盘回撤 | 事件峰值总仓/空仓 |", "|---|---:|---:|---:|---:|---:|"]
    for row in comparison["summary"]:
        if row["stress"] != "base":
            continue
        peaks = totals[f"{row['variant']}_base"]
        total_peak = max(p["max_positions_event_order"] for p in peaks)
        short_peak = max(p["max_short_positions_event_order"] for p in peaks)
        lines.append(f"| {labels[row['variant']]} | {row['trades']} | {row['positive_folds']}/6 | {row['linked_return']:.2%} | {row['worst_fold_drawdown']:.2%} | {total_peak}/{short_peak} |")
    lines += ["", f"风险限制使最差窗口收盘回撤从 {base['baseline']['worst_fold_drawdown']:.2%} 降至 {base['risk_only']['worst_fold_drawdown']:.2%}，仍持续亏损。V1 相比基线少约 {1-base['V1_entry_only']['trades']/base['baseline']['trades']:.1%} 闭仓，不能把少参与亏损称作稳定交易优势；旧暴露尾部仅 6 笔且自身资金 PnL 为负。X1 对应宽松/风险对照均更差。", "",
              "## 压力与统计", "", "| 方案 | 加倍成本串接净收益 | 延迟一根串接净收益 |", "|---|---:|---:|"]
    for name in labels:
        doubled = next(r for r in comparison["summary"] if r["variant"] == name and r["stress"] == "double_cost")
        delayed = next(r for r in comparison["summary"] if r["variant"] == name and r["stress"] == "delay_one_bar")
        lines.append(f"| {labels[name]} | {doubled['linked_return']:.2%} | {delayed['linked_return']:.2%} |")
    lines += ["", "加倍成本为手续费 0.2%/滑点 4bps，延迟场景为原成本加一根延迟。全部重新按固定风险预算定仓、重放生命周期/容量；不是固定数量的简单费用扣减。V1 加倍成本后只剩约 +0.03%，依然只有 15 笔。", "",
              "完整 UTC 周合并跨窗片段，排除 3 个残周；62 周分成连续的 42/20 周两段。2000 次、种子 20260620、两周 moving block 不跨未观测间隔、不首尾环绕。两项主比较使用相同风险限制的对照，BH-FDR 只覆盖这两个新候选，不覆盖以前研究和币池选择。", "",
              "| 比较（候选减风险对照） | 周净收益差 | 95% 分块区间 | q |", "|---|---:|---:|---:|"]
    for row in comparison["comparisons"]:
        lines.append(f"| {labels[row['candidate']]} | {row['delta']:.3%} | [{row['ci_low']:.3%}, {row['ci_high']:.3%}] | {row['q_value']:.6f} |")
    lines += ["", "V1 的相对差额显著主要说明其避免了很多基线亏损；15 笔不能证明它自身有正期望。X1 周差额区间整体为负。没有新独立留出、当前 PIT 币池/OI 完整证据或 OKX 确认，现有最终测试至少 100 笔门槛也未满足；不生成晋级配置。", "",
              "相关暴露仅按 UTC 日末、过去 30 日收益且至少 20 个共同观测、相关系数≥0.7 的同方向持仓观察。预热/新上市的历史不足会出现未知；零值不能当无共振风险，日末观察也不能代替盘中峰值。逐币、方向、周、折、拒绝原因均保存。", "",
              "模型采用连续数量，没有交易所最小名义本金、数量步长或订单成交队列；不能称可直接执行的订单级回测。前两轮分别发现满敞口后的浮点残差、多头重复预留滑点而拆出极小仓位，已保留于 `preliminary_numerical_capacity_defect` / `preliminary_duplicate_long_slippage_reserve`。在冻结信号/候选/风险参数不变的情况下修正现金权益预算并完整重跑；本报告只使用第三轮最终结果。最终 108 个场景没有低于 1e-6 美元名义本金的数值残单；这不是交易所最小金额约束。", "",
              "## 验证与后续", "",
              "交付核对见 [verification.json](verification.json)，真实输入/执行哈希见 [experiment_registry.json](experiment_registry.json)、[execution_snapshot.json](execution_snapshot.json)、[input_bindings_before_corrected_pnl.json](input_bindings_before_corrected_pnl.json) 和 [artifact_hashes.json](artifact_hashes.json)。108 个回放全部通过逐次数量守恒及闭仓净损益/现金权益勾稽，证明实现一致性，不证明策略盈利。",
              "保留工程修复；V1 继续关闭，X1 不启用；风险模块保留为离线研究接口。下一步应固定候选收集真正未曝光的前向样本，再按原成本/样本数/PIT/跨市场门槛验证。当前样本不再为补交易数扩搜参数。未授权的交易、部署和服务重启均未执行。", "",
              "复现流程：`strategy_optimization.py --source-root <原数据目录> --output <研究目录> --signals-only` → `--prepare-predecessors --signals-only` → `--replay-only`；信号任务可 `--resume-signals`，不得覆盖既有冻结注册。资金费通过 `research_funding_data.collect_funding` 从公开接口单独取证、交叉核对后放入 `funding_evidence`；当前研究目录保存了实际取证文件。回放代码哈希漂移会拒绝复用执行快照，应新建诊断目录。", ""]
    (directory / "REPORT.md").write_text("\n".join(lines))
    print(json.dumps({"report": str(directory / "REPORT.md"), "replays_reconciled": len(supplemental)}))


if __name__ == "__main__":
    main()
