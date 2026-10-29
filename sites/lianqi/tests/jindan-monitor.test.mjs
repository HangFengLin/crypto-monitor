import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import test from 'node:test';

async function render(props) {
  const url = new URL('../app/jindan-monitor.tsx', import.meta.url), require = createRequire(url);
  const ts = require('typescript'), React = require('react'), { renderToStaticMarkup } = require('react-dom/server');
  const source = await readFile(url, 'utf8').catch(() => '');
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } });
  const loaded = { exports: {} };
  const localRequire = name => name === './api-client.mjs' ? {} : name === './refresh-loop.mjs' ? {} : require(name);
  new Function('require', 'module', 'exports', compiled.outputText)(localRequire, loaded, loaded.exports);
  assert.equal(typeof loaded.exports.JindanStatusPanel, 'function');
  return renderToStaticMarkup(React.createElement(loaded.exports.JindanStatusPanel, props));
}

test('missing snapshots show unknown evidence without suggesting zero observed events', async () => {
  const html = await render({ status: { availability: 'missing', counts: null, per_symbol: null } });
  assert.match(html, /尚无采集快照/);
  assert.match(html, /暂无可靠数据/);
  assert.match(html, /96/);
  assert.match(html, /180/);
  assert.match(html, /仅观察，不下单/);
  assert.match(html, /2026年1—8月/);
  assert.doesNotMatch(html, /采集正常/);
});

test('stale status remains visible as old evidence and explicit zeros are rendered', async () => {
  const html = await render({ status: { availability: 'stale', updated_at: 1700000000, age_seconds: 210,
    phase: 'FEATURE_INITIALIZATION', operational_status: 'RUNNING', counts: { price_events: 0, flow_pass: null },
    per_symbol: { BTCUSDT: { closed_bars: 0, last_receive_ms: null } },
    errors: [{ code: 'SOURCE_GAP', count: 1 }], events: [] } });
  assert.match(html, /快照已过期/);
  assert.match(html, /特征与状态预热/);
  assert.match(html, /BTCUSDT/);
  assert.match(html, /SOURCE_GAP/);
  assert.match(html, /<strong>0<\/strong>/);
  assert.doesNotMatch(html, /采集正常/);
});

test('unknown FLOW and unsafe boundaries are visible without live financial metrics', async () => {
  const html = await render({ status: { availability: 'available', phase: 'COMMISSIONING',
    boundaries: { orders_enabled: true, final_period_access: false }, counts: {},
    events: [{ event_id: 'abc', symbol: 'ETHUSDT', measurement_minutes: 15, flow_status: 'UNKNOWN',
      on_time_status: 'LATE_DEPENDENCY', confirmed_time_ms: 1700000000000, side: -1 }] } });
  assert.match(html, /边界异常/);
  assert.match(html, /UNKNOWN/);
  assert.match(html, /LATE_DEPENDENCY/);
  assert.match(html, /ETHUSDT/);
  assert.doesNotMatch(html, /实盘收益|实时盈亏|累计盈利/);
});

test('historical research is separate, absent results are not presented as completed', async () => {
  const missing = await render({ status: { availability: 'missing' }, research: { availability: 'missing' } });
  assert.match(missing, /尚未发布历史试验结果/);
  assert.doesNotMatch(missing, /EXPLORATORY_COMPLETE/);
  const complete = await render({ status: { availability: 'missing' }, research: { availability: 'available',
    status: 'EXPLORATORY_COMPLETE', main_metrics: [{ policy_id: 'FLOW_BASE', BASE: { closed_trades: 6,
      total_net_return: -0.01, mean_net_r: null }, DOUBLE: { total_net_return: -0.02 } }] } });
  assert.match(complete, /历史参数试验/);
  assert.match(complete, /2024—2025/);
  assert.match(complete, /FLOW_BASE/);
  assert.match(complete, /-1\.00%/);
  assert.match(complete, /不代表策略通过验收/);
});


test('capture availability never implies completed labels or automatic scoring', async () => {
  const html = await render({ status: { availability: 'available', phase: 'AWAITING_SCORE_PREFLIGHT',
    capabilities: { raw_closed_1m: true, e2_labels: false, formal_scoring: false },
    readiness_gaps: ['E2_LABELS_NOT_IMPLEMENTED', 'SCORE_PREFLIGHT_REQUIRED'] } });
  assert.match(html, /后续标签和评分待独立验收/);
  assert.match(html, /不会自动评分/);
  assert.match(html, /E2_LABELS_NOT_IMPLEMENTED/);
  assert.doesNotMatch(html, /评分引擎已完成|全功能已上线/);
});

test('poll failures identify previous evidence as unavailable even when last snapshot was fresh', async () => {
  const html = await render({ status: { availability: 'available', phase: 'COMMISSIONING' }, error: '采集接口暂不可用' });
  assert.match(html, /采集快照暂不可读/);
  assert.doesNotMatch(html, />快照可用</);
});


test('research table states the two calendars and exposes the actual tested parameters', async () => {
  const html = await render({ status: { availability: 'missing' }, research: { availability: 'available',
    calendar_days: 700, reproduction: { status: 'PASS_EXACT', calendar_days: 731 },
    bounds: { start_utc: '2024-02-01T00:00:00+00:00', end_exclusive_utc: '2026-01-01T00:00:00+00:00' },
    main_metrics: [{ policy_id: 'OAT_FLOW', arm: 'FLOW', flow_threshold: 0.05, hold_minutes: 240,
      BASE: { closed_trades: 47 }, DOUBLE: {} }] } });
  assert.match(html, /700日/); assert.match(html, /731日/);
  assert.match(html, /FLOW阈值/); assert.match(html, /最长持有/); assert.match(html, /0\.05/);
});

test('descending collector events remain newest first and collector gaps are explicit', async () => {
  const html = await render({ status: { availability: 'available', unresolved_gap_count: 2,
    clock: { offset_uncertainty_ms: null }, storage: { used_bytes: 1024, limit_bytes: 4096, ok: true },
    events: [{ event_id: 'new', symbol: 'SOLUSDT', confirmed_time_ms: 1700000060000 },
             { event_id: 'old', symbol: 'ETHUSDT', confirmed_time_ms: 1700000000000 }] } });
  const table = html.split('近期PRICE / FLOW事件')[1];
  assert.ok(table.indexOf('SOLUSDT') < table.indexOf('ETHUSDT'));
  assert.match(html, /未解决缺口/); assert.match(html, /时钟误差范围/); assert.match(html, /存储配额/);
});
