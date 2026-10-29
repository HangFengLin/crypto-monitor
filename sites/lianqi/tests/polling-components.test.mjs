import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import test from 'node:test';

const settle = () => new Promise(resolve => setImmediate(resolve));

async function mounted(component, slowPath) {
  const url = new URL(`../app/${component}.tsx`, import.meta.url), require = createRequire(url);
  const ts = require('typescript'), React = require('react'), loops = require('../app/refresh-loop.mjs');
  const effects = [], states = [], calls = [], timers = new Map();
  let cursor = 0, serial = 0, now = 0, resolveBody;
  const body = new Promise(resolve => { resolveBody = resolve; });
  const clock = {
    setTimeout(fn, ms = 0) { const id = ++serial; timers.set(id, { fn, at: now + ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(fn, ms) { const id = ++serial; timers.set(id, { fn, at: now + ms, interval: ms }); return id; },
    clearInterval(id) { timers.delete(id); },
    async advance(ms) {
      const target = now + ms;
      while (true) {
        const entry = [...timers].filter(([, value]) => value.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
        if (!entry) break;
        now = entry[1].at; timers.delete(entry[0]);
        if (entry[1].interval) timers.set(entry[0], { ...entry[1], at: now + entry[1].interval });
        entry[1].fn(); await settle();
      }
      now = target; await settle();
    },
  };
  const hooks = { ...React, useState(initial) {
    const index = cursor++;
    if (!(index in states)) states[index] = initial;
    return [states[index], value => { states[index] = typeof value === 'function' ? value(states[index]) : value; }];
  }, useEffect(effect) { effects.push(effect); }, useMemo(fn) { return fn(); } };
  const fixtureRequire = name => name === 'react' ? hooks : name === './refresh-loop.mjs' ? {
    ...loops, createRefreshLoop(load, options) { return loops.createRefreshLoop(load, { ...options, timers: clock }); },
  } : name === './api-client.mjs' ? {
    async fetchWithTimeout(path, options = {}) {
      calls.push({ path, signal: options.signal });
      if (path.split('?')[0] === slowPath) await body;
      return { ok: true, async json() {
        return path.split('?')[0] === '/api/settings' ? { values: { paper: { enabled: true } } } :
          path.split('?')[0] === '/api/notifications/status' ? { automatic: { enabled: true, running: true } } : { trades: [], stats: {} };
      } };
    },
  } : require(name);
  const compiled = ts.transpileModule(await readFile(url, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } });
  const loaded = { exports: {} };
  new Function('require', 'module', 'exports', 'setInterval', 'clearInterval', compiled.outputText)(
    fixtureRequire, loaded, loaded.exports, clock.setInterval, clock.clearInterval);
  cursor = 0; loaded.exports.default();
  const cleanups = effects.map(effect => effect());
  await settle();
  return { calls, advance: clock.advance, resolve() { resolveBody(); }, close() { cleanups.forEach(fn => fn?.()); } };
}

for (const [component, path, intervalMs] of [
  ['paper-ledger', '/api/paper-trades', 15000],
  ['feishu-notifications', '/api/notifications/status', 10000],
  ['runtime-settings', '/api/settings', 30000],
]) {
  test(`${component} does not overlap a slow poll and aborts it on unmount`, async () => {
    const panel = await mounted(component, path);
    try {
      await panel.advance(intervalMs * 3);
      const requests = panel.calls.filter(call => call.path.split('?')[0] === path);
      assert.equal(requests.length, 1);
      if (component === 'paper-ledger') {
        assert.equal(requests[0].path, '/api/paper-trades?detail=compact');
        assert.equal(panel.calls.filter(call => call.path === '/api/okx-demo-ledger').length, 1,
          'legacy ledger should not wait for the slow paper ledger');
      }
      panel.close();
      assert.equal(requests[0].signal?.aborted, true);
      panel.resolve(); await settle();
      await panel.advance(intervalMs * 3);
      assert.equal(panel.calls.filter(call => call.path.split('?')[0] === path).length, 1);
    } finally { panel.close(); panel.resolve(); }
  });
}
