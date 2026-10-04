import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import test from "node:test";
import { compareResearchRuns, createLatestResearchRequest, researchConfidenceInterval, researchModelLabel, researchPercent, validationSummary } from "../app/research-evidence.mjs";

const metadata = {
  return_model: "linear_usdm_v1", holding_limit_convention: "entry_bar_is_first", max_observed_bars: 96,
  timestamp_boundary: "bar_open_label_not_execution_time", equity_boundary: "exit_label_compounding_without_open_position_mtm",
  funding_included: false, independent_slippage_included: false, execution_scope: "single_symbol_signal_diagnostic",
};
const baseline = {
  run_id: "baseline", symbol: "BTCUSDT", interval: "15m", market_data_source: "binance_usdm", config_revision: "fixture-v1",
  started_at: "2026-01-01T00:00:00Z", ended_at: "2026-02-01T00:00:00Z", execution_metadata: metadata,
  params: { return_model: "linear_usdm_v1", fee_rate: .001, reward_risk: 2, max_hold_bars: 96, stop_mode: "structure_atr" },
  metrics: { total_trades: 20, expectancy: .01, win_rate: .5, total_return: .12, max_drawdown: .06 },
};
const alternative = {
  ...baseline, run_id: "alternative", params: { ...baseline.params, stop_mode: "atr_trailing_after_1r" },
  metrics: { total_trades: 18, expectancy: .02, win_rate: .6, total_return: .15, max_drawdown: .04 },
};

// A missing cost/boundary comparison would admit these incompatible experiments.
test("a controlled stop-mode comparison reports observed differences without certifying a strategy", () => {
  const result = compareResearchRuns(baseline, alternative);
  assert.equal(result.comparable, true);
  assert.deepEqual(result.reasons, []);
  assert.ok(Math.abs(result.delta.expectancy - .01) < 1e-12);
  assert.ok(Math.abs(result.delta.max_drawdown + .02) < 1e-12);
  assert.match(result.conclusion, /诊断/);
  assert.doesNotMatch(result.conclusion, /验证通过|可实盘/);
});

test("different costs, source, time window, or holding convention block comparison with a useful reason", () => {
  const cases = [
    [{ ...alternative, params: { ...alternative.params, fee_rate: .002 } }, /手续费/],
    [{ ...alternative, market_data_source: "okx" }, /数据来源/],
    [{ ...alternative, started_at: "2026-01-02T00:00:00Z" }, /开始时间/],
    [{ ...alternative, execution_metadata: { ...metadata, holding_limit_convention: "legacy_inclusive_end" } }, /持仓计数/],
    [{ ...alternative, config_revision: "another-version" }, /配置版本/],
  ];
  for (const [candidate, expected] of cases) {
    const result = compareResearchRuns(baseline, candidate);
    assert.equal(result.comparable, false);
    assert.ok(result.reasons.some(reason => expected.test(reason)));
    assert.equal(result.delta, null);
  }
});

test("missing execution metadata prevents legacy records from appearing comparable", () => {
  const old = { ...alternative, params: { ...alternative.params, return_model: undefined }, execution_metadata: undefined };
  const result = compareResearchRuns(old, old);
  assert.equal(result.comparable, false);
  assert.ok(result.reasons.some(reason => /历史口径/.test(reason)));
  assert.ok(result.reasons.some(reason => /执行元数据/.test(reason)));
  assert.equal(researchModelLabel(old), "历史口径（旧版）");
});

test("absent execution records produce a concise explanation instead of eight repeated field gaps", () => {
  const result = compareResearchRuns({ ...baseline, execution_metadata: undefined }, { ...alternative, execution_metadata: undefined });
  assert.equal(result.comparable, false);
  assert.equal(result.delta, null);
  assert.ok(result.reasons.length <= 3);
  assert.ok(result.reasons.some(reason => /执行元数据/.test(reason)));
});

test("partial execution metadata and inconsistent accounting fields also fail closed", () => {
  const partial = { ...metadata };
  delete partial.funding_included;
  assert.ok(compareResearchRuns(baseline, { ...alternative, execution_metadata: partial }).reasons.some(reason => /资金费.*缺失/.test(reason)));
  assert.ok(compareResearchRuns(baseline, { ...alternative, params: { ...alternative.params, return_model: "legacy_ratio_v1" } }).reasons.some(reason => /口径.*不一致/.test(reason)));
});

test("missing uncertainty stays missing while a real zero remains visible", () => {
  assert.equal(researchPercent(null), "待补");
  assert.equal(researchPercent(0), "0.00%");
  assert.equal(researchConfidenceInterval(null, .01), "待补");
  assert.equal(researchConfidenceInterval(-.01, .02), "-1.00% ～ 2.00%");
});

test("zero observations cannot produce an attractive comparison or a false zero return", () => {
  const result = compareResearchRuns(baseline, { ...alternative, metrics: { total_trades: 0, total_return: 0 } });
  assert.equal(result.comparable, false);
  assert.ok(result.reasons.some(reason => /样本/.test(reason)));
  assert.equal(result.delta, null);
});

test("identical selections cannot qualify as a controlled comparison", () => {
  assert.equal(compareResearchRuns(baseline, baseline).comparable, false);
});
test("invalid negative cost values cannot qualify as a controlled comparison", () => {
  const negativeFee = { ...baseline, params: { ...baseline.params, fee_rate: -.001 } };
  assert.equal(compareResearchRuns(negativeFee, { ...alternative, params: { ...alternative.params, fee_rate: -.001 } }).comparable, false);
});
test("malformed execution metadata cannot qualify as a controlled comparison", () => {
  const malformedMetadata = { ...metadata, funding_included: "false" };
  assert.equal(compareResearchRuns({ ...baseline, execution_metadata: malformedMetadata }, { ...alternative, execution_metadata: malformedMetadata }).comparable, false);
});
test("invalid observation ceilings cannot qualify as complete execution metadata", () => {
  const bad = { ...metadata, max_observed_bars: Infinity };
  assert.equal(compareResearchRuns({ ...baseline, execution_metadata: bad }, { ...alternative, execution_metadata: bad }).comparable, false);
});
for (const key of ["holding_limit_convention", "timestamp_boundary", "equity_boundary", "execution_scope"]) {
  test(`unknown ${key} values cannot qualify as a documented execution convention`, () => {
    const unknown = { ...metadata, [key]: "undocumented_convention" };
    assert.equal(compareResearchRuns({ ...baseline, execution_metadata: unknown }, { ...alternative, execution_metadata: unknown }).comparable, false);
  });
}

test("positive baseline-retained results stay unpromoted", () => {
  const result = validationSummary({ status: "baseline_retained", kind: "formal_validation", candidates: [{ expectancy: .02 }] });
  assert.equal(result.passed, false);
  assert.match(result.label, /保留基线/);
});

test("a claimed candidate pass is rejected if its gates or date/accounting evidence are incomplete", () => {
  const complete = { status: "candidate_passed", kind: "formal_validation", started_at: baseline.started_at,
    ended_at: baseline.ended_at, interval: "15m", return_model: "linear_usdm_v1", gates: [{ label: "留出集", status: "pass" }] };
  assert.equal(validationSummary(complete).passed, true);
  assert.equal(validationSummary({ ...complete, gates: [] }).passed, false);
  assert.equal(validationSummary({ ...complete, gates: [{ label: "留出集", status: "missing" }] }).passed, false);
  assert.equal(validationSummary({ ...complete, ended_at: undefined }).passed, false);
  assert.equal(validationSummary({ ...complete, kind: "optimization_diagnostic" }).passed, false);
});

test("an old detail response is discarded after a newer selection or reset", () => {
  const requests = createLatestResearchRequest();
  const old = requests.begin("first");
  const newer = requests.begin("first");
  const independent = requests.begin("second");
  assert.equal(requests.isCurrent("first", old), false);
  assert.equal(requests.isCurrent("first", newer), true);
  assert.equal(requests.isCurrent("second", independent), true);
  requests.begin("first");
  assert.equal(requests.isCurrent("first", newer), false);
});

async function renderComponent(name, props) {
  const sourceUrl = new URL("../app/research-history.tsx", import.meta.url);
  const require = createRequire(sourceUrl);
  const ts = require("typescript");
  const compiled = ts.transpileModule(await readFile(sourceUrl, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  });
  const loaded = { exports: {} };
  new Function("require", "module", "exports", compiled.outputText)(require, loaded, loaded.exports);
  assert.equal(typeof loaded.exports[name], "function", `missing research presentation: ${name}`);
  return require("react-dom/server").renderToStaticMarkup(require("react").createElement(loaded.exports[name], props));
}

test("formal validation renders its failed gate and missing uncertainty instead of raw experiment data", async () => {
  const html = await renderComponent("ValidationCard", { validation: { id: "fixture", title: "离线验证", kind: "formal_validation",
    status: "baseline_retained", conclusion: "候选未通过留出集，保留基线。", started_at: baseline.started_at,
    ended_at: baseline.ended_at, interval: "15m", return_model: "linear_usdm_v1",
    gates: [{ label: "留出集", status: "fail", detail: "收益下界未达标" }],
    candidates: [{ name: "候选一", trades: 40, expectancy: .01, delta: .002, validation_gate: false }],
    runtime_config: { internal_secret_marker: "never_show_this" },
  } });
  assert.match(html, /保留基线/);
  assert.match(html, /收益下界未达标/);
  assert.match(html, /待补/);
  assert.match(html, /2026-01-01/);
  assert.doesNotMatch(html, /never_show_this|internal_secret_marker|<pre/);
});

test("optimization comparisons preserve paired-week accounting and their actual comparator", async () => {
  const html = await renderComponent("CandidateTable", { metricBasis: "paired_complete_utc_week_return", candidates: [{ name: "fee_buffer_partial", comparator: "fee_buffer", linked_return: .04, expectancy: .99, trades: 50, delta: .002, ci_low: -.001, ci_high: .005, q_value: .2, validation_gate: false }] });
  assert.match(html, /拼接组合收益/);
  assert.match(html, /预声明对照/);
  assert.match(html, /配对周差/);
  assert.match(html, /费用缓冲/);
  assert.match(html, /4\.00%/);
  assert.doesNotMatch(html, /单笔平均收益|99\.00%|相对基线差值/);
});

test("per-trade validation never substitutes portfolio linked returns for a missing expectancy", async () => {
  const html = await renderComponent("CandidateTable", { metricBasis: "per_trade_net_return", candidates: [{ name: "候选", linked_return: .82, delta: .002 }] });
  assert.match(html, /单笔平均收益/);
  assert.match(html, /待补/);
  assert.doesNotMatch(html, /82\.00%|拼接组合收益/);
});

test("diagnostic gate codes render their Chinese evidence gaps", async () => {
  const html = await renderComponent("ValidationCard", { validation: { id: "blocked", kind: "optimization_diagnostic", title: "诊断实验", status: "unverified", conclusion: "NO_SELECTION", gates: [{ label: "研究验证阻断", status: "fail", detail: "HOLDOUT_EXPOSED" }], candidates: [] } });
  assert.match(html, /留出集已被查看/);
  assert.match(html, /未选出可晋级候选/);
  assert.doesNotMatch(html, /HOLDOUT_EXPOSED|NO_SELECTION/);
});

test("quick comparison labels its realized curve and retains missing confidence intervals", async () => {
  const html = await renderComponent("QuickComparison", { first: baseline, second: alternative });
  assert.match(html, /逐笔复合收益/);
  assert.match(html, /闭仓曲线回撤/);
  assert.match(html, /置信区间/);
  assert.match(html, /待补/);
  assert.match(html, /诊断/);
  assert.doesNotMatch(html, /账户收益|账户回撤|验证通过/);
});

test("incompatible quick experiments show a reason without displaying an improvement ranking", async () => {
  const html = await renderComponent("QuickComparison", { first: baseline, second: { ...alternative, symbol: "ETHUSDT" } });
  assert.match(html, /不可比/);
  assert.match(html, /币种/);
  assert.doesNotMatch(html, /观察差值|优势候选/);
});
