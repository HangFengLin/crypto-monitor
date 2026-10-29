import assert from 'node:assert/strict';
import test from 'node:test';
import { startLiveStateUpdates } from '../app/refresh-loop.mjs';

const settle = () => new Promise(resolve => setImmediate(resolve));
const snapshot = state => JSON.stringify({protocol:'lianqi-delta-v1', type:'snapshot', seq:0, state});
const patch = (seq, set, remove = []) => JSON.stringify({protocol:'lianqi-delta-v1', type:'patch', seq, set, remove});

function fixture() {
  const streams = [], snapshots = [], errors = [], statuses = [];
  const tasks = [];
  const timers = {setTimeout(fn) { tasks.push(fn); return tasks.length; }, clearTimeout() {}};
  const feed = startLiveStateUpdates(async () => {}, {
    timers, createStream(url) { const stream = {url, readyState:1, close() {this.readyState=2;}}; streams.push(stream); return stream; },
    onSnapshot(value) { snapshots.push(value); }, onError(error) { errors.push(error); },
    onConnection(value) { statuses.push(value); },
  });
  return {feed, streams, snapshots, errors, statuses, tasks};
}

test('delta stream replaces changed fields and retains null, zero and deletions', async () => {
  const f = fixture();
  try {
    await settle(); const s = f.streams[0]; s.onopen();
    assert.equal(s.url, '/api/events?detail=compact&delta=1');
    s.onmessage({data:snapshot({prices:{BTC:1}, untouched:'keep', gone:1, list:[1]})});
    s.onmessage({data:patch(1, {prices:{BTC:0}, list:[], nullable:null}, ['gone'])});
    assert.deepEqual(f.snapshots.at(-1), {prices:{BTC:0}, untouched:'keep', list:[], nullable:null});
  } finally { f.feed.stop(); }
});

test('reconnect requires a new snapshot and ignores frames from replaced streams', async () => {
  const f = fixture();
  try {
    await settle(); const first = f.streams[0]; first.onopen();
    first.onmessage({data:snapshot({old:1})});
    first.readyState=2; first.onerror(); f.tasks.at(-1)();
    const next=f.streams[1]; next.onopen();
    first.onmessage({data:patch(1, {old:2})});
    next.onmessage({data:snapshot({fresh:0})});
    next.onmessage({data:patch(1, {fresh:null})});
    assert.deepEqual(f.snapshots, [{old:1}, {fresh:0}, {fresh:null}]);
  } finally { f.feed.stop(); }
});

test('missing or out-of-order patches force a reconnect before more state is applied', async () => {
  const f=fixture();
  try {
    await settle(); const first=f.streams[0]; first.onopen();
    first.onmessage({data:snapshot({value:1})});
    first.onmessage({data:patch(2, {value:3})});
    assert.equal(first.readyState, 2);
    assert.equal(f.statuses.at(-1), 'offline');
    assert.equal(f.errors.length, 1);
    first.onmessage({data:patch(1, {value:2})});
    assert.deepEqual(f.snapshots, [{value:1}]);
  } finally { f.feed.stop(); }
});

test('legacy snapshots from older servers remain supported', async () => {
  const f=fixture();
  try { await settle(); const s=f.streams[0]; s.onopen(); s.onmessage({data:'{"prices":{"BTC":0}}'});
    assert.deepEqual(f.snapshots, [{prices:{BTC:0}}]);
  } finally { f.feed.stop(); }
});

test('an in-flight fallback response cannot overwrite a newer stream snapshot', async () => {
  let signal, resolve;
  const wait = new Promise(done => { resolve = done; });
  const streams = [], delivered = [];
  const feed = startLiveStateUpdates(async next => {
    signal = next;
    await wait;
    if (!signal.aborted) delivered.push('old-poll');
  }, {
    createStream() { const s = {close() {}}; streams.push(s); return s; },
    onSnapshot() { delivered.push('stream'); }, onConnection() {},
  });
  try {
    await settle();
    streams[0].onopen();
    streams[0].onmessage({data:snapshot({updated_at:1})});
    assert.equal(signal.aborted, true);
    resolve(); await settle();
    assert.deepEqual(delivered, ['stream']);
  } finally { resolve(); feed.stop(); }
});
