# 设计：第五轮优化（历史快照）

> **历史快照，不是当前产品或发布文档。** 本文是 2026-08-18 的设计与实施记录，文件路径、浏览器验收状态和后续计划均可能已过时。当前事实见 [`../STATUS.md`](../STATUS.md)，未来路线见 [`../ROADMAP.md`](../ROADMAP.md)。

当时状态：P1–P6 已实施；当时证据保留在本目录的 [`VERIFICATION.md`](VERIFICATION.md) 和 [`PLAN-v3.md`](PLAN-v3.md)。本文后文的“当前”“下一轮”均只相对于该历史时点。
实施中对本设计的一处补充:human 节点也开放了 prompt 覆盖
(白名单 `_HUMAN_OVERRIDE_FIELDS = {prompt}`),与"每个节点的职责
都该有输入"的意图一致;其余按设计执行。

性质:优化轮,不是修 blocker。七项彼此独立,但按依赖与风险排成 6 个阶段,
每阶段可单独验收、单独回滚。

主人的三项决策已定:
- **prompt 覆盖语义** = 完整覆盖(不是追加)。
- **workdir** = 允许运行时覆盖。
- **示例数量** = 8 → 6。

配套文档:`docs/DESIGN-node-io-files.md`(通讯文件层,作为下一轮独立实施)。
本文件只管七项优化;两者的交界点在第 3 节 P4 与第 6 节写清了。

---

## 1. 七项问题的调研结论(先把事实说准)

每一条都读过代码,不是推测。下面「现状」列的行号是本轮实施前的位置。

### 问题 1:fallback 为什么不是下拉框

**现状**:`web/src/NodeDetail.tsx` 的「下次运行参数」区里,`model` 已经是 `<select>`
(选项来自 `modelOptions`,即已配置密钥供应商的可用模型),但 `fallback` 是一个
逗号分隔的纯文本 `<input>`,占位符 `provider:model, provider:model`,
提交时用 `value.split(',').map(trim).filter(Boolean)`。

**结论:主人的判断成立,这是真实的错误风险。** 具体风险有三层:
1. 手打 `provider:model` 容易错(拼错供应商 id、模型 id、少冒号);
2. 后端会拒绝,但拒绝发生在 preview / run 校验时,不是输入当场;
3. `spec.py` 本轮已加的两条约束(fallback 内部不得重复、fallback 不得含主模型)
   在文本框里完全没有可视化反馈,用户要等报错才知道。

**方案**:改成「已选链 + 追加下拉」的 chips 组件。已选项显示为可删除、可上下移的
标签(fallback 是**有序**链,顺序即尝试顺序,这点必须在 UI 上表达);
追加下拉的候选集 = `modelOptions` 减去主模型、减去已选项——把两条约束变成
**结构上不可能违反**,而不是靠报错纠正。跨供应商的候选加一个标记(如厂商名分组),
因为跨厂商 fallback 才真正能抗单厂商故障(第四轮真跑已验证过这条价值)。

无已配置供应商时:整块禁用并复用 model 下拉已有的「先到设置配置」提示文案。

### 问题 2:llm 节点有没有改文件的权力

**现状(核实过,不是推测)**:
- `llm` 节点是纯 HTTP 调用,`engine.py` 里走 `call_with_fallback` → adapters,
  **没有任何文件系统入口**;
- `research`、`human` 同样没有写用户目录的能力;
- 只有 `coding_agent` 能改文件,且改的是 `shutil.copytree` 出来的隔离副本
  `runs/<run_id>/worktrees/<node>.<iter>`,**原目录一个字节都不碰**;
- 节点怎么知道在哪个文件夹工作:靠 YAML 的 `workdir` 字段
  (见 `workflows/code-change-review-approve.yaml:28`),运行时被复制成隔离副本,
  CLI 的 cwd 指向副本。

**结论:llm 节点没有、也不该有改文件的权力。** 这不是缺陷,是红线①(无任意代码节点)
的直接后果。要改文件就用 `coding_agent`,它自带隔离 + diff 审计。

**但主人指出的真问题在别处**,而且成立:节点间只有一条被压平的文本管道,
`consumes` 只能引用 `task` / `<节点>.output` / `coding_agent` 的 `<节点>.diff`
(`atlas/spec.py:675-703` 的精确后缀匹配),投影把整份字节内联进一份文本
(`atlas/integrity.py:125-131`)。信息量与形式确实受损。
**这部分不在本轮**,已单独写成 `docs/DESIGN-node-io-files.md`。

本轮只做与它相邻、且不动产物契约的一件事:**`workdir` 进入运行时覆盖白名单**
(主人已决策),让「同一张改码图指向不同项目目录」不必改 YAML。
路径校验沿用现有的隔离副本安全检查,不放宽。

### 问题 3:循环的展示不对

**现状与根因(两个独立缺陷叠加)**:

YAML 数据是对的。`workflows/proposal-review-repair-loop.yaml:39-47` 明确写着
`author→reviewer`、`reviewer--pass-->END`、`reviewer--repair-->author`,
回边确实存在。问题全在渲染:

1. **句柄只有一对**。`web/src/GraphView.tsx:73` 只有 `Handle type="target" position={Position.Left}`,
   `:114` 只有 `Handle type="source" position={Position.Right}`。
   于是 `reviewer→author` 这条回边被迫从 reviewer 的**右侧**出发、连到 author 的**左侧**,
   与正向边 `author→reviewer` 用同一对句柄、同一种默认贝塞尔曲线,
   **两条线几乎完全重合**。视觉上就成了主人看到的样子:标签像是挂在 author 和
   reviewer 之间的一根线上,分不清哪条是回修。
2. **`.edge-back` 样式根本不存在**。`GraphView.tsx:231` 给回边加了 `edge-back` 类名,
   但 `web/src/styles.css` 里只有 `.edge-live`(321-322 行),
   **搜不到任何 `.edge-back` 规则**。也就是说此前承诺的「回边虚线区分」从未生效过。

标签内容本身是对的:`isBack` 时拼成 `${when} (≤${maxIterations} 轮)`,
即 `repair (≤2 轮)`,与主人期望的「repair ≤2 轮」一致——只是挂在了看不出方向的线上。

**方案**:
- 给节点补第二对句柄:底部 `source`(id 如 `back-src`)+ 顶部 `target`(id 如 `back-tgt`),
  回边显式指定 `sourceHandle`/`targetHandle` 走这一对;
- 回边改用 `smoothstep` 类型并给 `pathOptions.borderRadius`,让它绕外侧走,
  与正向边在几何上分离;
- 真正补上 `.edge-back` CSS:虚线 + 更低饱和的描边色 + 标签底色,
  与 `.edge-live` 的流动虚线区分开(一个表状态,一个表拓扑);
- 深浅两个主题都要看一眼(`--color-line` 一类变量在两套主题下取值不同)。

影响面:**所有含回边的示例一起修好**,不是只修 proposal-review-repair-loop。
`code-change-review-approve`(`reviewer--repair-->implementer`)同样受益。

### 问题 4:给每个节点单独输入

**现状**:
- UI:节点详情的「下次运行参数」只开放封闭的运行参数(model / fallback / thinking /
  max_output_tokens / temperature / seed / timeout_s / retry / max_turns),
  `prompt` **不在** `atlas/effective.py` 的 `_LLM_OVERRIDE_FIELDS` 白名单里;
- MCP:`atlas_run_workflow(workflow_id, task, dry_run, node_overrides)`
  (`atlas/mcp.py:439-449`)——`node_overrides` 走的是同一个白名单,
  所以**MCP 现在也不能给单个节点加输入**。全图只有一个 `task` 文本入口
  (`engine.py:568` 把它落盘成 `task.txt` 产物)。

**结论:主人的判断成立,而且比他以为的更缺——MCP 也不行。**
「工作流不仅总任务要输入,每个节点的职责也该给一个输入」是对的:
现在唯一的表达方式是改 YAML 的 `prompt`,那属于改图,不该为了一次运行去做。

**方案(主人已决策:完整覆盖)**:`prompt` 进入 `_LLM_OVERRIDE_FIELDS`
与 agent 的覆盖白名单,语义是**完整替换本次运行该节点的 prompt**,不是追加。
理由:追加语义会产生「原 prompt + 补充」的隐式拼接顺序,审计时无法一眼看出
模型真正收到了什么;完整覆盖则让 `runs/<id>/` 里的有效规格快照就是真相。

必须同时满足的约束:
- 覆盖只作用于**本次运行**,YAML 不变(红线②:YAML 是图真相);
- 有效规格指纹变化必须进账本,`effective.py` 的 `override_summary` 要含 prompt
  被覆盖的事实(**不要把 prompt 全文塞进摘要**,记 `changed: true` + 长度 + 哈希,
  全文在有效规格快照里);
- `consumes` 依然**不可覆盖**——它是接线,改它等于改拓扑;
- UI 用多行文本域,旁边显示继承来的原 prompt 以便对照,并给「恢复继承」按钮;
- dry_run / preview 要显示哪些节点的 prompt 被覆盖了(零成本可见)。

### 问题 5:diff 和 markdown 的查看窗口太小

**现状**:
- 已经有一个全屏工作台 `web/src/DiffWorkSpace.tsx`(`:93` 起),但它**只服务 diff**;
  入口是 `NodeDetail.tsx:443` 的按钮 → `App.tsx:568` 的 `onOpenDiff` → `App.tsx:621` 渲染;
- markdown / 文本产物走 `TextViewer`,内嵌在节点详情抽屉里,
  高度被 `styles.css:484` 的 `.tv-md { max-height: 320px }` 卡住;
  diff 内嵌预览被 `styles.css:459` 的 `.diff-wrap { max-height: 320px }` 卡住;
- 抽屉本身约 500px 宽,320px 高的框里看一份长报告确实很难受。

**结论:主人的判断成立。** 而且这不是「要新建一套查看器」,
是**把已有的 DiffWorkSpace 泛化成通用产物工作台**——`TextViewer` 已经具备
渲染/原文切换、虚拟滚动(react-virtuoso)、搜索、换行开关,
缺的只是一个大尺寸的承载容器。

**方案**:
- 把 `DiffWorkSpace` 提升为 `ArtifactWorkSpace`:按 `media_type` 分派到
  diff 渲染器或 `TextViewer`(判定逻辑复用 `TextViewer.tsx` 里已有的
  `artifactViewerMode`:media_type 权威,role 仅兼容旧产物);
- 节点详情里每个产物页签(报告/代码改动/完整输入)都加「放大查看」入口,
  不只 diff 有;
- 工作台里保留下载原文与 sha256 显示(审计可见性不能因为换了容器就丢);
- 内嵌预览的 320px 上限改成随抽屉高度自适应,并保留「放大」作为主路径;
- 键盘可达:Esc 关闭、焦点陷阱、`aria-modal`——现有 DiffWorkSpace 的行为要一起复核。

### 问题 6:示例太多、有重复

**现状**:8 个示例。逐一核对职责后,重复是真的:

| 示例 | 展示的能力 | 判定 |
|---|---|---|
| `multi-vendor-debate-judge` | 跨厂商辩论 + 裁决 | 留(第四轮真跑过) |
| `map-reduce-document-analysis` | 扇出扇入 map-reduce | 留(第四轮真跑过) |
| `parallel-research-synthesis` | 并行调研 + 汇合 + fallback 降级 | 留(第四轮真跑过,含降级证据) |
| `proposal-review-repair-loop` | 回边 + 条件出口 + 轮数上限 | 留(问题 3 的主证人) |
| `code-change-review-approve` | 隔离改码 + diff 双消费 + human 门 | 留(能力最全的一张) |
| `human-approval-pipeline` | human 审批门 | 留(human 的最小教学形态) |
| `fix-calculator` | 隔离改码 | **删** |
| `sqlite-checkpoint-analysis` | 单节点长文分析 | **删** |

删 `fix-calculator` 的依据不是我的判断,是仓库里已有的自述:
`workflows/code-change-review-approve.yaml:2` 的注释原文就是
「本图是 fix-calculator 的完整形态」。保留一个被自己文档宣告为子集的示例没有意义。

删 `sqlite-checkpoint-analysis` 的依据:它的能力(单 llm 节点长文分析)是
其余每一张图的组成部分,没有独占的教学价值;而它的具体主题(分析本项目的
checkpoint 实现)对新用户是噪声。

**功能完整性核对(删完仍然全覆盖)**:human 审查节点 ✓(human-approval-pipeline、
code-change-review-approve)、循环 + 条件出口 ✓(proposal-review-repair-loop、
code-change-review-approve)、改码节点 + diff ✓(code-change-review-approve)、
并行扇出扇入 ✓(map-reduce、parallel-research)、跨厂商与降级 ✓
(multi-vendor-debate-judge、parallel-research-synthesis)、结构化路由 ✓
(route_field 出现在两张图)。**没有能力因为删示例而失去展示。**

另外两张并行图(`map-reduce-document-analysis` / `parallel-research-synthesis`)
拓扑相似,不删,但要**改文案让差异一眼可见**:前者是「同一份材料切片后并行处理再合并」,
后者是「不同角度独立调研再综合,且演示主模型失败后 fallback 接管」。
`meta.title` / `meta.description` / `tags` 都要能区分。

### 问题 7:运行参数的默认值与说明

**现状(逐个追出来的真实默认值)**:

| 参数 | 真实行为 | 用户看到的 |
|---|---|---|
| `max_output_tokens` | **总是**发给供应商。节点未设时用供应商上限:`cfg.max_output_tokens`,没配则 8192(`atlas/adapters.py:237-238`);本机 SuperAI 配的是 16384。成本预估也用 `node.max_output_tokens or 8192`(`engine.py:284`) | 空输入框,无任何提示 |
| `temperature` | 节点未设时**不发这个字段**,用供应商自己的默认 | 空输入框 |
| `seed` | 同上,不发 | 空输入框 |
| `timeout_s` | 单次调用超时。llm adapters 的构造默认 300s(`adapters.py:61,112`);agent CLI 是 1800s。注意与 `guards.timeout_s` 不是一回事——后者是 run 级墙钟,在节点边界检查(`engine.py:298-301`) | 空输入框 |
| `retry` | 默认 0,且**只重试传输类错误**,不重试内容类失败(降级/截断走 fallback 链) | 空输入框 |

**结论:主人的判断成立。** 现在的空输入框有二义性:用户看不出「空 = 继承供应商上限」
还是「空 = 不限制」。`NumericOverrideInput`(`NodeDetail.tsx:109-161`)
甚至没有 `placeholder` 参数可用。

**方案**:
- `NumericOverrideInput` 加 `placeholder` 与 `hint` 支持,空值时**灰显真实生效值**
  (例:`max_output_tokens` 的占位符显示「继承 16384(SuperAI 上限)」)。
  生效值必须来自后端而不是前端猜:后端 preview 返回每个节点的**有效参数**,
  前端只显示,不自己算——否则两边默认值会漂移。
- 每个参数配一句人话说明(hover / 展开的帮助行),内容必须与实现一致:
  - `max_output_tokens`:本次调用最多生成多少 token。打满会触发截断检测并显式失败,
    不会静默截断。
  - `temperature`:随机性。留空用供应商默认。要可复现就配合 `seed`。
  - `seed`:同样输入尽量得到同样输出。多数供应商只是尽力而为,不是保证。
  - `timeout_s`:**单次模型调用**的超时。与 `guards.timeout_s`(整个运行的墙钟)不同。
  - `retry`:传输类错误重试次数。内容不合格不走 retry,走 fallback 链。
- MCP 侧同步:`dry_run` 的摘要里显示每个节点的有效参数(现在只有部分),
  让 AI 编排时也看得见默认值,而不是靠猜。
- 说明文案同时进 `web/src/guide/*`,保持界面与指南一处真相。

---

## 2. 贯穿全轮的红线(每阶段收尾都要自查)

1. **无任意代码节点**:七项里没有任何一项引入「填代码然后执行」的入口。
   prompt 覆盖是文本,不求值。
2. **YAML 是图真相**:本轮新增的覆盖字段只有 `prompt` 与 `workdir`,都只影响本次运行;
   `consumes`、`outputs`、图结构、节点类型一律不可运行时改。
3. **数据不静默丢失,校验先于花钱**:所有新增拒绝路径都在 `run_id` 分配之前;
   查看器泛化不得引入任何截断(`TextViewer` 的虚拟滚动是渲染优化,不改字节)。
4. **只绑 127.0.0.1**:本轮不动网络面。

---

## 3. 分阶段实施计划(6 阶段,依赖顺序)

原则:**后端契约先行,前端跟随,示例与文档收尾。** 每阶段自带验收物,
不通过不进下一阶段。

### P1 · 后端覆盖契约:prompt + workdir(问题 2 的相邻部分、问题 4)

先做这个,因为 P2 的两个前端控件都依赖它。

改动:
1. `atlas/effective.py`:`_LLM_OVERRIDE_FIELDS` 加 `prompt`;
   `_AGENT_OVERRIDE_FIELDS` 加 `prompt`、`workdir`。
2. `atlas/effective.py`:`override_summary` 对 prompt 记
   `{changed: true, base_len, new_len, new_sha256_prefix}`,**不记全文**;
   workdir 记新旧路径。
3. `workdir` 覆盖的安全校验:与 YAML 里的 `workdir` 走**同一条**校验路径
   (存在性、必须是目录、隔离副本可创建);不因为来自运行时就放宽。
4. `atlas/web.py` preview 响应 + `atlas/mcp.py` dry_run 摘要:列出被覆盖 prompt 的节点。
5. 前端类型:`web/src/types.ts` 的 `NodeOverride` 加 `prompt`、`workdir`。

验收:
- `uv run pytest` 全绿,新增测试覆盖:prompt 覆盖后有效规格指纹变化且进账本;
  覆盖摘要不含 prompt 全文;`consumes` 覆盖被拒绝;非法 workdir 在 `run_id` 之前 400;
  workdir 覆盖后隔离副本落在 `runs/<id>/worktrees/` 且原目录未被改动。
- 零成本(FakeProvider + FakeAgent runner),不花钱。

### P2 · 前端参数区重做(问题 1、问题 4 的 UI、问题 7)

三项都改同一块「下次运行参数」区域,合并做,避免反复返工。

改动:
1. fallback chips 组件:有序、可删、可重排,候选集自动排除主模型与已选项,
   跨厂商分组标记。
2. prompt 覆盖文本域:多行、显示继承原文对照、「恢复继承」按钮。
3. workdir 覆盖输入(仅 agent 节点):显示继承值,校验错误就地显示。
4. `NumericOverrideInput` 加 `placeholder` / `hint`;占位符显示后端给的有效值。
5. 参数说明行(五个参数各一句,文案见第 1 节问题 7)。

验收:
- `npm --prefix web run build && npm --prefix web run lint && npm --prefix web run test:diff` 全过;
- 真实浏览器:选一个 llm 节点,fallback 加两项并重排、prompt 覆盖后「恢复继承」生效、
  空数字框显示继承值;深浅两主题各一张截图。

### P3 · 通用产物工作台(问题 5)

改动:
1. `DiffWorkSpace` → `ArtifactWorkSpace`,按 media_type 分派(复用 `artifactViewerMode`)。
2. 节点详情每个产物页签都加「放大查看」;`App.tsx` 的 `onOpenDiff` 泛化为 `onOpenArtifact`。
3. `styles.css:459` / `:484` 的 320px 上限改自适应。
4. 无障碍复核:Esc、焦点陷阱、`aria-modal`、下载按钮可聚焦。

验收:
- 前端 build/lint/diff 测试全过(6 项 diff 测试不能退化);
- 真实浏览器:分别放大 markdown 报告、JSON 产物、diff、完整投影四种,
  确认渲染正确、sha256 与下载入口仍在;截图存证。

### P4 · 示例瘦身与差异化(问题 6)

放在 P1–P3 之后,因为示例文案要能顺带演示新做好的能力。

改动:
1. 删 `workflows/fix-calculator.yaml`、`workflows/sqlite-checkpoint-analysis.yaml`。
2. 清理引用。**已提前把引用点全部搜出来了**,实施时照单核对,不能只删文件留悬空引用:

   | 位置 | 现状 | 处理 |
   |---|---|---|
   | `scripts/run_demo_fix.py:12` | `spec_from_yaml_file(Path("workflows/fix-calculator.yaml"))`,整个脚本只有 20 行且专为它存在 | 连脚本一起删 |
   | `docs/ARCHITECTURE.md:587` | 目录树里列着 `sqlite-checkpoint-analysis.yaml(示例)` | 改成保留的某张图 |
   | `docs/VERIFICATION.md:425` | 历史证据「fix-calculator 新链路(run 20260817-165452-88f9c4)」 | **保留原文**,它是历史事实;补一句「该示例已于第五轮删除,能力由 code-change-review-approve 承接」 |
   | `web/src/guide/examples.md:3` | 「**八个**随附的图」与下面「六个正式示例」自相矛盾 | 改成六个;正文本身已经只写了 6 条,不必重写 |
   | `README.md:83` | 已写「六个正式示例」 | 不动 |
   | `README.md:95` | 「**8 个**随附示例的 LLM 模型均为空」 | 改成 6 |
   | `skill/SKILL.md:23` | 「已有**八个**正式示例」 | 改成六个并核对括号里的枚举 |
   | `tests/` | 搜过了,**没有任何测试引用这两个文件名** | 无需改测试 |

   注意 `docs/PLAN-v3.md` 里也有历史条目提到这两个示例,同 VERIFICATION 处理:
   历史记录不改写,只在本轮条目里说明删除。
3. `code-change-review-approve.yaml:1-2` 的注释更新——它现在原文写着
   「本图是 fix-calculator 的完整形态」,后者删掉后这句会指向不存在的东西。
4. 两张并行图的 `meta.title` / `description` / `tags` 差异化。
5. 编号注释统一。当前的编号已经是乱的:六张保留图写着「示例 1/6」到「示例 6/6」,
   而 `fix-calculator` 和 `sqlite-checkpoint-analysis` 根本没有编号
   (前者写「真实 coding_agent 演示」,后者写「示例工作流」)。
   删完编号刚好自洽,逐张核对一遍即可。

验收:
- `uv run pytest` 全绿;
- 六个示例逐个 `preview`(零成本)确认仍可加载、仍报未配置模型、图结构无损;
- `grep -ri "fix-calculator\|sqlite-checkpoint-analysis"` 只剩历史证据段落,无活引用;
- 前端示例列表页真实浏览器看一眼,六张卡片描述互不重复。

### P5 · 回边渲染真修(问题 3)

单独一个阶段,因为它必须以真实浏览器截图验收,不能靠单测。

改动:
1. `GraphView.tsx` AtlasNode 补底部 `source` / 顶部 `target` 句柄。
2. 回边显式指定 `sourceHandle` / `targetHandle` + `type: 'smoothstep'`。
3. `styles.css` 补 `.edge-back` 规则(虚线、低饱和描边、标签底色),
   与 `.edge-live` 语义区分。
4. Dagre 布局参数复核:补句柄后节点尺寸契约不能变
   (第四轮 MiniMap 的教训:节点 `width`/`height` 必须与 Dagre 一致,
   否则 MiniMap 的 `nodeHasDimensions` 会再次过滤全部节点)。**这条必须回归验证。**

验收(以浏览器为准,不以测试为准):
- `proposal-review-repair-loop`:能看到 reviewer 单独连回 author 的一条线,
  标签 `repair (≤2 轮)` 挂在这条线上,与 `author→reviewer` 明显分离;
- `code-change-review-approve`:回边同样正确;
- MiniMap 仍显示全部节点(第四轮成果不许退化);
- 深浅两主题各截图存证,进 `docs/VERIFICATION.md`。

### P6 · 文档同步与全量回归

改动:
1. `web/src/guide/models.md`:fallback 有序链语义 + 跨厂商价值 + chips 用法。
2. `web/src/guide/examples.md`:六个示例(删两个、差异化两个)。
3. `skill/SKILL.md`:`node_overrides` 新增 `prompt` / `workdir` 的正确用法与红线
   (强调完整覆盖语义、不可覆盖 consumes)。
4. README 的「当前未完成」清单更新。
5. `docs/VERIFICATION.md`:P1–P5 的证据归档。
6. `docs/PLAN-v3.md`:追加本轮条目(该文件已过 400 行膨胀阈值,只加简短条目 + 指向本文件)。
7. `docs/HANDOFF-next-round.md`:重写为「第五轮已完成 + 下一轮做通讯文件层」。

验收:
- `uv run pytest` 全量绿;
- 前端 build / lint / test:diff 全过;
- **付费真跑一次**:选一个含回边的示例(`proposal-review-repair-loop`),
  先 preview 再真跑,同时验证 prompt 覆盖真的进了投影、回边真的走了第二轮或正常 pass;
  账本 sidecar 哈希全部匹配。先零成本后真钱,不跳步。

---

## 4. 阶段依赖图

```
P1 后端覆盖契约 ──┬─→ P2 前端参数区
                  └─→ (P4 示例可用新能力写文案)
P3 产物工作台 ────────→ (独立,可与 P1/P2 并行)
P4 示例瘦身 ──────────→ P6
P5 回边渲染 ──────────→ P6
```

真正的硬依赖只有 P1 → P2。P3、P5 与其余互不影响,若要并行,P5 放最后做完整回归,
因为它动的是 GraphView 的几何契约,和第四轮 MiniMap 的修复共享同一段代码。

---

## 5. 风险与取舍

| 风险 | 判断 | 对策 |
|---|---|---|
| prompt 完整覆盖让审计变难(看不出与 YAML 的差异) | 真实 | 有效规格快照存全文;摘要记长度与哈希;UI 并排显示继承原文 |
| prompt 覆盖被误用成「改图」 | 真实 | 只作用于本次运行;YAML 不变;指南明确写「要长期生效就用 MCP 写 YAML」 |
| 补句柄导致节点尺寸变化,MiniMap 再次坏掉 | 真实,第四轮踩过 | P5 验收硬性包含「MiniMap 仍显示全部节点」;句柄用绝对定位不参与布局 |
| 删示例留下悬空引用 | 真实 | P4 用全仓库搜索 + `uv run pytest` 抓;删文件与清引用同一次提交 |
| 参数默认值前后端漂移 | 真实 | 有效值只由后端 preview 返回,前端不自算 |
| fallback chips 让「填一个还没配密钥的模型」变得不可能 | 是有意的取舍 | 候选集只来自已配置供应商;需要预填未配置模型时改 YAML |

---

## 6. 与通讯文件层的交界

本轮**不动**产物契约。具体地:
- 不加 `outputs` / `collect_files` 字段;
- 不加 `attachments` 运行输入;
- `consumes` 的可引用名字集合不变。

但本轮的两处工作会被通讯文件层直接复用,顺序上先做本轮是对的:
- P3 的 `ArtifactWorkSpace` 按 media_type 分派——通讯文件层的「每份产物一个页签」
  正好挂在它上面;
- P2 的参数说明模式(后端给有效值、前端只显示)——通讯文件层的产物槽位展示同理。

反过来,通讯文件层里已经论证过的一条决策在本轮生效:
`outputs` / `collect_files` 属于产物契约,**不进** `node_overrides` 白名单;
而 `prompt` / `workdir` 属于运行参数,可以进。这条界线是「会不会影响下游接线」——
影响接线的必须在 YAML 里。

下一轮顺序见 `docs/DESIGN-node-io-files.md` 第 6 节:阶段 A(多产物基座)→
阶段 B(agent 产物回流)→ 阶段 C(人的材料入口)。
