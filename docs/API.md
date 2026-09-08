# HTTP API

FastAPI also serves interactive OpenAPI documentation at `/docs` and the schema at `/openapi.json`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Web, Discord, reports, site probe, and research-only execution mode |
| `GET` | `/api/state` | Current monitoring dashboard snapshot |
| `GET` | `/api/events` | Server-Sent Events stream of dashboard snapshots |
| `GET` | `/api/site-monitor` | Internal endpoint probe state |
| `GET` | `/api/reports` | Hosted HTML report metadata |
| `GET` | `/api/watchlist` | Current monitored symbols and alert settings |
| `POST` | `/api/watchlist` | Validate and replace the watchlist |
| `POST` | `/api/network-test` | Test configured external market dependencies |
| `POST` | `/api/backtest` | Run a bounded strategy backtest summary |

Treat `/api/state` and the SSE payload as UI contracts rather than a versioned public API. Mutating endpoints return HTTP 4xx with a concrete validation message when input is invalid.

`/api/state` includes `signal_tracking.mode = "paper"`, `enabled`, `updated_at`, and
all active `positions` from the local strategy ledger. `strategy_trades` contains
the latest 20 observations and `strategy_stats` aggregates the retained ledger.
Each position exposes reference entry price, current price, stop, target, and
fee-adjusted `unrealized_pct`. Completed observations have `exit_price`,
`exit_reason`, `closed_at` (recording time), and, when a bar caused the exit,
`exit_kline_close_time` (bar time in milliseconds).

`/api/health` reports `execution_mode = "research_only"`. The old OKX robot
health/error routes and the `okx_bot_status` state field have been retired.
No API endpoint submits exchange orders.

## 本地五入口产品接口

- `GET /api/settings`：读取当前运行参数、完整策略快照和版本。
- `PUT /api/settings`：JSON `{values, expected_revision}`；保存并从下一次信号计算生效。无效值返回 400，旧版本覆盖返回 409。历史交易不重新套用新费率。
- `GET /api/paper-trades`：同一模拟账本的全部保留记录和统计，不包含交易所账户操作。
- `GET /api/research-runs`、`GET /api/research-runs/{run_id}`：读取本地实验列表、参数、数据区间和结果。
- `GET /api/backtest`：使用 Binance USDT 合约历史收盘 K 线，成功后自动保存实验；不会修改运行设置。
- `PUT /api/notifications/config`：JSON `{channel, webhook}`；按信号/系统/研究渠道保存本机 Webhook，空字符串停用。返回配置状态，不返回地址。
- `/api/binance-demo/status`：仅返回 `extension_disabled`，当前不需要交易所密钥。
