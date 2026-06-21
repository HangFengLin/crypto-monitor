# 加密货币信号监控与策略机器人

项目分成两部分：

1. 信号监控网站：只监控 `BTCUSDT`、`ETHUSDT`，用于看盘、支撑位、指标与项目信号提醒。
2. 策略交易机器人：独立扫描交易所可交易且满足流动性门槛的市值排名候选币种，继续使用项目里同一套 `ProjectSignalEngine` 策略。

## 15 分钟快速上手

只启动网站（不会下单）：

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

然后打开 `http://127.0.0.1:8080`，并用 `curl http://127.0.0.1:8080/api/health` 确认服务状态。需要理解完整组件关系、机器人选型和状态边界时，阅读 [架构与入口指南](docs/ARCHITECTURE.md)；接口清单见 [HTTP API](docs/API.md)，FastAPI 运行后也可直接打开 `/docs`。

## 策略研究与抗过拟合验证

参数研究使用独立的 `strategy_validation.py`，复用生产信号与持仓生命周期，但不会改写
`config.yaml`、下单或部署。它提供两年历史数据缓存、固定品种快照、walk-forward、
最终 20% 留出集、周分块 bootstrap、BH-FDR、成本压力和 OKX 跨市场复核。

一周 BTC 网络冒烟：

```bash
python3 strategy_validation.py --smoke --end 2024-01-08 --skip-okx --bootstrap-iterations 200
```

完整流程、候选覆盖文件和报告字段见 [抗过拟合策略验证](docs/STRATEGY_VALIDATION.md)。
官方数据限流或归档失败时，可用 `resume_strategy_validation.py --report-dir <报告目录>`
补齐缺失品种并重建统计，无需重跑已经完成的全部候选回放。

## 信号监控网站

网站默认只保留 `BTCUSDT`、`ETHUSDT`。监控列表会保存到 `watchlist.json`，但它只代表网站监控范围，不代表策略机器人交易池。

```bash
python3 app.py
```

电脑本机打开：

```text
http://127.0.0.1:8080
```

不要直接双击 `public/index.html` 作为日常使用入口。页面在 `file://` 模式下会尝试自动寻找本机 HTTP 服务，默认会扫描 `8080-8085`、`8000`、`5000` 和 Docker 常用的 `80` 端口；如果找不到，请以终端启动日志打印的地址为准。

手机和电脑连同一个 Wi-Fi 时，启动日志会打印类似下面的地址，直接在手机浏览器地址栏输入即可：

```text
Monitoring dashboard: http://192.168.1.23:8080
```

注意：浏览器只能访问已经运行的服务，不能凭空启动电脑上的 Python 进程。要让手机随时打开，需要让服务持续运行在电脑或云服务器上。

## 云服务器部署

安装依赖：

```bash
python3 -m pip install -r requirements.txt
```

以前台方式启动测试：

```bash
HOST=0.0.0.0 PORT=8080 python3 app.py
```

生产环境建议用 systemd 常驻运行，并用 Nginx/Caddy 做反向代理和 HTTPS。示例 systemd 服务：

```ini
[Unit]
Description=Chanlun Crypto Monitor
After=network.target

[Service]
WorkingDirectory=/opt/chanlun-monitor
ExecStart=/usr/bin/env HOST=127.0.0.1 PORT=8080 python3 app.py
Restart=always
RestartSec=5
Environment=DISCORD_WEBHOOK_URL=your_discord_webhook_url

[Install]
WantedBy=multi-user.target
```

如果直接暴露端口，防火墙需要放行 `8080`；如果使用 Nginx/Caddy，应用保持监听 `127.0.0.1:8080`，只开放 `80/443`。

项目也提供 Docker 部署方式，`docker-compose.yml` 已经把服务器 `80` 端口映射到应用 `8080`。默认只启动监控网站，不会启动带下单能力的 OKX 机器人：

```bash
docker compose up -d --build
```

当前 Docker Compose 默认启动：

- `crypto-project`：监控网站，只负责 BTCUSDT、ETHUSDT 面板和提醒。

OKX Demo 市值排名策略机器人被放在 `trading` profile 中，只有显式启用才会运行。这个 profile 默认仍是 signal-only，不会下单：

```bash
docker compose --profile trading up -d --build
```

如果只想单独启动或重建机器人：

```bash
docker compose --profile trading up -d --build okx-strategy-bot
```

云服务器上的 `.env` 如果只运行监控网站，至少需要：

```env
DISCORD_WEBHOOK_URL=your_discord_webhook_url
```

只有准备启用 OKX Demo 下单时，才需要配置凭证：

```env
OKX_API_KEY=your_okx_demo_api_key
OKX_SECRET_KEY=your_okx_demo_secret_key
OKX_PASSPHRASE=your_okx_demo_passphrase
```

下单模式必须显式叠加 order override；只写 `--profile trading` 不够：

```bash
docker compose -f docker-compose.yml -f docker-compose.order.yml --profile trading up -d --build okx-strategy-bot
```

如果 `OKX_API_KEY`、`OKX_SECRET_KEY` 或 `OKX_PASSPHRASE` 缺失，机器人会拒绝启动下单模式并写入 `startup_error`。启动时还会把本地未平仓状态与 OKX Demo 持仓逐项对账；标的、方向或数量不一致时写入 `reconcile_error` 并拒绝下单，避免重复开仓或失去仓位追踪。

机器人每轮扫描会先写入 `scan_started`，完成后写入 `scan`。默认单轮扫描超时是 300 秒，可通过 `config.yaml` 的 `bot.scan_timeout_seconds` 或环境变量 `BOT_SCAN_TIMEOUT_SECONDS` 调整。若某轮扫描卡住超过阈值，机器人会写入 `scan_timeout`、推送 Discord、主动退出，并由 Docker 的 `restart: unless-stopped` 自动拉起，避免容器还在但业务假死。若整轮扫描在拉取交易宇宙或行情前置步骤失败，机器人会写入 `scan_error` 后受控退出，方便 `/api/okx-bot/health` 直接暴露真实失败原因。

查看云端监控服务状态：

```bash
docker compose ps
```

启用 `trading` profile 后，再查看机器人日志和事件：

```bash
docker compose logs -f okx-strategy-bot
tail -f runtime/okx_market_cap_bot_events.jsonl
```

部署脚本默认只重建 `crypto-project`。如果需要同时重启 OKX 交易机器人，必须显式确认，避免在有未平仓持仓时误重启：

```bash
CONFIRM_RESTART_OKX_BOT=yes SERVICE='crypto-project okx-strategy-bot' ./deploy_vps.sh
```

上面的命令会把机器人安全地重启为 signal-only。只有明确确认继续启用 OKX Demo 下单时，才使用：

```bash
ENABLE_OKX_ORDER_MODE=yes CONFIRM_RESTART_OKX_BOT=yes SERVICE='crypto-project okx-strategy-bot' ./deploy_vps.sh
```

也可以先只构建 OKX 交易机器人镜像、不重启运行中的容器：

```bash
BUILD_ONLY=yes SERVICE='okx-strategy-bot' ./deploy_vps.sh
```

部署到有公网 IP 的云服务器后，手机可以直接访问：

```text
http://你的服务器公网IP/
```

如果绑定了域名并做好 DNS 解析，则访问：

```text
http://你的域名/
```

`/api/health` 可用于平台健康检查，返回 `ok: true` 代表服务已启动。
这个接口也会返回 `code_fingerprint`、`hostname`、`pid`、`started_at`、`discord_value_length`、`discord_last_ok_at` 和 `discord_last_error`，可以用来确认 VPS 当前跑的是哪份代码、是否读到了 Discord Webhook，以及最近一次推送是否失败。

如果怀疑 VPS 没有跑最新代码，先在本地和 VPS 分别看代码指纹：

```bash
python3 discord_diagnose.py --no-send
curl http://你的服务器公网IP/api/health
```

如果两个 `code_fingerprint` 不一致，说明 VPS 容器/进程不是当前这份代码，需要重新部署：

```bash
docker compose up -d --build
```

如果要在 VPS 上主动测试 Discord 推送，进入项目目录后运行：

```bash
python3 discord_diagnose.py --content "manual VPS Discord test"
```

使用 Docker 部署时，也可以直接在容器里测试：

```bash
docker compose exec crypto-project python discord_diagnose.py --content "container Discord test"
```

如果服务器前面加 Nginx 反向代理，`/api/events` 是 SSE 长连接，建议在对应 location 里关闭代理缓冲：

```nginx
proxy_buffering off;
proxy_cache off;
```

## 自用站点监控

应用内置了轻量站点探针，不需要注册账号、门户或多租户系统。默认每 60 秒从服务内部检查：

- `http://127.0.0.1:8080/api/health`
- `http://127.0.0.1:8080/api/reports`
- `http://127.0.0.1:8080/reports.html`

连续失败达到阈值后才会推送 Discord，恢复后可再推送一次恢复通知。查看完整探针状态：

```bash
curl http://你的服务器公网IP/api/site-monitor
```

常用配置放在 `.env`：

```env
SITE_MONITOR_ENABLED=true
SITE_MONITOR_INTERVAL_SECONDS=60
SITE_MONITOR_FAILURE_THRESHOLD=3
SITE_MONITOR_TARGETS=health=http://127.0.0.1:8080/api/health,reports_api=http://127.0.0.1:8080/api/reports,reports_page=http://127.0.0.1:8080/reports.html
SITE_MONITOR_MIN_REPORT_COUNT=0
SITE_MONITOR_REQUIRE_OKX_BOT_FILES=false
SITE_MONITOR_REQUIRE_OKX_BOT_OK=false
```

如果希望把报告和 OKX 机器人也纳入硬性告警，可以把 `SITE_MONITOR_MIN_REPORT_COUNT` 设为 `1`，并在确认 `trading` profile 正常运行后开启 `SITE_MONITOR_REQUIRE_OKX_BOT_FILES=true` 或 `SITE_MONITOR_REQUIRE_OKX_BOT_OK=true`。

## 回测报告托管

项目支持把 HTML 回测报告放在 VPS 的 `reports/` 目录中，由同一个 Web 服务托管。Docker 部署时，`docker-compose.yml` 会把宿主机的 `./reports` 挂载到容器内 `/app/reports`，容器重建后报告仍保留在 VPS 硬盘上。

访问报告列表：

```text
http://你的服务器公网IP/reports.html
```

直接打开单个报告：

```text
http://你的服务器公网IP/reports/BTCUSDT_15m_binance_project_signal_report.html
```

在 VPS 上生成报告并直接写入托管目录：

```bash
python3 project_signal_backtest.py --symbol BTCUSDT --interval 15m --limit 3000 --reports-dir reports
python3 backtest_visual_report.py --symbol BTC-USD --period 2y --reports-dir reports
```

也可以使用环境变量统一指定输出目录：

```bash
REPORTS_DIR=reports python3 project_signal_backtest.py --symbol ETHUSDT --interval 1h --limit 3000
```

把本地已有 HTML 报告迁移到 VPS 后，可以删除本地副本来释放空间：

```bash
rsync -av --progress ./*_report.html root@你的服务器公网IP:/opt/chanlun-monitor/reports/
```

这里的 `/opt/chanlun-monitor` 换成 VPS 上真实的项目目录。确认 `reports.html` 能看到报告后，再清理本地 HTML 文件。

## 网络代理

项目启动时会读取根目录 `.env` 并写入环境变量；同步 `urllib` 和异步 `aiohttp` 行情请求都会使用这些代理配置。需要通过 Clash/v2ray 等本机代理访问 Binance、OKX 或 Gate.io 时，可在 `.env` 中添加：

```env
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
ALL_PROXY=http://127.0.0.1:7890
NO_PROXY=127.0.0.1,localhost
```

Docker 容器里的 `127.0.0.1` 指向容器自身，不是宿主机。如果用 Docker 跑服务，并且代理软件在宿主机上监听 `7890`，请改用：

```env
HTTP_PROXY=http://host.docker.internal:7890
HTTPS_PROXY=http://host.docker.internal:7890
ALL_PROXY=http://host.docker.internal:7890
NO_PROXY=127.0.0.1,localhost
```

## 说明

- 默认监控 `BTCUSDT`、`ETHUSDT`。
- 监控列表会保存到 `watchlist.json`。
- 后端每 10 秒拉取一次 Binance 24 小时行情。
- 配置 Discord 后，会在启动后先推送一次 BTC/ETH 简报，之后每小时推送一次资金费率和 24h 涨跌变化。
- 每个启用信号的交易对会同时检查 15m、1h、4h、1d 的 MA5/MA10 与 MACD DIF/DEA 上穿下穿提醒。
- 前端通过 Server-Sent Events 自动刷新，不需要手动刷新页面。
- 自动计算 `15m`、`1h`、`4h`、`1d` 支撑点位，价格接近支撑位时发送提醒。
- 触发同一个支撑点位后不会重复发信，价格远离该支撑位后才会重新允许触发。
- `app.record_strategy_trades` 默认关闭，网站不会再默认承担策略机器人账本职责。

## Binance 策略机器人

机器人入口是：

```bash
python3 binance_strategy_bot.py
```

它会在运行时构建交易候选池：

```text
CoinGecko 市值前 100
 -> 排除稳定币
 -> Binance 现货 USDT 可交易过滤
 -> 24h USDT 成交额过滤
 -> 使用 ProjectSignalEngine 扫描信号
```

只扫描一轮用于测试：

```bash
python3 binance_strategy_bot.py --once
```

常用参数：

```bash
python3 binance_strategy_bot.py \
  --top-n 100 \
  --interval 15m \
  --min-quote-volume 10000000 \
  --max-open-positions 5
```

机器人状态保存在 `binance_strategy_bot_state.json`，开平仓信号日志写入 `binance_strategy_bot_events.jsonl`。当前 Binance 机器人先做独立策略扫描、信号推送和纸面持仓管理；真实下单执行应再接 Binance API 执行适配器。

## OKX 高流动性市值排名模拟盘机器人

多币种自动化模拟盘入口是：

```bash
python3 okx_market_cap_bot.py
```

默认只扫描、记录和推送信号，不提交订单。只跑一轮测试：

```bash
python3 okx_market_cap_bot.py --once
```

确认要提交 OKX Demo 模拟盘订单时，显式添加：

```bash
python3 okx_market_cap_bot.py --place-order
```

它会独立维护多个持仓，达到止损、2R 目标或 1R 保护位时自动平仓。Docker 部署时状态保存在 `runtime/okx_market_cap_bot_state.json`，事件写入 `runtime/okx_market_cap_bot_events.jsonl`。

当前默认扫描市值前 `200` 的候选币，随后只保留 OKX 24 小时估算成交额不低于 `500 万 USDT` 的永续合约。实时策略只加载最近 `300` 根 K 线（信号窗口为 200 根），并在 OKX 返回 429 时退避重试，避免扩大标的池后制造无意义的限流错误。每轮 `scan` 事件同时记录 `signal_states` 和互斥的 `signal_reasons`，可直接区分无背驰、等待确认、高周期不共振、震荡过滤、量价未确认和最终买卖过滤失败。`okx_scan_symbol_blocklist` 用于在信号计算前隔离持续返回异常行情的标的，不等同于订单黑名单。

默认仓位模式是 `bot.position_sizing: risk`：机器人在 `--place-order` 模式会读取 OKX Demo 的 USDT 权益，按 `risk_per_trade_pct` 和信号止损距离计算合约张数，再用 `max_position_notional_usdt` 限制单笔名义价值，最后按 OKX 的 `lotSz/minSz` 裁剪。当前默认参数是单笔风险 `0.5%`、名义价值上限 `50 USDT`；signal-only 模式未配置权益时会用 `1000 USDT` 纸面权益估算，不读取私有余额。如需临时回到固定张数，可把 `position_sizing` 改成 `fixed`，或启动时传 `--position-sizing fixed --size 1`。

云服务器使用 Docker Compose 时，只有显式启用 `trading` profile 才会启动 `okx-strategy-bot`，且默认仍是 signal-only。持续下单模式必须同时使用 `docker-compose.order.yml`。机器人启动、每轮扫描、开仓、平仓和错误都会写入 `runtime/okx_market_cap_bot_events.jsonl`。

JSONL 事件日志默认达到 10 MiB 时轮转并保留 5 份历史，可用 `EVENT_LOG_MAX_BYTES` 和 `EVENT_LOG_BACKUP_COUNT` 调整。Compose 也会限制容器 stdout/stderr 日志、CPU 和内存；资源上限可通过 `WEB_CPUS`、`WEB_MEMORY_LIMIT`、`BOT_CPUS`、`BOT_MEMORY_LIMIT` 覆盖。

## 配置边界

- `config.yaml`：策略、监控交易对/周期、支撑容差和机器人默认参数。
- `.env`：密钥、代理、运行路径和主机级覆盖；不要提交。
- `public/config.js`：仅用于 `file://` 页面在连接后端之前探测本机端口。
- `docker-compose.yml`：signal-only 安全默认；`docker-compose.order.yml`：显式 Demo 下单覆盖。

如果 OKX Demo 账户是双向持仓模式，在 `.env` 里设置 `OKX_POSITION_MODE=long_short`。机器人会在 SWAP 开仓和平仓订单里自动带上 `posSide=long/short`；单向持仓或现货模式不需要设置。OKX 下单失败时，事件日志会记录 `symbol`、`side`、`posSide`、`size`、`tdMode` 和完整错误，Discord 也会收到同样的失败摘要。

## OKX 回测与模拟盘

OKX 公共行情回测复用项目里的同一套 `ProjectSignalEngine` 策略：

```bash
python3 project_signal_backtest.py --exchange okx --symbol BTCUSDT --interval 15m --limit 1000
```

OKX 合约交易对会自动把 `BTCUSDT` 转成 `BTC-USDT-SWAP`。如果要回测现货：

```bash
python3 project_signal_backtest.py --exchange okx --okx-instrument-type SPOT --symbol BTCUSDT --interval 15m --limit 1000
```

模拟盘 API 密钥不要写进源码，建议在终端里设置环境变量：

```bash
export OKX_API_KEY="your_demo_api_key"
export OKX_SECRET_KEY="your_demo_secret_key"
export OKX_PASSPHRASE="your_demo_passphrase"
```

查看 OKX Demo 余额和最新策略信号，默认不会下单：

```bash
python3 okx_demo_signal.py --symbol BTCUSDT --interval 15m
```

确认要提交模拟盘市价单时，必须显式添加 `--place-order` 和数量：

```bash
python3 okx_demo_signal.py --symbol BTCUSDT --interval 15m --size 1 --place-order
```

建议 OKX API 权限只开模拟盘所需权限，不要开启提现权限。

## Discord 通知配置

使用 Discord Webhook 接收提醒。先在 Discord 频道设置里创建 Webhook，然后启动服务前配置：

```bash
export DISCORD_WEBHOOK_URL="your_discord_webhook_url"
python3 app.py
```

Discord 推送包括：

- BTCUSDT、ETHUSDT 每小时行情简报：最新价、24h 涨跌额、24h 涨跌幅、资金费率、下次资金费时间。
- 多周期支撑点位触发提醒。
- BTCUSDT、ETHUSDT 等启用缠论信号的交易对触发开多/开空观察提醒。
- 启用信号的交易对在 15m、1h、4h、1d 出现 MA5/MA10 或 MACD DIF/DEA 上穿下穿时即时提醒。
