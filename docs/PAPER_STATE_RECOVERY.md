# 本地 paper 状态与执行版本

本地 paper 跟踪使用 `PersistentSignalEngine`，保留原策略检测规则。状态目录默认跟随
paper 账本目录，可用 `SIGNAL_ENGINE_STATE_DIR` 指定独立目录；不要与其他进程共享。
checkpoint 身份包含品种、周期、命名空间、实际执行版本与执行配置 revision。

同版本恢复保存 pending 候选、已消费结构、首次 ready、最后 closed bar、首次完整响应
和输入 SHA。相同 closed bar 重复扫描返回首次响应；迟到输入修订不会改写已经发出的
决策。倒退时间、损坏 checkpoint 或写入失败会拒绝新信号；原子写失败会回滚内存状态。
不同代码或配置版本不会自动继承旧候选，须分别保留其版本和观察起点。

实际 actionable 决策额外保存当时状态、ready bars、响应和输入 SHA；`observed_at`
与 `created_at` 是本地首次观测秒时间，`decision_close_time` 是源 K 线毫秒时间。
本地观测时刻不是交易所数据的 `published_at`，不能补造旧记录的发布链。paper 记录
保留输入引用、ready 和首次观测信息，以供未来真实前向/事后核对。

配置快照的 `config_revision`、`config_engine_version` 与实际运行的 `engine_version`、
`execution_revision` 分开。旧配置通过完整性和类型校验后可以使用显式兼容默认值，
新增字段列在 `strategy_defaults_applied`；不会改写原配置字节。旧持仓保留开仓时的费用、
止损和版本，新的退出另记当前 `exit_engine_version`。

账本不存在可以初始化空状态；账本存在但读取、JSON 或结构校验失败时，保留原字节、
记录 ledger fault 并拒绝注册和自动更新。先备份并确认故障原因，不能删除账本让系统
假装从未持仓。正常初始化只读取账本，不刷新历史时间字段。

持仓更新先验证整批未消费 K 线：完全重复的 close time 幂等处理，冲突重复拒绝；
明确未关闭、未来 close time、非正或非有限 OHLC、非法价格范围不能推进持仓。已持仓
先按已知 open 处理跳空，再处理整根范围；不利止损穿越按实际 open 成交。同一根未知
范围同时触及止损与止盈仍按止损优先；该范围新设的 trailing 不会在同根回查触发。
开盘退出不会把退出后的 close/high/low 计入持仓路径。

实时点价必须有来源 `closeTime`、有限正价，且不在未来、距本地 now 不超过 30 秒。
K 线接口失败且报价陈旧或缺少源时刻时，仅记录 paper 数据故障，不成交、不扣费、
不推进 closed bar。合格点价可按其实际观测价格退出，不能以本地接收时间冒充源时间。

paper 入场仍为 `signal_reference_price`，研究账户为信号确认后下一真实开盘；paper
与研究的持有期限也各有原合同。共享退出逻辑不证明两个完整执行系统等价。合成恢复
测试不构成真实前向样本，多进程共用 checkpoint 与断电恢复也尚未独立验收。
