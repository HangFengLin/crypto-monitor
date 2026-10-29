import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

async function renderLegacyStats(stats) {
  const elements = new Map();
  const document = { querySelector(selector) {
    if (!elements.has(selector)) elements.set(selector, { innerHTML: "", textContent: "", addEventListener() {} });
    return elements.get(selector);
  } };
  const context = vm.createContext({ document, window: { location: { protocol: "http:" }, addEventListener() {} },
    performance: { now: () => 0 }, fetch: () => new Promise(() => {}) });
  vm.runInContext(await readFile(new URL("../../../public/app.js", import.meta.url), "utf8"), context);
  context.inputStats = stats;
  vm.runInContext("renderStrategyStats(inputStats, [])", context);
  return elements.get("#strategySummary").innerHTML;
}

test("legacy VPS page suppresses mixed totals even from older numeric backend payloads", async () => {
  const html = await renderLegacyStats({ total_trades: 3, closed_trades: 3, open_trades: 0,
    mixed_return_models: true, win_rate: .8765, expectancy: .1234, wins: 2, losses: 1,
    return_model_stats: { legacy_ratio_v1: { closed_trades: 1, expectancy: -.01 },
      linear_usdm_v1: { closed_trades: 2, expectancy: .02 } } });
  assert.doesNotMatch(html, /87\.65%|12\.34%/);
  assert.match(html, /历史比例（旧版）/);
  assert.match(html, /线性 USDT 合约/);
  assert.match(html, /-1\.00%/);
  assert.match(html, /2\.00%/);
});
