# 未实施功能清单（Backlog）

> 状态：2026-08-21 整理。本文是排期视图；实施合同与验收标准的权威来源是 [`ROADMAP.md`](ROADMAP.md)，本文不重复其细节。**这里列的都不是当前能力**——完成必须以代码、事件兼容、测试和用户文档同时落地为准。
>
> 复杂度依据 2026-08-21 的代码审计标注：风险主要来自 Windows 进程树终止、终态竞争，以及不能破坏现有的 SHA/事件重放/成本预留合同。

## 总览表

| 建议批次 | 编号 | 功能 | 用户可见效果 | 估算 | 风险 | 依赖 |
|---|---|---|---|---|---|---|
| R0 | — | 发布与仓库治理 | tag / 构建资产 / provenance 指向同一 commit；默认分支迁到 `main` 并保护；公开 CI 策略确定 | 低（流程性） | 低 | 无 |
| 第一批 | P4 | 共享 launcher + MCP 异步 + `atlas_list_runs` | `atlas_run_workflow(wait=false)` 立即返回 run_id，长任务不再占住 MCP 会话；MCP 能列出历史运行 | 4–7 人日 | 中 | P1（已完成） |
| 第一批 | P2 | 协作式 cancel + agent 成本停损 | 界面/MCP 发出取消请求后进程树真正退出；agent 自动 retry 受预算约束；`cancelled` 成为终态 | 6–10 人日 | **高** | P4 |
| 第二批 | P9 | controller heartbeat | 区分"控制器在等模型"和"事件流断了"；显示 attempt 上下文 | 3–5 人日 | 低 | P2 |
| 第二批 | P3 | 异常 taxonomy + 节点 `on_error` | 内容型节点失败可按图作者策略 continue 或走 `__failed__` 分支，而不是终止整图；治理异常（费用/完整性/审批）永不被吞 | 7–11 人日 | 中 | P2 的 RunCancelled |
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
| 运行报告导出 | 自包含 HTML/ZIP 报告（时间线、成本、diff、哈希），离线可开 | LLM 摘要只能是可选层 |
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
