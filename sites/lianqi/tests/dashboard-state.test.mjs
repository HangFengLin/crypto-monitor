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
async function mountedDashboard(initial) {
  const url = new URL('../app/lianqi-dashboard.tsx', import.meta.url);
  const require = createRequire(url), ts = require('typescript'), React = require('react');
  const { renderToStaticMarkup } = require('react-dom/server');
  const slots = [], effects = [], scheduled = [];
  let cursor = 0, stream;
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
    ...api, async fetchWithTimeout(path) {
      assert.equal(path, '/api/state');
      return { ok: true, async json() { return initial; } };
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
  const render = () => { cursor = 0; return renderToStaticMarkup(loaded.exports.LianqiDashboard()); };
  render();
  const cleanups = effects.map(effect => effect()).filter(Boolean);
  for (const fn of scheduled) fn();
  await new Promise(resolve => setImmediate(resolve));
  return { render, push(payload) { stream.onmessage({ data: JSON.stringify(payload) }); },
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

test('same-time valid updates and reconnect snapshots are still accepted', async () => {
  const dashboard = await mountedDashboard(snapshot(1000));
  try {
    dashboard.push(snapshot(1000, 'ADAUSDT', 105));
    assert.match(dashboard.render(), /最新价<\/span><strong>105/);
    dashboard.push(snapshot(1000, 'ETHUSDT', 106));
    assert.match(dashboard.render(), /ETHUSDT/);
    dashboard.push(snapshot(1001, 'BTCUSDT', 107));
    assert.match(dashboard.render(), /BTCUSDT/);
  } finally { dashboard.close(); }
});
