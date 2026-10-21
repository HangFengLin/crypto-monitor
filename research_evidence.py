"""Bounded, read-only summaries of archived research evidence.

Checksums bind the small files displayed here. They do not authenticate the
archive's author, replay raw market data, or confer production authorization.
"""
from __future__ import annotations

import ast
import codecs
import csv
import hashlib
import heapq
import io
import json
import math
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_RUN_BYTES = 8 * 1024 * 1024
MAX_CSV_ROWS = 5000
# Large account ledgers are reduced one row at a time, never cached with the
# summaries. Independent disk/work bounds prevent an unbounded public read.
MAX_REPLAY_BYTES = 64 * 1024 * 1024
MAX_RUN_REPLAY_BYTES = 512 * 1024 * 1024
MAX_SCAN_REPLAY_BYTES = 512 * 1024 * 1024
MAX_REPLAY_ITEM_CHARS = 512 * 1024
MAX_REPLAY_ROWS = 1_000_000
REPLAY_ARRAYS = frozenset({"equity", "trades", "fills", "signals", "rejections",
                           "open_positions", "funding_incomplete_symbols"})
RUN_NAME = re.compile(r"strategy_(validation|optimization)_[A-Za-z0-9_-]{1,100}\Z")
SHA256 = re.compile(r"[a-fA-F0-9]{64}\Z")
ACCOUNT_SCOPE = "synchronized_linear_perpetual_account_v1"
# Reviewed complete ASTs of the two pure predicates, not archive-provided code.
# CPython3.11 omits FunctionDef.type_params; 3.12 includes an empty list. Only
# these two exact equivalent shapes are allowed, with no generic normalization.
# Any rule/helper/signature or nonempty type-parameter change needs new review.
TRUSTED_GATE_AST = {
    "passes_final_candidate_gates": frozenset({
        "ebb47ada870c85f240c658a061d341a0d44f3a8ba25e0aa2b475f6f1bc66bebe",  # CPython3.11
        "0ad40505573a9c344f31b96dca1ad192fcfc2eef324b73d780772f03ac3aa163",  # CPython3.12
    }),
    "account_evidence_valid": frozenset({
        "c7c614e685c6eed80c53a72a7299d2a8f82902f7b5a764d36dedd53cb99d01f5",  # CPython3.11
        "57fa975538973f90ef166fd9bc299ccdc9015dd0f423bccd2fa47885d666b8f1",  # CPython3.12
    }),
}


class EvidenceIssue(ValueError):
    def __init__(self, status: str, detail: str):
        self.status = status
        self.detail = detail


class _Reader:
    def __init__(self, directory: Path, *, replay_budget=None):
        self.directory = directory
        self.remaining = MAX_RUN_BYTES
        self.replay_remaining = MAX_RUN_REPLAY_BYTES
        self.replay_budget = replay_budget if replay_budget is not None else [MAX_SCAN_REPLAY_BYTES]
        self.files: dict[str, bytes] = {}

    @contextmanager
    def _open(self, name: str, limit: int):
        if not _relative_name(name):
            raise EvidenceIssue("unverified", "产物路径不安全，未读取。")
        directory_fd = file_fd = None
        try:
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            parts = name.split("/")
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise EvidenceIssue("unverified", "产物非普通小文件或超过读取上限。")
            with os.fdopen(file_fd, "rb") as handle:
                file_fd = None
                yield handle
        except FileNotFoundError:
            raise EvidenceIssue("incomplete", f"缺少必要产物：{name}。") from None
        except OSError:
            raise EvidenceIssue("unverified", "产物不可安全读取；拒绝符号链接与特殊文件。") from None
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if directory_fd is not None:
                os.close(directory_fd)

    def read(self, name: str) -> bytes:
        if name in self.files:
            return self.files[name]
        # The real frozen cache-hash inventory is about 2.4 MiB; no cache
        # files are opened. Summary files retain the smaller bound.
        file_limit = 4 * 1024 * 1024 if name == "data_config_hashes.json" else MAX_FILE_BYTES
        limit = min(file_limit, self.remaining)
        with self._open(name, limit) as handle:
            content = handle.read(limit + 1)
        if len(content) > limit:
            raise EvidenceIssue("unverified", "产物超过读取上限。")
        self.remaining -= len(content)
        self.files[name] = content
        return content

    def replay(self, name: str, consume):
        hashes = _integrity(self, [])
        expected = hashes.get(name)
        if expected is None:
            raise EvidenceIssue("incomplete", f"必要产物未绑定哈希：{name}。")
        if self.replay_budget[0] <= 0:
            raise EvidenceIssue("unverified", "本次扫描预算耗尽；尚未读取其余账户回放。")
        limit = min(MAX_REPLAY_BYTES, self.replay_remaining)
        stream = None
        try:
            with self._open(name, limit) as handle:
                size = os.fstat(handle.fileno()).st_size
                if size > self.replay_budget[0]:
                    raise EvidenceIssue("unverified", "本次扫描预算不足；尚未读取其余账户回放。")
                stream = _ReplayJSON(handle, min(limit, self.replay_budget[0]))
                metadata, counts, canonical = stream.reduce(consume)
            if stream.digest.hexdigest() != expected.lower():
                raise EvidenceIssue("unverified", f"必要产物 SHA-256 不一致：{name}。")
            return metadata, counts, canonical
        except EvidenceIssue:
            raise
        except (UnicodeError, ValueError, RecursionError):
            raise EvidenceIssue("unverified", "账户回放 JSON 异常或超过逐项读取上限。") from None
        finally:
            if stream is not None:
                self.replay_remaining = max(0, self.replay_remaining - stream.bytes_read)
                self.replay_budget[0] = max(0, self.replay_budget[0] - stream.bytes_read)

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


def _replay_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite JSON number")
    return number


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON property")
        result[key] = value
    return result


class _ReplayJSON:
    """Bounded JSON rows plus a canonical hash; full arrays live only on disk.

    Spooling canonical property values preserves the existing sorted-key result
    fingerprint even when source properties arrive in another order. Each row
    is validated and discarded; temporary files close on every failure path.
    """

    def __init__(self, handle, limit):
        self.handle, self.limit = handle, limit
        self.buffer, self.position, self.eof = "", 0, False
        self.bytes_read = 0
        self.digest = hashlib.sha256()
        self.utf8 = codecs.getincrementaldecoder("utf-8")()
        self.decoder = json.JSONDecoder(parse_constant=_invalid_constant,
            parse_float=_replay_float, object_pairs_hook=_unique_object)

    def fill(self):
        content = self.handle.read(64 * 1024)
        self.bytes_read += len(content)
        if self.bytes_read > self.limit:
            raise ValueError("replay read budget exceeded")
        self.digest.update(content)
        self.eof = not content
        self.buffer = self.buffer[self.position:] + self.utf8.decode(content, final=self.eof)
        self.position = 0

    def peek(self):
        while True:
            while self.position < len(self.buffer) and self.buffer[self.position] in " \t\r\n":
                self.position += 1
            if self.position < len(self.buffer):
                return self.buffer[self.position]
            if self.eof:
                return ""
            self.fill()

    def take(self, token):
        if self.peek() != token:
            raise ValueError("unexpected JSON delimiter")
        self.position += 1

    def value(self):
        if not self.peek():
            raise ValueError("missing JSON value")
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.position)
            except json.JSONDecodeError:
                if self.eof or len(self.buffer) - self.position > MAX_REPLAY_ITEM_CHARS:
                    raise
                self.fill()
                continue
            if end - self.position > MAX_REPLAY_ITEM_CHARS:
                raise ValueError("oversized replay item")
            # A number may end at a chunk boundary, e.g. 1|e-3 or 12|34.
            if not self.eof and (end == len(self.buffer) or self.buffer[end] not in " \t\r\n,]}:"):
                if len(self.buffer) - self.position > MAX_REPLAY_ITEM_CHARS:
                    raise ValueError("oversized replay item")
                self.fill()
                continue
            self.position = end
            return value

    def reduce(self, consume):
        metadata, counts, spools = {}, {}, {}
        metadata_chars = rows = 0
        try:
            self.take("{")
            while self.peek() != "}":
                key = self.value()
                if not isinstance(key, str) or key in spools or len(spools) >= 64:
                    raise ValueError("invalid replay property")
                self.take(":")
                spool = spools[key] = tempfile.TemporaryFile()
                if key in REPLAY_ARRAYS:
                    self.take("[")
                    count = 0
                    spool.write(b"[")
                    while self.peek() != "]":
                        item = self.value()
                        encoded = json.dumps(item, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
                        if count:
                            spool.write(b",")
                        spool.write(encoded)
                        consume(key, item)
                        count += 1
                        rows += 1
                        if rows > MAX_REPLAY_ROWS:
                            raise ValueError("replay row budget exceeded")
                        if self.peek() == "]":
                            break
                        self.take(",")
                        if self.peek() == "]":
                            raise ValueError("trailing array comma")
                    self.take("]")
                    spool.write(b"]")
                    counts[key] = count
                else:
                    value = self.value()
                    encoded = json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
                    metadata_chars += len(encoded)
                    if metadata_chars > MAX_FILE_BYTES:
                        raise ValueError("replay metadata budget exceeded")
                    spool.write(encoded)
                    metadata[key] = value
                if self.peek() == "}":
                    break
                self.take(",")
                if self.peek() == "}":
                    raise ValueError("trailing object comma")
            self.take("}")
            if self.peek():
                raise ValueError("trailing JSON data")
            digest = hashlib.sha256(b"{")
            for index, key in enumerate(sorted(spools)):
                if index:
                    digest.update(b",")
                digest.update(json.dumps(key, separators=(",", ":")).encode() + b":")
                spool = spools[key]
                spool.seek(0)
                while chunk := spool.read(64 * 1024):
                    digest.update(chunk)
            digest.update(b"}")
            return metadata, counts, digest.hexdigest()
        finally:
            for spool in spools.values():
                spool.close()


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
    """Extract both reviewed pure functions without importing runtime modules."""
    functions = []
    for filename, name in (("validation_account.py", "account_evidence_valid"),
                           ("strategy_validation.py", "passes_final_candidate_gates")):
        source = Path(__file__).with_name(filename).read_text(encoding="utf-8")
        if len(source) > 256 * 1024:
            raise ValueError("unexpected gate source")
        matches = [node for node in ast.parse(source).body
                   if isinstance(node, ast.FunctionDef) and node.name == name]
        if len(matches) != 1:
            raise ValueError("unsupported gate source")
        function = matches[0]
        shape = hashlib.sha256(ast.dump(function, include_attributes=False).encode()).hexdigest()
        if shape not in TRUSTED_GATE_AST[name]:
            raise ValueError("unsupported gate source shape")
        functions.append(function)
    namespace = {"__builtins__": {"bool": bool, "abs": abs, "dict": dict, "float": float,
        "all": all, "any": any, "len": len, "isinstance": isinstance, "int": int, "str": str},
        "math": math, "SCOPE": ACCOUNT_SCOPE}
    exec(compile(ast.Module(body=functions, type_ignores=[]), "<repository-final-gate>", "exec"), namespace)
    return namespace["passes_final_candidate_gates"]


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                    separators=(",", ":")).encode()).hexdigest()


def _same_number(actual: Any, expected: Any) -> None:
    if not math.isclose(_number(actual), _number(expected), rel_tol=1e-10, abs_tol=1e-9):
        raise EvidenceIssue("unverified", "账户回放与摘要指标不一致。")


def _account_protocol(reader: _Reader, manifest: dict[str, Any], data_hashes: dict[str, Any]):
    """Verify frozen contract/coverage; never open external funding/cache paths."""
    _integrity(reader, ["account_contract.json", "coverage_gate.json", "candidate_registry.json"])
    contract = _object(reader.json("account_contract.json"))
    sha = hashlib.sha256(reader.read("account_contract.json")).hexdigest()
    execution = _object(contract.get("execution"))
    expected_keys = {"initial_equity", "risk_fraction", "max_positions", "max_risk_fraction",
        "direction_risk_fraction", "fee_rate", "slippage_bps", "max_hold_bars", "reward_risk", "stop_mode", "return_model"}
    metadata = _object(manifest.get("execution_metadata"))
    if (contract.get("schema_version") != 1 or contract.get("research_only") is not True
            or contract.get("mark_frequency") != "bar_close" or set(execution) != expected_keys
            or execution.get("return_model") != "linear_usdm_v1" or execution.get("stop_mode") != "structure_atr"
            or manifest.get("account_verified") is not True
            or metadata.get("execution_scope") != ACCOUNT_SCOPE
            or metadata.get("equity_boundary") != "shared_cash_plus_bar_close_open_position_mtm"
            or metadata.get("account_contract_sha256") != sha or metadata.get("account_execution") != execution
            or metadata.get("funding_included") is not True or metadata.get("independent_slippage_included") is not True):
        raise EvidenceIssue("unverified", "正式账户合同与执行边界未绑定。")
    for key in ("initial_equity", "risk_fraction", "max_risk_fraction", "reward_risk", "fee_rate", "slippage_bps"):
        if isinstance(execution[key], bool) or not isinstance(execution[key], (int, float)):
            raise EvidenceIssue("unverified", "账户合同数值类型异常。")
        _number(execution[key])
    for key in ("initial_equity", "reward_risk"):
        if execution[key] <= 0:
            raise EvidenceIssue("unverified", "账户合同资金或风险参数异常。")
    for key in ("risk_fraction", "max_risk_fraction"):
        if not 0 < execution[key] <= 1:
            raise EvidenceIssue("unverified", "账户合同资金或风险参数异常。")
    if not 0 <= execution["fee_rate"] < 1 or not 0 <= execution["slippage_bps"] < 10000:
        raise EvidenceIssue("unverified", "账户合同成本参数异常。")
    if any(isinstance(execution[key], bool) or not isinstance(execution[key], int) or execution[key] <= 0
           for key in ("max_positions", "max_hold_bars")):
        raise EvidenceIssue("unverified", "账户合同持仓或期限异常。")
    directions = execution["direction_risk_fraction"]
    if not isinstance(directions, dict):
        directions = dict(long=directions, short=directions)
    if (set(directions) != {"long", "short"}
            or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < _number(value) <= 1
                   for value in directions.values())):
        raise EvidenceIssue("unverified", "账户方向风险参数异常。")
    coverage = _object(reader.json("coverage_gate.json"))
    universe = _object(reader.json("universe_snapshot.json"))
    funding = _object(contract.get("funding_evidence"))
    if set(coverage) != {"binance", "okx"} or manifest.get("coverage_gate") != coverage:
        raise EvidenceIssue("unverified", "两交易所完整覆盖记录未绑定。")
    for venue in coverage:
        row = _object(coverage[venue])
        symbols = sorted(_name(_object(value).get("symbol")) for value in universe.get(venue, []))
        unsigned = {key: value for key, value in row.items() if key != "sha256"}
        bindings = _object(funding.get(venue))
        if (not symbols or len(set(symbols)) != len(symbols) or row.get("exchange") != venue
                or row.get("complete") is not True or row.get("duplicate_requested") is not False
                or row.get("requested") != symbols or row.get("evaluated") != symbols
                or any(row.get(key) != [] for key in ("failed", "unexpected", "duplicate_coverage"))
                or row.get("sha256") != _fingerprint(unsigned) or set(bindings) != set(symbols)
                or any(not SHA256.fullmatch(str(_object(value).get("sha256", ""))) for value in bindings.values())):
            raise EvidenceIssue("unverified", "请求/已评估/失败品种或资金费源摘要覆盖不一致。")
    registry = _object(reader.json("candidate_registry.json"))
    candidates = registry.get("candidates")
    if (not isinstance(candidates, list) or registry.get("base_config_sha256") != manifest.get("base_config_sha256")
            or registry.get("candidate_set_sha256") != data_hashes.get("candidate_set_sha256")
            or _fingerprint(candidates) != registry.get("candidate_set_sha256")):
        raise EvidenceIssue("unverified", "冻结候选登记与摘要不一致。")
    boundaries = [datetime.fromisoformat(manifest[key].replace("Z", "+00:00")) for key in ("start", "end")]
    if any(boundary.tzinfo is None for boundary in boundaries):
        raise EvidenceIssue("unverified", "正式账户研究时间缺少时区。")
    return contract, sha, coverage, tuple(int(boundary.timestamp() * 1000) for boundary in boundaries)


def _account_metrics(reader: _Reader, metrics: dict[str, Any], protocol, venue: str,
                     multiplier: int = 1) -> dict[str, Any]:
    contract, sha, coverage, (research_start, research_end) = protocol
    metrics = dict(metrics)
    for key in ("account_verified", "funding_complete", "coverage_complete", "funding_included",
                "notional_fees_included", "independent_slippage_included"):
        if not _boolean(metrics.get(key)):
            raise EvidenceIssue("unverified", "账户及完整成本声明缺失。")
        metrics[key] = True  # CSV booleans become actual booleans for the pure gate.
    for key in ("total_trades", "expectancy", "max_drawdown", "account_max_drawdown", "total_return", "account_total_return"):
        metrics[key] = _number(metrics.get(key))
    for key in ("fee_multiplier", "slippage_multiplier"):
        metrics[key] = _count(metrics.get(key))
        if metrics[key] != multiplier:
            raise EvidenceIssue("unverified", "手续费及滑点压力场景未绑定。")
    if metrics.get("account_scope") != ACCOUNT_SCOPE or metrics.get("account_contract_sha256") != sha:
        raise EvidenceIssue("unverified", "账户摘要合同或口径不一致。")
    for key in ("account_input_sha256", "account_result_sha256"):
        if not SHA256.fullmatch(str(metrics.get(key, ""))):
            raise EvidenceIssue("unverified", "账户输入/结果摘要缺失。")
    path = PurePosixPath(str(metrics.get("account_artifact_path", "")))
    if path.parent.name != "account_replays" or not re.fullmatch(r"(binance|okx)_[0-9]+_[0-9]+_[a-f0-9]{16}\.json", path.name):
        raise EvidenceIssue("unverified", "账户回放路径不符合冻结归档约定。")
    name = "account_replays/" + path.name
    initial = _number(contract["execution"]["initial_equity"])
    peak, previous, first, drawdown, final = initial, None, None, 0., initial
    trade_count, net_returns, pnl = 0, 0., 0.
    positive = {}
    expected_funding = {symbol: value["sha256"] for symbol, value in contract["funding_evidence"][venue].items()}

    def consume(key, row):
        nonlocal peak, previous, first, drawdown, final, trade_count, net_returns, pnl
        if key == "equity":
            row = _object(row)
            timestamp, final = _count(row.get("time")), _number(row.get("equity"))
            if previous is not None and timestamp <= previous:
                raise EvidenceIssue("unverified", "账户盯市路径时间退序或超界。")
            if first is None:
                first = timestamp
            previous, peak = timestamp, max(peak, final)
            drawdown = min(drawdown, final / peak - 1)
            _same_number(final, _number(row.get("cash")) + _number(row.get("unrealized_pnl")))
        elif key == "trades":
            row = _object(row)
            symbol = row.get("symbol")
            if symbol not in expected_funding:
                raise EvidenceIssue("unverified", "账户交易品种超出冻结覆盖。")
            value = _number(row.get("net_return"))
            net_returns = _number(net_returns + value)
            pnl = _number(pnl + _number(row.get("net_pnl")))
            positive[symbol] = _number(positive.get(symbol, 0.) + max(0., value))
            trade_count += 1

    result, counts, result_sha = reader.replay(name, consume)
    binding = _object(result.get("validation_binding"))
    parameters = _object(result.get("parameters"))
    expected_parameters = dict(contract["execution"])
    expected_parameters["fee_rate"] *= multiplier
    expected_parameters["slippage_bps"] *= multiplier
    if (metrics["account_input_sha256"] != _fingerprint(binding)
            or metrics["account_result_sha256"] != result_sha
            or binding.get("contract_sha256") != sha or binding.get("exchange") != venue
            or binding.get("coverage") != coverage[venue]
            or binding.get("fee_multiplier") != multiplier or binding.get("slippage_multiplier") != multiplier
            or any(parameters.get(key) != value for key, value in expected_parameters.items())
            or result.get("status") != "RESEARCH_ONLY" or result.get("promotion_eligible") is not False
            or result.get("funding_status") != "COMPLETE" or counts.get("funding_incomplete_symbols") != 0
            or counts.get("open_positions") != 0 or parameters.get("finalize") is not True
            or parameters.get("strict_funding") is not True):
        raise EvidenceIssue("unverified", "账户回放输入/结果/完整成本合同绑定不一致。")
    for key in ("data_sha256", "signal_inputs_sha256", "events_sha256", "strategy_sha256"):
        if not SHA256.fullmatch(str(binding.get(key, ""))):
            raise EvidenceIssue("unverified", "账户回放冻结输入摘要缺失。")
    code = _object(binding.get("code_sha256"))
    if len(code) != 8 or any(not SHA256.fullmatch(str(value)) for value in code.values()):
        raise EvidenceIssue("unverified", "账户回放源码摘要缺失。")
    if binding.get("funding_sha256") != expected_funding:
        raise EvidenceIssue("unverified", "账户回放资金费源摘要未绑定。")
    start, end = _count(binding.get("start")), _count(binding.get("end"))
    if (not research_start <= start < end <= research_end
            or path.name != f"{venue}_{start}_{end}_{metrics['account_input_sha256'][:16]}.json"):
        raise EvidenceIssue("unverified", "账户回放时间窗口或文件身份不一致。")
    if "trades" not in counts or not counts.get("equity"):
        raise EvidenceIssue("unverified", "账户交易与盯市路径缺失。")
    if not start < first <= end or previous != end:
        raise EvidenceIssue("unverified", "账户盯市路径未覆盖到冻结末端。")
    _same_number(metrics["total_trades"], trade_count)
    _same_number(metrics["expectancy"], net_returns / trade_count if trade_count else 0.)
    _same_number(metrics["max_drawdown"], drawdown)
    _same_number(metrics["account_max_drawdown"], drawdown)
    _same_number(metrics["total_return"], final / initial - 1)
    _same_number(metrics["account_total_return"], final / initial - 1)
    _same_number(final - initial, pnl)
    metrics["_binding"], metrics["_positive_returns_by_symbol"] = binding, positive
    return metrics


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


def _candidates(comparison: list[dict[str, str]], folds: list[dict[str, str]], *, account_mode: bool = False) -> list[dict[str, Any]]:
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
        account_ready = _boolean(row.get("account_gate_ready")) if account_mode else True
        declared_gate = fdr and account_ready and item["delta"] > 0 and fold_ratio >= .70 and symbol_ratio > .50 and drawdown_ratio <= 1.10
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
    comparison, folds = reader.csv("candidate_comparison.csv"), reader.csv("fold_metrics.csv")
    account_mode = (manifest.get("account_verified") is True or "account_contract.json" in hashes
                    or _object(manifest.get("execution_metadata", {})).get("execution_scope") == ACCOUNT_SCOPE)
    item["candidates"] = _candidates(comparison, folds, account_mode=account_mode)
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
        protocol = None
        if account_mode:
            protocol = _account_protocol(reader, manifest, data_hashes)
            for row in folds:
                verified = _account_metrics(reader, row, protocol, "binance")
                if row.get("candidate") == "baseline" and verified["_binding"]["strategy_sha256"] != base_config:
                    raise EvidenceIssue("unverified", "基线账户策略摘要未绑定冻结配置。")
            item.update(account_verified=True, metric_basis="shared_cash_plus_bar_close_open_position_mtm")
            item["gates"].append(_gate("正式账户合同与回放", "pass", "合同、两交易所请求覆盖、资金费源摘要、回放哈希及账户盯市/交易指标一致；未认证外部来源或重跑行情。"))
        else:
            item["gates"].append(_gate("正式账户证据", "missing", "历史逐笔诊断未绑定共享账户合同/完整成本/盯市路径，不能确认候选通过。"))
        selected = summary.get("selected_candidate")
        if selected is None:
            if summary.get("status") != "baseline_retained" or any(row.get("validation_gate") for row in item["candidates"]):
                raise EvidenceIssue("unverified", "未选候选与验证期记录结论不一致。")
            item.update(status="baseline_retained", conclusion=("验证期未选出候选，保留基线；未解封最终测试集。" if account_mode
                else "历史诊断验证期未选出候选，保留基线；未验证共享账户风险或完整成本。"))
            item["gates"].append(_gate("最终测试联合门槛", "not_applicable", "没有候选进入永久留出测试。"))
        else:
            selected = _name(selected)
            candidate = next((row for row in item["candidates"] if row["name"] == selected and not row["is_baseline"]), None)
            if candidate is None or candidate.get("validation_gate") is not True:
                raise EvidenceIssue("unverified", "入选候选未绑定通过的验证期对照。")
            item["gates"].append(_gate("验证期 FDR 联合证据", "pass", f"入选 {selected}，q={candidate['q_value']:.6g}；验证期通过不等于最终测试通过。"))
            baseline, final, cost = (_object(summary.get(key)) for key in ("baseline", "candidate", "double_cost"))
            if not account_mode:
                if summary.get("status") != "baseline_retained":
                    raise EvidenceIssue("unverified", "旧候选通过记录缺少正式账户证据，保守拒绝晋级结论。")
                for record, keys in ((baseline, ("expectancy", "max_drawdown")),
                                     (final, ("total_trades", "expectancy", "max_drawdown")),
                                     (cost, ("expectancy",))):
                    for key in keys:
                        _number(record.get(key))
                item.update(status="baseline_retained", conclusion="历史诊断记录当时保留基线；未验证共享账户风险或完整成本，不构成当前正式验收。")
                item["gates"].append(_gate("最终测试联合门槛", "fail", "缺少正式账户证据；仅展示历史保留基线记录，不执行旧逐笔回撤替代门槛。"))
                if "report.html" in hashes:
                    _integrity(reader, ["report.html"])
                    item["report_url"] = f"/reports/{quote(item['id'])}/report.html"
                return
            baseline = _account_metrics(reader, baseline, protocol, "binance")
            final = _account_metrics(reader, final, protocol, "binance")
            cost = _account_metrics(reader, cost, protocol, "binance", 2)
            okx = _object(summary.get("okx"))
            if okx.get("skipped") is True:
                raise EvidenceIssue("unverified", "正式账户记录跳过跨交易所复核。")
            else:
                okx_base = _account_metrics(reader, _object(okx.get("baseline")), protocol, "okx")
                okx_final = _account_metrics(reader, _object(okx.get("candidate")), protocol, "okx")
                okx_baseline, okx_candidate = okx_base["expectancy"], okx_final["expectancy"]
                direction = okx_candidate >= okx_baseline and okx_candidate > 0
                item["gates"].append(_gate("OKX 方向复核", "pass" if direction else "fail", f"候选 {okx_candidate:.6g}，基线 {okx_baseline:.6g}。"))
            share = _number(summary.get("max_symbol_profit_share"))
            if not 0 <= share <= 1:
                raise EvidenceIssue("unverified", "单品种盈利集中度数值异常。")
            bindings = [record["_binding"] for record in (baseline, final, cost, okx_base, okx_final)]
            if (len({(binding["start"], binding["end"]) for binding in bindings}) != 1
                    or bindings[0]["strategy_sha256"] != base_config or bindings[3]["strategy_sha256"] != base_config
                    or len({bindings[index]["strategy_sha256"] for index in (1, 2, 4)}) != 1):
                raise EvidenceIssue("unverified", "最终账户比较未绑定相同留出窗口或候选策略。")
            positive = final["_positive_returns_by_symbol"]
            total = sum(positive.values())
            _same_number(share, max(positive.values(), default=0.) / total if total > 0 else 1.)
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
    replay_budget = [MAX_SCAN_REPLAY_BYTES]
    for name in names:
        optimization = name.startswith("strategy_optimization_")
        item: dict[str, Any] = {"id": name, "kind": "optimization_diagnostic" if optimization else "formal_validation",
                               "title": "优化诊断对照" if optimization else "正式策略验证",
                               "status": "incomplete", "conclusion": "证据不完整，未确认研究结论。",
                               "started_at": None, "ended_at": None, "interval": "未记录",
                               "return_model": "legacy_ratio_v1", "metric_basis": "per_trade_net_return",
                               "account_verified": False, "gates": [], "candidates": []}
        try:
            (_optimization if optimization else _formal)(item, _Reader(directory / name, replay_budget=replay_budget))
        except EvidenceIssue as issue:
            item.update(status=issue.status, conclusion=issue.detail + " 未确认研究验证通过。", candidates=[], account_verified=False)
            item.pop("report_url", None)
            item["gates"].append(_gate("证据校验", "missing" if issue.status == "incomplete" else "fail", issue.detail))
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            item.update(status="unverified", conclusion="摘要字段异常，未确认研究验证通过。", candidates=[], account_verified=False)
            item.pop("report_url", None)
            item["gates"].append(_gate("证据校验", "fail", "摘要字段异常。"))
        result["validations"].append(item)
    return result
