# 抗过拟合策略验证

`strategy_validation.py` 是独立研究入口。它复用生产 `ProjectSignalEngine` 与
`position_manager.py`，但不会修改 `config.yaml`、启动机器人、提交订单或部署 VPS。

新验证默认采用 `linear_usdm_v1`，手续费按入场和退出的实际名义本金计算，入场 K 线
计为观察的第一根。执行版本与观察边界写入 manifest；旧报告缺少版本字段时，读取为
`legacy_ratio_v1` 与原期限语义，不能混入新版账户对照。显式诊断旧实验可传入
`--return-model legacy_ratio_v1 --holding-limit-convention legacy_inclusive_end`。
正式验证固定全额 `structure_atr` 退出；成本压力用相同账户和输入完整重放，手续费率
与独立滑点各乘二，资金费结算源保持冻结。逐笔 CSV 不能代替账户路径。

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

正式研究必须先提供独立账户合同及按交易所、品种绑定的完整资金费来源。合同未给定、
资金费缺失或冻结品种覆盖不完整时，程序拒绝正式候选筛选，不能按可用子集继续晋级。
合同的格式与来源限制见 [ACCOUNT_VALIDATION_CONTRACT.md](ACCOUNT_VALIDATION_CONTRACT.md)。
资金规模、风险比例、并发持仓、名义手续费和滑点需要事先确定；示例数值不能代替真实
经济与风险验收协议。正式账户只接受 `linear_usdm_v1` 与 `entry_bar_is_first`。

默认研究区间为最近完整 UTC 日之前两年、市值前 100 的 Binance USD-M 永续，并把同一
快照中排名最高的 20 个 OKX SWAP 留作晋级复核。先冻结品种池，并为池内全部品种准备
账户合同的资金费 SHA 绑定，再运行：

```bash
python3 strategy_validation.py \
  --universe-snapshot experiments/universe_snapshot.json \
  --account-contract experiments/account_contract.json \
  --bootstrap-iterations 2000
```

随机种子默认固定为 `20260620`，需要复现实验时可显式传入
`--seed 20260620`。正式模式拒绝 `--skip-okx`；该开关仅用于冒烟诊断。无账户合同的
冒烟指标固定标记 `account_verified=false`，不具有账户或候选晋级权威。
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
  --universe-snapshot experiments/universe_snapshot.json \
  --account-contract experiments/account_contract.json \
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
- 资金费率和 OI 按源事件时间匹配信号 K 线实际收盘毫秒前的最后值；恰好在下一根
  K 线开盘的事件不会被计入上一根，不使用未来值后向填充。源事件时间不自动证明实际
  发布时间或可得时间，真实 publication/available_at 链仍需独立核验。
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
python3 strategy_validation.py \
  --candidate-overrides experiments/candidates.yaml \
  --universe-snapshot experiments/universe_snapshot.json \
  --account-contract experiments/account_contract.json
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
- `coverage_gate.json`：每个交易所请求、已评估、失败及异常品种名单和哈希。任一冻结
  品种准备失败会在筛选前停止，仍保存覆盖错误，不能生成晋级结果。
- `account_contract.json`、`account_replays/`：原账户合同字节及统一资金回放的成交、
  费用、资金费、持仓、逐根收盘盯市净值、输入/源码绑定。候选门禁使用该账户回撤，
  不再使用按退出时间排列的逐笔复利曲线。
- `validation_trades.csv`、`test_trades_*.csv`：逐笔交易。
- `walk_forward_folds.csv`、`fold_metrics.csv`、`candidate_comparison.csv`、`test_summary.json`：
  训练/验证边界、walk-forward、FDR 与晋级门槛。
- `group_stats.csv`：边际和预声明二维分组，仅用于生成假设。
- `group_stats_full_8d.csv`：完整八维诊断，不用于排序或直接生成生产规则。
- `report.html`：总体、walk-forward、最终测试、成本压力及跨交易所复核摘要。

账户净值包含收盘时的持仓浮盈亏；它不能证明盘中最坏回撤，也不能代替真实保证金、
强平和盘口成本验收。资金费文件的 `complete` 和来源声明需要独立复核，哈希只能证明
绑定与字节完整性。程序筛选通过仍为 `RESEARCH_ONLY`，不等于通过绝对经济目标。

默认 `--min-group-trades 30`，统计输出包含 Wilson 95% 胜率区间、按 UTC 周分块的
bootstrap 区间，以及候选间比较的 BH-FDR 0.10 校正。最终 20% 测试集只在选定候选后
运行一次；若没有候选通过，正确结果就是保留基线且不生成晋级 YAML。

## 数据源失败后的断点续跑

如果官方端点限流或单个归档路径失败，先保留原报告目录，再运行：

```bash
python3 resume_strategy_validation.py \
  --report-dir reports/strategy_validation_<时间戳> \
  --account-contract experiments/account_contract.json \
  --reports-dir reports/replayed_accounts \
  --cache-dir runtime/backtest-data
```

恢复入口保留旧报告，在其目录之外建立新报告。它校验原生产配置、候选注册表、基线、
品种快照及哈希后，使用原区间和候选重新执行完整账户回放；旧逐笔 CSV 不能恢复账户
浮亏、资金约束或资金费。原实验已有账户合同的，必须提供同一 SHA 的合同；改变风险
或成本属于新实验，不能伪装为断点恢复。仅有旧逐笔诊断的线性实验需要新显式账户协议，
旧 `legacy_ratio_v1` 实验不能通过此入口变成账户验证。

重放已经看过的历史区间仍是已曝光样本；更换目录、修复代码或重建账户不会使它成为
未见过的样本外数据。缺失任一冻结品种或完整资金费来源，程序硬失败并保留阻塞原因。
