"""Exercise the real image entrypoint in a credential-free, network-none container."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        source = dict(attrs).get("src", "")
        if tag == "script" and source.startswith("/site/assets/"):
            self.scripts.append(source)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    interfaces = Path("/sys/class/net")
    if not interfaces.is_dir() or {p.name for p in interfaces.iterdir()} != {"lo"}:
        raise RuntimeError("Run this check inside docker run --network none")
    if os.geteuid() == 0 or (root / ".env").exists():
        raise RuntimeError("Image must run as a non-root user and contain no .env")
    if not (root / "public/site/index.html").is_file():
        raise RuntimeError("Built frontend is missing from image")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="lianqi-image-smoke-") as directory:
        temporary = Path(directory)
        watchlist = temporary / "watchlist.json"
        watchlist.write_text("[]\n")
        environment = {
            "PATH": os.defpath,
            "HOME": directory,
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PAPER_UNIVERSE_ENABLED": "false",
            "DISCORD_ENABLED": "false",
            "FEISHU_AUTO_ENABLED": "false",
            "FEISHU_TEST_ENABLED": "false",
            "SITE_MONITOR_ENABLED": "false",
            "WATCHLIST_FILE": str(watchlist),
            "STRATEGY_TRADES_FILE": str(temporary / "trades.json"),
            "RUNTIME_SETTINGS_FILE": str(temporary / "settings.json"),
            "FEISHU_OUTBOX_FILE": str(temporary / "outbox.json"),
            "REPORTS_DIR": str(temporary / "reports"),
        }
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with (temporary / "server.log").open("w+") as log:
            process = subprocess.Popen(
                [sys.executable, "app.py"], cwd=root, env=environment,
                stdout=log, stderr=subprocess.STDOUT,
            )
            checks = {}
            try:
                deadline = time.monotonic() + 30
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("Image entrypoint stopped before becoming ready")
                    try:
                        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
                            health = json.load(response)
                        break
                    except (OSError, urllib.error.URLError):
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Image entrypoint did not become ready") from None
                        time.sleep(0.1)
                if health.get("ok") is not True or health.get("discord_configured") is not False:
                    raise RuntimeError("Unexpected health or notification state")
                checks["health"] = True
                for route in ("/api/state", "/api/settings"):
                    with opener.open(f"http://127.0.0.1:{port}{route}", timeout=3) as response:
                        payload = json.load(response)
                        if not isinstance(payload, dict):
                            raise RuntimeError("Invalid API response: " + route)
                        if route == "/api/settings" and payload["values"]["notifications"]["enabled"] is not False:
                            raise RuntimeError("External notifications must be disabled")
                    checks[route] = True
                with opener.open(f"http://127.0.0.1:{port}/", timeout=3) as response:
                    body = response.read()
                if body != (root / "public/site/index.html").read_bytes():
                    raise RuntimeError("Served frontend differs from built index")
                assets = Assets()
                assets.feed(body.decode("utf-8"))
                if not assets.scripts:
                    raise RuntimeError("Built frontend references no packaged JavaScript")
                for asset in assets.scripts:
                    local = (root / "public" / asset.lstrip("/")).resolve()
                    if not local.is_relative_to(root / "public/site/assets"):
                        raise RuntimeError("Invalid frontend asset path")
                    with opener.open(f"http://127.0.0.1:{port}{asset}", timeout=3) as response:
                        if response.read() != local.read_bytes() or local.stat().st_size == 0:
                            raise RuntimeError("Packaged frontend JavaScript was not served")
                checks["frontend"] = True
                checks["packaged_javascript_assets"] = len(assets.scripts)
            except Exception:
                log.seek(0)
                sys.stderr.write(log.read()[-8000:])
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise RuntimeError("Image entrypoint failed to stop") from None
        if process.returncode not in (0, -15):
            raise RuntimeError("Image entrypoint failed during shutdown")
        print(json.dumps({
            "status": "PASS", "uid": os.geteuid(), "network": "loopback_only",
            "entrypoint": [sys.executable, "app.py"], "checks": checks,
            "external_notifications_enabled": False, "production_credentials_used": False,
            "server_exit_code": process.returncode,
        }, sort_keys=True))


if __name__ == "__main__":
    main()
