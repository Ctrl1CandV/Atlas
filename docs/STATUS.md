# Atlas 当前状态

> 最后核对：2026-08-23。本文是当前产品与发布事实的入口；历史计划和归档记录不能替代本文。

## 版本与支持范围

- 当前版本：`0.1.0`，支持 Windows 10/11 x64，Python 3.12。
- 当前公开分支：默认分支为 `main`（2026-08-23 迁移完成）；`release/v0.1.0-rc.1` 远端已删除。`main` 上启用 deletion/non-fast-forward 规则集（2026-08-23 强制生效）。公开 CI（`.github/workflows/ci.yml`）在 `main` 上通过。
- 分发方式：Git 仓库与 GitHub Release 中的源码 sdist；未发布到 PyPI，没有 wheel 或预编译安装器。
- Web 只支持回环地址；不支持多用户、远程暴露、Linux/macOS 生产运行。
- Atlas 不依赖 Atlas 托管服务，也不内置遥测；真实工作流通常会调用用户配置的远程模型供应商。

## 已实施能力

| 能力 | 当前合同 |
|---|---|
| YAML 静态图 | `llm`、`research`、`coding_agent`、静态并行、条件路由、有界循环和 human gate；节点 id 拒绝 Windows 保留设备名 |
| MCP 控制面 | 八个工具：validate、save、run（`wait=false` 异步返回 run_id）、list workflows、list runs、get run、cancel run（协作式）、resume interrupted run。run 支持传 `yaml` 全文跑未保存的自定义图（`persist_as` 真跑后固化）；stdio 之外，`atlas-web` 在 `/mcp` 以 streamable-http 提供同一工具面 |
| 共享 launcher（P4） | Web 启动/恢复/审批续跑与 MCP 走同一进程内 controller registry（每 run 唯一 controller）；运行摘要由账本派生，Web/MCP 共用同一构建函数 |
| 协作式取消（P2） | `atlas_cancel_run` 与 `POST /api/runs/{id}/cancel` 写原子请求文件；running 由 controller 在节点入口/候选切换/重试等待消费并唯一写 `run_cancelled`；paused/interrupted 由取消入口持锁直写终态；`cancelled` 可删除、拒绝 resume/approve；在途模型调用与 agent CLI 执行会跑完当次，请求不可撤回 |
| Controller 心跳（P9） | 每次 attempt 的派发窗口内定时写 `node_progress`（node/iteration/attempt/model/elapsed_ms/phase=waiting|retry）；只证明 controller 在等待，不声称模型内部进度或百分比；间隔默认与下限 30s，`ATLAS_NODE_HEARTBEAT_INTERVAL_S` run 级可配（低于下限大声拒绝）；窗口在 attempt 结束/失败/取消/终态后闭合，迟到 tick 被拒绝；fold 显式忽略该类型；容量代价如实计入：30s 一条 ≈ 每节点每天 2880 条（16 MiB 账本治理随 P10） |
| 终局可视化与总结（S1） | 终态 run 顶部零成本终局卡片：每节点一句话回顾（模型/耗时/token/成本/输出首段）+ 时间线，纯账本派生，Web 与 `atlas_get_run` 同源（`build_finale`）；图级 opt-in `summary: {model, prompt_hint?}` 在 run_done 前一次总结调用（进规格指纹与快照），成本走 CostLedger 受 `max_cost_usd` 约束，write-once 产物 + `run_summary_written`；失败记 `run_summary_failed` 不改终态；总结文本标注「LLM 叙述，事实以账本为准」；不做离线报告导出 |
| 工作流文件管理 | Web 页面可删除工作流；保存走 MCP 的 `expected_sha256` 读-改-写闭环（乐观锁防覆盖） |
| 零成本预检 | validate 与 dry-run 不调用供应商；`expected_execution_sha256` 可绑定预演与真跑身份 |
| 可审计运行 | append-only JSONL 事件、write-once 产物、读取时 SHA-256 断言、有效规格快照 |
| 成本保护（P0min） | 有 `max_cost_usd` 时派发前持久化 reservation；未知费率保守占用剩余预算；无 cap 不虚构金额 |
| 崩溃恢复（P1） | 动态派生 `interrupted`；只有 interrupted 可 resume；paused 只能 approve/reject |
| 人工审批 | 暂停条列出待审材料（消费产物 + 完整投影，带 SHA-256）并可放大审阅；驳回必填理由，前端与 API 同步强制 |
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

2026-08-23 公开 CI 基线（`main` @ `7eac07b`，GitHub Actions）：

- Windows 支持平台 job 全链路通过：locked sync、`atlas init`（含 UTF-8 stdio 修复，cp1252 控制台不再崩溃）、Web 测试/lint/build、全套测试（2026-08-26 起为 518 passed，随批次增长）、离线发布门、密钥/路径扫描、sdist 构建 + lock 约束冒烟安装。
- Ubuntu 兼容性信号 job（`continue-on-error`，明确不支持）同样通过：跨平台 agent CLI 桩修复后它反映真实兼容性。
- `real-api.yml` 仅手动触发（`environment: real-api` 保护），不计入常规 CI。

2026-08-22 审查后基线（本地 `docs/post-v0.1.0-release-hardening` 工作树）：

- Python：446 passed、1 skipped（无 symlink 权限账户）、5 个 `real_api` deselected。
- Web：22 tests passed，lint 0 告警，production build 成功。
- MCP streamable-http 端点经真实会话驱动：validate → dry-run → 真实运行 → 人工审批 → run_done 全链路。
- 10 节点 ad-hoc 自定义图真实运行（run `20260822-130740-32a44f`，Deepseek/SuperAI 多模型，含多入口并行、条件路由、有界回边、人工审批与门后节点）：8 个执行节点全部一次通过，361 秒（含 294 秒人工等待），约 2.5k/3.6k tokens in/out，全部产物哈希复验一致。

2026-08-19 阶段 D 历史基线（v0.1.0 发布时）：

- Python：427 passed、1 skipped、5 个 `real_api` deselected；Web：22 tests、lint 0、build 通过。
- 六个 shipped workflow 严格离线 validate/dry-run：0 provider call、0 agent call、0 run directory。
- 当时最终 release sdist：100 entries、0 scan findings；Python 3.12 离线安装、版本、三个 console scripts、当时为六个 MCP 工具。spec parse、配置初始化通过。
- 阶段 D 经 MCP stdio 对 Deepseek、SuperAI、Kiro 执行了示例矩阵、自定义图、agent 与失败路径；结果并非每个模型组合都成功，失败均按真实结果记录。

这些数字是各自源状态的历史证据，不自动证明后续工作树。公开 CI 自 2026-08-23 起存在并可引用（见上）；本地运行仍不等于受保护 GitHub environment 的 real-API job。

## 已知限制与运营教训

- 取消是协作式的：在途模型调用与 agent CLI 执行会跑完当次尝试才在下一节点边界终止；取消请求一经写入不可撤回（控制器死亡后 resume 会在首个节点边界消费它）。Web 界面的取消按钮尚未交付（API 与 MCP 工具已可用）。
- 回边循环的 `consumes` 是静态的：重跑轮输入与首轮相同，不携带触发重跑的审查意见——是"有界重试"而非"按批注修订"。反馈可见需要显式消费 `reviewer.output` 的修订节点；语义改进见 BACKLOG"循环携带反馈"。
- agent 自动 retry 的预算约束尚未落地（RFC 草案 `docs/rfcs/agent-retry-budget.md` 待评审）；`--max-budget-usd` 到 Claude CLI 的映射也未实现。
- human gate 只有 approve/reject；节点失败默认终止整图。
- 没有 run retention/index、跨 run artifact import、invocation hash 或 fork invalidation。
- release sdist 不包含 built `web/dist`，使用者仍需 Node.js 构建前端。
- Claude CLI 当前没有硬 `max_turns` 参数；`seed`/`temperature` 只进请求体，供应商是否尊重未验证。
- 阶段 D 曾出现一次 Kiro agent 首次 attempt 自报约 `$10.508`，随后自动 retry 被人工终止。直接原因是图没有 `max_cost_usd`、本地 pricing 全为 `null`，CLI 预算没有生效。所有真实 agent 示例都应配置预算；自动 retry 的默认策略需先经 RFC 决策，不能在没有评审时静默改行为。
- 本地 pricing 全 `null` 时设 `max_cost_usd` 的实际表现：首节点结算即按预留全额计入，后续节点会被"没有剩余预算"拦截（2026-08-22 run `20260822-113908-531dab` 实证）。要么填入确认过的费率，要么不设帽改用结构性约束控成本。

## 接下来

统一排期与审查问题的通俗解释见 [`PLAN-post-audit-2026-08-22.md`](PLAN-post-audit-2026-08-22.md)；各项实施合同见 [`ROADMAP.md`](ROADMAP.md)。原 rc.1 与 benchmark 计划已经关闭，只保留为历史记录：

- [`PLAN-rejection-reduction.md`](PLAN-rejection-reduction.md) — 减少截断/非法 JSON/成本帽三类拒绝性错误的已评审方向
- [`PLAN-rc1-followup.md`](PLAN-rc1-followup.md)
- [`PLAN-benchmark-optimizations.md`](PLAN-benchmark-optimizations.md)
- [`archive/README.md`](archive/README.md)
