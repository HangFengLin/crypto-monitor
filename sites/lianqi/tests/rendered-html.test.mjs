import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";
import { createRequire } from "node:module";

test("an unconfigured proxy never silently connects to a different backend", async () => {
  const { default: worker } = await import("../dist/server/index.js");
  const original = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => { calls++; return Response.json({ ok: true }); };
  try {
    const response = await worker.fetch(new Request("https://lianqi.example/api/state"), {}, {});
    assert.equal(response.status, 502);
    assert.equal(calls, 0);
  } finally { globalThis.fetch = original; }
});

test("configured proxy forwards saves and report paths to the selected backend", async () => {
  const { default: worker } = await import("../dist/server/index.js");
  const original = globalThis.fetch;
  const captured = [];
  globalThis.fetch = async request => {
    captured.push({ url: request.url, method: request.method, body: await request.text(), cookie: request.headers.get("cookie") });
    return Response.json({ ok: true });
  };
  try {
    const env = { LIANQI_ORIGIN_URL: "http://127.0.0.1:8080" };
    const body = JSON.stringify({ watchlist: [{ symbol: "BTCUSDT" }] });
    await worker.fetch(new Request("https://lianqi.example/api/watchlist", { method: "POST", body, headers: { cookie: "private=session" } }), env, {});
    await worker.fetch(new Request("https://lianqi.example/reports/example/report.html?view=full"), env, {});
    assert.deepEqual(captured, [
      { url: "http://127.0.0.1:8080/api/watchlist", method: "POST", body, cookie: null },
      { url: "http://127.0.0.1:8080/reports/example/report.html?view=full", method: "GET", body: "", cookie: null },
    ]);
  } finally { globalThis.fetch = original; }
});

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("https://lianqi.example/", { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders the 炼气 strategy terminal", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>炼气 · 实时策略监控<\/title>/);
  assert.match(html, /炼气/);
  assert.match(html, /实时策略监控/);
  assert.match(html, /总览/);
  for (const label of ["行情与信号", "模拟交易", "策略研究", "运行状态"]) assert.ok(html.includes(label));
  assert.equal((html.match(/class="nav-button(?: active)?"/g)||[]).length, 5);
  assert.match(html, /模拟跟踪/);
  assert.match(html, /最近信号与模拟退出/);
  assert.match(html, /信号模拟记录，不提交订单/);
  assert.doesNotMatch(html, /当前真实持仓|实盘胜率|OKX \/ Binance/);
  assert.match(html, /property="og:image" content="https?:\/\/[^"]+\/og\.png"/);
  assert.doesNotMatch(html, /codex-preview|Your site is taking shape|Building your site/);
});

test("removes all starter preview artifacts", async () => {
  const [page, layout, packageJson] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);

  assert.match(page, /LianqiDashboard/);
  assert.match(layout, /og\.png/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
  assert.doesNotMatch(page, /_sites-preview|SkeletonPreview/);
  await assert.rejects(access(new URL("../app/_sites-preview", import.meta.url)));
});

test("paper ledger includes preserved OKX Demo trade history", async () => {
  const paperLedger = await readFile(new URL("../app/paper-ledger.tsx", import.meta.url), "utf8");
  assert.match(paperLedger, /\/api\/okx-demo-ledger/);
  assert.match(paperLedger, /OKX Demo 历史账本/);
  assert.match(paperLedger, /估算盈亏/);
});

async function renderPaperComponent(name, props) {
  const sourceUrl = new URL("../app/paper-ledger.tsx", import.meta.url);
  const require = createRequire(sourceUrl);
  const ts = require("typescript");
  const source = await readFile(sourceUrl, "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
                       jsx: ts.JsxEmit.ReactJSX },
  });
  const loaded = { exports: {} };
  new Function("require", "module", "exports", compiled.outputText)(require, loaded, loaded.exports);
  assert.equal(typeof loaded.exports[name], "function", "the rendered ledger component must be available");
  const React = require("react");
  const { renderToStaticMarkup } = require("react-dom/server");
  return renderToStaticMarkup(React.createElement(loaded.exports[name], props));
}

// Run the real component's request effect without mounting a browser or starting
// its polling timer. Only the network and hook scheduler are controlled here.
async function renderPaperAfterRequests(responses) {
  const sourceUrl = new URL("../app/paper-ledger.tsx", import.meta.url);
  const require = createRequire(sourceUrl);
  const ts = require("typescript");
  const React = require("react");
  const { renderToStaticMarkup } = require("react-dom/server");
  const states = [], effects = [];
  let cursor = 0;
  const hooks = {
    ...React,
    useState(initial) {
      const index = cursor++;
      if (!(index in states)) states[index] = initial;
      return [states[index], value => { states[index] = typeof value === "function" ? value(states[index]) : value; }];
    },
    useEffect(effect) { effects.push(effect); },
    useMemo(calculate) { return calculate(); },
  };
  let requestPass = 0;
  const fixtureRequire = name => name === "react" ? hooks : name === "./api-client.mjs" ? {
    async fetchWithTimeout(url) {
      const response = responses[requestPass][url];
      if (response instanceof Error) throw response;
      if (!response) throw Error(`Missing request fixture: ${url}`);
      return response;
    },
  } : require(name);
  const compiled = ts.transpileModule(await readFile(sourceUrl, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  });
  const loaded = { exports: {} };
  new Function("require", "module", "exports", "setInterval", "clearInterval", compiled.outputText)(fixtureRequire, loaded, loaded.exports, () => 0, () => {});
  const render = () => {
    cursor = 0;
    return renderToStaticMarkup(React.createElement(loaded.exports.default));
  };
  render();
  const cleanups = [];
  for (requestPass = 0; requestPass < responses.length; requestPass++) {
    cleanups.push(effects[0]());
    await new Promise(resolve => setImmediate(resolve));
  }
  const html = render();
  for (const cleanup of cleanups) cleanup();
  return html;
}

test("paper ledger missing monetary and return fields remain unknown", async () => {
  const trade = { id: "fixture", symbol: "BTCUSDT", interval: "15m", direction: "long",
    status: "closed", entry_price: 100, exit_price: null, return_pct: null,
    opened_at: 1, exit_reason: "", notional_usdt: null, pnl_usdt: null };
  const html = await renderPaperComponent("TradeRow", { trade, source: "okx" });
  assert.match(html, /名义本金 --/);
  assert.match(html, /开仓 100 → --/);
  assert.match(html, /<strong class="neutral">--<\/strong>/);
  assert.doesNotMatch(html, /0\.00%|\$0\.00/);
  const zeroHtml = await renderPaperComponent("TradeRow", { trade: {
    ...trade, exit_price: 100, return_pct: 0, notional_usdt: 0, pnl_usdt: 0,
  }, source: "okx" });
  assert.match(zeroHtml, /0\.00%/);
  assert.match(zeroHtml, /\$0\.00/);
});

test("initial OKX history request failure is visible without a successful ledger", async () => {
  const html = await renderPaperAfterRequests([{
    "/api/paper-trades": { ok: true, async json() { return { trades: [] }; } },
    "/api/okx-demo-ledger": { ok: false },
  }]);
  assert.match(html, /role="alert"[^>]*>[^<]*读取 OKX Demo 历史账本失败/);
  assert.match(html, /本地纸面信号账本/);
});

test("OKX refresh failure preserves previously loaded history and displays its error", async () => {
  const trade = { id: "retained", symbol: "ETHUSDT", interval: "15m", direction: "long",
    status: "closed", entry_price: 100, exit_price: 101, return_pct: .01,
    opened_at: 1, exit_reason: "target", notional_usdt: 50, pnl_usdt: .5 };
  const paper = { ok: true, async json() { return { trades: [] }; } };
  const html = await renderPaperAfterRequests([{
    "/api/paper-trades": paper,
    "/api/okx-demo-ledger": { ok: true, async json() { return { source_exists: true, trades: [trade],
      stats: { total_trades: 1, closed_trades: 1, win_rate: 1, realized_pnl_usdt: .5 } }; } },
  }, {
    "/api/paper-trades": paper,
    "/api/okx-demo-ledger": new Error("fixture history unavailable"),
  }]);
  assert.match(html, /ETHUSDT/);
  assert.match(html, /fixture history unavailable/);
  assert.match(html, /1\.00%/);
});

test("paper accounting labels missing historical models as legacy and new records as linear", async () => {
  const trade = { id: "fixture", symbol: "BTCUSDT", interval: "15m", direction: "long",
                  status: "closed", entry_price: 100, exit_price: 105, return_pct: .048,
                  opened_at: 1, exit_reason: "protection_reached" };
  const oldHtml = await renderPaperComponent("TradeRow", { trade, source: "paper" });
  assert.match(oldHtml, /收益口径：历史比例（旧版）/);
  const newHtml = await renderPaperComponent("TradeRow", {
    trade: { ...trade, return_model: "linear_usdm_v1" }, source: "paper",
  });
  assert.match(newHtml, /收益口径：线性 USDT 合约/);
  assert.doesNotMatch(newHtml, /收益口径：历史比例/);
});

test("paper accounting renders mixed model warning and separate realized samples", async () => {
  const stats = { total_trades: 12, open_trades: 0, closed_trades: 12, wins: 6, losses: 6,
                  breakevens: 0, win_rate: .5, expectancy: .009, total_return: .108,
                  mixed_return_models: true, comparability_note: "fixture：请按收益版本分别比较。",
                  return_model_stats: { legacy_ratio_v1: { closed_trades: 8, expectancy: -.01 },
                                        linear_usdm_v1: { closed_trades: 4, expectancy: .02 } } };
  const html = await renderPaperComponent("StatBlock", { stats });
  assert.match(html, /fixture：请按收益版本分别比较。/);
  assert.match(html, /混合口径逐笔合计/);
  assert.match(html, /历史比例（旧版）/);
  assert.match(html, /线性 USDT 合约/);
  assert.match(html, /闭仓数/);
  assert.match(html, /单笔期望/);
  assert.match(html, />8</);
  assert.match(html, />4</);
  assert.match(html, /-1\.00%/);
  assert.match(html, /2\.00%/);
  const okxHtml = await renderPaperComponent("StatBlock", { stats, okx: true });
  assert.doesNotMatch(okxHtml, /按收益版本|混合口径逐笔合计|单笔期望/);
});
