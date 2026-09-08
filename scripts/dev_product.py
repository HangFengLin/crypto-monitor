#!/usr/bin/env python3
"""Start the original local website and its paper research API together."""

import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    for port in (8080, 3000):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                raise SystemExit(f"端口 {port} 已占用，请先核对已有服务；未启动第二套实例。")
    node = shutil.which("node")
    if not node:
        bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
        if bundled.exists():
            node = str(bundled)
    if not node:
        raise SystemExit("需要 Node.js 22.13 或更新版本")
    python = ROOT / ".venv/bin/python"
    if not python.exists():
        raise SystemExit("请先建立 .venv 并安装 requirements.txt")
    vinext = ROOT / "sites/lianqi/node_modules/vinext/dist/cli.js"
    if not vinext.exists():
        candidates = list((ROOT / "sites/lianqi/node_modules/vinext").glob("**/vinext.mjs"))
        if candidates:
            vinext = candidates[0]
    if not vinext.exists():
        raise SystemExit("请先在 sites/lianqi 安装依赖")
    env = dict(
        os.environ,
        PATH=str(Path(node).parent) + os.pathsep + os.environ.get("PATH", ""),
        LIANQI_ORIGIN_URL="http://127.0.0.1:8080",
    )
    children = []

    def stop(*_):
        for process in children:
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        children.append(subprocess.Popen([str(python), "scripts/dev_research.py"], cwd=ROOT, env=env))
        for _ in range(60):
            if children[0].poll() is not None:
                raise RuntimeError("本地接口启动失败")
            try:
                with urllib.request.urlopen("http://127.0.0.1:8080/api/health", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError("本地接口未在限定时间内就绪")
        children.append(
            subprocess.Popen(
                [node, str(vinext), "dev", "--host", "127.0.0.1", "--port", "3000"], cwd=ROOT / "sites/lianqi", env=env
            )
        )
        print("原网站：http://localhost:3000 · 本地接口：http://127.0.0.1:8080", flush=True)
        while all(p.poll() is None for p in children):
            time.sleep(0.5)
    finally:
        stop()
        for process in children:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
