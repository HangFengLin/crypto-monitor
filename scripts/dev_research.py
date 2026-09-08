"""Start the local research API; keep local experiments separate from saved ledgers."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / "runtime" / "local-research"
LOCAL.mkdir(parents=True, exist_ok=True)
os.environ.update(
    {
        "MARKET_DATA_SOURCE": "binance_usdm",
        "HOST": "127.0.0.1",
        "PORT": "8080",
        "DISCORD_ENABLED": "false",
        "FEISHU_TEST_ENABLED": "true",
        "FEISHU_AUTO_ENABLED": os.getenv("FEISHU_AUTO_ENABLED", "false"),
        "FEISHU_CONFIG_FILE": str(LOCAL / "feishu-config.json"),
        "RUNTIME_SETTINGS_FILE": str(LOCAL / "runtime-settings.json"),
        "FEISHU_OUTBOX_FILE": str(LOCAL / "feishu-outbox.json"),
        "WATCHLIST_FILE": str(LOCAL / "watchlist.json"),
        "EVENT_LOG_FILE": str(LOCAL / "signal_events.jsonl"),
        "STRATEGY_TRADES_FILE": str(LOCAL / "strategy_trades.json"),
    }
)
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    import uvicorn

    import app

    uvicorn.run(app.create_app(), host="127.0.0.1", port=8080, timeout_graceful_shutdown=5)
