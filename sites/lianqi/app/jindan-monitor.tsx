"use client";

import { useEffect, useState } from "react";
import { fetchWithTimeout } from "./api-client.mjs";
import { createRefreshLoop } from "./refresh-loop.mjs";

type Row = Record<string, unknown>;
type Snapshot = Row & {
  availability?: string; counts?: Row | null; per_symbol?: Record<string, Row> | null;
  boundaries?: Row | null; clock?: Row | null; storage?: Row | null; capabilities?: Row | null; readiness_gaps?: string[]; events?: Row[]; errors?: Row[];
};
type Research = Row & { main_metrics?: Array<Row & { BASE?: Row | null; DOUBLE?: Row | null }> };
const SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"];
const PHASE: Record<string, string> = {
  WAITING_CAPTURE: "等待采集起点", COMMISSIONING: "采集预检", FEATURE_INITIALIZATION: "特征与状态预热",
  CAUSAL_HISTORY_WARMUP: "因果历史预热", AWAITING_SCORE_PREFLIGHT: "等待评分预检", OBSERVATION: "前瞻观察",
  LABEL_MATURATION: "既有标签成熟", COMPLETED: "本窗口已结束", ENGINEERING_HALT: "工程暂停",
};
const AVAILABILITY: Record<string, string> = {
  available: "快照可用", missing: "尚无采集快照", unavailable: "采集快照暂不可读",
  invalid: "采集快照无效", stale: "快照已过期",
};
function metric(value: unknown, digits = 0): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "暂无可靠数据";
}
function percent(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? `${(value * 100).toFixed(2)}%` : "—";
}
function stamp(value: unknown, seconds = false): string {
  if (value === null || value === undefined || value === "") return "尚未记录";
  const date = typeof value === "number" ? new Date(value * (seconds ? 1000 : 1)) : new Date(String(value));
  return Number.isFinite(date.getTime()) ? date.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "尚未记录";
}
function text(value: unknown, fallback = "未知"): string { return typeof value === "string" && value ? value : fallback; }

export function JindanStatusPanel({ status, research, error = "", researchError = "" }: {
  status: Snapshot | null; research?: Research | null; error?: string; researchError?: string;
}) {
  const availability = error ? "unavailable" : status?.availability || "missing";
  const stale = availability !== "available" || Boolean(error);
  const boundaries = status?.boundaries;
  const boundaryViolation = Boolean(boundaries && Object.values(boundaries).some(value => value === true));
  const counts = status?.counts;
  const events = Array.isArray(status?.events) ? status.events.slice(0, 50) : [];
  const researchAvailable = research?.availability === "available";
  const researchRows = researchAvailable && Array.isArray(research?.main_metrics) ? research.main_metrics : [];
  return <div className="jindan-monitor">
    <section className="content-panel">
      <div className="section-heading"><div><p className="overline">JINDAN · PUBLIC MARKET OBSERVER</p><h2>金丹v1.0 · 仅观察，不下单</h2></div><span className={stale ? "warning" : "neutral"}>{AVAILABILITY[availability] || "状态未知"}</span></div>
      <p className="jindan-intro">记录价格冲击与主动成交方向背离。当前只检验实时可观察性，原策略尚未通过验收；2026年1—8月历史最终检验段继续锁定。</p>
      {error && <p role="alert" className="warning">{error}；下方若有数据，仅为上次快照。</p>}
      {boundaryViolation && <p role="alert" className="negative">边界异常：采集快照报告了启用权限，需核查服务隔离。</p>}
      <div className="stat-grid jindan-stats">
        <div><span>采集阶段</span><strong className="jindan-label">{PHASE[text(status?.phase)] || "暂无可靠数据"}</strong></div>
        <div><span>进程状态</span><strong className="jindan-label">{text(status?.operational_status)}</strong></div>
        <div><span>快照更新时间 · 北京时间</span><strong className="jindan-label">{stamp(status?.updated_at, true)}</strong></div>
        <div><span>快照距今 / 秒</span><strong>{metric(status?.age_seconds, 1)}</strong></div>
        <div><span>最新真实接收 · 北京时间</span><strong className="jindan-label">{stamp(status?.last_receive_ms)}</strong></div>
        <div><span>最新闭合1m起点 · 北京时间</span><strong className="jindan-label">{stamp(status?.last_closed_bar_ms)}</strong></div>
      </div>
      <p className="jindan-note">当前版本覆盖采集、事件、参考报价与恢复日志；后续标签和评分待独立验收。96日预热后进入等待预检，不会自动评分。</p>
      <div className="jindan-inline" aria-label="服务能力">{([["闭合bar采集", "raw_closed_1m"], ["增量事件", "incremental_price_flow_events"], ["参考报价", "bbo_reference_observation"], ["E2标签", "e2_labels"], ["ORIGIN标签", "origin_labels"], ["资金假设标签", "funding_cashflow_labels"], ["正式评分", "formal_scoring"]] as const).map(([label, key]) => <span key={key}>{label}：{status?.capabilities?.[key] === true ? "已提供" : status?.capabilities?.[key] === false ? "待实现或验收" : "未知"}</span>)}</div>
      {Boolean(status?.readiness_gaps?.length) && <p className="jindan-note">待验收项：{status?.readiness_gaps?.join(" · ")}</p>}
      <p className="jindan-note">快照超过120秒即视为过期。心跳、闭合bar、事件数量分别展示；无数据和零事件分别记录，期中不展示收益。</p>
    </section>

    <section className="content-panel">
      <div className="section-heading"><h2>采集覆盖与事件漏斗</h2><span>三币 · 15m / 60m</span></div>
      <div className="stat-grid jindan-stats">
        {([["PRICE事件", "price_events"], ["FLOW通过", "flow_pass"], ["FLOW未知", "flow_unknown"], ["JOINT标签", "joint_pass"], ["准时事件", "on_time_events"], ["晚到事件", "late_events"]] as const).map(([label, key]) => <div key={key}><span>{label}</span><strong>{metric(counts?.[key])}</strong></div>)}
      </div>
      <div className="jindan-table-wrap"><table className="jindan-table"><thead><tr><th>币种</th><th>闭合bar数</th><th>最新真实接收 · 北京时间</th><th>最新闭合bar起点 · 北京时间</th><th>已处理bar起点 · 北京时间</th><th>缺口</th></tr></thead><tbody>{SYMBOLS.map(symbol => {
        const row = status?.per_symbol?.[symbol];
        return <tr key={symbol}><th scope="row">{symbol}</th><td>{metric(row?.closed_bars)}</td><td>{stamp(row?.last_receive_ms)}</td><td>{stamp(row?.last_closed_bar_ms)}</td><td>{stamp(row?.processed_bar_ms)}</td><td>{metric(row?.gap_count)}</td></tr>;
      })}</tbody></table></div>
      <div className="jindan-inline"><span>累计缺口 {metric(status?.gap_count)}</span><span>未解决缺口 {metric(status?.unresolved_gap_count)}</span><span>修订 {metric(status?.revision_count)}</span><span>重连 {metric(status?.reconnect_count)}</span><span>参考报价缺失 {metric(counts?.reference_missing)}</span></div>
      <p className="jindan-note">时钟误差范围 ±{metric(status?.clock?.offset_uncertainty_ms, 1)} ms · 参考时钟{status?.reference_clock_usable === true ? "符合误差范围" : status?.reference_clock_usable === false ? "不符合误差范围" : "未核验"}。此状态不代表任一事件已满足截止时点。</p>
      <p className="jindan-note">存储配额：已用 {metric(status?.storage?.used_bytes)} / 上限 {metric(status?.storage?.limit_bytes)} bytes · 可用 {metric(status?.storage?.free_bytes)} bytes · {status?.storage?.ok === true ? "预算内" : status?.storage?.ok === false ? "预算异常" : "尚无存储证据"}</p>
      {Boolean(status?.errors?.length) && <ul className="jindan-errors" aria-label="近期采集错误">{status?.errors?.map((row, index) => <li key={index}><code>{text(row.code)}</code> · {stamp(row.at_ms)} · 次数 {metric(row.count)}</li>)}</ul>}
    </section>

    <section className="content-panel">
      <div className="section-heading"><h2>固定前瞻日历</h2><span>日历边界均按UTC冻结</span></div>
      <p className="jindan-intro">先以96个完整UTC日预热（6日特征与状态＋90日完整事件历史），后续标签和评分预检通过后才登记180日观察。低事件数、缺口和盈亏均不重置或延长窗口。</p>
      <dl className="jindan-dates">{([["实际激活", "activated_at_utc"], ["正式采集起点", "capture_start_utc"], ["特征初始化结束", "feature_initialization_end_utc"], ["预热结束", "warmup_end_utc"], ["评分起点", "score_start_utc"], ["评分终点", "score_end_utc"], ["固定标签封存", "dataset_close_utc"]] as const).map(([label, key]) => <div key={key}><dt>{label}</dt><dd>{typeof status?.[key] === "string" ? String(status[key]) : "尚未登记"}</dd></div>)}</dl>
      <p className="jindan-note">采集上线不等于评分已经开始；评分日期未登记时保持为空。资源审议底线为30个唯一主事件、20个非重叠簇、至少两币及前后两半覆盖；这些条件不替代原策略验收。</p>
    </section>

    <section className="content-panel">
      <div className="section-heading"><h2>近期PRICE / FLOW事件</h2><span>最多50条 · UNKNOWN保留</span></div>
      {events.length ? <div className="jindan-table-wrap"><table className="jindan-table"><thead><tr><th>确认时间 · 北京时间</th><th>币种 / 周期</th><th>研究方向</th><th>FLOW</th><th>JOINT</th><th>时效</th><th>参考报价</th></tr></thead><tbody>{events.map((row, index) => <tr key={`${row.event_id}-${index}`}><td>{stamp(row.confirmed_time_ms)}</td><th scope="row">{text(row.symbol)} / {metric(row.measurement_minutes)}m</th><td>{row.side === 1 ? "多" : row.side === -1 ? "空" : "未知"}</td><td>{text(row.flow_status, "UNKNOWN")}</td><td>{text(row.joint_status, "UNKNOWN")}</td><td>{text(row.on_time_status)}</td><td>{text(row.reference_status)}</td></tr>)}</tbody></table></div> : <p className="jindan-note">{availability === "available" ? "该快照暂无近期事件；是否属于真实零事件，需结合覆盖与缺口判断。" : "暂无可靠事件快照，不能据此判断市场无事件。"}</p>}
    </section>

    <section className="content-panel">
      <div className="section-heading"><h2>历史参数试验 · 与实时观察分开</h2><span>2024—2025已暴露开发数据</span></div>
      <p className="jindan-intro">仅展示独立发布的离线试验。不代表策略通过验收，不读取锁定最终段，也不把参数结果自动写入采集规则。</p>
      {researchError && <p role="alert" className="warning">{researchError}</p>}
      {!researchAvailable ? <p className="jindan-note">{research?.availability === "invalid" || research?.availability === "unavailable" ? "历史试验快照暂不可用。" : "尚未发布历史试验结果。"}</p> : <><p className="jindan-note">状态：{text(research?.status)} · 原参数复现：{text((research?.reproduction as Row | null)?.status)} · 已发布回放数 {metric(research?.cost_replays)}</p><p className="jindan-note">参数试验共同日历 {metric(research?.calendar_days)}日 · 原参数复现 {(metric((research?.reproduction as Row | null)?.calendar_days))}日；日历不同，不能直接混作同一验收。评分区间：{text((research?.bounds as Row | null)?.start_utc)} 至 {text((research?.bounds as Row | null)?.end_exclusive_utc)}（末端不含）。</p>{researchRows.length > 0 && <div className="jindan-table-wrap"><table className="jindan-table"><thead><tr><th>固定试验</th><th>FLOW阈值</th><th>最长持有 / 分钟</th><th>BASE笔数</th><th>BASE累计净收益</th><th>DOUBLE累计净收益</th><th>BASE平均净R</th></tr></thead><tbody>{researchRows.map((row, index) => <tr key={index}><th scope="row">{text(row.policy_id)}</th><td>{metric(row.flow_threshold, 2)}</td><td>{metric(row.hold_minutes)}</td><td>{metric(row.BASE?.closed_trades)}</td><td>{percent(row.BASE?.total_net_return)}</td><td>{percent(row.DOUBLE?.total_net_return)}</td><td>{metric(row.BASE?.mean_net_r, 4)}</td></tr>)}</tbody></table></div>}</>}
    </section>
  </div>;
}

export default function JindanMonitor() {
  const [status, setStatus] = useState<Snapshot | null>(null);
  const [research, setResearch] = useState<Research | null>(null);
  const [error, setError] = useState("");
  const [researchError, setResearchError] = useState("");
  useEffect(() => {
    let active = true;
    async function loadStatus(signal: AbortSignal) {
      try {
        const response = await fetchWithTimeout("/api/jindan/status", { signal, cache: "no-store" });
        if (!response.ok) throw Error("采集状态读取失败");
        const value = await response.json() as Snapshot;
        if (active && !signal.aborted) { setStatus(value); setError(""); }
      } catch { if (active && !signal.aborted) setError("采集接口暂不可用"); }
    }
    async function loadResearch(signal: AbortSignal) {
      try {
        const response = await fetchWithTimeout("/api/jindan/research", { signal, cache: "no-store" });
        if (!response.ok) throw Error("历史试验读取失败");
        const value = await response.json() as Research;
        if (active && !signal.aborted) { setResearch(value); setResearchError(""); }
      } catch { if (active && !signal.aborted) { setResearch(null); setResearchError("历史试验接口暂不可用"); } }
    }
    const live = createRefreshLoop(loadStatus, { intervalMs: 10000 });
    const historical = createRefreshLoop(loadResearch, { intervalMs: 60000 });
    void live.refresh(); void historical.refresh();
    return () => { active = false; live.stop(); historical.stop(); };
  }, []);
  return <JindanStatusPanel status={status} research={research} error={error} researchError={researchError} />;
}
