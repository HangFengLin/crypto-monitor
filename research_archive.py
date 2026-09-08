"""Durable local research results; independent of the paper execution ledger."""

import json
import re
import time
import uuid
from pathlib import Path

from product_settings import atomic_json


class ResearchArchive:
    def __init__(self, path):
        self.path = Path(path)

    def save(self, result):
        doc = {**result, "run_id": uuid.uuid4().hex, "saved_at": time.time()}
        atomic_json(self.path / (doc["run_id"] + ".json"), doc)
        return doc

    def get(self, run_id):
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise ValueError("实验编号无效")
        return json.loads((self.path / (run_id + ".json")).read_text())

    def list(self):
        results = []
        for path in sorted(self.path.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                doc = json.loads(path.read_text())
                results.append(
                    {
                        key: doc.get(key)
                        for key in (
                            "run_id",
                            "saved_at",
                            "ok",
                            "symbol",
                            "interval",
                            "limit",
                            "params",
                            "metrics",
                            "config_revision",
                            "market_data_source",
                            "started_at",
                            "ended_at",
                        )
                    }
                )
            except (ValueError, OSError):
                results.append({"run_id": path.stem, "ok": False, "error": "归档文件不可读"})
        return results
