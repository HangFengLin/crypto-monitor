# 加密货币策略研究与信号跟踪

当前主入口是原网站 `sites/lianqi`，采用 Binance USDT 合约公开行情与本地模拟信号账本，不需要交易所 API Key。五个入口为总览、行情与信号、模拟交易、策略研究、设置与诊断。

```bash
.venv/bin/python scripts/dev_product.py
```

打开 http://localhost:3000。完整安装、设置生效与本地数据说明见 [本地研究使用说明](docs/LOCAL_RESEARCH.md)。旧的交易所 Demo 与独立纸面机器人不参与当前启动流程；历史研究代码和账本保留。

## 信号跟踪规则

- 模拟开仓默认使用币安 USDT 永续中按市值排序的前 100 个币种，独立于网页自选列表，按 15m 周期扫描。
- 币池每 6 小时刷新；市值快照失效时暂停新开仓，已有仓位继续管理。名单和扫描进度显示在“模拟交易”页。
- `app.record_strategy_trades: true` 开启新信号记录；同一信号只记录一次。
- `strategy_trades.json`（Docker 中为 `runtime/strategy_trades.json`）保存模拟账本；重启恢复已有记录，不把旧告警补造为新跟踪。
- 进行中的跟踪全部显示。历史数量上限只限制已结束记录，不会丢掉尚未结束的跟踪。
- 退出沿用 `position_manager.py` 的策略规则；只处理已完成且尚未处理的 K 线，并以当前价格同步估值。同根 K 线同时触发止损与目标时，按止损优先处理。
- 跟踪采用信号参考价，不模拟撮合和实际成交。收益按配置扣除往返手续费；逐笔收益合计不是组合净值收益。
- 旧的 OKX 机器人脚本启动会返回“retired”；下单配置文件与 Docker 服务已移除。
- 本地修改不自动停止已运行的远程容器。部署迁移前应单独核对旧服务和未结束的 Demo 持仓，历史账本及缓存保留。

## 后端独立启动（维护用途）

只启动 API 与旧版兼容静态页面（不会下单，日常使用上面的原网站入口）：

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

已经生成 prepared 缓存后，可用 `vectorbt_research.py` 把生产信号与持仓生命周期只计算
一次，再批量筛选不会改变信号生成的评分、结构、背驰类型、方向和逆势过滤组合。该入口
强制保留最后 20% 时间及完整持仓周期，只生成 `RESEARCH_ONLY` 报告，不能晋级策略：

```bash
python3 vectorbt_research.py \
  --prepared-cache runtime/backtest-data/prepared/okx/BTCUSDT/15m/<cache>.json.gz
```

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

项目也提供 Docker 部署方式，`docker-compose.yml` 已经把服务器 `80` 端口映射到应用 `8080`。默认只启动监控和模拟跟踪网站：

```bash
docker compose up -d --build
```

当前 Docker Compose 默认启动：

- `crypto-project`：行情监控、信号跟踪、模拟账本、提醒和研究报告。

研究批处理服务 `universe-backtest` 放在 `backtest` profile 中，不随网站启动。部署脚本仅支持监控和研究服务；旧 OKX 下单环境开关或服务名会被拒绝。

若需要 Discord 通知，在服务器 `.env` 配置 `DISCORD_WEBHOOK_URL`。信号记录本身不依赖通知配置。

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
```

如需把研究报告纳入告警，可把 `SITE_MONITOR_MIN_REPORT_COUNT` 设为 `1`。

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

### Universe 市场全量回测

`universe_signal_backtest.py` 会按流动性筛选 OKX 市场候选，逐币写入结果和
checkpoint，最后生成聚合 CSV、候选假设和 `report.html`。默认参数是 OKX SWAP、
Top 200、最低 24 小时报价成交额 500 万 USDT、15m、20,000 根 K 线、30% holdout
和 96 bars embargo。这里的候选仅供研究，正式晋级仍需运行
`strategy_validation.py` 的 bootstrap 和 BH-FDR 验证。

本机直接运行：

```bash
python3 universe_signal_backtest.py
```

指定固定 UTC 历史窗口：

```bash
python3 universe_signal_backtest.py \
  --start 2024-01-01T00:00:00Z \
  --end 2025-01-01T00:00:00Z \
  --run-id okx-2024
```

VPS 上先只构建回测镜像，不会启动或重建网站与交易机器人：

```bash
docker compose --profile backtest build universe-backtest
```

五币种、500 bars 冒烟测试可以作为命名的一次性后台容器运行：

```bash
docker compose --profile backtest run -d \
  --name universe-backtest-smoke \
  universe-backtest \
  --max-symbols 5 --limit 500 --max-hold-bars 24 --embargo-bars 24 --run-id smoke
docker logs -f universe-backtest-smoke
```

冒烟通过后启动默认 Top 200 正式任务：

```bash
docker compose --profile backtest run -d \
  --name universe-backtest-top200 \
  universe-backtest \
  --run-id top200
docker logs -f universe-backtest-top200
```

需要测试中断恢复时，用正常停止信号并给进程 30 秒写完 checkpoint，然后再续跑：

```bash
docker stop --time 30 universe-backtest-smoke
docker logs universe-backtest-smoke
docker compose --profile backtest run -d \
  --name universe-backtest-smoke-resume \
  universe-backtest \
  --resume /app/reports/universe_backtest_smoke
```

运行产物保存在宿主机 `reports/universe_backtest_<run-id>/`，历史数据缓存保存在
`runtime/backtest-data/`；删除或重建一次性容器不会删除这些文件。报告写完后可在
`http://你的服务器公网IP/reports.html` 中查看。容器默认限制为 0.75 CPU、700 MiB
内存和 1200 MiB 内存加 swap，可通过 `.env` 的 `BACKTEST_CPUS`、
`BACKTEST_MEMORY_LIMIT`、`BACKTEST_MEMORY_SWAP_LIMIT` 覆盖。

Discord 回测通知默认关闭。VPS `.env` 的推荐配置是：

```env
BACKTEST_DISCORD_ENABLED=true
BACKTEST_DISCORD_WEBHOOK_URL=
BACKTEST_DISCORD_MIN_INTERVAL=900
REPORT_PUBLIC_BASE_URL=http://your-server.example.com
```

`BACKTEST_DISCORD_WEBHOOK_URL` 留空时回退到现有 `DISCORD_WEBHOOK_URL`。Webhook
只能放在 `.env`，不要写入代码、报告或命令行。通知发送失败只写入运行日志，不会
终止回测。

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

机器人状态保存在 `binance_strategy_bot_state.json`，开平仓信号日志写入 `binance_strategy_bot_events.jsonl`。当前 Binance 机器人先做独立策略扫描、信号推送和纸面持仓管理，不连接交易账户。网站信号跟踪使用自己的模拟账本，不合并该独立脚本的历史记录。

## 配置边界

- `config.yaml`：策略、监控交易对/周期、支撑容差和机器人默认参数。
- `.env`：密钥、代理、运行路径和主机级覆盖；不要提交。
- `public/config.js`：仅用于 `file://` 页面在连接后端之前探测本机端口。
- `docker-compose.yml`：监控网站和离线回测服务，不含交易执行服务。

## OKX 公开行情研究

OKX 公共行情回测复用项目里的同一套 `ProjectSignalEngine` 策略：

```bash
python3 project_signal_backtest.py --exchange okx --symbol BTCUSDT --interval 15m --limit 1000
```

OKX 合约交易对会自动把 `BTCUSDT` 转成 `BTC-USDT-SWAP`。如果要回测现货：

```bash
python3 project_signal_backtest.py --exchange okx --okx-instrument-type SPOT --symbol BTCUSDT --interval 15m --limit 1000
```

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
