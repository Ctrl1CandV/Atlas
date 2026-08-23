# Atlas 后续路线图

> 状态：2026-08-19 起生效。这里的“计划”都不是当前能力；完成必须以代码、事件兼容、测试和用户文档同时落地为准。

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

## 2. R0：发布与仓库治理

### 价值

让 tag、源码、构建资产和 provenance 指向同一 commit，避免用户验证出“证明有效但不是 tag 源码”的结果。

### 当前缺口

- `v0.1.0` tag 为 `4f9b0b5…`，当前 Release 资产从 `d34d785…` 构建。
- 默认分支仍叫 `release/v0.1.0-rc.1`，公开分支未保护。
- v0.1.0 之后的 README、截图和 release workflow 改动不属于 v0.1.0 tag/source package。
- 公开仓库没有完整 Windows CI；本地 untracked workflow 不能被宣传为公开 CI 证据。

### 实施合同

1. 不移动、重签或静默替换 `v0.1.0`；在发布记录中保留 as-built truth。
2. 下一发布建议为 `v0.1.1`：release workflow 从 exact tag checkout，断言 `HEAD == tag^{commit}`，版本与资产名从 tag 派生；证明和资产只针对该 checkout。
3. 在确认 GitHub 默认分支与外部链接后，把正式开发默认分支改为 `main`；启用禁止 force-push/删除及必要 status checks。
4. 明确公开 CI 策略：若继续不公开 tests/scripts，README 不能展示虚假的 CI badge；若公开 CI，则只上传脱敏、无 run/config 的产物。
5. 发布资产默认不可覆写；修复用新 patch version，不反复替换同一 Release 文件。

### 验收

- `git rev-list -n 1 <tag>`、workflow `head_sha`、provenance `gitCommit` 三者完全相等。
- 下载资产逐项匹配 `SHA256SUMS` 和 attestation subjects。
- clean Python 3.12 离线 smoke 使用下载资产，而不是本地另建的同名文件。
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
- 明确 Atlas `max_cost_usd` 与 Claude CLI `--max-budget-usd` 的映射、舍入、失败和 unknown 行为；无法传递时 fail-loud 或明确降级，不静默假装已限额。
- 自动 retry 会放大 agent 花销。先写 RFC 决定“agent 默认 `retry=0`”或“只有存在可执行预算时才允许 retry”；在决策前不改兼容行为。

### 验收

- 并发重复 cancel 只写一个请求和一个终态。
- Windows CLI 子孙进程全部退出，无 orphan；取消后不会自动 retry。
- HTTP 调用的延迟取消语义在 API/UI 中明确展示。
- cancel 与 approve/resume/完成的竞争测试覆盖所有顺序；seq、checkpoint、cost fold 可重放。
- 使用桩 CLI 证明 `--max-budget-usd` 的有效值、缺失、拒绝和超支报告路径。

估算：6–10 人日（含成本 RFC 与 UI/MCP）。

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

## 11. Stage E：独立价值项

这些项目不自动跟随主线，应各自形成 RFC/小批次：

| 项目 | 价值与边界 | 建议验收 |
|---|---|---|
| LLM `web_search` | provider tool-calling + 可插拔搜索后端；来源、查询、成本和条数落产物；不能把结果当可信事实 | tool schema/fallback/来源完整性/预算/恶意网页 prompt injection 测试 |
| Release 包含 built frontend | 让使用者免 Node；Git 仍不跟踪 `web/dist` | clean machine 解压即可启动，frontend hash 进入 manifest/provenance |
| Run report export | 自包含 HTML 或 ZIP+manifest，含时间线、成本、产物、diff、完整性哈希 | 离线打开、敏感字段明确、每个导出文件可校验；LLM 摘要只能是可选叙述层 |
| OS-level sandbox | 调研 Windows Sandbox 或 WSL backend；与 `local_cli` 并列而非把副本改名为沙箱 | 宿主路径/网络/凭据/输出回收威胁模型和逃逸测试 |
| Browser matrix | 主题、键盘调栏、200% 缩放、系统 Edge/Chromium | 可重复 GUI 测试与真实截图，不用源码字符串代替渲染验证 |
| 节点通讯文件 | 多命名产物、agent collect、attachments；详见 [`rfcs/node-io-files.md`](rfcs/node-io-files.md) | 每阶段保持 write-once、hash、no traversal、size caps 和 projection 完整性 |

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
