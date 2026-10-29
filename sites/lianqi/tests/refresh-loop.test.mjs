import assert from 'node:assert/strict';
import test from 'node:test';

const loops = await import('../app/refresh-loop.mjs').catch(() => ({}));
const settle = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

function clock() {
  let now = 0, serial = 0;
  const pending = new Map();
  return {
    setTimeout(fn, delay) { const id = ++serial; pending.set(id, { fn, at: now + delay }); return id; },
    clearTimeout(id) { pending.delete(id); },
    async advance(delta) {
      const target = now + delta;
      while (true) {
        const entry = [...pending].filter(([, item]) => item.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
        if (!entry) break;
        now = entry[1].at; pending.delete(entry[0]); entry[1].fn(); await settle();
      }
      now = target;
      await settle();
    },
  };
}

test('slow refreshes coalesce and the next poll waits until completion', async () => {
  assert.equal(typeof loops.createRefreshLoop, 'function', 'non-overlapping refresh controller is required');
  const timers = clock(), pending = deferred();
  let calls = 0;
  const loop = loops.createRefreshLoop(async () => { calls++; if (calls === 1) await pending.promise; }, { intervalMs: 15000, timers });
  const first = loop.refresh();
  assert.equal(loop.refresh(), first);
  await timers.advance(60000);
  assert.equal(calls, 1);
  pending.resolve(); await first;
  await timers.advance(14999);
  assert.equal(calls, 1);
  await timers.advance(1);
  assert.equal(calls, 2);
  loop.stop();
});

test('failed refresh releases the gate and later polling can recover', async () => {
  assert.equal(typeof loops.createRefreshLoop, 'function');
  const timers = clock();
  let calls = 0;
  const loop = loops.createRefreshLoop(async () => { calls++; if (calls === 1) throw Error('fixture failure'); }, { intervalMs: 10000, timers });
  await assert.rejects(loop.refresh(), /fixture failure/);
  await timers.advance(10000);
  assert.equal(calls, 2);
  loop.stop();
});

test('unmount cancellation aborts a pending load and stops future refreshes', async () => {
  assert.equal(typeof loops.createRefreshLoop, 'function');
  const timers = clock(), pending = deferred();
  let signal, calls = 0;
  const loop = loops.createRefreshLoop(async next => { calls++; signal = next; await pending.promise; }, { intervalMs: 15000, timers });
  const first = loop.refresh(); await settle();
  loop.stop();
  assert.equal(signal.aborted, true);
  pending.resolve(); await first;
  await timers.advance(60000);
  await loop.refresh();
  assert.equal(calls, 1);
});

test('healthy SSE suppresses polling, disconnect resumes it and reconnect pauses it', async () => {
  assert.equal(typeof loops.startLiveStateUpdates, 'function', 'SSE-aware refresh controller is required');
  const timers = clock(), statuses = [], snapshots = [];
  let calls = 0, requestedUrl, closed = false;
  const stream = { close() { closed = true; } };
  const feed = loops.startLiveStateUpdates(async () => { calls++; }, {
    timers, createStream(url) { requestedUrl = url; return stream; },
    onSnapshot(value) { snapshots.push(value); }, onConnection(value) { statuses.push(value); },
  });
  await settle();
  assert.equal(calls, 1);
  assert.equal(requestedUrl, '/api/events?detail=compact&delta=1');
  stream.onopen();
  await timers.advance(60000);
  assert.equal(calls, 1);
  stream.onmessage({ data: '{"prices":{"BTCUSDT":0}}' });
  assert.deepEqual(snapshots, [{ prices: { BTCUSDT: 0 } }]);
  stream.onerror(); await settle();
  assert.equal(calls, 2);
  await timers.advance(30000);
  assert.equal(calls, 3);
  stream.onopen();
  await timers.advance(60000);
  assert.equal(calls, 3);
  await feed.refresh();
  assert.equal(calls, 4, 'manual refresh must still work while SSE is online');
  feed.stop();
  assert.equal(closed, true);
  assert.ok(statuses.includes('online'));
  assert.ok(statuses.includes('offline'));
  stream.onerror();
  await timers.advance(60000);
  assert.equal(calls, 4);
});

test('an unavailable SSE implementation leaves bounded fallback polling active', async () => {
  assert.equal(typeof loops.startLiveStateUpdates, 'function');
  const timers = clock(); let calls = 0;
  const feed = loops.startLiveStateUpdates(async () => { calls++; }, {
    timers, createStream() { throw Error('SSE unavailable'); }, onSnapshot() {}, onConnection() {},
  });
  await settle(); await timers.advance(30000);
  assert.equal(calls, 2);
  feed.stop();
});

function eventStream() {
  return { readyState: 0, closes: 0, close() { this.closes++; this.readyState = 2; } };
}

test('a permanently CLOSED EventSource is rebuilt once after a bounded delay', async () => {
  // Real browsers can stop native retries after an HTTP 503. Polling alone
  // cannot reopen this CLOSED object when the server becomes available again.
  const timers = clock(), streams = [], statuses = [], snapshots = [];
  let loads = 0;
  const feed = loops.startLiveStateUpdates(async () => { loads++; }, {
    timers, createStream() { const stream = eventStream(); streams.push(stream); return stream; },
    onSnapshot(value) { snapshots.push(value); }, onConnection(value) { statuses.push(value); },
  });
  try {
    await settle();
    const first = streams[0];
    first.readyState = 1; first.onopen();
    first.readyState = 2; first.onerror(); first.onerror();
    await timers.advance(4999);
    assert.equal(streams.length, 1);
    await timers.advance(1);
    assert.equal(streams.length, 2, 'CLOSED streams require explicit reconstruction');
    const second = streams[1];
    second.readyState = 1; second.onopen();
    assert.equal(statuses.at(-1), 'online');
    const currentLoads = loads, currentStatuses = statuses.length;
    first.onmessage({ data: '{"stale":true}' });
    first.onopen(); first.onerror();
    assert.deepEqual(snapshots, []);
    assert.equal(statuses.length, currentStatuses, 'events from replaced streams must have no effect');
    await timers.advance(60000);
    assert.equal(streams.length, 2);
    assert.equal(loads, currentLoads, 'the replacement connection must pause fallback polling');
    second.onmessage({ data: '{"current":true}' });
    assert.deepEqual(snapshots, [{ current: true }]);
  } finally { feed.stop(); }
});

test('CONNECTING EventSource errors retain native retries without parallel streams', async () => {
  const timers = clock(), streams = [];
  const feed = loops.startLiveStateUpdates(async () => {}, {
    timers, createStream() { const stream = eventStream(); streams.push(stream); return stream; },
    onSnapshot() {}, onConnection() {},
  });
  try {
    streams[0].onerror();
    await timers.advance(60000);
    assert.equal(streams.length, 1, 'CONNECTING already has the browser native retry loop');
    streams[0].readyState = 1; streams[0].onopen();
    await timers.advance(60000);
    assert.equal(streams.length, 1);
  } finally { feed.stop(); }
});

test('stopping cancels the CLOSED-stream retry and ignores all late stream events', async () => {
  const timers = clock(), streams = [], statuses = [], snapshots = [];
  const feed = loops.startLiveStateUpdates(async () => {}, {
    timers, createStream() { const stream = eventStream(); streams.push(stream); return stream; },
    onSnapshot(value) { snapshots.push(value); }, onConnection(value) { statuses.push(value); },
  });
  const first = streams[0];
  first.readyState = 2; first.onerror();
  feed.stop();
  const statusCount = statuses.length;
  first.onopen(); first.onerror(); first.onmessage({ data: '{"late":true}' });
  await timers.advance(60000);
  assert.equal(streams.length, 1);
  assert.equal(first.closes, 1, 'a CLOSED stream should be retired exactly once');
  assert.equal(statuses.length, statusCount);
  assert.deepEqual(snapshots, []);
});

test('EventSource construction failures retry slowly and recover without a busy loop', async () => {
  const timers = clock(), stream = eventStream();
  let constructions = 0;
  const feed = loops.startLiveStateUpdates(async () => {}, {
    timers, createStream() { constructions++; if (constructions < 3) throw Error('temporary browser failure'); return stream; },
    onSnapshot() {}, onConnection() {},
  });
  try {
    assert.equal(constructions, 1);
    await timers.advance(4999);
    assert.equal(constructions, 1);
    await timers.advance(1);
    assert.equal(constructions, 2);
    await timers.advance(5000);
    assert.equal(constructions, 3);
    stream.readyState = 1; stream.onopen();
    await timers.advance(60000);
    assert.equal(constructions, 3);
  } finally { feed.stop(); }
});
