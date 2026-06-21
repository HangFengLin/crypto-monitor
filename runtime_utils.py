"""Small shared runtime helpers for durable files and notifications."""

from __future__ import annotations

import json
import hashlib
import os
import threading
import urllib.request
from pathlib import Path
from typing import Any


_LOG_LOCKS: dict[str, threading.Lock] = {}
_LOG_LOCKS_GUARD = threading.Lock()


def _log_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _LOG_LOCKS_GUARD:
        return _LOG_LOCKS.setdefault(key, threading.Lock())


def atomic_write_text(path: Path, content: str) -> None:
    """Replace a UTF-8 text file atomically within its destination directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _rotate_file(path: Path, backups: int) -> None:
    if not path.exists():
        return
    if backups <= 0:
        path.unlink()
        return
    oldest = path.with_name(f"{path.name}.{backups}")
    if oldest.exists():
        oldest.unlink()
    for index in range(backups - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if source.exists():
            os.replace(source, path.with_name(f"{path.name}.{index + 1}"))
    os.replace(path, path.with_name(f"{path.name}.1"))


def append_jsonl(
    path: Path,
    payload: dict[str, Any],
    *,
    max_bytes: int | None = None,
    backups: int | None = None,
) -> None:
    """Append one compact JSON record and rotate bounded local history."""
    if max_bytes is None:
        max_bytes = int(os.getenv("EVENT_LOG_MAX_BYTES", str(10 * 1024 * 1024)))
    if backups is None:
        backups = int(os.getenv("EVENT_LOG_BACKUP_COUNT", "5"))
    max_bytes = max(0, max_bytes)
    backups = max(0, backups)
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    with _log_lock(path):
        current_size = path.stat().st_size if path.exists() else 0
        if max_bytes and current_size and current_size + len(line.encode("utf-8")) > max_bytes:
            _rotate_file(path, backups)
        with path.open("a", encoding="utf-8") as file:
            file.write(line)


def post_discord(webhook_url: str, content: str, timeout: int = 12) -> None:
    """Post a Discord webhook without coupling to project configuration."""
    payload = json.dumps({"content": content}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def code_fingerprint(root: Path, relative_paths: tuple[str, ...]) -> str:
    """Hash deployment-critical files into a short, comparable fingerprint."""
    digest = hashlib.sha256()
    for relative_path in relative_paths:
        path = root / relative_path
        if not path.exists():
            digest.update(f"{relative_path}:missing\n".encode("utf-8"))
            continue
        digest.update(f"{relative_path}:".encode("utf-8"))
        digest.update(path.read_bytes())
        digest.update(b"\n")
    return digest.hexdigest()[:16]
