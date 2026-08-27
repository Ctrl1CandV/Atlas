# 未实施功能清单（Backlog）

> 状态：2026-08-21 整理。本文是排期视图；实施合同与验收标准的权威来源是 [`ROADMAP.md`](ROADMAP.md)，本文不重复其细节。**这里列的都不是当前能力**——完成必须以代码、事件兼容、测试和用户文档同时落地为准。
>
> 复杂度依据 2026-08-21 的代码审计标注：风险主要来自 Windows 进程树终止、终态竞争，以及不能破坏现有的 SHA/事件重放/成本预留合同。

## 已完成（2026-08-23）

- **P4 共享 launcher + MCP 异步 + `atlas_list_runs`** ✅：`atlas/launcher.py` controller registry；`wait=false` 预检后返回 run_id；`atlas_list_runs` 降序稳定分页；Web/MCP 共用 `runs.build_run_summary`。
- **P2 协作式取消** ✅（部分范围）：`run_cancelled` 终态、`atlas_cancel_run`（第 8 工具）、Web API 端点、llm/human/agent 入口与重试等待的消费点、agent retry 预算 RFC 草案。~~CLI 进程树终止、Web 界面取消按钮、`--max-budget-usd` 映射~~ 已随 D1–D3 交付（见下）；剩余 **agent retry 默认策略的 RFC 裁决**（D4）待用户拍板后实施。
- **P9 controller heartbeat** ✅（2026-08-26）：每次 attempt 派发窗口内 `node_progress`（attempt/model/elapsed_ms/phase）；间隔默认与下限 30s、`ATLAS_NODE_HEARTBEAT_INTERVAL_S` run 级可配；窗口在 attempt 结束/失败/取消/终态后闭合，迟到 tick 拒绝；fold 显式忽略；事件容量代价（30s ≈ 2880 条/节点/天）写入 STATUS,分段账本治理随 P10。
- **P3 异常 taxonomy + 节点 on_error** ✅（2026-08-26）：`atlas/exc.py` 分类层（治理永不可吞，未登记 fail-closed）；节点级 `on_error: stop/continue/branch`（默认零变化、默认值不进指纹）；branch 走保留键 `__failed__`（校验期强制）；软失败写 write-once 错误产物 + `node_failed_soft`，fold 显式忽略（删事件回归锁定）；Web/MCP/dry-run 同源展示。
- **P2 残余强化 D1–D3** ✅（2026-08-26）：Web 取消按钮（running/paused 可见,确认后走 `/api/runs/{id}/cancel`,HTTP 契约 403/404/409/paused 直写有测试）；`local_cli` 取消终止整棵进程树（watcher 轮询 cancel.request → taskkill /T /F 或 killpg,真实孙进程 kill 测试,树杀失败保持 AgentCliError fail-closed）；`--max-budget-usd ≤0` 派发前拒绝（有效映射/缺参预检/超支报告已有测试）。**D4(agent retry RFC 决策)未含——等用户裁决后再实施。**
- **S1 终局可视化 + 总结节点** ✅（2026-08-26）：终态 run 顶部零成本终局卡片（纯账本派生,Web/MCP 同源 `build_finale`）;图级 opt-in `summary: {model, prompt_hint?}` 在 run_done 前一次总结调用,write-once 产物 + `run_summary_written`,失败记 `run_summary_failed` 不改终态,成本受 `max_cost_usd` 约束,dry-run 明示;**不做离线导出**（用户定案）。

## 总览表

| 建议批次 | 编号 | 功能 | 用户可见效果 | 估算 | 风险 | 依赖 |
|---|---|---|---|---|---|---|
| R0 | — | 发布与仓库治理 | tag / 构建资产 / provenance 指向同一 commit；默认分支迁到 `main` 并保护；公开 CI 策略确定 | 低（流程性） | 低 | 无 |
| 第二批 | P9 | controller heartbeat | 区分"控制器在等模型"和"事件流断了"；显示 attempt 上下文 | 3–5 人日 | 低 | P2（✅ 已完成,见上） |
| 第二批 | P3 | 异常 taxonomy + 节点 `on_error` | 内容型节点失败可按图作者策略 continue 或走 `__failed__` 分支，而不是终止整图；治理异常（费用/完整性/审批）永不被吞 | 7–11 人日 | 中 | P2 的 RunCancelled（✅ 已完成,见上） |
| 第二批b | S1 | 执行终局可视化与总结节点 | run 结束后 Web 顶部"终局总结"卡片（零成本、纯账本派生）+ opt-in 总结节点（最终结果+各节点工作回顾，write-once 产物+事件）；2026-08-23 用户定案，**不做离线报告导出**（原 Stage E 条目移除） | 5–8 人日 | 中 | P4 的 summary builder（✅ 已完成,见上） |
| 第三批 | P7 | artifact import + invocation hash | 新 run 可安全复用旧 run 的昂贵上游产物（字节复制 + lineage 事件）；执行身份相同可自动 skip | 7–11 人日 | **高** | 现有 SHA 合同 |
| 第三批 | P13 | fork 与失效闭包 | 改一个节点的 prompt/model 后只重跑它和受影响后代，兄弟分支结果保留 | 4–7 人日 | 高 | P7 |
| 第三批 | P10 | retention / star / run index | 按数量/年龄自动清理运行（star 保护），轻量索引加速列表 | 4–7 人日 | 中 | P7 先行 |
| 第三批 | P11 | request_changes / routed approval | 审批从 approve/reject 二值扩展为三分支："要求修改"成为有审计、受循环上限约束的控制流 | 4–7 人日 | 中 | 现有审批校验 |

## Stage E（独立价值项，各自 RFC）

| 项目 | 效果 | 备注 |
|---|---|---|
| 循环携带反馈 | 回边重跑轮的输入包含触发重跑的审查意见,多轮收敛的反馈循环成为可表达语义 | 现状(2026-08-22 审查确认):`consumes` 是静态的,回边轮与首轮输入相同——是"有界重试"而非"按批注修订"。两个示例 YAML 与 skill 文档已改为如实措辞;反馈可见需像 mcp-adhoc 图那样加显式消费 `reviewer.output` 的修订节点。与 P11(request_changes)同属"循环语义"设计议题,实施前需 RFC 定语义(产物命名/迭代索引) |
| LLM `web_search` | llm 节点获得联网搜索能力（provider tool-calling + 可插拔后端），来源与成本落产物 | 需处理网页内容注入与预算 |
| Release 内置已构建前端 | 使用者免 Node.js 即可启动 | Git 仍不跟踪 `web/dist`；前端哈希进 provenance |
| OS 级沙箱调研 | 与 `local_cli` 并列的真实隔离后端（Windows Sandbox / WSL） | 目前目录副本明确不是沙箱 |
| 浏览器矩阵 GUI 测试 | 主题、键盘调栏、200% 缩放的可重复渲染验证 | 不用源码字符串代替截图 |
| 节点通讯文件 | 多命名产物（`outputs`）、agent 文件收集（`collect_files`）、运行附件（`attachments`） | 见 [`rfcs/node-io-files.md`](rfcs/node-io-files.md)，三阶段独立验收 |

## 明确移除项（不排期）

P5 可配置 retry backoff、P8 token guard、P12 durable failure workflow、P14 从 run 恢复 YAML。重新立项条件见 ROADMAP §13。

## 排序依据

1. 能否停止真实费用（P2 最优先的业务理由：2026-08-19 曾发生 agent 自报 ~$10.5 且自动 retry 放大的事故）；
2. 是否统一 Web/MCP 状态（P4 是 cancel/heartbeat/retention 的公共前置）；
3. 是否保持事件可重放；
4. 是否避免重复实现安全边界。

每批的通用验收底线：旧事件可读、状态可从事件重放、Web/MCP/API 共用领域函数、dry-run 零花销、real-API 测试默认排除、Python/Web 测试与文档同步。涉及崩溃或并发的能力必须有真实子进程 kill/竞争测试。
