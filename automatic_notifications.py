"""Durable change detection and outbox for paper research notifications."""

import fcntl
import json
import os
import time
from pathlib import Path
from urllib.parse import quote


class SenderLease:
    """OS-held ownership prevents overlapping server processes from sending twice."""

    def __init__(self, path):
        lock_path = Path(str(path) + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = lock_path.open("a")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            raise RuntimeError("Another notification sender owns this queue") from None

    def close(self):
        self.handle.close()


class Outbox:
    def __init__(self, path):
        self.path = Path(path)
        self.data = (
            json.loads(self.path.read_text())
            if self.path.exists()
            else {
                "initialized": False,
                "trades": {},
                "reports": {},
                "fault": None,
                "pending": [],
                "last_delivery": None,
            }
        )

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            json.dump(self.data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    def enqueue(self, key, channel, title, body):
        base = os.getenv("FEISHU_SITE_URL", "http://localhost:3000/").rstrip("/")
        self.data["pending"].append(
            {
                "key": key,
                "channel": channel,
                "title": title,
                "body": f"{body}\n通知编号：{key}\n网站：{base}/",
                "attempts": 0,
                "next_attempt": 0,
            }
        )

    def observe(self, trades, reports, fault, alerts=None):
        if alerts is not None:
            # Upgrade existing queues by baselining retained history once.
            seen = self.data.get("alerts")
            if seen is None:
                self.data["alerts"] = {alert["key"]: True for alert in alerts}
            else:
                for alert in alerts:
                    if alert["key"] not in seen:
                        self.enqueue(f"alert:{alert['key']}", "signal", alert["title"], alert["body"])
                        seen[alert["key"]] = True
        current = {str(t["id"]): t.get("status") for t in trades if t.get("id")}
        report_versions = {r["path"]: r.get("updated_at") for r in reports}
        if not self.data["initialized"]:
            self.data.update(initialized=True, trades=current, reports=report_versions)
        else:
            for trade in trades:
                key = str(trade.get("id", ""))
                if not key:
                    continue
                old = self.data["trades"].get(key)
                demo = trade.get("execution_mode") == "binance_demo"
                info = f"{trade.get('symbol')} · {trade.get('interval', '')} · {trade.get('direction', '')}\n入场参考价：{trade.get('entry_price')}\n止损：{trade.get('initial_stop_loss', trade.get('stop_loss'))}\n目标：{trade.get('target_price')}"
                if demo:
                    info = f"Binance 官方合约模拟盘 · 策略版本：{trade.get('strategy_version')}\n{trade.get('symbol')} · {trade.get('direction')}\n信号参考价：{trade.get('reference_price')}\n模拟成交均价：{trade.get('entry_price')}\n成交数量：{trade.get('executed_qty')}\n初始止损：{trade.get('initial_stop_loss')}"
                if old is None:
                    self.enqueue(
                        f"{key}:open", "signal", "💵 Binance 模拟盘开仓成交" if demo else "💵 新开仓信号（模拟）", info
                    )
                if old != "closed" and trade.get("status") == "closed":
                    result = trade.get("return_pct")
                    gain = f"{result * 100:.2f}%" if isinstance(result, (int, float)) else "未知"
                    reason = trade.get("exit_reason")
                    prefix = ""
                    if reason in ("stop_loss", "protected_stop", "trailing_stop"):
                        prefix = "❌ "
                    elif reason in ("take_profit", "protection_reached"):
                        prefix = "✅ "
                    self.enqueue(
                        f"{key}:closed",
                        "signal",
                        prefix + ("Binance 模拟盘平仓" if demo else "模拟退出"),
                        f"{info}\n退出价：{trade.get('exit_price')}\n退出原因：{trade.get('exit_reason')}\n模拟收益：{gain}",
                    )
            for report in reports:
                path = report["path"]
                if report_versions[path] != self.data["reports"].get(path):
                    base = os.getenv("FEISHU_SITE_URL", "http://localhost:3000/").rstrip("/")
                    self.enqueue(
                        f"report:{path}:{report_versions[path]}",
                        "research",
                        "研究报告已生成或更新",
                        f"报告：{path}\n打开：{base}/reports/{quote(path, safe='/')}",
                    )
            # Keep IDs of retained history as well, so reappearing records do not resend.
            self.data["trades"].update(current)
            self.data["reports"].update(report_versions)
        if fault:
            self.data["recovery_since"] = None
            if fault != self.data["fault"]:
                self.enqueue(
                    f"system:{time.time_ns()}",
                    "system",
                    "监控异常",
                    f"异常模块：{fault}。请到网站设置与诊断页查看详情。",
                )
                self.data["fault"] = fault
        elif self.data["fault"]:
            if self.data.get("recovery_since") is None:
                self.data["recovery_since"] = time.time()
            elif time.time() - self.data["recovery_since"] >= 60:
                self.enqueue(f"system:{time.time_ns()}", "system", "监控恢复", "此前异常模块已持续恢复至少 60 秒。")
                self.data["fault"] = None
                self.data["recovery_since"] = None
        self.save()

    def deliver(self, sender, allowed_channels=None):
        # Bound work per scan and preserve unsuccessful messages for later attempts.
        blocked_channels = set()
        attempts = 0
        for message in list(self.data["pending"]):
            channel = message["channel"]
            if allowed_channels is not None and channel not in allowed_channels:
                continue
            if channel in blocked_channels:
                continue
            if message["next_attempt"] > time.time():
                blocked_channels.add(channel)
                continue
            if attempts >= 3:
                break
            attempts += 1
            result = sender(message["channel"], message["title"], message["body"])
            self.data["last_delivery"] = {
                "channel": message["channel"],
                "ok": result["ok"],
                "at": time.time(),
                "error": result.get("error"),
            }
            if result["ok"]:
                self.data["pending"].remove(message)
            else:
                blocked_channels.add(channel)
                message["attempts"] += 1
                message["next_attempt"] = time.time() + min(300, 15 * 2 ** min(message["attempts"], 5))
            self.save()
