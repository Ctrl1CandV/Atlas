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
| 协作式取消（P2+D） | `atlas_cancel_run` 与 Web 运行页取消按钮(运行中/等待批准可见,确认框明示不可撤回)走同一请求文件；running 由 controller 在节点入口/候选切换/重试等待消费并唯一写 `run_cancelled`；paused/interrupted 由取消入口持锁直写终态；`local_cli` 在途执行收到取消即终止整棵进程树(Windows taskkill /T /F、POSIX killpg,真实子进程测试证明无孤儿),树杀失败大声失败不吞；SDK 模型调用仍跑完当次；`cancelled` 可删除、拒绝 resume/approve，请求不可撤回 |
| Controller 心跳（P9） | 每次 attempt 的派发窗口内定时写 `node_progress`（node/iteration/attempt/model/elapsed_ms/phase=waiting|retry）；只证明 controller 在等待，不声称模型内部进度或百分比；间隔默认与下限 30s，`ATLAS_NODE_HEARTBEAT_INTERVAL_S` run 级可配（低于下限大声拒绝）；窗口在 attempt 结束/失败/取消/终态后闭合，迟到 tick 被拒绝；fold 显式忽略该类型；容量代价如实计入：30s 一条 ≈ 每节点每天 2880 条（16 MiB 账本治理随 P10） |
| 产物导入与调用身份（P7） | 节点级 `imports: [{run, name}]` 从静稳终态 run 复制上游产物：源 stable lock 内校验 provenance 后字节克隆（temp+fsync+原子改名，写后复验），`artifact_imported` lineage 入账；每次 LLM 派发在 `node_started` 记 `invocation_sha256`（执行字段/有效 prompt/有序输入/后端身份，算法版本化）；invocation 完全相等且节点为无条件边 stop 策略 LLM 时零成本跳过（`node_imported_reused`），任一因子改变都不复用；跳过时刻按运行时 state 复核输入哈希，预测过期（同名产物被真实重跑覆盖）就委托真实执行；产物查找核对事件节点即生产者（多节点源不错配）；删除源 run 不影响已导入 run（克隆在本 run 内，绝不跨 run 路径引用）；缺源/运行中源在创建 run 目录前拒绝，源锁竞争确定性失败 |
| 三分支审批（P11） | human 节点 `approval_mode: binary|routed`（默认 binary=approve/reject，旧图指纹零变化）；routed 解锁第三决策 request_changes：必填非空意见、经保留键 `when: __changes__` 回边返回修订节点并消耗 max_iterations；三种决策共用同一锁内材料验证链（投影哈希/consumed/diff 摘要对照投影证据），binary 图收到 request_changes 在写事件前拒绝；领域校验单函数 engine/Web/MCP 同源；修改要求落 write-once `<node>.changes` 产物（新 role），成功审批覆写 route_facts 防残留误路由 |
| 运行保留/star/索引（P10） | `ATLAS_RETENTION_MAX_RUNS`/`ATLAS_RETENTION_MAX_AGE_DAYS` 默认全 null=永不自动删；候选选择是纯函数：只有 done/failed/cancelled 且无 star 进入可删除池（running/paused/interrupted、star、活跃 controller、无起点时间戳全部保护，保护对象不占配额；数量配额留池内最新 N 条、年龄阈值严格更老才切、双阈值并集）；删除走与 Web DELETE 同一执行器（stable lock 全程持有→同卷 .trash 隔离→no-follow 清理，tombstone 残留重试完成绝不复活）；star 是 run 内 write-once 标记文件（POST /api/runs/{id}/star 可带注记，取消=手工删文件无 API），任何有账本的 run 均可标（含 running）；列表轻量索引 `.runs-index.json`（size+mtime 指纹命中缓存、变更只重读该账本、剪枝对照全量成员、损坏大声重建），动态 interrupted 判定永不走缓存，列表结果与 full-fold 逐字段一致 |
| fork 与失效闭包（P13） | 图级 `fork: {run}` 从静稳终态源 run 再跑改过的图：静态重放两侧 invocation 身份（task 与未变上游产物哈希可从源账本静态取定）得 changed 集，闭包 = changed + 静态图全部后代（条件边计入；循环按 SCC 整体失效；join 命中 changed 分支必重跑）；闭包内禁止显式 imports（启动前拒绝）；闭包外且源事件证明产物完整的节点合成导入，走与显式 imports 完全相同的 P7 准入链与跳过门槛；`fork_planned` 全量入账（changed/closure/import map/算法版本 p13-fork-v1，run_started 带 fork_plan_sha256）；fork.run 进规格指纹（未 fork 旧图零变化）；dry-run 明示"重跑什么/从哪个 run 复制什么"；五类图（线性/并行/join/条件/循环）+ failed/paused 源均已测试 |
| 失败策略（P3） | 治理类异常永不可吞（费用/守卫/取消/deadline/规格/接线/路由/完整性/账本/审批/锁，未登记类型 fail-closed 按治理处理）；内容类失败（候选全部失败，含假成功与超时耗尽）可节点级 `on_error: stop/continue/branch`（默认 stop，旧图零变化，默认值不进指纹）；branch 走保留键 `__failed__`（校验期强制，每源至多一条，可与成功路径边型共存）；continue 拒绝条件出边；下游可消费 `<branch节点>.error`；软失败写 write-once 错误产物 + `node_failed_soft`，fold 显式忽略；__failed__ 路由按「节点最近一次结局」的 route_facts 事实判定（checkpoint 持久化，重入成功后不被残留错误产物误判）；AgentCliError 单独分类、白名单为空；Web/MCP 同源展示错误类与产物入口，dry-run 列出非默认 on_error 节点 |
| 终局可视化与总结（S1） | 终态 run 顶部零成本终局卡片：每节点一句话回顾（模型/耗时/token/成本/输出首段）+ 时间线，纯账本派生，Web 与 `atlas_get_run` 同源（`build_finale`）；图级 opt-in `summary: {model, prompt_hint?}` 在 run_done 前一次总结调用（进规格指纹与快照），成本走 CostLedger 受 `max_cost_usd` 约束，write-once 产物 + `run_summary_written`；失败记 `run_summary_failed` 不改终态；总结文本标注「LLM 叙述，事实以账本为准」；不做离线报告导出 |
| 工作流文件管理 | Web 页面可删除工作流；保存走 MCP 的 `expected_sha256` 读-改-写闭环（乐观锁防覆盖） |
| 零成本预检 | validate 与 dry-run 不调用供应商；`expected_execution_sha256` 可绑定预演与真跑身份；dry-run 对显式 `retry>0` 的 research/coding_agent 节点必现放大风险警告（K，RFC 已实施关闭），Web preview 与 MCP 同源透出 |
| search 检索节点（E-1） | 封闭类型 `search`：Atlas 自持后端（封闭枚举 tavily/searxng，key/base-url 缺失在预检位拒绝，dry-run 同样拦截）；写了 `model` 校验期拒绝。查询词三级来源（显式 ≤5 / 上游 JSON queries 截断至 5 并入账 / prompt 兜底单查询）；每次执行落 `search_performed` 事件 + write-once JSON 产物，`cost_usd`=后端实报或 null（不冒充 $0），有帽时派发前保守预留剩余预算。下游投影强制 `<untrusted-source>` 围栏 + 系统说明 + 闭合标签逃逸转义（围栏字节有正反例测试）；域名过滤只看初始 URL host（userinfo 伪装按 host 解析拒绝），不追重定向为如实限制。后端网络/HTTP 失败归内容类（`on_error` 可策略化，治理类照旧不可吞）；取消在每个 query 边界消费，`timeout_s` 覆盖整批；不进 P7 skip/P13 合成导入（复用=造假），显式 imports 保留 untrusted 标记 |
| 运行附件（E-2A） | MCP `atlas_run_workflow` 与 Web 运行接口接受 `attachments: [{name, path}]`：名字全小写 ASCII 正则（大写变体/同形 unicode 刻意拒绝）、保留后缀拒绝、不得叫 task 或撞节点 id；单件 ≤16 MiB、合计 ≤32 MiB；两阶段准入（read→size→SHA 在 run_id 分配前全量通过 → 统一原子落盘+写后复验，失败清理已落盘副本），不存在"带一半附件"的运行。字节克隆进 write-once 产物库（role=input），账本只记 name/sha256/bytes/basename，响应绝不回传源路径；下游经裸逻辑名 consumes 显式消费（保留后缀笔误仍在加载期拒绝），投影只含摘要行（名字·大小·sha256 前 12 位），原字节走产物工作台；`attachment_admitted` 事件紧跟 run_started，fold 显式忽略（回归锁）；消费附件的节点在 fork 时保守归 changed（新 run 附件字节可能不同），未消费附件的节点闭包不受影响；工具数仍 8（attachments 是参数） |
| agent collect（E-2B） | `agents.json` runner 配置的只读收集清单 `collect`（封闭字段，role 封闭 output/raw/report）：CLI 成功后按相对 glob 扫描执行目录（research=runner 实报临时目录，coding_agent=worktree；禁 `..`/绝对路径/反斜杠，symlink/junction 拒绝不追），命中文件 write-once 入库追加到 `node_done.artifacts` 尾部（相对路径字典序确定）；系统排除目录可追加不可删减；硬上限 ≤20 文件/单件 ≤16 MiB（产物上限）/合计 ≤64 MiB，超限与清洗同形逻辑名冲突均治理失败并带计数与字节事实；逻辑名 `{prefix}.{清洗相对路径}`（分隔符/点号折叠连字，保留 unicode 字母，非 ASCII 名可查看审批但裸名 consumes 只收 ASCII）；仅 local_cli 支持（runner 未报执行目录时响亮记账跳过）；不消耗模型调用；下游按裸逻辑名消费 |
| 发布 bundle（E-3） | Release 资产新增 bundle 包（干净代码树 git archive + 已构建 web/dist + manifest.json）；manifest 由与运行端同一函数写入（digest 排除 manifest 自身），打包阶段对暂存树与 zip 解包内容做两道排除名单机检（凭据/运行记录/缓存目录/密钥后缀），tracked 凭据混入当场失败；`atlas-web` 启动四级解析 dist（CLI `--dist` > `ATLAS_WEB_DIST` > 仓库 sibling > 包内 web-dist），全 miss fail-loud 附三条出路；manifest 哈希不符本地开发态 stderr 警告继续、CI 冒烟 job 断言相等否则 fail（同一函数两个调用侧）；Git 仍不跟踪 web/dist |
| 浏览器矩阵冒烟（E-4） | Playwright chromium e2e（`e2e/` 独立 npm 包，`npx playwright test`）：`helpers/server.py` 以 FakeProvider registry_factory 真实执行预种两个 run——gate run 跑到 human(routed) 门暂停（批准后由同一 FakeProvider 注册表续跑）、done run 跑完后归一 ts/duration_s 使终局卡片数字跨次确定，registry 全程零真实供应商调用；键盘审批流用例：Tab 焦点环按 DOM 序可达 批复说明→批准→要求修改→驳回，焦点可见断言（输入框 box-shadow 环、按钮 outline ≥2px），Shift+Tab 回位→keyboard.type 批复→Tab→Enter 等价点击续跑到终局卡片，全程禁 mouse.*、选择器只用 role/text/aria；终局卡片基线截图入仓 `e2e/__screenshots__/`（locale zh-CN + 时区 Asia/Shanghai + reduce-motion 锁定，两连跑逐字节一致，容差保持默认未调，不一致必须根因修复）；`e2e-smoke.yml` optional workflow（workflow_dispatch + release 触发，不并主双绿门，失败上传 traces 与截图 diff） |
| 可审计运行 | append-only JSONL 事件、write-once 产物、读取时 SHA-256 断言、有效规格快照 |
| 成本保护（P0min） | 有 `max_cost_usd` 时派发前持久化 reservation；未知费率保守占用剩余预算；无 cap 不虚构金额 |
| 崩溃恢复（P1） | 动态派生 `interrupted`；只有 interrupted 可 resume；paused 只能 approve/reject |
| 人工审批 | 暂停条列出待审材料（消费产物 + 完整投影，带 SHA-256）并可放大审阅；驳回必填理由，前端与 API 同步强制 |
| YAML 位置（P6） | 语法和主要语义错误返回 path/line/column；聚合错误不编造坐标 |
| Agent 执行 | 显式 `runner: local_cli` 才启用；缺配置或预检失败时在创建 run 前 fail-closed |
| Agent 改动证据 | 冻结 baseline，在副本执行，以普通文件字节清单生成完整文本 unified diff，审批绑定三摘要 |
| 本机 Web | 查看运行、输入输出、成本和产物（成本栏含未决预留额）；启动、审批、恢复 interrupted run、取消运行中/等待中的运行、下载完整事件账本、删除终态 run；运行列表自动轮询（MCP/其他标签页发起的运行无需手动刷新）；设置页有 agents.json 只读状态卡（启用/预检结论/失败原因），编辑仍走文件；管理本地配置 |

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

- Windows 支持平台 job 全链路通过：locked sync、`atlas init`（含 UTF-8 stdio 修复，cp1252 控制台不再崩溃）、Web 测试/lint/build、全套测试（2026-08-27 批次 E-2B 起为 664 passed / 2 skipped，其中新增 skip 为无 symlink 权限账户下的 collect 链接拒绝测试，随批次增长）、离线发布门、密钥/路径扫描、sdist 构建 + lock 约束冒烟安装。
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

- 取消是协作式的：在途模型调用与 agent CLI 执行会跑完当次尝试才在下一节点边界终止（`local_cli` 进程树除外——收到取消即整树终止）；取消请求一经写入不可撤回（控制器死亡后 resume 会在首个节点边界消费它）。
- 回边循环的 `consumes` 是静态的：重跑轮输入与首轮相同，不携带触发重跑的审查意见——是"有界重试"而非"按批注修订"。反馈可见需要显式消费 `reviewer.output` 的修订节点；语义改进见 BACKLOG"循环携带反馈"。
- agent 自动 retry 的预算约束尚未落地（RFC 草案 `docs/rfcs/agent-retry-budget.md` 待评审）。
- 节点失败默认终止整图。human gate 的三分支已交付：request_changes 需图作者显式 `approval_mode: routed` 并接线 `__changes__` 回边；「循环携带反馈」的完整语义仍是 Stage E 独立 RFC 议题——P11 通过显式消费 `<node>.changes` 已可实现反馈可见的修订环。
- retention 的清理触发点是"每次图执行完成后顺路清扫"；长期不跑图、也不起 atlas-web 的冷目录不会被自动清（可用任意一次执行或手工 DELETE 驱动）。fork 只复用源事件证明完整的产物，源 run 中从未执行/未完成的节点诚实重跑。索引目前不含成本摘要列（列表本就不展示成本）。
- release **sdist** 不包含 built `web/dist`（Git clone 用户仍需 Node.js 构建一次前端）；官方 Release 自 E-3 起附 **bundle 包**（干净代码树 + 已构建前端 + manifest 哈希），`atlas-web` 经四级解析自动识别（CLI `--dist` > `ATLAS_WEB_DIST` > 仓库 sibling > 包内 web-dist），manifest 哈希不符时本地开发态警告继续、发布冒烟断言相等。
- Claude CLI 当前没有硬 `max_turns` 参数；`seed`/`temperature` 只进请求体，供应商是否尊重未验证。
- 阶段 D 曾出现一次 Kiro agent 首次 attempt 自报约 `$10.508`，随后自动 retry 被人工终止。直接原因是图没有 `max_cost_usd`、本地 pricing 全为 `null`，CLI 预算没有生效。所有真实 agent 示例都应配置预算；自动 retry 的默认策略需先经 RFC 决策，不能在没有评审时静默改行为。
- 本地 pricing 全 `null` 时设 `max_cost_usd` 的实际表现：首节点结算即按预留全额计入，后续节点会被"没有剩余预算"拦截（2026-08-22 run `20260822-113908-531dab` 实证）。要么填入确认过的费率，要么不设帽改用结构性约束控成本。

## 接下来

统一排期与审查问题的通俗解释见 [`PLAN-post-audit-2026-08-22.md`](PLAN-post-audit-2026-08-22.md)；各项实施合同见 [`ROADMAP.md`](ROADMAP.md)。原 rc.1 与 benchmark 计划已经关闭，只保留为历史记录：

- [`PLAN-rejection-reduction.md`](PLAN-rejection-reduction.md) — 减少截断/非法 JSON/成本帽三类拒绝性错误的已评审方向
- [`PLAN-rc1-followup.md`](PLAN-rc1-followup.md)
- [`PLAN-benchmark-optimizations.md`](PLAN-benchmark-optimizations.md)
- [`archive/README.md`](archive/README.md)
