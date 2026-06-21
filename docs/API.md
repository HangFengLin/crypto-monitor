# HTTP API

FastAPI also serves interactive OpenAPI documentation at `/docs` and the schema at `/openapi.json`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Web, Discord, reports, site probe, and summarized OKX bot health |
| `GET` | `/api/state` | Current monitoring dashboard snapshot |
| `GET` | `/api/events` | Server-Sent Events stream of dashboard snapshots |
| `GET` | `/api/okx-bot/health` | Detailed OKX bot freshness, positions, and current error |
| `GET` | `/api/okx-bot/errors?limit=20` | Recent structured OKX error events |
| `GET` | `/api/site-monitor` | Internal endpoint probe state |
| `GET` | `/api/reports` | Hosted HTML report metadata |
| `GET` | `/api/watchlist` | Current monitored symbols and alert settings |
| `POST` | `/api/watchlist` | Validate and replace the watchlist |
| `POST` | `/api/network-test` | Test configured external market dependencies |
| `POST` | `/api/backtest` | Run a bounded strategy backtest summary |

Treat `/api/state` and the SSE payload as UI contracts rather than a versioned public API. Mutating endpoints return HTTP 4xx with a concrete validation message when input is invalid.
