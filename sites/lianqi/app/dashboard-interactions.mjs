export function finiteMetric(value) {
  if (value == null || (typeof value === 'string' && !value.trim())) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function selectMarketRows(watchlist, state, query = '', filter = 'all', sort = 'default') {
  const search = query.trim().toUpperCase();
  const rows = watchlist.filter(item => {
    const symbol = item.symbol.trim().toUpperCase();
    const change = finiteMetric(state?.prices?.[symbol]?.priceChangePercent);
    const signal = state?.signals?.[`${symbol}:${item.interval}`]?.signal;
    return (!search || symbol.includes(search)) && (
      filter === 'all' ||
      (filter === 'up' && change !== null && change >= 0) ||
      (filter === 'down' && change !== null && change < 0) ||
      (filter === 'signal' && ['long', 'short', 'filtered_buy', 'filtered_sell'].includes(signal))
    );
  });
  if (sort === 'default') return rows;
  return rows.sort((left, right) => {
    if (sort === 'symbol') return left.symbol.localeCompare(right.symbol);
    const metric = sort === 'volume-desc' ? 'quoteVolume' : 'priceChangePercent';
    const a = finiteMetric(state?.prices?.[left.symbol.trim().toUpperCase()]?.[metric]);
    const b = finiteMetric(state?.prices?.[right.symbol.trim().toUpperCase()]?.[metric]);
    if (a === null) return b === null ? 0 : 1;
    if (b === null) return -1;
    return sort === 'change-asc' ? a - b : b - a;
  });
}

export function prepareWatchlist(items) {
  const seen = new Set();
  return items.map((item, index) => {
    const symbol = item.symbol.trim().toUpperCase();
    if (!symbol) throw new Error(`第 ${index + 1} 行未填写交易对，请填写或删除该行。`);
    if (!/^[A-Z0-9]+USDT$/.test(symbol)) throw new Error(`第 ${index + 1} 行交易对格式不正确，请使用 BTCUSDT 这样的名称。`);
    if (seen.has(symbol)) throw new Error(`交易对重复：${symbol}，请保留一行。`);
    seen.add(symbol);
    return { ...item, symbol, email: '' };
  });
}

export function viewFromHash(hash) {
  const view = hash.replace(/^#/, '');
  return ['overview', 'markets', 'paper', 'research', 'settings'].includes(view) ? view : 'overview';
}

// GET and SSE can both deliver snapshots captured before a completed save.
export function createWatchlistSync() {
  let generation = 0;
  let saving = false;
  let expected = null;
  const signature = items => JSON.stringify(items.map(item => [
    item.symbol, item.interval, item.signal, item.indicator_alert, item.support_alert, item.email || '',
  ]));
  return {
    generation: () => generation,
    beginSave() { generation++; saving = true; },
    finishSave(items) { generation++; saving = false; expected = signature(items); },
    failSave() { generation++; saving = false; expected = null; },
    /** @param {number | null} [requestGeneration] */
    accept(items, requestGeneration = null) {
      if (saving) return false;
      if (requestGeneration !== null) {
        if (requestGeneration !== generation) return false;
        if (expected !== null) expected = signature(items);
        return true;
      }
      if (expected !== null) {
        if (signature(items) !== expected) return false;
        expected = null;
      }
      return true;
    },
  };
}
