# 已停用的扩展方案

当前产品只记录本地模拟交易，不需要币安 Demo 账户。下文为历史设计参考，启动入口已停用，不应按此部署。当前说明见 [LOCAL_RESEARCH.md](LOCAL_RESEARCH.md)。

# Binance USDT 本位合约模拟盘

本地迁移代码：实盘公开合约行情生成信号，官方 Demo 执行订单。
不提供实盘私有接口、实盘开关或自动晋级。现有 OKX 脚本保持 retired；
运行中的旧 VPS 容器不会随本地文件改变，尚未进行远程迁移。

## 产品入口

- `binance_demo_bot.py`：唯一新增官方模拟盘执行入口。
- `binance_demo.py`：Demo 专用签名请求、下单意图持久化、成交与持仓对账。
- `/binance-demo.html`、`/api/binance-demo/status`：独立模拟盘交易与异常状态。
- 原网页 `signal_tracking` 和 `binance_strategy_bot.py` 仍是纸面模拟，历史保留，
  不导入 Demo 持仓。网页的默认开发启动及 Compose 行情已改为 `binance_usdm`。
- 飞书使用原有持久化发送队列，新通知明确写“Binance 模拟盘”及策略版本。
  网页通知进程读取同一 `BINANCE_DEMO_DIR` 才能收到执行事件。

## 使用

在本机 `.env` 安全配置官方 Demo 账户的两项环境变量：
`BINANCE_DEMO_API_KEY`、`BINANCE_DEMO_SECRET_KEY`。不要把密钥写进报告或聊天。
账户必须为单向持仓；程序检查后拒绝双向模式，不自动更改账户设置。
建议使用无其他手工订单的专用 Demo 账户：未跟踪持仓/挂单会阻止新开仓。

```sh
.venv/bin/python binance_demo_bot.py --check
.venv/bin/python binance_demo_bot.py --probe
# 账户联调完成后，启动模拟盘；此命令会提交官方模拟订单。
.venv/bin/python binance_demo_bot.py --run --once
.venv/bin/python binance_demo_bot.py --run
```

`--check` 不联网；缺少密钥返回退出码 2。`--probe` 只读两个环境的公共合约信息。
Compose 的 `demo` profile 提供独立服务，但本次没有启动、部署或重启服务。
`BINANCE_DEMO_DIR` 默认 `runtime/binance-demo`，保存独立账本和源码/配置版本快照。
同一个目录通过文件锁只允许一个执行进程。不同机器/目录不可共用一个 Demo 账户。

## 策略与风控

复用 `ProjectSignalEngine`、指标实现和 `position_manager` 生命周期，不重新调参。
基础及高周期数据都来自实盘 USD-M 合约，仅使用完成的 K 线；短历史、缺口、过期数据跳过。
品种池为实盘与 Demo 同时支持的 USDT 永续，按实盘 24h 成交额排序并应用原成交额门槛和数量上限；
这不是旧的 CoinGecko 市值排名，池快照保存在账本，研究比较时必须注明此差异。

`binance_demo.entry_enabled` 单独控制新模拟订单，与停用的旧 `bot.entry_enabled` 分离。
沿用 `bot` 的信号方向、结构和评分过滤，以及每笔 0.5% 风险预算、50 USDT 名义金额上限、5 个持仓上限。
不提升杠杆或最小下单金额；交易所过滤器不满足则跳过。
参考价与 Demo 标记价偏差超过 `max_price_deviation_bps`（当前 100 bps）时跳过。
这些是执行边界，不是策略已经有效的证明。旧 OKX 历史盈亏不驱动新账户的品种冷却。

## 订单与故障处理

- 写入下单意图并 fsync 后才发送请求；稳定 client ID 防止同一信号重复下单。
- 超时/结果未知先查单，重启后也不盲目补发。无法证明结果时阻止新开仓。
- 入场部分成交先撤销余单，终态确认后按实际成交数量记录和保护。
- 入场成交后在 Demo 挂原始止损 `STOP_MARKET` 条件单，`closePosition=true`。
- 止损挂单失败会尝试只减仓退出并锁定后续新开仓；退出结果仍须对账。
- 策略后续移动止损/止盈由扫描进程基于已完成的实盘 K 线计算，并发送 Demo 市价减仓。
  原始交易所止损作为进程离线时的保护；离线期间不能执行本地移动止损。
- 不凭参考触发价格记录成交；平仓必须有交易所退出成交及零持仓证据。
- 平仓部分成交或手工更改导致数量不符时保持阻断，需核对剩余持仓；不自动调整账本抹平差异。
- 故障锁定不自动清除。不直接删除账本恢复；应先核对订单、保护单和持仓。
- 有持仓时策略版本变化会拒绝启动，需用保存的原版本完成持仓管理。

## 成交成本和研究边界

记录入场参考价、实际 Demo 成交均价、成交数量、方向调整后的滑点、版本与原始成交回报。
费用按币种保留；资金费保留原始 income 记录，不编造折算汇率。
成本抓取限定最近 7 天、每次最多 1000 条；达到上限或持仓跨度超窗明确标为不完整，
需另行补充历史对账，不能据此声称获得完整净收益。没有汇总为策略收益排名或组合净值。

网页原纸面账本仍只是参考价与既有费率模型，尚不具备盘口撮合、排队或市场冲击模拟。
Demo 成绩与纸面成绩分开，不作为实盘收益承诺；实盘仍是另行授权、另行验收的阶段。

## 本次验证

- 新增离线测试：路由隔离、风险取整、短/坏行情、意图持久化、超时查单、重启去重、
  部分入场成交、保护失败减仓、未跟踪持仓拒绝、平仓证据、API 状态和通知区分。
- 公共连接已成功，尚缺 Demo 密钥，未执行私有账户/下单/撤单/止损端到端联调。
- 网站新页面仅在隔离预览服务验证；既有本地服务、VPS、Sites 均未发布此改动。

官方接口依据：
[General Info](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info)、
[Trade API](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Order)。

### 2026-09-08 验证结果

- `PYTHONPYCACHEPREFIX=/tmp/binance-demo-pycache .venv/bin/python -m unittest discover -s tests`：183 项通过。
- 初次实际 K 线读取复现 `IncompleteRead`；增加仅公共 GET 的 3 次有界传输重试，
  回归测试通过，BTCUSDT/ETHUSDT 随后各成功使用 299 根已完成 15m K 线计算信号。
  该结果是当次连接证据，不代表网络以后不会失败；重试耗尽仍按异常处理。
- 实盘/Demo 公共合约信息探测均成功；`--check` 返回 `credentials_missing` 和退出码 2。
- Edge/Playwright 隔离浏览器：桌面 1280px 与手机 390px 页面检查通过，
  没有脚本异常、没有页面横向溢出；临时预览服务已停止。
- 新页面 JavaScript 语法检查、Python 测试、`git diff --check` 通过。
- 保留原工作区所有既有未提交改动；没有创建提交、发布或触发真实/模拟订单。
