import test from 'node:test';
import assert from 'node:assert/strict';
import { validateState, fetchWithTimeout } from '../app/api-client.mjs';

test('rejects an old backend instead of showing zero paper positions', () => {
  assert.throws(() => validateState({ watchlist: [], okx_bot_status: {} }), /后端版本/);
  const state = { watchlist: [], signal_tracking: { mode: 'paper', positions: [] } };
  assert.equal(validateState(state), state);
});

test('a stalled service produces a bounded actionable error', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = (_, options) => new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(options.signal.reason));
  });
  try { await assert.rejects(fetchWithTimeout('/api/state', {}, 10), /超时/); }
  finally { globalThis.fetch = original; }
});
