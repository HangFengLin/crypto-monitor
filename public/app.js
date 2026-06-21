const rowTemplate = document.querySelector("#rowTemplate");
const coinRows = document.querySelector("#coinRows");
const eventsEl = document.querySelector("#events");
const updatedAtEl = document.querySelector("#updatedAt");
const marketDataSourceEl = document.querySelector("#marketDataSource");
const discordDot = document.querySelector("#discordDot");
const discordStateText = document.querySelector("#discordStateText");
const discordDetail = document.querySelector("#discordDetail");
const botDot = document.querySelector("#botDot");
const botStateText = document.querySelector("#botStateText");
const botDetail = document.querySelector("#botDetail");
const errorTextEl = document.querySelector("#errorText");
const saveMessageEl = document.querySelector("#saveMessage");
const connectionDot = document.querySelector("#connectionDot");
const connectionText = document.querySelector("#connectionText");
const headerConnectionDot = document.querySelector("#headerConnectionDot");
const headerConnectionText = document.querySelector("#headerConnectionText");
const apiLatencyEl = document.querySelector("#apiLatency");
const watchCountEl = document.querySelector("#watchCount");
const watchMetaEl = document.querySelector("#watchMeta");
const triggerCountEl = document.querySelector("#triggerCount");
const signalCountEl = document.querySelector("#signalCount");
const volumeTotalEl = document.querySelector("#volumeTotal");
const rowCountEl = document.querySelector("#rowCount");
const marketSearchEl = document.querySelector("#marketSearch");
const marketFilterEl = document.querySelector("#marketFilter");
const backtestSymbolEl = document.querySelector("#backtestSymbol");
const backtestIntervalEl = document.querySelector("#backtestInterval");
const backtestLimitEl = document.querySelector("#backtestLimit");
const backtestRewardRiskEl = document.querySelector("#backtestRewardRisk");
const backtestStopModeEl = document.querySelector("#backtestStopMode");
const backtestStatusEl = document.querySelector("#backtestStatus");
const backtestSummaryEl = document.querySelector("#backtestSummary");
const backtestGroupsEl = document.querySelector("#backtestGroups");
const strategyStatusEl = document.querySelector("#strategyStatus");
const strategySummaryEl = document.querySelector("#strategySummary");
const strategyTradesEl = document.querySelector("#strategyTrades");
const botPositionsEl = document.querySelector("#botPositions");
const livePositionCountEl = document.querySelector("#livePositionCount");
const botPositionHintEl = document.querySelector("#botPositionHint");
const manageWatchlistEl = document.querySelector("#manageWatchlist");
const toggleMarketDetailsEl = document.querySelector("#toggleMarketDetails");
const configuredLocalPorts = window.MONITOR_BOOTSTRAP?.localPorts;
const localPorts = Array.isArray(configuredLocalPorts) && configuredLocalPorts.length
  ? configuredLocalPorts.filter((port) => Number.isInteger(port) && port > 0 && port <= 65535)
  : [8080, 80];
const fallbackWatchlist = [
  { symbol: "BTCUSDT", email: "", signal: true, indicator_alert: true, support_alert: true, interval: "15m" },
  { symbol: "ETHUSDT", email: "", signal: true, indicator_alert: true, support_alert: true, interval: "15m" },
];

let latestState = null;
let eventSource = null;
let connectionStatus = "";
let isManagingWatchlist = false;
let showMarketDetails = false;

function formatNumber(value, digits = 8) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return Number(value).toLocaleString("en-US", {
    maximumFractionDigits: digits,
  });
}

function formatUsd(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return `$${Number(value).toLocaleString("en-US", {
    maximumFractionDigits: digits,
  })}`;
}

function formatCompactUsd(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number) || number <= 0) return "--";
  if (number >= 1_000_000_000) return `$${(number / 1_000_000_000).toFixed(2)}B`;
  if (number >= 1_000_000) return `$${(number / 1_000_000).toFixed(2)}M`;
  if (number >= 1_000) return `$${(number / 1_000).toFixed(2)}K`;
  return formatUsd(number);
}

function formatTime(epochSeconds) {
  if (!epochSeconds) return "--";
  return new Date(epochSeconds * 1000).toLocaleTimeString("zh-CN", {
    hour12: false,
  });
}

function addRow(item = {}) {
  const node = rowTemplate.content.cloneNode(true);
  const tr = node.querySelector("tr");
  tr.querySelector(".symbol").value = item.symbol || "";
  hydrateSymbolCell(tr);
  tr.querySelector(".supportAlert").checked = item.support_alert !== false;
  tr.querySelector(".signal").checked = item.signal !== false;
  tr.querySelector(".indicatorAlert").checked = item.indicator_alert !== false;
  tr.querySelector(".interval").value = item.interval || "15m";
  tr.querySelector(".symbol").readOnly = !isManagingWatchlist;
  tr.querySelector(".remove").addEventListener("click", () => {
    tr.remove();
    renderPrices();
    updateOverview(latestState || {});
  });
  coinRows.appendChild(tr);
  renderPrices();
}

function collectWatchlist() {
  return [...coinRows.querySelectorAll("tr")]
    .map((tr) => ({
      symbol: tr.querySelector(".symbol").value.trim().toUpperCase(),
      email: "",
      signal: tr.querySelector(".signal").checked,
      indicator_alert: tr.querySelector(".indicatorAlert").checked,
      support_alert: tr.querySelector(".supportAlert").checked,
      interval: tr.querySelector(".interval").value,
    }))
    .filter((item) => item.symbol);
}

async function saveWatchlist() {
  if (window.location.protocol === "file:") {
    saveMessageEl.textContent = "请先通过 HTTP 地址打开页面";
    return;
  }
  saveMessageEl.textContent = "保存中...";
  try {
    const response = await fetch("/api/watchlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ watchlist: collectWatchlist() }),
    });
    if (!response.ok) {
      throw new Error(`保存失败：HTTP ${response.status}`);
    }
    const result = await response.json();
    saveMessageEl.textContent = result.ok ? "已保存，正在刷新行情" : result.error || "保存失败";
  } catch (error) {
    saveMessageEl.textContent = error?.message || "网络异常，保存失败";
  }
  window.setTimeout(() => {
    saveMessageEl.textContent = "";
  }, 2200);
}

function renderState(state) {
  latestState = state;
  updatedAtEl.textContent = formatTime(state.updated_at);
  marketDataSourceEl.textContent = formatMarketDataSource(state.market_data_source);
  renderDiscordStatus(state);
  renderBotStatus(state.okx_bot_status || {});
  errorTextEl.textContent = state.last_error ? `行情拉取失败：${state.last_error}` : "";
  renderSystemHealth(state);
  updateOverview(state);
  renderBacktestSymbolOptions(state.watchlist || []);
  renderBotPositions(state.okx_bot_status || {});
  renderStrategyStats(state.strategy_stats || {}, state.strategy_trades || []);
  renderPrices();
  renderEvents(state.events || []);
}

function formatMarketDataSource(source) {
  if (source === "gate") return "Gate.io Spot";
  return "Binance 24h Ticker";
}

function formatPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return `${(Number(value) * 100).toFixed(2)}%`;
}

function updateOverview(state) {
  const watchlist = collectWatchlist();
  const prices = state.prices || {};
  const events = state.events || [];
  const signals = state.signals || {};
  const nowSeconds = Date.now() / 1000;
  const triggerCount = events.filter((event) => nowSeconds - Number(event.created_at || 0) <= 86400).length;
  const activeSignals = Object.values(signals).filter((signal) =>
    ["long", "short", "filtered_buy", "filtered_sell"].includes(signal?.signal),
  ).length;
  const volume = watchlist.reduce((total, item) => {
    const ticker = prices[item.symbol] || {};
    return total + Number(ticker.quoteVolume || ticker.volume || 0);
  }, 0);
  const botStatus = state.okx_bot_status || {};
  const positionCount = Number(botStatus.positions?.length || botStatus.open_positions || 0);

  if (livePositionCountEl) livePositionCountEl.textContent = formatNumber(positionCount, 0);
  if (watchCountEl) watchCountEl.textContent = formatNumber(watchlist.length, 0);
  if (watchMetaEl) watchMetaEl.textContent = watchlist.length ? `${watchlist[0].interval || "15m"} 默认周期` : "等待添加币种";
  if (triggerCountEl) triggerCountEl.textContent = formatNumber(triggerCount, 0);
  if (signalCountEl) signalCountEl.textContent = formatNumber(activeSignals, 0);
  if (volumeTotalEl) volumeTotalEl.textContent = formatCompactUsd(volume);
}

function renderBacktestSymbolOptions(watchlist) {
  const selected = backtestSymbolEl.value;
  const symbols = [...new Set(watchlist.map((item) => item.symbol).filter(Boolean))];
  if (!symbols.length) symbols.push("BTCUSDT");
  backtestSymbolEl.innerHTML = symbols.map((symbol) => `<option value="${escapeHtml(symbol)}">${escapeHtml(symbol)}</option>`).join("");
  backtestSymbolEl.value = symbols.includes(selected) ? selected : symbols[0];
}

function renderStrategyStats(stats, trades) {
  const openCount = Number(stats.open_trades || 0);
  const closedCount = Number(stats.closed_trades || 0);
  strategyStatusEl.textContent = `${closedCount} 笔已平仓 · ${openCount} 笔持仓中`;
  strategySummaryEl.innerHTML = `
    <div class="metric"><span>累计开仓</span><strong>${formatNumber(stats.total_trades, 0)}</strong></div>
    <div class="metric"><span>实盘胜率</span><strong class="${stats.win_rate >= 0.45 ? "up" : stats.win_rate >= 0.35 ? "warn" : "down"}">${formatPercent(stats.win_rate)}</strong></div>
    <div class="metric"><span>成功/失败</span><strong>${formatNumber(stats.wins, 0)} / ${formatNumber(stats.losses, 0)}</strong></div>
    <div class="metric"><span>平均结果</span><strong class="${stats.expectancy >= 0 ? "up" : "down"}">${formatPercent(stats.expectancy)}</strong></div>
    <div class="metric"><span>止损失败</span><strong>${formatPercent(stats.stop_loss_rate)}</strong></div>
    <div class="metric"><span>推保护成功</span><strong>${formatPercent(stats.protection_rate)}</strong></div>
  `;

  if (!trades.length) {
    strategyTradesEl.innerHTML = '<div class="tradeItem"><span>还没有策略开仓记录</span></div>';
    return;
  }

  strategyTradesEl.innerHTML = trades
    .slice(0, 8)
    .map((trade) => {
      const statusText = trade.status === "open" ? "持仓中" : trade.outcome === "win" ? "成功" : "失败";
      const statusClass = trade.status === "open" ? "warn" : trade.outcome === "win" ? "up" : "down";
      const resultValue = trade.status === "open" ? trade.unrealized_pct : trade.return_pct;
      return `
        <div class="tradeItem">
          <span>
            <b class="${trade.direction === "long" ? "up" : "down"}">${escapeHtml(trade.symbol)} ${escapeHtml(trade.direction)}</b>
            <small>${escapeHtml(trade.interval || "15m")} · 入场 ${formatNumber(trade.entry_price)} · 止损 ${formatNumber(trade.stop_loss)} · 保护 ${formatNumber(trade.protection_price)}</small>
          </span>
          <em class="${statusClass}">${statusText}</em>
          <strong class="${Number(resultValue || 0) >= 0 ? "up" : "down"}">${formatPercent(resultValue)}</strong>
        </div>
      `;
    })
    .join("");
}

function renderBotStatus(status) {
  const health = status.health || (status.ok ? "running" : "unknown");
  const running = health === "running";
  const warning = ["stale", "stalled", "error"].includes(health);
  botDot.className = `dot ${running ? "online" : warning ? "" : "offline"}`;
  botStateText.textContent = status.health_label || (running ? "运行中" : "未知");
  const positionCount = Number(status.positions?.length || status.open_positions || 0);
  const scanText = status.last_scan_at ? `最近扫描 ${formatTime(status.last_scan_at)}` : "暂无扫描记录";
  botDetail.textContent = `${positionCount} 个真实持仓 · ${scanText}`;
  if (botPositionHintEl) {
    botPositionHintEl.textContent = running ? `${positionCount} 个持仓 · ${scanText}` : `${status.health_label || "状态未知"} · ${scanText}`;
  }
}

function renderSystemHealth(state = {}) {
  if (!headerConnectionDot || !headerConnectionText) return;
  const botHealth = state.okx_bot_status?.health || (state.okx_bot_status?.ok ? "running" : "unknown");
  const botNeedsAttention = ["stale", "stalled", "error"].includes(botHealth);
  const hasError = Boolean(state.last_error || state.okx_bot_status?.last_error);
  let dotClass = "";
  let label = "连接中";

  if (connectionStatus === "offline") {
    dotClass = "offline";
    label = "连接异常";
  } else if (botNeedsAttention || hasError) {
    label = "需要关注";
  } else if (connectionStatus === "online") {
    dotClass = "online";
    label = "运行正常";
  }

  headerConnectionDot.className = `dot ${dotClass}`;
  headerConnectionText.textContent = label;
}

function setManagementMode(enabled) {
  isManagingWatchlist = enabled;
  document.body.classList.toggle("manageMode", enabled);
  manageWatchlistEl?.setAttribute("aria-pressed", String(enabled));
  if (manageWatchlistEl) manageWatchlistEl.textContent = enabled ? "完成管理" : "管理币种";
  coinRows.querySelectorAll(".symbol").forEach((input) => {
    input.readOnly = !enabled;
  });
}

function toggleMarketDetailView() {
  showMarketDetails = !showMarketDetails;
  document.body.classList.toggle("showMarketDetails", showMarketDetails);
  toggleMarketDetailsEl?.setAttribute("aria-pressed", String(showMarketDetails));
  if (toggleMarketDetailsEl) toggleMarketDetailsEl.textContent = showMarketDetails ? "收起细节" : "展开细节";
}

function renderBotPositions(status) {
  const positions = status.positions || [];
  if (!botPositionsEl) return;
  if (!positions.length) {
    botPositionsEl.innerHTML = '<div class="tradeItem botPosition"><span>真实机器人暂无持仓</span><em>--</em><strong>--</strong></div>';
    return;
  }

  botPositionsEl.innerHTML = positions
    .slice(0, 8)
    .map((position) => {
      const direction = String(position.direction || "").toLowerCase();
      const directionClass = direction === "long" ? "up" : direction === "short" ? "down" : "";
      const bot = position.bot === "binance" ? "Binance" : "OKX";
      return `
        <div class="tradeItem botPosition">
          <span>
            <b class="${directionClass}">${escapeHtml(position.symbol || "--")} ${escapeHtml(direction || "--")}</b>
            <small>${bot} · ${escapeHtml(position.interval || "15m")} · 入场 ${formatNumber(position.entry_price)} · 止损 ${formatNumber(position.stop_loss)} · 目标 ${formatNumber(position.target_price)}</small>
          </span>
          <em>${position.opened_at ? formatTime(position.opened_at) : "--"}</em>
          <strong>${formatNumber(position.notional_usdt, 2)}</strong>
        </div>
      `;
    })
    .join("");
}

async function runBacktest() {
  if (window.location.protocol === "file:") {
    backtestStatusEl.textContent = "请先通过 HTTP 地址打开页面";
    return;
  }

  const params = new URLSearchParams({
    symbol: backtestSymbolEl.value || "BTCUSDT",
    interval: backtestIntervalEl.value || "15m",
    limit: backtestLimitEl.value || "30000",
    reward_risk: backtestRewardRiskEl.value || "2",
    stop_mode: backtestStopModeEl.value || "all",
  });
  backtestStatusEl.textContent = "统计中...";
  backtestSummaryEl.innerHTML = "";
  backtestGroupsEl.innerHTML = "";

  try {
    const response = await fetch(`/api/backtest?${params.toString()}`, { cache: "no-store" });
    const result = await response.json();
    if (!result.ok) throw new Error(result.error || "统计失败");
    renderBacktestResult(result);
  } catch (error) {
    backtestStatusEl.textContent = error.message;
  }
}

function renderBacktestResult(result) {
  const metrics = result.metrics || {};
  const modeResults = result.stop_mode_results || [];
  backtestStatusEl.textContent = `${result.symbol} ${result.interval} · ${result.limit} 根K线`;

  if (modeResults.length > 1) {
    backtestSummaryEl.innerHTML = modeResults
      .map(({ stop_mode: stopMode, metrics: modeMetrics = {} }) => renderStopModeMetrics(stopMode, modeMetrics))
      .join("");
  } else {
    backtestSummaryEl.innerHTML = `
    <div class="metric"><span>交易次数</span><strong>${formatNumber(metrics.total_trades, 0)}</strong></div>
    <div class="metric"><span>胜率</span><strong class="${metrics.win_rate >= 0.45 ? "up" : metrics.win_rate >= 0.35 ? "warn" : "down"}">${formatPercent(metrics.win_rate)}</strong></div>
    <div class="metric"><span>预期/笔</span><strong class="${metrics.expectancy >= 0 ? "up" : "down"}">${formatPercent(metrics.expectancy)}</strong></div>
    <div class="metric"><span>最大回撤</span><strong class="down">${formatPercent(metrics.max_drawdown)}</strong></div>
    <div class="metric"><span>累计收益</span><strong class="${metrics.total_return >= 0 ? "up" : "down"}">${formatPercent(metrics.total_return)}</strong></div>
    <div class="metric"><span>止损触发</span><strong>${formatPercent(metrics.stop_trigger_rate)}</strong></div>
    <div class="metric"><span>推保护成功</span><strong>${formatPercent(metrics.protection_rate)}</strong></div>
  `;
  }

  const groups = result.groups || [];
  if (!groups.length) {
    backtestGroupsEl.innerHTML = '<div class="groupItem"><span>没有可分组的交易样本</span></div>';
    return;
  }

  backtestGroupsEl.innerHTML = `
    <div class="groupHeader">
      <span>组合</span>
      <span>次数</span>
      <span>胜率</span>
      <span>均值</span>
    </div>
    ${groups
      .map(
        (group) => `
          <div class="groupItem">
            <span>${escapeHtml(group.signal)} · ${escapeHtml(group.signal_grade || "weak")} · ${escapeHtml(group.score_bucket || "unknown")} · 均分 ${formatNumber(group.avg_score, 1)} · ${escapeHtml(group.trend)} / ${escapeHtml(group.higher_trend || "unknown")} · ${escapeHtml(group.divergence_type)} · ${escapeHtml(group.strength_bucket)}</span>
            <b>${group.trades}</b>
            <b class="${group.win_rate >= 0.45 ? "up" : group.win_rate >= 0.35 ? "warn" : "down"}">${formatPercent(group.win_rate)}</b>
            <b class="${group.avg_return >= 0 ? "up" : "down"}">${formatPercent(group.avg_return)}</b>
          </div>
        `,
      )
      .join("")}
  `;
}

function stopModeLabel(stopMode) {
  if (stopMode === "atr_trailing_after_1r") return "1R后ATR移动";
  if (stopMode === "structure_atr") return "结构ATR";
  return stopMode || "--";
}

function renderStopModeMetrics(stopMode, metrics) {
  return `
    <div class="metric stopMetric">
      <span>${escapeHtml(stopModeLabel(stopMode))}</span>
      <strong class="${metrics.expectancy >= 0 ? "up" : "down"}">${formatPercent(metrics.expectancy)}</strong>
      <small>
        ${formatNumber(metrics.total_trades, 0)}笔 · 胜率 ${formatPercent(metrics.win_rate)} · 回撤 ${formatPercent(metrics.max_drawdown)} · 止损 ${formatPercent(metrics.stop_trigger_rate)} · 推保护 ${formatPercent(metrics.protection_rate)}
      </small>
    </div>
  `;
}

function renderPrices() {
  if (!latestState) return;
  const prices = latestState.prices || {};
  const signals = latestState.signals || {};
  const indicatorSignals = latestState.indicator_signals || {};
  const supportLevels = latestState.support_levels || {};
  [...coinRows.querySelectorAll("tr")].forEach((tr) => {
    const symbol = tr.querySelector(".symbol").value.trim().toUpperCase();
    const interval = tr.querySelector(".interval").value;
    const ticker = prices[symbol];
    const signal = signals[`${symbol}:${interval}`];
    const multiSignals = indicatorSignals[symbol] || {};
    const supports = supportLevels[symbol] || {};
    const priceEl = tr.querySelector(".price");
    const changeEl = tr.querySelector(".change");
    const rangeEl = tr.querySelector(".range");
    const supportEl = tr.querySelector(".supportLevels");
    const signalEl = tr.querySelector(".signalText");
    hydrateSymbolCell(tr, symbol, interval);
    tr.dataset.symbol = symbol;
    if (!ticker) {
      priceEl.textContent = "--";
      changeEl.textContent = "--";
      changeEl.className = "change";
      rangeEl.textContent = "--";
      supportEl.innerHTML = formatSupportLevels(supports);
      signalEl.textContent = "--";
      signalEl.title = "";
      signalEl.className = "signalText";
      tr.dataset.change = "flat";
      tr.dataset.signalState = "none";
      return;
    }

    const change = Number(ticker.priceChangePercent || 0);
    const price = Number(ticker.lastPrice || 0);
    const high = Number(ticker.highPrice || 0);
    const low = Number(ticker.lowPrice || 0);
    const rangePosition = high > low ? Math.max(0, Math.min(100, ((price - low) / (high - low)) * 100)) : 50;
    priceEl.innerHTML = `${formatNumber(price)}<small>≈ ${formatUsd(price)}</small>`;
    changeEl.innerHTML = `${change.toFixed(2)}%<small>${formatNumber(ticker.priceChange || 0, 4)}</small>`;
    changeEl.className = `change ${change >= 0 ? "up" : "down"}`;
    rangeEl.innerHTML = `${formatNumber(high, 4)}<small>${formatNumber(low, 4)}</small><span class="rangeBar" style="--range-position: ${rangePosition}%"><i></i></span>`;
    supportEl.innerHTML = formatSupportLevels(supports);
    const tdText = signal?.td_summary ? ` · TD ${signal.td_summary}` : "";
    const shortSignalText = signal ? signal.signal_name : "等待背驰";
    const indicatorSummary = Object.entries(multiSignals)
      .map(([signalInterval, item]) => `${signalInterval}: ${item?.ma?.label || "MA等待"} / ${item?.macd?.label || "MACD等待"}`)
      .join("；");
    signalEl.innerHTML = `
      <span class="chanlunLine">${escapeHtml(shortSignalText)}</span>
      ${formatIndicatorLevels(multiSignals)}
    `;
    signalEl.title = `${shortSignalText}${tdText}${indicatorSummary ? `；${indicatorSummary}` : ""}`;
    signalEl.className = `signalText ${signal?.signal === "long" ? "up" : signal?.signal === "short" ? "down" : signal?.signal === "filtered_buy" || signal?.signal === "filtered_sell" ? "warn" : ""}`;
    tr.dataset.change = change >= 0 ? "up" : "down";
    tr.dataset.signalState = signal?.signal && signal.signal !== "none" ? "active" : "none";
  });
  applyMarketFilters();
}

function hydrateSymbolCell(tr, symbolValue, intervalValue) {
  const symbol = (symbolValue || tr.querySelector(".symbol")?.value || "").trim().toUpperCase();
  const interval = intervalValue || tr.querySelector(".interval")?.value || "15m";
  const badge = tr.querySelector(".assetBadge");
  const meta = tr.querySelector(".symbolMeta");
  const base = symbol.replace(/USDT$|USD$|PERP$/g, "");
  if (badge) badge.textContent = base ? base.slice(0, 3) : "--";
  if (meta) meta.textContent = `${interval} · 永续监控`;
}

function applyMarketFilters() {
  const query = (marketSearchEl?.value || "").trim().toUpperCase();
  const filter = marketFilterEl?.value || "all";
  let visible = 0;

  [...coinRows.querySelectorAll("tr")].forEach((tr) => {
    const symbol = tr.dataset.symbol || tr.querySelector(".symbol").value.trim().toUpperCase();
    const matchesQuery = !query || symbol.includes(query);
    const matchesFilter =
      filter === "all" ||
      (filter === "up" && tr.dataset.change === "up") ||
      (filter === "down" && tr.dataset.change === "down") ||
      (filter === "signal" && tr.dataset.signalState === "active");
    const show = matchesQuery && matchesFilter;
    tr.classList.toggle("is-hidden", !show);
    if (show) visible += 1;
  });

  if (rowCountEl) rowCountEl.textContent = `共 ${visible} 条`;
}

function formatSupportLevels(levels) {
  const intervals = ["15m", "1h", "4h", "1d"];
  return intervals
    .map((interval) => {
      const level = levels[interval];
      const touched = level?.touched;
      const distance = level?.distance_pct;
      const distanceText = typeof distance === "number" ? `${distance.toFixed(2)}%` : "--";
      return `
        <span class="supportLevel ${touched ? "warn" : ""}">
          <b>${interval}</b>
          <em>${formatNumber(level?.support)}</em>
          <small>${level?.reason ? escapeHtml(level.reason) : distanceText}</small>
        </span>
      `;
    })
    .join("");
}

function formatIndicatorLevels(signals) {
  const intervals = ["15m", "1h", "4h", "1d"];
  return `
    <div class="indicatorLevels">
      ${intervals
        .map((interval) => {
          const signal = signals[interval];
          const ma = signal?.ma;
          const macd = signal?.macd;
          return `
            <span class="indicatorLevel">
              <b>${interval}</b>
              <em class="${indicatorClass(ma?.direction)}">${escapeHtml(ma?.label || "MA等待")}</em>
              <em class="${indicatorClass(macd?.direction)}">${escapeHtml(macd?.label || "MACD等待")}</em>
            </span>
          `;
        })
        .join("")}
    </div>
  `;
}

function indicatorClass(direction) {
  if (direction === "up") return "up";
  if (direction === "down") return "down";
  return "";
}

function renderEvents(events) {
  if (!events.length) {
    eventsEl.innerHTML = '<div class="event"><span>暂无触发记录</span></div>';
    return;
  }
  eventsEl.innerHTML = events
    .map((event) => {
      if (event.type === "chanlun") {
        const error = formatNotificationErrors(event);
        return `
          <div class="event">
            <strong>${escapeHtml(event.symbol)} ${escapeHtml(event.signal_name)}</strong>
            <span>${escapeHtml(event.interval || "15m")} · 参考价 ${formatNumber(event.price)} · 强度 ${formatNumber(event.strength, 2)} · ${formatTime(event.created_at)}</span>
            ${event.filter ? `<span>${escapeHtml(event.filter)}</span>` : ""}
            ${event.td_summary ? `<span>TD9 ${escapeHtml(event.td_summary)}</span>` : ""}
            ${error}
          </div>
        `;
      }

      if (event.type === "indicator") {
        const error = formatNotificationErrors(event);
        return `
          <div class="event">
            <strong>${escapeHtml(event.symbol)} ${escapeHtml(event.interval || "")} ${escapeHtml(event.indicator_label || event.indicator_name || "指标提醒")}</strong>
            <span>参考价 ${formatNumber(event.price)} · ${formatTime(event.created_at)}</span>
            ${error}
          </div>
        `;
      }

      if (event.type === "support") {
        const error = formatNotificationErrors(event);
        return `
          <div class="event">
            <strong>${escapeHtml(event.symbol)} ${escapeHtml(event.interval || "")} 支撑触发 ${formatNumber(event.support)}</strong>
            <span>当前价 ${formatNumber(event.price)} · 距离 ${formatNumber(event.distance_pct, 2)}% · ${formatTime(event.created_at)}</span>
            ${error}
          </div>
        `;
      }

      const direction = event.direction === "above" ? "高于" : "低于";
      const error = formatNotificationErrors(event);
      return `
        <div class="event">
          <strong>${escapeHtml(event.symbol)} ${direction} ${formatNumber(event.threshold)}</strong>
          <span>触发价 ${formatNumber(event.price)} · ${formatTime(event.created_at)}</span>
          ${error}
        </div>
      `;
    })
    .join("");
}

function renderDiscordStatus(state) {
  const status = state.discord_status || {};
  const configured = Boolean(status.configured || state.discord_configured);
  const running = Boolean(status.running);
  discordDot.className = `dot ${running ? "online" : configured ? "" : "offline"}`;
  discordStateText.textContent = status.label || (running ? "运行中" : configured ? "异常" : "未配置");
  if (!configured) {
    discordDetail.textContent = "需要 DISCORD_WEBHOOK_URL";
  } else if (status.last_error) {
    discordDetail.textContent = status.last_error;
  } else if (status.last_ok_at) {
    discordDetail.textContent = `最近发送成功 ${formatTime(status.last_ok_at)}`;
  } else {
    discordDetail.textContent = "已配置，等待首次发送";
  }
}

function formatNotificationErrors(event) {
  const errors = [];
  if (event.discord_error) errors.push(`Discord未发送：${event.discord_error}`);
  return errors.map((error) => `<span class="mailError">${escapeHtml(error)}</span>`).join("");
}

async function findLocalServer() {
  for (const port of localPorts) {
    const baseUrl = `http://127.0.0.1:${port}/`;
    try {
      const response = await fetch(`${baseUrl}api/state`, { cache: "no-store" });
      if (response.ok) return baseUrl;
    } catch (_error) {
      // Continue trying the next known development port.
    }
  }
  return "";
}

function renderFileModeNotice() {
  latestState = {
    watchlist: fallbackWatchlist,
    prices: {},
    signals: {},
    events: [],
    updated_at: null,
    smtp_configured: false,
    discord_configured: false,
    discord_status: { configured: false, running: false, label: "需 HTTP 服务" },
    okx_bot_status: { health: "unknown", health_label: "需 HTTP 服务", positions: [] },
    notification_configured: false,
    strategy_stats: {},
    strategy_trades: [],
  };
  coinRows.innerHTML = "";
  fallbackWatchlist.forEach(addRow);
  updatedAtEl.textContent = "--";
  renderDiscordStatus(latestState);
  renderBotStatus(latestState.okx_bot_status);
  renderBotPositions(latestState.okx_bot_status);
  setConnection("offline", "文件模式");
  errorTextEl.innerHTML =
    '当前是 file:// 打开，无法连接实时行情接口。请运行 <code>python3 app.py</code> 后打开终端打印的 HTTP 地址，默认是 <a class="textLink" href="http://127.0.0.1:8080/">http://127.0.0.1:8080/</a>。';
  eventsEl.innerHTML =
    '<div class="event"><strong>等待 HTTP 服务</strong><span>直接打开 HTML 只能看到静态界面，行情、缠论信号和 Discord 状态都需要后端接口。</span></div>';
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => {
    const map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" };
    return map[char];
  });
}

async function init() {
  if (window.location.protocol === "file:") {
    setConnection("offline", "寻找 HTTP 服务");
    errorTextEl.textContent = "正在寻找本地 HTTP 服务...";
    const serverUrl = await findLocalServer();
    if (serverUrl) {
      window.location.href = serverUrl;
      return;
    }
    renderFileModeNotice();
    return;
  }

  const startedAt = performance.now();
  const response = await fetch("/api/state");
  if (apiLatencyEl) apiLatencyEl.textContent = `${Math.round(performance.now() - startedAt)}ms`;
  const state = await response.json();
  coinRows.innerHTML = "";
  (state.watchlist || []).forEach(addRow);
  renderState(state);

  eventSource?.close();
  eventSource = new EventSource("/api/events");
  eventSource.onopen = () => setConnection("online", "实时连接");
  eventSource.onerror = () => setConnection("offline", "连接重试中");
  eventSource.onmessage = (event) => {
    try {
      renderState(JSON.parse(event.data));
    } catch (error) {
      setConnection("offline", "数据异常");
      errorTextEl.textContent = error?.message || "实时数据解析失败";
    }
  };
}

function setConnection(status, text) {
  connectionStatus = status;
  if (connectionDot) connectionDot.className = `dot ${status}`;
  if (connectionText) connectionText.textContent = text;
  renderSystemHealth(latestState || {});
}

document.querySelector("#addRow").addEventListener("click", () => addRow());
document.querySelector("#saveList").addEventListener("click", saveWatchlist);
document.querySelector("#runBacktest").addEventListener("click", runBacktest);
manageWatchlistEl?.addEventListener("click", () => setManagementMode(!isManagingWatchlist));
toggleMarketDetailsEl?.addEventListener("click", toggleMarketDetailView);
coinRows.addEventListener("input", () => {
  renderPrices();
  updateOverview(latestState || {});
});
marketSearchEl?.addEventListener("input", applyMarketFilters);
marketFilterEl?.addEventListener("change", applyMarketFilters);
window.addEventListener("beforeunload", () => {
  eventSource?.close();
});

init().catch((error) => {
  setConnection("offline", "启动失败");
  errorTextEl.textContent = error.message;
});
