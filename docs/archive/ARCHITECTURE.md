# 架构

一句话：**LangGraph 跑图，YAML 是图的真相，skill + MCP 是操控入口，Web 只做实时观测。**

读这份文档前先读 `../README.md`，特别是第 1 节的目标表和第 3 节的四条红线。
这里每个决定都是为了达成那些目标、守住那些红线。

---

## 1. 技术选型

### 分层

```
┌────────────────────────────────────────────────────────────┐
│  任意 harness（Claude Code / Codex / …）                     │
│  装上 Atlas 的 skill —— 人用自然语言描述需求，               │
│  agent 写 YAML、校验、调 MCP 工具跑                          │
└────────────────────────────────────────────────────────────┘
                    ↕ MCP（stdio）
┌────────────────────────────────────────────────────────────┐
│  Atlas MCP 服务（自建，工具面很小）                           │
│  · validate_workflow   校验，零成本                          │
│  · run_workflow        跑一张图（同步阻塞，事件实时落盘）      │
│  · list_workflows / get_run                                 │
└────────────────────────────────────────────────────────────┘
        │                              │
        │ 写/读 workflows/*.yaml        │ 事件流实时写
        ▼                              ▼
┌───────────────────────────┐  ┌───────────────────────────────┐
│  图定义（YAML，唯一真相）    │  │  运行记录 runs/<id>/           │
│  模型写、人也能看能改        │  │  events.jsonl + artifacts    │
└───────────────────────────┘  └───────────────────────────────┘
        │                              │
        ▼                              ▼
┌────────────────────────────────────────────────────────────┐
│  编排服务（自建，薄）                                         │
│  · YAML 校验 → 构造 LangGraph 图                             │
│  · 完整性校验层：产物落盘+哈希、消费端断言（核心价值）           │
│  · LangGraph：拓扑、状态、条件边、循环、并行、SQLite checkpoint │
│  · 模型适配层：五家供应商、失败链、假成功检测                   │
└────────────────────────────────────────────────────────────┘
                    ↕ SSE（只读）
┌────────────────────────────────────────────────────────────┐
│  Web 界面（自建，v1 只读）                                    │
│  实时看到：图结构、当前节点、每个节点的完整输入与输出、成本       │
└────────────────────────────────────────────────────────────┘
```

**为什么这样切。** 画布编辑器是整个产品里最难、最不确定的部分（拖拽交互 +
YAML 双向同步 + 布局持久化），而 v1 根本不需要它——搭建走 skill + MCP，
界面只做观测。这样 v1 里剩下的自建部分都是"薄"或"有清晰参考"的：
MCP 服务参考 Quorum 的成熟模式，界面参考任意只读 dashboard。

### 选型表

| 部分 | 选择 | 理由 |
|---|---|---|
| 语言 | Python 3.12+ | LangGraph 主版本语言；科研生态在这里 |
| 图引擎 | **LangGraph** | 见 `RESEARCH.md` 第 6 节。不自建 |
| 图定义格式 | **YAML** | 界面能读写、能进 git、能 diff、也能手写 |
| checkpoint | LangGraph 的 **SQLite** saver | 本地单机，避开 Postgres 类问题 |
| 后端框架 | FastAPI | 异步、SSE 简单、自带接口文档 |
| 前端 | React + TypeScript + Vite | 生态最大 |
| 流程图渲染 | React Flow（xyflow） | 事实标准 |
| 运行记录 | JSONL 事件流 + SQLite | 见第 6 节 |

**被否决的方案**见 `RESEARCH.md` 第 6 节，含 deepseek-harness、自建引擎、
n8n / Dify 类现成产品，以及各自的否决理由。

---

## 2. 图定义：YAML，界面读写

这是第二版的重要修正。第一版把图定义写成 Python 代码，那意味着"配置一个节点"
等于"改一行代码"——而项目主人不了解技术栈。**那样交付的是查看器，不是搭建器。**

一份图定义长这样：

```yaml
name: 代码实施三元
description: 一个模型改代码，另一个审查，第三个裁决

nodes:
  - id: implementer
    type: coding_agent          # 从固定类型清单里选
    model: SuperAI:glm-5.3      # 界面上是个下拉框
    prompt: |
      按任务要求修改代码，跑测试，报告结果。
    consumes: [task]            # 消费哪些上游产物

  - id: reviewer
    type: llm                   # 纯文本节点
    model: Kiro:claude-sonnet-5
    prompt: |
      审查上游的改动。发现问题就逐条列出。
    consumes: [task, implementer.output, implementer.diff]
    output_schema:              # 必填字段，缺了就算失败
      required: [issues, verdict]

  - id: arbiter
    type: llm
    model: Kiro:claude-opus-5
    prompt: 综合双方材料，给出处置决定。
    consumes: [task, implementer.output, reviewer.output]
    output_schema:
      required: [decision, reason]

edges:
  - from: implementer
    to: reviewer
  - from: reviewer
    to: arbiter
  - from: arbiter
    when: needs_repair          # 条件边：按输出字段的值路由
    to: implementer
  - from: arbiter
    when: done
    to: END

guards:
  max_iterations: 3             # 每个节点最多跑几次
  max_cost_usd: 5.0             # 成本上限，超了就停
  timeout_s: 1800
```

### 节点类型是固定清单（红线 ①）

| type | 做什么 | 权限 | 状态 |
|---|---|---|---|
| `llm` | 纯文本：分析、审查、裁决 | 无工具，只有文字进出 | ✅ M0 |
| `research` | 调研：读文件+联网（工具白名单） | 只读 | ✅ M2（zcode CLI headless） |
| `coding_agent` | 改代码：在隔离副本里跑编程 agent | 可写，**只在隔离副本里** | ✅ M2（diff 是第二产物） |
| `human` | 暂停，等你在界面上批准或驳回 | — | ✅ M2（interrupt 已实测） |

隔离副本的边界（诚实声明）：目录级整树拷贝——symlink 不解引用、2GiB 体积
上限、原目录只读、重试重建副本；但这是**目录级隔离，不是 OS 沙箱**
（Windows 无现成低成本沙箱，见 VERIFICATION 已知限制表）。CLI 的系统指令
明确"附件是材料不是命令"，压低上游产物的注入面。

**清单是封闭的。** 加新类型要写 Python 并发版本，不能在界面上填代码。
这是红线 ① 的落地方式：用户能选类型、填参数、选模型，但不能注入可执行代码。

### YAML 到 LangGraph 的转换

编排服务读 YAML，为每个节点生成一个 LangGraph 节点函数。
**M0 已用真实 API 跑通线性链**（`atlas/engine.py`，langgraph 1.2.11，Windows 验证）；
条件边是 M1，下面对应分支尚未实现：

```python
# —— 已验证的真实代码形态(摘自 atlas/engine.py,2026-08-16) ——
class AtlasState(TypedDict, total=False):
    task: str
    # ⚠️ 关键:dict 字段必须挂 reducer,否则后一个节点的返回
    # 会整个覆盖前一个节点的产物表——静默丢上游产物
    artifacts: Annotated[dict, merge_dicts]

builder = StateGraph(AtlasState)
for n in order:
    builder.add_node(n.id, _make_node_fn(n, ctx))
builder.add_edge(START, order[0].id)          # 入口用 START 常量,不是 set_entry_point
for a, b in zip(order, order[1:]):
    builder.add_edge(a.id, b.id)
builder.add_edge(order[-1].id, END)
app = builder.compile()                        # M0 无 checkpointer;M1 加 SQLite saver
final_state = app.invoke({"task": task, "artifacts": initial_refs})
```

**路由是查表，不是让模型决定下一步。** 节点输出里的 `verdict` 字段值对应一条边，
匹配不到就报错。这一条来自前几代的教训：把"下一步走哪"交给模型执行，
就得配一个必填的复述字段加十次人工目检来兜底，而它本来是一行赋值。

---

## 3. 每个节点跑不同模型

LangGraph 的节点就是普通 Python 函数，所以"每节点不同模型"不需要任何特殊机制——
函数里调哪个客户端就是哪个模型。配置从 YAML 的 `model` 字段来，界面上是个下拉框，
候选项从 `config/providers.json` 读。

```python
def make_llm_node(node: NodeSpec):
    def run(state: AtlasState) -> dict:
        # 1. 组装输入：从产物库按 consumes 取，逐个校验哈希
        projection = build_projection(state, node.consumes)   # 见第 4 节

        # 2. 调模型：走失败链，含假成功检测
        result = call_with_fallback(node.model, node.prompt, projection)  # 见第 5 节

        # 3. 产物落盘 + 记哈希
        artifact = store_artifact(node.id, result)

        return {"artifacts": [artifact], "events": [...]}
    return run
```

**M0 已按此实现并验证**（`atlas/engine.py::_make_node_fn`，事件顺序
`node_input → node_started → (model_failed…) → node_done` 落进 events.jsonl，
真实运行 5 次核对）。

三步的顺序是刻意的：**先校验输入，再调模型，再落盘。** 输入没校验就调模型，
等于花钱买一份基于残缺材料的输出。

---

## 4. 完整性校验层（核心）

红线 ③ 的落地。这一层的存在理由是：前三代同类项目全部死于同一个 bug——
节点 B 拿到的不是节点 A 的完整输出，而输出看起来完全正常。

### 产物按引用传递，不按值

节点输出写成文件，下游拿到**路径加哈希**，自己读全文。

```
runs/<run_id>/artifacts/
  implementer.output.1.json      # 第 1 轮
  implementer.output.1.sha256    # 内容哈希
  implementer.diff.1.patch
  reviewer.output.1.json
  implementer.output.2.json      # 第 2 轮，不覆盖第 1 轮
```

好处有四个：零截断；绕过 Windows 命令行长度上限（32767 字符，大 diff 当参数传必炸）；
返回给界面的可以只是摘要加路径；原文永久留存可回溯。

### 三条铁律

**① 读取时校验哈希，不符即中止。**

```python
def read_artifact(ref: ArtifactRef) -> str:
    content = ref.path.read_text(encoding="utf-8")
    actual = hashlib.sha256(content.encode()).hexdigest()
    if actual != ref.sha256:
        raise IntegrityError(
            f"产物 {ref.path} 的内容与落盘时的哈希不符。\n"
            f"  期望 {ref.sha256[:16]}…，实际 {actual[:16]}…\n"
            f"  这说明文件在落盘后被改动或损坏。本次运行中止。"
        )
    return content
```

**② 缺失产物显式失败，绝不给空串。**

```python
if artifact is None:
    raise WiringError(
        f"节点 {node.id} 声明消费 {name!r}，但产物库里没有它。\n"
        f"  这说明图的拓扑与执行顺序不一致——消费方在产出方之前被调度了。"
    )
```

给空串继续跑，就是让审查者审查空气然后输出一份看起来正常的报告。这正是那个 bug 的形态。

**③ 超长不截断，显式失败并给补救建议。**

纯文本节点必须把材料内联进 prompt。超过上限时**抛错**，提示改用能读文件的节点类型，
或减少扇入数量。宁可显式失败，也不静默截断。

### 截断哨兵（已有实测证据）

调用返回后，比对 prompt 的预期长度与供应商返回的输入 token 数。差距过大就报错：

```python
expected_tokens = len(prompt) / 3        # 粗估
if usage.input_tokens < expected_tokens * 0.3:
    raise TruncationError(
        f"疑似 prompt 未完整送达：预期约 {len(prompt)} 字符"
        f"（≈{expected_tokens:.0f} tokens），实际 input_tokens={usage.input_tokens}。"
        f"这通常意味着传参链路断了。"
    )
```

**这个机制不是理论设计，它在本次调研中真的抓到了一次。** 一个节点报出
"预期约 11154 字符，实际 input_tokens=108"——那就是数据传递断裂，
在没有哨兵的系统里它会安静地穿过去。详见 `RESEARCH.md` 第 9 节。

⚠️ 依赖 `usage` 字段，而部分网关不返回它。拿不到时记一条警告，不能当成"检查通过"。

### 与 LangGraph 的关系

LangGraph 负责把状态从 A 传到 B，这层它做得没问题。我们校验的是**产物内容本身**
在落盘和读取之间没有变化，以及消费方真的拿到了它声明要的东西。
两者不重叠：LangGraph 保证"引用传到了"，我们保证"引用指向的内容是完整的"。

---

## 5. 模型适配层：稳定性的真实难点

⚠️ **这一层的代码量不大，但坑最多。不要因为它在架构图的底部就以为它简单。**
第一版文档把它标成"薄"，同时又在别处写"稳定性的真实难点在这里"——自相矛盾，已改。

理由是一手数据：本次调研 7 次节点调用里 **3 次失败**，全部在这一层。
更要紧的是失败的形态（`RESEARCH.md` 第 9 节）：

| 实际发生的失败 | 会不会抛异常 |
|---|---|
| 返回成功但 result 字段为空 | **不会** |
| 只回了一个 "OK"，没有要求的字段 | **不会** |
| 进程退出码 1，错误输出为空 | 会，但没有任何诊断信息 |
| prompt 只送达 1%（108 / 11154 字符） | **不会**，靠哨兵抓 |
| 输出打满 max_tokens，句中被截断（M0 实测：deepseek 报 `finish_reason=length`） | **不会**，靠 `output_truncated` 警告抓 |
| 推理型模型把输出预算全烧在隐性思考上，可见文本为空（M0 实测：glm-5.3、deepseek-v4-pro 各两次） | **不会**，靠「返回内容为空」检查拦下后降级 |

**三分之二是"假成功"。** 它们返回 HTTP 200、格式合法、读起来正常，
只有内容是退化的。这是第二轮验证一致指出的盲区，第一版设计只按"报错类型"触发降级，
兜不住这一类。

### 假成功即降级

所以调用成功**不等于**节点成功。必须过三道检查：

```python
def call_with_fallback(model_ref: str, prompt: str, projection: str) -> NodeResult:
    for candidate in resolve_chain(model_ref):        # 主模型 + 备用链
        if breaker.is_open(candidate):
            continue                                  # 熔断中，跳过
        try:
            raw = call_model(candidate, prompt, projection)

            # 检查一：内容非空
            if not raw.text.strip():
                raise DegradedOutput("返回内容为空")

            # 检查二：prompt 真的送达了（截断哨兵）
            assert_not_truncated(prompt, raw.usage)

            # 检查三：必填字段齐全
            parsed = parse_output(raw.text)
            missing = [f for f in node.output_schema.required if f not in parsed]
            if missing:
                raise DegradedOutput(f"缺少必填字段：{missing}")

            return NodeResult(parsed, model_used=candidate)

        except (DegradedOutput, TruncationError, TransportError) as e:
            ledger.record_failure(candidate, e)        # 失败必须可见
            breaker.record(candidate, e)
            continue                                   # 换下一个候选

    raise AllCandidatesFailed(model_ref, attempts=ledger.attempts_for(model_ref))
```

关键点：**`DegradedOutput` 与网络错误走同一条降级路径。** 这就是"假成功即降级"。

### 输出截断的可见性（M0 实测后新增）

M0 的 5 次真实运行里，`max_tokens` 截断出现了 3 次（deepseek 的隐性推理
会烧输出预算，8192 token 只换回 3000–7500 字符的可见文本）。处置：

- **检测用协议信号，不用 token 数猜**：OpenAI 兼容端点的 `finish_reason=="length"`、
  Anthropic 端点的 `stop_reason=="max_tokens"`。纯阈值判断在实测里漏检过
  （报 8189/8192，差 3 个 token，报告实际在句中截断）。
- **M0 记警告不降级**（`output_truncated` 事件 + `node_done.output_truncated`
  字段）：截断的文本未必无用，降不降级是策略问题。M2 计划加
  `require_complete` 之类的节点级开关，让承重节点可把它升级为失败。
- **兜底是语义交叉检查**：完整性层保证下游模型看到的是全部原文，
  M0 里 reviewer 三次在语义上发现"报告在句中戛然而止"——这是设计意图
  （机器担保字节完整，模型担保内容完整）的活演示。
- **预算要按模型配**：推理型模型的适配器需要更大的 `max_output_tokens`
  （实测 glm-5.3 给 8192 时思考打满、文本为空；16384 后正常）。

### 失败链配置

每个节点可以配主模型加备用链。链上的候选**必须来自不同供应商**——
同一家网关出问题时，它的所有模型一起挂。

```yaml
- id: reviewer
  model: Kiro:claude-sonnet-5
  fallback:
    - Deepseek:deepseek-v4-flash
    - SuperAI:glm-5.3
```

### 降级必须可见

运行记录里分开记"请求的模型"和"实际应答的模型"。
用户会把备用模型的结论当成主模型的判断——尤其在多模型对比场景里，
两个节点降级到同一个模型时，"两份独立意见"就不独立了。界面上必须标出来。

### 熔断

只对"模型不可用"类错误计数（鉴权失败、模型 ID 无效、额度耗尽、连续超时）。
**不要**对"模型返回了但内容不合格"计数——那可能只是这一次提问的问题。

---

## 6. 运行记录：事件流

每次运行产生一条 append-only 事件流，落在 `runs/<run_id>/events.jsonl`。
它是唯一真相，界面显示的一切都从它派生。

```json
{"seq":1,"ts":"...","type":"run_started","graph":"triad","run_id":"..."}
{"seq":2,"ts":"...","type":"node_input","node":"reviewer","iteration":1,
 "projection_path":"runs/.../reviewer.input.1.txt","projection_sha256":"...",
 "consumed":[{"name":"implementer.output","path":"...","sha256":"..."}]}
{"seq":3,"ts":"...","type":"node_started","node":"reviewer","model_requested":"Kiro:claude-sonnet-5"}
{"seq":4,"ts":"...","type":"model_failed","node":"reviewer","model":"Kiro:claude-sonnet-5",
 "reason":"DegradedOutput: 缺少必填字段：['issues']"}
{"seq":5,"ts":"...","type":"node_done","node":"reviewer",
 "model_used":"Deepseek:deepseek-v4-flash","degraded":true,
 "output_path":"...","output_sha256":"...",
 "input_tokens":8231,"output_tokens":1442,"cost_usd":0.031,"duration_s":48.2}
```

⚠️ **`node_input` 事件是刻意加的。** 第一版的事件流只记输出，
而 G3 要求界面显示每个节点的**完整输入**——没有这个事件就没有数据来源。
第二轮验证把这一处点名为"规格空洞"，已补。

投影（即真正送进模型的那段文字）**整份落盘**并记哈希，界面显示的就是它。
这样"界面上看到的输入"和"模型实际收到的输入"是同一份，不是重建的近似值。

成本自己算：`cost_usd = input_tokens × 单价 + output_tokens × 单价`，
费率表放在 `config/pricing.json`（**需要你自己建并维护**，各家都不给现成的）。
拿不到 usage 时成本记 `null`，不要填一个猜的数字。

---

## 7. 操控与界面

**v1 的操控不走界面，走 skill + MCP。** 界面只做实时观测。
UI 编排（画布拖拽建图）推迟到最终版本——它是整个产品里最难且最不确定的部分，
而 v1 不需要它。

### 7.1 MCP 工具面（很小，刻意）

| 工具 | 作用 | 成本 |
|---|---|---|
| `atlas.validate_workflow` | 校验一份 YAML：格式、节点类型、连通性、死环、异质性 | **零** |
| `atlas.run_workflow` | 跑一张图；可带 `dry_run` 只渲染不执行 | 真实调用才花钱 |
| `atlas.list_workflows` | 列出已有的图 | 零 |
| `atlas.get_run` | 查某次运行的状态、结果路径、账本摘要 | 零 |

工具面刻意保持很小：**4 个工具**。harness 里的 agent 学得快、用不错。
每个工具的返回里都带"下一步建议"（比如校验失败时指出具体哪一行哪个字段），
减少 agent 来回试错。

**`validate` 和 `dry_run` 是省钱的闸门**：模型写的 YAML 可能有幻觉——
引用不存在的节点类型、边指向不存在的节点、图里有无出口的环。
这些全部在校验期拒绝，**零成本**。幻觉的图不应该花钱，这是从 Quorum
搬来的纪律（它的 `graph_invalid` 终态就是零成本的）。

### 7.2 run 的阻塞语义与"实时考察"

`run_workflow` **同步阻塞到整张图跑完**（和 Quorum 一致）。一张图几分钟，
harness agent 在等——**但人在 Web 界面上不是在等**：每个节点开始、每次模型调用、
每份产物落盘，事件都**实时写进** `runs/<id>/events.jsonl`，界面通过 SSE 实时看到。

这三方是并行的：

```
harness agent ──(MCP 调用，阻塞等待)──→ Atlas 引擎 ──(事件实时落盘)──→ Web 界面 ←─ 人实时考察
```

人在界面上发现问题 → 回到 harness 对话里说一句 → agent 改 YAML 重跑。
这就是 v1 的完整操作回路。

### 7.3 skill 的写法（给 harness agent 看的说明书）

skill 是 Atlas 对 harness agent 的全部"API 文档"。参考两份现成范例：

- **Quorum 的 `quorum-orchestrate` skill**：决策树（什么时候用/不用）、
  参数表、反模式表、成本量级、怎么读返回值——模式已被本机验证
- **dsh 的 `tool-workflow` DESCRIPTION**（`packages/workflow/tool-workflow/src/index.ts:138`）：
  把"给模型看的规范"写成一个自包含字符串——什么时候用（"only when the user
  explicitly asks"）、格式的精确契约、误用必炸的清单

从 dsh 那段规范里直接搬四条纪律：

1. **规范与工具同体**：格式契约写在工具描述/skill 里，不依赖部署时的 persona
2. **使用政策跟着工具走**："仅在用户明确要求工作流/多模型编排时使用"——防止 agent 滥用
3. **误用必炸，绝不静默降级**：传了不支持的参数就明确报错，不猜意图
4. **"agent 干活，编排只做协调"**：skill 里明确告诉 harness agent，
   你的职责是写好 YAML 和 prompt，不是替节点干活

skill 的骨架（M0 后再细化）：

```markdown
# atlas-orchestrate

## 这是什么
一个本地多模型工作流引擎。你写 YAML 定义节点和边，它跑图，人实时看。

## 什么时候用
用户明确要求"工作流/多模型协作/交叉验证/多角度调研"时。
单模型一次调用能解决的，不要用。

## 怎么写 YAML
（节点类型表、edges 语法、guards、consumes 语义——附一个最小完整示例）

## 反模式
- 互相审查的两个节点配了同一家模型（假独立）
- 消费方 consumes 里漏了要审的东西（审查空气）
- 不先 validate 就 run
- 图里有环但没设 max_iterations

## 成本量级
一次节点调用中位数几美分，长尾到过 $3+。大图先 dry_run。
```

### 7.4 Web 界面（v1 只读）

一个页面，左右两栏：

**左边：图。** React Flow 渲染 YAML 的节点和边，自动布局（dagre）。
运行时当前节点高亮、完成的打勾、失败的标红。YAML 里不存坐标。

**右边：详情抽屉。** 点任意节点：
- 这个节点**实际收到的完整输入**（投影原文，从 `node_input` 事件的落盘文件读，可下载）
- 它的**完整输出**（产物原文，可下载）
- 模型：请求了谁、实际谁应答（降级显式标注）、token、成本、耗时
- 每次失败尝试的错误分类

顶部一条：累计 token / 成本 / 耗时 / 运行状态。

**没有拓扑编辑功能。没有按钮去改节点、边、prompt 或权限边界。** 界面可为下一次运行
提交封闭清单内的临时节点参数覆盖；它不回写 YAML，并在运行前复用同一套节点校验。
结构改动仍发生在 harness 对话里（agent 改 YAML）或直接编辑文件。执行时先固化完全具体的
effective spec 快照，历史查看、恢复与审批只读该快照，不重新绑定当前模型或套用新覆盖。

### 7.5 通信

- 事件推送：**SSE**（单向够用，比 WebSocket 简单）
- 界面重连：先拉全量事件流，再续听（M1 实测踩坑后落地：客户端带
  `?after=<seq>` 自动重连直到终态事件；服务端在长空窗期发 keepalive 注释。
  事件空窗 92 秒就足以掐断裸连接——RESEARCH 9.2）
- 界面只绑 `127.0.0.1`（红线 ④）。**M1 追加两道**（独立审查查出）：
  Host 头白名单（防 DNS rebinding）+ 写操作要求 `X-Atlas-Request: 1`
  自定义头（防浏览器里恶意网页用 no-cors 静默驱动本机花钱——绑定
  127.0.0.1 挡不住浏览器，浏览器就是网络）

### 7.6 最终版本的 UI 编排（推迟，不是放弃）

主人明确"最后的版本可以去做"。推迟的理由：画布编辑 = 拖拽交互 + 双向 YAML 同步 +
布局持久化，是 v1 里最大的一块不确定性投入。而 skill + MCP 已经让"搭一张图"
对人变成说一句话的事。等 v1 跑稳、节点类型固定下来，画布编辑才有的放矢。

---

## 8. 人在环中（HITL）

只做一件事：**节点之间暂停**。

```yaml
- id: approve
  type: human
  prompt: 请审阅上游的改动，批准后继续。
```

跑到这个节点时，运行暂停，界面上出现批准/驳回按钮。你点了才继续。

**不做**节点执行中途的暂停，也**不做**暂停时编辑状态。理由见 `RESEARCH.md` 第 3 节：
所有平台在这里都出血，LangGraph 的两个开放 bug（循环里 interrupt 导致重复 resume、
时间旅行恢复 interrupt 不正确）以及 Dify 的"恢复后输出不显示也不持久化"全在这一带。
规律很清楚：**暂停好做，恢复难做。**

~~⚠️ HITL 的具体实现依赖 LangGraph 的 `interrupt` 语义，我没有实测过。~~
**M2 已实测（2026-08-16）**：`scripts/interrupt_smoke.py` 验证了"暂停—关闭
连接（模拟进程死亡）—新连接 `Command(resume)` 恢复—状态正确合并"与驳回
路径，退路方案不需要。已知开放 bug（循环里 interrupt、时间旅行恢复）不在
我们的用法里（节点边界暂停 + 同一 resume 语义），但含 human 节点的循环图
仍建议先小规模试。落地细节：审批在 Web 界面（批准/驳回按钮 + 批复说明），
spec 快照随 run 落盘（批复不依赖 workflows/ 里的 YAML 还在），人工审批的
等待时间不计入 `timeout_s`（那是人的时间，不是机器的）。

---

## 9. 目录结构

```
Atlas/
├── README.md
├── docs/
│   ├── ARCHITECTURE.md      本文件
│   ├── VERIFICATION.md      七条断言与里程碑
│   └── RESEARCH.md          事实、出处、被否决的方案
├── config/
│   ├── providers.json       供应商与模型（已从 Quorum 取来）
│   ├── models.reference.json 每个模型适合什么
│   ├── pricing.json         费率表（需自建，M0 未建，cost_usd 记 null）
│   ├── .env.example         密钥模板
│   └── .env                 真实密钥（gitignore）
├── workflows/               图定义（YAML，唯一真相）【M1 已实现】
│   └── proposal-review-repair-loop.yaml(示例,共 6 张)
├── runs/                    运行记录（gitignore）
│   └── <run_id>/
│       ├── events.jsonl     事件流（实时写，界面读它）【M0 已实现】
│       ├── checkpoint.sqlite【M1】
│       ├── artifacts/       节点产物原文 + 哈希【M0 已实现】
│       └── projections/     每个节点收到的完整投影原文 + 哈希【M0 已实现】
├── atlas/                   Python 后端（引擎 + MCP + Web API 同进程）
│   ├── config.py            providers.json + .env 加载,fail-closed 校验【M0】
│   ├── spec.py              数据模型 + YAML 解析 + 零成本校验【M1】
│   ├── engine.py            spec → LangGraph(条件边/循环/并行/checkpoint/续跑)【M1】
│   ├── m0_graph.py          M0 写死的两节点图 + 真实运行自检【M0】
│   ├── integrity.py         完整性校验层（产物/哈希/投影/缺失即失败）【M0】
│   ├── adapters.py          模型适配 + 失败链 + 假成功检测 + 假供应商【M0】
│   ├── events.py            事件流(append-only JSONL + fold_events)【M1】
│   ├── nodes/               四种节点类型的封闭注册表【M2,现在只有 llm 内置】
│   ├── mcp.py               MCP server（4 个工具）【M2】
│   └── web.py               FastAPI + SSE + 安全防护(只读观测)【M1 已实现】
├── scripts/
│   ├── langgraph_smoke.py   LangGraph Windows 冒烟 + reducer 语义验证【M0】
│   └── m0_real_two_node.py  真实两节点图入口【M0】
├── skill/
│   └── SKILL.md             给 harness agent 看的使用说明书（§7.3）【M2】
├── web/                     React + React Flow 前端(已构建,由后端托管)【M1 已实现】
└── tests/                   A1–A6 + 校验/安全/审查回归,共 59 个【M1】
```

**引擎、MCP、Web API 是同一个进程的三个入口。** 不拆服务——
本地单机，拆进程只会增加状态同步问题。一个进程读写同一份 runs/，
MCP 和 HTTP 只是两个协议适配层。

---

## 10. 刻意不做的事（v1）

| 不做 | 为什么 |
|---|---|
| 自建图引擎 | 自己写状态传递等于把三次失败的机会重新买回来 |
| **UI 编排（画布拖拽建图）** | v1 明确推迟。最难最不确定的部分，skill+MCP 已覆盖搭建需求。最终版本再做 |
| 让模型决定路由 | 路由是查表。交给模型就要配复述字段加人工目检来兜底 |
| 任意代码节点 | 红线 ①。模型生成的是 YAML 声明，只能引用封闭清单里的节点类型 |
| 图里嵌子图 | 扇出指数级、成本无界、从外面看不出来。以后要做再说 |
| 节点中途暂停 / 暂停时编辑状态 | 所有平台的共同出血点 |
| 绑 0.0.0.0 | 红线 ④ |
| 静默截断 | 宁可显式失败并给补救建议 |
| 多用户 / 云部署 | 非目标。认证是它的前置条件 |
| MCP 工具面扩张 | 4 个工具够 v1。工具越多 agent 越容易用错 |

---

## 11. 这份文档里哪些是未经核实的

诚实标注，避免被当成已知事实：

- ~~LangGraph 的 API 细节全部未实测~~ → **M0（2026-08-16）已在 Windows +
  langgraph 1.2.11 上验证**：`StateGraph` / `START` / `END` / `add_node` /
  `add_edge` / `compile` / `invoke`，以及 dict 状态字段挂 `Annotated` reducer
  的合并语义（不挂会静默覆盖上游产物表）。
  **M1（2026-08-16）继续验证**：`add_conditional_edges`（path_map 查表，
  router 异常原样传播）、SqliteSaver + `invoke(None, config)` 续跑
  （失败超步的 checkpoint 语义：成功兄弟分支的写入仍被提交）。
  **仍未验证**：`interrupt` 的恢复语义（M2 human 节点前必须先验证）、
  `update_state` 行为。
- **LangGraph Studio 的能力边界未实测**（第 7 节已标）。
- **各家网关是否尊重随机种子未实测**，直接影响 G6 的可复现程度。
- **成本估算**：`config/models.reference.json` 里的延迟与成本数字来自 Quorum 的账本，
  样本极少（多数是 3 个，有些是 0），文件里标了 caveat。别当性能排名用。
- **人力估算**：第二轮验证的三个模型分别估了"6–12 人周"和"核心闭环数周"，
  都是基于阅读的推测，不是测量。本文不写工期数字。
