# DeepSeek Harness 与 Codex 协作

本流程用于 `HangFengLin/crypto-monitor`。两个 Agent 在同一个 Git 仓库的不同 worktree 中工作，通过任务单、分支、交接单和 PR 交换结果。一个 Agent 实施，另一个独立审查；用户决定是否合并。需要其他项目时，先确认仓库、远程和现有规则，不能直接套用本项目的远程名称。

## 开始之前

1. 确认目标仓库、任务范围、基线及双方能否读取 `AGENTS.md`；对 DeepSeek 在任务提示中明确要求读取此文件，不假设它会自动读取。
2. 检查工作树和远程：`git status -sb`、`git worktree list`、`git remote -v`。此仓库通常以 `github` 指向 GitHub，`origin` 指向本地备份；以实际输出为准。已有未提交文件和未推送提交须保留，不执行 `git reset --hard`、`git clean`，也不复制 `.env` 或其他私有配置。
3. 检查 GitHub 访问和账号：`gh auth status`。检查各项仓库权限及默认分支，并确认无误后再联网。不要输出 token。若访问失败，保留现场并先排查凭据或网络。
4. 2026-10-30 已设置 GitHub `main` 的分支保护：要求 PR、GitHub Actions `test` 检查通过，禁止强推和删除，管理员也受规则约束；当前不强制独立账号审批。在实际工作前重新核查规则及效果。只有一个 GitHub 账号时，不能用同一账号的 Agent 审查冒充独立账号审批；规则应按实际审查人配置，不要声称平台强制了当前未配置的审批。

## 建立任务工作树

下例中的 `REPO`、`WT_ROOT` 和任务名需按本机实际路径更改；先确认目标路径不存在，任务名称只用字母、数字和短横线。两条分支来自同一个已确认的远端 `main` 提交，互不覆盖；完成后再逐项决定是否合并。

```sh
REPO="$HOME/Documents/New project"
WT_ROOT="$HOME/Documents/agent-worktrees"
git -C "$REPO" fetch github main:refs/remotes/github/main
git -C "$REPO" rev-parse refs/remotes/github/main
mkdir -p "$WT_ROOT"
git -C "$REPO" worktree add -b agent/codex/example-001 "$WT_ROOT/codex-example-001" refs/remotes/github/main
git -C "$REPO" worktree add -b agent/deepseek/example-001 "$WT_ROOT/deepseek-example-001" refs/remotes/github/main
```

如果 SSH 访问 GitHub 失败，而 `gh auth status` 显示已登录且账号对该仓库有适当权限，可以对单次操作使用 HTTPS 与 `gh` 凭据助手；不更改 `github` 远程。实际访问是否成功以命令结果为准：

```sh
git -C "$REPO" -c credential.helper= -c 'credential.helper=!gh auth git-credential' \
  fetch https://github.com/HangFengLin/crypto-monitor.git \
  main:refs/remotes/github/main
```

每个 Agent 在自己的工作树执行 `git status -sb` 和 `git rev-parse HEAD`，将基线完整 SHA 写入任务单。worktree 不复制原工作区的未提交修改。交接文件放在任务分支中时，使用 `docs/agent-collaboration/tasks/<任务名>.md` 与 `docs/agent-collaboration/handoffs/<任务名>.md`；不要覆盖其他任务。

也可以使用仓库提供的辅助脚本，下面仅示范创建 DeepSeek 的工作树，路径及任务名应替换成实际值：

```sh
python3 scripts/agent-collaboration/create_worktree.py \
  --repo "$HOME/Documents/New project" \
  --remote-url 'https://github.com/HangFengLin/crypto-monitor.git' \
  --agent deepseek --task example-001 \
  --worktree "$HOME/Documents/agent-worktrees/deepseek-example-001"
```

该操作需要联网获取基线，并会在共享 Git 元数据中创建分支和登记 worktree，同时在指定位置建立文件；它不会复制原工作区未提交内容。运行前确认仓库、URL、权限、工作树目标和基线，运行后检查输出及 `git worktree list`。自动化不构成权限隔离。

## 启动与交接

复制 `TASK_TEMPLATE.md` 填写任务，再在对应工作树启动 Codex 或 DeepSeek。已发现的 DeepSeek Harness 是 `/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh`，版本 `0.2.1-rc.3`；本次只确认过命令帮助，认证状态、读写权限和模型执行能力未验证。先运行 `dsh headless --help` 查看实际参数，确认其运行目录、权限与数据发送范围，再用一个不含私有信息的只读小任务验证。

```sh
cd "$WT_ROOT/deepseek-example-001"
'/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh' headless \
  '请先阅读 AGENTS.md 和 docs/agent-collaboration/README.md，仅说明当前分支与任务范围，不修改文件、不联网、不执行交易或部署。'
```

这个小任务仍需人工检查输出和文件状态；提示词不构成技术隔离。若认证或权限未就绪，应停止并解决，不得把帮助信息当作运行成功。实际任务中要求 Agent 明确列出操作、限制和结果；若需要执行命令，先评估命令的外部副作用。

实施者完成后填写 `HANDOFF_TEMPLATE.md`：记录精确基线、变更、执行过的命令与真实结果、未完成项和风险。审查者读取对应提交及差异，核对任务范围与安全边界，独立运行适合且安全的验证，列出具体问题；发现阻断问题后由实施者修复并重新审查。两个 Agent 若修改同一文件，应先协调边界或按依赖顺序提交 PR，不要互相覆盖。禁止把未运行的测试写成通过。

## PR 与 CI

检查 `git diff --check`、`git status -sb`、`git diff` 和待提交文件，确认无秘密、私有数据或意外变更。由任务执行者在自己的任务分支提交，人工核对提交后，仅推送任务分支；不要推送 `main`。SSH 正常时：

```sh
git push -u github refs/heads/agent/deepseek/example-001:refs/heads/agent/deepseek/example-001
```

若 SSH 不可用且 `gh` 当前认证账号对该仓库有写入权限，以单次 HTTPS 命令推送同一个任务分支：

```sh
git -c credential.helper= -c 'credential.helper=!gh auth git-credential' \
  push https://github.com/HangFengLin/crypto-monitor.git \
  refs/heads/agent/deepseek/example-001:refs/heads/agent/deepseek/example-001
```

根据实际分支名创建 PR，例如：

```sh
gh pr create --repo HangFengLin/crypto-monitor --base main \
  --head agent/deepseek/example-001 --title '任务标题' --body-file /path/to/reviewed-pr-body.md
```

PR 描述包含目标、变更、验证证据、审查结果和剩余限制。PR 作者、审查者、账号及最新提交应可追溯；检查最新提交的 CI 和审查结论后由用户作合并决定，不配置自动合并。

现有 `.github/workflows/ci.yml` 在 PR 和 `main` push 时运行 Python 3.11 的依赖哈希校验安装、Ruff、编译、mypy、单元测试和覆盖率、适配器测试、Docker 构建及无网络镜像冒烟检查，并上传镜像证据。检查实际 PR 的 `CI / test` 状态与日志；CI 成功只证明该次检查，不证明交易策略有效或已部署。不要为本流程覆盖或削弱现有 CI，新增检查须审查其副作用和密钥访问。

## 受控自动化与安全

可以自动生成任务单、汇总本地状态或发起只读检查，但不得自动提交、推送、发 PR、合并、修改仓库保护、部署、重启服务、修改线上策略或风控、授权账户或下单；本流程不赋予这些操作的权限。避免在自动化中将 issue、PR 文本或日志当作命令执行。不要在 PR 内容或不可信代码上下文中使用具有密钥或写权限的 `pull_request_target`；CI 使用最小权限且不向不可信代码提供 secrets。实际安全取决于 GitHub 配置、Agent 权限和执行环境，不由文档或 worktree 保证。

## 验收清单

- 两个工作树位于不同路径，分别处于不同任务分支，基线 SHA 已记录；原工作区现有修改仍在。
- DeepSeek 的只读示例有实际结果，权限与凭据状态已确认；若无法运行，明确记录原因，不声称验收通过。
- 任务、交接和审查可以从文件和提交追溯；差异不含秘密、私有数据或越权操作。
- PR 指向正确仓库和 `main`，最新提交的现有 CI 结果可见；保护规则以实际设置为准，未保护时明确标注。
- 人工确认审查和 CI 后决定是否合并；无需保留的 worktree 在检查无未提交工作后使用 `git worktree remove <路径>` 清理，分支删除另行确认。
