"use client";

import { useEffect, useMemo, useState } from "react";
import { fetchWithTimeout } from "./api-client.mjs";
import { finiteMetric } from "./dashboard-interactions.mjs";

type Trade = {
  id: string;
  symbol: string;
  inst_id?: string;
  interval: string;
  direction: string;
  status: string;
  entry_price: number;
  current_price?: number;
  exit_price: number | null;
  return_pct: number | null;
  unrealized_pct?: number;
  config_revision?: string;
  runtime_config?: unknown;
  return_model?: string;
  opened_at: number;
  closed_at?: number;
  exit_reason: string;
  fee_rate?: number;
  size?: string | number;
  notional_usdt?: number | null;
  pnl_usdt?: number | null;
  pnl_usdt_estimated?: boolean;
};

type TradeStats = {
  total_trades: number;
  open_trades: number;
  closed_trades: number;
  wins: number;
  losses: number;
  breakevens: number;
  win_rate: number;
  expectancy: number;
  total_return: number;
  realized_pnl_usdt?: number;
  mixed_return_models?: boolean;
  comparability_note?: string;
  return_model_stats?: Record<string, { closed_trades: number; expectancy: number }>;
};

type Universe = {
  enabled: boolean;
  ready: boolean;
  top_n: number;
  selection: string;
  updated_at?: number;
  interval: string;
  error?: string;
  members: { symbol: string; name?: string; market_cap_rank: number }[];
  scan?: { running?: boolean; checked_count?: number; eligible_count?: number; last_scan_at?: number; errors?: Record<string, string> };
};

type OkxDemoLedger = {
  source_exists: boolean;
  source_updated_at?: number | null;
  trades: Trade[];
  stats: TradeStats;
  retention_note?: string;
  error?: string;
};

function number(value: unknown, digits = 2) {
  const parsed = finiteMetric(value);
  if (parsed === null) return "--";
  return parsed.toLocaleString("zh-CN", { maximumFractionDigits: digits });
}

function percent(value: unknown) {
  const parsed = finiteMetric(value);
  if (parsed === null) return "--";
  return `${(parsed * 100).toFixed(2)}%`;
}

function money(value: unknown) {
  const parsed = finiteMetric(value);
  if (parsed === null) return "--";
  return new Intl.NumberFormat("zh-CN", { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(parsed);
}

function time(value?: number | null) {
  if (!value) return "--";
  return new Date(value * 1000).toLocaleString("zh-CN", { hour12: false });
}

function tone(value: unknown) {
  const parsed = finiteMetric(value);
  if (parsed === null || parsed === 0) return "neutral";
  return parsed > 0 ? "positive" : "negative";
}

function returnModelLabel(model?: string) {
  if (model === undefined || model === "legacy_ratio_v1") return "历史比例（旧版）";
  if (model === "linear_usdm_v1") return "线性 USDT 合约";
  return `未识别口径（${model}）`;
}

export function TradeRow({ trade, source }: { trade: Trade; source: "paper" | "okx" }) {
  const result = trade.status === "open" ? trade.unrealized_pct : trade.return_pct;
  return (
    <details className="report-row">
      <summary>
        <span>{trade.symbol} · {trade.interval} · {trade.direction === "long" ? "做多" : "做空"}</span>
        <span>{trade.status === "open" ? "持仓中" : "已退出"}</span>
        <strong className={tone(result)}>{percent(result)}</strong>
      </summary>
      <p>开仓 {number(trade.entry_price, 8)} → {number(trade.status === "open" ? trade.current_price : trade.exit_price, 8)} · {time(trade.opened_at)} · {trade.exit_reason || "open"}</p>
      {source === "okx" ? (
        <p>合约 {trade.inst_id || trade.symbol} · 数量 {trade.size ?? "--"} · 名义本金 {money(trade.notional_usdt)} · 估算盈亏 <strong className={tone(trade.pnl_usdt)}>{money(trade.pnl_usdt)}</strong>{trade.closed_at ? ` · 平仓 ${time(trade.closed_at)}` : ""}</p>
      ) : (
        <p>收益口径：{returnModelLabel(trade.return_model)} · 配置版本：{trade.config_revision || "历史记录未保存版本"} · 单边手续费：{trade.fee_rate === undefined ? "历史默认" : `${trade.fee_rate * 100}%`}</p>
      )}
    </details>
  );
}

export function StatBlock({ stats, okx = false }: { stats: TradeStats; okx?: boolean }) {
  const groups = Object.entries(stats.return_model_stats ?? {});
  const mixed = !okx && (stats.mixed_return_models || groups.length > 1);
  return (
    <>
      <div className="stat-grid">
        <div><span>全部记录</span><strong>{number(stats.total_trades, 0)}</strong></div>
        <div><span>已平仓</span><strong>{number(stats.closed_trades, 0)}</strong></div>
        <div><span>{mixed ? "混合口径胜率" : "胜率"}</span><strong className={mixed ? "neutral" : tone(stats.win_rate)}>{stats.closed_trades ? percent(stats.win_rate) : "暂无样本"}</strong></div>
        <div><span>{okx ? "估算盈亏" : mixed ? "混合口径逐笔合计" : "逐笔收益合计"}</span><strong className={mixed ? "neutral" : tone(okx ? stats.realized_pnl_usdt : stats.total_return)}>{okx ? money(stats.realized_pnl_usdt) : percent(stats.total_return)}</strong></div>
      </div>
      {mixed && <p role="note">{stats.comparability_note || "记录包含不同收益口径，请按收益版本分别比较；总体指标只用于展示。"}</p>}
      {!okx && groups.length > 0 && (
        <table>
          <caption>按收益口径分组（已平仓记录）</caption>
          <thead><tr><th scope="col">收益口径</th><th scope="col">闭仓数</th><th scope="col">单笔期望</th></tr></thead>
          <tbody>{groups.map(([model, group]) => <tr key={model}><td>{returnModelLabel(model)}</td><td>{number(group.closed_trades, 0)}</td><td className={tone(group.expectancy)}>{percent(group.expectancy)}</td></tr>)}</tbody>
        </table>
      )}
    </>
  );
}

export default function PaperLedger() {
  const [trades, setTrades] = useState<Trade[]>([]);
  const [stats, setStats] = useState<TradeStats | null>(null);
  const [filter, setFilter] = useState("all");
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [universe, setUniverse] = useState<Universe | null>(null);
  const [okxLedger, setOkxLedger] = useState<OkxDemoLedger | null>(null);
  const [okxError, setOkxError] = useState("");

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const res = await fetchWithTimeout("/api/paper-trades");
        if (!res.ok) throw Error("读取模拟记录失败");
        const data = await res.json() as { trades: Trade[]; stats?: TradeStats; universe?: Universe };
        if (active) {
          setTrades(data.trades);
          setStats(data.stats ?? null);
          setUniverse(data.universe ?? null);
          setError("");
          setLoaded(true);
        }
      } catch (caught) {
        if (active) setError(String(caught));
      }

      try {
        const okxRes = await fetchWithTimeout("/api/okx-demo-ledger");
        if (!okxRes.ok) throw Error("读取 OKX Demo 历史账本失败");
        const okxData = await okxRes.json() as OkxDemoLedger;
        if (active) {
          setOkxLedger(okxData);
          setOkxError(okxData.error || "");
        }
      } catch (caught) {
        if (active) setOkxError(String(caught));
      }
    }
    void load();
    const id = setInterval(load, 15000);
    return () => {
      active = false;
      clearInterval(id);
    };
  }, []);

  const shown = useMemo(
    () => trades.filter((trade) => filter === "all" || trade.status === filter).sort((a, b) => b.opened_at - a.opened_at),
    [filter, trades],
  );
  const okxTrades = okxLedger?.trades ?? [];

  return (
    <section className="view-stack">
      <div className="view-intro">
        <div>
          <h2>模拟交易记录</h2>
          <p>本地纸面信号和保留的 OKX Demo 历史账本分开展示。OKX Demo 记录只读保留，不再触发下单。</p>
        </div>
      </div>

      {!okxLedger && okxError && (
        <section className="content-panel">
          <h2>OKX Demo 历史账本</h2>
          <p role="alert">{okxError}</p>
        </section>
      )}

      {okxLedger && (
        <section className="content-panel">
          <div className="section-heading">
            <div>
              <h2>OKX Demo 历史账本</h2>
              <p>{okxLedger.retention_note}</p>
            </div>
            <span>更新 {time(okxLedger.source_updated_at)}</span>
          </div>
          <StatBlock stats={okxLedger.stats} okx />
          <p role={okxError ? "alert" : "status"}>{okxError || (okxLedger.source_exists ? `${okxTrades.length} 条开平仓记录` : "未找到历史账本")}</p>
          {okxTrades.slice(0, 80).map((trade) => <TradeRow key={trade.id} trade={trade} source="okx" />)}
          {okxTrades.length > 80 && <p>仅展示最近 80 条，完整数据仍保留在后端账本文件中。</p>}
        </section>
      )}

      {universe?.enabled && (
        <section className="content-panel">
          <div className="section-heading"><h2>币安市值模拟开仓池</h2><span>{universe.members.length} / {universe.top_n} 个</span></div>
          <p>{universe.selection === "global_top_n" ? `全市场市值前 ${universe.top_n} 与币安 USDT 永续取交集` : `币安可交易 USDT 永续按市值排序，取前 ${universe.top_n} 个`} · {universe.interval} 策略</p>
          <p role="status">{universe.ready ? (universe.scan?.running ? "扫描中" : "币池就绪") : "暂停新开仓"} · 本轮已检查 {universe.scan?.checked_count ?? 0} / {universe.scan?.eligible_count ?? universe.members.length} 个</p>
          {universe.error && <p role="alert">{universe.error}</p>}
          {!!Object.keys(universe.scan?.errors ?? {}).length && <p role="alert">{Object.keys(universe.scan?.errors ?? {}).length} 个品种本轮扫描失败，等待重试。</p>}
          <p>市值来源：CoinGecko · 币池更新：{time(universe.updated_at)} · 最近扫描：{time(universe.scan?.last_scan_at)}</p>
          <details><summary>查看币池名单</summary><ul>{universe.members.map((member) => <li key={member.symbol}>{member.symbol} · {member.name ?? ""} · 全市场市值第 {member.market_cap_rank} 名</li>)}</ul></details>
          <p>币池每 6 小时更新；池外不新增仓位，已有仓位继续按原规则管理。</p>
        </section>
      )}

      <section className="content-panel">
        <div className="section-heading">
          <div><h2>本地纸面信号账本</h2><p>信号触发后本地记账，按照记录的止损和止盈退出。未向交易所发单。</p></div>
          <label>筛选 <select value={filter} onChange={(event) => setFilter(event.target.value)}><option value="all">全部保留记录</option><option value="open">持仓中</option><option value="closed">已退出</option></select></label>
        </div>
        {stats && <StatBlock stats={stats} />}
        <p role="status">{error || (!loaded ? "读取中..." : `${shown.length} 条记录`)}</p>
        {shown.map((trade) => <TradeRow key={trade.id} trade={trade} source="paper" />)}
        {loaded && !shown.length && !error && <div className="empty-state">尚无符合条件的模拟记录；等待有效信号触发。</div>}
      </section>
    </section>
  );
}
