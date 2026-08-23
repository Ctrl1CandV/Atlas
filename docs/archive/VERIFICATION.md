# 验证与测试（历史快照）

> **历史验证日志，不是当前测试报告。** 本文跨越多个早期阶段，后文同时保留当时未完成项和后来补录的结果；工具数量、runner、价格语义、路径和测试数字均可能过时。当前版本事实见 [`../STATUS.md`](../STATUS.md)，每个未来批次的退出标准见 [`../ROADMAP.md`](../ROADMAP.md)。

本文保留的核心原则仍有效：**每一条验收标准都必须机器可验证。** 不接受“看起来正常”或“跑了一遍没报错”；但本文的历史通过记录不能替代当前工作树上的重新执行。

---

## 1. 核心断言

A1–A7 在本节保留详细原理和示意；v2 新增的 A8–A11 已落地，定义与状态见
`docs/PLAN-v2.md` 第 8 节。每条都是自动化测试，每次改动后全部重跑。

### A1 · 完整性（命门）

**从源产物重算，断言字节级相等。**

```python
def test_a1_no_silent_loss():
    run = execute_graph("tests/graphs/two_node.yaml", task="...")

    # 节点 A 的产物原文
    source = (run.dir / "artifacts" / "node_a.output.1.json").read_bytes()

    # 节点 B 实际收到的投影（从 node_input 事件取）
    input_event = run.events.find(type="node_input", node="node_b")
    projection = Path(input_event["projection_path"]).read_bytes()

    # 源产物的全部字节必须出现在投影里
    assert source in projection, "节点 A 的产物没有完整到达节点 B"

    # 哈希也必须对得上
    assert input_event["consumed"][0]["sha256"] == sha256(source).hexdigest()
```

这一条是**命门**。它机器可检地证明没有静默丢失。

为什么是它：前三代分别以三种不同机制丢了产物——算好的投影被丢弃、
40k 的 diff 截成头 500 尾 500、把传递交给一个模型去执行。三次的后果相同：
审查者审查它没完整看见的东西，而报告读起来很专业。

**A1 必须在写任何界面代码之前通过。**

### A2 · 缺失即失败

```python
def test_a2_missing_artifact_fails_loudly():
    # 故意构造一个消费不存在产物的图
    with pytest.raises(WiringError) as e:
        execute_graph("tests/graphs/broken_wiring.yaml", task="...")
    assert "产物库里没有它" in str(e.value)
    # 关键：不能是"跑完了但结果是空的"
```

绝不允许"给个空串继续跑"。

### A3 · 假成功即降级 ⭐ 新增

**这是第二轮验证补上的一条，也是最贴近真实故障的一条。**

本次调研的一手数据：7 次节点调用 3 次失败，其中**三分之二不抛异常**——
返回成功但内容为空、只回了一个 "OK"、必填字段缺失。第一版设计只按"报错类型"
触发降级，兜不住这一类。

```python
@pytest.mark.parametrize("bad_response", [
    "",                          # 空
    "OK",                        # 只回两个字母（真实发生过）
    '{"conclusion": "..."}',     # 缺 required 里的其他字段
    "   \n  ",                   # 只有空白
])
def test_a3_degraded_output_triggers_fallback(bad_response):
    with fake_provider(primary_returns=bad_response, fallback_returns=GOOD):
        run = execute_graph("tests/graphs/two_node.yaml", task="...")

    # 必须降级到备用模型，而不是接受这份退化输出
    assert run.events.find(type="model_failed", model="primary") is not None
    node_done = run.events.find(type="node_done", node="node_a")
    assert node_done["model_used"] == "fallback"
    assert node_done["degraded"] is True
```

配套的截断哨兵测试：

```python
def test_a3b_truncation_sentinel():
    # 模拟 prompt 只送达 1%（真实发生过：11154 字符 → 108 tokens）
    with fake_provider(reported_input_tokens=108):
        with pytest.raises(TruncationError) as e:
            execute_graph("tests/graphs/big_prompt.yaml", task="...")
    assert "疑似 prompt 未完整送达" in str(e.value)

def test_a3c_missing_usage_warns_not_passes():
    # 网关不返回 usage 时，必须记警告，不能当"检查通过"
    with fake_provider(usage=None):
        run = execute_graph("tests/graphs/two_node.yaml", task="...")
    assert run.events.find(type="sentinel_skipped") is not None
```

### A4 · 恢复不重复执行

```python
def test_a4_resume_does_not_reexecute():
    run_id = start_graph("tests/graphs/three_node.yaml", task="...")
    wait_until_node_done("node_a")
    kill_process()                      # 模拟崩溃

    resumed = resume_graph(run_id)

    # node_a 只能出现一次 node_done
    assert count_events(resumed, type="node_done", node="node_a") == 1
    # 且它的产物哈希与崩溃前一致
    assert resumed.artifact("node_a").sha256 == before_crash_sha
```

### A5 · 路由不含模型调用

```python
def test_a5_routing_is_pure_lookup():
    with count_model_calls() as counter:
        route = resolve_route(graph, node="arbiter", output={"verdict": "needs_repair"})
    assert counter.total == 0, "路由过程调用了模型"
    assert route == "implementer"

def test_a5b_unknown_verdict_fails():
    # 输出里的 verdict 匹配不到任何边 → 报错，不猜
    with pytest.raises(NoRouteError):
        resolve_route(graph, node="arbiter", output={"verdict": "???"})
```

### A6 · 事件流是唯一真相

```python
def test_a6_state_is_fold_of_events():
    run = execute_graph("tests/graphs/three_node.yaml", task="...")

    # 从事件流重放出的状态，必须等于运行时的最终状态
    replayed = fold_events(read_events(run.dir / "events.jsonl"))
    assert replayed == run.final_state

    # 删掉派生缓存后重新计算，结果不变
    (run.dir / "cache").unlink(missing_ok=True)
    assert fold_events(...) == replayed
```

派生数据可以随时丢弃重建，事件流不能。

### A7 · 多厂商真的成立

```python
def test_a7_heterogeneity_is_real():
    run = execute_graph("tests/graphs/three_vendors.yaml", task="...")

    used = [e["model_used"] for e in run.events.filter(type="node_done")]
    assert len(set(used)) == 3, f"三个节点实际只用了 {len(set(used))} 个模型"

    # 降级必须可见
    for e in run.events.filter(type="node_done"):
        if e["model_used"] != e["model_requested"]:
            assert e["degraded"] is True
```

⚠️ 这条测试能挡住"都降级到了同一个模型"，但挡不住"两个不同的模型名背后
是同一个底层模型"（网关别名）。后者只能靠人工确认。

---

## 2. 里程碑

⚠️ **里程碑按"你能看见/能操作的东西"排序，不按架构层次。** 第一版文档曾把界面压在
最后，第二轮验证一致指出这复制了前三代的死法——"基础设施做完精力耗尽、界面没接通"。
v1 的操控走 skill + MCP（M2 交付），画布编辑推迟到最终版本，不占里程碑。

### M0 · 最小骨架 ✅ 完成（2026-08-16）

**目标：证明数据完整地从 A 到了 B。不做界面。**

- [x] 装上 LangGraph，跑通官方最简例子（Windows 10 + Git Bash + langgraph 1.2.11，
  另验证了 dict 状态必须挂 `Annotated` reducer——否则下游覆盖上游产物表，见
  `scripts/langgraph_smoke.py`）
- [x] 用真实 API 替换 `ARCHITECTURE.md` 里的示意代码片段（第 2/3 节已换为已验证形态，
  第 11 节更新了已验证/未验证清单）
- [x] 写死一个两节点图（`atlas/m0_graph.py`，不做 YAML 解析）
- [x] 两个节点绑不同厂商的模型，真实调用（Deepseek:deepseek-v4-flash 走 openai
  端点 → SuperAI:glm-5.3 走 anthropic 端点，两种传输协议都验证到；跑 5 次，账本在 runs/）
- [x] 产物落盘 + 哈希 + `node_input` 事件
- [x] 事件流落盘

**闸门：A1、A2、A3 全部通过。** ✅ `uv run pytest` 15/15
（A1 用 60k 字符产物；A2 断言含"失败发生在花钱之前"；A3 覆盖四种退化形态 +
传输错误 + 全链失败 + 截断哨兵 + 无 usage 警告 + 输出打顶警告）。

M0 真实运行中观察到的新失败形态（已按第 3 节纪律固定成测试）：

| 形态 | 抓它的机制 | 测试 |
|---|---|---|
| 输出打满 max_tokens，句中截断（3 次） | `finish_reason=="length"` → `output_truncated` 警告事件 | `test_a3d_output_cap_is_visible` |
| 推理模型把预算烧在隐性思考，可见文本为空（2 次） | 「返回内容为空」→ DegradedOutput → 降级 | `test_a3_degraded_output_triggers_fallback[""]` |
| 返回 200 但内容为空（deepseek，1 次） | 同上 | 同上 |

### M1 · YAML + 只读界面 ✅ 完成（2026-08-16）

**目标：能在浏览器里实时看到图和每个节点的完整输入输出。**

- [x] YAML 解析与校验（节点类型必须在封闭清单里；另覆盖：条件边分组、
  路由字段必须在 required、入口推断、可达性、SCC 死环、有环必须设
  max_iterations、consumes 精确引用——15 类错误零成本拒绝，`tests/test_spec_validation.py`）
- [x] YAML → LangGraph 转换（`atlas/engine.py::_build_compiled_graph`）
- [x] 条件边（查表路由零模型调用，A5）、循环（max_iterations 守卫 + 逐轮产物留档）、
  并行（无条件扇出，join 的投影含全部上游原文）各跑通一次
- [x] SQLite checkpoint + 崩溃续跑（A4；并行分支失败时成功分支的成果仍被
  checkpoint——实测语义，只重跑失败分支）
- [x] FastAPI + SSE（`?after=seq` 断线重连 + keepalive 注释；Host 白名单 +
  写操作自定义头防浏览器跨站驱动；rid/wid 白名单防 Windows 反斜杠穿越）
- [x] React Flow 渲染图（dagre 自动布局，YAML 不存坐标）
- [x] 运行视图：当前节点高亮、点节点看**完整**输入输出（投影/产物原文可下载）、
  降级/截断/失败尝试可见、累计 token（成本列显示 —，pricing.json 未建不填猜的数）

**闸门：A1–A6 通过，且能在浏览器里完成"打开一个 YAML 图 → 点运行 → 实时看到每个节点的完整输入输出"。** ✅
59/59 测试通过（含 A1–A6 全部断言）；浏览器全流程实测两次
（第二次覆盖 SSE 修复后的完整生命周期），事件流实时滚动、节点状态逐个流转、
详情抽屉的完整输入含上游产物原文。

**M1 独立模型审查记录（glm，2026-08-16）**：4 个 🟠（跨站 POST 驱动、撕裂账本
序号回卷、崩溃续跑产物覆盖隐患、反斜杠路径穿越）+ 6 个 🟡 全部修复，
每条都固定成 `tests/test_review_fixes.py` 的回归测试。其中"并行崩溃覆盖"
一条经实测证伪了审查时的推测（LangGraph 会提交成功分支的写入），
但 write-once 落盘防御保留——硬杀进程的场景仍在。

### M2 · MCP + skill 闭环 ⭐ v1 的核心交付 ✅ 代码完成（2026-08-16，待主人验收）

**目标：在任意装了 Atlas skill 的 harness 里，用人话搭图并跑通。**

- [x] MCP server：4 个工具（validate / run / list / get），stdio 握手实测通过；
  ID 白名单防路径穿越（审查补）
- [x] `validate`：格式、节点类型、连通性、死环、异质性，全部零成本拒绝
  （同 vendor 节点对提示"假独立"风险）
- [x] `dry_run`：渲染每个节点的 prompt 与预估 token，不执行
- [x] skill：`skill/SKILL.md`（决策树 + YAML 语法 + 反模式 + 成本量级 +
  四条 dsh 纪律：规范同体/使用政策/误用必炸/agent 干活编排协调）
- [x] 四种节点类型全部实现（封闭注册表 `atlas/nodes/`）
  - [x] `llm`
  - [x] `research`（只读工具白名单 + 联网，经 zcode CLI headless）
  - [x] `coding_agent`（隔离副本 + git diff 产物；副本保留 symlink 不解引用、
    2GiB 体积上限、重试重建副本——审查补强。诚实声明：目录级隔离非 OS 沙箱）
  - [x] `human`（interrupt 暂停 → Web 界面批准/驳回 → Command(resume) 续跑；
    暂停—重启进程—恢复语义先经 `scripts/interrupt_smoke.py` 实测）
- [x] 失败链 + 熔断（只对传输类错误计数，3 次开 600s，线程安全）+
  假成功检测（完整版，M0 起累积 7 种形态全有测试）
- [x] 成本上限守卫（节点边界检查；费率未知记 `cost_unknown` 警告并在界面显示——
  `config/pricing.json` 全 null 起步，不猜数字）+ spec 快照落盘（批复不依赖
  workflows/ 文件还在）+ 原子 RUN.lock（并发批复只放行一个）

**闸门：A1–A7 通过** ✅（90 个自动化测试全绿；A7 真实三厂商跑通）；
**"由一个不看代码的人，在装了 skill 的 harness 里用人话搭图、跑通、实时看到全过程"**
——⏳ 这一项需要主人亲自验收（见下）。

**主人的验收步骤（不需要写任何代码）：**
1. 在任意 harness（比如现在这个）里装上 Atlas 的 skill（把 `skill/SKILL.md`
   的内容放到该 harness 的 skills 目录），注册 MCP server：
   `uv --project <ATLAS_HOME> run python -m atlas.mcp`
2. 浏览器打开 http://127.0.0.1:8321
3. 对 harness 说一句人话，比如："用 Atlas 搭一个三节点图：一个模型调研、
   另一家厂商的模型分析、第三家汇总，然后跑起来"
4. 看着界面实时滚动，验收 G1/G2/G3

**M2 独立模型审查记录（deepseek，2026-08-16）**：1 个 🔴（agent_runner 生产
路径未接线——测试全靠注入掩盖了必崩，典型"绿灯陷阱"）+ 5 个 🟠 + 5 个 🟡
全部修复并固定成 `tests/test_m2_review_fixes.py`。审查模型降级说明：首选
glm/longcat 不可用，由 deepseek 兜底完成。

### M5 · 界面重做 ✅ 代码完成（2026-08-17，待主人主观验收）

按 PLAN-v2 第 5 节（判断修正后的定位：美观高级是一等目标）：

- [x] 设计基底：Tailwind v4 token 体系（四层表面、单一强调色、语义状态色，
  degraded/截断显眼）；Geist/Geist Mono 自托管 + 数字 tabular-nums；
  Phosphor 图标；渐变描边/玻璃顶栏/微噪点材质；明暗双主题
  （切换按钮 + localStorage + 首帧前内联恢复不闪屏）
- [x] 长文本查看器：虚拟滚动、搜索高亮（大小写不敏感、剥 CR）、折行开关、
  字符/行计数
- [x] diff 查看器（主题跟随）
- [x] **完整性第一等公民**：节点详情里每个 consumed 产物的"已验证/待消费"
  chip（含 sha tooltip）；画布上数据完整到达过的边变绿（边级确认态）
- [x] 画布升级：状态图标（运行=旋转/完成=对勾/失败=叹号）、运行中呼吸光环、
  活动边流动虚线、降级虚线边框+角标、失败尝试角标、耗时+token 等宽
- [x] **回边虚线 + 轮数上限标注**（决定 1 的 UI 义务：`when (≤3 轮)`）
- [x] 入场编排（motion spring 阶梯，reduced-motion 直达；全部动画遵守
  prefers-reduced-motion）
- [x] 稳定性修复（审查后）：getRun 请求序号防乱序覆盖 + 150ms 合并
  （历史重放不打接口风暴）；events 状态截断；智能滚动（翻历史不被拽底）；
  SSE 重连指数退避 + 15 次上限（进程被杀的 run 不永远重连）
- [x] 主按钮/状态徽章/设置页按钮样式补齐（审查发现的类名脱节回归）

**M5 独立模型审查记录（glm，2026-08-17）**：无 🔴；4 个 🟠（CSS 类名与组件
脱节致主按钮裸奔、主题切换三连 bug、getRun 竞态+风暴、PLAN 承诺落差——
回边标注与边完整性未做）全部修复；🟡 修复了 IntegrityChip 待消费不可达、
diff 主题硬编码、paused 标签、浅色不可读文字、对比度提升、edge-live
选择器收窄、reduced-motion 加固、JSON 解析保护。安全专项：无 XSS 面
（diff 库的 dangerouslySetInnerHTML 路径仅在自定义 renderer 时触发，
未使用）、下载链接双层防穿越。

**主人验收反馈的修复（2026-08-17 第二轮）**：
1. 思考深度可见性断链：reasoning_tokens + thinking 档位此前只到引擎层，
   现已进 node_done 事件 → 节点 ⚡徽章（含实际思考 tok tooltip）+ 详情
   「思考深度」行。链路：适配器 → CallOutcome → 事件 → 界面，全通
2. **coding_agent 真实端到端跑通**（此前只有假 runner 测试）：demo-project
   一个 fizzbuzz(15) bug，agent 在隔离副本里正确修复（+2 行分支判断），
   副本测试 1 failed → 4 passed，原目录分毫未动，diff 产物完整可审阅
   （run 20260817-134311-364e19）。执行器实测选型：**claude CLI + 干净
   --settings + Atlas 供应商的 anthropic 端点**（Kiro 实测可用）；
   zcode headless 的配置链在当前版本不可用（cli/config 的 model 键满足
   launcher 检查仍报缺失，未定位到深层原因，保留代码与记录）；
   claude 用户级 settings 被 codemoss 网关插件接管，必须 --settings 绕开；
   headless 写权限需隔离副本背书的 skip-permissions（default 模式下
   无人批准，agent 只能干看——demo 第一次复现）。材料走 stdin。
   diff 净化：排除 __pycache__/*.pyc 噪音
3. 产物 Markdown 渲染（渲染/原文切换，投影保持原文=字面真相）
4. 小改动大优化：count-up 数字滚动、顶栏 token sparkline、事件滑入、
   键盘可达（list-item/prov-head 可 Tab+Enter）、思考徽章、
   consumes 支持 `<id>.diff`（demo 抓到的真 bug：校验从未允许引用
   coding_agent 的第二产物）

**诚实标注（未完成，留待主人验收后按反馈迭代）**：
动效表中的 count-up 数字滚动、顶栏 sparkline、骨架屏、事件滑入动效；
diff 查看器的语法高亮/按文件折叠；TextViewer 的上下游材料折叠分节；
键盘可达性（tabIndex/回车）；Lighthouse 与 CL<0.1 实测。
浏览器自动化在验证中途再次故障（环境间歇问题，与代码无关）——功能验证
在修复前构建上全部通过，最终视觉验收由主人在浏览器完成。

### M3 · 模型与供应商配置界面 ✅ 代码完成（2026-08-16，按 PLAN-v2）

**目标：不打开任何文件就能配好供应商、勾选模型、填密钥。**

- [x] `atlas/credentials.py`：.env 结构保留式读写、原子写、ACL 尽力收紧、
  写操作加锁；`CredentialView` 类型上没有 value 字段
- [x] `atlas/discovery.py`：模型列表拉取（五家实测全部可拉；按各家可用协议
  逐个试；4MB 实读上限；错误四分类）
- [x] `atlas/configapi.py` + web 挂载：供应商 CRUD、密钥写入、测试连接
  （=拉取，零成本）、拉取勾选写回白名单、手填退路
- [x] 前端设置页（`#settings` 直达）：供应商卡片（凭据三态圆点）、
  拉取→勾选（已配置默认保持勾选，用户改过的行赢）、新建独立卡片
- [x] **A8 密钥永不出界**：类型断言 + 遍历全部 GET 端点扫描响应
- [x] 真实闭环（curl 走界面按钮背后的同一批端点）：新建→假密钥拉取（401
  正确分类）→真实密钥拉取（Deepseek 2 模型）→白名单写回→删除（窄清理
  连带删 .env 行）→providers.json 恢复原状
- [ ] **界面验收（留给主人）**：浏览器打开 `http://127.0.0.1:8321/#settings`
  亲自走一遍。自动化浏览器当时交互通道故障（与 M3 代码无关，"刷新"按钮
  同样超时），渲染与 API 闭环已分别验证

### M4 · 节点参数与图结构 ✅ 代码完成（2026-08-17，按 PLAN-v2）

- [x] **思考深度四档**（主人决定 3 落地）：先逐模型真实探测（判据=响应里
  真的出现思考内容，不是"参数被接受"）——9 个模型真支持（Deepseek/SuperAI
  全部 effort 档位；Kiro:claude-opus-4-8、Minimax:MiniMax-M3 budget 数值），
  **7 个假支持被揪出**（Kiro 其余 4 个、RightCode 全部：参数被静默吞掉）。
  四档映射按主人规则（effort 只有低中高时极高→高；budget→1024/4096/16384/
  32768）；none 模型校验期拒绝；设了没生效记 `effort_ineffective` 警告（A10）
- [x] **九个节点参数**全部落地，**A9：每个都有"断言真的改变了发出的请求
  或执行行为"的测试**（max_output_tokens/thinking/temperature/seed/writable/
  allow_web/allowed_paths/timeout_s/retry）
- [x] 多入口（`entry: [a, b]`；无显式 entry 时多根=全部入口并行；显式单值
  entry 只跑那条腿）+ 扇入等待（LangGraph 天然汇合，M1 已有）
- [x] 成本守卫两道查（派发前 spent+projected>cap，Quorum 教训：别等花超才停）
- [x] **A11 图必能终止**：静态（无出口环拒收、有环必设上限）+ 动态
  （永不收敛的环被 max_iterations 拦停并记账）
- [x] 真实验证：Deepseek effort 档真实生效（reasoning_tokens=173）；
  Kiro budget 档经**自动端点切换**（thinking 只在 anthropic 端点生效，
  默认传输是 openai——registry 支持同供应商双协议适配器）后生效

**M4 独立模型审查记录（deepseek，2026-08-17）**：2 个 🔴（budget 预算≥输出
上限时 high/xhigh 档必被网关拒——校验期补"预算<上限"检查并给修复指引；
显式单值 entry 在多根图中被静默扩成全部入口——显式意图优先，其余根因
不可达拒收）+ 4 个 🟠（node.timeout_s 混用 run 墙钟语义会误杀靠后节点——
拆分为"单次调用超时 vs 整图墙钟"两个语义；human 接受不生效的参数——
加类型守卫；spec 指纹加 entries 键会误杀旧 run 续跑——entries 仅非空时
进指纹；retry 与熔断交互的放大效应——文档记录）+ 4 个 🟡（测试缺口补齐、
capabilities 信任边界、web/mcp 展示）全部修复。157 测试全绿。

**行为变化（记录在案）**：所有供应商 SDK 的内建重试固定关闭，唯一重试权归
Atlas 的显式 `retry` 参数（默认 0）；每一次真实网络尝试都进入 Atlas 的失败账本。
agent 节点同样不提供隐藏的免费重试，瞬时故障场景建议显式配置 `retry: 1`。

**M3 独立模型审查记录（deepseek，2026-08-16）**：6 个 🟠（含一个真密钥外泄面：
跨源重定向原样转发 `x-api-key`，已改为不跟随重定向）+ 5 个 🟡 + 5 个 🟢
全部修复，固定成 `tests/test_m3_review_fixes.py`（9 条）。要点：拒删最后
一个供应商（防配置面自锁）、密钥先写后落盘（失败无半成品）、未知字段
写回保留（防界面保存静默吃掉手工配置）、读-改-写加锁（防并发丢更新）、
派生名碰撞检测与删除引用扫描、422 不回显请求体。

### M6 · 产品完整性修复 ✅ 代码完成(2026-08-17,按 PLAN-v3)

- [x] **M6-B 类型化产物**:node_done 携带 artifacts 数组(封闭角色
  report/output/diff/projection/raw,含 bytes/media_type/complete/metadata);
  state 与 fold 同构(A6 照旧全等);旧事件兼容路径;web 透出。
  **A6 全量回归通过**(157 基线 + 19 新测试 = 176 全绿)
- [x] **M6-B diff 采集净化**:stat 与 patch 分离(patch 顶部不再混 stat);
  numstat 解析为结构化元数据;递归 glob 排除(`**/__pycache__/**` 等
  封闭清单);超限落摘要并标 complete=False;新增、删除、重命名、
  二进制语义保留
- [x] **M6-B Diff 专用工作区**:自研 unified diff 解析器
  (file→hunk→line,新旧双行号,不二次计算、无 HTML 注入);
  全宽工作区:摘要栏/文件树过滤/统一分栏/折叠/j-k 跳转/复制下载/
  折行开关/大文件保护;节点详情页签化(报告/代码改动/完整输入)
- [x] **M6-D 思考三层语义**:能力(effort/budget/none/unprobed)、
  请求档位(provider_default/四档)、响应证据(reasoning_tokens/
  thinking_block/none)分层;reasoning_kind 从适配器全程进事件
  (真实验证抓到漏传并修复);「未配置」改为「供应商默认」;
  Anthropic 存在性不再显示为 1 token;设置页能力徽章(unprobed 不冒充)
- [x] **M6-C MiniMap**:useNodesInitialized 门控(测量前显示骨架不显残图);
  colorMode 显式传主题;nodeColor 按状态映射;切图 fitView 重适配;
  GraphView 以 workflow id 作 key;节点 box-sizing 与 Dagre 尺寸契约统一
- [x] **M6-A 示例体系**:六个正式示例(并行调研/辩论裁决/审查修复循环/
  人工审批/代码实施审查/分片汇总),全部真实供应商可跑;meta 块
  (封闭字段,requires 与图结构一致性校验,不进指纹);
  工作流发现界面(搜索/分类/卡片/结构标签——并行=无条件扇出)
- [x] **M6-A atlas_save_workflow**:校验不过不落盘;新建拒绝覆盖;
  更新 expected_sha256 乐观锁;同目录临时文件+os.replace 原子写;
  id 白名单+Windows 保留名+纯符号拒绝;同厂商警告
- [x] **M6-E 路由与指南**:hash 路由(#/observe、#/runs/:rid/n/:nid、
  #/guide/:chapter、#/settings),刷新/后退/深链恢复;八个章节的
  使用指南(构建期固定 manifest 导入,无运行时任意文件读取)
- [x] 真实端到端复验:fix-calculator 新链路(run 20260817-165452-88f9c4;该示例已于第五轮删除,能力由 code-change-review-approve 承接)
  ——report+diff 双产物、patch 无 __pycache__ 噪音、reviewer 显示
  provider_default + 2,256 reasoning tokens

**待主人验收**:浏览器主观验收(观测台/指南/Diff 工作区/MiniMap 深浅主题);
P1–P5 走查。

### 第五轮 · 优化轮验证(2026-08-18)

设计见 `docs/DESIGN-round5-optimizations.md`,七项优化按 P1–P6 实施。

**自动化(零成本)**:
- `uv run pytest` 全绿(203 通过,含本轮新增 8 个:prompt/workdir 覆盖白名单、
  摘要脱敏、human 仅 prompt、workdir 同路校验、隔离副本落位、投影含覆盖文本、
  param_defaults 与引擎默认一致×2)。
- 前端 `build`/`lint`(oxlint 0 警告)/`test:diff` 6/6 通过。
- 六个示例逐个加载校验通过;`grep` 确认无活引用悬空。

**MCP(零成本)**:
- `dry_run_impl` 带 prompt 覆盖:返回 `prompt_overridden:["author"]`,
  每节点 `prompt_overridden` 标志 + 覆盖后全文,`overrides` 摘要为
  `{changed, chars, sha256_prefix}`(全文不进摘要)。

**真实运行(付费,先 preview 后真跑,全部留账)**:
- `20260818-131311-1752e7`:reviewer(SuperAI:qwen3.8-max+thinking medium)
  返回非 JSON 被 DegradedOutput 拒绝——校验没有绕过。
- `20260818-131650-0045f7`:跨厂商 fallback 生效尝试(主+备都结构化不合格,
  两次 model_failed 都在账本里)。
- `20260818-132232-0fac7c`:暴露**示例 reviewer prompt 的真实缺陷**——
  三个不同模型都漏顶层 `severity`,根因是 prompt 把 severity 写成了
  每条 issue 的属性。已修 YAML(三个顶层字段显式列出,并去掉该节点
  预置的 thinking: medium——实测 qwen3.8-max 开思考后输出非 JSON)。
- `20260818-132825-3eb8d4`:修复后回边真实触发(第 1 轮 repair → author
  修订第 2 轮),两轮仍不收敛,`max_iterations=2` 大声拦停(GuardViolation,
  "循环未收敛,停止")——守卫语义按设计工作。
- `20260818-134023-c5d183`:**验收通过**。`run_done`,两轮收敛
  (repair→pass),author 的 prompt 覆盖完整进入两轮投影,
  9/9 artifact/projection sidecar 哈希匹配,事件序列完整
  (run_started→4×node_started/done→run_done)。

**浏览器视觉验收(2026-08-18,真实浏览器,IAB)**:
本轮会话中浏览器 webview 曾持续故障("browser guest not attached"),
恢复后完成全部三项目,深浅两主题都验过:

1. **P5 回边**:proposal-review-repair-loop 与 code-change-review-approve
   两张图,回边(smoothstep 底出顶入)与正向边几何分离
   (实测回边包围盒 y 297–412,正向边 y 355–401),标签
   `repair ≤2 轮` 挂在回边上;`.edge-back` 计算样式
   `stroke-dasharray: 5px, 5px` + 主题色描边;MiniMap 两图分别
   3/4 节点无退化。
2. **P2 参数区**:fallback chips 增删排序实测(加两项→上移→移除,
   候选自动排除主模型与已选项);prompt 覆盖提交后状态变
   "已覆盖 · 仅本次运行","恢复继承"回到 YAML 原文;顶部待选模型
   横幅随配置实时收窄;参数占位符正确(未选模型时
   "选模型后按供应商上限",timeout_s 显示 "默认 300s" 且 YAML
   预置值 180 正常显示);每个参数 hover 说明可见。
3. **P3 产物工作台**:验收 run `20260818-134023-c5d183` 的 author
   执行报告"放大查看"打开 676px 工作台(markdown 渲染模式,
   TextViewer fill 填满),关闭按钮可用;下载链接与 sha256 显示保留。

浏览器验收还抓出并修复了**两个真实缺陷**:
- 回边分类把环上的无条件边也判成回边(author→reviewer 被画成绕行,
  主数据流失去直连边)。修正判定:**带 `when` 且回到祖先**才算回边
  (循环必须由条件边驱动,这是引擎语义)。
- **END 节点从未有过入边句柄**,→END 的边被 React Flow 静默隐藏
  ——存量缺陷,第四轮及更早的 pass→END 一直没画出来。
  补句柄后 proposal 图 3 条边、改码图 4 条边全部可见。

### 第五轮 · 主人审查返工:边的画法重设计(2026-08-18)

主人看过界面后否掉了上一版的边:**链路全变曲线、回边不是半椭圆、
"repair ≤2 轮"仍卡在两节点之间、部分线被节点遮盖**。复盘确认根因:

1. **正向边句柄绑错(真回归)**:给节点加回边句柄时把它声明在右侧
   出边句柄之前,而 React Flow 对未显式指定句柄的边取**第一个**
   匹配句柄——全部正向边从节点底部出发弯向目标,还会从节点底下
   穿过(边层级低于节点)。上轮验收只对比了回边/正向边的 y 区间,
   没查正向边起点句柄,截图又无法目视——**验收方法有盲区,已认**。
2. smoothstep 矩形折线不是循环回边的正确形态。

**重设计(参考 mermaid / node-red 的 LR 流程图画法)**:
- 正向边一律 `type: 'straight'`,句柄全部显式 id 并被边显式引用
  (`in-left`/`out-right`),不再依赖声明顺序;
- 回边换成自定义 `LoopBackEdge`:手写三次贝塞尔
  `M sx,sy C sx,sy+d tx,ty+d tx,ty`,从源底部下探、节点行下方扫过、
  回勾进目标底部——半椭圆 U 弧;标签按贝塞尔 t=0.5 公式挂在弧的
  最低点。不用 `getBezierPath`:它的控制点偏移对同一行句柄恒为 0
  (`calculateControlOffset` 的 distance≥0 分支忽略 curvature),
  画不出下探(实测弧是平的才发现);
- Dagre `marginy` 24→110:fitView 只按节点包围盒适配,不留余量
  弧和标签会被裁出视野。

**重设计后实测(真实浏览器,几何测量)**:proposal 图正向边
y 355–355(严格水平直线),回边弧最低点 y 482(节点底 405 下方
77px),标签在弧底 x 590(两节点中心的中间、但**在节点行下方**);
改码图 3 条正向直线 + 回弧;并行图三条直线(水平/斜线)汇入空隙列
不穿节点;人工审批全直线;MiniMap 各图 3/4/5 节点无退化;
深浅两主题虚线与标签样式一致。pytest 203 + 前端三件套重跑全绿。

**M6 独立模型审查记录(deepseek,2026-08-17)**:3 个 🟠(diffParse 对
顶层 a/ b/ 目录二次剥前缀导致路径错位;_collect_diff 全量读内存判定超限
+git 子进程无 timeout;save 的检查-替换 TOCTOU 并发缺口)+ 11 个 🟡
(fold 注入 None 路径、畸形条目缺 name 即崩、非 git 提示不显示、git 吃
用户全局 color/ext-diff 配置、capability 枚举 unknown/unprobed 漂移、
_run_threads 泄漏、Diff 工作区键盘劫持输入框、切运行不关工作区、
启动运行不更新 URL、切运行残留节点选中、O(N²) indexOf)+ 3 项建议
**全部修复**。修复过程中又抓出两个真 bug:`--no-ext-diff` 放全局位导致
全部 git 调用 rc=129 被吞(diff 产物变空,回归测试立即抓住);八进制
转义路径需按 UTF-8 字节解码而非 Latin-1 码点。修复皆有回归测试
(180 测试全绿,前端 build+lint 通过,服务器已带终版重启)。
审查模型降级说明:首选 glm/longcat 不可用,由 deepseek 兜底。

### M6 的验收标准(P1–P5,设计记录)

主人第二轮界面验收确认了五项问题;完整方案与设计取舍见 `docs/PLAN-v3.md`。

**M6 保持不变的约束:** 不增加任意代码节点;YAML 和事件流仍是真相;
数据完整性与哈希不因前端重构退化;所有保存校验发生在写文件和花钱之前;
Web 继续只监听 `127.0.0.1`。

**闸门:** A1–A11 全绿(✅ 176 测试),加上 P1–P5 主人走查;
独立模型完成安全与兼容性审查;主人完成最终视觉与可理解性验收。

#### P1 · 示例不是限制

六个正式示例覆盖并行汇合、有限循环、人工审批和 coding agent；自定义图与示例
使用同一引擎；仅调用 MCP 的 agent 能安全完成设计、校验、保存和运行；非法图在
保存和调用模型前失败。

#### P2 · 代码改动可准确审阅

patch 以 typed artifact 进入事件、API 和前端；Web 下载字节与下游实际消费字节一致；
多文件、新建、删除、重命名、二进制、无换行结尾、截断和解析失败都有明确语义，
不再把整份 unified patch 当成相对空文本的一篇新增文档。

#### P3 · MiniMap 表达完整结构

MiniMap 节点数量、非零尺寸和相对布局正确；viewport 与运行状态同时可见；首次加载、
工作流切换、主题切换和窗口尺寸变化后均不残留、不消失、不失真。

#### P4 · 思考语义不误导

能力 `effort/budget/toggle/none/unprobed`、请求 `provider_default/low/medium/high/xhigh`
和响应 `reasoning_tokens/thinking_block/no_evidence` 分开显示；未探测不等于不支持，
presence 不显示为 token 数，指定但无证据时警告可见。

#### P5 · 不读源码也能完成基本操作

不熟悉技术栈的使用者只依靠产品内指南，能选择或生成工作流、校验并运行、查看完整
输入输出、理解状态和思考信息、审阅 coding agent 的改动，并找到安全边界和故障解释。

### 场景验证

- [x] **代码场景（基础闭环）**：`coding_agent` 在隔离副本修复真实小项目，跑测试，
  产出 diff，原目录未修改；专用 Diff 审阅与人工批准纳入 M6/P2
- [ ] **代码场景**：`coding_agent` 改一个真实小项目，跑测试，产出 diff，人工批准后落盘
- [x] **科研场景**：三个模型独立分析同一问题，第四个汇总，原文全部留存（`20260818-105642-b125e7`；主模型失败后显式 fallback 接管，降级可审计）
- [ ] 固定随机种子重跑，记录差异（**先确认各家网关是否尊重 seed**）
- [ ] 长时间运行不泄漏进程和文件句柄
- [ ] 幻觉 YAML 的拒收测试：让 agent 生成 10 张故意有错的图，验证全部零成本拒绝

**闸门：两个场景各连续跑 5 次，A1–A7 每次都过；10 张坏图零成本拒收率 100%。**

---

## 3. 测试怎么组织

### 假供应商（大部分测试用它）

```python
class FakeProvider:
    """可编程的假模型。不花钱，可复现，能精确构造各种失败。"""
    def __init__(self, responses: list[str], usage: dict | None = None,
                 fail_at: int | None = None): ...
```

**A1–A6 全部用假供应商测。** 好处：不花钱、毫秒级、能构造真实世界难复现的失败
（空返回、只回 OK、prompt 截断、进程退出码 1 且 stderr 为空）。

### 真实调用（少而精）

只有 A7 和里程碑闸门需要真钱。规矩：

- 标 `@pytest.mark.real_api`，默认跳过
- 每个里程碑手工跑一次，把账本存进 `runs/`
- 观察到新的失败形态就**加一个假供应商用例**固定它

这条最后一句是关键：真实失败一旦发生，就把它变成一个永久的自动化测试。
本文的 A3 就是这么来的。

### 一条纪律

**任何一次真实失败，都要先补一个能复现它的测试，再修。**
否则你只是修了这一次。

---

## 4. 已知会失败或做不到的

诚实标注，避免把已知限制当成 bug 反复调查。

| 项目 | 状态 | 说明 |
|---|---|---|
| 网关不返回 usage | 部分供应商 | 成本记 `null`，截断哨兵失效并记警告 |
| 随机种子可复现 | **可能做不到** | 多数网关不保证；先测再承诺 G6 的可复现程度 |
| 同底模冒充异质 | 测不出来 | 网关别名，只能人工确认 |
| Windows 沙箱 | 弱 | `coding_agent` 的隔离靠目录副本，不是 OS 级 |
| 节点中途崩溃 | 整个节点重跑 | 恢复粒度是节点边界，刻意的 |
| LangGraph interrupt 语义 | **未实测** | M2 前先验证；退路见 ARCHITECTURE 第 8 节 |

---

## 5. 上线前检查清单

```
□ A1–A11 全部通过
□ config/.env 不在版本库里（git status 看不到它）
□ config/ 目录下没有明文密钥（只有 .env 里有，且它被 gitignore）
□ 服务只绑 127.0.0.1
□ 没有任何"用户填代码然后执行"的入口（YAML 只能引用封闭节点类型清单）
□ 界面上能看到每个节点的完整输入输出，且可下载原文
□ 降级过的节点在界面上有明显标记
□ 成本上限守卫真的会拦停（构造一次超支验证）
□ 坏 YAML 全部在校验期零成本拒绝（抽 10 份幻觉图验证）
□ 一个不看代码的人，在装了 skill 的 harness 里用人话搭出并跑通一张三节点图，
  同时在浏览器里实时看到了全过程
```

最后一条是整个项目是否达成目标的最终判据。

---

## 6. 这份文档的局限

- 代码片段是**示意**，用的 API 名称未经核实（LangGraph 未安装）。
  M0 的第一件事是用真实 API 替换它们。
- 断言 A1–A7 的**设计**经过三个不同厂商模型的独立审查，但**实现**尚未存在。
- 不写工期估算。第二轮验证的模型给出了"6–12 人周"一类数字，
  那是基于阅读的推测而非测量，写进文档会被当成承诺。
