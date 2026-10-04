"""Auditable Binance paper-entry universe; no exchange order transport."""

import copy
import hashlib
import json
import threading
import time
from pathlib import Path

from product_settings import atomic_json


class PaperUniverse:
    def __init__(self, path, top_n=100, selection="exchange_top_n", refresh_seconds=21600):
        self.path = Path(path)
        self.top_n = top_n
        self.selection = selection
        self.refresh_seconds = refresh_seconds
        self.lock = threading.RLock()
        self.data = {}
        self.error = None
        self.next_attempt = 0
        try:
            saved = json.loads(self.path.read_text()) if self.path.exists() else {}
            if saved.get("top_n") == top_n and saved.get("selection") == selection:
                self.data = saved
        except (OSError, ValueError):
            self.error = "币池快照读取失败"

    def status(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            data = copy.deepcopy(self.data)
            age = now - data.get("updated_at", 0)
            ready = bool(data.get("members")) and 0 <= age < self.refresh_seconds and not self.error
            return {**data, "top_n": self.top_n, "selection": self.selection,
                    "source": "CoinGecko market cap + Binance USD-M perpetuals",
                    "ready": ready, "error": self.error, "members": data.get("members", []),
                    "refresh_seconds": self.refresh_seconds}

    def refresh(self, loader, now=None):
        now = time.time() if now is None else now
        with self.lock:
            if self.status(now)["ready"] or now < self.next_attempt:
                return
            self.next_attempt = now + 300
        try:
            members = loader()
            if not members or (self.selection == "exchange_top_n" and len(members) != self.top_n):
                raise ValueError("币池数量不符合筛选规则")
            data = {"top_n": self.top_n, "selection": self.selection, "updated_at": now, "members": members}
            data["snapshot_id"] = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]
            atomic_json(self.path.parent / (self.path.stem + "-snapshots") / (data["snapshot_id"] + ".json"), data)
            atomic_json(self.path, data)
            with self.lock:
                self.data = data
                self.error = None
        except Exception as exc:
            with self.lock:
                self.error = f"币池刷新失败（{type(exc).__name__}），暂停新开仓"
