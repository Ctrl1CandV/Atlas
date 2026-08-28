# 验证报告（2026-08-28）

> 本报告是发布面的质量凭证入口。开发/测试套件（Python 单测与图夹具、浏览器
> e2e、Web 内嵌单测）保留在维护者的私有开发树中，**不随本公开仓库分发**；
> 本文记录基线数字、真实运行证据与已知发现，供使用者与审计者核对。
> 数字与命令输出一一对应，不手写宣称。

## 1. 自动化基线（2026-08-28，维护者开发树）

| 套件 | 结果 | 说明 |
|---|---|---|
| Python 全量 | **666 passed / 2 skipped / 5 deselected** | 5 个 `real_api` 用例默认排除（需一次性凭据、可能计费）；2 个 skip 为无 symlink 权限环境的链接拒绝测试 |
| Web 单测 | **22 passed** | 逻辑层（diff 解析/停靠布局/订阅等） |
| Web lint / build | **0 告警 0 错误 / 构建通过** | oxlint + tsc -b + vite build |
| 浏览器 e2e（Playwright chromium/Edge/Firefox） | **7 项全过，两连跑截图逐字节一致** | 键盘审批流 ×3 浏览器 + 终局卡片 dark/light × 100%/200% 四组合基线 |
| CI（公开，GitHub Actions） | 三 job 全绿 | Windows 支持平台 / Web dist bundle 冒烟 / Ubuntu 兼容性信号 |

CI 在公开树上执行的门：锁定依赖同步、`atlas init` 干净初始化、Web lint/build、
Python 字节编译、六工作流离线 validate/dry-run、干净初始化与 MCP 静默门、
发布面秘密/私有路径/占位符扫描、sdist 构建 + 锁约束冒烟安装、
**文档契约门**（`scripts/docs_contract_gate.py`：反 OS 隔离宣称绊线 +
双语边界事实 + MCP 工具数口径，内置正反样例自证）。

## 2. 真实端到端验证（2026-08-28，本机真实供应商）

方法：以生产配置启动 `atlas-web`（真实 runs 目录 + 真实模型供应商），自建
MCP streamable-http 客户端直连 `/mcp`，8 个工具逐一真调；Web API 以 HTTP
客户端同步覆盖。真实开销：**26 次模型派发，tokens in 2,695 / out 6,056**
（微型任务、最便宜计费模型；公开费率折算不足 0.01 美元）。

| # | 验证项 | 结果 |
|---|---|---|
| 1 | MCP 工具面 | `tools/list` 恰好 **8** 个，名称与文档口径逐一对合 |
| 2 | 工作流库 | `atlas_list_workflows` 全部 valid；6 个示例 `atlas_validate_workflow` 全过（返回 `file_sha256`） |
| 3 | 准入 fail-closed | 未登记供应商被拒并列出全部已有供应商；search 节点缺 `TAVILY_API_KEY` 预检拒绝（"不猜、不降级"） |
| 4 | 真实运行（llm ×2 + 总结节点） | done；真实 token 入账；投影含任务原文与上游产物逐字原文（完整性语义）；产物下载 200；`atlas_get_run` 幂等（两次调用字节一致）；账本 12 事件含 `run_summary_written` |
| 5 | fork 复用（零开销路径） | 同图 fork：changed/closure 为空，2 节点全部导入复用，**0 次模型派发**；仅改 reviewer prompt：**只重跑 reviewer**，writer 复用——失效闭包精确成立 |
| 6 | 运行附件 | 准入 SHA 与源文件一致；产物库字节克隆；投影仅摘要行、原文不内联；账本只记 basename，响应与账本均无完整源路径 |
| 7 | 人工审批（binary） | paused → Web `approve` → done，审批事件入账 |
| 8 | 人工审批（routed 三分支） | `request_changes`（必填意见）→ 修订回边真实执行（producer→gate→reviser→producer→gate）→ 再 paused → `approve` → done；决策序列 `[request_changes, approve]` 完整入账，`<node>.changes` 产物落库 |
| 9 | 协作式取消 | `wait=false` 立即返回 → `atlas_cancel_run` → `run_cancelled` 终态 |
| 10 | 崩溃恢复 | 运行中硬杀服务器进程树 → 重启 → 动态状态正确判为 `interrupted`（非"运行中"假象）→ `atlas_resume_run` 从 checkpoint 续跑 → done |
| 11 | star/删除/列表 | star 200、重复 star 409（write-once）、**星标 run 删除被拒**；failed run 删除 200→GET 404；`atlas_list_runs` 最新优先 |
| 12 | 保存乐观锁 | 陈旧 `expected_sha256` 更新被拒；正确 sha 更新成功 |
| 13 | 失败路径（真实世界） | 供应商偶发空响应 → `AllCandidatesFailed` 响亮失败，账本完整记录每次 `model_failed` 原因 |
| 14 | 成本记账诚实性 | 本部署未填单价（pricing 显式 null）→ 所有调用 `cost_usd=null` 并计入 `actual_cost_unknown_count`，**不冒充 $0**；填入单价后自动转真实成本口径 |

## 3. OS 级隔离调研结论（2026-08-28）

按预先定义（先于实测提交，git 历史可证）的 GO/NO-GO 判据，对
WSL2（Ubuntu 24.04.2 / WSL 2.7.11 / Windows 10 Pro）真实 spike 得出
**NO-GO**：启动开销与文件 IO 劣化达标（冷启动 7.2s ≤ 10s；drvfs 写 368 MB/s
对比原生 ext4 1.9 GB/s，劣化 5.2× ≤ 10×），流式回传/退出码/env 白名单
（73 → 19 条，`WSLENV` 显式放行）全过——但**取消不级联**：硬杀 Windows 侧
`wsl.exe` 客户端后 setsid 脱离的 Linux 孙进程存活并继续服务（独立客户端
实证），仅 `wsl --terminate` 能回收。结论：**agent 保持同用户宿主进程现状**，
README/SECURITY 的边界表述不变；缓解路径（专用发行版 terminate、发行版内
取消看门）留待真实需求出现时独立立项。

## 4. 已知发现

1. **低危**：运行进行中 `GET /api/runs/{rid}/events.jsonl` 可能因账本并发追加
   超出预先声明的 Content-Length 而被截断（服务器日志 `h11 LocalProtocolError`，
   复测即恢复）。终态后不受影响；活跃期界面走 SSE 事件流。修复方向：读时刻
   定长快照。
2. search 节点的后端 happy path 未做真实外网调用（部署未配置
   Tavily/SearXNG 凭据）；其 fail-closed 预检已真实验证，注入围栏与后端协议
   由开发树中的字节级正反例测试覆盖。

## 5. 复核指引

公开树可复核的部分：`uv run python scripts/docs_contract_gate.py`（文档契约）、
六示例工作流的离线 validate/dry-run、`atlas-web` 启动与 `/mcp` 探活、
Web lint/build。开发树复核（维护者）：`uv run pytest -q`、`npm --prefix web test`、
浏览器 e2e（`npx playwright test`）。
