import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import test from 'node:test';

const watch = symbol => ({ symbol, interval: '15m', signal: true, indicator_alert: true, support_alert: true });
const snapshot = (updated_at, symbol = 'ADAUSDT', price = 104) => ({
  updated_at, watchlist: [watch(symbol)], prices: { [symbol]: { lastPrice: price } },
  signals: {}, indicator_signals: {}, support_levels: {}, events: [], strategy_stats: {}, strategy_trades: [],
  last_error: null, signal_tracking: { mode: 'paper', positions: [], enabled: false },
});

// Exercise the real GET/SSE applyState path. Only browser scheduling/network are
// controlled; the component, snapshot validation and synchronization stay real.
async function mountedDashboard(initial, { saveOk = true } = {}) {
  const url = new URL('../app/lianqi-dashboard.tsx', import.meta.url);
  const require = createRequire(url), ts = require('typescript'), React = require('react');
  const { renderToStaticMarkup } = require('react-dom/server');
  const slots = [], effects = [], scheduled = [];
  let cursor = 0, stream, lastTree, serverSnapshot = structuredClone(initial);
  const hooks = { ...React,
    useState(initialValue) {
      const i = cursor++;
      if (!(i in slots)) slots[i] = initialValue;
      return [slots[i], next => { slots[i] = typeof next === 'function' ? next(slots[i]) : next; }];
    },
    useRef(initialValue) {
      const i = cursor++;
      if (!(i in slots)) slots[i] = { current: initialValue };
      return slots[i];
    },
    useEffect(effect) { effects.push(effect); }, useCallback(fn) { return fn; }, useMemo(fn) { return fn(); },
  };
  const api = require('../app/api-client.mjs');
  const compile = source => ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  const fixtureRequire = name => name === 'react' ? hooks : name === './api-client.mjs' ? {
    ...api, async fetchWithTimeout(path, options = {}) {
      if (path === '/api/state') return { ok: true, async json() { return structuredClone(serverSnapshot); } };
      assert.equal(path, '/api/watchlist');
      assert.equal(options.method, 'POST');
      if (!saveOk) return { ok: false, async json() { return { detail: 'fixture save rejected' }; } };
      // Production saves the list without changing the market scan timestamp.
      serverSnapshot.watchlist = JSON.parse(options.body).watchlist;
      return { ok: true, async json() { return { ok: true, watchlist: structuredClone(serverSnapshot.watchlist) }; } };
    },
  } : name.startsWith('./') && !name.endsWith('.mjs') ? (() => {
    const child = { exports: {} };
    new Function('require', 'module', 'exports', compile(readFileSync(new URL(name + '.tsx', url), 'utf8')))(
      fixtureRequire, child, child.exports);
    return child.exports;
  })() : require(name);
  const window = { location: { hash: '#markets' }, setTimeout(fn) { scheduled.push(fn); return 0; },
    setInterval() { return 0; }, clearTimeout() {}, clearInterval() {}, addEventListener() {},
    removeEventListener() {}, requestAnimationFrame(fn) { fn(); } };
  class EventSource { constructor() { stream = { close() {}, onmessage: null }; return stream; } }
  const compiled = ts.transpileModule(await readFile(url, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } });
  const loaded = { exports: {} };
  new Function('require', 'module', 'exports', 'window', 'EventSource', compiled.outputText)(
    fixtureRequire, loaded, loaded.exports, window, EventSource);
  const render = () => {
    cursor = 0;
    lastTree = loaded.exports.LianqiDashboard();
    return renderToStaticMarkup(lastTree);
  };
  const nodes = (node, predicate) => {
    if (Array.isArray(node)) return node.flatMap(child => nodes(child, predicate));
    if (!node || typeof node !== 'object') return [];
    return [...(predicate(node) ? [node] : []), ...nodes(node.props?.children, predicate)];
  };
  const button = predicate => {
    render();
    const matches = nodes(lastTree, node => node.type === 'button' && predicate(node.props));
    assert.equal(matches.length, 1, 'Expected one real dashboard button');
    return matches[0].props;
  };
  render();
  const cleanups = effects.map(effect => effect()).filter(Boolean);
  for (const fn of scheduled) fn();
  await new Promise(resolve => setImmediate(resolve));
  return { render, push(payload) { stream.onmessage({ data: JSON.stringify(payload) }); },
    async save(symbol) {
      button(props => props.children === '管理币种').onClick();
      render();
      const row = nodes(lastTree, node => node.type?.name === 'MarketRow' && node.props.managing)[0];
      assert.ok(row, 'Expected the real editable MarketRow');
      row.props.onChange(row.props.index, { symbol });
      const save = button(props => props.children === '保存监控列表');
      assert.equal(save.disabled, false, 'Changed draft must be saveable');
      await save.onClick();
      await new Promise(resolve => setImmediate(resolve));
    },
    cancelEditing() { button(props => props.children === '取消修改').onClick(); },
    async refresh(payload) {
      serverSnapshot = structuredClone(payload);
      await button(props => props['aria-label'] === '刷新实时数据').onClick();
    },
    close() { for (const cleanup of cleanups) cleanup(); } };
}

test('older SSE cannot roll back GET state after reload', async () => {
  const dashboard = await mountedDashboard(snapshot(1000));
  try {
    assert.match(dashboard.render(), /ADAUSDT/);
    dashboard.push(snapshot(820, 'SOLUSDT', 500));
    const html = dashboard.render();
    assert.match(html, /ADAUSDT/);
    assert.doesNotMatch(html, /SOLUSDT/);
    assert.match(html, /最新价<\/span><strong>104/);
  } finally { dashboard.close(); }
});

test('same-list same-time price updates and reconnect snapshots are still accepted', async () => {
  const dashboard = await mountedDashboard(snapshot(1000));
  try {
    dashboard.push(snapshot(1000, 'ADAUSDT', 105));
    assert.match(dashboard.render(), /最新价<\/span><strong>105/);
    dashboard.push(snapshot(1001, 'ADAUSDT', 107));
    assert.match(dashboard.render(), /最新价<\/span><strong>107/);
  } finally { dashboard.close(); }
});

for (const clock of [1000, null]) {
  test(`reload GET anchors its list against old SSE with the same ${clock} scan clock`, async () => {
    const dashboard = await mountedDashboard(snapshot(clock));
    try {
      assert.match(dashboard.render(), /ADAUSDT/);
      dashboard.push(snapshot(clock, 'SOLUSDT'));
      const html = dashboard.render();
      assert.match(html, /ADAUSDT/);
      assert.doesNotMatch(html, /SOLUSDT/);
    } finally { dashboard.close(); }
  });

  test(`matching SSE after a real successful save cannot release the ${clock} list anchor`, async () => {
    const dashboard = await mountedDashboard(snapshot(clock, 'SOLUSDT'));
    try {
      await dashboard.save('ADAUSDT');
      assert.match(dashboard.render(), /ADAUSDT/);
      dashboard.push(snapshot(clock, 'ADAUSDT', 105));
      assert.match(dashboard.render(), /最新价<\/span><strong>105/);
      dashboard.push(snapshot(clock, 'SOLUSDT'));
      const html = dashboard.render();
      assert.match(html, /ADAUSDT/);
      assert.doesNotMatch(html, /SOLUSDT/);
    } finally { dashboard.close(); }
  });
}

test('failed save preserves the confirmed list when its draft is cancelled', async () => {
  const dashboard = await mountedDashboard(snapshot(1000), { saveOk: false });
  try {
    await dashboard.save('ETHUSDT');
    assert.match(dashboard.render(), /fixture save rejected/);
    dashboard.cancelEditing();
    assert.match(dashboard.render(), /ADAUSDT/);
    dashboard.push(snapshot(1000, 'SOLUSDT'));
    const html = dashboard.render();
    assert.match(html, /ADAUSDT/);
    assert.doesNotMatch(html, /SOLUSDT/);
  } finally { dashboard.close(); }
});

test('a later successful GET confirms an external list change and replaces the SSE anchor', async () => {
  const dashboard = await mountedDashboard(snapshot(1000));
  try {
    dashboard.push(snapshot(1000, 'ETHUSDT'));
    assert.match(dashboard.render(), /ADAUSDT/);
    await dashboard.refresh(snapshot(1000, 'ETHUSDT', 106));
    const confirmed = dashboard.render();
    assert.match(confirmed, /ETHUSDT/);
    assert.doesNotMatch(confirmed, /ADAUSDT/);
    dashboard.push(snapshot(1000, 'ETHUSDT', 107));
    assert.match(dashboard.render(), /最新价<\/span><strong>107/);
    dashboard.push(snapshot(1000, 'ADAUSDT'));
    const afterOld = dashboard.render();
    assert.match(afterOld, /ETHUSDT/);
    assert.doesNotMatch(afterOld, /ADAUSDT/);
  } finally { dashboard.close(); }
});
