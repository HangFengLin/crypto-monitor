# 抗过拟合策略验证

`strategy_validation.py` 是独立研究入口。它复用生产 `ProjectSignalEngine` 与
`position_manager.py`，但不会修改 `config.yaml`、启动机器人、提交订单或部署 VPS。

安装研究和静态图表依赖：

```bash
python3 -m pip install -r requirements-validation.txt
```

## 快速验证

先运行离线测试：

```bash
PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest discover -s tests
```

网络冒烟只取一周 BTC 数据，不属于常规 CI：

```bash
python3 strategy_validation.py \
  --smoke \
  --end 2024-01-08 \
  --skip-okx \
  --bootstrap-iterations 200
```

完整研究默认取最近完整 UTC 日之前两年、市值前 100 的 Binance USD-M
永续，并把同一快照中排名最高的 20 个 OKX SWAP 留作晋级复核：

```bash
python3 strategy_validation.py --bootstrap-iterations 2000
```

随机种子默认固定为 `20260620`，需要复现实验时可显式传入
`--seed 20260620`。完整模式即使使用 `--skip-okx` 也不会放行候选；该开关只适合
数据准备或冒烟诊断，缺少跨交易所证据时结果必定保留基线。
历史数据默认按快照顺序使用 4 个进程并发准备（`--data-workers 4`）；完成的指标、
高周期上下文、资金费率和 OI 会按区间与基线配置哈希写入压缩缓存。落盘和统计顺序
仍按不可变快照固定，因此并发不会改变随机结果；受限环境可改用
`--data-executor thread`。OKX 历史分页固定串行执行，避免公开接口并发触发 429 后
把前 20 复核池静默缩小；429 会进行有上限的指数退避。
候选回放默认按品种使用最多 8 个进程（`--evaluation-workers 8`，实际默认不超过
机器 CPU 数）；每个 fold 和候选的结果仍按快照顺序聚合。

## VectorBT 高速研究 PoC

`vectorbt_research.py` 是正式验证之前的离线筛选层。它从一份既有 prepared 缓存读取
K 线，不访问交易接口；生产 `ProjectSignalEngine` 和 `position_manager.py` 先生成一次
不可变信号事件及逐笔退出结果，VectorBT 再同时计算下列研究过滤组合：

- `min_signal_score`
- `min_structure_score`
- `divergence_filter`
- `signal_direction`
- `block_local_countertrend`

这些字段不改变背驰检测和确认状态机。`confirmation_mode`、背驰强度阈值、RSI、ADX、
微观结构等会改变信号生成的参数仍必须走 `strategy_validation.py`，不能放进这个 PoC
假装获得同等验证。

安装研究依赖后，显式传入一份缓存：

```bash
python3 -m pip install -r requirements-validation.txt
python3 vectorbt_research.py \
  --prepared-cache runtime/backtest-data/prepared/okx/BTCUSDT/15m/<cache>.json.gz
```

默认组合为 162 组。程序会先执行一次原始 `run_backtest`，再校验缓存路径的交易数量、
方向、入场、初始止损、目标、退出和净收益完全一致；一致性失败时退出码为 2。研究区间
只允许在前 80% 时间开仓，并在永久留出集前再预留完整 `max_hold_bars`，不会读取最后
20% 的交易结果。

每次运行写入新的 `reports/vectorbt_poc_<symbol>_<UTC时间>/`，包含：

- `manifest.json`：输入 SHA-256、数据覆盖、研究/留出边界、一致性和实测耗时；
- `candidate_results.csv`：全部候选的交易数、胜率、期望、复利收益和回撤；
- `candidate_trades.csv`：候选逐笔交易；
- `report.md`：只列达到最低交易数的研究摘要。

所有产物固定标记 `RESEARCH_ONLY`。预计提速按“原始单次回放耗时 × 候选数”计算，属于
投影，不是全部候选逐个重跑的实测；任何候选仍需回到本文件的 walk-forward、永久留出
集、FDR、成本压力和 OKX 跨市场门槛。

首次运行会下载并缓存官方归档，耗时和磁盘占用取决于品种上市时间。建议在正式运行时
显式指定 UTC 区间，以便日后完全复现：

```bash
python3 strategy_validation.py \
  --start 2024-06-20 \
  --end 2026-06-20 \
  --cache-dir data/strategy_validation \
  --reports-dir reports
```

## 数据时间边界

- Binance K 线使用官方月包并以日包补齐当前 UTC 月；已结束月份若没有月包则视为
  合约尚未上市，不再逐日探测。Unicode 合约路径会做百分号编码。资金费率通过官方接口按时间分页。
- OI 使用 Binance Vision 的 5 分钟 `metrics` 日包，只下载信号涉及日期及其前一日，并校验官方 checksum。
- OKX K 线来自 OKX 历史 K 线接口；资金费率使用 OKX 官方月度历史归档并由 REST
  补齐当前月，避免 REST 约三个月的可见窗口截断两年研究。OI 使用同一币种 Binance
  USD-M 合约作为代理，报告会明确标注。
- 资金费率和 OI 都用信号 K 线实际收盘毫秒前最后一个已发布值；恰好在下一根
  K 线开盘时发布的数据不会被计入上一根，不使用未来值后向填充。
- 每段包含 250 根指标预热；交易不得跨 fold 边界，验证边界隔离 96 根 15 分钟 K 线。
- 候选集合预先声明，不在训练窗内继续拟合参数；240 天训练窗和 60 天验证窗边界
  会写入产物，验证期只检验预声明候选。

## 候选覆盖文件

额外候选只能写在独立 YAML 中，且顶层只能包含 `strategy`。每个候选仍应只改变一个
参数族，避免组合搜索扩大多重比较空间：

```yaml
strategy:
  confirmation_mode: both
```

运行时传入：

```bash
python3 strategy_validation.py --candidate-overrides experiments/candidates.yaml
```

需要声明多个预设候选时，使用 `candidates` 列表；每项必须包含唯一名称和单一参数族：

```yaml
candidates:
  - name: stricter_rsi
    strategy:
      buy_rsi_threshold: 35
      sell_rsi_threshold: 65
```

无效字段会直接失败。只有候选通过全部门槛时，报告目录才会生成
`candidate_strategy.yaml`；该文件也不会自动写回生产配置。

## 输出与判读

每次运行使用独立目录 `reports/strategy_validation_<时间戳>`，包含：

- `manifest.json`、`data_config_hashes.json`、`artifact_hashes.json`：区间、随机种子、配置、缓存数据和产物哈希。
- `report_source_notes.json`：技术报告章节契约、图表映射、来源产物和图表省略原因。
- `universe_snapshot.json`：不可变研究池快照及排除原因。
- `data_coverage.csv`：各品种 K 线、资金费率和 OI 覆盖情况。
- `validation_trades.csv`、`test_trades_*.csv`：逐笔交易。
- `walk_forward_folds.csv`、`fold_metrics.csv`、`candidate_comparison.csv`、`test_summary.json`：
  训练/验证边界、walk-forward、FDR 与晋级门槛。
- `group_stats.csv`：边际和预声明二维分组，仅用于生成假设。
- `group_stats_full_8d.csv`：完整八维诊断，不用于排序或直接生成生产规则。
- `report.html`：总体、walk-forward、最终测试、成本压力及跨交易所复核摘要。

默认 `--min-group-trades 30`，统计输出包含 Wilson 95% 胜率区间、按 UTC 周分块的
bootstrap 区间，以及候选间比较的 BH-FDR 0.10 校正。最终 20% 测试集只在选定候选后
运行一次；若没有候选通过，正确结果就是保留基线且不生成晋级 YAML。

## 数据源失败后的断点续跑

如果官方端点限流或单个归档路径失败，先保留原报告目录，再运行：

```bash
python3 resume_strategy_validation.py \
  --report-dir reports/strategy_validation_<时间戳> \
  --cache-dir runtime/backtest-data
```

断点入口只重放覆盖报告中失败的 Binance 品种，随后从逐笔交易重建全部 fold 指标、
bootstrap、FDR 和候选选择，并强制补齐完整 OKX 前 20 后重新执行最终稳健性门槛。
如果生产 `config.yaml` 哈希与原实验不同、Binance 仍有缺失或 OKX 少于快照数量，程序
会硬失败而不是生成部分报告。
