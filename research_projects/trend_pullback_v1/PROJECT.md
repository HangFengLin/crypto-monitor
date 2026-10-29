# TPR1：上涨趋势中的回调回收

本研究于 2026 年 10 月 25 日建立，只研究一个新入场假设，不改线上策略参数，不下单、部署或重启服务。完整规则与成本、账户假设保存在 [protocol.json](protocol.json)。

**当前决定：TPR1未通过固定历史诊断，不采用，也不追加参数搜索。** 基础成本账户收益-28.58%，双倍成本-43.92%。同日启动的前向采集在尚无新样本时提前中止，12周验证未完成；原协议和产物保留。详见 [历史回测报告](../../reports/trend_pullback_historical_20261025/REPORT.md)。

## 先使用现有回测模型

不必等满前向观察期才判断方案是否值得投入。历史诊断入口是 `scripts/trend_pullback_backtest.py`，复用已有的 `ProjectSignalEngine`、`position_manager.py` 和 `research_portfolio.replay_portfolio`，不另建撮合或会计引擎。它在相同品种、时间、资金和成本下，对比原入场与唯一 TPR1 入场，运行基础成本、双倍成本和额外延迟一根的六个账户场景。

本轮历史区间固定为 UTC 2026-06-20 至 2026-10-25（结束日不含），附加协议见 [historical_diagnostic_20261025.json](historical_diagnostic_20261025.json)。7—8月及9—10月初的数据曾用于其他研究，因此历史回测只能标记为诊断，不能把刚下载的历史改称真实前向或未见留出集。前向采集协议与冻结起点保持不变；两项工作可以同时进行。

## 唯一入场方案

品种固定为 Binance USDT 永续的 BTCUSDT、ETHUSDT，只做多。最近一根已完整收盘的 UTC 4 小时 K 线满足：收盘价 > EMA20 > EMA60，且 EMA20 高于前一根完整 4 小时 K 线的 EMA20。

15 分钟前一根 A 的最低价触及或低于 EMA20，收盘价不高于 EMA20；紧随其后的 B 收盘价重新高于 EMA20，并突破 A 的最高价，且 B 最低价不低于 A 的最低价，才产生一次入场意图。没有 MACD 背驰、缩放量或评分门槛；不等待更晚的 B，不扩展参数网格。

初始止损固定为 `min(A.low, B.low, B.close - 1.5 × ATR14_B)`。只有收盘后实际收到行情才作判断，模拟执行时间为首次本地观测之后的下一个 15 分钟开盘。它不是按信号收盘价已经成交。执行开盘已越过止损时，复用账户引擎拒绝该次入场。

退出复用现有 `position_manager.py` 与 `research_portfolio.py`，固定 `structure_atr`、96 根最长持仓和线性 USDT 会计。通常到 1R 全额退出；本研究没有同时调整止盈、止损或仓位参数。

## 新数据边界

初始化时以实际系统时钟冻结协议、源码、策略配置及哈希。从冻结之后产生、及时收到的完整 K 线才可能进入独立观察。首次历史行情只用于预热，即使刚下载也不属于新样本。每个决策保留原始公开数据、本地请求和接收时间、等待或拒绝原因，以及未来执行意图。

及时接收固定为 K 线收盘后 90 秒以内；缺口、迟到和行情修订保留为证据问题，不能用事后补数消除。高周期只能使用决策时已经完整收盘且已经收到的行情。测试注入的行情或时钟永远不能成为真实前向验证。

观察期固定为冻结后 12 周，中途只查看采集覆盖和信号数量，不查看收益选方案。结束后与同品种、同时间、同账户约束的原信号引擎多头分支比较；基础成本、双倍成本和额外延迟一根分别完整重放。资金费使用真实结算时点、费率、mark price 和当时持仓数量，缺失不会按零处理。

12 周是本次固定研究期限，不是盈利保证或通用合格线。即使通过协议内的继续研究筛选，也仍是研究结果，不证明真实账户或统计优势，不替代现有样本外及跨市场门槛，不自动晋级。

## 本地使用

从仓库根目录执行，输出目录只能使用这个实验的新目录，不能指向旧研究：

```bash
.venv/bin/python scripts/trend_pullback_forward.py --directory reports/trend_pullback_forward_20261025 init
.venv/bin/python scripts/trend_pullback_forward.py --directory reports/trend_pullback_forward_20261025 collect
.venv/bin/python scripts/trend_pullback_forward.py --directory reports/trend_pullback_forward_20261025 status
.venv/bin/python scripts/trend_pullback_forward.py --directory reports/trend_pullback_forward_20261025 follow
```

到期后使用同一目录的 `evaluate` 子命令。冻结后不要编辑协议或绑定的源码；需要改动时另建研究版本，并重新开始前向边界。采集只读取 Binance 公开行情，不使用账户密钥；它不会创建系统服务或自动部署到 VPS。本地机器休眠、退出或网络中断会影响连续观察，必须保留缺口。

## 与旧研究的区别

TPR1 不复用已失败的 V1 缩放量表达，不把 C1 生命周期或 C2 成本过滤包装成新优势，也不改写 T28/T84 周调仓趋势组合项目。过去已经看过的账本和历史研究都不属于这次新验证。BTC、ETH 两币结果不能推广为线上前 100 币池的结果。
