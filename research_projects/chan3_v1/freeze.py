"""Freeze the CHAN3_V1 protocol/code/data hashes before the single run."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
TARGETS = (
    "PROJECT.md",
    "protocol.json",
    "chan3.py",
    "test_chan3.py",
    "prepare_data.py",
    "run_study.py",
    "data_manifest.json",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=HERE, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return "unknown"


def main() -> int:
    if (RESULTS / "summary.json").exists():
        raise SystemExit("results/summary.json exists; refusing to re-freeze after results")
    missing = [name for name in TARGETS if not (HERE / name).exists()]
    if missing:
        raise SystemExit(f"missing freeze targets: {missing}")
    protocol = json.loads((HERE / "protocol.json").read_text(encoding="utf-8"))
    if protocol.get("status") != "FROZEN_BEFORE_RESULTS":
        raise SystemExit("protocol status is not FROZEN_BEFORE_RESULTS")
    payload = {
        "project": "CHAN3_V1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(),
        "python": sys.version,
        "platform": platform.platform(),
        "protocol_status": protocol["status"],
        "one_shot": protocol["one_shot"],
        "hashes": {name: sha256(HERE / name) for name in TARGETS},
    }
    (HERE / "FREEZE.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("frozen:", ", ".join(f"{name}={payload['hashes'][name][:12]}" for name in TARGETS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
