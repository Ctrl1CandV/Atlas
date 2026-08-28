# Atlas 后续路线图

> 状态：2026-08-19 起生效；2026-08-23 深化——R0 闭环，各期补充"落地锚点"（模块/事件/表面/测试级方案）。这里的“计划”都不是当前能力；完成必须以代码、事件兼容、测试和用户文档同时落地为准。

## 1. 排序原则

优先顺序由四件事决定：能否停止真实费用、是否统一 Web/MCP 状态、是否保持事件可重放、是否避免重复实现安全边界。

推荐主线：

1. 发布来源与分支治理；
2. **P4 shared launcher + MCP async/list runs**；
3. **P2 协作式 cancel 与 agent 成本停损**；
4. **P9 controller heartbeat**；
5. **P3 异常 taxonomy + node `on_error`**；
6. **P7 artifact import/invocation hash → P13 fork**；
7. **P10 retention/star/index**；
8. **P11 request_changes/routed approval**；
9. Stage E 与节点通讯文件 RFC 按需求拆批。

P0min、P1、P6 已实施。P5、P8、P12、P14 已移除且不排期；除非出现新的真实需求和独立 RFC，不从旧计划直接复活。

每一批必须满足：旧事件可读、状态可从事件重放、Web/MCP/API 共用领域函数、dry-run 无花销、real-API 默认排除、相关 Python/Web 测试与文档同步。涉及崩溃或并发的能力必须含真实子进程 kill/竞争测试，不能只用普通异常代替。

## 2. R0：发布与仓库治理（✅ 已闭环，2026-08-23）

### 落地记录

- 默认分支已迁移 `main`（7eac07b），`release/v0.1.0-rc.1` 远端已删除；`main` 上 deletion/non-fast-forward 规则集已强制启用（2026-08-23）。
- 公开 CI（ci.yml）在 `main` 双 job 全绿；README 挂 CI 徽章；`release` environment 必须人工批准（required reviewer 已配置）。
- tests/scripts 已公开；release-assets.yml 保留全量验证链。
- `v0.1.0` tag 与资产来源差异保持 as-built 披露，不改动。

### 遗留合同（下次发布 v0.1.1 执行）

1. 不移动、不重签 `v0.1.0`；发布记录保留 as-built truth。
2. `v0.1.1` 从 exact tag checkout 构建，断言 `HEAD == tag^{commit}`，版本与资产名从 tag 派生。
3. 发布资产默认不可覆写；修复用新 patch version。

### 下次发布验收

- `git rev-list -n 1 <tag>`、workflow `head_sha`、provenance `gitCommit` 三者完全相等。
- 下载资产逐项匹配 `SHA256SUMS` 和 attestation subjects。
- clean Python 3.12 冒烟安装使用下载资产，而不是本地另建的同名文件。
- Release 正文准确标注 source-only、Windows、无 PyPI/installer 和已知边界。

## 3. R1 / P4：共享 launcher、MCP 异步与 `atlas_list_runs`

### 价值

长任务不再占住整个 MCP 会话；同时先统一运行启动与汇总，避免随后 cancel、heartbeat、retention 各自复制 Web/MCP 逻辑。

### 当前缺口

`atlas_run_workflow` 同步到 done/paused/failed；Web 使用后台线程。两者共享 interrupted 状态派生，但不共享 launcher、controller registry 或同构 summary。

### 依赖

P1 已完成。P4 应先于 P2；否则 cancel 会被迫接入两套 controller 生命周期。

### 实施合同

1. 抽出进程内 shared launcher/controller registry，供 Web start、MCP start/resume 和 approval continuation 使用。
2. `atlas_run_workflow(wait: bool = true)` 保持默认兼容；`wait=false` 只有在完整 preflight、execution identity 和 run lock 准入后才返回 `run_id/status`。
3. 新增 `atlas_list_runs(limit, cursor)`；与 Web 共用 summary builder 和稳定分页规则。
4. 状态闭合集合至少覆盖 starting/running/interrupted/paused/done/failed；P2 落地后加入 cancelled。
5. stdio 进程退出不冒充独立 daemon。进程死亡后，P1 应让其他入口看到 interrupted 并恢复。
6. 工具数、skill、`.mcp.json` 说明、内置指南和契约测试同步更新。

### 验收

- `wait=false` 在预检后快速返回，轮询可达 paused/terminal。
- kill MCP 子进程后，另一个控制面观察到 interrupted 并只恢复未完成节点。
- 同步默认行为不变；Web/MCP 对同一 run 的状态、节点、成本和错误摘要字节级/结构化等价。
- 并发启动、resume、approve 不产生两个 controller 或重复终态。

估算：4–7 人日。

### 落地锚点（2026-08-23 深化）

- **模块**：新建 `atlas/launcher.py`——进程内 ControllerRegistry（run_id → controller 线程句柄，注册/注销加锁，防双 controller）；`atlas/runs.py` 增 `build_run_summary(run_dir)` 领域函数，Web 列表与 MCP `atlas_list_runs` 共用（状态/节点/成本/错误摘要 + 按 run_id 降序稳定游标分页）；`atlas/web.py` 的后台线程与 `atlas/mcp.py` 的 run/resume 全部改走 launcher。
- **事件**：无新事件类型。wait=false 只改变控制面返回时机，事件序列与 P1 interrupted 派生完全不变。
- **表面**：`atlas_run_workflow` 增 `wait: bool = true`（默认同步，兼容不变）；第 7 个 MCP 工具 `atlas_list_runs(limit, cursor)`；skill、`.mcp.json`、内置指南、`tests/test_docs_agent_contract.py` 工具数契约同步。
- **测试**：新 `tests/test_launcher_registry.py`（并发 start/resume/approve 不产生双 controller、不写重复终态）；扩展 `tests/test_p1_kill_resume.py`（kill MCP 子进程后另一入口观察到 interrupted 并只补未完成节点）；wait=false 预检后快速返回且轮询可达 paused/terminal；Web/MCP 对同一 run 的 summary 结构化等价。
- **实施顺序**：① 抽 registry + summary（Web 先切换，行为零变化）→ ② MCP `wait=false` → ③ `atlas_list_runs` → ④ kill/竞争测试补齐。

## 4. R2 / P2：协作式取消与真实费用停损

### 价值

阶段 D 已证明，只有 timeout 和人工杀进程不足以控制昂贵 agent。取消必须成为事件与进程生命周期合同，而不是 UI 按钮。

### 当前缺口

没有 cancel request、cancellation token 或 `run_cancelled`。长 HTTP 调用不能强杀；CLI 虽有进程树终止工具，但未接入运行级取消。

### 依赖

P4 shared launcher；复用 P1 状态派生和现有 stable run lock；成本沿用 P0min reservation。

### 实施合同

1. 使用 run 目录内原子 create-if-absent cancel request，包含 request id、时间和可选 reason；请求不能等当前 controller 长时间持有的排他锁。
2. 重复 cancel 幂等。running/interrupted/paused 接受；done/failed/cancelled 返回冲突。
3. controller 在节点入口、fallback 切换、retry 等待、checkpoint 边界消费 token；sleep 改为可唤醒等待。
4. local CLI 保存受控进程句柄并终止整个进程树。HTTP SDK 首版只能等待在途调用返回或 timeout，不宣称任意时刻强杀。
5. running 只由 controller 写唯一 `run_cancelled`；paused 在锁内复核后可直接终止。cancel/approve/resume/natural completion 竞争只能产生一个合法终态。
6. 未决 reservation 因取消仍保守计入，不能释放为可再次消费的预算。
7. cancelled 成为可删除终态，并进入 Web/MCP summary。

### Agent 成本治理同时落地

- 所有真实 agent 示例与指南必须要求或强烈建议 `guards.max_cost_usd`，并解释 Atlas cap 只有在价格/runner 预算能落实时才是供应商账单边界。
- pricing 全为 `null` 且图含 agent 时，preview 显示醒目的 operational warning；不能把 warning 写成已阻止收费。
  - 批次 K 将补齐 retry 维度的同类 warning（retry>0 的 agent 节点必被提示放大风险）。
- 明确 Atlas `max_cost_usd` 与 Claude CLI `--max-budget-usd` 的映射、舍入、失败和 unknown 行为；无法传递时 fail-loud 或明确降级，不静默假装已限额。
- 自动 retry 会放大 agent 花销。先写 RFC 决定“agent 默认 `retry=0`”或“只有存在可执行预算时才允许 retry”；在决策前不改兼容行为。
  - **已裁决（2026-08-27）**：采纳 agent 缺省 retry=0 的书面承诺 + dry-run 组合警告（批次 K 实施）；准入硬拦否决。见 `rfcs/agent-retry-budget.md` 决议节。

### 验收

- 并发重复 cancel 只写一个请求和一个终态。
- Windows CLI 子孙进程全部退出，无 orphan；取消后不会自动 retry。
- HTTP 调用的延迟取消语义在 API/UI 中明确展示。
- cancel 与 approve/resume/完成的竞争测试覆盖所有顺序；seq、checkpoint、cost fold 可重放。
- 使用桩 CLI 证明 `--max-budget-usd` 的有效值、缺失、拒绝和超支报告路径。

估算：6–10 人日（含成本 RFC 与 UI/MCP）。

### 落地锚点（2026-08-23 深化）

- **模块**：`atlas/runs.py` 增 cancel request 文件（目录内原子 create-if-absent，含 request_id/时间/可选 reason；请求路径绝不等待 controller 排他锁）与 `cancelled` 终态状态机（只有 controller 在锁内写）；`atlas/engine.py` 在节点入口、fallback 切换、retry 等待、checkpoint 边界消费 token，sleep 改可唤醒等待；`atlas/nodes/local_cli.py` + `atlas/nodes/agent.py` 保存进程句柄并终止整个进程树（复用 `tests/test_p1_kill_resume.py` 的真子进程 kill 基建）；HTTP SDK 首版语义如实标注为"等待在途调用返回或 timeout"。
- **事件**：新增 `run_cancelled`（controller-only 唯一终态事件）；cancel request 文件是触发器不是账本内容，重放只看事件。
- **表面**：MCP 第 8 个工具 `atlas_cancel_run(run_id, reason?)`（幂等；running/interrupted/paused 接受，done/failed/cancelled 返回冲突）；Web 运行页取消按钮；cancelled 进入 Web/MCP summary 且可删除；成本面板对未决 reservation 的取消语义如实展示。
- **成本停损**：先出 RFC 决定 agent 默认 `retry=0` 还是"存在可执行预算才允许 retry"；`--max-budget-usd` 映射进 `local_cli` 预检路径，用桩 CLI 测有效值/缺失/拒绝/超支报告；pricing 全 null 且含 agent 的图在 preview 发醒目运营警告（不写成已阻止收费）。
- **测试**：竞争矩阵（cancel × approve × resume × 自然完成的全顺序，终态唯一）；真子进程树终止无 orphan；取消后不自动 retry；事件可重放且 cost fold 一致。
- **实施顺序**：① request 文件 + 状态机 + `run_cancelled` → ② engine token 消费点 → ③ 进程树终止接入 → ④ MCP/Web 表面 → ⑤ 成本 RFC 与 agent 预算映射落地。

## 5. R3 / P9：controller heartbeat

### 价值

让用户区分“Atlas controller 仍在等待”与“控制器/事件流已断”，并为 cancel 提供 attempt 上下文。

### 当前缺口

节点只有 started/done；SSE keepalive 只证明连接循环活着，不能证明 controller 活着。

### 依赖

P2 的 attempt context/cancellation lifecycle；后续由 P10 控制事件增长。

### 实施合同

- 定时写 `node_progress`：node、iteration、attempt、candidate/runner、controller elapsed、phase（waiting/retry）。
- 只称 controller heartbeat，不声称模型内部进度或百分比。
- attempt 完成、失败、取消和终态后必须停止；迟到 heartbeat 被拒绝或忽略。
- interval 有下限且可配置；30 秒一次约为每节点每天 2880 事件，必须在容量设计中真实计入。

### 验收

慢 fake provider/CLI 显示递增 elapsed；冻结调用时措辞仍准确；结束后无泄漏线程或迟到事件；event fold 不因 heartbeat 改变终态。

估算：3–5 人日。

### 落地锚点（2026-08-23 深化）

- **模块**：`atlas/engine.py` 每 attempt 挂 watchdog 线程定时写事件；间隔默认 30 秒（下限 30 秒、run 级可配），不读 YAML 图文件，避免把运营参数混进图语义。
- **事件**：新增 `node_progress`：node、iteration、attempt、candidate/runner、controller elapsed_ms、phase（waiting/retry）；`fold_events` 显式声明忽略该类型（终态与既有语义零变化，需回归测试锁定）。
- **容量**：事件量级计入账本 16MiB 治理（30 秒一次 ≈ 每节点每天 2880 条）；P10 的分段账本方案落地前，文档如实写出长跑的容量代价。
- **测试**：慢 fake provider/CLI 显示递增 elapsed；冻结调用时措辞仍只说"controller 在等待"；终态后线程停止、迟到 heartbeat 被拒绝；event fold 不因 heartbeat 改变终态。

## 6. R4 / P3：异常 taxonomy 与节点 `on_error`

### 价值

允许内容型节点失败后按图作者策略继续或走失败分支，同时保证费用、完整性、路由和运行 deadline 永远不能被吞掉。

### 当前缺口

候选耗尽或节点失败最终都落 `run_failed`；节点 timeout 与整图 deadline 的异常边界不够适合策略化处理。

### 依赖

P2 的 `RunCancelled` 分类；先完成 taxonomy，再开放 YAML 字段。

### 实施合同

- 治理/控制异常永不可吞：CostExceeded、GuardViolation、RunCancelled、run deadline、Spec/Integrity/Wiring/NoRoute、approval rejection、checkpoint/invariant。
- 内容异常可策略化：候选全部失败、假成功耗尽、明确 node-local timeout。
- AgentCliError 单独分类；只有显式白名单子类可 soft-fail，baseline/diff/安全扫描错误仍是治理错误。
- `on_error` 闭合枚举：stop（默认）、continue、branch。branch 只走保留键 `__failed__`；校验期要求对应边。
- soft failure 写 write-once error artifact 和 `node_failed_soft`，必须能从旧/新事件 fold。

### 验收

每类异常做正反测试；配置 continue 也不能吞治理异常；旧图行为不变；Web/MCP 同源显示 error class/artifact/route。

估算：7–11 人日。

### 落地锚点（2026-08-23 深化）

- **模块**：新建 `atlas/exc.py` 异常分类层——治理类（CostExceeded、GuardViolation、RunCancelled、run deadline、Spec/Wiring/NoRoute、Integrity、CheckpointInvariant、ApprovalRejected）永不可吞；内容类（候选全部失败、假成功耗尽、node-local timeout）可策略化；`AgentCliError` 单独分类，仅显式白名单子类可 soft-fail。`atlas/engine.py` 节点失败路径按类分发；`atlas/spec.py` 校验节点级 `on_error`。
- **YAML**：节点字段 `on_error: stop|continue|branch`（默认 stop，旧图零变化）；`branch` 要求存在到保留键 `__failed__` 的边，校验期强制，不等到运行期。
- **事件/artifact**：soft failure 写 write-once error artifact（含 error class、原始异常摘要、节点上下文）+ 新事件 `node_failed_soft`；fold 必须从旧/新事件都得到同一终态（反例测试：新事件缺失时按失败处理）。
- **表面**：Web/MCP 同源展示 error class、error artifact 入口、`__failed__` 路由结果；dry-run 列出图中所有非默认 `on_error` 节点。
- **测试**：每个异常类正反例各一；`on_error: continue` 配置下治理异常仍终止整图；三策略 × 循环/并行/join 组合重放一致。

## 6b. R4c / S1：执行终局可视化与总结节点（2026-08-23 用户定案）

### 定案

执行结束后必须给出最终可视化结果，并加一个总结节点对结果做总结——不仅给最终结果，还要回顾工作流各节点做了什么。导出可查看的离线报告**不做**（原 Stage E"运行报告导出"条目移除）。

### 落地锚点

- **零成本终局视图**：`atlas/web.py` 运行页顶部"终局总结"卡片——最终结果摘要、每节点一句话回顾（模型/耗时/token/成本）、时间线与成本可视化；数据纯由事件账本派生（fold + 成本折叠 + 节点输出首段摘要），复用 P4 的共享 summary builder（P4 是前置）；无新事件、无 LLM 调用。
- **总结节点（opt-in）**：`atlas/spec.py` 图级 `summary: {model, prompt_hint?}`（默认关）；`atlas/engine.py` 在 run_done 前执行一次总结调用，输入为各节点摘要投影；`atlas/artifacts.py` 写 run 级 write-once 产物（sha256 入账）+ 事件 `run_summary_written`（model/usage/sha256）；失败记 `run_summary_failed`，run 终态不变、可重试；成本进 CostLedger 受 `max_cost_usd` 约束。
- **表面**：Web 卡片与 MCP `atlas_get_run` 的 summary 字段同源；`dry_run_impl` 列出"将执行总结（模型 X，预估 1 次调用）"。
- **不做**：离线报告导出、分享链接、文件打包。
- **测试**：零成本视图离线断言（无 LLM 也能渲染）；总结成功/失败/预算耗尽三路径；fold 终态不变；产物哈希与事件可复验；总结内容标注"LLM 叙述，事实以账本为准"。

估算：零成本视图 2–3 人日 + 总结节点 3–5 人日；依赖 P4 的 summary builder。

## 7. R5 / P7：artifact import 与 invocation hash

### 价值

调试长图时安全复用昂贵上游结果，而不是指向可能被删除的旧 run 路径。

### 当前缺口

没有 import/pin/invocation identity。直接跨 run 引用会与删除和完整性合同冲突。

### 依赖

事件/产物现有 SHA 合同；P10 必须理解 lineage，但 P7 要先落地。

### 实施合同

1. 准入时锁住并读取源 run，验证事件、role、大小和 hash，把字节复制到新 run 的 write-once artifacts，再验证写后 hash。
2. 写 `artifact_imported` lineage：source run/logical name/hash、新 path/hash、时间、算法版本。
3. `invocation_sha256` 覆盖节点执行字段、有效 prompt、有序输入 hashes、provider/runner execution identity 和算法版本。
4. 只有 invocation identity 完全相等才可自动 skip；imports/skip plan 进入 execution identity，dry-run 明示清单。

### 验收

导入后删除源 run，新 run 仍可完成；任一 prompt/model/input/runner 改变都不复用；复制崩溃无半产物；与源删除竞争时锁行为确定。

估算：7–11 人日。

### 落地锚点（2026-08-23 深化）

- **模块**：`atlas/artifacts.py` 负责字节复制与写后 hash 复验；`atlas/runs.py` 在源 run 的 stable lock 内校验事件、role、大小、hash 后才复制；`atlas/engine.py` + `atlas/effective.py` 计算 `invocation_sha256`（节点执行字段、有效 prompt、有序输入 hash、provider/runner execution identity、算法版本），进入 execution identity 与 `expected_execution_sha256` 合同（`tests/test_prepared_execution.py` 已有锚点可扩展）。
- **YAML**：节点级 `imports: [{run, name}]`；校验期解析（源 run 必须终态且事件证明该产物完整），拒绝指向运行中/中断 run。
- **事件**：新增 `artifact_imported`（源 run/logical name/hash、新 path/hash、时间、算法版本）；imports/skip plan 进入 execution identity，dry-run 明示"将从哪个 run 复制什么、将跳过什么"。
- **测试**：导入后删除源 run，新 run 仍可完成；任一 prompt/model/input/runner 改变都不复用；复制中途崩溃无半产物（kill 测试）；与源删除并发时锁行为确定。

## 8. R5b / P13：fork 与失效闭包

### 价值

换一个节点的 prompt/model 时，只重跑该节点及受影响后代，保留安全的兄弟分支结果。

### 依赖

P7 invocation hash/import。

### 实施合同

- 比较源 snapshot/invocation identities 得到 changed set。
- 在静态图计算 changed nodes + descendants 的 invalidation closure；循环按强连通分量整体失效。
- closure 内禁止 import/skip；closure 外只有 identity 相等且依赖完整才复制。
- join 依赖 changed 分支时必须重跑；不能先 pin 全部再意外跳过目标节点。
- lineage、changed set、closure、import map 和算法版本进入 dry-run、事件与 execution identity。

### 验收

线性、并行、join、条件边和循环图分别验证；failed/paused 源 run 只能导入事件证明完整的产物。

估算：4–7 人日。

### 落地锚点（2026-08-23 深化）

- **模块**：`atlas/m0_graph.py` 增失效闭包计算——比较源 snapshot 与新图的 invocation identities 得 changed set，静态图上取 changed nodes + 全部后代；循环按强连通分量整体失效（不做循环内部分保留）。
  - **实施偏差（2026-08-27 已交付）**：闭包计算落在新建的 `atlas/fork.py`（纯计划层，只读源账本），而非 `m0_graph.py`——后者实为 M0 自检示例图，锚点写作时名不副实；决策记录于此。测试覆盖五类图 + failed/paused 源；顺带交付 P7 skip 的运行时输入复核与多节点源产物错配修复（详见 CHANGELOG）。
- **规则**：闭包内禁止 import/skip；闭包外仅 identity 完全相等且依赖完整才复制；join 依赖 changed 分支时必须重跑（防止"先 pin 全部再意外跳过目标节点"）。
- **事件/dry-run**：changed set、closure、import map、算法版本全部进 dry-run 输出、事件与 execution identity。
- **测试**：线性、并行、join、条件边、循环五类图分别验证；failed/paused 源 run 只能导入事件证明完整的产物。

## 9. R6 / P10：retention、star 与 run index

### 价值

控制长期磁盘占用和逐目录 full fold 延迟，同时不破坏 imported artifact lineage。

### 当前缺口

只有手工终态删除；无 age/count/star/index。

### 依赖

P7 先保证源 run 删除不会制造悬空引用；可复用 P4 summary builder。

### 实施合同

- 默认 `max_runs`/`max_age_days` 均为 null，不自动删除。
- star/annotation 保护；running/paused/interrupted 永不自动删。
- 候选选择与删除分离；删除必须复用 stable run lock、同卷 tombstone、no-follow 清理，禁止直接 `rmtree`。
- 轻量索引是可丢弃缓存，事件仍是真相；损坏可重建。

### 验收

age/count 决策确定；star 和非终态保护；清理崩溃可重试；索引与抽样 full fold 一致；P7 lineage 不悬空。

估算：4–7 人日。

### 落地锚点（2026-08-23 深化）

- **模块**：`atlas/runs.py` 拆"候选选择"与"删除执行"两步；删除复用 stable run lock、同卷 tombstone、no-follow 清理，禁止直接 `rmtree`；star 是 run 目录内 write-once 标记文件；轻量索引（run_id、状态、时间、star、成本摘要）为可丢弃缓存，损坏即重建，事件仍是唯一真相。
- **配置**：`max_runs`/`max_age_days` 默认 null（永不自动删）；running/paused/interrupted 与 star 标记永不自动删。
  - **实施偏差（2026-08-27 已交付）**：清理阈值走环境变量而非图守卫配置（保留治理是目录级运营面,不属于工作流规格）；触发点是"每次图执行完成顺路清扫"与手工 DELETE；索引接线进 `list_run_summaries`(指纹命中即免整本重读),成本摘要列暂未纳入(列表本不展示成本);star 取消无 API(write-once,手工删文件)。
- **联动**：与账本 16MiB 治理合并设计——retention 是控容量的主路径，分段账本+索引是备选；P7 lineage 引用的源 run 有 import 标记时不自动删（或删除前校验无引用）。
- **测试**：age/count 决策确定性；star 与非终态保护；清理进程崩溃后可重试且无半删状态；索引与抽样 full fold 一致；P7 lineage 不悬空。

## 10. R7 / P11：request_changes 与 routed approval

### 价值

把“要求修改”作为显式、有审计、受循环上限约束的控制流，而不是 reject 后人工重开 run。

### 当前缺口

human 只有 approve/reject。

### 依赖

复用现有 `_verify_approval_material`；不得新增弱审批路径。

### 实施合同

- human 增加显式 `approval_mode` 和闭合 `decisions`；旧 spec 默认 binary。
- routed 模式可加入 request_changes，要求非空 comment，并通过有界回边返回生产者。
- approve/reject/request_changes 全部先验证 projection、consumed 和 baseline/result/patch 三摘要，再持久化 decision。
- Web、API、MCP 使用同一枚举和领域函数。

### 验收

旧图兼容；三分支可重放；缺 comment 拒绝；任何摘要、role、sha、consumed 篡改对三种 decision 都在写事件前拒绝。

估算：4–7 人日。

### 落地锚点（2026-08-23 深化）

- **模块**：`atlas/engine.py` human 节点增 `approval_mode: binary|routed`（默认 binary，旧图零变化）与闭合 `decisions` 枚举（approve/reject/request_changes）；复用 `_verify_approval_material` 的三摘要校验，三种 decision 全部先验 projection/consumed/baseline/result/patch 再持久化；`atlas/web.py` 与 `atlas/mcp.py` 同枚举同领域函数。
- **循环语义**：request_changes 必填非空 comment，经有界回边返回生产者节点；回边轮输入沿用静态 `consumes`（循环携带反馈的完整语义仍是独立 RFC，见 Stage E）；`max_iterations` 消耗与 reject 一致。
  - **实施偏差（2026-08-27 已交付）**：回边用保留路由键 `when: __changes__` 显式接线到修订节点——不隐式指向"生产者",因为修订者未必是直接上游;配套 `_check_cycles` 新例外:SCC 含 __changes__ 回边且有无条件逃逸边即视为有界合法环;修改要求以 write-once `<node>.changes` 产物承载,反馈可见由消费它的修订节点达成(完整循环携带反馈仍归 Stage E)。
- **测试**：旧图兼容；三分支决策可重放；缺 comment 拒绝；任何摘要/role/sha/consumed 篡改对三种 decision 都在写事件前拒绝；与 P3 `on_error` 组合不产生旁路。

## 11. Stage E：独立价值项

这些项目不自动跟随主线，应各自形成 RFC/小批次：

| 项目 | 价值与边界 | 建议验收 |
|---|---|---|
| LLM `web_search` ✅ 已实施（2026-08-27，E-1） | 实施形态为封闭 `search` 节点 + Atlas 自持可插拔后端（tavily/searxng），**排除** provider tool-calling（原表中该形态不落地）；来源、查询、成本落 `search_performed` 事件与 write-once 产物，下游投影 untrusted 围栏；域名过滤只看初始 URL host 为如实限制 | tool schema/fallback/来源完整性/预算/prompt injection 测试全绿（`tests/test_e1_search.py`，24 项）；实施细节与偏差见 [`PLAN-stage-e-2026-08-27.md`](PLAN-stage-e-2026-08-27.md) E-1 章 |
| Release 包含 built frontend | 让使用者免 Node；Git 仍不跟踪 `web/dist` | clean machine 解压即可启动，frontend hash 进入 manifest/provenance |
| OS-level sandbox | 调研 Windows Sandbox 或 WSL backend；与 `local_cli` 并列而非把副本改名为沙箱 | 宿主路径/网络/凭据/输出回收威胁模型和逃逸测试 |
| Browser matrix | 主题、键盘调栏、200% 缩放、系统 Edge/Chromium | 可重复 GUI 测试与真实截图，不用源码字符串代替渲染验证 |
| 节点通讯文件 | 多命名产物、agent collect、attachments；详见 [`rfcs/node-io-files.md`](rfcs/node-io-files.md) | **附件（E-2A）与 agent collect（E-2B）已实施（2026-08-27）**，设计以 [`PLAN-stage-e-2026-08-27.md`](PLAN-stage-e-2026-08-27.md) E-2A/B 章为准（collect 采用 agents.json runner 配置而非 RFC 的 YAML `collect_files`；附件采用本机路径而非 base64）；每阶段保持 write-once、hash、no traversal、size caps 和 projection 完整性；多命名产物（`outputs` 围栏块）未实施 |

## 12. 运营与宣传后续

### GitHub Social Preview（人工设置）

GitHub Social Preview 需要仓库管理员在网页手动上传，不能仅靠 commit 完成：

1. 从真实运行截图裁剪 1280×640 分享图（至少 640×320，保持 2:1）；不包含密钥、路径、prompt 私有内容或供应商账号。
2. 建议画面：Atlas 标识/一句中文定位 + 运行总览截图 + “本地 · MCP · 可审计”。不要宣称强沙箱或完全成本安全。
3. Repository **Settings → General → Social preview → Edit** 上传；分别检查 GitHub、聊天软件和窄屏裁剪效果。
4. 将最终源图保存在 `assets/`，记录人工上传日期；网页设置本身无法由仓库文件证明。

### 中文社区推广

以 Release 正文为事实底稿，分别为 V2EX、即刻、掘金准备短帖/长文：

- 开头讲清：本地 Windows 工具、YAML 图、MCP 控制、Web 审计。
- 展示真实截图、六个示例和“先 dry-run 再付费”。
- 把失败也写清：模型结构化输出/截断差异、一次 agent 成本超支、同用户进程不是 OS 沙箱。
- 提供 Release、SHA256、SBOM/provenance 验证入口。
- 不写“绝对安全”“不会超支”“完全离线”或“所有模型都成功”。
- 发布后记录链接与用户反馈，只有真实反复出现的需求才改变路线优先级。

## 13. 明确移除项

| 编号 | 状态 | 重新立项条件 |
|---|---|---|
| P5 可配置 retry backoff | 移除 | 固定等待出现可复现稳定性/限流问题 |
| P8 token guard | 移除 | 无 pricing 场景形成高频真实需求，并能证明 agent usage 口径 |
| P12 durable failure workflow | 移除 | Atlas 转向长期无人值守服务端编排 |
| P14 从 run 恢复 YAML | 移除 | 用户反复需要定义恢复且 snapshot 规范化价值明确 |

无新证据时保持移除；旧计划中的设计段落只是历史研究，不是 backlog。
