# Atlas 稳定性升级计划:对标开源基准(2026-08)

状态:**定稿,待排期**。本文档定稿于 2026-08-18,来源是一次完整的对标调研
任务(见第 1 节)。它是后续所有优化阶段的**输入文档与唯一事实来源**:
实施任何一项前,先在本文档中确认方案边界;如需偏离,先改本文再改代码。

定位:本文不改变 rc.1 的任何红线(本地、回环、只读 UI、fail-closed、
事件流唯一真相)。所有建议都必须在现有守卫语义之内落地。

---

## 1. 本次任务说明

### 1.1 背景与动机

Atlas 的核心组合(YAML 图 + MCP 控制面 + 本地 Web 审批 + 成本守卫 + 审计
账本)在开源世界已有多个成熟对应物(TrueForge、Enju、BoundFlow、
agent-blueprint、Dify、n8n 等)。这批项目经过大量真实使用,其设计中的
共性机制可以视为「经过大规模验证的解法」。本次任务的目的是:

1. 通读 Atlas 全部核心代码,确认现状(哪些已领先、哪些是真缺口);
2. 调研上述项目与成熟执行引擎(LangGraph、Temporal、n8n、Azure Prompt
   flow、CrewAI、Prefect、Dagster)的**具体机制**(字段名、语义、UX 流程,
   不是营销文案);
3. 逐项对照,产出可采纳方案清单,按「影响 ÷ 成本」排序,作为后续优化
   阶段的工作底稿。

判断结论(一句话):**Atlas 的账本层(事件流、成本预留、完整性、执行身份)
处于同类第一梯队;差距集中在故障恢复面(崩溃续跑未暴露、无取消、无节点级
容错)与等待/调试体验面(MCP 同步阻塞、无单节点重跑、长调用无心跳)。**

### 1.2 方法

- 代码通读:`atlas/engine.py`(1126 行)、`atlas/events.py`、`atlas/costs.py`、
  `atlas/adapters.py`、`atlas/web.py`、`atlas/mcp.py`,以及 spec/引擎的
  交互面;关键缺口用全项目 grep 交叉验证(例如 `resume_graph` 只有测试
  调用、`cancel` 零命中)。
- 外部调研:两路并行——成熟执行引擎设计(LangGraph/Temporal/n8n/
  Prompt flow,直接读源码与官方文档);同类编排器机制(TrueForge/Enju/
  BoundFlow/agent-blueprint/Dify/CrewAI/Prefect/Dagster,读仓库与文档)。
- 对照原则:**只采纳与 Atlas 哲学兼容的机制**(静态 YAML 图、事件流唯一
  真相、fail-closed、只读 UI);每个方案标注参考来源与不采纳的边界。

### 1.3 本文的使用方式

- 第 3 节是现状证据(带代码位置),实施时以此为基线做回归对照;
- 第 5 节每项方案自带「验收标准」,直接转化为测试用例(项目现有 223 项
  后端测试的纪律不变);
- 第 6 节是明确不做的事,避免后续阶段被「别的项目都有」绑架;
- 第 7 节给出建议的落地顺序与阶段划分。

---

## 2. 基准全景

与 Atlas 相关度从高到低:

| 项目 | 定位 | 与 Atlas 的关系 |
|---|---|---|
| [TrueForge](https://github.com/truefoundry/trueforge) | agent harness,YAML 目录 + 人类检查点 + 聊天 UI + 本地单进程 | 功能面几乎 1:1 重叠的最近竞品 |
| [Enju](https://github.com/tamerh/enju) | 人与 agent 同跑一张 YAML DAG,单二进制 MCP + Web,git commit 审计 | 同为「MCP + YAML 图 + human gate」路线 |
| [BoundFlow](https://github.com/boundflow/boundflow) | agent 控制面:审批、成本治理、持久执行、审计回执 | 治理面(预算/审批/恢复)的参照 |
| agent-blueprint(PyPI) | YAML 编译到 LangGraph,声明式 policies | 预算/审批声明式 schema 的参照 |
| [Dify](https://github.com/langgenius/dify) | 低代码 LLM 应用平台,可视化工作流 | 节点级错误策略/单节点调试 UX 的参照 |
| [n8n](https://docs.n8n.io) | 工作流自动化平台 | 错误处理/钉扎/部分执行/保留策略的参照 |
| [LangGraph](https://github.com/langchain-ai/langgraph) | 图执行框架(Atlas 的底层) | checkpoint/interrupt/重试语义的权威来源 |
| [Temporal](https://temporal.io) | 持久执行引擎 | 事件溯源/重试策略/版本化/心跳的权威来源 |
| [Azure Prompt flow](https://microsoft.github.io/promptflow/) | YAML LLM 流程 + 评测 | 连接管理/单节点测试/评测的参照 |
| [CrewAI Flows](https://docs.crewai.com/concepts/flows) | 轻量流编排 | fork/resume 状态持久化参照 |
| [Prefect](https://docs.prefect.io/v3/how-to-guides/workflows/retries) / [Dagster](https://docs.dagster.io/deployment/execution/run-retries) | 数据流编排 | 重试退避/失败点续跑参照 |

调研时点为 2026-08,机制描述以当期文档/源码为准;实施某项前应复核上游
是否已有变化。

---

## 3. Atlas 现状评估

### 3.1 领先项(后续优化不得回退)

以下机制在对照中**优于或等同于**全部参考项目,是 Atlas 的资产:

1. **Append-only 事件流 + 撕裂尾恢复**(`atlas/events.py`)。字节级偏移的
   撕裂行截断、seq 单调、续写安全——比 Temporal Event History 的本地等价物
   更细致,比 TrueForge 会话事件更完整。`fold_events`(A6)保证「重放 ==
   运行时状态」。
2. **成本预留-结算状态机**(`atlas/costs.py`)。派发前预留、结算幂等
   (reservation_id)、崩溃后未决预留按全额保守计入、费率未知记 null 不猜。
   保守程度超过 agent-blueprint 的 `policies.budgets` 与 BoundFlow 的
   RuntimePolicy。
3. **执行身份三哈希**(spec/backend/execution sha256,`atlas/engine.py` 的
   `prepare_execution` / `_check_persisted_execution_identity`)。续跑与批复
   时拒绝任何漂移——是 Temporal「版本不匹配警告」的严格版。
4. **假成功三道检查**(`atlas/adapters.py`):空内容、截断哨兵
   (input_tokens 远小于 prompt 估计)、必填字段缺失,与传输错误同走降级
   链。没有一家参考项目同时做这三件。
5. **熔断器**只对传输失败计数(`CircuitBreaker`),不冤枉内容型失败。
6. **MCP 保存的乐观并发**(`expected_sha256` + `os.link` 原子占位 +
   锁内复核,`atlas/mcp.py` 的 `save_workflow_impl`)。
7. **per-run 跨进程 OS 锁**(`msvcrt`/`fcntl`,`.locks/` 永久文件,
   非 TTL 接管)。
8. **confirm-before-run**:`expected_execution_sha256` 让调用方锁定「预览
   过的那份执行配置」才真跑。
9. **零成本校验/预览**:`atlas_validate_workflow` 与 `dry_run` 不打供应商。
10. **回环安全模型**:Host 校验 + `X-Atlas-Request` 头 + 硬编码
    127.0.0.1。

### 3.2 确认的缺口(证据)

以下每条都有代码位置或 grep 证据,是后续方案的锚点:

| # | 缺口 | 证据 | 后果 |
|---|---|---|---|
| G1 | **崩溃续跑是死代码** | `resume_graph`(`atlas/engine.py:839`)仅被 `tests/` 调用;web.py / mcp.py 均未导入 | 进程被 kill 后 run 永远停在 running,用户无恢复入口 |
| G2 | **账本无 interrupted 状态** | `fold_events`(`atlas/events.py:146-201`)只有 pending/running/paused/done/failed;SSE 用 `stream_closed` 约 60s 空读后放弃(`atlas/web.py:645-648`)是唯一兜底 | 界面把死 run 显示成"运行中",误导用户 |
| G3 | **无取消机制** | 全项目 grep `cancel` 零命中 | 跑偏的运行只能等 `timeout_s` 或杀进程 |
| G4 | **MCP 真跑同步阻塞** | `run_workflow_impl`(`atlas/mcp.py:330`)"同步阻塞到完成/暂停/失败" | 10 分钟工作流卡死 agent 对话 10 分钟;MCP 也没有 list-runs |
| G5 | **任一节点失败 = 整图失败** | `call_with_fallback` 抛 `AllCandidatesFailed` → `_invoke` 落 `run_failed`;无 continue 语义 | 9 节点成功、第 10 个失败时,已花费全部作废 |
| G6 | **重试退避原始** | 固定 0.5s sleep(`atlas/adapters.py:555-558`) | 瞬时故障下的重试节奏不合理,无上限增长控制 |
| G7 | **语义校验错误无行号** | `atlas/spec.py` 无 yaml mark/行号使用(仅 PyYAML 语法错误自带位置) | agent 修 YAML 全靠文字描述来回试 |
| G8 | **失败后重跑整图重新花钱** | run 请求无产物复用/钉扎参数 | 调试长图的边际成本 = 全图成本 |
| G9 | **费率未知时预算守卫失效** | `max_cost_usd` 对 null 费率只发 `cost_unknown` 警告 | 冷启动(无 pricing.json)时唯一的失控保护是 `timeout_s` |
| G10 | **长调用期间无进度信号** | 事件只有 node_started/node_done,SSE keepalive 是唯一生命迹象 | 推理模型跑 5 分钟,UI 分不清"在思考"还是"挂了" |
| G11 | **runs/ 无限增长** | 无保留/清理策略;`list_runs` 每次全量读所有 run 的全部事件(`atlas/web.py:398-420`) | 磁盘与列表性能随时间劣化 |
| G12 | **human 节点只有二值批复** | `approve_run` 只认 approve/reject(`atlas/engine.py:918-984`) | 「打回修改」只能 reject 整图重来 |

---

## 4. 参考项目机制详录

### 4.1 成熟执行引擎

**LangGraph**(Atlas 的底层;来源:源码 `libs/langgraph/langgraph/types.py`、
`pregel/_retry.py`,docs.langchain.com)

- **RetryPolicy**:`initial_interval=0.5s`、`backoff_factor=2.0`、
  `max_interval=128s`、`max_attempts=3`、`jitter=True`(附加
  `uniform(0,1)`)、`retry_on` 可按异常类/谓词过滤;支持**策略列表**按异常
  匹配。重试间**清空 task.writes**,防止部分写入污染状态。
- **interrupt() 语义**:抛内部异常 → 保存 checkpoint → 同 thread_id 用
  `Command(resume=value)` 恢复;**整个节点从头重跑**,resume 值成为
  `interrupt()` 的返回值;interrupt 前的副作用必须幂等。Atlas 已内建此
  语义(human 节点),此处仅作边界确认。
- **checkpoint 谱系**:`CheckpointTuple.parent_config` + metadata 的
  `parents` 映射支持从任意步 fork;`pending_writes` 区分在途节点的部分
  写入。**图结构增删不影响已有线程续跑**,但 resume 点之前的任务/中断
  顺序变化会错配——Atlas 的 execution_sha256 校验已更严格地覆盖此风险。
- **TimeoutPolicy**(新版):`run_timeout` 与 `idle_timeout` 分离,心跳可
  刷新 idle 超时。

**Temporal**(来源:docs.temporal.io encyclopedia)

- **Event History**:工作流代码不发副作用,而是产生 Command → 服务端落
  成 append-only 事件;崩溃后重放历史,已记录的 activity 结果/定时器直接
  复用不重算。Atlas 的事件流 + checkpoint 正是这对组合。
- **Activity RetryPolicy**(默认开启):`InitialInterval=1s`、
  `BackoffCoefficient=2.0`、`MaximumInterval=100×initial`、
  `MaximumAttempts=∞`;`ApplicationError(non_retryable=True)` 短路;
  错误可带 `next_retry_delay` 覆盖退避。**重试失败的那一步,不是整条
  工作流**。
- **心跳**:长 activity 周期性 ping 且**载荷可延续到下一次重试尝试**
  (单步内断点续传);节流 `min(heartbeatTimeout×0.8, 60s)`;**取消只在
  心跳点送达**。
- **版本化**:执行记录里存工作流定义版本/哈希,续跑版本不匹配时告警;
  `workflow.patched()` 三阶段(marker → deprecate → 删除)让旧执行按旧
  逻辑跑完。

**n8n**(来源:docs.n8n.io)

- **节点级韧性**:Node Settings 里 `Retry On Fail`(maxTries/
  waitBetweenTries)+ **`On Error: StopWorkflow | Continue | Continue
  (error output)`**——错误数据从专用输出端口继续流动。
- **错误工作流**:Workflow Settings 指定一个以 **Error Trigger** 节点开头
  的工作流;任何图失败时收到 `{execution: {id, url, retryOf, error:
  {message, stack}, lastNodeExecuted, mode}, workflow: {id, name}}`;
  一个错误工作流可服务全部图。
- **钉扎与部分执行**(pin-and-mock):任意节点输出可 pin 并手改 JSON,
  之后的手动运行直接替换用钉扎数据并**跳过该节点**;「Execute step」只跑
  该节点 + 为补齐其输入所需的那部分祖先。
- **数据生命周期**:执行记录按年龄(默认 336h)与数量(10k)修剪;
  **被标注(annotated)的执行永不修剪**;硬删除有 1h 缓冲。
- **三种执行模式**:manual / partial(单步)/ production,历史执行只读
  可"拷回"数据。

**Azure Prompt flow**(来源:microsoft.github.io/promptflow)

- **单节点测试**:`pf flow test --node <name> [--debug]`,一次只测一个
  节点;`--ui` 起聊天界面;每次 run 打印本地 trace UI URL。
- **连接管理**:命名连接实体,`configs` 与 `secrets` 分离;所有回读脱敏;
  OS keyring 加密;`Type.from_env()` 零存储回退。Atlas 的
  providers.json/.env 分离与此同构。
- **评测即「跑在 run 之上的流」**:`pf run create` 可以 `run: <旧run>` +
  `prediction: "${run.outputs.x}"`,评测流消费基线 run 的输出并算指标;
  `show-details`(逐行)/`show-metrics`(聚合)/archive/restore。

### 4.2 同类编排器

**TrueForge**(来源:仓库 + truefoundry.dev 文档)

- **审批检查点**:turn 以 `tool.approval_required` 事件结束,携带
  `tool_calls[{id, source_event_id}]`;客户端用 `source_event_id` 解析回
  模型消息读取拟执行载荷。恢复 = 新 turn,输入项
  `{type: 'user.tool_approval', toolCallId, approval: {status:
  'allow'|'deny', reason}}`;审批项不能与普通用户消息混发。
- **断线重连**:持久化 `session.id / turnId / lastSequenceNumber`;
  `subscribeToTurn({afterSequenceNumber})` 增量重放;SSE 自动重连用
  `Last-Event-ID`。Atlas 的 `?after=N` + seq 已同构。
- **本地模式**:单进程 + SQLite,无登录;目录 YAML 可编辑。

**Enju**(来源:仓库)

- **任务动作四选**:`answer / review / vote / compute`——动作决定该任务
  由人、agent 还是脚本执行;人机是同一张 DAG 上的对等节点。
- **评审循环**:判定 `approve / request_changes / reject`;
  `request_changes` 把修订任务连同反馈**重新入队给生产者**;生命周期
  `pending → ready → claimed → running → review → done`。
- **git commit 审计**:每次尝试是一个 commit,归因与审计从 git 历史免费
  获得;协调器只记录任务状态与决定,从不碰内容。

**BoundFlow**(来源:仓库)

- **审批即返回值**:工作流返回 `AwaitApproval(on_approve=Next(...),
  on_reject=Complete(), justification=...)`;状态服务端持久化,"从停下的
  地方精确恢复";敏感操作只存在于批准后分支。
- **三层治理**:(1) 运行中 RuntimePolicy(`max_cost_usd` + 工具调用/token/
  时延上限);(2) 运行后趋势规则 `AgentRule(metric=COST_USD, op=GT,
  threshold, window=5, action=SetModel("claude-haiku"))`——自动降级模型;
  (3) `WorkflowRule(NUM_FAILURES > 3 → SetVersion(1) | Pause | Cooldown)`
  回滚到上个好版本。
- **租约式持久执行**:"run 被检查点化并租借;worker 崩溃后另一个续跑"。

**agent-blueprint**(来源:PyPI 文档)

- `policies.approvals`:`mode: selective|all`、`tools: [...]`、
  `on_violation: block|warn`(warn 记 `policy_violation` 事件)。
- `policies.budgets`:`max_tokens_per_run`、`max_latency_seconds`、
  `max_cost_usd`——**token 超限中途熔断**,时延完成时检查,全部发
  `policy_violation` 事件。
- `contracts.state.invariants` 在**每个 agent 节点后重查**,违反先发
  `contract_failed` 再抛;`policies.tool_usage.max_calls_per_node/run`;
  `policies.escalation.confidence_threshold` 中途改道到评审节点。

**Dify**(来源:docs.dify.ai)

- **节点级错误策略**(LLM/HTTP/Code/Tool 节点):`None`(默认停)|
  **Default Value**(类型匹配的兜底输出)| **Fail Branch**(专用错误
  路径,暴露 `error_type`/`error_message` 变量供分支使用)。
- **迭代节点**:错误模式 `terminated`(默认)/ `continue-on-error`
  (失败项 → null)/ `remove-abnormal-output`(从数组过滤)。
- **调试 UX**:单节点运行(选节点 + 测试输入);**Variable Inspector**
  缓存各步输出、**允许编辑缓存值后只重跑该节点**;每个节点有「Last
  run」(输入/输出/耗时/错误)。
- **版本**:Current Draft / Latest / Previous;Publish 把草稿快照成新版本;
  Restore 把旧版本完整载回草稿;草稿与已发布运行分离。

**CrewAI / Prefect / Dagster**(交叉验证)

- CrewAI:`@persist`(SQLite)+ UUID 状态;`kickoff(inputs={"id": uuid})`
  **恢复**;`restore_from_state_id=uuid` **fork**(新 id,历史保留)。
  `@human_feedback` 把自由文本反馈分类为离散结果(`approved/rejected/
  needs_revision`)触发对应监听。
- Prefect:`retries` int;`retry_delay_seconds` 标量或**列表**
  `[1,2,4,8]`(或 `exponential_backoff(factor=2)` 生成);`retry_jitter_factor`;
  `retry_condition_fn(task, task_run, state) -> bool`。缓存:
  `cache_policy = INPUTS + TASK_SOURCE`、`cache_expiration`、`refresh_cache`。
- Dagster:`execution.retries: ALL_STEPS | NONE | FROM_FAILURE`;
  **从失败恢复只重跑失败步**——因为 IO manager 持久化了完成步的输出。

---

## 5. 可采纳方案清单

排序 = 影响 ÷ 成本。每项含:问题锚点(第 3 节 G#)、参考设计、建议方案、
守卫交互、验收标准。**所有新事件类型都必须能被旧 `fold_events` 忽略或
显式兼容**(事件流向后兼容是硬约束,见 A6)。

### 第一梯队:故障恢复面(稳定性主战场)

#### P1 暴露崩溃续跑 + 孤儿检测(G1, G2)

- **参考**:Temporal 持久执行与租约恢复;Dagster `FROM_FAILURE`(完成步
  输出持久化,只重跑失败步);CrewAI `kickoff(inputs={"id"})` 恢复。
- **现状**:`resume_graph` 已实现且被 4 个测试文件覆盖,含身份漂移拒绝、
  锁语义、遗留账本兼容——只缺入口。
- **方案**:
  1. Web 新端点 `POST /api/runs/{rid}/resume`:
     - `_check_id` → `acquire_run_lock`(忙 → 409);
     - 从 `runs/<rid>/spec.snapshot.json` 加载 spec(无快照按现行
       `_spec_for_run` 回退逻辑);
     - 校验账本状态 ∈ {paused, interrupted}(见下)→ 调 `resume_graph`;
     - 执行放后台线程,与 `start_run` 同模式;返回 `{run_id, resumed:
       true}`。
  2. **interrupted 派生状态(不写账本)**:`fold_events` 不改(账本只记
     发生过的事);在 web 的 `get_run`/`list_runs` 层叠加推导——
     `status == "running"` 且 `rid not in _run_threads` 且
     `acquire_run_lock` 试探成功(成功即无进程持有,随即释放)→ 展示状态
     `interrupted`。原因文案:"进程退出前未写终态事件(可能被 kill)"。
     注意:试探锁必须在同进程锁登记检查之后做,避免与自身后台线程竞争。
  3. 启动扫描:web 进程启动时遍历 runs,把所有推导为 interrupted 的 run
     记入内存集合,列表页打标。不写任何文件。
  4. MCP 新工具 `atlas_resume_run(run_id)`:同逻辑,返回
     `summarize_run` 风格的结果;spec 漂移(spec_snapshot 的
     execution_sha256 与当前后端不符)如实报错,不静默。
  5. 同时提供 `POST /api/runs/{rid}/mark-failed`(写 `run_failed` 事件,
     error_type="marked_by_user"):用户明确放弃时,账本不留在假 running。
- **守卫交互**:身份三哈希校验沿用;成本账本从事件重建
  (`CostLedger(cap, spent=_settled_spent_usd(events))` 已在
  `resume_graph` 内);`run_resumed` 事件已定义。
- **验收**:
  - kill 进程 → 重启 web → 列表显示 interrupted → 点续跑 → 从断点继续,
    已完成节点不重跑、已花成本不重算;
  - spec 已改的旧 run:续跑被拒绝并报 execution_sha256 不符;
  - paused 状态的 run 依旧只能走 approve(不被 resume 端点绕过 human 门)。

#### P2 协作式取消(G3)

- **参考**:n8n 取消;Temporal「取消只在心跳点送达」——即取消是协作式的,
  在检查点生效,不强抢。
- **方案**:
  1. 请求:Web `POST /api/runs/{rid}/cancel`,写标记文件
     `runs/<rid>/cancel.flag`(跨进程可见、零依赖;内容为请求时间戳)。
     幂等:已存在则 200。非 running/paused 状态 → 409。
  2. 引擎检查点(全部在已有边界上,不引入强抢):
     - `call_with_fallback` 每次候选切换与重试 sleep 前;
     - 每个节点函数入口(guard 检查处一并);
     - human 节点暂停本身即为天然取消点:cancel 请求可把 paused 的 run
       直接落终态。
  3. 终态:抛 `RunCancelled` → `_invoke` 的 except 落
     **`run_cancelled`** 事件(新类型)+ 释放成本预留(未决 reservation
     按现行 outstanding 语义保留,不下调已花金额)。`fold_events` 增加
     `"cancelled"` 状态;`delete_run` 允许终态扩为
     done/failed/cancelled。
  4. **如实文档化**:在途 HTTP 不会被中断,取消在当前调用结束或节点超时
     后生效——这正是 Temporal 心跳语义,不假装强杀。
- **验收**:长运行中 cancel → 秒级(或在当前调用结束后)停止;账本终态
  一致;再次 cancel → 409;旧版本读取带 run_cancelled 的账本不崩
  (fold 向后兼容测试)。

#### P3 节点级错误策略 `on_error`(G5)

- **参考**:n8n `On Error: Stop | Continue | Continue (error output)`;
  Dify `error_strategy: none | default_value | fail_branch`(暴露
  error_type/error_message 变量)。
- **方案**:
  1. YAML:节点新增可选字段 `on_error`,闭合枚举:
     - `stop`(默认,现行为);
     - `continue`:节点失败时写**错误占位产物**
       `<node>.output.{iteration}.json`,内容
       `{"__atlas_error__": true, "error_type": ..., "error_message":
       ...,"iteration": ...}` 并落 `node_failed_soft` 事件;图继续。
       下游投影会原样内联该 JSON——下游模型可见上游失败,语义诚实;
     - `branch`:失败时走**保留路由键** `__failed__` 的条件出边。节点
       需声明 `on_error: branch` 且图中存在 `when: __failed__` 的出边,
       校验期强制配对(缺边 → SpecError)。正常路由不受影响;`__failed__`
       进入路由候选白名单但仅对声明了 `on_error: branch` 的节点合法。
  2. **可吞异常白名单**(关键安全边界,校验期与运行期双重检查):
     可被 continue/branch 捕获的只有 `AllCandidatesFailed` 与节点级
     `TimeoutViolation`;**不可吞**:`CostExceeded`(预算语义就是停)、
     `GuardViolation`(循环不收敛)、`HumanRejected`(人的否决)、
     `RunCancelled`、`NoRouteError`(查表失败是图缺陷)、以及所有
     SpecError/引擎内部异常。原则:**节点内容失败可容错,治理与控制流
     失败必须停**。
  3. dry_run 渲染增加每节点的 `on_error` 展示,预览时可见容错边界。
  4. `node_failed_soft` 计入 fold 的 nodes_done?——不计 done、不计
     fail;get_run 里该节点 status 显示 `failed_soft`。
- **验收**:
  - 10 节点图第 10 节点配 `on_error: continue` 且失败 → 前 9 产物完整、
    run 状态 done、失败节点标 failed_soft、错误产物可被下游 consumes;
  - `on_error: branch` + `when: __failed__` 出边 → 走错误分支;
    配了 branch 但没配错误出边 → 校验期拒绝;
  - `CostExceeded` 在 continue 节点上照常整图停止(不可吞测试)。

#### P4 MCP 异步运行 + `atlas_list_runs`(G4)

- **参考**:TrueForge 断线重连模型(立即返回事件位置,客户端增量拉)。
- **方案**:
  1. `atlas_run_workflow` 增加参数 `wait: bool = True`(默认 True 保持
     现行为,不破坏既有调用方);`wait: false` 时:完成 prepare/锁/目录
     准入检查后**后台线程执行**,立即返回
     `{run_id, status: "started", next: "用 atlas_get_run 轮询"}`。
  2. 新工具 `atlas_list_runs(limit=20)`:返回
     `{run_id, graph, status, nodes_done 数, started}` 列表(与 web
     list_runs 同源逻辑抽公共函数,避免两处漂移)。
  3. **MCP 进程生命周期风险如实文档化**:stdio server 被 client 断开则
     后台线程随进程终止 → run 变 interrupted;恢复路径就是 P1 的
     `atlas_resume_run`。这两个特性必须同批交付,互为兜底。
- **验收**:`wait: false` 秒回;轮询到 done;kill server 后
  `atlas_list_runs` 显示 interrupted,`atlas_resume_run` 可续。

#### P5 退避重试升级(G6)

- **参考**:LangGraph RetryPolicy(0.5s 起步、×2、128s 封顶、jitter、
  retry_on 过滤、重试间清 writes);Prefect `retry_delay_seconds` 列表;
  Temporal `BackoffCoefficient=2, MaximumInterval=100×initial`。
- **现状**:`retry` int + 固定 0.5s;「只对传输错误重试、假成功直接换
  候选」的语义是对的,**保留**。
- **方案**:
  1. YAML 可选新字段 `retry_backoff_s`:正浮点列表,如 `[0.5, 1, 2, 4]`;
     缺省 `[0.5]`(即现状:每档 0.5s)。第 n 次重试的 sleep =
     `min(列表第 min(n, len-1) 项, remaining_timeout)`;每档附加
     `uniform(0, 0.5)` 抖动(LangGraph 默认 jitter 语义)。
  2. `retry` 语义不变(同模型重试次数上限);列表比列表短时用最后一项
     (Prefect 语义)。
  3. 超时交互沿用:`sleep_s = min(sleep_s, remaining_timeout())`,
     剩余不足时不进 sleep(现有 agent runner 守卫同思路)。
  4. MCP `node_overrides` 白名单增加 `retry_backoff_s`。
- **验收**:传输失败序列的重试间隔符合列表;deadline 临近时跳过长 sleep;
  未配置时行为与现在逐字节一致(回归测试锁定)。

### 第二梯队:调试与等待体验

#### P6 校验错误带 YAML 行号(G7)

- **参考**:Prompt flow / Pydantic 的字段级错误定位。
- **方案**:
  1. `spec_from_yaml` 改用自定义 Loader(继承 `yaml.SafeLoader`),在
     construct_mapping 时记录每个顶层/节点级 key 的 `(line, column)`
     到旁路表(路径 → mark),不污染数据模型。
  2. `SpecError` 消息模板追加 `"(yaml 第 {line} 行)"`:至少覆盖
     未知字段、节点级非法值、边悬空、guards 非法、consumes 引用错误。
     拿不到 mark 的错误(如整图级可达性)退化为纯文字,不编造位置。
  3. PyYAML 语法错误本自带 mark,统一格式输出。
- **验收**:构造「悬空边」「未知节点字段」「非法 guards」三份坏 YAML,
  错误消息各自包含正确定义行号;`atlas_validate_workflow` 返回中可见。

#### P7 产物钉扎 + 自动跳过(G8)

- **参考**:n8n pin-and-mock(pin 住并手改节点输出,后续运行跳过该节点)
  + Execute step(只跑补齐输入所需的祖先);Dify Variable Inspector
  (编辑缓存值重跑单节点)。
- **方案**:
  1. run 请求(Web 与 MCP)新参数 `pinned_artifacts:
     {"<node>.output": "<source_run_id>", ...}`:
     - 准入:源 run 必须终态;引用的产物必须存在且 sha256 与源账本
       一致(复用 `read_artifact` 的完整性断言);
     - 注入:作为初始 `state.artifacts` 合入(在 task 产物之后,可被
       本 run 实际输出覆盖);
     - 跳过:某节点若**其全部输出均被钉扎且其 consumes 在钉扎集 +
       task 中可满足**,引擎跳过执行并落 `node_skipped_pinned` 事件
       (记来源 run 与 sha256,账本可审计)。
  2. **dry_run 必须渲染跳过清单**(哪些节点将跳过、产物来自哪个 run)——
     与「预览即所得」纪律一致;`pinned_artifacts` 也纳入
     execution_sha256 计算(它改变执行行为,属于执行身份)。
  3. 不做「手改产物后钉扎」的 n8n 全量功能:本地文件可直接改 run 目录,
     但那会破坏哈希断言——**显式不支持**,文档说明理由(完整性优先)。
- **验收**:失败 run 修复后新 run 钉扎前 9 个产物 → 只花第 10 个节点及
  后继的钱;被钉扎产物哈希与源不符 → 拒绝运行;dry_run 显示跳过清单。

#### P8 token 预算守卫 `guards.max_tokens`(G9)

- **参考**:agent-blueprint `max_tokens_per_run`(token 超限中途熔断)。
- **动机的独特契合点:token 守卫不依赖 pricing.json**——在费率全 null 的
  冷启动阶段也能兜住失控循环,与「拿不到费率记 null 不猜」哲学同构。
- **方案**:
  1. YAML:`guards.max_tokens: int`(可选)。语义:本 run 全部 llm 节点
     input+output token 累计上限(与 max_cost_usd 同为派发前预估 +
     结算实值的预留模型)。
  2. 复用 `CostLedger` 预留-结算骨架,平行建 `TokenLedger`(或参数化
     单个 ledger 类):预留 = 输入估算(prompt 字符/3)+ max_output_tokens
     上界;结算 = usage 实值;`cost_settled` 事件增加 tokens 字段或新增
     `token_settled`(选择前者,少一类事件;旧读侧忽略未知字段)。
  3. 超限异常 `TokenLimitExceeded`,**不可被 on_error 捕获**(治理语义)。
  4. agent 节点如实标注:CLI 会话无 token 计量 → 计 0 并发一次
     `token_unknown` 警告(与 cost_unknown 同款诚实)。
- **验收**:无 pricing.json 时,配 `max_tokens` 的循环图在超限处停止;
  事件流可重放出 token 账;未配置时零行为变化。

#### P9 心跳进度事件(G10)

- **参考**:Temporal heartbeat(节流 `min(timeout×0.8, 60s)`,载荷可
  续传);LangGraph idle/run 双超时(心跳刷新 idle)。
- **方案**:
  1. 节点派发后启动看门线程(或惰性:在 sleep/重试间隙发),每 30s 落
     一条 `node_progress` 事件:`{node, iteration, elapsed_s, attempt,
     candidate}`。SSE 已会自动推送。
  2. 事件量守卫:单事件 < 1KB,16MB 上限内可容纳数百小时运行;不改上限。
  3. UI 展示「运行中 Ns · 第 k 次尝试 · 候选 X」;与 SSE keepalive 互补
     ——keepalive 证明连接活着,progress 证明引擎活着。
  4. 折叠读侧:`fold_events` 忽略 node_progress(不进状态)。
- **验收**:模拟慢供应商(FakeProvider sleep)时,get_run 轮询可见
  node_progress 递增;账本 fold 不受影响。

#### P10 runs 保留策略(G11)

- **参考**:n8n 按年龄(336h)/数量(10k)修剪 + **标注过的执行永不修剪**
  + 1h 硬删缓冲。
- **方案**:
  1. `config/` 新增保留配置(带 example 文件):`max_runs`、
     `max_age_days`,默认**不限**(保守,不惊喜删除)。
  2. 标星:`runs/<rid>/starred`(空文件)标记永不删;Web 端点
     `POST/DELETE /api/runs/{rid}/star`。
  3. 清理器:web 启动时 + 每日一次;候选仅终态(done/failed/cancelled)
     且未标星;按 above 条数/年龄从最旧删除;复用 `_delete_run_locked`
     的锁 + tombstone 流程(不另写删除路径)。
  4. `list_runs` 性能:同批加轻量索引缓存(内存 dict:rid → status/
     started,启动扫描构建,删除时维护),避免每次全量读事件。
- **验收**:超限时最旧的未标星终态 run 被清;标星与 running 的绝不删;
  索引与全量 fold 抽查一致。

### 第三梯队:差异化增强(按需排期)

#### P11 human 节点三值批复 + 批复路由(G12)

- **参考**:Enju 评审判定 `approve / request_changes / reject`(带反馈
  重入队);CrewAI `@human_feedback`(自由文本 → 离散结果)。
- **方案**(利用现有条件边机制,改动最小化):
  1. human 节点可选声明 `route_field: decision`(缺省无路由,现行为);
     声明后,decision 值(approve/reject/request_changes)成为路由键,
     出边用 `when: approve` / `when: request_changes` 分流。
  2. `request_changes` 的语义由图作者用现有回边 + 反馈产物表达:
     批复记录本身已是产物(`<human>.output`,含 decision 与 comment),
     上游修订节点 `consumes: [<human>.output]` 即可拿到反馈。
  3. Web 批复 UI 增加第三个按钮与必填 comment(request_changes 时)。
  4. `HumanRejected` 语义不变:仅 `reject` 终止整图。
- **验收**:声明 route_field 的 human 节点三分支各走各路;未声明的行为
  与现在完全一致;反馈 comment 出现在修订节点的投影里。

#### P12 失败触发器 `on_failure`(可选)

- **参考**:n8n Error Trigger(专用错误工作流,收到 execution id/url、
  error message+stack、lastNodeExecuted、workflow 名;一个服务全部图)。
- **方案**:workflow `meta.on_failure_workflow: <workflow_id>`;run_failed
  落账后异步触发该图,task 自动注入
  `{"run_id", "error", "last_node", "workflow"}`(结构对齐 n8n 的载荷)。
  **防递归**:被触发的 run 自身失败不再级联(实现:触发链深 1,或
  meta 标记内部触发来源)。cost/timeout 守卫照常适用于触发 run。
- **验收**:主图失败 → 触发图以注入 task 运行;触发图再失败不级联。

#### P13 fork 运行(= P7 的语法糖)

- **参考**:CrewAI `restore_from_state_id`(新 id,历史保留);LangGraph
  checkpoint `parents` 谱系。
- **方案**:run 请求 `fork_from: <run_id>` + `node_overrides`——展开为
  「源 run 全部终态产物的 pinned_artifacts(P7)+ 自动跳过」,再叠加
  overrides(典型:换掉失败节点的模型)。不新建机制,复用 P7;
  `run_started` 记 `forked_from`,审计可见。
- **验收**:fork 一个 done run 换模型 → 只新节点花钱;账本可见血缘。

#### P14 从 run 恢复工作流定义(可选)

- **参考**:Dify 版本 Restore(旧版本完整载回草稿);Enju git 历史。
- **方案**:MCP `atlas_restore_workflow_from_run(run_id, workflow_id)`:
  从 `runs/<id>/spec.snapshot.json` 反转 `spec_to_snapshot`,走
  `atlas_save_workflow` 的完整乐观并发链(新 id 或 expected_sha256)。
  不引入版本表——per-run 快照已是完整版本史,缺的只是取回入口。
- **验收**:删除 workflows/ 中 YAML 后从历史 run 恢复出等价定义
  (spec_fingerprint 一致)。

---

## 6. 明确不采纳清单及理由

| 设计 | 来源 | 不采纳理由 |
|---|---|---|
| 可视化画布编辑器 | n8n / Dify / Seer / Approving | 违背「UI 只读、操控走 MCP/文件」红线;UI 复杂度天花板低正是稳定性来源;调研中无证据表明需要放弃 |
| 图=代码 + 确定性约束(禁随机/时钟/IO) | Temporal | 那是给命令式工作流代码用的;Atlas 图是数据,哈希断言路由 + 执行身份已解决同样的「重放可信」问题 |
| 队列模式 / 多 worker / Redis | n8n queue mode、BoundFlow lease | 本地单机单人产品;per-run OS 锁已覆盖真实并发面 |
| LLM 参与路由 | 部分低代码平台 | 路由是查表不是猜测(红线,`NoRouteError` 的存在就是理由) |
| 动态扇出(Send) | LangGraph | YAML 静态图哲学;map-reduce 已用无条件扇出表达;引入动态拓扑破坏可静态分析性 |
| 趋势式自动降级模型 | BoundFlow AgentRule | 有价值但依赖历史数据积累与多 run 统计,超出当前本地单机阶段;fallback 链已覆盖单次运行内降级 |
| 手改产物后钉扎 | n8n pin 手编 JSON | 破坏 sha256 完整性断言;完整性优先,用「改 YAML 后 fork」替代 |
| 密钥进 keyring | Prompt flow | 现行 `.env` + env 注入 + 全链路不回显已满足;引入 OS keyring 增加攻击面与平台差异 |

---

## 7. 实施阶段建议

依赖关系:P1 与 P4 必须同批(interrupted 检测 × MCP 生命周期互为兜底);
P3 依赖异常分类清理;P13 依赖 P7;P12 依赖 P1(触发 run 也要能恢复)。

- **阶段一(恢复与控制,预计最小批次)**:P1 暴露续跑 + interrupted 检测、
  P4 MCP 异步 + list_runs、P5 退避列表、P2 取消。
  → 交付后:「进程死了能救、对话不再卡、跑偏了能停」。
- **阶段二(容错与省钱)**:P3 on_error、P7 钉扎 + 跳过、P13 fork、
  P8 token 守卫。
  → 交付后:「节点失败不再废全图、调试不再全图重付、冷启动也有预算兜底」。
- **阶段三(体验 polish)**:P6 行号、P9 心跳、P10 保留策略、P11 三值
  批复;P12/P14 视需求。
- 每阶段收口:全量回归(223+ 测试)+ 新增验收测试;CHANGELOG 与
  `skill/SKILL.md`、`docs/mcp.md` 同步更新(工具面变了必须改文档)。

## 8. 来源索引

执行引擎:LangGraph(checkpoint base / RetryPolicy / human-in-the-loop,
github.com/langchain-ai/langgraph 与 docs.langchain.com);Temporal
(docs.temporal.io:workflows、retry-policies、worker-versioning、
task-queues、detecting-activity-failures);n8n(docs.n8n.io:
handle-errors-gracefully、types-of-executions、pin-and-mock-data、
work-with-nodes、manage-execution-data);Azure Prompt flow
(microsoft.github.io/promptflow:flow-yaml-schema-reference、
pf-command-reference、manage-connections、manage-runs)。

同类编排器:TrueForge(github.com/truefoundry/trueforge、truefoundry.dev);
Enju(github.com/tamerh/enju);BoundFlow(github.com/boundflow/boundflow);
agent-blueprint(pypi.org/project/agent-blueprint);Dify
(docs.dify.ai:predefined-error-handling-logic、loop、version-control、
step-run);CrewAI(docs.crewai.com/concepts/flows);Prefect
(docs.prefect.io/v3:retries、cache-workflow-steps);Dagster
(docs.dagster.io/deployment/execution/run-retries)。

竞品全景(第一轮检索,2026-08-18):TrueForge、Enju、BoundFlow、
agent-blueprint、Orchflow、Marchward、Humand、Approving、Kontrol、
Seer、AgentMesh、rp-engine、zenflow 等;Microsoft MXC
(github.com/microsoft/mxc,含 Windows Sandbox 后端)为未来 coding_agent
沙箱后端的首选基础。
