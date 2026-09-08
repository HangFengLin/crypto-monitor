"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { fetchWithTimeout, validateState } from "./api-client.mjs";
import ResearchHistory from "./research-history";
import RuntimeSettings from "./runtime-settings";
import PaperLedger from "./paper-ledger";
import FeishuNotifications from "./feishu-notifications";

type View = "overview" | "markets" | "paper" | "research" | "settings";
type Direction = "up" | "down" | "none" | string;

type WatchItem = {
  symbol: string;
  email?: string;
  signal: boolean;
  indicator_alert: boolean;
  support_alert: boolean;
  interval: string;
};

type Price = {
  lastPrice?: number;
  priceChange?: number;
  priceChangePercent?: number;
  highPrice?: number;
  lowPrice?: number;
  quoteVolume?: number;
};

type Indicator = { direction?: Direction; label?: string };
type IntervalSignal = { ma?: Indicator; macd?: Indicator };
type Support = { support?: number; distance_pct?: number; touched?: boolean; reason?: string };
type EventRecord = Record<string, unknown> & {
  type?: string;
  symbol?: string;
  interval?: string;
  created_at?: number;
  price?: number;
  signal_name?: string;
  indicator_label?: string;
  indicator_name?: string;
  direction?: string;
};

type Position = {
  symbol?: string;
  direction?: string;
  current_price?: number;
  unrealized_pct?: number;
  interval?: string;
  entry_price?: number;
  stop_loss?: number;
  target_price?: number;
  opened_at?: number;

};

type StrategyTrade = {
  symbol?: string;
  direction?: string;
  status?: string;
  outcome?: string;
  exit_price?: number;
  exit_reason?: string;
  closed_at?: number;
  interval?: string;
  entry_price?: number;
  stop_loss?: number;
  return_pct?: number;
  unrealized_pct?: number;
};

type LianqiState = {
  updated_at?: number;
  last_error?: string | null;
  market_data_source?: string;
  watchlist?: WatchItem[];
  prices?: Record<string, Price>;
  signals?: Record<string, { signal?: string; signal_name?: string; td_summary?: string }>;
  indicator_signals?: Record<string, Record<string, IntervalSignal>>;
  support_levels?: Record<string, Record<string, Support>>;
  events?: EventRecord[];
  strategy_stats?: Record<string, number>;
  strategy_trades?: StrategyTrade[];
  discord_status?: { configured?: boolean; running?: boolean; label?: string; last_error?: string; last_ok_at?: number };
  signal_tracking?: {
    enabled?: boolean;
    health?: string;
    health_label?: string;
    open_positions?: number;
    position_count?: number;
    positions?: Position[];
    updated_at?: number;
    last_error?: string | null;
    scan_stale?: boolean;
  };
  site_monitor?: {
    enabled?: boolean;
    ok?: boolean;
    last_run_at?: number;
    results?: Array<{ name?: string; ok?: boolean; duration_ms?: number; error?: string | null }>;
  };
};

type Report = { name?: string; path: string; url: string; size_bytes: number; updated_at: number };
type BacktestResult = Record<string, unknown> & {
  ok?: boolean;
  error?: string;
  symbol?: string;
  interval?: string;
  limit?: number;
  metrics?: Record<string, number>;
  stop_mode_results?: Array<{ stop_mode: string; metrics: Record<string, number> }>;
  groups?: Array<Record<string, string | number>>;
};

const NAV: Array<{ id: View; label: string; caption: string }> = [
  { id: "overview", label: "总览", caption: "当前状态" },
  { id: "markets", label: "行情与信号", caption: "监控与触发记录" },
  { id: "paper", label: "模拟交易", caption: "本地交易账本" },
  { id: "research", label: "策略研究", caption: "实验与报告" },
  { id: "settings", label: "运行状态", caption: "服务与异常" },
];

const INTERVALS = ["15m", "1h", "4h", "1d"];

function number(value: unknown, digits = 2) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "--";
  return parsed.toLocaleString("zh-CN", { maximumFractionDigits: digits });
}

function percent(value: unknown, inputIsRatio = true) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "--";
  return `${(parsed * (inputIsRatio ? 100 : 1)).toFixed(2)}%`;
}

function compactUsd(value: unknown) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "--";
  return new Intl.NumberFormat("zh-CN", {
    notation: "compact",
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(parsed);
}

function time(value?: number, includeDate = false) {
  if (!value) return "--";
  return new Date(value * 1000).toLocaleString("zh-CN", {
    month: includeDate ? "2-digit" : undefined,
    day: includeDate ? "2-digit" : undefined,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function bytes(value: number) {
  if (!Number.isFinite(value)) return "--";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(2)} MB`;
}

function signalClass(direction?: string) {
  if (direction === "up" || direction === "long") return "positive";
  if (direction === "down" || direction === "short") return "negative";
  if (direction?.startsWith("filtered")) return "warning";
  return "neutral";
}

function eventLabel(event: EventRecord) {
  if (event.type === "indicator") return event.indicator_label || event.indicator_name || "指标提醒";
  if (event.type === "support") return `支撑位触发 ${number(event.support, 6)}`;
  if (event.type === "chanlun") return event.signal_name || "缠论信号";
  return event.direction === "above" ? "突破价格阈值" : event.direction === "below" ? "跌破价格阈值" : "市场事件";
}

function eventDetail(event: EventRecord) {
  if (event.type === "support") return `当前价 ${number(event.price, 6)} · 距离 ${number(event.distance_pct, 2)}%`;
  if (event.type === "indicator") return `参考价 ${number(event.price, 6)}`;
  if (event.type === "chanlun") return `参考价 ${number(event.price, 6)} · 强度 ${number(event.strength, 2)}`;
  return `触发价 ${number(event.price, 6)}`;
}

function metricTone(value: unknown, reverse = false) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed === 0) return "neutral";
  return (reverse ? parsed < 0 : parsed > 0) ? "positive" : "negative";
}

export function LianqiDashboard() {
  const [view, setView] = useState<View>("overview");
  const [state, setState] = useState<LianqiState | null>(null);
  const [watchlist, setWatchlist] = useState<WatchItem[]>([]);
  const [connection, setConnection] = useState<"connecting" | "online" | "offline">("connecting");
  const [latency, setLatency] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [marketFilter, setMarketFilter] = useState("all");
  const [managing, setManaging] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState("");
  const [reports, setReports] = useState<Report[]>([]);
  const [reportsStatus, setReportsStatus] = useState("等待读取");
  const [backtestStatus, setBacktestStatus] = useState("选择参数后开始统计");
  const [backtestResult, setBacktestResult] = useState<BacktestResult | null>(null);
  const [backtestRunning, setBacktestRunning] = useState(false);
  const [backtestForm, setBacktestForm] = useState({ symbol: "BTCUSDT", interval: "15m", limit: "3000", rewardRisk: "2", stopMode: "all", feeRate: "0.001" });

  const applyState = useCallback((next: LianqiState) => {
    validateState(next);
    setState(next);
    setWatchlist((current) => (managing ? current : next.watchlist || []));
    setError(next.last_error || "");
  }, [managing]);

  const loadState = useCallback(async () => {
    const started = performance.now();
    try {
      const response = await fetchWithTimeout("/api/state", { cache: "no-store" });
      if (!response.ok) throw new Error(`数据服务返回 ${response.status}`);
      const payload = await response.json() as LianqiState;
      setLatency(Math.round(performance.now() - started));
      applyState(payload);
      setConnection("online");
    } catch (caught) {
      setConnection("offline");
      setError(caught instanceof Error ? caught.message : "暂时无法连接数据服务");
    }
  }, [applyState]);

  useEffect(() => {
    const initialLoad = window.setTimeout(() => void loadState(), 0);
    const polling = window.setInterval(() => void loadState(), 30000);
    const stream = new EventSource("/api/events");
    stream.onerror = () => setConnection("offline");
    stream.onmessage = (message) => {
      try {
        applyState(JSON.parse(message.data) as LianqiState);
        setConnection("online");
      } catch (caught) {
        setConnection("offline");
        setError(caught instanceof Error ? caught.message : "实时数据解析失败");
      }
    };
    return () => {
      window.clearTimeout(initialLoad);
      window.clearInterval(polling);
      stream.close();
    };
  }, [applyState, loadState]);

  const loadReports = useCallback(async () => {
    setReportsStatus("正在读取…");
    try {
      const response = await fetchWithTimeout("/api/reports", { cache: "no-store" });
      if (!response.ok) throw new Error(`读取失败 ${response.status}`);
      const payload = await response.json() as { reports?: Report[]; count?: number };
      setReports(payload.reports || []);
      setReportsStatus(payload.count ? `${payload.count} 份报告，按更新时间排列` : "暂无报告");
    } catch (caught) {
      setReportsStatus(caught instanceof Error ? caught.message : "报告读取失败");
    }
  }, []);

  useEffect(() => {
    if (view !== "research" || reportsStatus !== "等待读取") return;
    const reportLoad = window.setTimeout(() => void loadReports(), 0);
    return () => window.clearTimeout(reportLoad);
  }, [loadReports, reportsStatus, view]);

  const bot = state?.signal_tracking || {};
  const stats = state?.strategy_stats || {};
  const events = useMemo(() => state?.events || [], [state?.events]);
  const positions = bot.positions || [];
  const activeSignals = useMemo(() => Object.values(state?.signals || {}).filter((item) =>
    ["long", "short", "filtered_buy", "filtered_sell"].includes(item.signal || "")
  ).length, [state?.signals]);
  const recentAlerts = useMemo(() => events.filter((item) => Number(state?.updated_at || 0) - Number(item.created_at || 0) <= 86400).length, [events, state?.updated_at]);
  const marketRows = useMemo(() => watchlist.filter((item) => {
    const symbol = item.symbol.toUpperCase();
    const price = state?.prices?.[symbol];
    const signal = state?.signals?.[`${symbol}:${item.interval}`]?.signal;
    const matchesQuery = !query || symbol.includes(query.trim().toUpperCase());
    const change = Number(price?.priceChangePercent || 0);
    const matchesFilter = marketFilter === "all" || (marketFilter === "up" && change >= 0) ||
      (marketFilter === "down" && change < 0) || (marketFilter === "signal" && signal && signal !== "wait" && signal !== "none");
    return matchesQuery && matchesFilter;
  }), [marketFilter, query, state, watchlist]);

  const health = connection === "offline" ? "offline" : state?.last_error ? "warning" : connection;
  const healthLabel = health === "online" ? "运行正常" : health === "warning" ? "需要关注" : health === "offline" ? "连接异常" : "连接中";

  function navigate(next: View) {
    setView(next);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function updateWatch(index: number, patch: Partial<WatchItem>) {
    setWatchlist((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item));
  }

  async function saveWatchlist() {
    setSaving(true);
    setSaveMessage("正在保存…");
    try {
      const normalized = watchlist.map((item) => ({ ...item, symbol: item.symbol.trim().toUpperCase(), email: "" })).filter((item) => item.symbol);
      const response = await fetchWithTimeout("/api/watchlist", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ watchlist: normalized }),
      });
      const payload = await response.json() as { ok?: boolean; detail?: string; error?: string; watchlist?: WatchItem[] };
      if (!response.ok || !payload.ok) throw new Error(payload.detail || payload.error || "保存失败");
      setWatchlist(payload.watchlist || normalized);
      setManaging(false);
      setSaveMessage("监控列表已更新");
      window.setTimeout(() => setSaveMessage(""), 2400);
    } catch (caught) {
      setSaveMessage(caught instanceof Error ? caught.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  async function runBacktest() {
    if (backtestRunning) return;
    setBacktestRunning(true);
    setBacktestStatus("正在读取历史行情并计算…大样本可能需要 1–2 分钟。");
    setBacktestResult(null);
    const params = new URLSearchParams({
      symbol: backtestForm.symbol,
      interval: backtestForm.interval,
      limit: backtestForm.limit,
      reward_risk: backtestForm.rewardRisk,
      stop_mode: backtestForm.stopMode,
      fee_rate: backtestForm.feeRate,
    });
    try {
      const response = await fetchWithTimeout(`/api/backtest?${params}`, { cache: "no-store" }, 120000);
      const payload = await response.json() as BacktestResult & { detail?: string };
      if (!response.ok || !payload.ok) throw new Error(payload.detail || payload.error || "统计失败");
      setBacktestResult(payload);
      setBacktestStatus(`${payload.symbol} · ${payload.interval} · ${number(payload.limit, 0)} 根 K 线`);
    } catch (caught) {
      setBacktestStatus(caught instanceof Error ? caught.message : "回测失败");
    } finally {
      setBacktestRunning(false);
    }
  }


  return (
    <div className="site-shell">
      <aside className="side-nav">
        <button className="brand" onClick={() => navigate("overview")} aria-label="返回炼气总览">
          <span className="brand-glyph">气</span>
          <span><strong>炼气</strong><small>LIANQI TERMINAL</small></span>
        </button>
        <nav aria-label="主要功能">
          {NAV.map((item, index) => (
            <button key={item.id} className={view === item.id ? "nav-button active" : "nav-button"} onClick={() => navigate(item.id)}>
              <span className="nav-index">0{index + 1}</span>
              <span><strong>{item.label}</strong><small>{item.caption}</small></span>
            </button>
          ))}
        </nav>
        <div className="rail-foot">
          <span className={`status-light ${health}`} />
          <span><strong>{healthLabel}</strong><small>后台服务 · {latency === null ? "--" : `${latency}ms`}</small></span>
        </div>
      </aside>

      <main className="main-stage">
        <header className="top-bar">
          <div>
            <p className="overline">LIVE STRATEGY INTELLIGENCE</p>
            <h1>{NAV.find((item) => item.id === view)?.label}</h1>
          </div>
          <div className="top-actions">
            <div className="updated"><span>数据更新时间</span><strong>{time(state?.updated_at)}</strong></div>
            <button className="refresh-button" onClick={loadState} aria-label="刷新实时数据">↻ <span>刷新</span></button>
          </div>
        </header>

        {error && <div className="error-strip" role="alert"><strong>数据提醒</strong><span>{error}</span><button onClick={() => setError("")} aria-label="关闭提醒">×</button></div>}

        {view === "overview" && (
          <section className="view-stack" aria-label="炼气总览">
            <div className="hero-grid">
              <article className="hero-panel">
                <div className="hero-copy">
                  <p className="overline">当前态势</p>
                  <h2>跟踪信号<br /><em>验证策略</em></h2>
                  <p>实时汇总模拟跟踪、策略信号与系统异常。先处理需要行动的事项，再进入市场细节。</p>
                  <div className="hero-actions">
                    <button className="primary-action" onClick={() => navigate("markets")}>查看实时行情 <span>→</span></button>
                    <button className="text-action" onClick={() => navigate("settings")}>查看运行状态</button>
                  </div>
                </div>
                <div className="signal-orbit" aria-hidden="true">
                  <span className="orbit orbit-one" />
                  <span className="orbit orbit-two" />
                  <span className={`core ${health}`}><i /></span>
                  <small>LIVE</small>
                </div>
              </article>
              <div className="metric-stack">
                <article className="metric-card accent"><span>模拟跟踪</span><strong>{number(positions.length, 0)}</strong><small>信号模拟记录，不提交订单</small></article>
                <article className="metric-card"><span>活跃信号</span><strong>{number(activeSignals, 0)}</strong><small>缠论与指标信号</small></article>
                <article className="metric-card"><span>近 24H 保留事件</span><strong>{number(recentAlerts, 0)}</strong><small>最近 50 条中的触发记录</small></article>
              </div>
            </div>

            <section className="content-panel position-panel">
              <div className="section-heading"><div><p className="overline">NEEDS ATTENTION</p><h2>当前模拟跟踪</h2></div><span className={`inline-status ${bot.enabled ? "positive" : "warning"}`}>{bot.enabled ? "模拟跟踪" : "新记录已暂停"} · 最近更新 {time(bot.updated_at)}</span></div>
              {positions.length ? <div className="position-grid">{positions.map((position, index) => (
                <article className="position-card" key={`${position.symbol}-${index}`}>
                  <div><span className={`direction-tag ${signalClass(position.direction)}`}>{position.direction || "--"}</span><small>模拟 · {position.interval || "15m"}</small></div>
                  <h3>{position.symbol || "--"}</h3>
                  <dl><div><dt>入场</dt><dd>{number(position.entry_price, 6)}</dd></div><div><dt>止损</dt><dd>{number(position.stop_loss, 6)}</dd></div><div><dt>目标</dt><dd>{number(position.target_price, 6)}</dd></div><div><dt>现价</dt><dd>{number(position.current_price, 6)}</dd></div><div><dt>模拟浮动收益</dt><dd className={metricTone(position.unrealized_pct)}>{percent(position.unrealized_pct)}</dd></div></dl>
                </article>
              ))}</div> : <div className="empty-state"><span>○</span><strong>当前没有模拟跟踪</strong><p>捕捉到开仓信号后开始跟踪，触发止损、目标或保护规则后记录模拟退出。</p></div>}
            </section>

            <section className="content-panel"><div className="section-heading"><div><h2>最近信号与模拟退出</h2><p>全部持仓、退出明细和开仓时配置，统一保存在模拟交易账本。</p></div><button className="secondary-action" onClick={()=>navigate("paper")}>打开模拟交易 →</button></div></section>
            <div className="split-grid">
              <section className="content-panel">
                <div className="section-heading"><div><p className="overline">STRATEGY LEDGER</p><h2>保留样本统计</h2></div><span>{number(stats.closed_trades, 0)} 笔已结束</span></div>
                <div className="stat-grid">
                  <div><span>累计信号</span><strong>{number(stats.total_trades, 0)}</strong></div>
                  <div><span>模拟胜率</span><strong className={metricTone(stats.win_rate)}>{stats.closed_trades ? percent(stats.win_rate) : "暂无样本"}</strong></div>
                  <div><span>平均结果</span><strong className={metricTone(stats.expectancy)}>{percent(stats.expectancy)}</strong></div>
                  <div><span>逐笔收益合计</span><strong className={metricTone(stats.total_return)}>{percent(stats.total_return)}</strong></div>
                </div>
              </section>
              <section className="content-panel compact-events">
                <div className="section-heading"><div><p className="overline">LATEST SIGNALS</p><h2>最近触发</h2></div><button className="text-action" onClick={() => navigate("markets")}>全部记录 →</button></div>
                <div className="event-list">{events.slice(0, 4).map((event, index) => <EventRow key={`${event.created_at}-${index}`} event={event} />)}</div>
              </section>
            </div>
          </section>
        )}

        {view === "markets" && (
          <section className="view-stack">
            <div className="view-intro"><div><p className="overline">MARKET MATRIX</p><h2>实时行情与多周期信号</h2><p>价格、支撑位、均线与 MACD 信号聚合在同一行，减少来回切换。</p></div><button className={managing ? "secondary-action active" : "secondary-action"} onClick={() => setManaging((value) => !value)}>{managing ? "退出管理" : "管理币种"}</button></div>
            <section className="content-panel market-panel">
              <div className="market-toolbar">
                <label className="search-field"><span>搜索交易对</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="BTC / ETH" /></label>
                <label className="select-field"><span>筛选</span><select value={marketFilter} onChange={(event) => setMarketFilter(event.target.value)}><option value="all">全部行情</option><option value="up">上涨</option><option value="down">下跌</option><option value="signal">有信号</option></select></label>
                <span className="result-count">{marketRows.length} 个交易对</span>
              </div>
              <div className="market-list">
                {marketRows.map((item) => <MarketRow key={item.symbol} item={item} state={state} managing={managing} index={watchlist.indexOf(item)} onChange={updateWatch} onRemove={(index) => setWatchlist((items) => items.filter((_, itemIndex) => itemIndex !== index))} />)}
              </div>
              {managing && <div className="manage-bar"><button className="secondary-action" onClick={() => setWatchlist((items) => [...items, { symbol: "", signal: true, indicator_alert: true, support_alert: true, interval: "15m" }])}>＋ 添加币种</button><span aria-live="polite">{saveMessage}</span><button className="primary-action" disabled={saving} onClick={saveWatchlist}>{saving ? "保存中…" : "保存监控列表"}</button></div>}
            </section>
          </section>
        )}

        {view === "markets" && (
          <section className="view-stack">
            <div className="view-intro"><div><p className="overline">ALERT STREAM</p><h2>信号与告警记录</h2><p>按最新触发时间排列，集中查看缠论、指标、支撑位与价格阈值事件。</p></div><div className="large-count"><strong>{events.length}</strong><span>条记录</span></div></div>
            <section className="content-panel"><div className="event-list full">{events.length ? events.map((event, index) => <EventRow key={`${event.created_at}-${index}`} event={event} detailed />) : <div className="empty-state"><strong>暂无触发记录</strong></div>}</div></section>
          </section>
        )}

        {view === "research" && (
          <section className="view-stack">
            <div className="view-intro"><div><p className="overline">STRATEGY LAB</p><h2>信号回测</h2><p>使用 Binance USDT 合约已收盘 K 线。单次参数不修改运行设置；结果自动保存。</p></div></div>
            <section className="backtest-layout">
              <div className="content-panel backtest-form">
                <div className="section-heading"><div><p className="overline">PARAMETERS</p><h2>统计参数</h2></div></div>
                <label><span>交易对</span><select value={backtestForm.symbol} onChange={(event) => setBacktestForm({ ...backtestForm, symbol: event.target.value })}>{(watchlist.length ? watchlist : [{ symbol: "BTCUSDT" }]).map((item) => <option key={item.symbol} value={item.symbol}>{item.symbol}</option>)}</select></label>
                <div className="field-pair"><label><span>周期</span><select value={backtestForm.interval} onChange={(event) => setBacktestForm({ ...backtestForm, interval: event.target.value })}>{["15m", "1h", "4h", "1d"].map((item) => <option key={item}>{item}</option>)}</select></label><label><span>K 线数</span><input type="number" min="300" max="50000" step="1000" value={backtestForm.limit} onChange={(event) => setBacktestForm({ ...backtestForm, limit: event.target.value })} /></label></div>
                <div className="field-pair"><label><span>目标 R</span><input type="number" min="0.3" max="5" step="0.1" value={backtestForm.rewardRisk} onChange={(event) => setBacktestForm({ ...backtestForm, rewardRisk: event.target.value })} /></label><label><span>止损方法</span><select value={backtestForm.stopMode} onChange={(event) => setBacktestForm({ ...backtestForm, stopMode: event.target.value })}><option value="all">全部对比</option><option value="structure_atr">结构 ATR</option><option value="atr_trailing_after_1r">1R 后移动</option></select></label></div>
                <label><span>单边手续费比例（0.001 = 0.1%）</span><input type="number" min="0" max="0.02" step="0.0001" value={backtestForm.feeRate} onChange={event=>setBacktestForm({...backtestForm,feeRate:event.target.value})} /></label>
                <button className="primary-action wide" disabled={backtestRunning} onClick={runBacktest}>{backtestRunning ? "统计中…" : "开始统计"} <span>→</span></button>
              </div>
              <div className="content-panel result-panel">
                <div className="section-heading"><div><p className="overline">RESULTS</p><h2>回测结果</h2></div><span>{backtestStatus}</span></div>
                {backtestResult ? <BacktestResults result={backtestResult} /> : <div className="empty-state tall"><span>⌁</span><strong>等待计算</strong><p>选择左侧参数后开始统计，结果用于策略研究。</p></div>}
              </div>
            </section>
          </section>
        )}

        {view === "research" && (
          <section className="view-stack">
            <ResearchHistory />
            <div className="view-intro"><div><p className="overline">RESEARCH ARCHIVE</p><h2>回测报告</h2><p>所有已生成的可视化报告集中归档，按更新时间倒序排列。</p></div><button className="secondary-action" onClick={loadReports}>↻ 刷新报告</button></div>
            <section className="content-panel"><div className="section-heading"><div><p className="overline">ARCHIVE</p><h2>{reportsStatus}</h2></div></div><div className="report-list">{reports.map((report, index) => <a href={report.url} target="_blank" rel="noreferrer" className="report-row" key={report.path}><span className="report-number">{String(index + 1).padStart(2, "0")}</span><span className="report-copy"><strong>{report.path}</strong><small>{time(report.updated_at, true)} · {bytes(report.size_bytes)}</small></span><span className="report-open">打开 ↗</span></a>)}</div></section>
          </section>
        )}

        {view === "paper" && <PaperLedger />}

        {view === "settings" && (
          <section className="view-stack">
            <RuntimeSettings />
            <FeishuNotifications />
            <div className="view-intro"><div><p className="overline">SYSTEM DIAGNOSTICS</p><h2>服务状态</h2><p>查看当前服务状态和异常；配置调整与链路排查由后台处理。</p></div></div>
            <div className="diagnostic-grid">
              <section className="content-panel service-panel">
                <div className="section-heading"><div><p className="overline">SERVICES</p><h2>运行状态</h2></div></div>
                <ServiceRow label="信号跟踪" status={bot.enabled ? "模拟跟踪" : "新记录已暂停"} ok={Boolean(bot.enabled)} detail={`${number(positions.length, 0)} 个模拟跟踪 · 最近更新 ${time(bot.updated_at)}`} />
                <ServiceRow label="Discord 通知" status={state?.discord_status?.label || "未知"} ok={Boolean(state?.discord_status?.running)} detail={state?.discord_status?.last_error || `最近成功 ${time(state?.discord_status?.last_ok_at)}`} />
                <ServiceRow label="站点巡检" status={state?.site_monitor?.ok ? "正常" : "需要关注"} ok={Boolean(state?.site_monitor?.ok)} detail={`最近检查 ${time(state?.site_monitor?.last_run_at)}`} />
                <ServiceRow label="市场数据" status={state?.last_error ? "需要关注" : state?.updated_at ? "Binance 合约" : "等待数据"} ok={Boolean(state?.updated_at && !state?.last_error)} detail={`站点响应 ${latency === null ? "--" : `${latency}ms`}`} />
              </section>

            </div>
            <section className="content-panel probe-panel"><div className="section-heading"><div><p className="overline">ENDPOINT PROBES</p><h2>内部巡检目标</h2></div></div><div className="probe-grid">{(state?.site_monitor?.results || []).map((result) => <div key={result.name}><span className={`status-light ${result.ok ? "online" : "offline"}`} /><span><strong>{result.name}</strong><small>{result.error || `${number(result.duration_ms, 0)}ms`}</small></span></div>)}</div></section>
          </section>
        )}
      </main>

      <nav className="mobile-nav" aria-label="移动端功能导航">
        {NAV.map((item) => <button key={item.id} className={view === item.id ? "active" : ""} onClick={() => navigate(item.id)}><span>{item.label}</span></button>)}
      </nav>
    </div>
  );
}

function EventRow({ event, detailed = false }: { event: EventRecord; detailed?: boolean }) {
  return <article className="event-row"><span className={`event-mark ${signalClass(event.direction)}`} /><div><strong>{event.symbol || "系统"} · {eventLabel(event)}</strong><small>{event.interval || "实时"} · {eventDetail(event)}</small>{detailed && event.td_summary ? <small>TD9 {String(event.td_summary)}</small> : null}</div><time>{time(event.created_at, detailed)}</time></article>;
}

function MarketRow({ item, state, managing, index, onChange, onRemove }: { item: WatchItem; state: LianqiState | null; managing: boolean; index: number; onChange: (index: number, patch: Partial<WatchItem>) => void; onRemove: (index: number) => void }) {
  const symbol = item.symbol.toUpperCase();
  const price = state?.prices?.[symbol] || {};
  const change = Number(price.priceChangePercent || 0);
  const signal = state?.signals?.[`${symbol}:${item.interval}`] || {};
  const indicators = state?.indicator_signals?.[symbol] || {};
  const supports = state?.support_levels?.[symbol] || {};
  const base = symbol.replace(/USDT$|USD$|PERP$/g, "").slice(0, 4) || "--";
  return <article className="market-row">
    <div className="asset-cell"><span className="asset-code">{base}</span><div>{managing ? <input value={item.symbol} aria-label="交易对" onChange={(event) => onChange(index, { symbol: event.target.value.toUpperCase() })} /> : <strong>{symbol}</strong>}<small>{item.interval} · 永续监控</small></div></div>
    <div className="price-cell"><span>最新价</span><strong>{number(price.lastPrice, 8)}</strong><small>24H 高 {number(price.highPrice, 6)} · 低 {number(price.lowPrice, 6)}</small></div>
    <div className={`change-cell ${change >= 0 ? "positive" : "negative"}`}><span>24H</span><strong>{percent(change, false)}</strong><small>{compactUsd(price.quoteVolume)}</small></div>
    <div className="signal-cell"><span>当前信号</span><strong className={signalClass(signal.signal)}>{signal.signal_name || "等待数据"}</strong><div className="indicator-strip">{INTERVALS.map((interval) => { const current = indicators[interval]; return <span key={interval}><b>{interval}</b><i className={signalClass(current?.ma?.direction)} title={current?.ma?.label}>MA</i><i className={signalClass(current?.macd?.direction)} title={current?.macd?.label}>MACD</i></span>; })}</div></div>
    <div className="support-cell"><span>多周期支撑</span><div>{INTERVALS.map((interval) => <span className={supports[interval]?.touched ? "touched" : ""} key={interval}><b>{interval}</b><em>{number(supports[interval]?.support, 6)}</em></span>)}</div></div>
    {managing && <div className="market-manage"><select value={item.interval} aria-label="信号周期" onChange={(event) => onChange(index, { interval: event.target.value })}>{["1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"].map((value) => <option key={value}>{value}</option>)}</select><label><input type="checkbox" checked={item.signal} onChange={(event) => onChange(index, { signal: event.target.checked })} />缠论</label><label><input type="checkbox" checked={item.indicator_alert} onChange={(event) => onChange(index, { indicator_alert: event.target.checked })} />指标</label><label><input type="checkbox" checked={item.support_alert} onChange={(event) => onChange(index, { support_alert: event.target.checked })} />支撑</label><button onClick={() => onRemove(index)}>删除</button></div>}
  </article>;
}

function BacktestResults({ result }: { result: BacktestResult }) {
  const modes = result.stop_mode_results?.length ? result.stop_mode_results : [{ stop_mode: "structure_atr", metrics: result.metrics || {} }];
  return <div className="backtest-results"><div className="mode-results">{modes.map((mode) => <article key={mode.stop_mode}><span>{mode.stop_mode === "atr_trailing_after_1r" ? "1R 后 ATR 移动" : "结构 ATR"}</span><strong className={metricTone(mode.metrics.expectancy)}>{percent(mode.metrics.expectancy)}</strong><small>预期 / 笔</small><dl><div><dt>交易</dt><dd>{number(mode.metrics.total_trades, 0)}</dd></div><div><dt>胜率</dt><dd>{percent(mode.metrics.win_rate)}</dd></div><div><dt>回撤</dt><dd>{percent(mode.metrics.max_drawdown)}</dd></div><div><dt>止损</dt><dd>{percent(mode.metrics.stop_trigger_rate)}</dd></div></dl></article>)}</div>{Boolean(result.groups?.length) && <div className="group-results"><div className="group-head"><span>信号组合</span><span>次数</span><span>胜率</span><span>均值</span></div>{result.groups?.slice(0, 12).map((group, index) => <div className="group-row" key={index}><span>{group.signal} · {group.signal_grade || "weak"} · {group.trend}</span><b>{number(group.trades, 0)}</b><b>{percent(group.win_rate)}</b><b className={metricTone(group.avg_return)}>{percent(group.avg_return)}</b></div>)}</div>}</div>;
}

function ServiceRow({ label, status, ok, detail }: { label: string; status: string; ok: boolean; detail: string }) {
  return <div className="service-row"><span className={`status-light ${ok ? "online" : "offline"}`} /><span><strong>{label}</strong><small>{detail}</small></span><em className={ok ? "positive" : "warning"}>{status}</em></div>;
}
