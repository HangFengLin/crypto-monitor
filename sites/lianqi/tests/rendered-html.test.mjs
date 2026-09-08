import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

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
