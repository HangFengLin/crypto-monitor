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
| `GET` | `/api/backtest` | Run a bounded strategy backtest summary |
| `GET` | `/api/research-evidence` | Read experiment comparisons and validation evidence; never runs experiments |

Treat `/api/state` and the SSE payload as UI contracts rather than a versioned public API. Mutating endpoints return HTTP 4xx with a concrete validation message when input is invalid.

## VPS website transport

The VPS serves the built website from `public/site`; its source is
`sites/lianqi/vps/main.tsx` and the shared `sites/lianqi/app` components. Build it
with `pnpm run build:vps` in `sites/lianqi`. A local build does not update the VPS.

Bulk JSON and static responses negotiate gzip. The `/api/events` stream bypasses
compression so the pinned server middleware cannot buffer live frames. Clients
that reject gzip continue receiving plain responses.

`GET /api/state`, `/api/paper-trades`, and `/api/events` accept
`detail=compact|full`; omission selects `full` for existing clients. The VPS
website requests `compact`, which omits repeated record-level runtime
configuration and calculation traces. Prices, errors, accounting versions,
historical universe identity, exits, unknown fields, zeroes and nulls remain.
`detail=full` retains complete information; neither view changes persisted data.
Invalid detail values return HTTP 422.

Each SSE connection immediately receives the current snapshot, checks for
changes every three seconds, and sends a data frame only when its selected view
changes. Idle connections receive a comment heartbeat every 15 seconds. A new
connection receives a fresh snapshot even if the previous connection sent it.

Clients may opt into `/api/events?detail=compact&delta=1`. Its first data frame
is a `lianqi-delta-v1` snapshot with sequence zero; later frames replace the
listed top-level fields and remove keys named in `remove`. Null, zero and empty
values are ordinary replacements. Sequence numbers increase by one, and any
new connection starts with a fresh snapshot. A client that cannot apply a frame
in sequence must reconnect instead of guessing. Without `delta=1`, the existing
complete-snapshot contract is unchanged.

While SSE is open the dashboard pauses background state polling; a disconnect
resumes it. If the browser permanently closes the stream after an HTTP error,
the dashboard retries a fresh connection after five seconds; native reconnects
remain in charge while the connection is still retryable. Initial and manual refreshes remain available. Ledger, notification
and rules refreshes wait for completion before scheduling another request, and
unmounting aborts pending requests. Finite API deadlines cover the complete
response download, including time after response headers arrive.

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
- `GET /api/research-evidence`：只读聚合正式验证与离线优化诊断。返回中文结论、同实验候选对照、收益版本与门槛证据；摘要哈希不匹配或证据缺失时降级。所有结果保持 `RESEARCH_ONLY`，工程勾稽通过不代表策略有效。接口不触发计算、不修改设置，也不返回原始配置。
- `GET /api/backtest`：使用 Binance USDT 合约历史收盘 K 线，成功后自动保存实验；不会修改运行设置。
- `PUT /api/notifications/config`：JSON `{channel, webhook}`；按信号/系统/研究渠道保存本机 Webhook，空字符串停用。返回配置状态，不返回地址。
- `/api/binance-demo/status`：仅返回 `extension_disabled`，当前不需要交易所密钥。
