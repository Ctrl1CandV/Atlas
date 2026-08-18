# Atlas v0.1.0-rc.1 收尾计划(设计定稿)

状态:**实施中（当前阶段 A：Worktree Runner）**。本文档定稿于 2026-08-18,承接 `.zcode/plans/` 中的
rc.1 发布准备计划,覆盖从当前代码状态到"可日常使用的首个版本"的全部剩余设计、
方案与阶段划分。实施过程如需偏离本文,先改本文再改代码。

---

## 1. 目的与范围

rc.1 的发布准备(安全守卫、契约、双语文档、CI、发布链)已完成并全量验证。
本计划补上最后一块能力缺口与配套体验:

1. **coding/research agent 的真实执行后端**(阶段 A)——所有者已确认:首版必须
   具备在隔离副本内创建、删除、修改文件的能力;
2. 配套功能与文档批次(阶段 B);
3. 回归闸门(阶段 C);
4. 真实花销实测(阶段 D,所有者已授权使用 SuperAI/Kiro/Deepseek);
5. 后续批次立项(阶段 E,不在本轮实施)。

不在范围:PyPI/wheel 分发、Linux/macOS 支持、多用户认证、Web 远程暴露。

## 2. 现状基线(2026-08-18)

已实现并验证:

- 后端 223 项测试通过(两轮);前端 13 项测试、lint 0 错误、构建成功;
- 强守卫:成本预留-结算、整图 deadline、审批同步锁 409、run 404、SSE 增量、
  资源上限全部 fail-loud;
- 隔离基础设施:worktree 完整拷贝(拒 symlink/junction)、原目录只读、
  git diff 回收(非零退出码/超限即失败)、产物 sha256 完整性;
- 演示运行 `20260818-172341-e3850e` 端到端验证:agent 在副本改 3 文件 →
  diff 工作区逐行展示 → 人工批准 → done;
- 双语 README、9 章指南、CI(clean-clone/secret 扫描)、release 链
  (sdist+SHA256+SBOM,不含 PyPI);
- sdist 干净树扫描:无真实密钥、无本机绝对路径。

已知缺口:

- **agent 生产执行 fail-closed**(无后端):Windows Sandbox 无官方无人值守 API,
  真 OS 沙箱本轮不可行——阶段 A 以"受控本机 runner"补能力,OS 级强化列入阶段 E;
- 浏览器矩阵遗留:主题切换、键盘调栏、200% 缩放仅有实现+单测覆盖,未实测;
- README 产品截图未落盘;release 归档不含已构建前端(使用者仍需 Node)。

工具链决定(已确认):uv 必须;Git 必须;Node ≥22.12 + npm 仅在需要构建
`web/dist` 时必须(源码 clone 与 sdist 均不含 dist)。纯使用者免 Node 的
release 归档列入阶段 E。

## 3. 阶段总览

| 阶段 | 内容 | 规模 | 依赖 |
|---|---|---|---|
| A | Worktree Runner(agent 执行后端) | L | 无 |
| B1 | 删除运行记录(API+UI) | S | 无 |
| B2 | 首次启动自动初始化 + `atlas init` | S | 无 |
| B3 | allow_web 在节点详情可见(YAML 为真相) | S | A 语义落地后更有意义,可并行 |
| B4 | MCP harness 接入文档 | S | 无 |
| B5 | skill 边界章节更新 | S | A |
| C | 全量回归 + 发布闸门 | M | A、B1–B5 |
| D | 真实花销实测(SuperAI/Kiro/Deepseek) | M | C 全绿 + 所有者预算确认 |
| E | 后续批次(见 §8) | — | D 之后 |

实施顺序:A → B1 → B2 → B3 → B4 → B5 → C → D。E 仅立项不实施。

---

## 4. 阶段 A:Worktree Runner(受控本机执行后端)

### 4.1 动机与安全边界(必须如实写进所有对用户可见的文档)

所有者决策(2026-08-18):首版必须具备文件创建/删除/修改能力;接受
"完整拷贝目标项目到隔离目录,改造只发生在副本内"的边界;Windows Sandbox
强化后端延后。

边界等级声明:**目录隔离 + 进程约束,不是 OS 级沙箱。**

- 消除的风险:原项目目录被写(副本隔离)、无关环境变量与多余密钥泄漏
  (显式 allowlist)、无界网络工具(allow_web 默认关)、无界文件访问
  (cwd 锁定 + allowed_paths)、不可审计的改动(git diff + sha256)。
- 残留的风险(明示):agent 进程以当前用户权限运行,理论上仍可读取宿主
  用户目录;这是 v1 为换取能力接受的取舍,由所有者签字接受。
- 绝不回退的既有红线:runner 未启用/CLI 缺失/供应商不兼容时**显式失败**,
  不静默降级;事件与日志不落密钥。

### 4.2 配置契约

新增 `config/agents.json`(真实文件 gitignored,附 `agents.example.json`):

```json
{
  "runner": "local_cli",
  "cli": {
    "kind": "claude",
    "command": "claude",
    "extra_args": []
  }
}
```

- `runner` 缺省或值为 `"fail_closed"`(缺文件时的默认):保持现状,
  agent 节点在 preview 之前即被拒,错误信息指向本文档;
- `runner: "local_cli"`:启用本机 CLI 后端;
- `cli.kind` v1 仅 `"claude"`(Anthropic `claude` CLI);其他值显式报
  "未知 CLI 后端";
- `command` 可覆盖为绝对路径;`extra_args` 透传(文档标注风险自担)。

供应商兼容性契约:agent 节点的 `model: <provider>:<model_id>` 必须解析到
**暴露 Anthropic 兼容端点**的供应商(claude CLI 走 `ANTHROPIC_BASE_URL`/
密钥环境变量)。供应商配置已含 base_url 与 api_key_env;不兼容供应商
(仅 OpenAI 协议)在 preview 阶段被拒,错误信息说明原因。

### 4.3 执行流程

1. **前置校验**(花钱/落盘之前,复用既有 `validate_executable_spec` 门):
   agents.json 配置合法、CLI 在 PATH、模型解析成功、供应商端点兼容;
2. coding_agent:复用既有 `_prepare_worktree`(完整拷贝含 .git、体积上限、
   symlink 拒绝);research:无 workdir,只读运行;
3. 投影附件(task + 声明的上游产物)写入临时 prompt 文件;
4. 子进程启动:
   - `cwd` = worktree(或 research 时的空临时目录);
   - 环境为**显式 allowlist**:仅注入所选供应商的密钥变量与 base_url、
     必要的 PATH/SYSTEMROOT/TEMP 等系统变量;不继承其余 `os.environ`;
   - `--model <model_id>`;`max_turns` 映射 CLI 对应参数;
   - `allow_web: false`(默认):CLI 工具配置禁用 WebSearch/WebFetch;
     `allow_web: true`:放行;`allowed_paths` 传入 CLI 的权限配置;
   - 超时 = min(节点 timeout_s, 图剩余 deadline),子进程超时即终止并失败;
5. 输出契约(复用既有实现):退出码非零一律失败(stdout 摘要入失败账本);
   stdout 作为执行报告产物落盘;writable 节点回收 git diff(既有
   `_collect_diff`,非零/超限失败);
6. 事件标记:`node_started`/`node_done` 增加 `runner: "local_cli"` 字段,
   NodeDetail 显示"本机受控执行(目录隔离)";成本/预算走既有预留-结算。

### 4.4 需同步更新的五个表面(防文档漂移)

SECURITY.md 边界措辞、README/README.zh-CN"当前不能执行"章节、指南
"沙箱与隐私"章、`skill/SKILL.md` 的 "RC agent boundary" 节、
`workflows/code-change-review-approve.yaml` 头部注释与 meta(去"预演"字样,
改为真实可执行示例)。

### 4.5 验收标准

- 单元/集成测试用桩 CLI(脚本替身)断言:cwd 锁定、环境 allowlist
  (不含未声明密钥/变量)、`--model` 传递、allow_web 工具开关参数、
  非零退出失败、超时终止、diff 回收、research 无 workdir;
- 未启用 runner / CLI 不存在 / 供应商不兼容:preview 前被拒、无 run 目录、
  错误信息可操作;
- 既有 fail-closed 回归全绿(默认路径行为不变);
- 真实 CLI 仅出现在 `@pytest.mark.real_api` 标记的可选测试中;
- 五个文档表面更新后,全仓 grep 无"RC 暂不可真实执行"残留。

---

## 5. 阶段 B:配套功能批次

### B1 删除运行记录

- `DELETE /api/runs/{rid}`:仅允许终态(`done`/`failed`);
  `paused`(待审批)与 `running` 拒绝并说明原因;RUN.lock 存在且未过期时拒绝;
  删除整个 run 目录(artifacts/projections/checkpoint/worktrees);
- UI:运行记录条目加删除按钮 + 确认弹窗;设置区加"清理全部已完成"
  (逐条套用同一 API,汇总结果);
- 测试:终态可删、paused 拒绝、锁保护、目录确实消失、SSE/列表刷新。

### B2 首次启动自动初始化

- `atlas.web`/`atlas.mcp` 启动时:providers/capabilities/pricing/
  models.reference 四个真实 JSON 缺失且对应 example 存在 → 从 example
  复制;`config/.env` 缺失 → 写入注释模板(来自 .env.example);
  **任何已存在文件一律不动**;
- Web 设置页在发生过初始化时显示一次性提示"已从模板初始化默认配置";
- 新增 `atlas init` 控制台命令:执行同样动作并打印结果与下一步指引;
- README/指南 quickstart 去掉 `Copy-Item` 步骤,流程变为
  `uv sync` → `uv run atlas-web` → 浏览器设置页配供应商;
- 测试:空 config 目录启动后文件就位;已有文件不被覆盖;重复启动幂等。

### B3 allow_web 可见性(v1 范围)

- YAML 仍是唯一真相(权限字段**不**进入 node_overrides,维持
  "覆盖不改变权限字段"的既有契约);
- agent 节点详情(NodeDetail)显示 allow_web 当前值与一行解释
  (默认关;开启后 agent 可用网络搜索工具);
- 指南"沙箱与隐私"章补 allow_web 语义说明;
- 作为运行时开关的 per-run 覆盖与 llm 节点联网搜索一并放入阶段 E。

### B4 MCP harness 接入文档

- 新增 `docs/mcp.md`:给出 ZCode、Claude Code、Cursor 三种 harness 的
  现成配置片段,统一命令形态
  `uv --directory <ATLAS_HOME> run atlas-mcp`;
- 指南"MCP 与人工审批"章与 README:把"在另一终端运行"改为
  "或直接配置进你的 harness(见 docs/mcp.md)";
- MCP server 本身不改动。

### B5 skill 更新

- `skill/SKILL.md`:"RC agent boundary" 节改写为 Worktree Runner 事实
  (可执行、边界等级、allow_web、供应商兼容要求);
- 六个示例清单中 code-change 条目去掉"preview-only"标注;
- 保持无本机路径、五工具语义不变。

---

## 6. 阶段 C:回归与发布闸门

1. 全量后端测试 + 前端 test/lint/build;
2. 六工作流零成本 validate/preview(含 agent 节点新前置校验);
3. sdist 重建 + 干净树扫描(secret/绝对路径)+ 导入与子集测试冒烟;
4. clean-clone 冒烟:按 README 从零走 quickstart(验证 B2 生效);
5. 浏览器抽查:agent 节点运行视图、diff 工作区、删除记录交互;
   遗留未实测项(主题/键盘/200%)保持如实标注;
6. 更新 RELEASE_CHECKLIST 与 CHANGELOG;
7. 输出闸门结果;任何失败项修复后重跑对应项。

## 7. 阶段 D:真实花销实测(授权已获)

前置:A–C 全绿,且所有者再次确认预算上限(默认总帽 **$2.00**,
单 run `guards.max_cost_usd` ≤ $0.50,可由所有者上调)。

流程(每条 run 先 preview/dry_run,红线):

1. **示例矩阵**:6 个示例工作流 × SuperAI/Kiro/Deepseek,每示例选该供应商
   最便宜可用模型;记录 run_id、模型、账面成本、供应商报告成本、时长、状态;
2. **agent 实测**:`code-change-review-approve` 以 scratch git 项目为 workdir
   真实执行:验证副本隔离(原目录不动)、diff 正确性、审批恢复、成本入账;
3. **自定义图**:并行三分支 + 有界修复循环 + 人工门 + coding_agent,
   验证汇合语义与循环上限;
4. **失败路径**:坏密钥、超时、预算不足——错误必须清楚且 run 正确终态;
5. 证据:每条运行的记录表 + 关键 UI 截图;问题记入发现清单;
6. 评级输出:每项"达到预期 / 部分达到(附差距) / 未达到(附原因)";
   有问题 → 修复 → 仅复跑受影响项 → 更新本节结论。

## 8. 阶段 E:后续批次(立项不实施)

| 项 | 一句话设计 | 前置 |
|---|---|---|
| llm 节点 web_search | 供应商 tool-calling + 可插拔搜索后端(Brave/Tavily,可选 key,无 key 即功能关闭并明示);结果带来源落产物;条数与成本上限;allow_web 运行时覆盖一并评估 | D 结论 |
| release 归档含已构建前端 | release 工作流归档时附 `web/dist`,纯使用者免 Node;Git 仍不含 dist | 无 |
| 运行报告打包 | run 导出自包含 HTML(时间线+产物+diff+成本+完整性哈希)或 ZIP+manifest;可选 llm 叙述摘要 | D 之后调研出 RFC |
| OS 级沙箱强化 | Windows Sandbox(.wsb 脚本化)或 WSL 后端调研,替换/并列 local_cli | A 落地后 |
| 浏览器矩阵补测 | 主题切换、键盘调栏、200% 缩放实测 | 无 |

## 9. 全程红线(不变)

1. 不读取、不打印、不提交 `config/.env` 与真实密钥;文档与事件不落密钥;
2. 任何真实花销运行前必须 preview/dry_run;
3. 报告诚实:未验证不写"已通过";
4. 实施期间不替所有者执行 git init 之外的任何提交/推送(GitHub 上传由
   所有者自行完成);
5. 所有 fail-loud 契约(非零退出、diff 失败、超限、未知费率)不放松。

## 10. 决策记录

| 日期 | 决策 | 依据 |
|---|---|---|
| 2026-08-18 | rc.1 代码基线验收(223+13 测试,发布链就绪) | 本文档 §2 |
| 2026-08-18 | 首版必须有文件增删改能力;接受"隔离副本+进程约束"边界;OS 沙箱延后 | 所有者明确授权 |
| 2026-08-18 | 首次启动自动初始化配置,quickstart 去 Copy-Item | 所有者要求简化 |
| 2026-08-18 | llm 节点联网搜索放后续批次;v1 仅落实 agent 的 allow_web 语义与可见性 | 所有者同意分两步 |
| 2026-08-18 | MCP 以 harness 配置片段接入,不改 server | 所有者建议 |
| 2026-08-18 | 授权 SuperAI/Kiro/Deepseek 真实花销实测,排在全部修正之后 | 所有者授权 |

## 11. 实施记录

### 2026-08-18 · 阶段 A · 实施启动与 CLI 契约核对
- 实际改动：阶段状态进入实施中；核对本机 Claude Code 2.1.228 的非交互参数与现有 agent/成本/续跑接口。
- 验证证据：`claude --help` 显示 `-p`、`--model`、`--output-format json`、工具权限与 `--max-budget-usd`，但没有 `--max-turns`。
- 计划偏差：`max_turns` 保留为 Atlas 规格与审计字段，当前 Claude CLI 不伪造不存在的硬参数；实际硬边界由节点/整图 deadline 与可用时的 CLI 预算承担。`allow_web=false` 仅禁用 WebSearch/WebFetch，不宣称阻断 Bash 发起的网络；`extra_args` 改为安全白名单/危险参数拒绝，不能无条件透传。这些均不改变“受控本机执行、非 OS 沙箱”的阶段目标。
- 遗留问题：真实供应商/CLI 调用只在阶段 D、预算再次确认并完成 dry-run 后执行；阶段 A 只运行桩 CLI。

### 2026-08-18 · 阶段 B4 · MCP harness 接入文档
- 实际改动：新增 `docs/mcp.md`，提供 ZCode、Cursor、Claude Code 的 stdio 配置示例；README 与内置 quickstart/MCP 指南改为优先说明可配置进 harness，同时保留手动启动方式；同步把 `atlas/mcp.py` 与 `tests/test_mcp.py` 的陈旧“四工具”注释改为五工具，未改运行行为。
- 验证证据：Python `json.loads` 成功解析文档内 3 个 JSON 示例；公开文档本机绝对路径扫描无命中；目标文档无“另一终端运行”旧措辞；`uv --directory <ATLAS_HOME> run pytest tests/test_mcp.py` 通过（6 passed）。
- 计划偏差：无；B4 独立实施，未推进仍处于阶段 A 的全局当前阶段。
- 遗留问题：无。

### 2026-08-18 · 阶段 A 与 B1–B5 · 实际落地及既有验证补录
- 实际改动：阶段 A 已落地显式启用的 Claude `local_cli`、同用户受控进程、最小子进程环境、worktree 副本、超时与预算接入；B1–B5 已分别落地终态运行删除与 UI 清理、原子 create-if-absent 初始化及通知、NodeDetail 的 YAML `allow_web` 可见性、三类 MCP harness 文档和 skill/公开边界说明。
- 验证证据：无付费后端套件既有结果为 `uv run pytest` 254 passed、5 个 `real_api` deselected，阶段 A/B 定向测试 95 passed；前端套件扩充后既有结果为 16 passed，lint 0 错误，生产构建成功（仅 chunk 大小警告）；桩 CLI、Windows 子进程终止和 MCP stdout 静默探针均已覆盖，未调用真实模型。
- 计划偏差：实现范围覆盖 A 与 B1–B5，但 REVIEW-001 发现结构性安全与一致性缺口，因此未把全局阶段推进到 C。
- 遗留问题：以 REVIEW-001 blocker 和已接受 REV-001 为准；真实付费与发布闸门继续暂停。

### 2026-08-18 · REVIEW-001 · blocker 确认与修复回流
- 实际改动：独立复盘确认 3 个致命问题（可写 Git 元数据绕过 diff、Git clean filter/父环境执行链、未知费用释放后可重复占用）和 4 个重要问题（baseline 未冻结、运行/删除锁竞态、MCP/Web 预检不一致、notice 仅进程内同步），裁决不通过并回流 developer/planner。
- 验证证据：commit 绕过、恶意 clean filter 与宿主 sentinel、未知费用重复预算、MCP/Web 分歧、跨进程删除竞态均有可复现证据；详细命令与结果保留在下方 REVIEW-001 原文。
- 计划偏差：原阶段 A/B 实现不能直接进入 C；需先实施已接受 REV-001，并以攻击回归及 REVIEW-002 关闭 blocker。
- 遗留问题：REVIEW-001 尚未由独立 REVIEW-002 正式关闭；阶段 C/D 与真实付费保持暂停。

### 2026-08-18 · REV-001 修复 · 安全状态与跨入口一致性里程碑
- 实际改动：diff 采集改为比较冻结 baseline 与 agent 结果的普通文件字节清单，生成完整文本 unified diff，不执行 Git add/filter/hook/attributes/textconv/external diff，二进制变更 fail-loud，审批绑定 `baseline_digest`/`result_digest`/`patch_digest`；运行生命周期使用稳定 OS run lock 与 tombstone 删除；预算改为持久 reservation 和未知费用保守占用；初始化 notice 改为跨进程队列/CAS；MCP/Web 预检与错误分类已合流。
- 验证证据：针对 commit/attributes/filter/hook、baseline 篡改与非法文件、未知费用 retry/并发/重放、run/delete/approve 竞争、notice 旧 ack/新事件及 MCP/Web 一致性的回归已纳入修复验证面；本里程碑不替代最终全量无付费套件、sdist clean smoke 与 REVIEW-002。
- 计划偏差：按已接受 REV-001 实施，不改变“同用户进程不是 OS 沙箱”的既定边界；`PreparedExecution` 冻结与消费链路仍在施工并待最终验证。
- 遗留问题：完成 `PreparedExecution`、最终攻击回归、全量后端/前端与发布闸门验证后方可申请 REVIEW-002；真实付费仍暂停。

## 12. 独立审查

### REVIEW-001 · 2026-08-18 · 实施复盘（reviewer）
- 审查对象：实施结果（代码 + PLAN 阶段 A 与 B1–B5 当前落地）
- 产出复述：当前实现新增显式启用的 Claude `local_cli`、同用户进程与环境白名单、worktree 复制和 Git diff 回收、CLI 预算/超时；Web/MCP 在运行前具体化模型覆盖并预检；B1 提供终态运行删除及 UI 清理，B2 提供原子 create-if-absent 初始化、stdio 静默和一次性提示，B3 在 NodeDetail 展示 YAML `allow_web`，B4/B5 更新 harness 与 agent 边界文档。agent 的 `model/max_turns/timeout_s/retry/prompt/workdir` 可覆盖，`allow_web/writable/allowed_paths/拓扑/consumes` 保持封闭。
- 四维结果：正确性不通过——完整 diff、凭据隔离、预算上限和跨入口预检存在可复现违约；完整性不通过——实现记录未登记 A 完成及 B1/B2/B3/B5，且根目录不是 Git 仓库，无法以干净 Git diff 归因本轮改动；回归性有条件——现有无付费套件全绿，但缺少下述攻击场景；优越性不通过——Git 后处理信任了 agent 可写的 `.git`，预算把“未知实际费用”等价成零，锁与初始化通知仅部分覆盖跨进程竞争。
- 已验证：未读取 `config/.env`、未调用真实模型。`uv run pytest`：254 passed、5 个 `real_api` deselected；阶段 A/B 定向测试：95 passed；`npm --prefix web test`：14 passed；lint 0 错误；build 成功（仅有 chunk 大小警告）。桩 CLI 验证环境 allowlist、模型/工具参数和费用解析；Windows 超时桩确认子孙进程被终止；`python -m atlas.mcp` 启动探针返回 0 且 stdout 为 0 字节。红队复现：agent 在副本内提交后 `_collect_diff` 返回 0 字节、0 文件；agent 写入 Git clean filter 后，`git add -A` 以 Atlas 父进程完整环境执行并读到合成宿主秘密；一次未知费用结算后两次会话均获得完整 `$0.50`；MCP dry-run 在同一供应商预检错误下返回成功而 Web preview 返回 400；另一应用实例可在终态事件已写但 checkpoint 尚占用时进入 DELETE，Windows 抛未处理 `PermissionError`。
- 问题汇总：致命① `atlas/nodes/agent.py` 以当前可写 `.git/HEAD` 为 diff 基线，agent 可自行 commit 后得到空“完整 diff”，审批材料可被绕过；致命② `_collect_diff` 的 `git add -A` 会执行 agent 可配置的 Git clean filter，且未清空父进程环境，突破 CLI 凭据 allowlist并形成任意同用户代码执行链；致命③ `actual_cost=None` 会释放全部预留且不计 spent，retry/后续 agent 可再次获得完整预算，`max_cost_usd` 可累计超支。重要① clean-workdir 只在预检时检查，复制前不冻结 HEAD/索引/状态，外部并发改动可混入并被归因给 agent；重要②初始 `execute_graph` 不持 `RUN.lock`，B1 跨进程删除与执行收尾不互斥，且删除文件占用错误未转成受控 409/5xx；重要③ MCP dry-run 只做 agent runner 预检，LLM-only 图不走 Web preview 使用的 registry/`validate_executable_spec`，preview-run-approve-resume 预检不一致；重要④ B2 active 文件跨进程 create-if-absent 合格且 MCP stdout 未污染，但一次性 notice 的 `_NOTICE_LOCK` 仅限进程内，Web/MCP 并发启动或确认可丢失/误删更新后的提示。次要① `atlas/nodes/agent.py` 顶部仍称“只允许 Windows 沙箱 runner/成本记 null”，与 local_cli 事实漂移；次要② PLAN 实施记录与实际 A、B1–B5 文件状态不一致；B4 三份 JSON 仅验证可解析，未在三个真实 harness 中接入实测。
- 决策路径：B。问题共享“把同用户 agent 可变状态当成可信控制面、跨入口/跨进程安全状态未统一”的结构性根因，不能作为少量独立小修关闭。按本轮明确约束不创建或修改其他文件；后继方案应由 planner 在允许新增 PLAN 后落盘，至少冻结不可变 Git 基线并禁用 filters/hooks/父环境继承、定义未知费用保守结算、统一 Web/MCP/approve/resume 预检、让初始执行持有跨进程 run 锁并为 notice 建立跨进程 CAS。
- 最终裁决：不通过。关闭条件：上述 3 个致命与 4 个重要问题均修复并增加攻击回归；重跑完整后端/前端检查；以真实 Git 仓库的干净 diff 给出实施归因；补齐 A、B1–B5 实施记录后，再进行独立 REVIEW-002。阶段 C/D，尤其任何真实花销测试，在此之前不得开始。
- 回流：developer 修复；planner 评估路径 B 二次方案；memory-keeper 核验实施记录、文档事实与后继关系。

### REVIEW-002 · 2026-08-18 · 实施复盘（reviewer）
- 审查对象：实施结果（代码 + 已接受 REV-001 与 REVIEW-001 关闭条件）
- 产出复述：当前实现已把 coding_agent 改为冻结 baseline/result 普通文件字节清单并生成完整文本 unified diff，采集路径不调用 Git；加入稳定 `.locks` OS 锁、同卷 tombstone 删除、持久 reservation 重放、冻结凭据的 PreparedExecution、notice 队列/journal/CAS，以及 Web/MCP 共用的 registry/runner 预检和 execution fingerprint。锁、notice、核心攻击回归与前端闸门实测通过，但审批对象、冻结时 Git 清洁性、凭据漂移、MCP 输入边界和成本 unknown 计数仍有可复现缺口。
- 四维结果：正确性不通过——human 审批记录未绑定三摘要，冻结前未立即复核 HEAD/index/clean，credential value 漂移不改变 execution_sha256，MCP 与 Web 对 task 边界不一致，uncapped unknown attempt 被重复计数；完整性不通过——REVIEW-001 的完整审批、冻结时基线复核、跨入口一致和全量回归条件未关闭，项目仍无 `docs/SPEC.md` 且根目录不是 Git 仓库，无法给出要求的干净 Git diff 归因；回归性不通过——完整后端套件 4 项 local_cli 测试失败；优越性有条件——byte manifest、稳定锁/tombstone、notice journal/CAS 与非秘密 descriptor 的结构明显优于 REVIEW-001 版本，但安全状态尚未端到端绑定。
- 已验证：全程未读取 `config/.env`、未调用真实供应商。REV-001 定向套件 `uv run pytest tests/test_agent_diff_security.py tests/test_run_locking.py tests/test_costs_breaker.py tests/test_cost_reporting.py tests/test_prepared_execution.py tests/test_config_init.py tests/test_config_api.py` 为 75 passed、1 skipped；完整 `uv run pytest` 为 305 passed、4 failed、1 skipped、5 real_api deselected，失败均在 `tests/test_local_cli_runner.py` 的直接 runner helper 未冻结凭据；前端 test 16 passed、lint 0 错误、build 成功（仅 chunk 警告）；`uv lock --check` 通过；sdist 构建成功且 160 个条目中未发现 active config、runs、`.git`、`.zcode`、cache 或 `web/dist`。独立复现：preflight 后写入未提交文件仍被 `_freeze_baseline` 接受；一条无 cap unknown agent 调用重放为 unknown_count=2；Web 对空/超大 task 返回 400/413，而 MCP 分别执行成功/创建 failed run；凭据变化后 execution_sha256 不变但 descriptor 不含密钥；coding→human 的 gate projection 与 `run_approval` 均不含 baseline/result/patch digest。
- 问题汇总：致命① `atlas/nodes/agent.py` 只在 runner preflight 期间由 `atlas/nodes/local_cli.py` 检查 clean Git，真正 `_freeze_baseline` 前不复核 HEAD/index/status，窗口内外部未提交改动会被纳入 baseline 并错误归因给 agent；致命②三摘要仅存在于 coding `node_done`/diff artifact metadata，`build_projection` 只内联 artifact 字节，human gate 看不到摘要，`run_approval` 也只记录 decision/comment，未实现“审批对象是 `{baseline_digest,result_digest,patch_digest}`”。重要① LocalCliRunner 冻结凭据且 descriptor 无秘密是正确方向，但 credential value 变化不改变 execution_sha256，preview/dry-run 的 expected fingerprint 可在凭据漂移后仍通过；重要② Web 在 run 分配前拒绝空/超限 task，MCP 不做同一校验，超限时还创建失败 run，跨入口契约仍不一致；重要③ uncapped agent 同时写无 reservation_id 的 `cost_unknown` 与 `cost_settled(cost_unknown=true)`，重放把一次未知调用计为两次，Web/MCP 的 unknown count 失真；重要④完整后端回归 4 项失败，阶段 C 闸门不绿。未发现新的 lock/tombstone、notice queue/journal/CAS 或 Git filter/agent commit 绕过；commit/filter 攻击回归通过。
- 决策路径：B。存在审批完整性与基线归因两个致命问题，并伴随 fingerprint、入口一致性和持久账本的共享状态契约缺口；不是少量独立小修。已接受 REV-001 的方案方向仍适用，应由 developer 完成其未落地的不变量并补攻击回归，planner 决定是否需要新的后继修订；本轮按任务约束仅追加 REVIEW-002，不修改生产代码或新增方案文件。
- 最终裁决：有条件、不通过（REVIEW-002 未通过）。关闭条件：①在真正冻结前原子化复核源 HEAD/index/clean，并增加 preflight→freeze 竞态攻击测试；②将三摘要带入 human 可见审批材料、审批请求与持久 `run_approval`，锁内复核其与待批 diff artifact/event 一致；③定义 credential rotation 的无秘密指纹/代际身份并证明 preview→run、approve/resume 漂移拒绝；④Web/MCP 共用 task 非空/大小校验且拒绝前不创建 run；⑤每个 agent attempt 的 unknown 只计一次并保持 capped/uncapped、崩溃重放及 Web/MCP 汇总一致；⑥修复 4 项 local_cli 测试并重跑完整无付费后端、前端和攻击套件全绿；⑦在真实 Git 仓库中提供干净 diff 归因。以上关闭并经独立复核前，阶段 C/D、发布和真实花销继续暂停。
- 回流：developer 修复；planner 核对 REV-001 未完成项并决定是否追加修订；memory-keeper 核验关闭证据与计划状态。

## 13. 计划修订

### REV-001 提案 · 2026-08-18
- 作者：project-planner
- 原因与证据：REVIEW-001 复现了 agent 在副本内 commit 后审批 Diff 为空、Git clean filter 继承宿主环境执行、未知费用重复获得完整预算，以及运行删除、预检和初始化通知的跨进程不一致。方案级对抗进一步指出随机目录本身不是同用户安全边界，审批必须绑定不可变字节摘要，锁必须使用 run 目录外的稳定 OS 文件锁。
- 覆盖目标：`原始计划 / 阶段 A / 执行流程与验收`、`原始计划 / 阶段 B1/B2`、`原始计划 / 阶段 C`。
- 替换内容：
  1. **冻结字节基线而非信任 agent 的 Git 元数据。** coding 节点真正执行前立即复核源仓库 HEAD 与 clean 状态，复制一次随机命名的 frozen baseline；生成排除 `.git` 与固定缓存的逐文件 SHA-256/大小清单及 tree digest，并在每次 retry 和最终比较前重验。所有 retry 只从 frozen baseline 派生，不再重新读取源 workdir。源仓库此后变化不影响本次结果，UI/事件记录源 HEAD、baseline digest、result digest 与 patch digest。
  2. **Diff 采集不执行仓库代码。** 不再对 agent 工作树运行 `git add`、clean/smudge、hooks、attributes、textconv 或外部 diff。由 Atlas 安全枚举器在 agent 进程树结束后比较 baseline/result 的普通文件字节，拒绝 reparse point、hardlink、ADS/冒号、控制字符、尾随点/空格、设备名、大小写冲突与嵌套 `.git`；以固定资源上限生成 unified text diff 与 binary change 标记。审批对象是 `{baseline_digest,result_digest,patch_digest}`，agent 修改或提交自身 `.git` 不影响比较结果。
  3. **同用户边界如实限定。** controller 内存中的预期 digest 和进程生命周期用于检测 agent 对 frozen baseline 的篡改；Windows runner 必须确保 CLI 进程树结束后才比较。由于没有 OS 沙箱，同用户恶意进程理论上仍可主动攻击 Atlas 控制器或其他宿主文件，此风险继续明示，不把目录随机化冒充强隔离。
  4. **未知费用保守占用。** 每次 agent attempt 使用唯一 reservation；若 `total_cost_usd` 缺失、JSON 损坏、超时或调用后失败，在有 cap 时将本次 reservation 全额转为 `accounted_cost_usd`，不释放重用。已知费用按实际结算，超过 cap 时先持久记录再失败。事件与 Web/MCP 分开显示 known actual、accounted/guarded、unknown count；存在 unknown 时不得把 known 小计标作完整实际总额。无 cap 时仍记录 unknown，但不虚构金额。
  5. **稳定跨进程运行锁。** 在 `runs/.locks/<rid>.lock` 使用稳定、持久、不按 TTL 抢占的 OS 文件锁和进程内互斥；先取得锁再创建/发布 run 目录，初始执行、resume、approve、delete 共用。执行释放前关闭 checkpoint/事件句柄并结束子进程。删除持锁后同卷 rename 为 tombstone，再 no-follow 清理；sharing violation 返回可重试 409/423，其他删除错误为受控 5xx。
  6. **统一 prepared preflight。** Web preview/run、MCP dry-run/run、approve/resume 共享同一预检函数与错误分类；preview/dry-run 每次零成本重检，run 直接消费当次冻结的 registry/runner/provider 解析结果。MCP dry-run 也校验 LLM registry。resume/approve 对剩余 agent 重新验证当前运行后端，配置漂移显式拒绝，不静默换端点。
  7. **初始化通知采用跨进程队列/CAS。** active 配置的原子 create-if-absent 保留；notice 使用稳定 OS 文件锁、事件队列和原子 replace。read 为 at-least-once；ack 在锁内按 event_id 删除对应事件并保留后来事件。初始化先写 journal 再创建 active 文件，崩溃恢复可补发通知；不承诺浏览器 exactly-once 展示。
  8. **重新验证。** 增加 commit 绕过、恶意 attributes/filter/hook、宿主环境 sentinel、baseline 篡改、路径/reparse/hardlink、未知费用 retry/并行/崩溃重放、run/delete/approve 双向竞争、MCP/Web 预检一致、notice 多进程与旧 ack 新事件攻击测试；全量无付费测试、前端 test/lint/build、sdist clean smoke 与 REVIEW-002 全部通过前，阶段 D 和发布继续暂停。
- 影响范围：`atlas/nodes/agent.py`、`atlas/nodes/local_cli.py`、`atlas/costs.py`、`atlas/engine.py`、`atlas/web.py`、`atlas/mcp.py`、`atlas/config_init.py`、事件/API/UI 汇总、相关测试与安全文档。
- 重新验证要求：关闭 REVIEW-001 的 3 个致命与 4 个重要问题；以攻击回归证明，不以普通 happy-path 测试替代；不调用真实模型。

### REV-001 裁决 · 2026-08-18
- 结果：已接受
- 裁决者：project-planner
- 理由：REVIEW-001 有可复现证据，原实现无法满足完整审批、环境隔离、成本上限与跨进程一致性。对抗复核要求的 OS 强隔离超出所有者已接受的 v1 边界，因此不把“抵抗任意同用户恶意进程”加入承诺；其余可证明的不变量全部纳入本修订。
- 独立证据：REVIEW-001；方案对抗复核（同用户边界、byte manifest、预算状态机、稳定 OS 锁、notice CAS）。
