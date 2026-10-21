# 2026-10-21 代码审计修复验收

范围为当前研究网站、模拟账本和研究证据接口。原有未提交的账户合同、状态恢复、
冻结研究复现及构建锁文件随本次版本一并保留并检查。运行数据、配置凭据和研究
产物不进入 Git 或发布包。工程验收不表示策略收益有效，也不授权交易所下单。

| 问题 | 修复和验收条件 | 回归证据 |
| --- | --- | --- |
| 公开回测耗尽监控线程池 | 研究计算和完整证据扫描使用独立单任务执行池，无等待队列；忙时返回 429。HTTP 取消后，实际任务完成才释放名额。 | `tests/test_audit_api_guards.py` |
| Paper ATR 移动止损缺少指标 | 原始 K 线先按时间去重排序，真实冲突停止本批处理，再按现有 ATR 算法计算；止损更新从下一根生效。 | `tests/test_paper_runtime_integrity.py` |
| 决策文件与 checkpoint 提交间崩溃 | 同根 K 线恢复先验证原决策摘要、输入、配置、前状态和首次时钟，再真实重放比对完整响应并补交 checkpoint。冲突文件不覆盖。 | `tests/test_signal_checkpoint_profiles.py` |
| 错误 watchlist 请求清空配置 | 必须显式提供数组及非空字符串 symbol；结构错误返回 400，磁盘和待确认状态不变；显式空数组仍可清空。 | `tests/test_audit_api_guards.py` |
| HTTP 限流重试过长 | 同步、异步默认最多三次，共享 60 秒重试预算；每次超时收紧到剩余额度，支持秒数及日期 Retry-After，末次失败不再等待。 | `tests/test_public_transport_budget.py` |
| 完整账户回放被摘要预算拒绝 | 流式归约完整回放并校验原 canonical hash；摘要与回放预算分离，大数组不进缓存，单次扫描总预算有界。 | `tests/test_research_evidence.py` |

本轮本地验收：

- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -p 'test_*.py'`：604 项通过。
- 研究项目独立测试：14 项通过；冻结复现入口 `--verify-only` 通过，未重算或升级原研究结论。
- Ruff 全仓通过；mypy 的 CI 四模块通过；110 个 Python 源文件编译通过；`git diff --check` 通过。
- 前端生产构建、VPS 静态构建、全部 56 项 Node 测试、TypeScript 和 ESLint 通过。
- 145 天无交易账户回放约 3.27 MB，正常核验；默认 12 候选 × 5 folds 的真实引擎回放约 81.39 MB，扫描约 1.7 秒。另一次 tracemalloc 测量峰值约 2.11 MB，未缓存完整数组。

边界：同步 urllib 约束的是重试预算与每次 socket 超时，持续滴流响应不具有严格
墙钟中止保证。账户回放上限为单文件 64 MiB、单次扫描 512 MiB，超过时保守报告
本次读取预算不足。Checkpoint 修复恢复同根 K 线，不新增跨周期追赶策略。

发布还需验证候选 Linux 镜像真实入口、无网络隔离冒烟及上线后 HTTP/容器状态。
发布时保留服务器账本和设置，比较源文件哈希；只更新 `crypto-project`。
