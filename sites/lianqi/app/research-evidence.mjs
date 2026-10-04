/**
 * @typedef {{run_id?:string, symbol?:string, interval?:string, started_at?:string, ended_at?:string,
 * market_data_source?:string, config_revision?:string, return_model?:string,
 * params?:Record<string,string|number|boolean|null|undefined>,
 * execution_metadata?:Record<string,string|number|boolean|undefined>, metrics?:Record<string,number|undefined>}} ResearchRun
 */

/** @param {unknown} value */
export function researchNumber(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** @param {unknown} value @param {string} [missing] */
export function researchPercent(value, missing = "待补") {
  const number = researchNumber(value);
  return number === null ? missing : `${(number * 100).toFixed(2)}%`;
}

/** @param {unknown} low @param {unknown} high */
export function researchConfidenceInterval(low, high) {
  const first = researchNumber(low), last = researchNumber(high);
  return first === null || last === null || first > last ? "待补" : `${researchPercent(first)} ～ ${researchPercent(last)}`;
}

/** @param {unknown} value */
function timestamp(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}/.test(value)) return null;
  const date = Date.parse(value);
  return Number.isFinite(date) ? date : null;
}

/** @param {unknown} start @param {unknown} end */
function validRange(start, end) {
  const first = timestamp(start), last = timestamp(end);
  return first !== null && last !== null && first < last;
}

/** @param {unknown} start @param {unknown} end */
export function researchDateRange(start, end) {
  if (!validRange(start, end)) return "日期范围待补";
  return `${new Date(timestamp(start)).toISOString().slice(0, 10)} ～ ${new Date(timestamp(end)).toISOString().slice(0, 10)}（UTC）`;
}

/** @param {ResearchRun|null|undefined} run */
function returnModel(run) {
  return run?.execution_metadata?.return_model ?? run?.params?.return_model ?? run?.return_model;
}

/** @param {ResearchRun|null|undefined} run */
export function researchModelLabel(run) {
  const model = returnModel(run);
  return model === "linear_usdm_v1" ? "线性 USDT 合约" : !model || model === "legacy_ratio_v1" ? "历史口径（旧版）" : "未知收益口径";
}

/** @param {ResearchRun|null|undefined} run */
export function quickResearchConclusion(run) {
  if (returnModel(run) !== "linear_usdm_v1") return "历史口径仅供复盘，不与新版收益比较。";
  if (!((researchNumber(run?.metrics?.total_trades) ?? 0) > 0)) return "暂无已闭仓样本，不能判断效果。";
  return "这是单币信号诊断；还需样本外与成本压力验证，不能据此认定策略优势。";
}

const EXECUTION_FIELDS = {
  return_model: "执行收益口径", holding_limit_convention: "持仓计数规则", max_observed_bars: "实际观察上限",
  timestamp_boundary: "时间边界", equity_boundary: "收益曲线边界", funding_included: "资金费标记",
  independent_slippage_included: "独立滑点标记", execution_scope: "执行范围",
};
const EXECUTION_CONVENTIONS = {
  holding_limit_convention: ["entry_bar_is_first", "legacy_inclusive_end"],
  timestamp_boundary: ["bar_open_label_not_execution_time"],
  equity_boundary: ["exit_label_compounding_without_open_position_mtm"],
  execution_scope: ["single_symbol_signal_diagnostic"],
};

/** @param {ResearchRun|null|undefined} first @param {ResearchRun|null|undefined} second */
export function compareResearchRuns(first, second) {
  /** @type {string[]} */
  const reasons = [];
  if (first?.run_id && first.run_id === second?.run_id) reasons.push("请选择两次不同的实验");
  /** @param {string} label @param {unknown} a @param {unknown} b */
  const compare = (label, a, b) => {
    if (a === null || a === undefined || a === "" || b === null || b === undefined || b === "") reasons.push(`${label}缺失`);
    else if (a !== b) reasons.push(`${label}不同`);
  };
  compare("币种", first?.symbol, second?.symbol);
  compare("周期", first?.interval, second?.interval);
  compare("开始时间", timestamp(first?.started_at), timestamp(second?.started_at));
  compare("结束时间", timestamp(first?.ended_at), timestamp(second?.ended_at));
  if (!validRange(first?.started_at, first?.ended_at)) reasons.push("基准日期范围无效");
  if (!validRange(second?.started_at, second?.ended_at)) reasons.push("对照日期范围无效");
  compare("数据来源", first?.market_data_source, second?.market_data_source);
  compare("配置版本", first?.config_revision, second?.config_revision);
  compare("单边手续费", researchNumber(first?.params?.fee_rate), researchNumber(second?.params?.fee_rate));
  compare("目标盈亏比", researchNumber(first?.params?.reward_risk), researchNumber(second?.params?.reward_risk));
  compare("最大持仓上限", researchNumber(first?.params?.max_hold_bars), researchNumber(second?.params?.max_hold_bars));
  if (returnModel(first) !== "linear_usdm_v1" || returnModel(second) !== "linear_usdm_v1") reasons.push("历史口径或未知收益版本不可比较");
  /** @type {[string, ResearchRun|null|undefined][]} */
  const runs = [["基准", first], ["对照", second]];
  for (const [name, run] of runs) {
    if (!run?.execution_metadata) reasons.push(`${name}执行元数据缺失`);
    if (run?.params?.return_model && run?.execution_metadata?.return_model && run.params.return_model !== run.execution_metadata.return_model) reasons.push(`${name}收益口径记录不一致`);
    if (!((researchNumber(run?.metrics?.total_trades) ?? 0) > 0)) reasons.push(`${name}闭仓样本不足`);
    const fee = researchNumber(run?.params?.fee_rate);
    if (fee !== null && fee < 0) reasons.push(`${name}手续费无效`);
    if (run?.execution_metadata && ["funding_included", "independent_slippage_included"].some(key => typeof run.execution_metadata?.[key] !== "boolean")) reasons.push(`${name}执行成本标记无效`);
    const observedBars = researchNumber(run?.execution_metadata?.max_observed_bars);
    if (run?.execution_metadata && (observedBars === null || !Number.isInteger(observedBars) || observedBars < 1)) reasons.push(`${name}实际观察上限无效`);
    for (const [key, accepted] of Object.entries(EXECUTION_CONVENTIONS)) {
      if (run?.execution_metadata?.[key] !== undefined && !accepted.includes(String(run.execution_metadata[key]))) reasons.push(`${name}${EXECUTION_FIELDS[key]}未识别`);
    }
  }
  if (first?.execution_metadata && second?.execution_metadata) {
    for (const [key, label] of Object.entries(EXECUTION_FIELDS)) compare(label, first.execution_metadata[key], second.execution_metadata[key]);
  }
  const unique = [...new Set(reasons)];
  const comparable = unique.length === 0;
  /** @type {Record<string,number|null>|null} */
  let delta = null;
  if (comparable) {
    delta = {};
    for (const key of ["expectancy", "win_rate", "total_return", "max_drawdown"]) {
      const a = researchNumber(first?.metrics?.[key]), b = researchNumber(second?.metrics?.[key]);
      delta[key] = a === null || b === null ? null : b - a;
    }
  }
  return { comparable, reasons: unique, delta, conclusion: comparable ? "条件一致，可查看诊断差值；尚不能判定样本外优势。" : "这两次实验不可比，请统一条件后再做对照。" };
}

/** @param {{status?:string,kind?:string,started_at?:string,ended_at?:string,interval?:string,return_model?:string,gates?:{status?:string}[]}} validation */
export function validationSummary(validation) {
  const gates = Array.isArray(validation.gates) ? validation.gates : [];
  const passed = validation.kind === "formal_validation" && validation.status === "candidate_passed" && validRange(validation.started_at, validation.ended_at)
    && !!validation.interval && validation.return_model === "linear_usdm_v1" && gates.length > 0
    && gates.every(gate => gate.status === "pass" || gate.status === "not_applicable")
    && gates.some(gate => gate.status === "pass");
  if (passed) return { passed: true, label: "候选通过研究验证", tone: "positive" };
  if (validation.status === "baseline_retained") return { passed: false, label: "候选未通过，保留基线", tone: "negative" };
  if (validation.status === "smoke_only") return { passed: false, label: "仅完成流程冒烟", tone: "neutral" };
  if (validation.status === "incomplete") return { passed: false, label: "验证受阻，证据未完整", tone: "negative" };
  if (validation.kind === "optimization_diagnostic" && gates.some(gate => gate.status === "fail")) return { passed: false, label: "诊断验证未通过", tone: "negative" };
  return { passed: false, label: validation.kind === "optimization_diagnostic" ? "诊断完成，尚待正式验证" : "验证证据待补", tone: "neutral" };
}

const EVIDENCE_REASONS = {
  HOLDOUT_EXPOSED: "留出集已被查看，不能作为独立样本外证据",
  PIT_UNIVERSE_UNVERIFIED: "历史币池的时点一致性尚未核验",
  OI_AVAILABLE_AT_UNVERIFIED: "持仓量数据的可用时间尚未核验",
  NO_OKX_CONFIRMATION: "缺少 OKX 跨交易所复核",
  NO_SELECTION: "未选出可晋级候选",
  RESEARCH_ONLY: "仅供研究",
};

/** @param {string|undefined} detail */
export function researchEvidenceText(detail) {
  let text = detail || "证据待补";
  for (const [code, description] of Object.entries(EVIDENCE_REASONS)) text = text.replaceAll(code, description);
  return text;
}

export function createLatestResearchRequest() {
  /** @type {Map<string,number>} */
  const versions = new Map();
  return {
    /** @param {string} key */
    begin(key) { const version = (versions.get(key) ?? 0) + 1; versions.set(key, version); return version; },
    /** @param {string} key @param {number} version */
    isCurrent(key, version) { return versions.get(key) === version; },
  };
}
