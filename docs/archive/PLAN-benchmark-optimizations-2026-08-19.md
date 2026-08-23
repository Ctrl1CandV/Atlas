# Atlas 稳定性升级路线：对标开源基准（2026-08）

状态：**2026-08-19 批次收窄（所有者决定）：阶段 C 与精简批次一（P0min + P1 + P6，
13–20 人日）已在本地收口；首轮 reviewer 指出的成本、SSE、YAML、resume 准入和文档
blocker 均已修复并通过最终本地闸门与 REVIEW-004 独立复核。阶段 D 已于 2026-08-19
经所有者再次确认后执行完毕（全程经 MCP stdio 真实运行；发现并修复两个 agent
生产 blocker——CLI 契约正则误判与 CLI 用户 settings 劫持端点；发生一次 Kiro
sonnet-5 超支事件，详见 `PLAN-rc1-followup.md` §7.1）。tag、产物上传与
provenance 由所有者自行执行；公开仓库已按所有者决定移除开发产物，远端 CI 不在
公开仓库运行。发布后保留 P2/P3/P4/P7/P9/P10/P11/P13 并与阶段 E 并轨；
P5、P8、P12、P14 已移除（理由见 §7.4）。本文同时记录实施合同与当前完成状态。**
本文保留 **2026-08-18 外部调研结论**，但 Atlas 现状、代码锚点、缺口和实施方案均以
**2026-08-19 当前 HEAD（`8c71b6b`）源码事实**为准，阶段 D 修复后本地测试基线为
427 passed、1 skipped、5 real_api deselected（新增 CLI 契约排版回归）。外部机制不是
Atlas 当前能力；源码事实也不反向改写外部调研时点。实施中若 HEAD 已变化，应先复核符号，
再更新本文，不能把近似行号当成稳定 API。

定位：本路线不改变 rc.1 红线：本地单机、仅回环访问、事件流为运行事实来源、
完整性与治理错误 fail-closed、路由只查表不猜。UI 边界应准确表述为：
**工作流定义和图画布只读；启动运行、人工审批、删除运行记录和配置管理可通过
受控本地 API 写状态。** 因而本文拒绝的是 Web 图编辑器，不是所有 UI 写操作。

收窄后仍在路线内的十一项（P0min、P1、P6、P2、P3、P4、P7、P9、P10、P11、
P13）合计约 **45–70 个熟悉代码库的单人开发日**：D 前精简批次约 13–20 人日，
发布后保留项约 32–50 人日。不得作为一个 PR 或一个版本一次性交付。

---

## 1. 任务、时间边界与使用方式

### 1.1 目标

Atlas 的 YAML 图、MCP 控制面、本地 Web、成本守卫和审计账本，在 TrueForge、
Enju、BoundFlow、agent-blueprint、Dify、n8n 以及 LangGraph、Temporal 等项目中
都有可比较机制。本路线的目标是：

1. 保留 2026-08-18 对成熟引擎和同类编排器的具体机制调研；
2. 用 2026-08-19 Atlas HEAD 重新判断哪些能力已经领先、哪些仍是缺口；
3. 保留 P0–P14 编号作为调研追踪号，但只实施所有者保留的十一项；
4. 区分 D 前精简批次、发布后保留项和永久移除项，避免把调研清单误当成交付承诺。

结论：Atlas 的事件、产物完整性、执行身份、审批证据和本地并发控制已较强；本次
精简批次已补齐**崩溃后的产品化恢复、LLM reservation 持久化和 YAML 语义错误行列**。
当前保留的发布后主缺口是**取消、MCP 异步、节点级容错、可复用调试和生命周期管理**。

### 1.2 两类事实必须分开引用

- **2026-08-18 外部调研结论**：第 2、4、8 节。它描述当时读取到的上游文档和
  源码；实施前应复核上游版本。
- **2026-08-19 当前 Atlas 源码事实**：第 3 节及第 5 节的“当前锚点”。符号名是
  主引用，行号仅为 HEAD 上的近似导航。
- **计划性设计**：第 5–7 节。字段名、事件名和 API 是批准的目标合同，不代表
  当前已经实现。

### 1.3 当前验证基线（2026-08-19 最终本地闸门，REVIEW-004 已通过）

- 源状态：最终候选以承载本文、检查单和 §11 实施证据的同一提交为准；不得用提交前的
  `8c71b6b` 或含额外未提交改动的工作树替代。
- 后端：`uv run pytest` 在 Python 3.14.6 与 `uv run --isolated --python 3.12 pytest`
  在 Python 3.12.9 均 **426 passed, 1 skipped, 5 real_api deselected**；包含未知费率
  reservation、真实子进程强杀、动态 interrupted、Windows WAL 恢复、resume 状态优先、
  YAML 重复键/alias/资源上限和非法 Unicode 回归。
- 前端：`npm --prefix web test` **22 passed**、oxlint 0/0、build 成功；包含 interrupted
  控制通知、持久游标单调和同 run 重新订阅回归。
- 严格六工作流离线闸门（`scripts/release_workflow_gate.py`）：6/6、registry/runner
  预检各 6、0 供应商调用、0 agent 调用、0 run 目录。
- clean-init 闸门：两次真实 `atlas init` 均成功且幂等，六模板逐字节一致，
  `agents.runner=fail_closed`，MCP stdout 0 字节。
- sdist 闸门：**173 条目、0 发现**；Python 3.12 离线安装完整锁定依赖，核心模块导入、
  spec 解析、六 MCP 工具与配置初始化全部通过。
- fresh-source README 全路径走查仍沿用阶段 C 的隔离证据：`uv sync --locked
  --all-groups` → `npm ci`（0 漏洞）→ `npm run build`；升级路径二次 sync 幂等；
  `atlas-web` 200/6 工作流，六个活动配置自动创建且 `fail_closed`。
- 远端 GitHub Actions、tag、发布上传、provenance 与阶段 D 真实供应商调用均未执行；
  以上全部是本地、无付费证据。首轮 reviewer blocker 已修复，REVIEW-004 独立复核通过；
  远端 CI、tag、上传与 provenance 仍须实际执行后才能宣称远端发布完成。

实施每个后续保留项时仍须在同一源状态重跑适用的完整无付费测试，不能沿用上述
数字替代该项证据。

---

## 2. 基准全景（2026-08-18 外部调研结论）

| 项目 | 定位 | 与 Atlas 的关系 |
|---|---|---|
| [TrueForge](https://github.com/truefoundry/trueforge) | agent harness、YAML 目录、人类检查点、聊天 UI、本地单进程 | 功能面最接近 |
| [Enju](https://github.com/tamerh/enju) | 人与 agent 同跑 YAML DAG，单二进制 MCP + Web，git 审计 | 同为 MCP + YAML + human gate |
| [BoundFlow](https://github.com/boundflow/boundflow) | 审批、成本治理、持久执行、审计回执 | 治理与恢复参照 |
| agent-blueprint（PyPI） | YAML 编译到 LangGraph，声明式 policies | 预算和审批 schema 参照 |
| [Dify](https://github.com/langgenius/dify) | 低代码 LLM 应用平台 | 节点错误策略和调试 UX 参照 |
| [n8n](https://docs.n8n.io) | 工作流自动化平台 | 错误处理、钉扎、部分执行、保留策略参照 |
| [LangGraph](https://github.com/langchain-ai/langgraph) | 图执行框架，也是 Atlas 底层 | checkpoint、interrupt、重试语义来源 |
| [Temporal](https://temporal.io) | 持久执行引擎 | 事件溯源、重试、心跳、版本化参照 |
| [Azure Prompt flow](https://microsoft.github.io/promptflow/) | YAML LLM flow + 评测 | 连接、单节点测试、run-on-run 参照 |
| [CrewAI Flows](https://docs.crewai.com/concepts/flows) | 轻量流编排 | persist/fork/human feedback 参照 |
| [Prefect](https://docs.prefect.io/v3/how-to-guides/workflows/retries) / [Dagster](https://docs.dagster.io/deployment/execution/run-retries) | 数据流编排 | 退避、缓存、from-failure 参照 |

---

## 3. Atlas 现状评估（2026-08-19 当前 HEAD 源码事实）

### 3.1 已有资产：后续不得回退

1. **Append-only 事件流和撕裂尾恢复**：`EventLog`、`EventReader`、
   `fold_events`（`atlas/events.py`，约 146 行）。状态重放不读派生缓存，seq 和
   增量读取已有测试约束。
2. **类型化 write-once 产物和读回哈希断言**：`store_artifact`、
   `read_artifact`、`artifact_entry`（`atlas/integrity.py`、`atlas/artifacts.py`）。
   节点输入投影先验证消费产物，再调用模型。
3. **成本账本已有可重放 reservation 状态机**：`CostReservation`、
   `fold_cost_accounting`、`CostLedger`（`atlas/costs.py`）。只有设置 `max_cost_usd` 时
   才创建 reservation：可计算 projected 时预留 projected，费率未知时调用
   `reserve_remaining()` 预留全部剩余额度；未决 reservation 在崩溃重放时保守计入。
   无 cap 时不创建 reservation、不写 `cost_reserved`，也不虚构预留金额，并保留旧无 id
   事件兼容。
4. **agent attempt 的保守成本处理**：`make_agent_node_fn`（
   `atlas/nodes/agent.py`，约 622–755 行）遵循同一 cap 边界；有 cap 时持久化 reservation，
   未知费用按预留全额 accounted、不释放重用；无 cap 时只记录可信实际值或 unknown，
   不虚构 reservation/金额。
5. **执行身份三层哈希**：`prepare_execution`、
   `_check_persisted_execution_identity`（`atlas/engine.py`，约 122–240 行）绑定
   spec、registry、runner 和 credential revision；preview→run、resume、approve
   能拒绝后端漂移。
6. **假成功检查和失败链**：`call_with_fallback`（`atlas/adapters.py`，约
   471 行）区分传输失败、空输出、截断和必填字段缺失；熔断器只统计传输失败。
7. **审批证据已强化**：`_verify_approval_material`（`atlas/engine.py`，约
   975–1063 行）在锁内校验投影哈希、消费产物、`baseline_digest`、
   `result_digest`、`patch_digest`，并要求投影证据键集完整覆盖；P11 必须复用，
   不能另开弱审批路径。
8. **稳定 per-run 跨进程 OS 锁**：`acquire_run_lock`/`release_run_lock` 和
   `runs/.locks/<rid>.lock`（`atlas/engine.py`，约 1182 行以后）。锁文件永久存在，
   文件存在不等于持锁，不按 mtime/TTL 抢占。
9. **安全删除骨架已存在**：`_delete_run_locked`（`atlas/web.py`，约 422 行）
   持 run lock 后把终态目录同卷 rename 到 `.trash/<rid>` tombstone，再 no-follow
   清理并把 sharing violation 转成受控错误。P10 必须复用，不能另造删除器。
10. **confirm-before-run 和零成本预检**：`expected_execution_sha256`、
    `dry_run_impl`、Web preview。有效规格和后端预检可在 run_id/付费前失败。
11. **MCP 保存边界需准确描述**：`save_workflow_impl`（`atlas/mcp.py`，约
    135–235 行）对新建用 `os.link` 提供跨进程原子占位；更新使用进程内
    `_SAVE_LOCK`、哈希复核和 `os.replace`，**不是跨进程线性化 CAS**。
12. **UI 是受控本地操作面，不是纯只读站点**：`atlas/web.py` 和 `web/src/`
    已支持启动、审批、终态删除和配置写入；工作流定义/画布保持只读。

### 3.2 缺口 G1–G13

| # | 当前事实与符号锚点 | 影响 |
|---|---|---|
| G1 | **已由 P1 关闭**：公共 `resume_graph`、Web `POST /api/runs/{rid}/resume` 与 MCP `atlas_resume_run` 只准入锁内复核为 interrupted 的运行；paused/终态/活跃运行拒绝且不追加账本 | checkpoint 恢复已产品化，仍保持 human gate 独立语义 |
| G2 | **已由 P1 关闭**：`derive_run_status` 以事件状态、本地 controller 与稳定 OS run lock 动态派生 `interrupted`；Web 列表/详情/SSE 与 MCP 查询同源，不写伪造事件 | 被 kill 的控制器不再让运行永久显示 running；锁探测不确定时仍 fail-closed |
| G3 | **无取消合同**：无 cancel request、cancellation token、`run_cancelled` 终态 | HTTP 调用只能等返回/超时，CLI 只能靠外部终止或 deadline |
| G4 | **MCP 真跑同步阻塞**：`run_workflow_impl`（`atlas/mcp.py`，约 339 行）同步到 done/paused/failed；Web 已用后台线程。两者只共享 `derive_run_status`，尚未共享 launcher 或同构 summary | 长工作流占住 MCP 会话，启动与汇总语义仍可能分叉 |
| G5 | **节点失败默认整图失败**：`_invoke`（`atlas/engine.py`，约 1273 行）最终落 `run_failed`；无 `on_error`、soft-failure 重放或错误分支 | 已完成结果不能有策略地继续使用 |
| G6 | **两条 retry 路径均为固定等待**：LLM `call_with_fallback` 固定 0.5s（`atlas/adapters.py`，约 563–572 行）；agent 固定 2s（`atlas/nodes/agent.py`，约 761–762 行） | 无配置退避；两条路径行为不同且等待不可取消 |
| G7 | **已由 P6 关闭**：`atlas/spec.py` 使用 parse-local 路径→mark 旁路表，`SpecError` 结构化携带 path/line/column；快照与指纹不包含源码位置 | Web/MCP 可直接指出语法和主要语义错误的字段路径与一基行列；聚合错误不编造位置 |
| G8 | **无安全的跨 run 产物复用**：run 请求没有 import/pin/invocation hash | 调试长图需重付上游成本；直接引用旧 run 路径又会与删除冲突 |
| G9 | **无 token 守卫**：只有 `max_cost_usd`；费率未知时 LLM 美元守卫覆盖不足，agent usage 也不保证可计量 | 无 pricing 时缺少独立用量上限；若把 unknown 当 0 会产生假安全 |
| G10 | **长调用无 controller 进度事件**：只有 node_started/node_done；SSE keepalive 仅证明连接循环活着 | UI 无法区分 controller 正在等待调用与事件流断开 |
| G11 | **无自动保留策略和列表索引**：`list_runs`（`atlas/web.py`，约 398–420 行）逐目录全量 fold；已有手工删除/tombstone，但无 age/count/star | runs 增长后磁盘和列表延迟持续上升 |
| G12 | **human 只有二值审批**：`_make_human_node_fn` 和 `approve_run`（`atlas/engine.py`，约 659、1102 行）只认 approve/reject；现有强摘要验证不可绕过 | request_changes 不能作为显式控制流返回生产者 |
| G13 | **已由 P0min 关闭**：有 `max_cost_usd` 时，LLM 每次真实派发按 projected 预留，费率未知则 `reserve_remaining`，并写带独立 id 的 `cost_reserved`；无 cap 时不创建 reservation/金额。结算记录 actual/accounted/unknown/usage，有 reservation 的未知和崩溃窗口按预留额保守占用 | 有成本帽的恢复不再重复释放可能已花费的预算；无帽路径保持诚实 unknown；旧无 id 事件仍兼容 |

---

## 4. 参考机制详录（2026-08-18 外部调研结论）

本节保留调研细节，但不声称这些字段在 Atlas 已实现。

### 4.1 成熟执行引擎

**LangGraph**（来源：`libs/langgraph/langgraph/types.py`、`pregel/_retry.py`、
docs.langchain.com）

- `RetryPolicy`：`initial_interval=0.5s`、`backoff_factor=2.0`、
  `max_interval=128s`、`max_attempts=3`、`jitter=True`（附加
  `uniform(0,1)`），`retry_on` 可按异常类/谓词过滤，也支持策略列表；重试间
  清空 `task.writes`，避免部分写污染状态。
- `interrupt()`：抛内部异常、保存 checkpoint，同 `thread_id` 用
  `Command(resume=value)` 恢复；整个节点从头重跑，interrupt 前副作用必须幂等。
- checkpoint 谱系：`CheckpointTuple.parent_config`、metadata `parents` 和
  `pending_writes` 支持 fork 与在途写区分。图结构变化不必然阻止旧 thread 续跑，
  但 resume 点前任务/interrupt 顺序变化会错配；Atlas 的 execution identity 更严。
- 新版 TimeoutPolicy 区分 `run_timeout` 与 `idle_timeout`，心跳可刷新 idle timeout。

**Temporal**（来源：docs.temporal.io encyclopedia）

- Event History：工作流代码产生 Command，服务端 append-only 落事件；崩溃后重放，
  已记录 activity 结果和 timer 不重算。
- Activity RetryPolicy 默认：`InitialInterval=1s`、`BackoffCoefficient=2.0`、
  `MaximumInterval=100×initial`、`MaximumAttempts=∞`；
  `ApplicationError(non_retryable=True)` 短路，也可用 `next_retry_delay` 覆盖。
- 长 activity 心跳可携带下一次 retry 所需载荷；节流约
  `min(heartbeatTimeout×0.8, 60s)`；取消在心跳/协作点送达，不等于任意时刻强杀。
- 版本化通过执行历史和 patch marker 让旧执行按旧逻辑完成。

**n8n**（来源：docs.n8n.io）

- Node Settings：`Retry On Fail`（maxTries/waitBetweenTries）和
  `On Error: StopWorkflow | Continue | Continue (error output)`。
- Error Workflow：以 Error Trigger 开始，接收 execution id/url/retryOf、error、
  lastNodeExecuted、workflow id/name；一个错误工作流可服务多个图。
- pin-and-mock：可 pin 并编辑节点 JSON，手动运行跳过该节点；Execute step 只运行
  目标节点和补齐输入所需祖先。
- 执行数据按年龄（默认 336h）和数量（10k）修剪；annotated execution 不修剪，
  硬删除有缓冲期。
- manual/partial/production 三种执行模式，历史执行只读，可把数据拷回调试环境。

**Azure Prompt flow**（来源：microsoft.github.io/promptflow）

- `pf flow test --node <name> [--debug]` 单节点测试；`--ui` 可启动交互界面。
- 连接实体把 configs 与 secrets 分离，回读脱敏，可使用 OS keyring 或 env fallback。
- 评测可消费旧 run：`pf run create` 指向基线 run 的输出，再提供逐行详情、聚合指标、
  archive/restore。

### 4.2 同类编排器

**TrueForge**（来源：仓库与 truefoundry.dev）

- 审批 turn 以 `tool.approval_required` 结束，携带 tool call id 和 source event id；
  恢复使用独立的 user tool approval 输入，审批不能和普通消息混发。
- 断线重连持久化 session/turn/lastSequenceNumber，通过 after sequence 或
  `Last-Event-ID` 增量重放。
- 本地模式是单进程 + SQLite、无登录、目录 YAML。

**Enju**（来源：仓库）

- task action 为 answer/review/vote/compute，人和 agent 是同一 DAG 的对等节点。
- review 支持 approve/request_changes/reject；request_changes 携反馈重新入队生产者，
  生命周期为 pending→ready→claimed→running→review→done。
- 每次 attempt 用 git commit 审计，协调器只管理状态和决定。

**BoundFlow**（来源：仓库）

- 审批作为返回值：`AwaitApproval(on_approve=Next(...), on_reject=Complete(), ...)`，
  状态持久化后从暂停点恢复。
- 三层治理：运行中 RuntimePolicy；运行后基于成本/时延趋势切模型；工作流失败趋势
  可回滚、暂停或 cooldown。
- 持久执行使用 checkpoint + lease，worker 崩溃后可由另一 worker 续跑。

**agent-blueprint**（来源：PyPI 文档）

- `policies.approvals` 有 selective/all、tools 和 block/warn。
- `policies.budgets` 有 per-run token、latency、cost；token 可中途熔断并发
  `policy_violation`。
- state invariants 在 agent 节点后重查，另有 tool call 上限和低置信度升级路径。

**Dify**（来源：docs.dify.ai）

- 节点错误策略：None、Default Value、Fail Branch；错误分支暴露 type/message。
- iteration 错误模式：terminated、continue-on-error（null）、
  remove-abnormal-output。
- 调试有单节点运行、Variable Inspector、编辑缓存后重跑节点、Last run 输入/输出/耗时。
- 版本有 Draft/Latest/Previous，Restore 把旧版本载回草稿。

**CrewAI / Prefect / Dagster**（交叉验证）

- CrewAI：`@persist` + UUID 恢复；`restore_from_state_id` fork 新 id；human feedback
  可归类为 approved/rejected/needs_revision。
- Prefect：retry delay 可为标量、列表或 exponential backoff，另有 jitter factor 和
  retry condition；缓存键可组合 inputs 与 task source，并有 expiration/refresh。
- Dagster：`ALL_STEPS | NONE | FROM_FAILURE`；from-failure 依赖 IO manager 持久化
  已完成输出，只重跑失败步。

---

## 5. 保留方案与已移除提案（沿用 P0–P14 追踪号）

通用约束：所有新增执行字段必须进入规范化 spec 和 execution identity；展示型字段才可
留在 `meta`。所有新事件必须定义旧 reader 的兼容行为；状态、成本、soft failure 和
lineage 必须能从事件重放，不允许只存在于 Web 内存。Web/MCP 应复用同一领域函数，
不得各自实现一套状态判断。

### P0min 最小 LLM reservation 持久化（G13）〔已实现，本地闸门全绿〕

**依赖/优先级：仅作为 P1 的财务安全前置；不借机重构 agent 或统一 accounting API。**

- 当前锚点：`CostLedger`/`fold_cost_accounting`；LLM `_make_node_fn._reserve/_settle`；
  agent 路径只作为现有事件合同参考，本批不改。
- 方案：
  1. 仅在设置 `max_cost_usd` 时创建 reservation：可计算 projected 时调用
     `CostLedger.reserve(projected)`；费率未知时调用 `reserve_remaining()` 原子预留全部
     剩余额度。随后同步写 `cost_reserved`（node、iteration、attempt、model、
     `reservation_id`、`reserved_usd`）；事件写入失败时不得调用 provider。无 cap 时
     reservation 为 `None`，不写 `cost_reserved`，也不虚构金额。
  2. 成功、传输失败、假成功和已返回但不可计量的 attempt 均写 `cost_settled`，包含
     `actual_cost_usd`、`accounted_cost_usd`、`cost_unknown` 和 usage；存在 reservation
     时沿用同一 id，未知费用按该 reservation 全额保守计入。无 cap 时 id/accounted 可为
     null，只记录可信实际值或 unknown。
  3. 有 cap 且 reserve 后、settlement 前崩溃时，`fold_cost_accounting` 把 outstanding
     全额计入 accounted；恢复以重放后的 accounted 作为已花费，不能重复释放预算。
  4. 兼容旧无 id 的 LLM `cost_settled` 和旧 `node_done.cost_usd`；不改变 snapshot、
     execution identity、agent 账本和 Web/MCP 汇总结构。
- 验收：有 cap 时真实子进程在 reservation 落盘后被 kill，重放仍保守占用；
  retry/fallback 每个实际派发有独立 id 且恰好结算一次；无 cap 时没有 reservation 事件
  或虚构金额；旧事件与 agent 回归不变。

### P1 仅恢复动态判定的 interrupted run（G1、G2）〔已实现，本地闸门全绿〕

- 当前锚点：`resume_graph`、`fold_events`、run lock、Web `_run_threads`。
- 方案：
  1. 建立 Web/MCP 共用 `derive_run_status`。`interrupted` 是**动态派生视图，
     不写伪造终态事件**：账本 fold 为 running、当前入口无活跃本地 controller、
     且对稳定 run lock 的非阻塞探测确认无人持有时，才显示 interrupted。探测失败仍是
     running；无法确认时 fail-closed，不提供 resume。
  2. `resume_run` 在真正启动前重新取得排他 run lock，并在锁内重读事件、checkpoint、
     spec snapshot、完整 execution identity，再确认调用前状态确实满足 interrupted。
     只有该动态状态可调用 `resume_graph`。
  3. **paused 永远只能走 approve/reject（P11 后含显式决策）**；resume API 对 paused、
     done、failed、cancelled 一律 409，不能绕过 human gate。
  4. Web 增加 resume 入口；MCP 增加 `atlas_resume_run`。两者复用领域级锁内
     interrupted 准入与 `derive_run_status`；共享 launcher、MCP 异步及同构 summary
     不属于 P1 已完成范围，留给发布后 P4。
  5. 可提供显式“放弃 interrupted run”操作，但必须写可审计终态（例如
     `run_failed`/专用 abandoned event），不能只改 UI 标签。
- 验收：用独立子进程执行慢节点并由父进程强制 kill，重启控制面后动态显示
  interrupted，恢复只重跑 checkpoint 未完成工作；普通 Python 异常不能冒充崩溃测试；
  paused 的 resume 在任何入口都被拒，approve 仍通过现有摘要和身份校验。

### P2 原子请求 + cancellation token 的协作式取消（G3）

- 依赖：P0；复用 P1 的共享状态派生，并在 P4 落地后再与共享 launcher 集成。
- 方案：
  1. 取消请求不能等待当前执行持有的排他 run lock。使用 run 目录内原子
     create-if-absent 请求记录（含 requested_at/reason/request id）和进程内
     cancellation token；创建必须抗并发、跨进程可见。
  2. 首次对 running/interrupted/paused 请求返回 202/200；重复请求幂等返回当前
     cancellation 状态，不报冲突；只有 done/failed/cancelled 等既有终态请求返回 409。
  3. controller 在节点入口、候选切换、retry 等待、checkpoint 边界检查 token。
     retry sleep 改为可唤醒等待。
  4. local CLI runner 保存受控子进程句柄，收到取消可调用既有进程树终止机制；HTTP
     模型 SDK 首版不宣称能强杀在途请求，只能等调用返回或 timeout，再消费 token。
  5. paused 无 worker：取消端点在写请求后取得 run lock，复核仍 paused，再直接写唯一
     `run_cancelled`。running 由 owner 写终态，防止两个 writer 并发写事件。
  6. 取消后的未决 reservation 按 P0 保守计入，不因取消释放；`fold_events` 支持
     cancelled，删除器把 cancelled 视为终态。
- 验收：并发重复 cancel 只产生一个终态；CLI 进程树被终止且无遗留子进程；HTTP
  调用明确在返回/超时后取消；cancel 与 approve、resume、自然完成竞争时只有一个合法
  终态，事件 seq 和 accounting 可重放。

### P3 异常分类后再提供节点级 `on_error`（G5）

- 依赖：先完成异常 taxonomy；P2 的 cancellation 必须归类。已移除的 P8 不是硬依赖。
- 分类合同：
  - **治理/控制异常（永不可吞）**：CostExceeded、GuardViolation、run deadline、
    RunCancelled、Spec/Integrity/Wiring/NoRoute、
    approval rejection、checkpoint/内部 invariant。
  - **节点内容异常（可按策略处理）**：LLM 候选全部失败、假成功耗尽 fallback、明确的
    node-local timeout。
  - **agent failure（单独类别）**：AgentCliError 及其受控子类；只有图作者显式允许才可
    soft-fail，安全扫描、baseline/diff 完整性错误仍提升为不可吞。
  - **deadline 拆分**：节点 timeout 可被策略处理；`guards.timeout_s` 的 run deadline
    永远终止整图，不能复用同一个宽泛 TimeoutViolation 模糊判断。
  - `TokenLimitExceeded` 仅保留为未来若重新立项用量治理时可能采用的治理异常类别；
    当前未实现，P3 不依赖已移除的 P8。
- YAML `on_error` 闭合枚举：`stop`（默认）、`continue`、`branch`。continue 写
  write-once 错误产物；branch 只走保留键 `__failed__`，校验期要求对应条件边。
- 每次 soft failure 写 `node_failed_soft`，含分类、错误摘要、iteration、错误产物 hash、
  选择策略；`fold_events` 必须重放出节点 `failed_soft` 和产物。不得只在 get_run 临时算。
- 验收：内容/允许的 agent failure 可 continue/branch；所有治理、路由、完整性和 run
  deadline 在配置 continue 时仍整图失败；旧事件可读，新事件 fold 与运行状态一致。

### P4 MCP 异步运行和运行列表（G4）

- **硬依赖顺序：P1 → P4。** 没有 interrupted 恢复，不开放 `wait=false`。
- `atlas_run_workflow(wait: bool = true)` 保持默认同步兼容；`wait=false` 完成有效规格、
  prepared identity、run id 和锁准入后交共享 launcher，立即返回 run id/status。
- Web start、MCP start/resume、approval continuation 共用 launcher registry、
  `derive_run_status` 和 summary builder；不能继续保留 Web/MCP 两套节点/成本汇总。
- 新增 `atlas_list_runs(limit, cursor)`；状态必须包含 starting/running/interrupted/paused/
  done/failed/cancelled，并与 Web 列表同源。
- stdio MCP 进程退出会终止其线程，后续由 P1 显示 interrupted 并恢复；文档不得把
  daemon thread 描述成独立服务。
- 验收：wait=false 在预检后快速返回，轮询到终态；kill MCP 子进程后能从另一进程
  观察 interrupted 并恢复；同步默认和现有调用方无行为变化。

### P5 两条 retry 路径的统一退避（G6）〔2026-08-19 所有者裁定：移除，不排期〕

**移除理由**：当前固定 0.5s（LLM）/2s（agent）退避在本地单人场景完全够用，改成
可配置退避列表的收益在单机上几乎无感。以下为原设计，仅存档，不实施。


- 同时改 LLM 0.5s 路径和 agent 2s 路径，不只改 adapter。
- 保留 `retry` 表示“额外尝试次数”。新增可验证的 delay 配置（标量或正数列表均可，
  最终 schema 只选一种规范化表示）和 `retry_jitter_s`；列表耗尽后复用末项。
- **兼容默认**：LLM 未配置时仍 0.5s，agent 仍 2s；
  `retry_jitter_s` 默认 **0**。只有显式配置才加入随机抖动，不能用 LangGraph 的默认
  jitter 改写 Atlas 现有确定性测试。
- 只对既有可重试类别生效：LLM 仍只重试 transport，不重试假成功；agent 仍只重试
  明确可重试 AgentCliError 子类。等待受 run deadline 和 P2 token 控制。
- Web/MCP node overrides、snapshot 和 dry-run 展示同步支持。
- 验收：两种节点的默认时序回归不变；配置列表、上限、jitter=0 和固定随机源均可测；
  deadline 临近不进入超长等待，取消能立即唤醒。

### P6 YAML 语义错误带行列（G7）〔已实现，本地闸门全绿〕

- 自定义 `yaml.SafeLoader` 在 construct mapping/sequence 时建立“规范路径→mark”旁路表，
  不把 mark 混进 dataclass 或 fingerprint。
- `SpecError` 结构化携带 path、line、column；至少覆盖未知字段、节点字段值、guards、
  悬空边、consumes 和条件路由。整图可达性拿不到唯一位置时只报告相关路径，不编造行号。
- PyYAML 语法错误统一渲染；Web/MCP 使用同一 serializer。
- 验收：坏 YAML 的行列精确；同一 spec 的 fingerprint 与引入 mark 前一致；通过 snapshot
  构造的错误没有源码 mark 时正常降级。

### P7 安全导入产物 + invocation hash（G8）

- 不允许新 run 的 state 指向可被删除的源 run 文件。准入时读取源事件和产物，验证源
  hash/role/大小后，**复制字节到新 run 的 write-once artifacts 目录**，重新验证写后
  hash，再写 `artifact_imported`。
- lineage 至少记录 source_run_id、source logical name、source sha256、new path/hash、
  import time；源 run 随后删除不影响新 run。
- 为每次节点调用计算规范化 `invocation_sha256`：节点执行字段、有效 prompt、按逻辑名排序
  的输入 artifact hashes、相关 prepared backend identity/runner descriptor 和算法版本。
  只有源产物对应 invocation hash 与新运行完全相等，才自动复用/跳过。
- imports、skip plan 和算法版本进入 execution identity；dry-run 显示将复制和跳过的清单。
  不支持手改源产物后跳过哈希断言。
- 验收：导入后删除源 run，新 run 仍可完成/审计；任一输入、prompt、模型、runner 或
  artifact hash 改变即不复用；复制中断不留下可引用半产物；并发删除源 run 时持正确锁
  或明确失败。

### P8 `guards.max_tokens`，agent 不可计量时 fail-closed（G9）〔2026-08-19 所有者裁定：移除，不排期〕

**移除理由**：token 帽与既有 `max_cost_usd` 高度重叠，仅在"无 pricing.json 冷启动"
这一窄场景才有独立价值；发布与 D 用的都是已验证费率的美元帽。以下为原设计，仅存档。


- 归档设计曾依赖 P0 的 attempt reservation/event 模型。若未来重新立项，LLM token
  ledger 可使用派发前估算预留、usage 实值结算和崩溃 outstanding 保守占用；
  `TokenLimitExceeded` 仅是可能采用的治理异常类别预留，不构成当前 P3 的依赖。
- token 口径明确为 input + output；reasoning token 若供应商已包含则不重复加，若协议
  单列则规范化一次。估算和实际口径必须版本化并落事件。
- **首版 fail-closed：含 agent 的图不能启用 `max_tokens`，除非 runner 已声明并通过可靠、
  同口径 usage capability 验证。** 因此只要有效图包含 research/coding_agent 且 runner
  不可计量，配置 `max_tokens` 就在零成本预检失败。绝不把 agent unknown 记 0，也不只发
  warning 后继续。
- 后续 runner 若能证明 usage 合同，再按 capability 显式开放；不能根据某次恰好有 usage
  推断全图可计量。
- 验收：无 pricing 时 LLM 图仍受 token cap；崩溃重放不重复预算；含不可计量 agent 的
  图在 run id 前拒绝；`on_error` 不能吞 TokenLimitExceeded。

### P9 controller heartbeat，而非供应商内部进度（G10）

- 节点派发后由 Atlas controller 定时写 `node_progress`，字段含 node、iteration、
  attempt、candidate/runner、controller elapsed 和 phase（waiting/retry）。文案必须说明
  “controller 仍在等待”，不声称模型内部正在生成或完成百分比。
- heartbeat writer 必须随 attempt/取消/终态停止，且与事件写锁兼容；fold 忽略其状态
  副作用，但 summary 可显示最后心跳。
- 容量按真实口径设计：30s 一条即每节点每天 2880 条；即使每条小于 1 KiB，也约为
  数 MiB/节点/天。当前单事件大小上限不是总账本保留上限，因此需配置合理 interval、
  最小间隔，并由 P10 生命周期策略控制长期增长，不能宣称“16MB 可容纳数百小时”。
- 验收：慢 fake provider/CLI 显示递增 controller elapsed；冻结 provider 线程时 heartbeat
  仍准确表述 waiting；完成/取消后无泄漏线程或迟到事件。

### P10 复用现有 tombstone 删除路径的保留策略（G11）

- 默认 `max_runs`/`max_age_days` 均为 null，即**不自动删除**。
- 增加 star/annotation 保护；候选只包括未 star 的 done/failed/cancelled。running、paused、
  interrupted 永不自动删。
- 清理器只负责选候选，实际删除必须复用现有 tombstone retention/deletion path，即调用
  `_delete_run_locked` 的 run lock、同卷 tombstone、no-follow 清理和可重试错误语义；
  不得直接 `rmtree(runs/<rid>)`，也不得另造第二条删除实现。
- Web/MCP 共用轻量 run 索引，启动时可从事件重建；索引是缓存而非事实来源，损坏时丢弃
  重建。P7 已复制 imported artifact，因此源 run 删除不会制造悬空引用。
- 验收：age/count 同时存在时选择确定；star、非终态不删；清理崩溃留下 tombstone 可由
  既有路径重试；索引与抽样 full fold 一致。

### P11 显式 approval mode/decisions 和 request_changes（G12）

- 不再用“是否声明 `route_field`”隐式开启。human 节点新增显式
  `approval_mode`（默认 binary）和闭合 `decisions`；binary 保持 approve/reject，routed
  可显式加入 request_changes。路由字段固定为审批 decision，校验期要求对应条件边。
- request_changes 必须有非空 comment，并由图作者用有界回边返回生产者；reject 仍终止，
  approve 走批准分支。循环仍受 max_iterations。
- **所有决策，包括 request_changes 和 reject，都先走现有
  `_verify_approval_material` 三摘要/投影/consumed 完整覆盖校验**，再持久化决定；不能为
  第三个按钮新增弱端点。事件记录 decision、comment、projection hash、approved/reviewed
  consumed 和 diff digests。
- Web 按 spec 渲染按钮；MCP/API 同一枚举。旧 human spec 不变。
- 验收：旧二值图兼容；三值路由逐分支可测；request_changes 缺 comment 拒绝；篡改
  baseline/result/patch、role、sha 或 consumed 时三种决定都在写 approval 前拒绝。

### P12 top-level durable failure policy（顶层持久失败策略；G5 的运行级补充）〔2026-08-19 所有者裁定：移除，不排期〕

**移除理由**：失败触发另一工作流是服务端/自动化编排场景，本地单人工具几乎无使用场景，
却属大改（顶层 policy + durable 幂等触发）。以下为原设计，仅存档。


- 这是 **top-level durable failure policy**，不是展示元数据或进程内回调。failure handler
  是执行语义，必须从不参与 fingerprint 的 `meta` 移到**顶层闭合 `failure_policy`**，
  进入 snapshot、spec fingerprint 和 execution identity。
- policy 至少声明 handler workflow、最大链深/递归策略和是否处理 cancelled；默认关闭。
- 主 run 失败后先写 durable `failure_handler_scheduled`，含 handler spec/execution hash、
  payload hash、depth 和确定性 idempotency key；child run 使用由该 key 派生的稳定身份。
  启动后写 `failure_handler_started(child_run_id)`，完成可写 completed/failed。重启扫描
  scheduled 未完成项；即使崩溃发生在 child 创建和 started 事件之间，也由稳定 key/run id
  去重，不能重复触发。
- payload 是 write-once task artifact，包含 parent run、terminal seq、error taxonomy、
  last node 和 workflow identity；handler 使用自己的预算/deadline。
- 验收：在 scheduled 前后、child 创建前后逐点 kill，最终最多一个 child；handler 失败
  不无限递归；修改 policy 会改变 execution identity。

### P13 按 changed node + descendants 失效的 fork（G8）

- 依赖 P7。`fork_from` 不等于“pin 源 run 全部产物”。先比较源 snapshot/invocation hashes
  与新有效规格，得到显式 changed nodes，再在静态图上计算其全部 descendants 的
  invalidation closure。
- closure 内节点及其产物禁止导入/跳过；closure 外、invocation hash 相同且依赖完整的
  已完成产物才按 P7 复制。目标 changed node 必须执行，不能因先 pin 全部而被跳过。
- 并行图只失效受影响分支；join 若依赖 changed 分支则属于 descendant，必须重跑。循环图
  对 SCC 整体失效，再向后闭包，避免只失效环中一个节点造成混合迭代状态。
- `run_started`/lineage 记录 forked_from、changed set、closure、import map 和算法版本；这些
  进入 execution identity 和 dry-run。
- 验收：改单节点模型只重跑该节点和后代；兄弟分支安全复用；join/循环 closure 正确；
  failed 源 run 只能导入事件证明完整且 hash 合法的产物。

### P14 从 run 恢复规范化工作流定义（G8 的定义恢复）〔2026-08-19 所有者裁定：移除，不排期〕

**移除理由**：per-run spec 快照已存在，"取回定义"入口使用频率极低，不足以单列。
以下为原设计，仅存档。


- MCP 提供 restore 操作，读取 `spec.snapshot.json`，经 `spec_from_snapshot` 验证后输出
  **规范化 YAML**，再走 `save_workflow_impl` 的 id 白名单、校验和 expected_sha256 合同。
- 承诺是 `spec_fingerprint`/规范化语义等价，不承诺恢复原 YAML 注释、键顺序、空行、
  anchors 或手工格式。返回结果明确标注 normalized restore。
- 旧 snapshot 缺字段时只按现有兼容规则恢复；无法无歧义规范化则 fail-closed。恢复到已
  存在 workflow 仍需要 fresh expected hash；不要声称更新是跨进程强 CAS。
- 验收：删除定义后恢复，重新解析 fingerprint 等于 snapshot；注释不作为测试要求；坏
  snapshot、目标并发修改、未知字段均拒绝。

---

## 6. 明确不采纳及边界

| 设计 | 来源 | 决定 |
|---|---|---|
| Web 可视化图编辑器 | n8n/Dify | 不采纳。工作流定义/画布保持只读；这不禁止运行、审批、删除、配置的受控写状态 |
| 命令式 workflow deterministic sandbox | Temporal | 不照搬。Atlas 图是规范化数据，以事件、checkpoint 和执行身份约束重放 |
| 队列、多 worker、Redis、租约集群 | n8n/BoundFlow | 当前本地单机不引入；稳定 OS run lock 覆盖当前并发面 |
| LLM 猜路由 | 部分平台 | 永不采纳；路由仍是闭合查表，未知值 NoRouteError |
| 动态拓扑扇出 | LangGraph Send | 当前不采纳，保留静态可分析 YAML；静态并行/map-reduce 足够 |
| 趋势式自动切模型 | BoundFlow | 延后；当前 fallback 处理单 run 降级，历史趋势治理不是本路线核心 |
| 手改产物后继续信任旧 hash | n8n pin | 不采纳；P7 只导入验证后复制的 write-once 字节 |
| unknown agent token 计 0 | — | 明确禁止；P8 已移除，但未来任何用量守卫仍不得把 unknown 当 0 |
| P5/P8/P12/P14 | 本文原提案 | 所有者按本地单人场景复核后移除；仅保留存档，不纳入任何批次 |
| 把 controller heartbeat 称为模型进度 | — | 明确禁止；若实施 P9，只能证明 Atlas controller 仍在等待 |
| 把目录副本称为 OS 沙箱 | — | 明确禁止；local CLI 是同用户进程边界，OS 沙箱另行立项 |

---

## 7. 批次划分：阶段 D 前必做 vs 发布后（2026-08-19 所有者精简决定）

### 7.1 定位重估与划分标准

Atlas 是**本地、单人、Windows 桌面 RC**，不是多用户生产服务端。据此重估后，
所有者于 2026-08-19 做出精简决定：批次一只保留对本地单人场景**必要或收益极大**
的三项，并**永久移除四项价值存疑设计**（见 §7.4）。

批次一（阶段 D 与 v0.1.0 首版发布前）只收两类：

1. **崩溃/断连后付费运行可恢复、可判死**，且恢复后预算账本不重算——否则 D 期间
   一次进程退出就烧掉不可挽回的钱，`max_cost_usd` 帽也不可信；
2. **零风险、显著提速 agent 写 YAML 迭代**的最便宜改进。

其余一切——取消、心跳、MCP 异步、节点容错、调试省钱、生命周期、审批增强——
统一延后到 v0.1.0 发布后，与 `PLAN-rc1-followup.md` 阶段 E 并轨。

估算假设（两批通用）：单位为**一名熟悉当前代码库的工程师的人日**，含设计、实现、
故障/并发测试、契约同步与文档；不引入数据库/队列/多 worker，不跑真实付费模型；
区间上限覆盖崩溃测试暴露的返工。

### 7.2 批次一：D 前必做（v0.1.0 发布前完成，精简版）

| 顺序 | 项 | 范围 | 估算 | 为什么必须在 D 前 |
|---|---|---|---:|---|
| 1 | **P0min 最小 LLM 预留持久化** | 仅 LLM 派发前落 `cost_reserved`、崩溃 outstanding 保守计入；不做统一 accounting 框架重构 | **2–3 人日** | 纯粹作为 P1 的账本前置：恢复后预算不被重算，`max_cost_usd` 在崩溃窗口仍可信。只补 G13 这一个洞，不扩大到 agent 路径重构 |
| 2 | **P1 崩溃续跑 + interrupted 检测** | 共享 `derive_run_status`、Web resume 入口、`atlas_resume_run`、领域级锁内 resume 准入；共享 launcher/MCP 异步/同构 summary 留给 P4；真实子进程 kill 测试 | **8–12 人日** | 最真实的痛点：多节点/agent 长任务跑到一半进程崩了，run 永远显示"运行中"且只能整图重来。`resume_graph` 引擎已就绪且有测试，主要缺产品入口。paused 仍只能 approve/reject，不被 resume 绕过 |
| 3 | **P6 YAML 语义错误带行列** | 自定义 SafeLoader 建路径→mark 旁路表；SpecError 带 line/column；不进指纹 | **3–5 人日** | 最便宜的一项。Atlas 主用法是 agent 写 YAML，行号把"改一轮猜一轮"循环砍半，且零执行风险；可与 P0/P1 并行穿插 |

**批次一合计 13–20 人日。** P0min 与 P1 强绑定（无恢复场景则 P0min 无意义），
一起做或一起不做。批次一全绿 + 所有者重新确认预算与时点 → 启动 D → D 全绿 →
发布 v0.1.0。

### 7.3 批次二：发布后（与阶段 E 并轨，保留项）

以下均有真实价值但可延后到有历史 run 与真实用量之后：

| 项 | 保留理由 | 估算 |
|---|---|---:|
| **P2 协作式取消** | 停损开关；本地单人可先用杀进程/等 timeout 兜底 | 4–6 人日 |
| **P4 MCP 异步 + list_runs** | 长运行不阻塞对话；依赖 P1 恢复能力，随 P2 批次 | 3–5 人日 |
| **P9 controller 心跳** | 长调用可区分"在跑/挂了"；与 P2 共享 attempt 上下文 | 3–5 人日 |
| **P3 节点级 `on_error`** | 节点失败不废整图；大改（需异常 taxonomy），本地"重跑即可"可接受 | 6–9 人日 |
| **P7 产物导入 + invocation hash** | 调试省钱，价值依赖发布后积累的历史 run | 6–9 人日 |
| **P13 fork（依赖 P7）** | 换模型只重跑受影响闭包；同样依赖历史 run | 3–5 人日 |
| **P10 retention** | runs 增长治理；本地磁盘不紧张，现有手动删除先顶 | 3–5 人日 |
| **P11 三值批复** | request_changes 路由；需求驱动，无明确诉求前不排 | 4–6 人日 |

**批次二合计约 32–50 人日**，在 v0.1.0 发布后按需逐项实施，与阶段 E 条目
（llm web_search、release 含构建前端、运行报告打包、OS 沙箱调研、浏览器矩阵补测）
并轨排期。

### 7.4 永久移除（不再纳入任何批次，2026-08-19 所有者决定）

以下四项经本地单人场景复核后**移出路线**，不是延后：

| 项 | 移除理由 |
|---|---|
| **P5 退避重试升级（G6）** | 当前固定 0.5s/2s 对本地单人完全够用；退避列表几乎无感收益，不值得 schema 与两路 runner 改动 |
| **P8 token 守卫（G9）** | 与已有 `max_cost_usd` 高度重叠，仅在"无 pricing.json 冷启动"窄场景有独立价值；D 与发布均用已验证费率的美元帽 |
| **P12 失败触发器（G5 运行级）** | 面向自动化/服务端编排；本地单人工具几乎无使用场景，却是触及执行身份的大改 |
| **P14 从 run 恢复定义（G8）** | per-run 快照已存在，"取回定义"入口使用频率极低，不足以支撑一条 MCP 工具 + CAS 链 |

对应缺口 G5（部分）、G6、G9 在本路线内标记为**接受现状**；若未来出现真实需求，
需重新立项而非从本文恢复。

### 7.5 依赖图和统一闸门

- 硬依赖（保留项内）：`P0min → P1`；`P1 → {P4, P2}`；`P2 ↔ P9`（共享 attempt 上下文）；
  `P7 → P13`；异常 taxonomy → P3。P1 已提供共享 status；共享 launcher 与同构 summary
  由发布后 P4 落地，再供 P2/P4/P10 复用。
- 批次门槛：批次一内部按 P0min→P1→P6（P6 可并行）收口；批次二不得先于批次一与 D。
- P11 必须复用当前 `_verify_approval_material`；P10 必须复用现有 tombstone 删除路径；
  这些是实现审查的否决条件。
- 每个阶段独立要求：
  1. 新旧事件兼容和 fold 等价测试；
  2. 无付费完整后端回归（real_api deselect）及相关前端 test/lint/build；
  3. 至少一个真实子进程 kill/并发竞争测试，不能只用普通异常替代崩溃；
  4. Web/MCP/API/schema/snapshot/dry-run 同步；
  5. 当前测试数量和未验证事项按实际命令记录，不沿用历史数字；
  6. CHANGELOG、用户文档和 skill 在工具/字段实际落地时同步。

---

## 8. 来源索引（2026-08-18 外部调研）

执行引擎：LangGraph（checkpoint base、RetryPolicy、human-in-the-loop，
github.com/langchain-ai/langgraph 与 docs.langchain.com）；Temporal
（docs.temporal.io：workflows、retry-policies、worker-versioning、task-queues、
detecting-activity-failures）；n8n（docs.n8n.io：handle-errors-gracefully、
types-of-executions、pin-and-mock-data、work-with-nodes、manage-execution-data）；
Azure Prompt flow（microsoft.github.io/promptflow：flow YAML schema、CLI、connections、
runs）。

同类编排器：TrueForge（github.com/truefoundry/trueforge、truefoundry.dev）；Enju
（github.com/tamerh/enju）；BoundFlow（github.com/boundflow/boundflow）；
agent-blueprint（pypi.org/project/agent-blueprint）；Dify（docs.dify.ai：error handling、
loop、version control、step run）；CrewAI（docs.crewai.com/concepts/flows）；Prefect
（docs.prefect.io/v3：retries、cache workflow steps）；Dagster
（docs.dagster.io/deployment/execution/run-retries）。

竞品全景检索时点为 **2026-08-18**：TrueForge、Enju、BoundFlow、agent-blueprint、
Orchflow、Marchward、Humand、Approving、Kontrol、Seer、AgentMesh、rp-engine、
zenflow 等；Microsoft MXC（github.com/microsoft/mxc，含 Windows Sandbox 后端）可作为
未来 coding_agent OS 沙箱调研候选，但不属于 P0–P14 当前承诺。

## 9. 实施记录

### 2026-08-19 · reviewer 文档事实漂移 blocker · 修复进行中
- 实际改动：按最新代码校正 P0min 的 cap/reservation 分支、P1 已完成边界、P3/P8 依赖、results 指南、rc.1 当前状态、B5 六工具与最终 sdist 口径；未修改生产代码、CI 或 Git 配置。
- 验证证据：`uv run pytest tests/test_docs_agent_contract.py tests/test_mcp_docs_contract.py tests/test_release_gates.py` 为 32 passed；目标文档 `git diff --check` 通过；陈旧事实扫描无命中（保留的命中均是否定“最终完成”或明确留给 P4 的正确表述）。
- 计划偏差：无功能范围变化；只纠正文档事实漂移。本文已超过 400 行，已触发膨胀阈值，回流 project-planner 评估是否拆分后继 PLAN，本轮不自行拆分。
- 遗留问题：developer 修复与自审已完成，仍待 reviewer 最终复核，不能宣称 blocker 最终关闭；远端 CI、tag、upload、provenance 与阶段 D 均未执行。
