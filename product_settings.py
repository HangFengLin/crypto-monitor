"""Validated local runtime settings with immutable revision snapshots."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import threading
import time
from dataclasses import asdict
from pathlib import Path

from config import load_config
from strategy import DEFAULT_CONFIG

ROOT = Path(__file__).resolve().parent
_LOCK = threading.RLock()
_BASE = load_config()
_STRATEGY = asdict(DEFAULT_CONFIG)
ENGINE_SOURCE_FILES = (
    "strategy.py", "position_manager.py", "indicators.py", "app.py", "data_client.py",
    "product_settings.py", "signal_engine_state.py",
    "project_signal_backtest.py", "backtest_statistics.py", "historical_market_data.py",
    "config.py", "runtime_utils.py",
)
ENGINE_VERSION = hashlib.sha256(
    b"".join(name.encode()+b"\0"+((ROOT/name).read_bytes() if (ROOT/name).exists() else b"<absent>")+b"\0"
             for name in ENGINE_SOURCE_FILES)
).hexdigest()[:12]
_EDITABLE = (
    "min_divergence_strength",
    "confirmation_mode",
    "require_higher_trend_alignment",
    "atr_stop_multiplier",
    "max_chop",
)


def default_values():
    return {
        "paper": {
            "enabled": bool(_BASE.get("app", {}).get("record_strategy_trades", True)),
            "reward_risk": _BASE.get("app", {}).get("strategy_reward_risk", 2.0),
            "fee_rate": _BASE.get("app", {}).get("strategy_fee_rate", 0.001),
            "stop_mode": _BASE.get("app", {}).get("strategy_stop_mode", "structure_atr"),
            "signal_direction": "all",
            "min_signal_score": 0,
        },
        "strategy": {key: _STRATEGY[key] for key in _EDITABLE},
        "notifications": {
            "enabled": os.getenv("FEISHU_AUTO_ENABLED", "false").lower() in {"true", "1", "yes"},
            "signal": True,
            "system": True,
            "research": True,
        },
    }


def validate(values):
    defaults = default_values()
    if not isinstance(values, dict) or values.keys() != defaults.keys():
        raise ValueError("设置分组不完整或包含未知字段")
    for group in defaults:
        if not isinstance(values[group], dict) or values[group].keys() != defaults[group].keys():
            raise ValueError(f"{group} 设置字段不完整或包含未知字段")
    for group, key in [
        ("paper", "enabled"),
        ("strategy", "require_higher_trend_alignment"),
        *[("notifications", k) for k in defaults["notifications"]],
    ]:
        if not isinstance(values[group][key], bool):
            raise ValueError(f"{key} 必须为开关值")
    bounds = [
        ("paper", "reward_risk", 0.3, 5),
        ("paper", "fee_rate", 0, 0.02),
        ("paper", "min_signal_score", 0, 20),
        ("strategy", "min_divergence_strength", 0, 10),
        ("strategy", "atr_stop_multiplier", 0.1, 10),
        ("strategy", "max_chop", 0, 100),
    ]
    for group, key, lo, hi in bounds:
        value = values[group][key]
        if (
            (not isinstance(value, (int, float)) or isinstance(value, bool))
            or not math.isfinite(value)
            or not lo <= value <= hi
        ):
            raise ValueError(f"{key} 必须在 {lo} 到 {hi} 之间")
    if not isinstance(values["paper"]["min_signal_score"], int) or isinstance(
        values["paper"]["min_signal_score"], bool
    ):
        raise ValueError("最低信号评分必须为整数")
    for group, key, allowed in [
        ("paper", "stop_mode", {"structure_atr", "atr_trailing_after_1r"}),
        ("paper", "signal_direction", {"all", "long", "short"}),
        ("strategy", "confirmation_mode", {"either", "both"}),
    ]:
        if values[group][key] not in allowed:
            raise ValueError(f"{key} 选项无效")
    return copy.deepcopy(values)


def atomic_json(path, data, *, private=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)
    if private:
        path.chmod(0o600)


class SettingsConflict(ValueError):
    pass


def _effective_for_execution(document):
    """Validate a signed historical configuration before applying current defaults."""
    values = validate(document["values"])
    effective = document["effective_strategy"]
    if not isinstance(effective, dict) or set(effective) - set(_STRATEGY):
        raise ValueError("配置包含当前引擎不支持的策略字段")
    for name, value in effective.items():
        template = _STRATEGY[name]
        if isinstance(template, bool):
            valid = isinstance(value, bool)
        elif isinstance(template, int):
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif isinstance(template, float):
            valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        else:
            valid = isinstance(value, type(template))
        if not valid:
            raise ValueError(f"策略字段 {name} 类型不兼容")
    for name, value in values["strategy"].items():
        if effective.get(name) != value:
            raise ValueError("生效策略与已保存参数不一致")
    return {**_STRATEGY, **effective}, sorted(set(_STRATEGY) - set(effective))


def execution_settings(document):
    """Bind next execution to current source while retaining immutable config identity."""
    source = copy.deepcopy(document.get("configuration_snapshot", document))
    try:
        payload = {key:source[key] for key in ("values", "effective_strategy", "engine_version")}
        digest = hashlib.sha256(json.dumps(payload,sort_keys=True,allow_nan=False).encode()).hexdigest()[:16]
        if source["revision"] != digest:
            raise ValueError("配置快照与版本不匹配")
        effective, added = _effective_for_execution(source)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("运行配置与当前执行引擎不兼容") from exc
    binding = {"config_revision":source["revision"],"engine_version":ENGINE_VERSION,"effective_strategy":effective}
    execution_revision = hashlib.sha256(json.dumps(binding,sort_keys=True,allow_nan=False).encode()).hexdigest()[:16]
    return dict(source, effective_strategy=effective, engine_version=ENGINE_VERSION,
                config_revision=source["revision"], config_engine_version=source["engine_version"],
                execution_revision=execution_revision, strategy_defaults_applied=added,
                engine_source_files=list(ENGINE_SOURCE_FILES), configuration_snapshot=source)


class SettingsStore:
    def __init__(self, path):
        self.path = Path(path)
        self.defaults = default_values()

    def document(self, values, saved_at=None):
        effective = {**_STRATEGY, **values["strategy"]}
        payload = {"values": values, "effective_strategy": effective, "engine_version": ENGINE_VERSION}
        revision = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()[:16]
        return dict(
            payload,
            revision=revision,
            saved_at=saved_at,
            mode="paper",
            market_data_source="binance_usdm",
            applies_to="下一次信号计算；已有模拟交易保留开仓时规则",
        )

    def get(self):
        with _LOCK:
            if not self.path.exists():
                return self.document(copy.deepcopy(self.defaults))
            try:
                doc = json.loads(self.path.read_text())
                validate(doc["values"])
                if not re.fullmatch(r"[0-9a-f]{16}", doc["revision"]) or not isinstance(
                    doc["effective_strategy"], dict
                ):
                    raise ValueError("Invalid revision")
                payload = {key: doc[key] for key in ("values", "effective_strategy", "engine_version")}
                digest = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()[:16]
                if digest != doc["revision"]:
                    raise ValueError("配置快照与版本不匹配")
                _effective_for_execution(doc)
                return doc
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("运行设置损坏，已停止采用新配置；请恢复有效版本") from exc

    def save(self, values, expected_revision):
        values = validate(values)
        with _LOCK:
            current = self.get()
            if expected_revision != current["revision"]:
                raise SettingsConflict("设置已被修改，请重新读取后再保存")
            doc = self.document(values, time.time())
            history = self.path.parent / "settings-versions"
            atomic_json(history / (current["revision"] + ".json"), current)
            atomic_json(history / (doc["revision"] + ".json"), doc)
            atomic_json(self.path, doc)
            return doc

    def version(self, revision):
        if not re.fullmatch(r"[0-9a-f]{16}", revision):
            raise ValueError("版本编号无效")
        path = self.path.parent / "settings-versions" / (revision + ".json")
        if path.exists():
            return json.loads(path.read_text())
        current = self.get()
        if current["revision"] == revision:
            return current
        raise FileNotFoundError("配置版本不存在")
