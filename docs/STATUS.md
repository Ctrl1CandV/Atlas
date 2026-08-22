# Atlas 当前状态

> 最后核对：2026-08-19。本文是当前产品与发布事实的入口；历史计划和归档记录不能替代本文。

## 版本与支持范围

- 当前版本：`0.1.0`，支持 Windows 10/11 x64，Python 3.12。
- 当前公开分支：`release/v0.1.0-rc.1`。正式版发布后分支名仍保留 RC 字样，这是仓库管理遗留，不代表包版本仍是 RC。
- 分发方式：Git 仓库与 GitHub Release 中的源码 sdist；未发布到 PyPI，没有 wheel 或预编译安装器。
- Web 只支持回环地址；不支持多用户、远程暴露、Linux/macOS 生产运行。
- Atlas 不依赖 Atlas 托管服务，也不内置遥测；真实工作流通常会调用用户配置的远程模型供应商。

## 已实施能力

| 能力 | 当前合同 |
|---|---|
| YAML 静态图 | `llm`、`research`、`coding_agent`、静态并行、条件路由、有界循环和 human gate |
| MCP 控制面 | 七个工具：validate、save、run、list workflows、get run、resume interrupted run、delete workflow |
| 零成本预检 | validate 与 dry-run 不调用供应商；`expected_execution_sha256` 可绑定预演与真跑身份 |
| 可审计运行 | append-only JSONL 事件、write-once 产物、读取时 SHA-256 断言、有效规格快照 |
| 成本保护（P0min） | 有 `max_cost_usd` 时派发前持久化 reservation；未知费率保守占用剩余预算；无 cap 不虚构金额 |
| 崩溃恢复（P1） | 动态派生 `interrupted`；只有 interrupted 可 resume；paused 只能 approve/reject |
| YAML 位置（P6） | 语法和主要语义错误返回 path/line/column；聚合错误不编造坐标 |
| Agent 执行 | 显式 `runner: local_cli` 才启用；缺配置或预检失败时在创建 run 前 fail-closed |
| Agent 改动证据 | 冻结 baseline，在副本执行，以普通文件字节清单生成完整文本 unified diff，审批绑定三摘要 |
| 本机 Web | 查看运行、输入输出、成本和产物；启动、审批、恢复 interrupted run、删除终态 run、管理本地配置 |

## 不可弱化的安全边界

- Claude CLI 是当前用户身份下的宿主进程，目录副本不是 OS 沙箱。它理论上可访问当前用户可访问的其他路径。
- Atlas 不写 coding agent 的原项目目录；diff 采集不执行 `git add`、filter、hook、attributes、textconv 或 external diff。
- `allow_web: false` 只是不授予 Claude CLI 的 WebSearch/WebFetch；可写 coding agent 的 Bash 仍可能联网。
- `allowed_paths` 只适用于 research 或 `writable: false` 的 coding agent；`--add-dir` 不是只读安全边界。
- 不读取、不打印、不提交 `config/.env`；`runs/` 可能含完整 prompt、源码、输出和审批证据，Git ignore 不等于加密。
- 所有真实花销运行必须先 validate/dry-run；未验证事项不能写成已通过。

## v0.1.0 发布事实

正式 Release：<https://github.com/Ctrl1CandV/Atlas/releases/tag/v0.1.0>。

- annotated tag `v0.1.0` 的 tag object 为 `8da8d822350803ef44dd524a3afa036bc24132fe`，peeled commit 为 `4f9b0b5fb4b14fe0523e1cc47cc5e11597d55a94`；tag 未签名。
- 当前 Release 有三个资产：sdist、SPDX JSON SBOM、`SHA256SUMS`。
- 当前资产由 GitHub Actions run `32254337034` 从 commit `d34d785e1f2203453e62c16fdcc612295d6e8715` 构建，matching attestation ID 为 `41605837`。
- **已知来源差异：tag 指向 `4f9b0b5…`，资产证明指向 `d34d785…`。** 两者之间唯一 tracked 变化是 release workflow，且该文件不进入 sdist；这降低了 payload 漂移可能性，但不能把两个 commit 写成同一个身份。
- 不改写或移动既有 tag 来掩盖差异。后续版本应从 exact tag checkout 并在发布前断言 tag commit 与构建 commit 相等。

完整摘要、哈希和验证边界见 [`release-v0.1.0.md`](release-v0.1.0.md)。

## 验证状态

2026-08-19 在阶段 D 修复后记录的本地基线：

- Python：427 passed、1 skipped、5 个 `real_api` deselected。
- Web：22 tests passed，lint 0，production build 成功。
- 六个 shipped workflow 严格离线 validate/dry-run：0 provider call、0 agent call、0 run directory。
- 当时最终 release sdist：100 entries、0 scan findings；Python 3.12 离线安装、版本、三个 console scripts、当时为六个 MCP 工具;后续分支增至七个(新增 delete workflow)。spec parse、配置初始化通过。
- 阶段 D 经 MCP stdio 对 Deepseek、SuperAI、Kiro 执行了示例矩阵、自定义图、agent 与失败路径；结果并非每个模型组合都成功，失败均按真实结果记录。

这些数字是该源状态的历史证据，不自动证明后续工作树。当前公开仓库没有完整 CI workflow，因此没有可引用的公开 Windows CI 通过记录；本地阶段 D 也不等于受保护 GitHub environment 的 real-API job。

## 已知限制与运营教训

- MCP 真跑默认同步阻塞；没有 `atlas_list_runs`，也没有共享 launcher。
- 没有协作式 cancel。HTTP 调用只能等待返回/timeout；CLI 只能依赖 deadline 或外部终止。
- human gate 只有 approve/reject；节点失败默认终止整图。
- 没有 run retention/index、跨 run artifact import、invocation hash 或 fork invalidation。
- release sdist 不包含 built `web/dist`，使用者仍需 Node.js 构建前端。
- Claude CLI 当前没有硬 `max_turns` 参数。
- 阶段 D 曾出现一次 Kiro agent 首次 attempt 自报约 `$10.508`，随后自动 retry 被人工终止。直接原因是图没有 `max_cost_usd`、本地 pricing 全为 `null`，CLI 预算没有生效。所有真实 agent 示例都应配置预算；自动 retry 的默认策略需先经 RFC 决策，不能在没有评审时静默改行为。

## 接下来

实施顺序、依赖和逐项验收标准见 [`ROADMAP.md`](ROADMAP.md)。原 rc.1 与 benchmark 计划已经关闭，只保留为历史记录：

- [`PLAN-rc1-followup.md`](PLAN-rc1-followup.md)
- [`PLAN-benchmark-optimizations.md`](PLAN-benchmark-optimizations.md)
- [`archive/README.md`](archive/README.md)
