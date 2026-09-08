"""Feishu webhook delivery. Credentials stay server-side."""

import json
import os
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from product_settings import atomic_json

CHANNELS = {"signal": "信号与模拟跟踪", "system": "系统异常", "research": "研究报告"}
_lock = threading.Lock()
_results = {}


def config_path():
    return Path(
        os.getenv("FEISHU_CONFIG_FILE", str(Path(__file__).resolve().parent / "runtime" / "feishu-config.json"))
    )


def webhook(channel):
    path = config_path()
    config = json.loads(path.read_text()) if path.exists() else {}
    return config.get(channel, os.getenv(f"FEISHU_{channel.upper()}_WEBHOOK_URL", "").strip())


def valid_webhook(url):
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "open.feishu.cn"
        and parsed.path.startswith("/open-apis/bot/v2/hook/")
        and bool(parsed.path.removeprefix("/open-apis/bot/v2/hook/"))
        and not parsed.query
        and not parsed.fragment
    )


def save_webhook(channel, url):
    if channel not in CHANNELS or not isinstance(url, str):
        raise ValueError("通知渠道或地址无效")
    url = url.strip()
    if url and not valid_webhook(url):
        raise ValueError("请输入有效的飞书机器人 HTTPS 地址")
    with _lock:
        path = config_path()
        config = json.loads(path.read_text()) if path.exists() else {}
        config[channel] = url
        atomic_json(path, config, private=True)
        _results.pop(channel, None)


def status():
    with _lock:
        return {
            channel: {"label": label, "configured": bool(webhook(channel)), **_results.get(channel, {})}
            for channel, label in CHANNELS.items()
        }


def send(channel, title, body):
    if channel not in CHANNELS:
        return {"ok": False, "error": "未知通知渠道"}
    url = webhook(channel)
    result = {"ok": False, "sent_at": time.time()}
    if not valid_webhook(url):
        result["error"] = "飞书通知地址未配置或格式错误"
    else:
        payload = {"msg_type": "text", "content": {"text": f"炼气｜{title}\n{body}"[:15000]}}
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                reply = json.loads(response.read())
            code = reply.get("code", reply.get("StatusCode"))
            if code == 0:
                result["ok"] = True
            else:
                result["error"] = f"飞书拒绝消息，错误码 {code}"
        except Exception as exc:
            # Transport exceptions may include the secret URL: never expose them.
            result["error"] = f"飞书发送失败（{type(exc).__name__}）"
    with _lock:
        _results[channel] = dict(result)
    return result
