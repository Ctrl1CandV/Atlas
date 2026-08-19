# Atlas

[English](README.md)

Atlas 是一个本地、可审计的工作流引擎。YAML 定义图，MCP 工具负责校验、预演和运行，本机 Web 界面展示已记录的节点输入、输出、失败与产物。

> **候选版本：** Python 包版本 `0.1.0rc1`，产品/发布版本 `0.1.0-rc.1`，Git tag `v0.1.0-rc.1`。这是仅支持 Windows 10/11 的源码预发布版，没有发布到 PyPI，也没有预编译安装器。

## 诚实的 RC 范围

- 当前可用：`llm`、`research` 和 `coding_agent` 工作流、有界路由/循环、六个 MCP 工具、带源码位置的 YAML 校验、本机运行查看、仅 interrupted 运行的 checkpoint 恢复、人工审批门。
- 生产 agent 执行必须显式启用：只有 `config/agents.json` 明确设置 `runner` 为 `local_cli` 时，Atlas 才启用 Claude CLI runner。缺少配置或任一预检条件不满足时，都会在创建 run 前 fail-closed。
- Claude CLI 是当前用户身份下的宿主进程。Atlas 不写原目录；对可写 `coding_agent`，Atlas 冻结 baseline，并比较该 baseline 与 agent 结果的普通文件字节清单，生成完整文本 unified diff。
- Diff 采集不执行 `git add`、filter、hook、attributes、textconv 或 external diff。二进制变更 fail-loud，审批证据绑定 `baseline_digest`、`result_digest` 与 `patch_digest`。
- Worktree 副本**不是 OS 沙箱**。进程理论上可访问当前用户有权访问的任意宿主路径；`allowed_paths`、工具选择和回环绑定都不是安全边界。
- 随附六个与具体模型无关的示例。“多厂商”等名称只表达预期拓扑；只有用户明确绑定真实不同的供应商/模型后，意见才可称为独立。

## 环境要求

- Windows 10 或 11 x64（RC 支持的运行平台）
- Python 3.12 与 [uv](https://docs.astral.sh/uv/)
- Node.js 22.12 或更新版本及 npm，用于私有 Web 构建
- Git，用于源码开发和 coding-agent 源目录 HEAD/clean 状态预检（diff 生成本身不执行 Git）
- Claude Code 2.1.0 或更新版本，仅在启用生产 agent 执行时需要

## 从源码安装或升级

使用可信源码压缩包或已有检出；本文不虚构仓库 URL。

```powershell
uv sync --locked --all-groups
npm --prefix web ci
npm --prefix web run build
```

升级时先替换/更新源码并阅读 `CHANGELOG.md`，再重复锁定依赖同步和 Web 全新安装。运行配置保留在本机；修改活动配置前先比较新版 `config/*.example.json`。

首次启动 `atlas-web` 或 `atlas-mcp` 时，Atlas 会从通用模板创建缺失的本机配置，任何已有文件都不会被覆盖。也可以显式初始化：

```powershell
uv run atlas init
```

只编辑被忽略的运行文件。凭据只放 `config/.env`；不要提交或分享活动供应商、agent、能力、价格、运行、prompt 或输出数据。未知价格保持 `null`。

Agent 执行默认关闭，直至 `config/agents.json` 显式包含 `"runner": "local_cli"`。每个选定的 agent 模型必须属于已配置供应商，该供应商需提供 `anthropicBaseUrl`、模型 allowlist 和 `config/.env` 中当前供应商的凭据。详见 [`config/README.md`](config/README.md)。

## 本机运行

```powershell
uv run python -m atlas.web   # 等价：uv run atlas-web
```

打开 <http://127.0.0.1:8321>。服务必须留在回环地址；当前 RC 没有多用户认证或远程部署安全模型。

### 在 harness 中使用 MCP

仓库自带 [`.mcp.json`](.mcp.json)。用 Claude Code（或其他支持项目级 MCP 配置的 harness）打开克隆下来的 Atlas 仓库，六个 MCP 工具会通过 stdio 自动启动，无需手动开终端。配置执行的是 `uv --directory . run atlas-mcp`，以仓库根目录为工作目录，只要先完成 `uv sync --locked --all-groups` 即可在任何机器上使用。

每个工作流都应先校验、再 dry-run，检查模型绑定、守卫和费用后，才明确请求真实运行。校验和 dry-run 不调用供应商；真实运行可能收费。已确认费率时美元帽按预估与实际费用结算；费率未知但设置了成本帽时，Atlas 会保守占满本次剩余预算以阻止后续复用，但无法证明供应商实际账单未超过该帽。`human` 节点在本机界面等待决定。

`coding_agent.workdir` 必须指向已存在目录。Atlas 不会在 YAML 中展开 `${ATLAS_HOME}`。随附示例使用相对路径 `demo-project`，仅当从 Atlas 源码根目录启动时才按预期解析；否则应通过受支持的本次运行覆盖传入已存在的绝对路径。

Agent 子进程环境只包含必要系统变量、所选供应商端点和当前供应商凭据。`allow_web` 默认 `false`；设为 `true` 只会增加 Claude CLI 的 `WebSearch` 与 `WebFetch` 工具。它不是 OS 级网络隔离：可写 coding agent 拥有 `Bash`，仍可能联网。`allowed_paths` 仅适用于 `research` 或 `writable: false` 的 `coding_agent`；可写 coding 与 `allowed_paths` 的组合会在创建 run 前失败，因为 Claude `--add-dir` 不是只读边界。`max_turns` 仍是经过校验的工作流/规格字段，但当前 Claude CLI 没有硬轮次参数；硬限制由 deadline 和已配置预算承担。

## 安全与隐私

- 把任务、网页内容、仓库和模型输出都视为不可信数据。
- `runs/` 可能包含完整 prompt、源码、输出、diff、哈希与审批记录。Git 忽略不等于加密或留存管理。
- 不要在 issue 或 CI 产物中暴露 `config/.env`、活动配置或运行产物。
- 不要把目录副本、路径白名单、工具列表、`allow_web` 或回环绑定当作 OS 安全边界。

使用真实凭据前阅读 [`SECURITY.md`](SECURITY.md)。工作流字段和 MCP 用法见 [`skill/SKILL.md`](skill/SKILL.md) 与内置指南。

## 开发验证

```powershell
uv lock --check
uv run python -m compileall -q atlas
uv run pytest
npm --prefix web run lint
npm --prefix web run test:diff
npm --prefix web run build
uv build --sdist
```

真实供应商测试默认排除、可能收费且必须主动运行。手动 CI 使用受保护环境、单个受限 discovery 测试和作业超时，并且只能使用一次性测试凭据。

贡献规则见 [`CONTRIBUTING.md`](CONTRIBUTING.md)，版本变化见 [`CHANGELOG.md`](CHANGELOG.md)。

## 许可证

Apache License 2.0。见 [`LICENSE`](LICENSE) 与 [`NOTICE`](NOTICE)。
