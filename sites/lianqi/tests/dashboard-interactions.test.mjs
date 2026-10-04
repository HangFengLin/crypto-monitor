import assert from 'node:assert/strict';
import test from 'node:test';

const interactions = await import('../app/dashboard-interactions.mjs').catch(() => ({}));
const item = symbol => ({ symbol, interval: '15m', signal: true, indicator_alert: true, support_alert: true });
const rows = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT'].map(item);
const state = {
  prices: { BTCUSDT: { priceChangePercent: 2, quoteVolume: 100 }, ETHUSDT: { priceChangePercent: -3, quoteVolume: 300 }, SOLUSDT: { priceChangePercent: 0, quoteVolume: 200 } },
  signals: { 'ETHUSDT:15m': { signal: 'short' }, 'BTCUSDT:15m': { signal: 'wait' } },
};

test('search and filters combine without treating unavailable prices as rising', () => {
  assert.equal(typeof interactions.selectMarketRows, 'function');
  assert.deepEqual(interactions.selectMarketRows(rows, state, ' eth ', 'signal', 'default').map(row => row.symbol), ['ETHUSDT']);
  assert.deepEqual(interactions.selectMarketRows(rows, state, '', 'up', 'default').map(row => row.symbol), ['BTCUSDT', 'SOLUSDT']);
  assert.deepEqual(interactions.selectMarketRows(rows, state, 'DOGE', 'all', 'default'), []);
});

test('sorting keeps missing values last and does not mutate the watchlist', () => {
  assert.equal(typeof interactions.selectMarketRows, 'function');
  assert.deepEqual(interactions.selectMarketRows(rows, state, '', 'all', 'change-desc').map(row => row.symbol), ['BTCUSDT', 'SOLUSDT', 'ETHUSDT', 'XRPUSDT']);
  assert.deepEqual(interactions.selectMarketRows(rows, state, '', 'all', 'change-asc').map(row => row.symbol), ['ETHUSDT', 'SOLUSDT', 'BTCUSDT', 'XRPUSDT']);
  assert.deepEqual(interactions.selectMarketRows(rows, state, '', 'all', 'volume-desc').map(row => row.symbol), ['ETHUSDT', 'SOLUSDT', 'BTCUSDT', 'XRPUSDT']);
  assert.deepEqual(rows.map(row => row.symbol), ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT']);
});

test('missing and malformed prices remain unknown', () => {
  assert.equal(typeof interactions.finiteMetric, 'function');
  for (const value of [undefined, null, '', 'bad', Infinity]) assert.equal(interactions.finiteMetric(value), null);
  assert.equal(interactions.finiteMetric('0'), 0);
});

test('watchlist validation preserves settings and normalizes symbols', () => {
  assert.equal(typeof interactions.prepareWatchlist, 'function');
  const original = { ...item(' ethusdt '), signal: false };
  assert.deepEqual(interactions.prepareWatchlist([original]), [{ ...original, symbol: 'ETHUSDT', email: '' }]);
  assert.equal(original.symbol, ' ethusdt ');
  assert.deepEqual(interactions.prepareWatchlist([]), []);
});

test('blank or duplicate draft rows cannot disappear silently on save', () => {
  assert.equal(typeof interactions.prepareWatchlist, 'function');
  assert.throws(() => interactions.prepareWatchlist([item('')]), /第 1 行.*交易对/);
  assert.throws(() => interactions.prepareWatchlist([item('BTCUSDT'), item(' btcusdt ')]), /重复.*BTCUSDT/);
  assert.throws(() => interactions.prepareWatchlist([item('BTC/USDT')]), /格式/);
});

test('view hashes restore known views and fall back safely for unrelated anchors', () => {
  assert.equal(typeof interactions.viewFromHash, 'function');
  for (const view of ['overview', 'markets', 'paper', 'research', 'settings']) assert.equal(interactions.viewFromHash(`#${view}`), view);
  for (const hash of ['', '#unknown', '#%invalid', '#main-content']) assert.equal(interactions.viewFromHash(hash), 'overview');
});

test('a successful save cannot be rolled back by an earlier fetch or a delayed stream', () => {
  assert.equal(typeof interactions.createWatchlistSync, 'function');
  const sync = interactions.createWatchlistSync();
  const earlier = sync.generation();
  sync.beginSave();
  const during = sync.generation();
  assert.equal(sync.accept(rows, sync.generation()), false);
  sync.finishSave([item('DOGEUSDT')]);
  assert.equal(sync.accept(rows, earlier), false);
  assert.equal(sync.accept(rows, during), false);
  assert.equal(sync.accept(rows), false);
  assert.equal(sync.accept([item('DOGEUSDT')], sync.generation()), true);
  assert.equal(sync.accept(rows), false);
  assert.equal(sync.accept([item('DOGEUSDT')]), true);
  assert.equal(sync.accept([item('ADAUSDT')]), true);
});

test('save failure resumes new snapshots but still rejects a fetch from before the save', () => {
  assert.equal(typeof interactions.createWatchlistSync, 'function');
  const sync = interactions.createWatchlistSync();
  const earlier = sync.generation();
  sync.beginSave();
  const during = sync.generation();
  sync.failSave();
  assert.equal(sync.accept(rows, earlier), false);
  assert.equal(sync.accept(rows, during), false);
  assert.equal(sync.accept(rows, sync.generation()), true);
});
