import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import test from 'node:test';
import { viewFromHash } from '../app/dashboard-interactions.mjs';

const now = 1793286000000;
const candle = (offset, close) => ({ open_time: now - 900000 * (8 - offset), close_time: now - 900000 * (7 - offset),
  open: close - 1, high: close + 2, low: close - 2, close, volume: 100 });
const patterns = [
  { id: 'eth-bottom', pattern: 'double_bottom', symbol: 'ETHUSDT', interval: '15m', direction: 'long',
    stage: 'forming', level: 2560, invalid_level: 2490, detected_at_ms: now - 120000,
    updated_at_ms: now - 120000, available_at_ms: now - 120000, reason: 'second_pivot_confirmed',
    chart: Array.from({ length: 7 }, (_, i) => candle(i, 2510 + i * 5)),
    points: [{ time_ms: now - 4500000, price: 2500, kind: 'first_bottom' }] },
  { id: 'btc-retest', pattern: 'breakout_retest', symbol: 'BTCUSDT', interval: '1h', direction: 'short',
    stage: 'confirmed', level: 77000, invalid_level: 77400, detected_at_ms: now - 300000,
    updated_at_ms: now - 60000, available_at_ms: now - 60000, reason: 'retest_confirmed',
    chart: Array.from({ length: 7 }, (_, i) => candle(i, 76800 - i * 5)), points: [] },
  { id: 'sol-expired', pattern: 'double_top', symbol: 'SOLUSDT', interval: '15m', direction: 'short',
    stage: 'expired', level: 120, invalid_level: 123, updated_at_ms: now - 40000, chart: [], points: [] },
];
const snapshot = { status: 'healthy', updated_at: now / 1000, last_scan_at: now / 1000,
  symbols: ['ETHUSDT', 'BTCUSDT', 'SOLUSDT'], intervals: ['15m', '1h'], mode: 'observation_only',
  counts: { forming: 1, confirmed: 1, invalidated: 0, expired: 1 }, patterns, events: [] };

async function mounted(props) {
  const url = new URL('../app/pattern-monitor.tsx', import.meta.url), require = createRequire(url);
  const ts = require('typescript'), React = require('react'), { renderToStaticMarkup } = require('react-dom/server');
  const source = await readFile(url, 'utf8').catch(() => '');
  const states = []; let cursor = 0;
  const hooks = { ...React, useState(initial) {
    const index = cursor++;
    if (!(index in states)) states[index] = typeof initial === 'function' ? initial() : initial;
    return [states[index], value => { states[index] = typeof value === 'function' ? value(states[index]) : value; }];
  }, useRef(initial) {
    const index = cursor++;
    if (!(index in states)) states[index] = { current: initial };
    return states[index];
  }, useMemo(fn) { return fn(); }, useEffect() {} };
  const compiled = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } });
  const loaded = { exports: {} };
  const fixtureRequire = name => name === 'react' ? hooks :
    ['./api-client.mjs', './refresh-loop.mjs'].includes(name) ? {} : require(name);
  new Function('require', 'module', 'exports', compiled.outputText)(fixtureRequire, loaded, loaded.exports);
  assert.equal(typeof loaded.exports.PatternPanel, 'function', 'the pattern observation panel is not implemented');
  let tree;
  function render() {
    cursor = 0; tree = loaded.exports.PatternPanel({ nowMs: now, ...props });
    return renderToStaticMarkup(tree);
  }
  function nodes(node) {
    if (!node || typeof node !== 'object') return [];
    if (Array.isArray(node)) return node.flatMap(nodes);
    return [node, ...nodes(node.props?.children)];
  }
  return { render, click(label) {
    render();
    const button = nodes(tree).find(node => node.type === 'button' && node.props['aria-label'] === label);
    assert.ok(button, `missing accessible control: ${label}`); button.props.onClick();
    return render();
  } };
}

test('pattern hash opens the observation view and unrelated hashes still fall back', () => {
  assert.equal(viewFromHash('#patterns'), 'patterns');
  assert.equal(viewFromHash('#unrelated'), 'overview');
});

test('empty warmup distinguishes waiting from a confirmed absence of patterns', async () => {
  const panel = await mounted({ snapshot: { status: 'starting', patterns: [], counts: {} } });
  const html = panel.render();
  assert.match(html, /正在预热/); assert.match(html, /仅观察，不自动开仓/);
  assert.match(html, /已收盘/); assert.match(html, /等待/);
  assert.doesNotMatch(html, /监控正常|高胜率|累计盈利/);
});

test('a selected shape renders frozen levels, chart, causal observation time and direction', async () => {
  const panel = await mounted({ snapshot, initialPatternId: 'eth-bottom' });
  const html = panel.render();
  assert.match(html, /双底颈线/); assert.match(html, /2,560/); assert.match(html, /2,490/);
  assert.match(html, /形态关键位/); assert.match(html, /失效边界/); assert.match(html, /<svg/);
  assert.match(html, /实际观察时间/); assert.match(html, /待确认/);
  assert.doesNotMatch(html, /sol-expired/);
});

test('selecting another formation changes the actual chart and levels', async () => {
  const panel = await mounted({ snapshot });
  const html = panel.click('查看 BTCUSDT 1h 突破回踩');
  assert.match(html, /aria-label="BTCUSDT 1h 突破回踩形态图"/);
  assert.match(html, /77,000/); assert.match(html, /77,400/); assert.match(html, /观察偏空/);
  assert.match(html, /aria-pressed="true"[^>]*>[^]*?BTCUSDT/);
});

test('phase filtering reveals ended formations and clear filtering restores active opportunities', async () => {
  const panel = await mounted({ snapshot });
  const expired = panel.click('筛选 已到期');
  assert.match(expired, /SOLUSDT/); assert.doesNotMatch(expired, /查看 ETHUSDT/);
  assert.match(expired, /足够的K线/);
  const active = panel.click('筛选 进行中');
  assert.match(active, /查看 ETHUSDT/); assert.match(active, /查看 BTCUSDT/);
  assert.doesNotMatch(active, /查看 SOLUSDT/);
});

test('stale and failed polling preserve old evidence without calling it live healthy data', async () => {
  const old = await mounted({ snapshot: { ...snapshot, last_scan_at: (now - 180000) / 1000 } });
  assert.match(old.render(), /数据已过期/); assert.doesNotMatch(old.render(), />监控正常</);
  const failed = await mounted({ snapshot, error: '形态接口暂不可用' });
  const html = failed.render(); assert.match(html, /上次快照/); assert.match(html, /暂不可用/);
  assert.doesNotMatch(html, />监控正常</);
});

test('scan heartbeat determines freshness even when the last closed candle is naturally old', async () => {
  const recentScan = await mounted({ snapshot: { ...snapshot,
    updated_at: (now - 900000) / 1000, last_scan_at: (now - 10000) / 1000 } });
  const html = recentScan.render();
  assert.equal(html.includes('>监控正常<'), true);
  assert.equal(html.includes('数据已过期'), false);
  const legacy = await mounted({ snapshot: { ...snapshot, last_scan_at: undefined,
    updated_at: (now - 180000) / 1000 } });
  assert.equal(legacy.render().includes('数据已过期'), true);
});

test('a notification deep link can open an ended shape without silently showing another chart', async () => {
  const ended = await mounted({ snapshot, initialPatternId: 'sol-expired' });
  const html = ended.render();
  assert.match(html, /SOLUSDT · 15m/); assert.match(html, /双顶颈线/);
  assert.match(html, /aria-label="筛选 全部" aria-pressed="true"/);
  const missing = await mounted({ snapshot, initialPatternId: 'no-longer-retained' });
  const absent = missing.render();
  assert.match(absent, /这条形态已不在近期列表中/);
  assert.doesNotMatch(absent, /<svg/);
});

test('warmup and backfill identities are visible instead of suggesting new live alerts', async () => {
  const panel = await mounted({ snapshot: { ...snapshot, patterns: [
    { ...patterns[0], origin: 'warmup' }, { ...patterns[1], origin: 'backfill' },
  ] } });
  const html = panel.render();
  assert.match(html, /历史识别/); assert.match(html, /补录识别/);
  assert.doesNotMatch(html, />新观察</);
});

test('malformed chart observations never create NaN geometry or invented levels', async () => {
  const panel = await mounted({ snapshot: { ...snapshot, patterns: [{ ...patterns[0], level: null,
    invalid_level: NaN, chart: [{ close: NaN }, { ...candle(1, 2510), high: Infinity }] }] } });
  const html = panel.render();
  assert.doesNotMatch(html, /NaN|Infinity/); assert.match(html, /足够的K线/);
  assert.match(html, /尚未提供/);
});
