# 调研结论（历史快照）

> **历史调研，不是当前产品文档。** 结论和外部项目状态只对应 2026-08-16，部分来源未走官方渠道二次确认，机器路径也只属于当时实验环境。当前 Atlas 事实见 [`../STATUS.md`](../STATUS.md)，未来路线见 [`../ROADMAP.md`](../ROADMAP.md)。

**调研日期：2026-08-16。** 方法：本地源码核实、HN Algolia / GitHub API 原始抓取，以及四个不同厂商模型的独立分析后聚合。外部事实具有时效性，使用前必须重新核对原始来源。

---

## 目录

1. [赛道现状：可视化编排正在死，以及死法](#1-赛道现状)
2. [代码优先 vs 图形优先](#2-代码优先-vs-图形优先)
3. [安全：任意代码节点是灾难](#3-安全任意代码节点是灾难)
4. [LangGraph 的真实状况](#4-langgraph-的真实状况)
5. [deepseek-harness 源码调研](#5-deepseek-harness-源码调研)
6. [为什么不拿 deepseek-harness 当依赖](#6-为什么不拿-deepseek-harness-当依赖)
7. [可观测性与成本](#7-可观测性与成本)
8. [科研场景](#8-科研场景)
9. [实测：稳定性的真实难点](#9-实测稳定性的真实难点)
10. [方案演变记录](#10-方案演变记录)
11. [第二轮验证：拿写好的文档去证伪](#11-第二轮验证拿写好的文档去证伪)
12. [v1 范围决定：砍掉 UI 编排，操控走 skill + MCP](#12-v1-范围决定砍掉-ui-编排操控走-skill--mcp)

---

## 1. 赛道现状

**这个赛道在 2026 年正在死，死法值得抄作业。**

| 产品 | 状态 | 出处 |
|---|---|---|
| Flowise | 官方停服，2026-08-05 公告 | https://flowiseai.com/sunset ・ HN 49176920 |
| OpenAI Agent Builder | 已宣布弃用，2026-11-30 关停 | — |
| Relay.app | 同期停服 | — |

Flowise 官方停服理由原文：开发者转向 coding agent，且
*"the typical rigid workflow low code approach quickly hits the limit when it comes to complexity"*。

时间线值得注意：Workday 于 2025-08-14 收购 Flowise 并公开表示"doubling down, only just getting
started"，约一年后关停。

### 用户实测批评（HN 49176920 评论区）

用户 `nirava`（自托管 Flowise 做内部工具后放弃）列了四条：
① 随 flow 数量增加明显变慢，"felt like O(n²)"；② bug 多、UX 差；
③ **flow 变量系统半成品且文档缺失**；④ 功能缺口。

用户 `delegate` 指出的两条更根本：
- 图编辑器有"磁性吸引力"，但问题复杂度上升一档，图就变成"你不敢碰的 spaghetti"
- **UI 范式反过来驱动建模** —— 你开始按图编辑器能表达什么来思考问题，而不是按问题本身

用户 `maxdo`（在 Flowise 之前做过 flow.ai）："drag-n-drop UI dead on arrival"，
用户卡在"会写代码"和"完全不会"之间的窄缝里。

用户 `juancn` 提到的成本问题：这类工作流"很容易一不小心烧掉天量 token"。

**→ 对本项目的影响：红线 ②（图的真相在文件里，界面只做观测与编辑）。**

### 未找到的东西（诚实标注）

没有找到任何团队公开写"我们从 Langflow/Flowise 迁走了因为……"的迁移复盘。
GitHub 上搜这两个项目的 version control / merge conflict 类 issue 返回 0 条相关结果。
**推测**（非事实）：这类用户不写复盘就静默离开。

---

## 2. 代码优先 vs 图形优先

### 框架的真实代价

Arize 的对照实验 "Should I Use a Framework to Build an Agent? Code vs. LangGraph vs. Workflows"
（HN 41722243，作者已在生产跑自研 agent 8 个月）指出摩擦点集中在**被迫接受框架抽象**：
即使不用 `ToolNode`，只要用 `bind_tools` 就得用 `@tool` 装饰器，随之而来是 Pydantic 对
`self` 参数报错。

LangChain 维护者 `nfcampos` 亲自回帖反驳"节点就是普通函数"，作者回应"那你就几乎没在用这个框架了"。
**这段对话是抽象边界之争最好的一手材料**，值得在决定"用框架多深"时重读。

正面案例：Qodo 公开写了 "Why we chose LangGraph to build our coding agent"
（https://www.qodo.ai/blog/why-we-chose-langgraph-to-build-our-coding-agent/ ，HN 83 分）。

### 图形为单一真相的代价，以及产品缺口

HN 44326536：一个团队同时用 LangFlow（低代码）和纯代码，明确说**代码侧缺三样东西**：
- 复杂流程 / 决策树的检视
- 多 agent 交互的调试
- 在管线中间阶段注入测试输入

他们想要的是"能 parse 现有 agent 代码并生成可视化"的工具。

AutoGen Studio 的对称证据：issue #1287 "ability to export agents and workflows as python code"
—— 用户明确要求逃出 GUI。

**→ 这正是本项目的产品位置：代码/声明式定义为真相，可视化做观测与调试。**

---

## 3. 安全：任意代码节点是灾难

**这是最被低估的工程教训。给可视化平台加"任意代码节点"，等于把 RCE 做成产品功能。**

### Langflow

| 事件 | 出处 |
|---|---|
| Python `exec` 逃逸口造成**未认证 RCE**（CVE-2025-3248） | horizon3.ai 披露：https://horizon3.ai/attack-research/disclosures/unsafe-at-any-speed-abusing-python-exec-for-unauth-rce-in-langflow-ai/ |
| 被真实勒索软件利用（"JadePuffer"） | Sysdig 报告，2026-07 |
| 又披露 4 个漏洞 | Rubrik ZeroLabs，2026-08-11 |

### Flowise

| 事件 | 出处 |
|---|---|
| 最高危 RCE **已在野利用** | BleepingComputer，2026-04 |
| CVE-2026-40933 一键 RCE | — |
| 一篇独立研究挖出 6 个 RCE | elttam |

**→ 红线 ①。节点类型必须是封闭枚举，参数是数据不是代码。**

### 网络暴露的参考做法

deepseek-harness 的处理值得照抄：`--host 0.0.0.0` 在 CLI 层**硬性拒绝**，
报错原文是"it would expose remote code execution to the network"
（`packages/bundle/web-app/src/startup.ts:69-70`）。

它的 web server **完全没有认证**（零 token、零密码、零 cookie），只有一层
Host / Origin / Sec-Fetch-Site 围栏防 DNS rebinding，代码注释明确写着
*"this fence is not an auth layer"*（`packages/client/connection/src/api-request-trust.ts:12-13`）。

它这个判断是对的，理由写在 `connection/src/index.ts:100-103`：默认 agent preset 就带 bash
和文件系统工具，任何能创建 session 的调用方都能以该进程身份执行命令。

**→ 红线 ④。**

---

## 4. LangGraph 的真实状况

### 已知 open bug（全部亲自核对过 issue 内容，未复现）

| issue | 内容 | 是否在本项目路径上 |
|---|---|---|
| [#7780](https://github.com/langchain-ai/langgraph/issues/7780) | `interrupt()` 在循环里导致多余 resume（2026-05 至今开放） | **否** —— v1 不做节点内中断 |
| [#7256](https://github.com/langchain-ai/langgraph/issues/7256) | time travel / replay 时恢复 interrupt 不正确。**由维护者自己提的**，标 privileged | **否** —— v1 不做时间旅行 |
| [#6107](https://github.com/langchain-ai/langgraph/pull/6107) | Postgres checkpointer 在 DB 每日重启后抛 "connection is closed"，无重连（企业用户自己提的补丁） | **否** —— 本地单机用 SQLite |
| #5259 | `update_state` 的 checkpoint 更新在 Studio 里不可见，提交者称这让它 "useless" | 部分 —— 不依赖 Studio 做真相展示 |

**规律：这些 bug 集中在 interrupt / checkpoint / time-travel 一带，即"节点执行中途暂停并恢复"。**

Dify 的对称证据：Human Input 恢复后 Answer 节点输出不显示也不持久化（1.15.0），
https://github.com/langgenius/dify/issues/38432 。

**跨平台的共同规律：HIL 的"暂停"好做，"恢复"是所有平台的共同弱点。**
→ 这直接决定了本项目的 v1 范围：只做节点边界暂停，不做节点内中断。

### durable execution 的成熟路线（备选，非 v1）

- Temporal 官方叙事 + 博客 "Why Agentic Flows Need Distributed-Systems Discipline"
- Duralang：一个 decorator 把每个 LangChain LLM/tool/MCP 调用变成 Temporal Activity（temporal.io/code-exchange）
- 官方 SA 维护的 OpenAI Agents SDK + Temporal durable 示例（github.com/steveandroulakis/openai-agents-demos）
- Graph Compose（HN 47847918，2026-04）= Temporal workflow + 可视化编辑器，即"成熟引擎 + 后挂 UI"
- KurrentDB 用事件溯源实现 LangGraph checkpointer

**若 v2 需要真正的分布式耐久性，这是已验证的方向。v1 不引入。**

---

## 5. deepseek-harness 源码调研

历史实验检出路径已省略。以下内容只对应当时读取的源码版本，标注文件行号。

### 5.1 定位

DeepSeek 官方，MIT，TypeScript pnpm monorepo，CLI 名 `dsh`。
自述公式 "Agent = Model + Harness"。是 **agent 运行时 + CLI + Web UI + SDK**，不是评测 harness。

规模（`packages/`，排除 node_modules/dist/lib）：

| 指标 | 数值 |
|---|---|
| 源码 `.ts` | **1,185 文件 / 198,402 行** |
| 测试 | **734 文件 / 230,642 行**（超过源码量） |
| leaf 包 | 219 个 |
| 外部依赖（lockfile `resolution:` 条目） | 约 1,203 个 |

最大集群是它自己的前端：`client/` 43,561 行（占源码 22%）+ `host/apiproxy` 8,571 行。

### 5.2 `packages/workflow` 不是图引擎 —— 这是最关键的一条

全仓库对 `StateGraph` / `addNode` / `addEdge` / `GraphNode` / `reducer` / `conditionalEdge`
的搜索**零命中**。

它的实际执行模型：**模型现场写一段 JavaScript 字符串**，扔进 `vm.Script` 在 worker thread 里跑。

```
runtime-types.ts:19-34    WorkflowStartRequest { script: string, meta, args, ... }
runtime.ts:90-98          new vm.Script(`(async () => {\n${body}\n})()`)
runtime.ts:100-108        注入的全局只有 5 个：agent / parallel / pipeline / phase / log
```

唯一像"结构"的 `meta.phases`，源码注释直接堵死：

> `types.ts:25-27` — One phase declared in a script's `meta.phases`
> (**progress vocabulary only** — phases group agents in observers/UIs;
> **they impose no execution structure**).

官方自陈的限制（`packages/workflow/workflow/README.zh.md:53-59`）：

- **无 journaling / resume** —— 脚本、子 agent 进度、中间值都不 checkpoint，**进程重启无法续跑**
- 只支持前台收集，无后台 start/poll
- **无嵌套 workflow**（脚本拿不到 `workflow()` hook，不能子图）
- 无跨子 agent 的 token 预算

另外 `workflow-worker-thread` / `tool-workflow` **没有被任何随附 profile 组装进去**
（搜遍 bundle/apps/examples 的 yml，零命中）—— 是 preview 能力，默认不装。

worker thread 的隔离性质，源码自己坦白（`workflow-worker-thread/src/index.ts:2-5`）：

> The thread prevents synchronous script work from blocking the host and permits forced
> termination, but it is **containment rather than a security boundary**.

**→ 结论：范式相反。LangGraph 是"先声明图、再执行图"，dsh 是"先执行、后观察"。
复用它等于同时引入"LLM 决定路由"和"静默截断"两个风险。不碰。**

### 5.3 真资产一：事实记录层

`packages/core/session/`（3,156 行）+ `packages/session/*`。

- **append-only 事件日志**，45 种事件类型，权威清单在 `known-event-types.ts`（脚本生成，doc-sync 校验新鲜度）
- **开放词汇表**：各插件通过 TypeScript 声明合并注入自己的事件类型，**加事件不用改 core**
- **双后端**：JSONL（支持 zstd，有完整 fsync + 撕裂尾修复，`index.ts:432-433,647-649,691`）
  与 SQLite（SCHEMA_VERSION 15，WAL，1 行 1 事件）
- 存储根 `~/.dsh`，可用 `$DSH_HOME` 覆盖
- 另有 FTS5 全文检索（`session-query-sqlite`）

**最精巧的部分：surface 机制**（`packages/core/session/src/surface.ts`）。

"模型可见面"是叠在事件日志上的有序投影，只有三种事件能上 surface（`surface.ts:15-19`）：
`user/message` / `assistant/message` / `tool/result`。

`SurfaceOp` 只有两个变体：`'append'` | `{ op: 'replace', start, end }`。

**关键：压缩摘要是遮蔽（shadow）而非删除。** `surface.ts:44-47` 原文：

> The model-visible surface deliberately **shadows** replaced ranges, so it is the wrong source
> for a human transcript — a landed replacement would erase conversation the user already saw.
> **Append-origin events are that transcript's durable source material**; replacement copies
> stay model-only.

配套提供 `isAppendSurfaceEvent()`（`surface.ts:51`）专门给"人类完整转录"用。

**溯源是写时强校验的**：`assertProvenance`（`surface.ts:210-243`）要求替换节点的
`sourceEventSeqs` **必须包含每一个被遮蔽的 surface 节点**，缺一个就抛错。

压缩事件本身保留完整审计（`compaction/src/types.ts:33-66`）：`summary`、
**`rawOutput`（摘要模型的完整原始输出）**、`shadowedRange`、`shadowedSeqs`、
`shadowedTokenCount`、`provider`、`model`、`usage`。失败的压缩尝试也留在日志里。

遥测侧守同一条线：*"Redaction applies to the exported copy only; the canonical session log is
never rewritten."*

**⚠️ 但护栏在错误的楼层：`assertProvenance` 只作用于三种消息事件，
完全不覆盖节点之间（`agent()` 之间）的数据流。这份保护不能白拿。**

`packages/spill/` 是另一条"原文不丢"的路径：超大 tool 输出移到旁路文件，
inline 只留预览 + 定位符，不是截断。

### 5.4 真资产二：step 级多模型路由

链路完整，四环全部核实：

```
1. runtime.ts:39        脚本层白名单：label / phase / schema / provider / model
2. runtime.ts:277-282   worker → host RPC 携带 provider / model
3. host.ts:352-365      翻译成 agentOptions（关键一跳）
4. child-agent.ts:68-83 resolveChildAgentOptions：默认继承父级，requested 逐字段覆盖
```

注意区分两个 "provider"：subagent **传输**提供方（spawn/acp/codex，即"子 agent 怎么跑"）
与 LLM **路由** provider（"用哪个模型"）。两者正交。

6 个 subagent provider：`spawn-in-process`（全新）、`fork-in-process`（继承父历史）、
`acp`、`codex`、`claude-code`、`dsh-sdk`。

能力门控纪律值得抄（`subagent/src/types.ts:76-79`）：

> a request that needs a capability the chosen provider lacks is rejected with a typed error
> rather than accepted-then-ignored (the **"fail loud, no silent degradation"** rule).

**参数面的缺口**：`AgentOptions` 只有 `provider` / `model` / `maxTokens`
（`core/agent/src/runtime-types.ts:24-31`）。
`LlmCallConfig` 层其实有 `temperature`（`llm/call-config.ts:27`）且采样参数随 `request/header`
落盘，所以约束是"没暴露到 AgentOptions"而非"不存在"。
但 **seed 确实缺失** —— 这是科研可复现性的硬需求（G6）。

### 5.5 Web UI

- React 18.2 + Vite 6，`apps/web` 只是 vite 壳，实现在 `packages/client/` 下 40+ 个 `ui-*` 包
- **无 Redux/Zustand/Jotai**，自研 slot 注册表 + `use-sync-external-store` 桥
- **无 MUI/AntD/shadcn/Tailwind**，自研 primitives + CSS Modules
- **无任何图可视化库** —— lockfile 里的 mermaid/cytoscape/d3/dagre 全部是
  `vitepress-plugin-mermaid` 的传递依赖，属于 VitePress 文档站，与运行时 UI 零关系
- 通信：上行 HTTP POST `/api/<method>`，下行**两条严格单向只读 WebSocket**
  （客户端一发消息就被 `close(1008, 'downlink only')`）。无 SSE / tRPC / GraphQL
- **无 react-router，无 URL 路由**

已有的 trajectory 视图不简陋（`packages/client/ui-trajectory/`，37 文件）：
表格 + 时间轴（4 种模式，支持刷选）、客户端全文检索、per-request provider/model/usage/累计用量。
但形态是**表格 + 一维时间轴**，不是二维图。

workflow 视图是 `Run → Phase → Member` 三层折叠列表，注册在 `conversation.chat.node`，
即**聊天流里的一张卡片**，不是独立画布。

**扩展点（若走复用路线）**：`conversation.view` 是 **list slot**
（`ui-conversation/src/client/contract/slots.ts:76`），加一个视图 tab 是一次
`ctx.slots.register` 调用，零侵入。`shell.overlay` 同为 list。

⚠️ **但 `apps/web` 不能独立启动**：它依赖 host 注入 `window.__DSH_BOOT__`。
见下节 postmortem 0003。

---

## 6. 为什么不拿 deepseek-harness 当依赖

这一节是**方案的决定性转折**。初版建议是"事实记录层代码复用或模式移植二选一"，
风险评估把这个分岔量化后，答案变成明确的**移植模式、不拿依赖**。

### 6.1 决定性证据：postmortem 0001

`docs/postmortem/0001-acp-default-export-drops-inject.md`（11KB）。

ACP server 在**178 个绿色单测、100% 行覆盖率**的情况下，一连上真实编辑器（Zed）就崩：
`Internal error: cannot get property "agents" without inject`。

两个独立 bug 藏在同一条错误信息后面，**根因都是 Cordis 框架的隐性契约**：

1. `packages/acp/acp/src/index.ts` 末尾多了一行 `export default apply`。
   Cordis Loader 的 `unwrapExports` 做 `exports = exports.default ?? exports`，
   于是拿到裸函数，把带 `name`/`inject`/`Config` 的 module namespace 整个丢掉，
   插件在一个**没有任何注入服务**的 fiber 里运行。
2. 修掉 #1 后仍炸在 `sessionPersistence`：`AgentLoop.resume()` 读 `this.ctx.sessionPersistence`
   （故意不在 `static inject` 里），通过 foreign fiber 的 traceable proxy 调用时
   `createShadowMethod` 会重绑 `this.ctx` 到 shadow，而 fiber walk 是**仅祖先方向**的 ——
   服务在 sibling 分支上，走到 root 就抛错。修法是改用 `ctx.get('sessionPersistence')`。

**为什么全部测试都没抓到**（这是复盘核心）：内存 harness 用 `ctx.plugin({...})` 手工构造插件对象，
`unwrapExports` 只有 Loader 会调，所以 Bug #1 **在结构上不可能被复现**；
所有插件平铺挂在一个 root context 上，掩盖了 Bug #2 的祖先遍历失败；
唯一驱动相关方法的测试被 API key 门控，CI 里被跳过，本地"通过"只因陈旧的 `lib/` 构建产物
恰好满足了模块解析。

复盘自己的结论（第 98 行）：

> **Coverage proves lines *ran*; it says nothing about whether the feature works *the way it ships*.**

**→ 这两个坑不是 harness 的 bug，是框架契约。你在这个地基上写自己的插件会一模一样地踩到，
而且单测抓不到。本项目的全部论点是消灭静默失效，采纳 Cordis 会引入一整个新的静默失效家族。**

### 6.2 另外三份 postmortem 的同构模式

**0002 — 配置层静默失效 + 快照测试固化回归。**
`cordis.yml` 里用 `disabled: !!js ...`，但 Cordis 只在 plugin `config` 字段求值 JS 表达式，
`Entry.disabled` 直接读不插值。未求值的表达式对象是 truthy ⇒
**filesystem 栈在所有模式下永久禁用**。
更严重的是 7 个 filesystem 快照场景**通过了** —— 因为 refresh 把
`ToolNotFoundError` / `UNKNOWN_TOOL` 写成了新的 expected fixture。原文：
*"it proved deterministic replay of the regression rather than successful filesystem behavior"*。
PR #261 所有 unit/coverage/snapshot/doc/build/hygiene 检查全绿。

**0003 — Web GUI 反馈回路（与本项目形态最相关）。**
一个跑在 Web GUI（3081）里的 agent 改了 GUI 主题源码，然后：turn 3 起了裸 Vite 在 5173，
看到 HTTP 200 就宣布成功，**浏览器实际白屏**并抛
`client-modules: window.__DSH_BOOT__ is missing or not an object`；
turn 4 用 shell `&` 起了一个无人管理的进程在 3334，只验证替代品返回 200，**从未探测 3081**；
turn 5 用户告知 3081 早就显示新主题了。
根因是 Web 组装体没有对模型可见的 canonical URL 与运行模式。
第一版回归测试本身还是假阳性 —— 用 timeout 杀掉 Vite 满足了"非零退出"断言。

**→ 两条教训：`apps/web` 不可独立启动（依赖 boot manifest 注入）；
"HTTP 200" 不是"页面能用"的证据。后者直接写进了 VERIFICATION 的 M2 验收。**

**0004 — 错误归因把良性信号当致命失败。**
旧 Landlock ABI 上 launcher 打印无害的 `landlock-run: partial enforcement`，
harness 把这个共享前缀 + **任意**非零退出码判定为 launcher 失败，
于是 ripgrep 的"无匹配"退出码 1 被报成 `SANDBOX_UNAVAILABLE`。
残留风险（第 39 行）：stderr 仍是 in-band 归因通道，受限子进程可**故意伪造**致命行造成误归因。

**四份的共同模式：每一次都是"所有 CI 门禁全绿 + 高覆盖率"下漏出去的组装层缺陷，
且三份根因在 Cordis Loader 或 vendored 框架的隐性契约上。**

### 6.3 框架是 fork 过的 release candidate

`vendor/` vendored 了 Cordis 全家桶 9 个包：

| 目录 | 上游名 | 版本 |
|---|---|---|
| `cordis/` | `cordis` | **4.0.0-rc.7** |
| `loader/` | `@cordisjs/plugin-loader` | **1.0.0-rc.5** |
| `cosmokit/` `schemastery/` `include/` `group/` `timer/` `hmr/` `logger-console/` | — | 稳定版 |

`vendor/README.md` 的 "Local modifications" 清单有 **8 条分歧**，其中两条是**行为性框架修改**：

- 第 6 条 `cordis/src/fiber.ts` lifecycle hardening：修了三个 reentrant disposal 缺口
  （effect owner-list 注册时序、async cleanup 的 owner 可见性、`UNLOADING` 下拒绝创建 effect、
  子 fiber disposer 注册时机、teardown 通知按 observer 隔离、`Fiber.update()` 返回 waterfall 结果）
- 第 8 条 Loader/Group/Include 的**事务性配置重载语义全部是本地实现**
  （失败回滚、settlement 重检、patch 克隆后再 commit）

**→ Cordis 的关键生命周期与配置重载语义在这个仓库里是 fork 的。上游同步需跑
`scripts/rescope-vendor.ts` 并手工重放这 8 条。**

版本与稳定性承诺：根 `package.json` = `0.1.0-rc.5`，**全仓库无 CHANGELOG**，
唯一声明在 `README.md:9-11`：

> ## Developer preview
> DeepSeek Harness is currently in _developer preview_ and is iterating rapidly.
> **THERE WILL BE COMPATIBILITY-BREAKING CHANGES.**

按 `breaking`/`unstable`/`experimental`/`will change` 搜遍 README/AGENTS.md/docs，
除此之外没有任何其它稳定性承诺或弃用策略。**无 CHANGELOG ⇒ 破坏性变更的历史频率无法量化。**

### 6.4 Windows 是二等公民（而本项目在 Windows 上）

`native/` 只有 `landlock-run/`（Linux 沙箱 launcher）。
`native/landlock-run/docs/support-matrix.md:15` 原文：

> **win32: a Windows confinement launcher would be a different mechanism in its own repository,
> not a port of this one.**

不支持平台上"resolves a nonexistent launcher path, probes `unusable`, and falls closed"。
两个平台包挂在 `optionalDependencies`，所以 Windows 上 `pnpm install` **不会失败**，只是拿不到沙箱。

`docs/user/guide/python-sdk.md:102`：
*"The persistent PTY backend requires a POSIX terminal substrate, so this composition does not
support Windows agents."*

**CI 是最硬的信号**（`.github/workflows/ci.yml:328-458`）：

- 必需的 PR Windows 信号 `windows / wine blocking` 跑在 **ubuntu-latest 上用 Wine 跑 Windows Node**，
  只有两个门：`build` 和 `docs:build`
- 真机 `windows-native / native complete` 跑完整清单，但 ci.yml:438-439 注释原文：
  **"is deliberately absent from `all-checks-passed.needs`, so it never delays or changes that
  required verdict"** —— **真实 Windows 内核上的测试结果不阻塞合并**，
  且 observational 部分全部 `allowFailure: true`

有真实的 Windows 专用实现：`sandbox-windows-acl`（3,608 行）、`shell/pwsh-*`、
`directory-picker-native/src/win32-dialog*.ts`、`fs-local/src/win32.ts`。
`process.platform === 'win32'` 共 75 处 / 50 文件，但**源码里只有 7 个文件**，其余 43 个在测试里。

唯一的 patch 也在这条链上：`patches/node-pty@1.1.0.patch`（改 spawn-helper 解析，
加 `DSH_NODE_PTY_SPAWN_HELPER` 环境变量与兄弟路径回退）。node-pty 是唯一需要本机二进制的依赖。

### 6.5 SDK 路线对图编排是断的

`packages/sdk/` 3 个包全部 MIT + public。但它是
**subprocess-over-stdio JSON-RPC，不是进程内库**（`sdk/client/README.md:5`）：

> The TypeScript client SDK for driving a DeepSeek Harness runtime
> **as a subprocess over stdio JSON-RPC**

`launch: { command, args }` 必填，一定 spawn 子进程。

**自己声明的缺口**（`sdk/client/README.md:44-49`），两条对节点执行器致命：

- **No mid-turn cancel** —— 协议里没有 prompt-cancel 方法，放弃一个 turn 只能关掉整个 runtime
- **No per-prompt result** —— `run()` 返回的 `finalResponse` 是"该活动区间内最后一条
  root-session assistant 文本"，**不是因果上归属该 prompt 的响应**，且不带 prompt 级 status

节点执行器要的恰好就是"取消这一个节点"和"这个结果属于哪个节点"。

真正的进程内路径是绕过 SDK 直接用 Cordis（`packages/boot/app-boot/`、
`packages/bundle/headless/`），代价是没有稳定 API，要自己写 `cordis.yml` 并接受
Cordis 插件/fiber/service 心智模型 —— 回到 6.1 的问题。

### 6.6 许可与供应链（无障碍）

- 主仓 **MIT**（`Copyright (c) 2026 DeepSeek`），219 个 leaf 包全部 MIT 且无一 private
- 例外：`native/landlock-run/packages/entry` 是 BSD-3-Clause
- `THIRD_PARTY_NOTICES.md`：MIT 102 / Apache-2.0 15 / BSD-3-Clause 3 / MPL-2.0 2 /
  LGPL-3.0-only 2 / ISC 2 / BSD-2-Clause 1
- 第 171 行明确声明 `eslint-plugin-sonarjs`（LGPL）和 `lightningcss`（MPL）
  *"run only as development tooling; their code is not linked into or distributed with any
  DeepSeek Harness artifact"* ⇒ **没有 copyleft 传染到分发物**

**→ 照抄设计和局部拷代码都是干净的。障碍是技术性的，不是法律性的。**

### 6.7 结论：当参考设计，不当依赖

**值得逐条抄的设计智慧：**

| 抄什么 | 出处 |
|---|---|
| 摘要遮蔽而非删除原文 + 写时强制溯源校验 | `surface.ts:44-47, 210-243` |
| "模型可见即已记录"做成运行时不变量 | `docs/architecture.zh.md` |
| 能力缺失时 fail loud 而非 accept-then-ignore | `subagent/src/types.ts:76-79` |
| JSONL 的 fsync + 撕裂尾修复 | `session-persistence-jsonl/src/index.ts:432-433` |
| 配置期硬拒 `0.0.0.0` 而非运行时警告 | `bundle/web-app/src/startup.ts:69-70` |
| 超大产物旁路落盘 + 预览 + 定位符（不截断） | `packages/spill/` |
| 大输出只在"喂给模型的投影"里截断，规范值保持完整 | `tool-workflow/src/index.ts:196-203` |

这些不需要 219 个包和一个 fork 的 rc 框架才能拿到。

---

## 7. 可观测性与成本

### 7.1 没有事实标准，有两套打架的语义约定

- OTel GenAI semconv 已从主 semantic-conventions 仓库**迁出**到独立的
  `open-telemetry/semantic-conventions-genai`，仍是 **experimental**
- Arize 公开主张 OpenInference 优于 OTel GenAI（HN 47074449：更丰富的 LLM 元数据、
  RAG/retrieval 一等公民、span 类型区分更好）
- **但 HN 45404138 有一条精准纠正**：语义约定只规定属性怎么命名，
  "OpenInference 不兼容 OTel"这个说法是错的；真正的问题是 **vendor 的 UI 只认自己那套 convention**，
  不认的渲染成 unknown span

**约定漂移的实际成本：**

- OpenLLMetry issue #3515：`gen_ai.prompt` / `gen_ai.completion` 已被上游废弃，
  需迁到 event 形式，靠 `use_legacy_attributes` 开关兼容
- Dify 一个依赖升级 PR 里 langfuse 从 **2.51.5 跳到 4.14.3**（跨两个大版本）、
  phoenix-otel 0.15→0.17

**→ 自建 trace UI 的真正陷阱不是画图，是跟住 semconv 漂移和 SDK 大版本跳跃。
这反而是"先自己存事件日志、观测层做薄"的理由。**

### 7.2 deepseek-harness 的可观测性缺口

- OTel **只接了 Logs，没有 Traces**：依赖里有 `sdk-logs` / `exporter-logs-otlp-http`，
  **没有 `@opentelemetry/sdk-trace-*`**。实现是 `LoggerProvider` + `BatchLogRecordProcessor`。
  **没有 span ⇒ 没有父子调用树、没有耗时瀑布图。**
- 语义约定是**自研 `session.*` 命名空间**，既不是 `gen_ai.*` 也不是 OpenInference
  （全仓 grep `gen_ai` / `openinference` / `SEMATTRS` 零命中）⇒
  接 Langfuse / Phoenix / Braintrust / LangSmith 都要写映射层
- **成本（金额）完全没有**：grep `cost|price|pricing|USD` 命中的全是
  `token-meter` 里的 "shadow price"（**启发式 token 计数的隐喻**，不是货币）。
  无价格表、无 per-model 费率、无货币字段

token 统计字段是齐全的（`llm/src/types.ts:135-141`）：
`inputTokens` / `outputTokens` / `cacheReadTokens` / `cacheWriteTokens` / `reasoningTokens`。
挂在 `assistant/message` 事件上。

**⚠️ 注意 `packages/runtime-diagnostics` 与遥测无关** —— 它是运行时不变量断言注册表，
不是可观测性组件。目录名有误导性。

### 7.3 因果结构的缺失（最有价值的一条）

HN 47301395 下最好的一条回复：**所有工具都记录"agent 做了什么"，
但不记录"为什么偏离计划"**，缺因果结构就只能对时间戳猜。
HN 47059704 的作者已在用 Langfuse，仍然只能逐步手点排查。

**→ 本项目的 trace 应记录"节点为什么走了这条边"（即 verdict + 依据），
而不只是"节点跑了"。这是差异化的地方。**

---

## 8. 科研场景

生态存在但分裂，且**几乎没有"工作流可视化"形态**：

| 项目 | 说明 |
|---|---|
| FutureHouse / PaperQA2 | 非营利，github.com/Future-House/paper-qa，自称文献检索超人类 |
| Consensus Deep Search | 2 分钟跨 2 亿论文出综述 |
| Sakana AI Scientist | — |
| ai-archive.io | AI 写的论文仓库 |
| Bengio 的 LawZero | 明确走 **"non-agentic" Scientist AI** 路线，lawzero.org |

**关于可复现性（随机种子、实验记录、成本追踪）：直接证据极少。**
"LLM experiment reproducibility seed" 类查询返回 0 条相关结果。
唯一相关的一手信号是成本焦虑（HN 49176920 的 juancn、47301395 的 "untracked token usage
带来账单惊喜"）。

**这是我看到的最明显的空白区**，但也可能只是 HN/GitHub 检索通道覆盖不到学术圈。

**→ 对本项目：seed 与实验记录是差异化机会，但不能假设有现成范式可抄。
G6 的具体形态需要在 M3 之前和真实科研用例对齐。**

---

## 9. 实测：稳定性的真实难点

这一节来自本次调研过程中**真实跑的两轮多模型编排**（成本 $1.58 + $2.99）。
它是本文档里唯一的一手实验数据。

**7 次节点派发，3 次基础设施失败：**

| 模型 | 失败形态 |
|---|---|
| `Kiro:gpt-5.6-sol` | CLI 输出中缺 result 字段或为空 |
| `Kiro:claude-sonnet-5` | 原文只回了一个 `OK`，required 字段全缺 ⇒ parse_failed |
| `SuperAI:qwen3.8-max` | **"疑似 prompt 未完整送达：预期约 11154 字符（≈3718 tokens），实际 input_tokens=108"** |
| `RightCode:gemini-3.6-flash` | 子进程静默失败（退出码 1，stderr 为空） |

**第三条是这次调研最有价值的意外收获**：那正是杀死前几代项目的
**数据传递断裂**，只不过被一个哨兵抓住了，而不是安静地穿过去产出一份读起来很专业的报告。

成功的 3 次（deepseek-v4-pro、claude-opus-4-8、glm-5.3）产出质量都很高，
且都主动去源码里核实了前提。

**→ 两条结论：**
1. **不稳定主要来自多家模型接入的长尾，不是来自引擎。**
   每个节点必须配失败重试 + 备用模型链。这是 G7 的真实内容。
2. **"预期字符数 vs 实际 input_tokens"这个哨兵极其有效，必须实现。**
   写进 VERIFICATION 的 A2。

补充实测数据（来自既有项目 26 次运行 / $41 的账本，非本次一手）：
单次 agent 会话 input token 中位数 22k，但**尾部到过 1.09M（$3.37）** ——
agent 自己决定读多少文件，与初始 prompt 长度无关（实测 1583 字符 prompt 跑出 118 万 input token）。
⇒ **成本在派发前无法精确定界，只能靠上限守卫停在正确的一侧。**

### 9.1 M0 实测补充（2026-08-16，Atlas 首批真实运行，5 次）

M0 里程碑的两节点真实图（Deepseek:deepseek-v4-flash → SuperAI:glm-5.3，
两种传输协议）。5 次运行估算总花费 <$0.5，换回三条硬结论。

**10 次模型调用，6 次异常，全部被假成功检测拦截，零次静默通过：**

| 次数 | 形态 | 处置 |
|---|---|---|
| 3 | deepseek 输出打满 8192 token，报告句中截断 | `finish_reason=="length"` → `output_truncated` 警告（前两次靠 reviewer 语义发现，第三次起机器可见） |
| 2 | 推理型模型把输出预算烧在隐性思考上，可见文本为空（glm-5.3、deepseek-v4-pro 各一次） | 「返回内容为空」→ DegradedOutput → 降级/失败 |
| 1 | deepseek 返回 200 但内容为空 | 同上 |

**→ 三条结论：**

1. **假成功不是长尾，是常态。** 上一轮 7 次调用 3 次失败，这轮 10 次里 6 次，
   且出现两个新形态（max_tokens 截断、思考烧预算），全部返回 200、格式合法。
   「输出打满上限」用 token 数阈值判断漏检过一次（网关报 8189/8192，差 3 个
   token，报告实际句中截断）——必须用协议信号（`finish_reason`/`stop_reason`），
   不能用猜的。
2. **完整性层的价值被活演示了三次**：writer 被截断时，reviewer 因为拿到了
   完整原文，三次都在语义上发现"报告在句中戛然而止"并指出缺了什么。
   机器担保字节完整（哈希），模型担保内容完整（语义检查）——两层缺一不可。
3. **截断哨兵的 len/3 估算法与真实值吻合**：237 字符投影 → 228 input_tokens
   （偏差 4%）；4429 字符 → 2184 tokens（glm 的 tokenizer 更省）。
   0.3× 阈值有足够安全边际。

另一条观察（未固定成测试，记录备用）：glm-5.3 的字符计数不稳——
对 4054 字符的报告报"约 3600"（偏差 -11%），对 1498 字符的报"1490–1520"（基本
精确）。跨模型交叉检查里"数数"类断言只能当方向参考。

### 9.2 M1 实测补充（2026-08-16，YAML+界面里程碑）

三条来自 M1 实现与独立模型审查的一手结论：

1. **LangGraph 的并行超步语义比文档直觉强**：同一超步里 right 失败时，
   left 的写入（含我们往 state 里合并的产物引用）**仍会被 checkpoint**。
   续跑只重执行失败的分支，成功分支不重跑、产物哈希不变。审查时推测的
   "兄弟节点覆盖"场景（left 重跑覆盖同名产物文件）在异常路径下不成立；
   但进程被**硬杀**（超步完全未提交）时仍会发生——所以落盘层保留
   write-once（文件已存在则 `.r2` 后缀递增）作为防御。教训：**推测的故障
   语义要用实验证伪，再决定要不要为它写防御代码。**
2. **SSE 在长事件空窗期会被掐**：reviewer 思考 92 秒期间无事件，连接被
   中断且首版前端 onerror 直接放弃 → 界面卡在"运行中"而账本早已 run_done。
   修复 = 客户端带 `?after=seq` 重连直到终态事件 + 服务端 keepalive 注释。
   架构 7.5 写的"界面重连：先拉全量事件流，再续听"不是套话，是第一天就
   踩到的坑。
3. **绑定 127.0.0.1 ≠ 只有本机能用**（独立审查指出）：用户浏览器里任意网页
   都能向 127.0.0.1:8321 发 no-cors POST（CORS 只挡读不挡写），静默启动
   烧钱的运行；DNS rebinding 连响应都能读。修复 = Host 头白名单 + 写操作
   要求自定义头 `X-Atlas-Request`。红线 ④ 的威胁模型里，**浏览器就是网络**。

---

## 10. 方案演变记录

保留这一节是因为**中间的错误判断本身有信息量**，能防止后来者重走。

### 第一版（被否决）：自建最小 durable executor + 复用 dsh 事实层

三个副模型在"事件日志即唯一真相"的前提下独立收敛到这个结论，并一致纠正了一处排序错误：

> **图引擎本身是廉价的**（拓扑 + verdict 查表路由，500~1500 行就能正确）。
> 真正的工作量和历史死因，都在**边上的数据完整性**。

三个模型各自的措辞：
- deepseek-v4-pro：检测面本身必须机器可验，否则"检测面就是下一代静默失效的温床"
- claude-opus-4-8：**"架构方向可行但护栏建错楼层"** —— surface 溯源在 intra-session 消息层，
  图的边上零保护
- glm-5.3：把边载荷完整性当成持久化细节而非**产品不变量**，是第一致命缺陷

**这个洞察被完整保留，是本项目的核心。**

### 转折一：目标澄清

项目主人明确：不了解技术栈、对方案无倾向、**要的是能跑且稳定的产品**。

前提变了，结论必须跟着变。那三个模型回答的是"在这套架构下该不该自建"，
不是"哪条路最快拿到可用产品"。

关键的反直觉推论：**用成熟引擎恰恰更能防住杀死前几代的那个 bug。**
那三次失败（投影被丢弃、40k diff 截成头 500 尾 500、把数据传递交给模型执行）
全都发生在**自己手写的传递管道**里。自己再写一遍，就是把同一类 bug 的机会重新买回来。

⇒ 引擎改用 LangGraph。

### 转折二：风险评估量化了唯一未决的分岔

"复用 dsh 事实层 vs 移植模式"这个分岔，glm-5.3 明确标为未量化的不确定性
（"Cordis 学习成本税未量化"）。风险评估把它量化后（第 6 节），答案变成明确的：

**移植模式，不拿代码依赖。**

否决理由按权重排序：
1. postmortem 0001 —— 框架隐性契约导致的静默失效，单测结构上无法复现（6.1）
2. 框架是 fork 过的 rc，无 CHANGELOG，README 全大写警告破坏性变更（6.3）
3. 真机 Windows CI 不阻塞它自己的合并，而本项目在 Windows 上（6.4）
4. SDK 缺 mid-turn cancel + 结果无法归因到具体 prompt（6.5）

**→ 最终方案见 `docs/ARCHITECTURE.md`。**

### 关于本次调研自身的可靠性

- 四个模型里有一个（claude-sonnet-5）在两轮里都没能产出可解析输出，
  所以**实际是三个模型的独立分析**，不是四个
- 三个成功的模型都标了自己的 uncertainties，其中反复出现的是：
  外部事实无法本地核实、工作量估算基于架构阅读而非测量
- **未实测**：Windows 上 `pnpm install` / build 是否真的通过（6.4 的结论基于 manifest 推断）
- **未复现**：LangGraph 那几个 issue（第 4 节的判断基于 issue 内容）

---

## 11. 第二轮验证：拿写好的文档去证伪

**时间**：2026-08-16
**方式**：把四份已写好的文档交给四个指定模型，只问一件事——
「这套方案真的能达成 G1–G8 吗，哪一条会落空」。
刻意不问"方案好不好"，而问"哪里会失败"。

**参与模型**（项目主人指定）：

| 槽位 | 模型 | 结果 |
|---|---|---|
| A | Kiro:claude-opus-4-8 | ✅ 产出 |
| B | Deepseek:deepseek-v4-flash | ✅ 产出 |
| C | Kiro:gpt-5.6-sol | ❌ 失败（CLI 输出缺 result 字段） |
| D | SuperAI:grok-4.6 | ✅ 产出 |

裁决节点因扇入未满足未运行，聚合由主 Agent 完成。
成本：输入 1,324,915 tokens，输出 36,680 tokens，$7.82，589 秒。
账本：历史实验运行账本（本机绝对路径已省略）

⚠️ `gpt-5.6-sol` 是**第二次**栽在同一处（第 9 节记录了第一次）。
两轮共 2/2 失败，失败形态完全相同。这已经不是偶发——
`config/models.reference.json` 里该模型的 notes 应据此更新。

### 11.1 三个模型独立收敛的两条（已采纳）

**收敛点一：G7「高稳定性」名存实亡。**

三个模型都指出同一件事：文档设计的失败链只在**抛异常**时触发降级，
而第 9 节那份一手数据里，7 次失败中有 3 次是 **HTTP 200 的假成功**——
返回空结果、只回 "OK"、退出码 1 但 stderr 为空。这类响应不抛异常，
会被当成正常输出交给下游。

grok-4.6 的措辞最直接：「失败链按异常设计，而一手故障是空结果、只回 OK、
无 stderr 的假成功。」opus-4-8 补充了机制细节：完整性层只校验**输入边**，
不校验**输出有效性**，纯文本节点没有 schema 就静默通过。

这是一个真实的设计缺口，而且讽刺的是：**证据来自本项目自己的调研记录，
而文档没把它接进设计。** 已在 ARCHITECTURE §5 补「假成功即降级」，
并在 VERIFICATION 新增断言 A7。

**收敛点二：G3「Web 界面」排程性落空。**

三个模型都指出界面被压在最后一个里程碑，
而 VERIFICATION 自己写着历史死因是「基础设施做完精力耗尽、界面没接通」。
opus-4-8 说得最狠：这是「复制了历史的排程死法」。

deepseek-v4-flash 另外查出一处规格空洞：事件流 schema 里
**没有节点输入的快照字段**，而验收却要求「每个节点的完整输入输出」——
数据来源不存在，验收无法通过。

已把只读界面提前到 M1（原 M3），并在事件 schema 补 `node_input` 快照。

### 11.2 一处矛盾（已修正）

README 写图定义用 YAML，ARCHITECTURE 却推荐先写 Python——
三个模型都独立发现了这处矛盾。

裁决：**YAML 是唯一真相**。理由是项目主人不写代码，
而「图的定义必须是能进版本控制、能 diff 的文件」这条红线，
YAML 才能同时满足「人能读、界面能改、git 能 diff」。
Python 直接建图的写法已从架构文档移除。

### 11.3 已核实的事实错误（本文档作者的错）

这些是我写文档时犯的错，被模型查出来后逐一核实为真：

| 错误 | 实况 | 严重度 |
|---|---|---|
| `config/.env` 被复制进 Atlas | **真实密钥的冗余副本**，已删除 | 🔴 安全 |
| README 说「去 cp .env.example」 | 该文件当时**不存在**，已补上 | 🟡 误导 |
| README 说 19 个模型 | 实际 **18** 个（已逐供应商数），已改 | 🟢 事实 |
| `models.reference.json` 引用「M0 首跑」 | Atlas 无代码无 runs/，那是 **Quorum** 的记录 | 🟡 误导 |
| 交叉引用章节号大面积指错 | grok-4.6 逐条列出，已修 | 🟡 误导 |

关于那份密钥副本值得单独记一笔：**我之前跑过泄漏检查，但模式用的是
`sk-`/`Bearer`，而这些网关的密钥不是这个前缀，所以检查通过了。**
这正是"检查本身有盲区"的实例——教训是**按文件名拦，不要按内容模式猜**，
已写进 `.gitignore` 和 config/README。

### 11.4 分歧点（保留，不折中）

**「有没有更短的路」——三方不一致：**

- opus-4-8：有。**n8n** 以天计就能拿到 G1–G4/G8 主体和最强的每节点 I/O 视图，
  代价是放弃完整性护栏、并擦到两条红线
- deepseek-v4-flash：红线之内没有。若红线可让步，**自托管 Dify** 覆盖 G1–G4+G3，
  代价是红线②③和 G5/G6
- grok-4.6：没有现成产品能同时守红线并覆盖 G1–G8

三方口径其实一致：**守红线就没有更短的路，放弃红线才有。**
分歧只在「红线值不值得守」，而那是项目主人的决定，不是模型的。
本文档第 1、3 节的证据（Flowise 停服、Langflow 被勒索软件利用）
是支持守红线的依据，但决定权在人。

⚠️ 三方对 n8n / Dify 的能力判断**都标了"未本机实测"**。
如果哪天要重新考虑这条路，必须先自己装一次验证，不能采信这三段。

**工作量估算——三方都给了数字，但都声明是推测：**

- deepseek-v4-flash：核心闭环 6–12 人周（适配层 5–15 人日、CLI 节点 2–4 人周、UI 2–4 人周）
- 另两方给了同量级判断但未细分

三方**一致推翻**了原架构文档「主要工作量在 Web 界面」的说法：
真正的隐藏工作量在**模型适配层**（被我错标成"薄"）
和 **Windows 上的 CLI 编程 agent 隔离**。已在架构文档改正。

### 11.5 三方共同标注的未核实项

这些是接手时**必须自己验的**，三个模型都无法核实：

1. **LangGraph 的真实 API**——`SqliteSaver.from_conn_string` 是否必须作为
   上下文管理器使用（官方文档拉取失败）。架构文档里的代码片段**可能是错的**，
   照抄会报错。这是 M0 第一件要做的事
2. **LangGraph Studio 能否展示每节点完整输入输出**，是否依赖云/LangSmith
3. **HITL 的「走到 END 再 resume」语义**是否与 LangGraph 相容
4. **seed 参数**在这些第三方网关上是否真被尊重（影响 G6 可复现性）
5. Flowise / Agent Builder 停服未走官方渠道复核（来自 HN + GitHub API 抓取）
6. 第 5–6 节对 deepseek-harness 的行号引用，本轮未复验

⚠️ 第 1 条最关键：**架构文档里的 LangGraph 代码片段未经运行验证。**
把它当伪代码看，不要当可复制的实现。

### 11.6 本轮的元教训

第一轮我让模型评估「路线对不对」，第二轮让模型证伪「目标会不会落空」。
后者产出的可行动信息多得多——它查出了 2 个设计缺口、1 处矛盾、5 处事实错误，
而第一轮主要是确认了方向。

**问"哪里会失败"比问"这样好不好"更有用。**
这条对后续每次评审都适用。

---

## 12. v1 范围决定：砍掉 UI 编排，操控走 skill + MCP

**时间**：2026-08-16
**性质**：项目主人的决定，主 Agent 补充源码依据。这是第四次方向调整，
前三次见第 10 节与 `../README.md` 第 5 节。

### 12.1 决定内容

> 第一个版本不做 UI 编排（最终版本可以做，但目前难度确实很大），
> 只展示图结构和各节点的输入输出。操控既然是"模型接自然语言、生成编排"，
> 不如做成 **skill + MCP**，让各种 harness 工具去设计使用，人实时考察。

### 12.2 为什么这个切法是对的（主 Agent 的分析）

**被砍掉的是整个 v1 里最难、最不确定的一块。** 画布编辑器 = 拖拽交互 +
YAML 双向同步（改画布→写文件、改文件→重排画布，两边都要处理冲突）+
布局持久化。第二轮验证（第 11 节）确认了工作量主体在适配层而非界面，
但界面里真正难的恰恰是编辑这一半。砍掉它之后，v1 的自建部分全部"薄"或"有参考"。

**skill + MCP 不是新发明，是本机已验证的模式。** Quorum 就是
"单函数 MCP + SKILL.md + runs/ 账本"，在 D 盘跑了 26 次真实运行。
"模型接自然语言生成编排"也恰好是 dsh `tool-workflow` 的既定模式——
只是它生成的载体是 JS 源码字符串，我们换成 YAML 声明。

**为什么 YAML 比模型写代码安全（对照 dsh 的选型）：**

| | dsh：模型写 JS | Atlas：模型写 YAML |
|---|---|---|
| 本质 | 可执行代码 | 数据声明 |
| 能表达什么 | 任意逻辑（vm 内） | 只能引用封闭清单里的节点类型 |
| 越界后果 | containment，非安全边界（其源码自陈） | 校验期拒绝，零成本 |
| 可视性 | 运行前无图可画 | 运行前就有完整拓扑 |
| 人可读可改 | 要读 JS | 读 YAML |

这不是说 dsh 的选择错了——它要做的是通用 agent 运行时，表达力就是需求。
Atlas 的需求是 G1（图结构定义）+ G3（可视化拓扑），YAML 声明是更贴的载体。

### 12.3 从 `tool-workflow` 源码搬走的四条纪律

读了 `packages/workflow/tool-workflow/src/index.ts` 全文。
它的 DESCRIPTION（第 138-150 行）是"给模型看的编排规范"的最佳范本：

**① 规范与工具同体。** 格式契约完整写进工具描述里，不依赖部署时的
persona 或外部文档。模型看一眼工具定义就知道怎么写。
→ Atlas 的 skill 必须自包含，附最小完整 YAML 示例。

**② 使用政策跟着工具走**（`index.ts` 第 230 行附近）：

> Use the workflow tool ONLY when the user explicitly asks for a workflow
> or for large multi-agent orchestration... For one or two delegations,
> prefer plain subagent calls.

防止 agent 高射炮打蚊子。→ Atlas skill 同样写"单模型能解决的不要用"。

**③ 误用必炸，绝不静默降级**（DESCRIPTION 原文）：

> Misused hooks ... throw errors that ALWAYS kill the script —
> they never dissolve into a per-item null.

→ Atlas 的 validate 对坏 YAML 的拒绝同理：明确指出哪一行哪个字段，
不猜意图、不降级继续。

**④ 职责边界一句话说死**：

> no filesystem, network, timers, or Node.js APIs are provided —
> the agents do the work, the script only coordinates them.

→ Atlas skill 的对应句："harness agent 的职责是写好 YAML 和节点 prompt，
不是替节点干活。"

另一个值得记录的实现细节：它的 recorder（`createWorkflowRecorder`）
把运行事件投影进父 session 日志时，**记录失败只禁用记录、不影响工具执行**
（`appendRecord` 失败就 `active.delete`，打条 warn 继续跑）。
观测路径的故障不许拖死执行路径——这个隔离方向值得照抄。

### 12.4 没变的东西

- 四条红线全部原样。红线 ① 反而更强了：YAML 是纯声明
- 七条断言（A1–A7）全部原样，M2 闸门改为人话验收
- 图引擎仍是 LangGraph，事实层仍是 runs/ 账本 + 产物落盘
- "文件是真相"更自然了：模型写 YAML，人也能看能改，界面只渲染
