"""Bounded, read-only summaries of archived research evidence.

Checksums bind the small files displayed here. They do not authenticate the
archive's author, replay raw market data, or confer production authorization.
"""
from __future__ import annotations

import ast
import csv
import hashlib
import heapq
import io
import json
import math
import os
import re
import stat
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_RUN_BYTES = 8 * 1024 * 1024
MAX_CSV_ROWS = 5000
RUN_NAME = re.compile(r"strategy_(validation|optimization)_[A-Za-z0-9_-]{1,100}\Z")
SHA256 = re.compile(r"[a-fA-F0-9]{64}\Z")


class EvidenceIssue(ValueError):
    def __init__(self, status: str, detail: str):
        self.status = status
        self.detail = detail


class _Reader:
    def __init__(self, directory: Path):
        self.directory = directory
        self.remaining = MAX_RUN_BYTES
        self.files: dict[str, bytes] = {}

    def read(self, name: str) -> bytes:
        if name in self.files:
            return self.files[name]
        if not _relative_name(name) or "/" in name:
            raise EvidenceIssue("unverified", "产物路径不安全，未读取。")
        directory_fd = file_fd = None
        try:
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            info = os.fstat(file_fd)
            # The real frozen cache-hash inventory is about 2.4 MiB; no cache
            # files are opened. Summary files retain the smaller bound.
            file_limit = 4 * 1024 * 1024 if name == "data_config_hashes.json" else MAX_FILE_BYTES
            limit = min(file_limit, self.remaining)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise EvidenceIssue("unverified", "产物非普通小文件或超过读取上限。")
            with os.fdopen(file_fd, "rb") as handle:
                file_fd = None
                content = handle.read(limit + 1)
            if len(content) > limit:
                raise EvidenceIssue("unverified", "产物超过读取上限。")
            self.remaining -= len(content)
            self.files[name] = content
            return content
        except FileNotFoundError:
            raise EvidenceIssue("incomplete", f"缺少必要产物：{name}。") from None
        except OSError:
            raise EvidenceIssue("unverified", "产物不可安全读取；拒绝符号链接与特殊文件。") from None
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if directory_fd is not None:
                os.close(directory_fd)

    def json(self, name: str) -> Any:
        content = self.read(name)
        try:
            return json.loads(content.decode("utf-8"), parse_constant=_invalid_constant)
        except (UnicodeError, ValueError, RecursionError):
            raise EvidenceIssue("unverified", f"{name} 不是有效的有限数值 JSON。") from None

    def csv(self, name: str) -> list[dict[str, str]]:
        content = self.read(name)
        try:
            reader = csv.DictReader(io.StringIO(content.decode("utf-8")))
            rows = []
            for row in reader:
                if len(rows) >= MAX_CSV_ROWS or None in row or None in row.values():
                    raise ValueError("invalid or oversized CSV")
                rows.append(row)
            return rows
        except (UnicodeError, ValueError, csv.Error):
            raise EvidenceIssue("unverified", f"{name} 格式异常或超过行数上限。") from None


def _invalid_constant(value: str) -> None:
    raise ValueError("nonfinite JSON constant")


def _relative_name(name: Any) -> bool:
    if not isinstance(name, str) or not name or "\\" in name:
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in name.split("/"))


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceIssue("unverified", "摘要结构异常。")
    return value


def _number(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        raise EvidenceIssue("unverified", "必要指标缺失或非有限数值。")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise EvidenceIssue("unverified", "必要指标缺失或非有限数值。") from None
    if not math.isfinite(number):
        raise EvidenceIssue("unverified", "必要指标缺失或非有限数值。")
    return number


def _count(value: Any) -> int:
    number = _number(value)
    if number < 0 or number > 2**53 - 1 or not number.is_integer():
        raise EvidenceIssue("unverified", "交易数或实验计数异常。")
    return int(number)


def _boolean(value: Any) -> bool:
    if value is True or value == "True":
        return True
    if value is False or value == "False":
        return False
    raise EvidenceIssue("unverified", "门槛证据缺失或类型异常。")


def _name(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value):
        raise EvidenceIssue("unverified", "候选名称或版本字段异常。")
    return value


def _time(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value
    except ValueError:
        return None


def _gate(label: str, status: str, detail: str) -> dict[str, str]:
    return {"label": label, "status": status, "detail": detail}


def _integrity(reader: _Reader, required: list[str], *, optimization: bool = False) -> dict[str, str]:
    manifest = _object(reader.json("artifact_hashes.json"))
    hashes = _object(manifest.get("file_hashes")) if optimization else manifest
    if len(hashes) > 2048 or not all(_relative_name(name) and isinstance(digest, str) and SHA256.fullmatch(digest)
                                   for name, digest in hashes.items()):
        raise EvidenceIssue("unverified", "产物哈希目录含异常路径或摘要。")
    for name in required:
        content = reader.read(name)
        digest = hashes.get(name)
        if not digest:
            raise EvidenceIssue("incomplete", f"必要产物未绑定哈希：{name}。")
        if hashlib.sha256(content).hexdigest() != digest.lower():
            raise EvidenceIssue("unverified", f"必要产物 SHA-256 不一致：{name}。")
    return hashes


@lru_cache(maxsize=1)
def _final_gate():
    """Load only the existing pure predicate from trusted repository code."""
    source = Path(__file__).with_name("strategy_validation.py").read_text(encoding="utf-8")
    if len(source) > 256 * 1024:
        raise ValueError("unexpected gate source")
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "passes_final_candidate_gates")
    expected = ["baseline_metrics", "candidate_metrics", "double_cost_metrics"]
    returned = function.body[-1] if function.body else None
    if (not isinstance(returned, ast.Return) or not isinstance(returned.value, ast.Call)
            or not isinstance(returned.value.func, ast.Name) or returned.value.func.id != "bool"
            or len(returned.value.args) != 1 or not isinstance(returned.value.args[0], ast.BoolOp)
            or not isinstance(returned.value.args[0].op, ast.And) or len(returned.value.args[0].values) != 6):
        raise ValueError("unsupported gate result structure")
    if ([arg.arg for arg in function.args.args] != expected
            or [arg.arg for arg in function.args.kwonlyargs] != ["okx_direction_consistent", "max_symbol_profit_share"]
            or function.decorator_list or function.args.defaults or function.args.vararg or function.args.kwarg
            or any(not isinstance(node, (ast.Expr, ast.Assign, ast.Return)) for node in function.body)
            or any(isinstance(node, (ast.Import, ast.ImportFrom, ast.Attribute)) for node in ast.walk(function))
            or any(not isinstance(node.func, ast.Name) or node.func.id not in {"bool", "abs"}
                   for node in ast.walk(function) if isinstance(node, ast.Call))):
        raise ValueError("unsupported gate source")
    namespace = {"__builtins__": {"bool": bool, "abs": abs, "dict": dict, "float": float}}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<repository-final-gate>", "exec"), namespace)
    return namespace["passes_final_candidate_gates"]


def _return_model(manifest: dict[str, Any], summary: dict[str, Any], gates: list[dict[str, str]]) -> str:
    values = []
    arguments = _object(manifest.get("arguments", {}))
    for record in (arguments, manifest.get("execution_metadata", {}), summary.get("execution_metadata", {})):
        record = _object(record)
        if record.get("return_model") is not None:
            values.append(_name(record["return_model"]))
    if len(set(values)) > 1:
        raise EvidenceIssue("unverified", "收益口径字段互相冲突。")
    if values:
        return values[0]
    gates.append(_gate("历史收益口径", "not_applicable", "旧档未保存收益版本，按 legacy_ratio_v1 展示；不并入新版对照。"))
    return "legacy_ratio_v1"


def _candidates(comparison: list[dict[str, str]], folds: list[dict[str, str]]) -> list[dict[str, Any]]:
    totals: dict[str, tuple[int, float]] = {}
    seen_folds = set()
    for row in folds:
        name = _name(row.get("candidate"))
        key = (name, row.get("fold"))
        if key in seen_folds or row.get("phase", "validation") != "validation":
            raise EvidenceIssue("unverified", "fold 指标重复或混入其他阶段。")
        seen_folds.add(key)
        trades, expectancy = _count(row.get("total_trades")), _number(row.get("expectancy"))
        count, weighted = totals.get(name, (0, 0.0))
        totals[name] = _count(count + trades), _number(weighted + trades * expectancy)
    if "baseline" not in totals:
        raise EvidenceIssue("incomplete", "缺少同一实验的基线 fold 指标。")
    output = []
    baseline_trades, weighted = totals["baseline"]
    baseline = {"name": "baseline", "is_baseline": True, "trades": baseline_trades}
    if baseline_trades:
        baseline["expectancy"] = weighted / baseline_trades
    output.append(baseline)
    seen = {"baseline"}
    for row in comparison:
        name = _name(row.get("candidate"))
        if name in seen or name not in totals:
            raise EvidenceIssue("unverified", "候选比较与 fold 指标无法绑定。")
        seen.add(name)
        item: dict[str, Any] = {"name": name, "is_baseline": False}
        for key in ("delta", "ci_low", "ci_high", "q_value"):
            item[key] = _number(row.get(key))
        if item["ci_low"] > item["ci_high"] or not 0 <= item["q_value"] <= 1:
            raise EvidenceIssue("unverified", "候选区间或 q 值异常。")
        item["validation_gate"] = _boolean(row.get("validation_gate"))
        fdr = _boolean(row.get("fdr_reject"))
        fold_ratio = _number(row.get("fold_positive_ratio"))
        symbol_ratio = _number(row.get("symbol_nonworse_ratio"))
        drawdown_ratio = _number(row.get("max_drawdown_ratio"))
        if not 0 <= fold_ratio <= 1 or not 0 <= symbol_ratio <= 1 or drawdown_ratio < 0:
            raise EvidenceIssue("unverified", "验证期稳健性比率异常。")
        # Cross-check the stored statistical gate exactly as compare_candidates
        # declares it. This adapter does not calculate new tests or tune rules.
        declared_gate = fdr and item["delta"] > 0 and fold_ratio >= .70 and symbol_ratio > .50 and drawdown_ratio <= 1.10
        if item["validation_gate"] != declared_gate or (fdr and item["q_value"] > .10):
            raise EvidenceIssue("unverified", "候选验证期结论与 FDR/增量证据不一致。")
        trades, weighted = totals[name]
        item["trades"] = trades
        if trades:
            item["expectancy"] = weighted / trades
        output.append(item)
    if set(totals) != seen:
        raise EvidenceIssue("incomplete", "候选比较表未覆盖全部 fold 候选。")
    return output


def _formal(item: dict[str, Any], reader: _Reader) -> None:
    required = ["manifest.json", "test_summary.json", "data_config_hashes.json", "universe_snapshot.json",
                "candidate_comparison.csv", "fold_metrics.csv"]
    hashes = _integrity(reader, required)
    item["gates"].append(_gate("摘要产物完整性", "pass", "展示所依赖的小产物 SHA-256 一致；未重放原始行情或逐笔交易。"))
    manifest, summary, data_hashes = (_object(reader.json(name)) for name in required[:3])
    if manifest.get("test_summary") != summary:
        raise EvidenceIssue("unverified", "manifest 与最终测试摘要不一致。")
    item["return_model"] = _return_model(manifest, summary, item["gates"])
    item["started_at"], item["ended_at"] = _time(manifest.get("start")), _time(manifest.get("end"))
    if item["started_at"] is None or item["ended_at"] is None:
        raise EvidenceIssue("incomplete", "未记录完整研究时间边界。")
    if datetime.fromisoformat(item["started_at"].replace("Z", "+00:00")) >= datetime.fromisoformat(item["ended_at"].replace("Z", "+00:00")):
        raise EvidenceIssue("unverified", "研究时间边界没有严格先后顺序。")
    item["interval"] = _name(manifest.get("interval"))
    before, after = manifest.get("production_config_sha256_before"), manifest.get("production_config_sha256_after")
    base_config = manifest.get("base_config_sha256")
    if (manifest.get("production_config_modified") is not False or manifest.get("deployed") is not False
            or not isinstance(before, str) or not SHA256.fullmatch(before) or before != after
            or not isinstance(base_config, str) or not SHA256.fullmatch(base_config)
            or data_hashes.get("production_config_file_sha256") != before
            or data_hashes.get("base_config_sha256") != manifest.get("base_config_sha256")
            or not isinstance(data_hashes.get("candidate_set_sha256"), str)
            or not SHA256.fullmatch(data_hashes["candidate_set_sha256"])
            or data_hashes.get("universe_snapshot_sha256") != hashlib.sha256(reader.read("universe_snapshot.json")).hexdigest()):
        raise EvidenceIssue("unverified", "配置未改动或冻结输入的摘要绑定不一致。")
    item["gates"].append(_gate("冻结输入与配置边界", "pass", "实验内配置前后及池快照摘要一致；不核验当前生产配置或历史缓存。"))
    item["candidates"] = _candidates(reader.csv("candidate_comparison.csv"), reader.csv("fold_metrics.csv"))
    if _count(manifest.get("candidate_count")) != len(item["candidates"]):
        raise EvidenceIssue("unverified", "候选数量与对照证据不一致。")
    arguments = _object(manifest.get("arguments", {}))
    if "smoke" in arguments and not isinstance(arguments["smoke"], bool):
        raise EvidenceIssue("unverified", "smoke 模式字段类型异常。")
    smoke = arguments.get("smoke") is True or item["id"].endswith("_smoke") or summary.get("status") == "smoke_only"
    if smoke:
        item.update(status="smoke_only", conclusion="仅完成网络与产物冒烟；未通过正式策略验证，不能用于参数晋级。")
        item["gates"].append(_gate("最终测试联合门槛", "not_applicable", "smoke 不执行正式留出测试与晋级判断。"))
    else:
        selected = summary.get("selected_candidate")
        if selected is None:
            if summary.get("status") != "baseline_retained" or any(row.get("validation_gate") for row in item["candidates"]):
                raise EvidenceIssue("unverified", "未选候选与验证期记录结论不一致。")
            item.update(status="baseline_retained", conclusion="验证期未选出候选，保留基线；未解封最终测试集。")
            item["gates"].append(_gate("最终测试联合门槛", "not_applicable", "没有候选进入永久留出测试。"))
        else:
            selected = _name(selected)
            candidate = next((row for row in item["candidates"] if row["name"] == selected and not row["is_baseline"]), None)
            if candidate is None or candidate.get("validation_gate") is not True:
                raise EvidenceIssue("unverified", "入选候选未绑定通过的验证期对照。")
            item["gates"].append(_gate("验证期 FDR 联合证据", "pass", f"入选 {selected}，q={candidate['q_value']:.6g}；验证期通过不等于最终测试通过。"))
            baseline, final, cost = (_object(summary.get(key)) for key in ("baseline", "candidate", "double_cost"))
            baseline = {key: _number(baseline.get(key)) for key in ("expectancy", "max_drawdown")}
            final = {"total_trades": _count(final.get("total_trades")), **{key: _number(final.get(key)) for key in ("expectancy", "max_drawdown")}}
            cost = {"expectancy": _number(cost.get("expectancy"))}
            okx = _object(summary.get("okx"))
            if okx.get("skipped") is True:
                direction = False
                item["gates"].append(_gate("OKX 方向复核", "missing", "实验跳过跨交易所复核；不能通过最终门槛。"))
            else:
                okx_baseline = _number(_object(okx.get("baseline")).get("expectancy"))
                okx_candidate = _number(_object(okx.get("candidate")).get("expectancy"))
                direction = okx_candidate >= okx_baseline and okx_candidate > 0
                item["gates"].append(_gate("OKX 方向复核", "pass" if direction else "fail", f"候选 {okx_candidate:.6g}，基线 {okx_baseline:.6g}。"))
            share = _number(summary.get("max_symbol_profit_share"))
            if not 0 <= share <= 1:
                raise EvidenceIssue("unverified", "单品种盈利集中度数值异常。")
            try:
                passed = _final_gate()(baseline, final, cost, okx_direction_consistent=direction, max_symbol_profit_share=share)
            except (OSError, ValueError, TypeError, KeyError, NameError, StopIteration, SyntaxError):
                raise EvidenceIssue("unverified", "现有最终判据缺失或结构改变，未执行替代门槛。") from None
            item["gates"].append(_gate("最终测试联合门槛", "pass" if passed else "fail",
                f"复用现有判据；候选交易 {final['total_trades']}，期望 {final['expectancy']:.6g} vs 基线 {baseline['expectancy']:.6g}，回撤 {final['max_drawdown']:.6g}，双倍成本期望 {cost['expectancy']:.6g}，单品种盈利占比 {share:.6g}。"))
            expected_status = "candidate_passed" if passed else "baseline_retained"
            if summary.get("status") != expected_status:
                raise EvidenceIssue("unverified", "记录结论与现有最终联合门槛不一致。")
            item.update(status=expected_status, conclusion="候选通过历史研究流程门槛；仅限研究，不能授权生产或交易。" if passed else "候选未通过全部最终门槛，保留基线；不改生产参数。")
    if "report.html" in hashes:
        _integrity(reader, ["report.html"])
        item["report_url"] = f"/reports/{quote(item['id'])}/report.html"


def _optimization(item: dict[str, Any], reader: _Reader) -> None:
    names = ["experiment_registry.json", "comparison.json", "execution_reconciliation.json", "fold_metrics.json"]
    _integrity(reader, names, optimization=True)
    registry, comparison, reconciliation = (_object(reader.json(name)) for name in names[:3])
    folds = reader.json("fold_metrics.json")
    item["gates"].append(_gate("诊断摘要完整性", "pass", "仅核对展示所依赖的小产物；未核验所有大回放或重跑实验。"))
    if any(record.get("status") != "RESEARCH_ONLY" or record.get("selection") != "NO_SELECTION"
           or record.get("promotion_eligible") is not False for record in (registry, comparison)):
        raise EvidenceIssue("unverified", "诊断记录的 RESEARCH_ONLY/NO_SELECTION 边界不一致。")
    declared_folds = registry.get("folds")
    variants, stresses = registry.get("variants"), registry.get("stress")
    if not isinstance(declared_folds, list) or not isinstance(variants, dict) or not isinstance(stresses, (list, dict)) or not isinstance(folds, list):
        raise EvidenceIssue("incomplete", "缺少完整诊断实验矩阵。")
    fold_names = {_name(_object(row).get("name")) for row in declared_folds}
    variant_names = {_name(name) for name in variants}
    stress_names = {_name(name) for name in stresses}
    expected = {(fold, variant, stress) for fold in fold_names for variant in variant_names for stress in stress_names}
    observed = {(_object(row).get("fold"), row.get("variant"), row.get("stress")) for row in folds}
    if not expected or len(expected) > 2000 or observed != expected or len(folds) != len(expected):
        raise EvidenceIssue("unverified", "诊断 fold/方案/成本矩阵不完整或重复。")
    rows = comparison.get("summary")
    if not isinstance(rows, list) or len(rows) != len(variant_names) * len(stress_names):
        raise EvidenceIssue("incomplete", "诊断对照摘要不完整。")
    observed_summary = {(row.get("variant"), row.get("stress")) for row in rows if isinstance(row, dict)}
    if observed_summary != {(variant, stress) for variant in variant_names for stress in stress_names}:
        raise EvidenceIssue("unverified", "诊断摘要与实验矩阵不一致。")
    primary = _object(registry.get("primary_tests"))
    comparisons = comparison.get("comparisons")
    if not isinstance(comparisons, list) or {(_object(row).get("candidate"), row.get("comparator")) for row in comparisons} != set(primary.items()) or len(comparisons) != len(primary):
        raise EvidenceIssue("unverified", "统计对照不符合原实验预声明关系。")
    replay_rows = reconciliation.get("rows")
    expected_replays = {(fold, f"{variant}_{stress}") for fold, variant, stress in expected}
    if (not isinstance(replay_rows, list) or len(replay_rows) != len(expected)
            or _count(reconciliation.get("replays")) != len(expected)
            or {(_object(row).get("fold"), row.get("replay")) for row in replay_rows} != expected_replays
            or any(_object(row).get("accounting_reconciled") is not True for row in replay_rows)):
        raise EvidenceIssue("unverified", "工程勾稽计数或结果不一致。")
    item["gates"].append(_gate("工程回放勾稽", "pass", f"{len(expected)} 组记录一致；工程通过不表示策略验证通过。"))
    common = _object(registry.get("common", {}))
    item["return_model"] = _name(common.get("return_model", "legacy_ratio_v1"))
    item["interval"] = _name(common["interval"]) if common.get("interval") else "未记录"
    boundaries = [_object(row).get(key) for row in declared_folds for key in ("start", "end")]
    if all(isinstance(value, (int, float)) and math.isfinite(value) for value in boundaries):
        item["started_at"] = datetime.fromtimestamp(min(boundaries) / 1000, timezone.utc).isoformat()
        item["ended_at"] = datetime.fromtimestamp(max(boundaries) / 1000, timezone.utc).isoformat()
    by_name = {_name(row["candidate"]): row for row in comparisons}
    candidates = []
    for row in rows:
        if _count(row.get("folds")) != len(fold_names):
            raise EvidenceIssue("unverified", "诊断摘要 fold 数不一致。")
        if row.get("stress") != "base":
            continue
        name = _name(row.get("variant"))
        candidate: dict[str, Any] = {"name": name, "is_baseline": name == "baseline", "trades": _count(row.get("trades")),
                                     "linked_return": _number(row.get("linked_return")), "validation_gate": False}
        if name in by_name:
            source = by_name[name]
            candidate["comparator"] = _name(source.get("comparator"))
            for key in ("delta", "ci_low", "ci_high", "q_value"):
                candidate[key] = _number(source.get(key))
            if candidate["ci_low"] > candidate["ci_high"] or not 0 <= candidate["q_value"] <= 1:
                raise EvidenceIssue("unverified", "诊断统计区间异常。")
        candidates.append(candidate)
    reasons = comparison.get("reasons")
    if not isinstance(reasons, list) or len(reasons) > 30:
        raise EvidenceIssue("incomplete", "缺少诊断阻断原因。")
    for reason in reasons:
        item["gates"].append(_gate("研究验证阻断", "fail", _name(reason)))
    item["gates"].append(_gate("历史执行版本", "not_applicable", "只展示冻结历史摘要，不表示当前源码已通过这些工程检查。"))
    item.update(status="unverified", conclusion="诊断研究验证未通过，NO_SELECTION；正向诊断与工程勾稽不能用于晋级或生产。",
                metric_basis="paired_complete_utc_week_return", candidates=candidates)


def list_validation_evidence(reports_dir: str | Path) -> dict[str, Any]:
    """Return at most 50 isolated research summaries; never mutate artifacts."""
    result: dict[str, Any] = {"scope": "RESEARCH_ONLY", "validations": []}
    directory = Path(reports_dir)
    if directory.is_symlink():
        return result
    try:
        with os.scandir(directory) as entries:
            names = heapq.nlargest(50, (entry.name for entry in entries
                                       if RUN_NAME.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False)),
                                    key=lambda name: (name.split("_", 2)[2], name))
    except OSError:
        return result
    for name in names:
        optimization = name.startswith("strategy_optimization_")
        item: dict[str, Any] = {"id": name, "kind": "optimization_diagnostic" if optimization else "formal_validation",
                               "title": "优化诊断对照" if optimization else "正式策略验证",
                               "status": "incomplete", "conclusion": "证据不完整，未确认研究结论。",
                               "started_at": None, "ended_at": None, "interval": "未记录",
                               "return_model": "legacy_ratio_v1", "metric_basis": "per_trade_net_return",
                               "gates": [], "candidates": []}
        try:
            (_optimization if optimization else _formal)(item, _Reader(directory / name))
        except EvidenceIssue as issue:
            item.update(status=issue.status, conclusion=issue.detail + " 未确认研究验证通过。", candidates=[])
            item.pop("report_url", None)
            item["gates"].append(_gate("证据校验", "missing" if issue.status == "incomplete" else "fail", issue.detail))
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            item.update(status="unverified", conclusion="摘要字段异常，未确认研究验证通过。", candidates=[])
            item.pop("report_url", None)
            item["gates"].append(_gate("证据校验", "fail", "摘要字段异常。"))
        result["validations"].append(item)
    return result
