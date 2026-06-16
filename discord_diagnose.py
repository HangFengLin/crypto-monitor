#!/usr/bin/env python3
"""Diagnose Discord webhook loading and delivery for this deployment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from config import ENV_FILE, load_env_file


ROOT = Path(__file__).resolve().parent


def code_fingerprint() -> str:
    digest = hashlib.sha256()
    for relative_path in (
        "app.py",
        "config.py",
        "docker-compose.yml",
        "okx_market_cap_bot.py",
        "binance_strategy_bot.py",
    ):
        path = ROOT / relative_path
        if not path.exists():
            digest.update(f"{relative_path}:missing\n".encode("utf-8"))
            continue
        digest.update(f"{relative_path}:".encode("utf-8"))
        digest.update(path.read_bytes())
        digest.update(b"\n")
    return digest.hexdigest()[:16]


def masked(value: str) -> str:
    if len(value) <= 16:
        return "*" * len(value)
    return f"{value[:8]}...{value[-8:]}"


def env_file_has_key(key: str) -> bool:
    if not ENV_FILE.exists():
        return False
    try:
        for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            env_key = line.split("=", 1)[0].strip()
            if env_key.startswith("export "):
                env_key = env_key.removeprefix("export ").strip()
            if env_key == key:
                return True
    except OSError:
        return False
    return False


def post_discord(webhook_url: str, content: str) -> dict[str, Any]:
    payload = json.dumps({"content": content[:2000]}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Chanlun-Crypto-Monitor-Diagnose/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read(500).decode("utf-8", errors="replace")
            return {"ok": response.status < 400, "status": response.status, "body": body}
    except urllib.error.HTTPError as exc:
        body = exc.read(500).decode("utf-8", errors="replace")
        return {"ok": False, "status": exc.code, "error": body or str(exc)}
    except (OSError, urllib.error.URLError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test Discord webhook loading and delivery.")
    parser.add_argument("--no-send", action="store_true", help="Only inspect local config; do not send a Discord message.")
    parser.add_argument("--content", default="", help="Extra text to include in the diagnostic Discord message.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env_file()
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    fingerprint = code_fingerprint()

    result: dict[str, Any] = {
        "ok": False,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "cwd": str(ROOT),
        "env_file": str(ENV_FILE),
        "env_file_exists": ENV_FILE.exists(),
        "env_file_has_discord_key": env_file_has_key("DISCORD_WEBHOOK_URL"),
        "discord_value_length": len(webhook_url),
        "discord_value_masked": masked(webhook_url) if webhook_url else "",
        "code_fingerprint": fingerprint,
        "sent": False,
    }

    if not webhook_url:
        result["error"] = "DISCORD_WEBHOOK_URL is empty after loading .env"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2

    if args.no_send:
        result["ok"] = True
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    timestamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    content = (
        f"Discord diagnostic test\n"
        f"host={socket.gethostname()} pid={os.getpid()} code={fingerprint} time={timestamp}"
    )
    if args.content:
        content = f"{content}\n{args.content}"

    delivery = post_discord(webhook_url, content)
    result["sent"] = delivery.get("ok", False)
    result["delivery"] = delivery
    result["ok"] = bool(delivery.get("ok"))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
