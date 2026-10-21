# 正式研究账户合同

`strategy_validation.py --account-contract <JSON>` 使用显式冻结账户协议。程序不替项目
所有者选择资金规模、风险预算或滑点；缺少合同会在正式研究获取数据之前拒绝运行。
合同不修改 `config.yaml`，也不授权下单。网络冒烟可不提供合同，其结果仅为逐笔诊断。

顶层字段：

| 字段 | 要求 |
| --- | --- |
| `schema_version` | 整数 `1` |
| `research_only` | 布尔值 `true` |
| `mark_frequency` | 字符串 `bar_close` |
| `execution` | 下表全部字段，不接受额外执行字段 |
| `funding_evidence` | 按 `binance`、`okx` 和各自冻结品种绑定的来源文件 |

`execution` 字段：

| 字段 | 类型和约束 |
| --- | --- |
| `initial_equity` | 有限正数；账户初始资金 |
| `risk_fraction` | `(0, 1]`；每次入场的初始止损风险预算 |
| `max_positions` | 正整数；最多并发持仓数 |
| `max_risk_fraction` | `(0, 1]`；全账户初始风险占用上限 |
| `direction_risk_fraction` | `(0, 1]`，或 `long`、`short` 各自的上限映射 |
| `fee_rate` | `[0, 1)`，须与冻结 CLI 参数相等 |
| `slippage_bps` | `[0, 10000)`；与手续费分别建模 |
| `max_hold_bars` | 正整数，须与冻结 CLI 参数相等 |
| `reward_risk` | 有限正数，须与冻结 CLI 参数相等 |
| `stop_mode` | `structure_atr` |
| `return_model` | `linear_usdm_v1` |

账户引擎还限制总名义持仓不超过资金；不会因多币并发让每笔都占用同一份完整资金。
该研究账户不是交易所保证金、杠杆或强平模拟。最大回撤来自统一时间轴的收盘盯市，
包含持仓浮盈亏；它不是 tick 级或盘中极值回撤。

每个资金费绑定的形状如下，尖括号表示必须替换的证据，不是默认值：

```json
{
  "binance": {
    "<冻结品种>": {"path": "<来源文件路径>", "sha256": "<文件字节SHA256>"}
  },
  "okx": {
    "<冻结品种>": {"path": "<来源文件路径>", "sha256": "<文件字节SHA256>"}
  }
}
```

相对路径以合同所在目录解析。来源文件必须包含准确的 `symbol`、`exchange`、
`market="linear_perpetual"`、对应交易所的 `source`、`complete=true`、`gaps=[]`、
非空 `raw_records` 和 `records`。原始结算字段 `fundingTime`、`fundingRate`、
`markPrice` 必须逐条对应规范化 `time`、`rate`、`mark_price`，不得重复结算。
不能用 Binance 文件满足 OKX 的资金费要求，也不能用信号输入里的最后费率代替结算。
新 Binance 采集文件保留原始记录并声明市场身份；旧文件不会自动改写或补造身份。

正式执行先准备完整冻结品种池，再检查来源和资金费覆盖；任一缺失会阻断筛选。完整性
声明和哈希证明的是调用者提交的来源绑定，仍需独立核对历史覆盖及实际发布时点。
实际结算以持仓数量 × 结算 mark price × rate 计入现金，按多空方向取符号。

每次回放保存 `account_replays/`，绑定评分及预热输入、事件、资金费来源 SHA、策略配置、
账户合同、窗口、交易所、成本倍数和执行源码 SHA。逐笔记录净损益须与最终账户净值
变化对账。成本压力重新运行账户，手续费率和滑点各乘二；资金费源不变。

原合同的完整字节 SHA 随实验冻结。同一协议恢复允许移动合同路径，但不允许改变风险
或成本数值；报告恢复始终写入新目录。已曝光样本仍然已曝光，程序生成候选文件只表示
满足既有研究筛选条件，不能证明新样本外有效性或授权交易。
