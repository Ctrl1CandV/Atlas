# PLAN · Stage E 五项与 D4 收官——实施设计（定稿 2026-08-27）

> **读者与用法**：本文是用户 2026-08-27 拍板后的**实施方案定稿**。后续每一批开工前以对应章节为合同；与 ROADMAP/HANDOFF 冲突时以本文为准，偏差要在该批次报告里注明并回写本文。执行协议不变：每批仍走「批次报告 → 审查 Agent 放行 → 提交推送 → CI 双绿」。
>
> **决策记录（摘要）**：①C2 真实单价**舍弃**——pricing.json 保持可选的手工补充，不再作为待办或提醒事项；成本控本以结构性约束为主。②D4 裁决=采纳「书面承诺确认 + dry-run 组合警告」，**否决**准入硬拦（选项 B）。③Stage E 五项全量立项，执行顺序 **K → E-1 → E-2A → E-2B → E-3 → E-4冒烟 → E-4完整 → E-5**。

---

## 批次 K · D4 收官小批次（先行）

### K-1 书面承诺落档

把「research/coding_agent 节点**默认永不自动重跑**；retry 是显式选择且必然被预演提示」升格为产品承诺，落到：

| 文件 | 动作 |
|---|---|
| `README.md` / `README.en.md` | 能力边界处各加一句固定句式 |
| `skill/SKILL.md` | agent 字段说明处加同一句式 |
| `web/src/guide/concepts.md` | agent 字段事实节加同义说明 |
| 下一条 CHANGELOG（Changed） | 显著声明默认语义并给出背景事故引用 |

固定句式（双语版同义）：
> research/coding_agent 节点缺省不自动重跑（retry 缺省 0）；显式声明 `retry: N` 后，dry-run 必须出现放大风险警告。

### K-2 dry-run 组合警告（RFC 选项 C 的落地）

- 触发条件：节点 `type ∈ {research, coding_agent}` 且 `retry > 0`。
- 实现点：`atlas/mcp.py::_dry_run_warnings` 追加一类警告（现有降价/未知费率警告同框架）。
- 文案必含三要素：将自动重跑至多 N 次（按"失败即整份重跑、开销翻倍"口径表述）；当前是否存在可执行成本约束（区分「未设 max_cost_usd」「费率未知」两种表述）；替代建议（去掉 retry、用 max_iterations / timeout_s / 更便宜模型的结构性控本）。
- 同图多个命中节点合并为一条列表型警告，避免刷屏。
- 测试：dry-run 输出断言警告存在且含上述要素；**反向验证**（临时移除警告逻辑 → 测试必须转红）。
- 与 spec 层的衔接：`NodeSpec.retry` 缺省 0 已有测试锁定（A9 参数矩阵），补一行注释指向本决议即可。

**验收门**：上面 1–2 全部落地并通过审查；RFC 文件状态从「已裁决」改为「已实施关闭」。Q1 开放问题按裁决记录处理：旧图（未写 retry）按快照冻结语义照常执行，不做追溯性拒绝。

---

## E-1 · LLM web_search（首选项）

### 目标与非目标

- **目标**：让图谱具备可审计、可计费、结构化的联网检索能力——每一次搜索都是账本事件 + write-once 产物。
- **非目标**：不改造 agent CLI 自带的 WebSearch/WebFetch（那仍是 `allow_web` 的域，属 CLI 行为）；不做搜索结果的语义评分；不让模型自主决定"何时搜"（那是 provider tool-calling 形态，明确排除）。

### 表面（YAML）

新增封闭节点类型 `search`：

```yaml
- id: lit
  type: search
  prompt: 围绕任务检索近两年关键文献，最多 3 个查询
  consumes: [task]            # 或上游产出的查询词产物
  backend: tavily             # 封闭枚举，见下
  max_results: 5              # 每个 query 的结果条数上限
  allowed_domains:            # 可选，域名白名单
    - arxiv.org
```

`search` 节点一次执行 = 把 prompt 解析出 ≤N 个查询逐一调用后端，聚合成单一输出产物。节点级 `timeout_s`/`retry`/`on_error` 语义沿用现有守卫体系。

### 模块与后端协议

新建 `atlas/search.py`：

```python
class SearchBackend(Protocol):
    def search(self, query: str, *, max_results: int,
               allowed_domains: list[str]) -> SearchResultList: ...
# SearchResult: {url, title, snippet(截断), published?: str}
```

- 内置实现：`TavilyBackend`（需 `TAVILY_API_KEY`）、`SearxngBackend`（需 base-url）、`NullBackend`（测试注入，仿 FakeProvider 手法）。
- 后端枚举封闭；key 走既有 `config/.env` 链路，缺失即 **fail-closed**（校验期拒绝该 backend 引用）。
- 准入同样走 `_resolve_models` 式预检：search 节点的 backend 在花钱前解析。

### 事件与产物契约

- 新事件 `search_performed`：node/iteration/backend/query/results_count/duration_ms/cost_usd(可空)/result 列表（url+title 截断）。fold 显式 pass + 删事件回归锁（与前几批事件同纪律）。
- 产物 `<id>.output`（role=output，application/json）：结构化结果数组原文落盘；projection 给下游消费时，整块包进 `<untrusted-source>` 围栏并在注入测试里断言围栏有效。
- 成本规则（呼应 C2 舍弃）：**backend 自报费用则如实入账，否则 cost_usd=null 如实显示**；不猜测、不换算。预算侧靠结构性约束（max_results、查询数上限=prompt 要求、节点 timeout_s/retry=0 默认）。

### 与既有机制的组合约束（写明防旁路）

- search 节点**不参与 P7 自动跳过与 P13 合成导入**（非 llm 类型天然被门槛挡住）：结果永远现搜，防陈旧信息被复用——此语义写进 skill 文档。
- 心跳覆盖：每次 query 调用挂 NodeHeartbeat begin/end（窗口粒度=query）。
- 取消消费点：query 边界检查 `cancel_requested()`。

### 测试锚点

注入 NullBackend 断言事件字段与产物 JSON 结构；无 key 校验期拒绝；域名白名单过滤正反例；query 数超 prompt 上限截断＋告警；injection 围栏测试（恶意页面文本不得改变图控制流）；成本 null/自报两条路径。

### 验收门（= ROADMAP §11 对应行）

tool 接口可 fallback；来源完整性（每条结果带 url+title+查询词溯源）；预算如实（null 或实报）；恶意网页注入不影响路由与终态。

工程量：**5–8 人日**。

---

## E-2 · 节点通讯文件（attachments + agent collect）

草案依据 `docs/rfcs/node-io-files.md`，本文裁剪为两期交付 + 一期延后。五铁律全程不可妥协：write-once、SHA-256、路径 no-follow 不穿越、体积上限、投影完整性。

### Phase A —— 运行附件（attachments）

- 表面：Web 发起运行与 MCP `atlas_run_workflow` 新增参数 `attachments: [{name, path}]`（path 为发起机器上的绝对路径文件）。
- 准入：启动时逐个读 bytes → SHA-256 → 按 write-once 复制进 run 目录产物库（合计上限 32 MiB、单件 16 MiB，超限 loud 拒绝）；登记成初始 consumed 产物，人工审批投影照常能看到。
- 安全：路径仅读取一次内容，不在账本保留外部原始路径明文以外的任何链接语义；账本记 `source_path_absent_after_admission=true` 类事实，杜绝悬空引用。
- MCP 契约同步四处锁（工具数仍 8——attachments 是 run 工具的新可选参数而非新工具）。

### Phase B —— agent 多命名收集（collect）

- 表面：`agents.json` 的 runner 配置增加只读清单 `collect: [{pattern, name_prefix, role}]`；`pattern` 为相对 workdir 的 glob（禁绝对路径、禁 `..`，no-follow 遍历），例如 `{pattern: "patches/*.patch", name_prefix: "patch", role: "output"}`。
- 收集时机：CLI 正常结束后扫描，逐文件走 write-once 入库并把 `ArtifactRef` 附加到当次 `node_done.artifacts[]`；下游经 `<node>.<name>` 消费。
- 上限：匹配文件数 ≤20、总字节 ≤64 MiB，超限整节点治理失败（fail-loud，绝不静默截断清单）。
- 与 diff 基线机制并存：collect 不替代 diff，二者可共存于同一节点。

### Phase C —— 跨节点显式附件链路

延后单独立项（依赖 A/B 的实战反馈），不在本计划内承诺日期。

### 测试锚点（两期共用）

穿越样例（`../`、绝对路径、symlink 指向外部）全部拒绝；重复名 write-once 冲突；size cap 双侧；审批投影含附件的正例；MCP 参数契约更新（runs 参数白名单变更要有负例）。

验收门：五铁律各自正反测试 ＋ 发布门/文档四联同步。

工程量：A **3–4 人日**，B **4–5 人日**；Phase C 另评。

---

## E-3 · Release 内置已构建前端

### 设计要点

- 发布工作流新增 dist 构建阶段：release 时跑 `npm ci && npm run build`，产物目录打进 release 资产包（zip 内增加 `web/dist/` 子树），同时独立上传 `dist.zip` 并在 `SHA256SUMS` 中登记；manifest/provenance 增加 `frontend_sha256` 字段，延续 v0.1.0 来源一致性教训（资产必须能对回 commit）。
- Git 继续不跟踪 `web/dist`；sdist 本身也不塞 dist（Python 打包面保持纯净），防止双源漂移。
- 启动查找顺序（`atlas/web.py` 静态目录解析）：CLI 参数 > 环境变量 `ATLAS_WEB_DIST` > 仓库 sibling `web/dist` > 发布包内嵌相对路径；全 miss 时 fail-loud 报错并附构建命令指引，禁止静默空页。
- CI 加一个"发布冒烟 job"：解压资产 → `uv sync` → 起 atlas-web → HTTP 探测首页 200 与一个已知静态资源哈希匹配 manifest。

### 验收门

干净机器（无 Node）解压即可获得可用界面；manifest 中前端哈希与资产一致；老路径（本地开发者自行构建）行为不变。

工程量：**2–3 人日**。

---

## E-4 · 浏览器矩阵 GUI 测试

分两步交付，先冒烟后全量。

### 冒烟子集（先行）

Playwright（chromium 单引擎）起 atlas-web fixture：
1. 建 run → 暂停在 human 门 → **纯键盘**完成一轮 approve（Tab 序可达、Enter 触发、焦点环不断链）；
2. 一张终局卡片基线截图入库；
3. CI 作为 optional workflow 手动/release 触发，不并进主双绿门槛。

### 完整矩阵（随后）

桌面 Chromium × {dark, light} × {100%, 200%} 四组合截图对比（直方图阈值宽松判稳，禁逐像素）＋ Edge/Firefox 冒烟遍历；动画一律 reduce-motion 关闭以保证确定性。

### 铁律

渲染 flake 一律根因修复不许重跑掩盖；截图基线变更必须在 PR 描述里附人眼可见的差异理由。

验收门：键盘流可在零鼠标交互下完成审批全流程；四组合截图稳定通过两连跑。

工程量：冒烟 **2 人日**；完整矩阵另计 **3 人日**。

---

## E-5 · OS 级沙箱调研（末位）

### 产出物（调研型，不含生产功能）

`docs/RESEARCH-os-sandbox.md`：
1. 威胁模型四象限：宿主文件系统写面 / 网络出口 / 凭据与环境变量泄露 / 产物回收完整性；
2. WSL2 spike 结论：workdir 挂载映射、`.env` 凭据传递方式、跨 OS 信号（取消→进程树终止在 Linux namespace 内的等价物）、超时边界；
3. Windows Sandbox（.wsb 自动生成）可行性与授权前提；
4. 容器化（Podman/Docker Desktop）对照表；
5. demo runner 结论文档（形如 feature-flag 原型 `ATLAS_AGENT_SANDBOX=wsl` 是否值得立案）。

### 明确不做

不把现有 `sandbox_runner` 改名为沙箱冒充隔离；调研结论出来前 README/skill 不得出现任何"已隔离"类措辞（既有的诚实性红线）。

验收门：文档评审通过 + spike 仓库内可复现脚本；是否立项真正的隔离 runner 由你基于调研另拍。

工程量：**4–6 人日**（含 spike）。

---

## 附：明确的范围外记录

以下三项本轮裁决**搁置、不排期**（用户原话：价值不明显、不紧急）：16MiB 分段账本治理、熔断状态持久化、max_parallelism。重启条件各自记录在 BACKLOG 对应行；任何一项要复活都必须先回到本文追加一节设计再实施。

## 附：与存量计划的衔接

- ROADMAP §11 表保持原样作为战略索引，本文是其执行细化；冲突时以本文为准。
- `rfcs/agent-retry-budget.md` 决议见该文「决议」节；其 K 小批次先于以上全部项目。
- C2 相关历史措辞已从活文档移除（HANDOFF 常备任务、BACKLOG 提醒行），仅在决策登记表留审计痕迹。
