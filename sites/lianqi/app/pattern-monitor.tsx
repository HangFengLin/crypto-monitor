"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { fetchWithTimeout } from "./api-client.mjs";
import { createRefreshLoop } from "./refresh-loop.mjs";

type Stage = "forming" | "confirmed" | "invalidated" | "expired";
type Filter = Stage | "active" | "all";
type Candle = { open_time: number; close_time: number; open: number; high: number; low: number; close: number; volume?: number };
type Point = { time_ms: number; price: number; kind?: string };
export type Formation = {
  id: string; symbol: string; interval: string; pattern: string; direction: string; stage: Stage;
  origin?: string; level?: number | null; invalid_level?: number | null; reason?: string;
  detected_at_ms?: number; updated_at_ms?: number; available_at_ms?: number; bar_close_time?: number;
  points?: Point[]; chart?: Candle[];
};
export type PatternSnapshot = {
  status?: string; mode?: string; source?: string; updated_at?: number; last_scan_at?: number;
  started_at?: number; symbols?: string[]; intervals?: string[]; errors?: Array<{ symbol?: string; interval?: string; error_class?: string }>;
  patterns?: Formation[]; events?: Formation[]; counts?: Partial<Record<Stage, number>>;
};
const PHASE: Record<Stage, string> = { forming: "待确认", confirmed: "已确认", invalidated: "已失效", expired: "已到期" };
const FILTERS: Array<[Filter, string]> = [["active", "进行中"], ["all", "全部"], ["forming", "待确认"],
  ["confirmed", "已确认"], ["invalidated", "已失效"], ["expired", "已到期"]];
const POINTS: Record<string, string> = { first_bottom: "第一低点", second_bottom: "第二低点", first_top: "第一高点",
  second_top: "第二高点", neckline: "颈线", range_boundary: "区间边界", breakout: "突破", retest: "回踩", confirmation: "确认" };

function finite(value: unknown): value is number { return typeof value === "number" && Number.isFinite(value); }
function number(value: unknown): string {
  return finite(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: Math.abs(value) >= 100 ? 2 : 6 }) : "尚未提供";
}
function stamp(value: unknown, seconds = false): string {
  if (!finite(value) || value <= 0) return "尚未记录";
  const date = new Date(value * (seconds ? 1000 : 1));
  return Number.isFinite(date.getTime()) ? date.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "尚未记录";
}
function shortStamp(value: number): string {
  return new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}
function name(row: Formation): string {
  return row.pattern === "double_bottom" ? "双底颈线" : row.pattern === "double_top" ? "双顶颈线" : "突破回踩";
}
function origin(row: Formation): string {
  return row.origin === "warmup" ? "历史识别" : row.origin === "backfill" ? "补录识别" : row.origin === "live" ? "新观察" : "观察记录";
}
function explanation(row: Formation): string {
  if (row.stage === "invalidated") return "价格已经越过该形态的失效边界，本次跟踪结束。";
  if (row.stage === "expired") return "已到固定观察期限，本次形态跟踪结束。";
  if (row.stage === "confirmed") return row.pattern === "breakout_retest" ?
    "首次回踩后，后续已收盘K线完成再次突破。继续观察失效边界。" :
    "后续已收盘K线已经越过冻结颈线的确认边界。继续观察失效边界。";
  return row.pattern === "breakout_retest" ? "已发现区间突破，等待首次回踩及随后收盘确认。" :
    "两个极值已获得右侧K线确认，等待后续收盘突破颈线。";
}
function validRows(rows: unknown): Formation[] {
  if (!Array.isArray(rows)) return [];
  return rows.filter((row): row is Formation => Boolean(row && typeof row === "object" &&
    typeof row.id === "string" && typeof row.symbol === "string" && typeof row.interval === "string" && row.stage in PHASE));
}

function PatternChart({ row }: { row: Formation }) {
  const bars = Array.isArray(row.chart) ? row.chart.filter(bar => bar &&
    [bar.open_time, bar.close_time, bar.open, bar.high, bar.low, bar.close].every(finite) &&
    bar.low > 0 && bar.high >= Math.max(bar.open, bar.close) && bar.low <= Math.min(bar.open, bar.close)).slice(-80) : [];
  if (bars.length < 2) return <div className="pattern-chart-empty"><p>等待足够的K线绘制形态图。</p><span>关键价位仍在上方保留，不使用示例图替代真实行情。</span></div>;
  const points = Array.isArray(row.points) ? row.points.filter(point => finite(point?.time_ms) && finite(point?.price)) : [];
  const level = finite(row.level) ? row.level : null, invalid = finite(row.invalid_level) ? row.invalid_level : null;
  const prices = [...bars.flatMap(bar => [bar.high, bar.low]), ...points.map(point => point.price),
    ...(level === null ? [] : [level]), ...(invalid === null ? [] : [invalid])];
  const low = Math.min(...prices), high = Math.max(...prices), span = high - low || Math.max(high * .01, .001);
  const min = low - span * .1, max = high + span * .1;
  const width = 720, height = 280, x = (index: number) => 10 + index * (width - 20) / (bars.length - 1);
  const y = (price: number) => 10 + (max - price) / (max - min) * (height - 20);
  const candleWidth = Math.max(2, Math.min(9, (width - 20) / bars.length * .55));
  const ticks = Array.from({ length: 5 }, (_, i) => max - (max - min) * i / 4);
  return <div className="pattern-chart">
    <div className="pattern-chart-legend"><span><i className="pattern-key-level" />形态关键位</span><span><i className="pattern-key-invalid" />失效边界</span><span><i className="pattern-key-pivot" />结构极值</span></div>
    <div className="pattern-plot">
      <svg className="pattern-candles" role="img" aria-label={`${row.symbol} ${row.interval} ${name(row)}形态图`} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none">
        <title>{`${row.symbol} · ${row.interval} · ${name(row)}`}</title>
        <desc>横轴为图中已收盘K线的时间，纵轴为价格。实线为形态关键位，虚线为失效边界，圆点为已确认结构极值。</desc>
        {ticks.map((tick, index) => <line key={index} x1={0} x2={width} y1={y(tick)} y2={y(tick)} className="pattern-grid-line" vectorEffect="non-scaling-stroke" />)}
        {bars.map((bar, index) => <g key={`${bar.close_time}-${index}`} className={bar.close >= bar.open ? "pattern-up" : "pattern-down"}>
          <line x1={x(index)} x2={x(index)} y1={y(bar.high)} y2={y(bar.low)} vectorEffect="non-scaling-stroke" />
          <rect x={x(index) - candleWidth / 2} y={Math.min(y(bar.open), y(bar.close))} width={candleWidth} height={Math.max(1, Math.abs(y(bar.open) - y(bar.close)))} />
          <title>{`${shortStamp(bar.close_time)}；收盘 ${number(bar.close)}`}</title>
        </g>)}
        {level !== null && <line x1={0} x2={width} y1={y(level)} y2={y(level)} className="pattern-level-line" vectorEffect="non-scaling-stroke" />}
        {invalid !== null && <line x1={0} x2={width} y1={y(invalid)} y2={y(invalid)} className="pattern-invalid-line" vectorEffect="non-scaling-stroke" />}
        {points.filter(point => point.time_ms >= bars[0].open_time && point.time_ms <= bars.at(-1)!.close_time).map((point, index) => {
          const nearest = bars.reduce((best, bar, i) => Math.abs(bar.close_time - point.time_ms) < Math.abs(bars[best].close_time - point.time_ms) ? i : best, 0);
          return <circle key={index} cx={x(nearest)} cy={y(point.price)} r={5} className="pattern-pivot-dot"><title>{`${POINTS[point.kind || ""] || "结构点"} · ${number(point.price)}`}</title></circle>;
        })}
      </svg>
      <div className="pattern-price-axis" aria-label="价格轴">{ticks.map((tick, index) => <span key={index} style={{ top: `${y(tick) / height * 100}%` }}>{number(tick)}</span>)}</div>
    </div>
    <div className="pattern-time-axis" aria-label="K线时间轴"><span>{shortStamp(bars[0].close_time)}</span><span>{shortStamp(bars[Math.floor(bars.length / 2)].close_time)}</span><span>{shortStamp(bars.at(-1)!.close_time)}</span></div>
    <p className="pattern-caption">已收盘K线 · 北京时间 · 图中末根收盘 {number(bars.at(-1)!.close)}。极值点的位置与系统实际观察时间分别保留。</p>
  </div>;
}

export function PatternPanel({ snapshot, error = "", nowMs = null, initialPatternId = null }: {
  snapshot: PatternSnapshot | null; error?: string; nowMs?: number | null; initialPatternId?: string | null;
}) {
  const [filter, setFilter] = useState<Filter>(initialPatternId ? "all" : "active");
  const [selectedId, setSelectedId] = useState<string | null>(initialPatternId);
  const detailRef = useRef<HTMLDivElement>(null);
  const formations = useMemo(() => validRows(snapshot?.patterns).slice().sort((a, b) =>
    (b.updated_at_ms || 0) - (a.updated_at_ms || 0)), [snapshot?.patterns]);
  const visible = formations.filter(row => filter === "all" || filter === "active" && ["forming", "confirmed"].includes(row.stage) || row.stage === filter);
  const selected = selectedId ? visible.find(row => row.id === selectedId) : visible[0];
  function showDetails() {
    if (typeof window !== "undefined" && window.matchMedia("(max-width: 1080px)").matches) {
      detailRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }
  useEffect(() => {
    if (initialPatternId && selected?.id === initialPatternId) showDetails();
  }, [initialPatternId, selected?.id]);
  const missingSelection = Boolean(selectedId && !selected);
  const heartbeat = finite(snapshot?.last_scan_at) ? snapshot.last_scan_at : snapshot?.updated_at;
  const expired = finite(nowMs) && finite(heartbeat) && nowMs - heartbeat * 1000 > 120000;
  const status = error ? "unavailable" : expired ? "stale" : snapshot?.status || "starting";
  const statusText = ({ healthy: "监控正常", starting: "正在预热", partial: "部分品种暂不可用", stale: "数据已过期", unavailable: "形态数据暂不可用" } as Record<string, string>)[status] || "状态待确认";
  const count = (stage: Stage) => finite(snapshot?.counts?.[stage]) ? snapshot!.counts![stage]! : snapshot ? formations.filter(row => row.stage === stage).length : null;
  const endedCount = count("invalidated") === null || count("expired") === null ? null : count("invalidated")! + count("expired")!;
  const events = validRows(snapshot?.events).slice().sort((a, b) => (b.updated_at_ms || 0) - (a.updated_at_ms || 0)).slice(0, 12);
  return <div className="pattern-monitor">
    <section className="content-panel pattern-summary">
      <div className="section-heading"><div><p className="overline">PRICE STRUCTURE OBSERVER</p><h2>价格形态监控</h2></div><span className={status === "healthy" ? "positive" : "warning"} role="status">{statusText}</span></div>
      <p className="pattern-intro">突破首次回踩、双底与双顶颈线。<strong>仅观察，不自动开仓。</strong> 每个形态保留关键价位、失效边界和实际观察时间。</p>
      {error && <p className="pattern-warning" role="alert">{error}；下方保留上次快照，不能作为最新行情。</p>}
      {status === "stale" && !error && <p className="pattern-warning" role="alert">快照超过两分钟未更新，下方为上次记录；请等待连接恢复。</p>}
      {status === "starting" && <p className="pattern-note">正在读取已收盘K线并建立形态基线。历史预热只用于识别结构，不补发旧提醒。</p>}
      {status === "partial" && <p className="pattern-warning" role="alert">部分行情尚未完成更新。已取得的观察继续保留，缺失部分不按“没有形态”处理。</p>}
      <div className="pattern-metrics"><div><span>待确认</span><strong>{count("forming") ?? "—"}</strong></div><div><span>已确认</span><strong>{count("confirmed") ?? "—"}</strong></div><div><span>已结束</span><strong>{endedCount ?? "—"}</strong></div></div>
      <div className="pattern-summary-foot"><span>Binance USDT永续 · {(snapshot?.intervals || ["15m", "1h"]).join(" / ")}</span><span>最近检查 · {stamp(snapshot?.last_scan_at, true)}</span><span>{snapshot?.symbols?.length ? `${snapshot.symbols.length}个监控币种` : "等待监控范围"}</span></div>
    </section>

    <section className="content-panel pattern-workspace">
      <div className="section-heading"><h2>形态观察</h2><span>{error || status === "stale" ? "上次快照" : "每10秒刷新"}</span></div>
      <div className="pattern-filters" aria-label="形态状态筛选">{FILTERS.map(([value, label]) => <button type="button" key={value} aria-label={`筛选 ${label}`} aria-pressed={filter === value} onClick={() => { setFilter(value); setSelectedId(null); }}>{label}</button>)}</div>
      <div className="pattern-layout">
        <div className="pattern-list" aria-label="形态列表">{visible.length ? visible.map(row => <button type="button" className="pattern-row" key={row.id} aria-label={`查看 ${row.symbol} ${row.interval} ${name(row)}`} aria-pressed={selected?.id === row.id} onClick={() => { setSelectedId(row.id); showDetails(); }}>
          <span className="pattern-row-top"><strong>{row.symbol.replace(/USDT$/, "")}</strong><span>{row.interval}</span><span className={`pattern-stage pattern-stage-${row.stage}`}>{PHASE[row.stage]}</span></span>
          <span className="pattern-row-name">{name(row)} <span className={row.direction === "long" ? "positive" : "negative"}>{row.direction === "long" ? "偏多" : "偏空"}</span></span>
          <span className="pattern-row-bottom"><span>{origin(row)}</span><time>{stamp(row.updated_at_ms)}</time></span>
        </button>) : <div className="pattern-list-empty"><strong>{status === "starting" ? "等待形态基线" : "当前筛选下暂无形态"}</strong><p>{status === "healthy" ? "有符合固定规则的新形态时，会在这里出现。" : "数据尚未完整更新，不能据此判断市场没有形态。"}</p>{filter !== "all" && <button className="text-action" type="button" onClick={() => { setFilter("all"); setSelectedId(null); }}>查看全部记录</button>}</div>}</div>
        <div className="pattern-detail" aria-live="polite" ref={detailRef}>
          {selected ? <>
            <div className="pattern-detail-title"><div><p className="pattern-kicker">{selected.symbol} · {selected.interval} · {origin(selected)}</p><h3>{name(selected)} <span className={selected.direction === "long" ? "positive" : "negative"}>{selected.direction === "long" ? "观察偏多" : "观察偏空"}</span></h3></div><span className={`pattern-stage pattern-stage-${selected.stage}`}>{PHASE[selected.stage]}</span></div>
            <p className="pattern-explanation">{explanation(selected)}</p>
            <dl className="pattern-levels"><div><dt>形态关键位</dt><dd>{number(selected.level)}</dd></div><div><dt>失效边界</dt><dd>{number(selected.invalid_level)}</dd></div></dl>
            <PatternChart row={selected} />
            <dl className="pattern-times"><div><dt>实际观察时间 · 北京时间</dt><dd>{stamp(selected.available_at_ms)}</dd></div><div><dt>对应K线收盘 · 北京时间</dt><dd>{stamp(selected.bar_close_time)}</dd></div></dl>
            {Boolean(selected.points?.length) && <div className="pattern-points" aria-label="结构关键点">{selected.points?.filter(point => finite(point?.price)).map((point, index) => <span key={index}>{POINTS[point.kind || ""] || "结构点"} <strong>{number(point.price)}</strong></span>)}</div>}
            <p className="pattern-note">待确认最多观察12根后续K线；确认后最多继续跟踪24根。确认表示价格条件成立，不代表未来收益已获验证。</p>
          </> : <div className="pattern-detail-empty"><span aria-hidden="true">◇</span><h3>{missingSelection ? "这条形态已不在近期列表中" : "选择一个形态查看细节"}</h3><p>{missingSelection ? "请从列表选择其他记录，系统不会用另一张图代替这条形态。" : "图中会标注关键位置和失效边界，方便核对每一次提醒。"}</p></div>}
        </div>
      </div>
    </section>

    {events.length > 0 && <section className="content-panel"><div className="section-heading"><h2>最近状态变化</h2><span>最近12条</span></div><ol className="pattern-timeline">{events.map((row, index) => <li key={`${row.id}-${row.stage}-${index}`}><span className={`pattern-stage pattern-stage-${row.stage}`}>{PHASE[row.stage]}</span><div><strong>{row.symbol} · {row.interval} · {name(row)}</strong><p>{origin(row)} · {stamp(row.available_at_ms)}</p></div></li>)}</ol></section>}

    <section className="content-panel pattern-how"><div className="section-heading"><h2>这两类提醒怎样形成</h2><span>固定规则 · 无收益承诺</span></div><div className="pattern-rule-grid"><div><h3>突破后的首次回踩</h3><p>先锁定历史整理边界，等待收盘突破。后续首次回踩后，只有更晚的收盘K线再次突破回踩段高低点，才标为已确认。</p></div><div><h3>双底／双顶颈线</h3><p>两个极值需要右侧K线确认，再冻结中间颈线。之后收盘越过确认边界才提醒；图上的低点或高点不是当时就已发出的信号。</p></div></div><p className="pattern-note">历史识别与补录识别保留独立标记。形态仅用于观察；原有开平仓与风险规则继续独立运行。</p></section>
  </div>;
}

export default function PatternMonitor() {
  const [snapshot, setSnapshot] = useState<PatternSnapshot | null>(null);
  const [error, setError] = useState("");
  const [nowMs, setNowMs] = useState<number | null>(null);
  const [initialPatternId] = useState(() => typeof window === "undefined" ? null : new URLSearchParams(window.location.search).get("pattern"));
  useEffect(() => {
    let active = true;
    const loop = createRefreshLoop(async (signal: AbortSignal) => {
      try {
        const response = await fetchWithTimeout("/api/patterns", { signal, cache: "no-store" });
        if (!response.ok) throw Error("pattern snapshot unavailable");
        const value = await response.json() as PatternSnapshot;
        if (!value || typeof value !== "object" || !Array.isArray(value.patterns) || value.mode !== "observation_only") throw Error("invalid pattern snapshot");
        if (active && !signal.aborted) { setSnapshot(value); setError(""); setNowMs(Date.now()); }
      } catch { if (active && !signal.aborted) { setError("形态接口暂不可用"); setNowMs(Date.now()); } }
    }, { intervalMs: 10000 });
    void loop.refresh();
    return () => { active = false; loop.stop(); };
  }, []);
  return <PatternPanel snapshot={snapshot} error={error} nowMs={nowMs} initialPatternId={initialPatternId} />;
}
