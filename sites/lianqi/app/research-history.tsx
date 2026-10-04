"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchWithTimeout } from "./api-client.mjs";
import {
  compareResearchRuns, createLatestResearchRequest, quickResearchConclusion,
  researchConfidenceInterval, researchDateRange, researchEvidenceText, researchModelLabel,
  researchNumber, researchPercent, validationSummary,
} from "./research-evidence.mjs";

type Run = NonNullable<Parameters<typeof compareResearchRuns>[0]> & { saved_at?: number; error?: string; ok?: boolean };
type Candidate = { name: string; is_baseline?: boolean; trades?: number; expectancy?: number; linked_return?: number; comparator?: string; delta?: number; ci_low?: number; ci_high?: number; q_value?: number; validation_gate?: unknown };
type Gate = { label: string; status: string; detail?: string };
type Validation = { id: string; kind: string; title: string; status: string; conclusion?: string; started_at?: string; ended_at?: string; interval?: string; return_model?: string; metric_basis?: string; report_url?: string; gates: Gate[]; candidates: Candidate[] };
type Slot = "first" | "second";
const METRICS = { total_trades: "闭仓样本", win_rate: "胜率", expectancy: "单笔平均收益", total_return: "逐笔复合收益", max_drawdown: "闭仓曲线回撤" };
const STOP_MODES: Record<string, string> = { structure_atr: "结构 ATR 止损", atr_trailing_after_1r: "1R 后跟踪止损", structure_atr_target_only: "固定止损与目标", partial_1r_breakeven: "1R 部分退出" };
const SOURCES: Record<string, string> = { binance_usdm: "币安 USDT 永续", okx: "OKX 历史数据" };
const CANDIDATES: Record<string, string> = { baseline: "基础策略", direction_score: "方向与评分过滤", fee_buffer: "费用缓冲", fee_buffer_partial: "费用缓冲加部分退出" };
function candidateLabel(name: string) { return CANDIDATES[name] || name; }

function gateLabel(value: unknown) {
  if (value === true || value === "pass" || value === "passed") return "通过";
  if (value === false || value === "fail" || value === "failed") return "未通过";
  if (value === "not_applicable") return "不适用";
  return "待补";
}
function stopLabel(run: Run) { return STOP_MODES[String(run.params?.stop_mode)] || "止损规则待补"; }
function samples(value: unknown) { const count = researchNumber(value); return count === null ? "待补" : count.toLocaleString("zh-CN"); }
function metricValue(run: Run, key: string) {
  if (key === "total_trades") return samples(run.metrics?.[key]);
  return (researchNumber(run.metrics?.total_trades) ?? 0) > 0 ? researchPercent(run.metrics?.[key]) : "暂无样本";
}

export function CandidateTable({ candidates, metricBasis = "per_trade_net_return" }: { candidates: Candidate[]; metricBasis?: string }) {
  const [visible, setVisible] = useState(5);
  if (!candidates.length) return <p className="research-muted">候选对照数据待补。</p>;
  if (!["per_trade_net_return", "paired_complete_utc_week_return"].includes(metricBasis)) return <p className="research-muted">收益口径待确认，暂不展示候选数值。</p>;
  const pairedWeeks = metricBasis === "paired_complete_utc_week_return";
  return <>
    {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- Make the horizontal comparison region keyboard-scrollable. */}
    <div className="research-table-wrap" tabIndex={0} role="region" aria-label="同实验候选对照表，可横向滚动">
      <table className="research-table"><caption>同一实验的候选对照</caption><thead><tr><th scope="col">候选</th><th scope="col">闭仓样本</th><th scope="col">{pairedWeeks ? "拼接组合收益" : "单笔平均收益"}</th><th scope="col">{pairedWeeks ? "相对预声明对照的周收益差" : "相对基线差值"}</th><th scope="col">{pairedWeeks ? "配对周差 95% 区间" : "差值置信区间"}</th><th scope="col">校正后显著性</th><th scope="col">验证门槛</th></tr></thead>
        <tbody>{candidates.slice(0, visible).map((candidate, index) => <tr key={`${candidate.name}-${index}`}><th scope="row">{candidateLabel(candidate.name)}{candidate.is_baseline && <span className="research-baseline">基线</span>}{pairedWeeks && !candidate.is_baseline && <span className="research-comparator">对照：{candidate.comparator ? candidateLabel(candidate.comparator) : "待补"}</span>}</th><td>{samples(candidate.trades)}</td><td>{researchPercent(pairedWeeks ? candidate.linked_return : candidate.expectancy)}</td><td>{candidate.is_baseline ? "基准" : researchPercent(candidate.delta)}</td><td>{researchConfidenceInterval(candidate.ci_low, candidate.ci_high)}</td><td>{researchNumber(candidate.q_value) === null ? "待补" : candidate.q_value?.toFixed(3)}</td><td>{candidate.is_baseline ? "基准" : gateLabel(candidate.validation_gate)}</td></tr>)}</tbody>
      </table>
    </div>
    {visible < candidates.length && <button className="secondary-action research-more" onClick={() => setVisible(count => count + 5)}>再显示 5 个候选（还有 {candidates.length - visible} 个）</button>}
    <p className="research-muted">收益差值仅描述本次实验。置信区间与校正后的显著性缺失时，不推断优势。</p>
    {pairedWeeks && <p className="research-muted">拼接收益来自本次组合诊断；统计差值取完整 UTC 周的配对收益，按各候选预声明的对照计算。不同对照的差值不能直接排名。</p>}
  </>;
}

export function ValidationCard({ validation }: { validation: Validation }) {
  const summary = validationSummary(validation);
  const gates = Array.isArray(validation.gates) ? validation.gates : [];
  const candidates = Array.isArray(validation.candidates) ? validation.candidates : [];
  const reportUrl = validation.report_url?.startsWith("/reports/") ? validation.report_url : null;
  return <article className="research-validation">
    <div className="research-card-heading"><div><span className="research-kicker">{validation.kind === "formal_validation" ? "正式研究验证" : "优化诊断"}</span><h3>{validation.title}</h3></div><span className={`research-status ${summary.tone}`}>{summary.label}</span></div>
    <p className="research-range">{researchDateRange(validation.started_at, validation.ended_at)} · {validation.interval || "周期待补"} · {researchModelLabel({ return_model: validation.return_model })}</p>
    <p className="research-conclusion">{researchEvidenceText(validation.conclusion || summary.label)}</p>
    {validation.status === "candidate_passed" && !summary.passed && <p role="note" className="research-warning">验证状态与证据不完整，暂不认定通过。</p>}
    {gates.length > 0 ? <details className="research-checks"><summary>查看检验明细 · {gates.length} 项{gates.some(gate => gate.status === "fail" || gate.status === "missing") && " · 存在未通过或待补项"}</summary><ul className="research-gates">{gates.map((gate, index) => <li key={`${gate.label}-${index}`}><span className={gate.status === "fail" ? "negative" : "research-muted"}>{gateLabel(gate.status)}</span><div><strong>{gate.label}</strong>{gate.detail && <p>{researchEvidenceText(gate.detail)}</p>}</div></li>)}</ul></details> : <p className="research-muted">验证门槛证据待补。</p>}
    <CandidateTable candidates={candidates} metricBasis={validation.metric_basis} />
    {reportUrl && <a className="research-report-link" href={reportUrl} target="_blank" rel="noreferrer">查看完整研究报告 ↗</a>}
  </article>;
}

export function QuickRunSummary({ run, title }: { run: Run; title: string }) {
  return <article className="research-run-summary"><h4>{title} · {run.symbol || "币种待补"} · {run.interval || "周期待补"}</h4>
    <p>{researchDateRange(run.started_at, run.ended_at)}</p><p>{stopLabel(run)} · {researchModelLabel(run)}</p>
    <div className="stat-grid">{Object.entries(METRICS).map(([key, label]) => <div key={key}><span>{label}</span><strong>{metricValue(run, key)}</strong></div>)}</div>
    <p className="research-muted">单笔平均收益置信区间：{researchConfidenceInterval(run.metrics?.expectancy_ci_low, run.metrics?.expectancy_ci_high)}</p>
    <p className="research-muted">{quickResearchConclusion(run)}</p>
  </article>;
}

export function QuickComparison({ first, second }: { first: Run | null; second: Run | null }) {
  if (!first || !second) return <div className="research-selection-summary">
    <p className="research-muted">选择一次实验作为基准，再选一次作为对照。只有条件一致时才展示收益差值。</p>
    {first && <QuickRunSummary run={first} title="基准实验" />}{second && <QuickRunSummary run={second} title="对照实验" />}
  </div>;
  const comparison = compareResearchRuns(first, second);
  return <div className="research-comparison"><p className={`research-conclusion ${comparison.comparable ? "" : "research-warning"}`}>{comparison.conclusion}</p>
    <p className="research-range">基准：{researchDateRange(first.started_at, first.ended_at)}<br />对照：{researchDateRange(second.started_at, second.ended_at)}</p>
    {!comparison.comparable ? <><ul className="research-reasons">{comparison.reasons.map(reason => <li key={reason}>{reason}</li>)}</ul><div className="research-summary-grid">{[{ run: first, title: "基准实验" }, { run: second, title: "对照实验" }].map(({ run, title }) => <article className="research-run-summary" key={title}><h4>{title} · {run.symbol || "币种待补"} · {run.interval || "周期待补"}</h4><p>{stopLabel(run)} · {researchModelLabel(run)}</p><p className="research-muted">{quickResearchConclusion(run)}</p></article>)}</div></> : <>
      <p className="research-muted">已对齐保存的币种、周期、日期、来源、配置和执行条件；止损方式可以作为对照变量。原始行情数据的一致性仍待核验。</p>
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- Make the horizontal comparison region keyboard-scrollable. */}
      <div className="research-table-wrap" tabIndex={0} role="region" aria-label="快速实验对照表，可横向滚动"><table className="research-table"><caption>{first.symbol} · {first.interval} · {researchModelLabel(first)}</caption><thead><tr><th scope="col">指标</th><th scope="col">基准实验</th><th scope="col">对照实验</th><th scope="col">观察差值</th></tr></thead><tbody>
        <tr><th scope="row">止损方式</th><td>{stopLabel(first)}</td><td>{stopLabel(second)}</td><td>对照变量</td></tr>
        {Object.entries(METRICS).map(([key, label]) => <tr key={key}><th scope="row">{label}</th><td>{metricValue(first, key)}</td><td>{metricValue(second, key)}</td><td>{key === "total_trades" ? "—" : researchPercent(comparison.delta?.[key])}</td></tr>)}
        <tr><th scope="row">单笔平均收益置信区间</th><td>{researchConfidenceInterval(first.metrics?.expectancy_ci_low, first.metrics?.expectancy_ci_high)}</td><td>{researchConfidenceInterval(second.metrics?.expectancy_ci_low, second.metrics?.expectancy_ci_high)}</td><td>待配对验证</td></tr>
      </tbody></table></div>
      <p className="research-muted">差值为对照减去基准，单位是百分点；回撤下降仅描述已闭仓曲线。未模拟持仓期间净值、资金费和独立滑点。</p>
    </>}
  </div>;
}

export default function ResearchHistory({ refreshKey = 0 }: { refreshKey?: number }) {
  const [runs, setRuns] = useState<Run[]>([]), [validations, setValidations] = useState<Validation[]>([]);
  const [runsError, setRunsError] = useState(""), [evidenceError, setEvidenceError] = useState("");
  const [runsLoading, setRunsLoading] = useState(true), [evidenceLoading, setEvidenceLoading] = useState(true);
  const [visibleRuns, setVisibleRuns] = useState(5), [visibleValidations, setVisibleValidations] = useState(2);
  const [selected, setSelected] = useState<Record<Slot, string>>({ first: "", second: "" });
  const [details, setDetails] = useState<Record<Slot, Run | null>>({ first: null, second: null });
  const [detailErrors, setDetailErrors] = useState<Record<Slot, string>>({ first: "", second: "" });
  const [detailLoading, setDetailLoading] = useState<Record<Slot, boolean>>({ first: false, second: false });
  const requests = useRef(createLatestResearchRequest());

  const loadRuns = useCallback(async () => {
    const token = requests.current.begin("runs");
    setRunsLoading(true); setRunsError("");
    try {
      const response = await fetchWithTimeout("/api/research-runs");
      if (!response.ok) throw new Error("实验列表读取失败，请重试。");
      const data = await response.json() as { runs?: Run[] };
      if (!Array.isArray(data.runs)) throw new Error("实验列表格式异常，请重试。");
      if (requests.current.isCurrent("runs", token)) setRuns(data.runs);
    } catch (error) { if (requests.current.isCurrent("runs", token)) setRunsError(error instanceof Error ? error.message : "实验列表读取失败。"); }
    finally { if (requests.current.isCurrent("runs", token)) setRunsLoading(false); }
  }, []);
  const loadEvidence = useCallback(async () => {
    const token = requests.current.begin("evidence");
    setEvidenceLoading(true); setEvidenceError("");
    try {
      const response = await fetchWithTimeout("/api/research-evidence");
      if (!response.ok) throw new Error("研究证据读取失败，请重试。");
      const data = await response.json() as { validations?: Validation[]; scope?: string };
      if (!Array.isArray(data.validations) || data.scope !== "RESEARCH_ONLY") throw new Error("研究证据范围或格式异常，暂不展示验证结论。");
      if (requests.current.isCurrent("evidence", token)) setValidations(data.validations);
    } catch (error) { if (requests.current.isCurrent("evidence", token)) setEvidenceError(error instanceof Error ? error.message : "研究证据读取失败。"); }
    finally { if (requests.current.isCurrent("evidence", token)) setEvidenceLoading(false); }
  }, []);
  useEffect(() => { const timer = window.setTimeout(() => { void loadRuns(); }, 0); const gate = requests.current; return () => { window.clearTimeout(timer); gate.begin("runs"); }; }, [loadRuns, refreshKey]);
  useEffect(() => { const timer = window.setTimeout(() => { void loadEvidence(); }, 0); const gate = requests.current; return () => { window.clearTimeout(timer); for (const key of ["evidence", "first", "second"]) gate.begin(key); }; }, [loadEvidence]);

  async function selectRun(slot: Slot, id: string) {
    const token = requests.current.begin(slot);
    setSelected(current => ({ ...current, [slot]: id }));
    setDetails(current => ({ ...current, [slot]: null })); setDetailErrors(current => ({ ...current, [slot]: "" }));
    setDetailLoading(current => ({ ...current, [slot]: !!id }));
    if (!id) return;
    try {
      const response = await fetchWithTimeout(`/api/research-runs/${encodeURIComponent(id)}`);
      if (!response.ok) throw new Error("实验详情读取失败，请重试。");
      const run = await response.json() as Run;
      if (run.run_id !== id || run.error || run.ok === false) throw new Error(run.error || "实验详情不完整，请重新选择。");
      if (requests.current.isCurrent(slot, token)) setDetails(current => ({ ...current, [slot]: run }));
    } catch (error) { if (requests.current.isCurrent(slot, token)) setDetailErrors(current => ({ ...current, [slot]: error instanceof Error ? error.message : "实验详情读取失败。" })); }
    finally { if (requests.current.isCurrent(slot, token)) setDetailLoading(current => ({ ...current, [slot]: false })); }
  }

  const availableRuns = runs.filter(run => run.run_id && !run.error && run.ok !== false);
  return <div className="research-history">
    <section className="content-panel"><div className="section-heading"><div><h2>研究结论与验证证据</h2><p>先看候选是否通过验证，再看同一实验的对照。研究结果不会自动修改运行规则。</p></div><button className="secondary-action" disabled={evidenceLoading} onClick={() => void loadEvidence()}>{evidenceLoading ? "读取中…" : "刷新证据"}</button></div>
      {evidenceError && <p role="alert" className="research-warning">{evidenceError}<button className="secondary-action" disabled={evidenceLoading} onClick={() => void loadEvidence()}>重试</button></p>}
      {!evidenceError && !evidenceLoading && validations.length === 0 && <div className="empty-state">暂无研究验证证据；快速诊断不能替代正式验证。</div>}
      {!evidenceError && validations.slice(0, visibleValidations).map(validation => <ValidationCard key={validation.id} validation={validation} />)}
      {!evidenceError && visibleValidations < validations.length && <button className="secondary-action research-more" onClick={() => setVisibleValidations(count => count + 2)}>查看更多研究（还有 {validations.length - visibleValidations} 项）</button>}
      <p className="research-muted">所有结论限于研究用途；通过研究验证也不代表未来收益保证。</p>
    </section>
    <section className="content-panel"><div className="section-heading"><div><h2>快速实验对照</h2><p>选择两次条件一致的实验，观察止损方式等诊断结果。</p></div><button className="secondary-action" disabled={runsLoading} onClick={() => void loadRuns()}>{runsLoading ? "读取中…" : "刷新实验"}</button></div>
      {runsError && <p role="alert" className="research-warning">{runsError}<button className="secondary-action" disabled={runsLoading} onClick={() => void loadRuns()}>重试</button></p>}
      {!runsLoading && !runsError && !runs.length && <div className="empty-state">暂无快速实验；完成一次快速诊断后，结果会自动归档。</div>}
      {availableRuns.length > 0 && <div className="research-selectors">{(["first", "second"] as Slot[]).map(slot => <label key={slot}>{slot === "first" ? "基准实验" : "对照实验"}<select value={selected[slot]} onChange={event => void selectRun(slot, event.target.value)}><option value="">请选择实验</option>{availableRuns.map(run => <option key={run.run_id} value={run.run_id}>{run.symbol} · {run.interval} · {run.saved_at ? new Date(run.saved_at * 1000).toLocaleString("zh-CN", { hour12: false }) : "保存日期待补"}</option>)}</select></label>)}</div>}
      {(["first", "second"] as Slot[]).map(slot => <div key={slot}>{detailLoading[slot] && <p role="status">正在读取{slot === "first" ? "基准" : "对照"}实验…</p>}{detailErrors[slot] && <p role="alert" className="research-warning">{detailErrors[slot]}<button className="secondary-action" onClick={() => void selectRun(slot, selected[slot])}>重试详情</button></p>}</div>)}
      {!details.first && !details.second && !detailLoading.first && !detailLoading.second && availableRuns[0] && <QuickRunSummary run={availableRuns[0]} title="最近一次快速诊断" />}
      <QuickComparison first={details.first} second={details.second} />
      {runs.length > 0 && <details className="research-archive"><summary>查看实验记录（{runs.length} 条）</summary><ul className="research-run-list">{runs.slice(0, visibleRuns).map((run, index) => <li key={run.run_id || index}><div><strong>{run.error || `${run.symbol || "币种待补"} · ${run.interval || "周期待补"}`}</strong>{!run.error && <><p>{researchDateRange(run.started_at, run.ended_at)}</p><span>{SOURCES[run.market_data_source || ""] || "数据来源待补"} · {researchModelLabel(run)}</span></>}</div>{run.run_id && !run.error && run.ok !== false && <div className="research-run-actions"><button className="secondary-action" aria-pressed={selected.first === run.run_id} onClick={() => void selectRun("first", run.run_id || "")}>作为基准</button><button className="secondary-action" aria-pressed={selected.second === run.run_id} onClick={() => void selectRun("second", run.run_id || "")}>作为对照</button></div>}</li>)}</ul>{visibleRuns < runs.length && <button className="secondary-action research-more" onClick={() => setVisibleRuns(count => count + 5)}>再显示 5 条（还有 {runs.length - visibleRuns} 条）</button>}</details>}
    </section>
  </div>;
}
