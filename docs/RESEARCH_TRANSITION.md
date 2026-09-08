# 策略研究与信号跟踪改造

## 当前范围

完成本地代码改造：保留行情监控、Discord 信号提醒、历史研究和共享策略引擎，
将首页的机器人持仓改为本地模拟信号跟踪。公开 OKX 历史行情和跨交易所研究门槛
暂时保留；账户访问、下单入口和机器人在线状态依赖退出日常工作流。

尚未部署 VPS 或 Sites，未停止或重启任何远程服务。旧版本运行中的容器不会因为
本地 Compose 服务删除而自动停止。上线迁移需单独核对旧服务、Demo 持仓和历史账本。

## 已实现

- 新信号去重建档，参考入场价、当前价、止损、目标和扣费模拟浮动收益持续更新。
- 尚未结束的记录全部展示，历史数量限制只裁剪已结束记录。
- 只回放入场之后、已完成且尚未处理的 K 线；保留处理进度，避免重复触发。
- 触发退出后记录退出价、原因和结果；刚结束的旧记录也会排到最近历史前面。
- 每次估值或处理进度变化保存账本；重启恢复现有记录，不把旧告警补造为新跟踪。
- 从自选列表移除币种，不停止其已有跟踪的价格更新。
- 两套网页读取 `signal_tracking`；不再使用机器人状态文件。
- 删除 OKX 健康/错误接口、Compose 交易服务和 order override。
- 旧 OKX 脚本主入口返回 retired 和退出码 2；私有请求适配器拒绝账户与订单请求。
- 旧辅助函数暂留，供现有研究测试兼容；它们不构成可启动的交易服务。

## 证据与限制

行为测试见 `tests/test_signal_tracking.py` 和 `tests/test_research_only.py`。
新增核心行为先验证失败，再实现；重启恢复另有持久化回归测试。

已用现有 `.venv` 安装项目声明的 `requirements-validation.txt`，未更改依赖版本声明。

- `PYTHONPYCACHEPREFIX=/tmp/codex-pycache .venv/bin/python -m unittest discover -s tests`：146 项通过。
- Sites `pnpm run build`：通过。
- Sites `node --test tests/rendered-html.test.mjs`：2 项通过。
- `node --check public/app.js`、`bash -n deploy_vps.sh`、`git diff --check`：通过。
- 独立 `tsc --noEmit` 仍受既有 Cloudflare Workers 类型缺失影响。
  临时生成运行时类型后，未使用的 `db/index.ts` 仍要求未声明的 `Env.DB`；没有为
  信号跟踪引入数据库绑定，也未改动该预留模块。生产构建和渲染测试均通过。
- 未执行真实市场回放或浏览器交互测试，不将模拟记录表述为交易所成交。

此前未提交的机器人源码和 README 已复制到
`backups/research-transition-VnK8WS/`，保留迁移前状态供核对。
历史账本、报告、OKX 行情缓存均保留。

模拟收益基于信号参考价并按现有费率扣费，不包含真实成交和订单簿冲击。
逐笔收益合计不是组合资金曲线。长时间离线后超出行情窗口的历史缺口不会自动补齐。
