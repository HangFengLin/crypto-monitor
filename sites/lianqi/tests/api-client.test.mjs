import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
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

async function withBodyServer(run) {
  const server = createServer((request, response) => {
    response.writeHead(request.url === '/rejected' ? 503 : 200, { 'Content-Type': 'application/json', 'X-Fixture': 'body' });
    response.flushHeaders();
    if (request.url === '/stalled') response.write('{');
    else response.end(request.url === '/rejected' ? '{"detail":"unavailable"}' : '{"value":0}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try { await run(`http://127.0.0.1:${server.address().port}`); }
  finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
}

test('the deadline still expires after headers arrive while the body is stalled', async () => {
  await withBodyServer(async base => {
    await assert.rejects(fetchWithTimeout(`${base}/stalled`, {}, 50), /超时/);
  });
});

test('buffering keeps the native response metadata and error body readable', async () => {
  await withBodyServer(async base => {
    const response = await fetchWithTimeout(`${base}/rejected`);
    assert.equal(response.status, 503);
    assert.equal(response.ok, false);
    assert.equal(response.url, `${base}/rejected`);
    assert.equal(response.headers.get('X-Fixture'), 'body');
    assert.equal(response.bodyUsed, false);
    assert.deepEqual(await response.json(), { detail: 'unavailable' });
    const successful = await fetchWithTimeout(`${base}/complete`);
    assert.deepEqual(await successful.json(), { value: 0 });
  });
});

test('caller cancellation interrupts an unfinished response body', async () => {
  await withBodyServer(async base => {
    const controller = new AbortController();
    const request = fetchWithTimeout(`${base}/stalled`, { signal: controller.signal }, 5000);
    setTimeout(() => controller.abort(), 20);
    await assert.rejects(request, { name: 'AbortError' });
  });
});
