# RFC：节点间通讯文件（node I/O files）

状态：**未来设计，v0.1.0 未实施，尚未批准进入开发。** 本文中的 `outputs`、`collect_files`、`attachments`、`attachment.*` 与 `atlas:file` 都是提案字段，不是可用 API。实施前必须重新核对当前符号，并完成附件本机路径读取、符号链接/重解析点、容量限制与事件兼容的威胁模型。

| 能力 | v0.1.0 | RFC 目标 |
|---|---|---|
| 每节点一个主输出 | 已实现 | 保持兼容 |
| coding agent 文本 diff | 已实现 | 保持摘要绑定 |
| 命名附加输出 | 未实现 | `outputs` 提案 |
| agent 文件回收 | 未实现 | `collect_files` 提案 |
| 运行附件 | 未实现 | `attachments` 提案 |

原始需求：纯文本单通道会损失信息量与结构，希望增加只为节点通讯而存在的临时文件能力。当前状态和排期分别见 [`../STATUS.md`](../STATUS.md) 与 [`../ROADMAP.md`](../ROADMAP.md)。

---

## 1. 现状:为什么纯文本传输真的有损

先把当前机制说准,再说它损在哪。**它不是"没有文件",而是"只有一份被拼接成文本的文件"。**

### 1.1 现在的传输链路

节点 A 产出 → `store_artifact()` 写 `runs/<id>/artifacts/A.output.1.txt` + `.sha256` 旁车
→ state 里只传引用 `{name, path, sha256}`(`integrity.py:29-42`)
→ 节点 B 声明 `consumes: [task, A.output]`
→ `build_projection()` 读回、**逐个哈希断言**、把原样字节**内联**进一份投影文本
(`integrity.py:125-131`),整份落盘为 `projections/B.input.1.txt`
→ 投影文本作为 prompt 送进模型(`engine.py:328`),或作为 stdin 送进 agent CLI
(`nodes/agent.py:202-206`)。

内联格式是固定的分隔符包裹:

```
<节点 B 的 prompt>

===== 上游产物 [A.output] 开始 =====
<A 的原样字节>
===== 上游产物 [A.output] 结束 =====
```

### 1.2 这套机制好的地方(必须保住)

- **红线③的全部保证都建立在它上面**:读取时哈希断言(铁律①)、缺失显式失败
  (铁律②)、超长不截断(铁律③)。A1 测试断言的是「源产物字节 ⊆ 投影字节」——
  这是一条可被机器验证的强不变式。
- 产物 write-once(`_unique_path`,`integrity.py:45-60`),崩溃续跑不覆盖旧字节。
- 接线在**加载期**就校验:`consumes` 只能引用 `task`、`<节点id>.output`、
  或 coding_agent 的 `<节点id>.diff`,笔误当场拒绝(`spec.py:683-703`)。

### 1.3 真正的损失在哪(主人的判断成立)

| 损失 | 具体表现 | 现在的后果 |
|---|---|---|
| **只有一条输出通道** | 每个 llm 节点只能产出一份 `X.output`(coding_agent 多一份 `.diff`) | 想同时输出"结论"和"结构化数据"只能塞进同一份文本,下游必须靠 prompt 约定去切 |
| **格式被压成 text/json** | 落盘扩展名只有 `.txt` / `.json`(`engine.py:345`,由 `required_fields` 决定) | CSV、YAML、Mermaid、多份 Markdown 片段都只能当无类型文本 |
| **消费是"全有或全无"** | 内联的是整份字节,无法只要其中一段 | 上游输出很长时,下游被迫吃全量,浪费上下文与钱 |
| **agent 的产物无法回流** | coding_agent 在 worktree 副本里可以写文件,但只有 `stdout` 和由冻结 baseline/result 普通文件字节比较生成的文本 unified diff 会变成产物 | agent 生成的 `report.md`、`metrics.json` 全部丢弃 |
| **人的材料无法进图** | 只有 `task` 一个文本入口 | 想给一份长材料/一份 CSV,只能粘进任务描述框 |

一句话:**当前不是"通讯没有文件",而是"通讯只有一条被压平的文本管道"。**
主人要的是把这条管道**扩成有名字、有类型、可多份的通讯文件层**。

---

## 2. 设计目标与非目标

### 目标

1. 一个节点可以产出**多份命名产物**,每份有自己的 media_type。
2. 下游可以**按名字精确消费**其中的某几份,而不是被迫吃全量。
3. `coding_agent` 在其隔离副本里生成的文件，可以**声明为产物**回流给下游。当前 `research` 没有可写项目副本，本 RFC 不暗示它已经具备同样的文件收集能力；若未来扩展，必须单独定义执行和权限边界。
4. 人可以在启动运行时**附加材料文件**,作为图的额外输入。
5. 以上全部沿用现有完整性保证:哈希断言、缺失显式失败、不截断、write-once、
   加载期接线校验、事件账本可审计。

### 非目标(明确不做)

- **不做通用可写文件系统**。通讯文件只在 `runs/<run_id>/` 之内,生命周期 = 一次运行。
  节点永远不能借它写用户项目目录(那是 coding_agent 隔离副本的职责,且只写副本)。
- **不做节点间共享可变状态**。通讯文件是 write-once 的产物,不是黑板/数据库。
  没有"节点 B 改一下节点 A 的文件再传给 C"这种语义——那会摧毁哈希审计链。
- **不做任意代码节点**(红线①)。声明式 YAML,封闭字段。
- **不放弃字节级完整性**(红线③)。任何"为了省 token 而截断"的方案直接否掉。

---

## 3. 核心设计:把"产物"从单数变复数

### 3.1 概念模型

引入一个概念:**通讯文件(comm file)= 带名字与类型的运行内产物**。

它不是新东西,而是把现有 `ArtifactRef` 从"每节点一份"扩成"每节点多份":

```
产物名(全局唯一)     = <节点id>.<槽位名>
槽位名(slot)         = output | diff | 用户在 YAML 里声明的名字
物理位置             = runs/<run_id>/artifacts/<节点id>.<槽位>.<轮次>.<ext>
完整性               = 每份都有 .sha256 旁车 + state 里的引用哈希
消费                 = consumes: [task, A.output, A.metrics, B.report]
```

关键决策:**沿用现有的 `<名字>` 命名空间和 `consumes` 语法**,不引入第二套引用机制。
好处是加载期校验、投影内联、事件账本、前端产物页签全部**自动继承**,改动面最小。

### 3.2 三种产出方式(封闭清单)

#### (a) `outputs` 声明:llm 节点产出多份结构化产物

YAML 新增可选字段 `outputs`,声明除 `output` 之外的附加槽位:

```yaml
- id: analyst
  type: llm
  prompt: |
    分析材料。先输出人读的结论正文。
    然后在末尾用围栏块给出两份机器可读产物:
    ```atlas:file name=metrics media_type=application/json
    {"risk": "high", "confidence": 0.7}
    ```
    ```atlas:file name=table media_type=text/csv
    项目,评分
    A,3
    ```
  consumes: [task]
  outputs:
    - name: metrics
      media_type: application/json
      required: true          # 缺失即节点失败(默认 true)
    - name: table
      media_type: text/csv
      required: false
```

执行时:模型返回的整份文本仍原样落盘为 `analyst.output`(**不变,审计基线**),
同时解析 `atlas:file` 围栏块,把每份内容**额外**落盘为 `analyst.metrics`、`analyst.table`。

为什么用围栏块而不是让模型写文件:llm 节点没有文件系统权限(核实过:纯 HTTP 调用),
也不该有。围栏块是"模型唯一能表达多份产物"的方式,且原文仍在 `output` 里可审计。

失败语义(fail-closed):
- 声明了 `required: true` 但围栏块缺失 → 节点失败,报"声明了产物 metrics 但输出里没有",
  与现有 `required_fields`(JSON 字段缺失)同级;
- 围栏块声明的 name 不在 `outputs` 白名单 → 忽略并记 warning 事件(不静默:界面可见);
- `media_type` 与内容不符(如声明 json 但解析失败)→ 节点失败。

#### (b) `collect_files`：coding agent 把副本里的文件声明为产物

此能力仅为未来的 writable `coding_agent` 设计。当前 `research` 使用受限工具和临时工作目录，不承诺一个可供收集的项目副本，因此不纳入首版 `collect_files`：

```yaml
- id: implementer
  type: coding_agent
  workdir: D:/path/to/project
  collect_files:
    - name: report
      path: .atlas-out/report.md      # 相对隔离副本根,禁止 .. 与绝对路径
      media_type: text/markdown
      required: false
    - name: metrics
      path: .atlas-out/metrics.json
      media_type: application/json
```

执行时:CLI 跑完后,从隔离副本读这些路径,`store_artifact` 成 `implementer.report`、
`implementer.metrics`。路径校验:必须相对、解析后必须仍在副本内(防 `..` 穿越)、
单文件与总量都有上限(沿用 `DIFF_MAX_BYTES` 量级的常量),超限则显式失败而非截断。

这条同时解决了"agent 产物只能靠 stdout"的损失。任务指令里会告诉 agent:
"需要给下游的结构化产物写到 `.atlas-out/` 下"。

#### (c) `attachments`:人在启动运行时附加材料

启动 run 时可附加文件,成为 `attachment.<名字>` 产物:

```
POST /api/workflows/{id}/run
{ "task": "...", "attachments": [{"name": "spec", "media_type": "text/markdown", "content_base64": "..."}] }
```

节点可 `consumes: [task, attachment.spec]`。加载期校验:引用的 attachment 名必须
在运行请求里提供,否则**在分配 run_id 之前**拒绝(与现在"模型未配置就拒绝"同一位置)。

大小上限明确(如单份 4 MiB、总量 16 MiB),超限拒绝不截断。落盘到
`runs/<id>/artifacts/attachment.<名字>.<ext>`,与其他产物同等审计。

MCP 侧同步时，首版只接受调用方显式传入的附件字节（例如 base64 + 名称 + media type），不接受让 MCP server 自行读取任意本机路径。后者会扩大 harness 对本机文件的读取能力；只有未来引入显式授权根目录、规范化路径、no-follow 检查、大小上限和审计事件后，才可另行提案。

### 3.3 消费侧:精确按名字取,可选择性内联

`consumes` 语法不变,只是可引用的名字变多了:

```yaml
consumes: [task, analyst.metrics, implementer.diff, attachment.spec]
```

投影内联保持现在的格式与铁律(原样字节、分隔符包裹、整份落盘、哈希断言)。
**这就是"精确消费"的实现**:下游只内联它声明的那几份,不再被迫吃全量。

进一步的可选项(本轮不做,记录为后续演进):对超大产物支持"按引用传递"——
投影里只放路径与摘要,让 agent 节点自己去读。它会破坏"字节 ⊆ 投影"这条强不变式,
需要单独设计一条弱化但仍可审计的不变式,故不在本轮范围。

---

## 4. 架构影响面(按层)

| 层 | 文件 | 改动 |
|---|---|---|
| 规格 | `atlas/spec.py` | `NodeSpec` 加 `outputs`/`collect_files`;`_parse_node` 白名单扩两项并做封闭校验(名字合法、槽位不撞 `output`/`diff`、路径相对且无 `..`、media_type 在白名单);`validate_spec` 的 consumes 规则扩展到"任意已声明槽位 + attachment.*" |
| 完整性 | `atlas/integrity.py` | 不变(`store_artifact`/`read_artifact`/`build_projection` 已经是按引用工作的,天然支持多份) |
| 产物元数据 | `atlas/artifacts.py` | `ARTIFACT_ROLES` 已含 `output/report/diff/projection/raw`,新槽位统一用 `raw` 或新增 `data` 角色(倾向新增 `data`,让界面能区分"给机器的结构化产物") |
| 执行 | `atlas/engine.py` | llm 节点在 `store_artifact(output)` 之后解析 `atlas:file` 围栏块并落盘附加产物;事件 `node_done.artifacts` 自然多几条 |
| 执行 | `atlas/nodes/agent.py` | CLI 跑完后按 `collect_files` 从副本收集;任务指令补一句"结构化产物写 `.atlas-out/`" |
| 入口 | `atlas/web.py`、`atlas/mcp.py` | `run` 接口接受 `attachments`;加载期校验引用的 attachment 是否提供;dry_run 显示将产生/消费哪些通讯文件(零成本) |
| 前端 | `web/src/NodeDetail.tsx` | 产物页签从"报告/代码改动/完整输入"扩成动态列表(每份通讯文件一个页签,按 media_type 选渲染器,复用本轮新增的大窗口查看器) |
| 前端 | `web/src/GraphView.tsx` | 边的标签可显示传输的产物名(可选,增强"数据流可见") |
| 文档 | `skill/SKILL.md`、`web/src/guide/*` | 教 AI 与人怎么用通讯文件;强调它只在 run 内、write-once |

**注意本轮已确认的相邻改动**:`prompt` 与 `workdir` 进入 `node_overrides` 白名单。
`outputs`/`collect_files` 属于**产物契约**(等于拓扑的一部分),因此**不进**覆盖白名单——
改它们要改 YAML。理由:它决定下游能不能接线,运行时改会让 consumes 校验失去意义。

---

## 5. 与四条红线的关系(逐条论证)

**① 无任意代码节点** — 通讯文件是声明式的:名字、路径、media_type 都在 YAML 的封闭字段里。
没有引入任何"填一段代码然后执行"的入口。围栏块只是**模型输出的解析格式**,
不会被求值。

**② YAML 是图真相** — 产物契约(有哪些槽位、消费哪些)只能在 YAML 里定义;
运行时覆盖白名单不含它们。attachment 是**运行输入**(与 task 同级),不是图结构。

**③ 数据不静默丢失,校验先于花钱** — 每份通讯文件都走 `store_artifact`(write-once +
`.sha256` 旁车),消费时走 `read_artifact`(哈希断言),投影仍是原样字节内联。
新增的所有失败路径都是**显式失败**:声明必需却缺失、路径穿越、超限、
media_type 不符、引用未提供的 attachment(在 run_id 之前拒绝)。
A1 的不变式"被消费产物的字节 ⊆ 投影字节"对每一份通讯文件继续成立。

**④ 界面只绑 127.0.0.1** — 不涉及网络面变化。attachment 上传走既有的
`X-Atlas-Request` + Host 校验的本机写接口。

补充安全边界:
- 通讯文件**永远只写 `runs/<run_id>/` 之内**。`collect_files` 只**读**隔离副本,
  不给任何节点新增对用户目录的写权限。
- `collect_files` 的路径必须相对且解析后仍在副本内;符号链接逃逸要显式拒绝。
- attachment 内容由本机用户提供，落盘前算哈希并写入账本；不解析、不执行。
- 首版 MCP 附件不得是 server 侧任意路径读取。若未来允许路径，必须将授权根目录、路径规范化、reparse/symlink/hardlink/设备文件拒绝、竞态复核和读取审计作为独立威胁模型，不可仅依靠“本机运行”放宽。

---

## 6. 实施方向(建议分三阶段,可独立验收)

### 阶段 A:多产物基座(改动最小,收益最大)
1. `spec.py`:加 `outputs` 字段与封闭校验;扩展 consumes 引用规则。
2. `artifacts.py`:新增 `data` 角色。
3. `engine.py`:llm 节点解析 `atlas:file` 围栏块,落盘附加产物,进 `node_done.artifacts`。
4. 前端:节点详情产物页签动态化 + 复用大窗口查看器。
5. 测试:围栏块正常/缺失必需项/未声明名字/media_type 不符/多轮迭代命名不冲突;
   A1 不变式对附加产物同样断言;假供应商即可覆盖,零成本。
6. 一个新示例或改造现有示例演示"结论 + 结构化数据"双产物。

### 阶段 B:agent 产物回流
1. `spec.py`：加 `collect_files`（首版仅 writable `coding_agent`）和路径安全校验。
2. `nodes/agent.py`:跑完从副本收集;任务指令补充 `.atlas-out/` 约定。
3. 测试:路径穿越拒绝、符号链接拒绝、超限显式失败、required 缺失失败、
   收集成功后下游可消费(FakeAgent runner,不花钱)。

### 阶段 C:人的材料入口
1. `web.py` / `mcp.py`:`run` 接受 `attachments`,加载期校验引用完整性。
2. 前端:运行栏加"附加材料"(本机文件选择,显示大小与哈希)。
3. 测试:超限拒绝、引用未提供的 attachment 在 run_id 之前 400、
   attachment 参与投影且哈希可断言。

每阶段结束都要:`uv run pytest` 全绿、前端 build/lint、真实浏览器看一眼产物页签,
并在当前验证记录中保存证据，不把 `docs/archive/VERIFICATION.md` 这份历史快照当作活动状态。付费真跑只在阶段 A 完成后做一次
(用一个双产物示例验证围栏块在真实模型上的可用性),先 preview 再真跑。

---

## 7. 已知风险与取舍

| 风险 | 判断 | 对策 |
|---|---|---|
| 模型不按格式输出围栏块 | **真实风险**,概率不低 | `required: true` 时显式失败并把原文留在 `output` 里可审计;prompt 模板给明确示例;失败链/retry 照常生效 |
| 围栏块与模型正文里的代码块混淆 | 需要精确解析 | 用 `atlas:file` 这种带前缀的 info string,只认带 `name=` 的;解析器必须容忍嵌套围栏(取最外层匹配) |
| 产物数量膨胀导致 run 目录很大 | 中等 | 每份有大小上限;总量上限;超限显式失败;`runs/` 清理已是既有实践 |
| 下游 prompt 需要知道产物结构 | 设计问题非技术问题 | 投影里的分隔符已含产物名;文档教"声明 media_type 并在 prompt 里说清字段" |
| attachment 变成"绕过 YAML 的隐形输入" | 需要可见性 | attachment 名必须被 consumes 显式引用才会进投影;dry_run 与界面都列出它;进 run 快照与账本 |

---

## 8. 明确留给下一轮的问题

- 超大产物的"按引用传递"(只给路径+摘要,让 agent 自己读):需要一条新的、
  仍可机器断言的弱不变式,不能悄悄放弃 A1。
- 产物之间的派生关系可视化(哪份产物喂给了哪个节点的哪个槽位)。
- attachment 的复用(同一份材料给多次运行用),现在每次运行独立落盘。
