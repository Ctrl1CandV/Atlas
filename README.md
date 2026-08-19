# Atlas

**中文** · [English](README.en.md)

![version](https://img.shields.io/github/v/tag/Ctrl1CandV/Atlas) ![license](https://img.shields.io/badge/license-Apache--2.0-green) ![platform](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgrey) ![python](https://img.shields.io/badge/python-3.12-blue)

Atlas 是一个**本地、可审计的多模型工作流引擎**。用 YAML 定义图，让不同厂商的模型分工协作——并行调研、交叉辩论、代码实施、人工审批——你在本机 Web 界面实时看到每个节点的完整输入与输出。

> **发布范围：** 当前正式版本是 `v0.1.0`，仅支持 Windows 10/11 x64，以源码 sdist 发布；尚未发布到 PyPI，也没有预编译安装器。Git clone 与发布 sdist 的内容并不完全相同，见下方安装说明。

![Atlas 运行视图](assets/observe-run.png)

点开任意节点，可以看到它的完整输入与输出、实际使用的模型、token 与耗时：

![Atlas 节点详情](assets/observe-node.png)

## 为什么是 Atlas

- **多模型协作**：跨厂商 fallback 链、辩论裁决、并行分片、有界修复循环；六个 MCP 工具构成控制面，你的 AI 助手（Claude Code 等）可以直接替你写图、校验、预演、运行。
- **先预演后付费**：`validate` 与 `dry_run` 零成本；假成功检测会拦下空输出、截断和缺必填字段的"成功"——模型只回一句 OK 也过不了关。
- **全程可审计**：append-only 事件账本、write-once 产物与哈希断言、审批证据绑定 baseline/result/patch 三摘要。
- **崩溃可恢复**：控制器被杀后运行自动判定为 interrupted，checkpoint 续跑只补未完成节点，成本预留不重算预算。
- **人工在环**：`human` 节点把图暂停在 Web 界面，等你审阅真实产物后批准或驳回。
- **本地优先**：Web 仅绑回环地址，凭据只存本机 `config/.env`，Atlas 没有托管控制面或内置遥测；真实运行仍可能调用你配置的远程模型供应商。

## 能力与边界（如实说明）

- 当前可用：`llm`、`research` 和 `coding_agent` 工作流、有界路由/循环、六个 MCP 工具、带源码位置的 YAML 校验、本机运行查看、仅 interrupted 运行的 checkpoint 恢复、人工审批门。
- 生产 agent 执行必须显式启用：只有 `config/agents.json` 明确设置 `runner` 为 `local_cli` 时，Atlas 才启用 Claude CLI runner。缺少配置或任一预检条件不满足时，都会在创建 run 前 fail-closed。
- Claude CLI 是当前用户身份下的宿主进程。Atlas 不写原目录；对可写 `coding_agent`，Atlas 冻结 baseline，并比较该 baseline 与 agent 结果的普通文件字节清单，生成完整文本 unified diff。
- Diff 采集不执行 `git add`、filter、hook、attributes、textconv 或 external diff。二进制变更 fail-loud，审批证据绑定 `baseline_digest`、`result_digest` 与 `patch_digest`。
- Worktree 副本**不是 OS 沙箱**。进程理论上可访问当前用户有权访问的任意宿主路径；`allowed_paths`、工具选择和回环绑定都不是安全边界。
- 随附六个与具体模型无关的示例（并行调研综合、多厂商辩论裁决、map-reduce 分析、修复循环、人工审批管线、代码实施审查）。“多厂商”等名称只表达预期拓扑；只有你明确绑定真实不同的供应商/模型后，意见才可称为独立。

## 环境要求

- Windows 10 或 11 x64（支持的运行平台）
- Python 3.12 与 [uv](https://docs.astral.sh/uv/)
- Node.js 22.12 或更新版本及 npm，用于私有 Web 构建
- Git，用于源码开发和 coding-agent 源目录 HEAD/clean 状态预检（diff 生成本身不执行 Git）
- Claude Code 2.1.0 或更新版本，仅在启用生产 agent 执行时需要

## 从源码安装或升级

```powershell
git clone https://github.com/Ctrl1CandV/Atlas.git
cd Atlas
uv sync --locked --all-groups
npm --prefix web ci
npm --prefix web run build
```

也可以从 [v0.1.0 Release](https://github.com/Ctrl1CandV/Atlas/releases/tag/v0.1.0) 下载 Atlas 自定义源码 sdist（附 `SHA256SUMS` 与构建来源证明）。GitHub 自动生成的 Source code 归档、Git clone 和该 sdist 是三种不同输入；`v0.1.0` sdist 不含项目级 `.mcp.json`、英文 README 与页面截图，使用 MCP 时应优先 clone 仓库或手动配置 `atlas-mcp`。当前分支已修正下一版 sdist 的内容清单，但不会静默替换既有 `v0.1.0` 资产。

升级时先更新源码并阅读 `CHANGELOG.md`，再重复锁定依赖同步和 Web 全新安装。运行配置保留在本机；修改活动配置前先比较新版 `config/*.example.json`。

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

打开 <http://127.0.0.1:8321>。服务必须留在回环地址；当前没有多用户认证或远程部署安全模型。

### 在 harness 中使用 MCP

Git clone 自带 [`.mcp.json`](.mcp.json)。用 Claude Code（或其他支持项目级 MCP 配置的 harness）打开克隆下来的 Atlas 仓库，六个 MCP 工具会通过 stdio 自动启动，无需手动开终端。配置执行的是 `uv --directory . run atlas-mcp`，以仓库根目录为工作目录，只要先完成 `uv sync --locked --all-groups` 即可使用。已发布的 `v0.1.0` sdist 不含该文件；下一版 sdist 内容清单已补入。

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
npm --prefix web test
npm --prefix web run lint
npm --prefix web run build
uv build --sdist
```

真实供应商测试默认排除、可能收费且必须主动运行。参见 [`CONTRIBUTING.md`](CONTRIBUTING.md) 与 [`CHANGELOG.md`](CHANGELOG.md)。

## 许可证

Apache License 2.0。见 [`LICENSE`](LICENSE) 与 [`NOTICE`](NOTICE)。
