from __future__ import annotations

import os
from pathlib import Path
from typing import Any


CONFIG_FILE = Path(__file__).resolve().parent / "config.yaml"
ENV_FILE = Path(__file__).resolve().parent / ".env"
PROXY_ENV_KEYS = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}


def sync_proxy_env_aliases() -> None:
    for upper_key in PROXY_ENV_KEYS:
        lower_key = upper_key.lower()
        if upper_key in os.environ:
            os.environ.setdefault(lower_key, os.environ[upper_key])
        elif lower_key in os.environ:
            os.environ.setdefault(upper_key, os.environ[lower_key])


def load_env_file(path: Path = ENV_FILE) -> None:
    sync_proxy_env_aliases()
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key.removeprefix("export ").strip()
        if not key:
            continue
        value = value.strip().strip('"').strip("'")
        if value and not os.environ.get(key, "").strip():
            os.environ[key] = value
        env_value = os.environ.get(key, value)

        upper_key = key.upper()
        lower_key = key.lower()
        if upper_key in PROXY_ENV_KEYS:
            os.environ.setdefault(upper_key, env_value)
            os.environ.setdefault(lower_key, env_value)

    sync_proxy_env_aliases()


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def load_config(path: Path = CONFIG_FILE) -> dict[str, Any]:
    if not path.exists():
        return {}

    config: dict[str, Any] = {}
    section: dict[str, Any] | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith(" ") and line.endswith(":"):
            section = {}
            config[line[:-1].strip()] = section
            continue
        if section is not None and line.startswith("  ") and ":" in line:
            key, value = line.strip().split(":", 1)
            section[key.strip()] = _parse_scalar(value)
    return config


def config_value(config: dict[str, Any], section: str, key: str, default: Any) -> Any:
    section_data = config.get(section, {})
    if not isinstance(section_data, dict):
        return default
    return section_data.get(key, default)


load_env_file()
