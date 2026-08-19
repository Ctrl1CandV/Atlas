# Atlas v0.1.0-rc.1 收尾计划（当前事实与发布闸门）

状态：**阶段 A、B、C 与精简批次一已于 2026-08-19 在本地收口；首轮 reviewer
发现的成本、SSE、YAML、resume 准入与文档 blocker 均已修复，最终本地闸门与
REVIEW-004 独立复核通过。** 本文最初定稿于 2026-08-18，并于 2026-08-19 按当前源码重新审计。
剩余发布工作是远端 CI、tag、产物上传、provenance 与阶段 D；阶段 D 仍须
所有者重新确认时点和预算，阶段 E 不在本轮。
实施过程如需偏离本文，先改本文再改代码。

---

## 1. 目的与范围

本计划记录 rc.1 从既有工作流引擎到可日常使用版本的最后交付。2026-08-19
复核确认：A 的生产 agent 后端与安全修订、B1–B5 的功能主体已经落地；C 与
精简批次一的本地 preview、clean-init、前端测试、sdist smoke 和浏览器验收已经收口，
首轮 reviewer 指出的成本、SSE、YAML、resume 准入和文档 blocker 已完成修复，
最终本地闸门与 REVIEW-004 独立复核通过；这不代表远端发布已经完成。
本文补齐以下能力与证据:

1. **coding/research agent 的真实执行后端**(阶段 A)——所有者已确认:首版必须
   具备在隔离副本内创建、删除、修改文件的能力;
2. 配套功能与文档批次(阶段 B);
3. 回归闸门(阶段 C);
4. 真实花销实测（阶段 D，授权保留但执行时点已推迟）；
5. 后续批次立项（阶段 E，不在本轮实施）。

不在范围:PyPI/wheel 分发、Linux/macOS 支持、多用户认证、Web 远程暴露。

## 2. 当前基线（2026-08-19 复核）

已实现并已有无付费证据：

- 显式启用的 Claude `local_cli` 生产 runner；缺配置、CLI、兼容端点、模型或凭据时
  在创建 run 前 fail-closed；
- coding agent 冻结源基线，在副本内执行，并比较 baseline/result 的普通文件字节
  manifest 生成完整文本 unified diff；采集不执行 Git filter/hook/attributes；
- `PreparedExecution` 冻结 spec、后端和非秘密凭据代际身份；run/approve/resume/delete
  使用稳定 `.locks` OS 锁；审批绑定 baseline/result/patch 三摘要；
- agent 成本在有 `max_cost_usd` 时采用持久 reservation 与保守未知费用结算；无 cap
  不虚构 reservation 或金额；初始化通知采用跨进程 journal/queue/CAS；Web 与 MCP 共用
  task 和执行预检契约；
- 最终本地闸门：Python 3.14.6 与隔离 Python 3.12.9 均为 **425 passed、1 skipped、
  5 real_api deselected**；前端 **22 passed**、lint 0、build 成功；六工作流和 clean-init
  闸门全绿；最终 sdist **173 条目、0 发现**，Python 3.12 离线安装与六 MCP 工具探针通过。

当前发布缺口：

- 首轮 reviewer blocker 已全部修复并通过最终本地闸门与 REVIEW-004 独立复核；
- 远端 GitHub Actions 尚未运行，tag 尚未创建，发布产物、校验和、SBOM 与 provenance
  尚未上传/验证，下载后 smoke 也待发布后执行；
- 阶段 D 真实供应商调用尚未授权启动；
- 浏览器矩阵遗留：主题切换、键盘调栏、200% 缩放仍未实测；
- README 产品截图未落盘；release 归档不含已构建前端（使用者仍需 Node）。

工具链决定(已确认):uv 必须;Git 必须;Node ≥22.12 + npm 仅在需要构建
`web/dist` 时必须(源码 clone 与 sdist 均不含 dist)。纯使用者免 Node 的
release 归档列入阶段 E。

## 3. 阶段总览

| 阶段 | 内容 | 规模 | 依赖 |
|---|---|---|---|
| A | Worktree Runner（agent 执行后端） | L | **实现完成** |
| B1 | 删除运行记录（API+UI） | S | **实现完成，C 中补交互证据** |
| B2 | 首次启动自动初始化 + `atlas init` | S | **实现完成，C 中补 clean-checkout** |
| B3 | allow_web 在节点详情可见（YAML 为真相） | S | **完成** |
| B4 | MCP harness 接入文档 | S | **完成** |
| B5 | skill 边界章节更新 | S | **完成** |
| C | 全量回归 + 发布闸门 | M | **本地收口且 REVIEW-004 通过；远端 CI 待 push** |
| D | 真实花销实测（SuperAI/Kiro/Deepseek） | M | **精简批次一已本地全绿；待所有者重新确认时点与预算** |
| E | 后续批次（见 §8） | — | 本轮不实施 |

当前实施顺序：C 与精简批次一（P0min + P1 + P6）已本地收口且独立复核通过 →
运行远端 CI，并完成 tag/upload/provenance → 所有者重新确认后执行 D → 发布 v0.1.0 →
保留的发布后优化与阶段 E 并轨。D/E 未在本轮执行。

---

## 4. 阶段 A:Worktree Runner(受控本机执行后端)

### 4.1 动机与安全边界(必须如实写进所有对用户可见的文档)

所有者决策(2026-08-18):首版必须具备文件创建/删除/修改能力;接受
"完整拷贝目标项目到隔离目录,改造只发生在副本内"的边界;Windows Sandbox
强化后端延后。

边界等级声明:**目录隔离 + 进程约束,不是 OS 级沙箱。**

- 降低的风险：原项目目录被写（副本隔离）、无关环境变量与多余密钥泄漏
  （显式 allowlist）、默认开放 WebSearch/WebFetch（`allow_web` 默认关）、
  不可审计的改动（冻结 baseline/result 普通文件字节 manifest + SHA-256）。
  `workdir`、`allowed_paths` 与工具清单都不是 OS 访问控制边界。
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
- `command` 可覆盖为绝对路径；`extra_args` 仅允许经过审计的安全白名单
  （当前仅 `--verbose`），会改变模型、权限、输出格式或执行边界的参数一律拒绝。

供应商兼容性契约:agent 节点的 `model: <provider>:<model_id>` 必须解析到
**暴露 Anthropic 兼容端点**的供应商(claude CLI 走 `ANTHROPIC_BASE_URL`/
密钥环境变量)。供应商配置已含 base_url 与 api_key_env;不兼容供应商
(仅 OpenAI 协议)在 preview 阶段被拒,错误信息说明原因。

### 4.3 执行流程

1. **前置校验**(花钱/落盘之前,复用既有 `validate_executable_spec` 门):
   agents.json 配置合法、CLI 在 PATH、模型解析成功、供应商端点兼容;
2. writable coding agent 在真正执行前复核源 HEAD/index/clean，冻结一次 baseline，
   每次 retry 只从该 baseline 派生副本；普通文件枚举拒绝 reparse point、hardlink、
   ADS/设备名/大小写冲突等危险形态；research 无用户 workdir，以只读工具运行；
3. 投影附件(task + 声明的上游产物)写入临时 prompt 文件;
4. 子进程启动:
   - `cwd` = worktree(或 research 时的空临时目录);
   - 环境为**显式 allowlist**:仅注入所选供应商的密钥变量与 base_url、
     必要的 PATH/SYSTEMROOT/TEMP 等系统变量;不继承其余 `os.environ`;
   - `--model <model_id>`；当前 Claude CLI 没有硬 `max_turns` 参数，该字段仅作为
     已校验规格元数据，硬边界来自节点/整图 deadline 与可用预算；
   - `allow_web: false`（默认）不授予 WebSearch/WebFetch；`true` 才授予；
     `allowed_paths` 仅允许 research 或 `writable: false` 的 coding agent 使用，
     通过 `--add-dir` 提供附加目录，但它不是只读或安全隔离边界；
   - 超时 = min(节点 timeout_s, 图剩余 deadline),子进程超时即终止并失败;
5. 输出契约：退出码非零一律失败（脱敏 stdout/stderr 摘要进入失败账本）；
   stdout JSON 中的报告作为产物落盘；writable 节点比较冻结 baseline/result 的
   普通文件字节 manifest，生成完整文本 unified diff；二进制与资源超限 fail-loud；
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

- `DELETE /api/runs/{rid}`：仅允许终态（`done`/`failed`）；`paused` 与
  `running` 拒绝；所有运行操作共用 `runs/.locks/<rid>.lock` 的稳定 OS 锁，
  绝不按 mtime/TTL 抢占；删除先同卷 rename 为 tombstone，再 no-follow 清理；
- UI:运行记录条目加删除按钮 + 确认弹窗;设置区加"清理全部已完成"
  (逐条套用同一 API,汇总结果);
- 测试:终态可删、paused 拒绝、锁保护、目录确实消失、SSE/列表刷新。

### B2 首次启动自动初始化

- `atlas.web`/`atlas.mcp`/`atlas init` 启动时：providers、models.reference、
  capabilities、pricing、agents 五个 JSON 与 `.env` 缺失且模板存在时，按模板
  原子 create-if-absent；`agents.json` 默认 `runner: fail_closed`；
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
- 保持无本机路径；当时为五工具语义（历史记录）。当前 P1 新增 `atlas_resume_run` 后为六工具。

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

## 7. 阶段 D：真实花销实测（授权保留，执行已推迟）

所有者于 2026-08-19 决定并同日收窄：阶段 D 在 `PLAN-benchmark-optimizations.md`
**精简批次一（P0min + P1 + P6，约 13–20 人日：最小 LLM 预留持久化、崩溃恢复/
`interrupted` 产品入口、YAML 语义错误行列）全绿后**启动；P2/P3/P4/P7/P9/P10/
P11/P13 与阶段 E 一样延后到 v0.1.0 发布后按需实施，P5/P8/P12/P14 已移除。
启动 D 时仍需所有者重新确认时点和预算，
并坚持每条真实运行先 preview/dry-run。历史建议帽为总计 **$2.00**、单 run
`guards.max_cost_usd` ≤ $0.50；它不是本轮授权。

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
| llm 节点 web_search | 供应商 tool-calling + 可插拔搜索后端；结果带来源落产物；条数与成本上限；allow_web 运行时覆盖一并评估 | benchmark 核心阶段与 D 结论 |
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
| 2026-08-18 | 授权 SuperAI/Kiro/Deepseek 真实花销实测，排在全部修正之后 | 所有者授权 |
| 2026-08-19 | 阶段 D 推迟到 benchmark 计划的核心能力完成之后；本轮不执行 D/E | 所有者决定 |
| 2026-08-19 | 收窄 benchmark：D 前只实施 P0min + P1 + P6（约 13–20 人日）；P2/P3/P4/P7/P9/P10/P11/P13 发布后按需实施；P5/P8/P12/P14 移除 | 所有者决定 |
| 2026-08-19 | `allowed_paths` 仅允许不可写 agent；`--add-dir` 不冒充只读边界 | 当前 Claude CLI 契约与安全复核 |

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

### REVIEW-003 · 2026-08-18 · 实施复盘（reviewer）
- 审查对象：实施结果（代码 + REVIEW-002 关闭条件 + 修复 commit `2c30560`/`5cd4c5d`）。
- 产出复述：暂停审批新增 `parse_projection_evidence`（`atlas/integrity.py`）从哈希锚定投影字节解析 diff 三摘要，`_verify_approval_material`（`atlas/engine.py`）在锁内复核投影文件哈希、消费产物哈希，并把账本 diff metadata 与投影证据逐项交叉验证后才写 `run_approval`；OpenAI/Anthropic descriptor 增加非秘密 `credentialRevision`（`atlas/adapters.py` `_llm_credential_revision`，域分隔 SHA-256，不含密钥值），密钥轮换同时改变 `backend_sha256` 与 `execution_sha256`；manifest diff 补发 `diff --git` 惯例头（`atlas/nodes/agent.py`，`5cd4c5d`）修复前端 0 文件解析；仓库已转为真实 Git，基线与两个修复 commit 归因干净。
- 四维结果：正确性有条件——上轮致命项（伪造 baseline/result 摘要批准）与密钥轮换漂移均被独立复现拒绝，但新发现重要①（diff 证据交叉验证的触发条件自引用可变账本，role 降级/条目 sha256 伪造可静默剥离审批的 diff 证据绑定）；完整性通过——后端 327 passed、1 skipped、5 deselected 与闸门一致，两个新增攻击回归真实存在且通过，Git 归因干净（工作树 clean）；回归性通过——byte-manifest diff（含 5cd4c5d 头部回归）、SourceBaselineToken 竞态、审批投影/patch 篡改拒绝、保守预算、稳定锁/tombstone no-follow、notice CAS、MCP task 校验全部保持全绿，无回归；优越性通过——投影证据锚定与 backend/execution 双哈希身份明显优于 REVIEW-002 版本，但触发条件来源是设计弱点。
- 已验证：未读取 `config/.env`、未调用真实模型或供应商。后端 `pytest`：327 passed、1 skipped、5 deselected；定向 `tests/test_human_gate.py::test_approve_rejects_forged_metadata_digests_against_projection` 与 `tests/test_prepared_execution.py::test_llm_credential_rotation_changes_execution_identity` 通过；前端 `npm --prefix web test`：16 passed，`oxlint` 0 错误；`git log --oneline` 三个 commit、`git status` 干净，`git show --stat` 归因清晰。独立红队复现（reviewer 自建脚本，非项目测试，从生产 API 构建真实暂停 run 后篡改）：A 伪造 baseline/result 摘要 → `IntegrityError`（与投影证据不符）且账本未追加 `run_approval`/`run_resumed`；B 伪造 `patch_digest` → 拒绝（与产物哈希不符）；C 摘要置空 → 拒绝（缺少审批摘要）；E 篡改投影文件本体 → 拒绝（投影哈希不符）；R 仅换 api_key（同 provider_id/base_url/credential_ref，descriptor 除 `credentialRevision` 外逐字段一致）→ `backend_sha256`/`execution_sha256` 均改变，轮换后 `approve_run` 以 `SpecError`（执行后端已漂移）拒绝；D 把 node_done 中 diff 条目 role 改为 `""`、G 伪造该条目 `sha256` 使账本匹配落空 → 两者均绕过全部摘要校验获得批准（新发现）。
- 问题汇总：重要① `atlas/engine.py` `_verify_approval_material` 的 diff 交叉验证仅在账本 `artifact.role == "diff"` 且 name+sha256 与 node_done 匹配时触发，触发条件取自被防护的同一可变账本；暂停期把 role 降级或伪造条目 sha256 即可跳过校验，`run_approval.approved_diffs` 静默缺失 diff 证据，违背"摘要以投影证据为准"的修复意图与 REVIEW-002 关闭条件②（持久 run_approval 记录三摘要）。缓解事实：投影内容绑定与产物哈希校验不受影响，无法注入伪造摘要（A/B/C 均拒），`approved_diffs` 无下游安全决策消费，故非致命。次要① `credentialRevision` 为静态域分隔 SHA-256，对低熵密钥理论上构成离线猜测预言机（LLM 密钥为高熵随机值，记录为已接受权衡）。残余边界（非新发现）：事件账本无哈希链，可一致性重写全账本的攻击者本就可伪造任意审计，超出 REV-001 已裁定的"同用户非 OS 沙箱"边界。
- 决策路径：A。根因是本轮修复内单一的触发条件来源缺陷，非结构性缺口：把触发锚改为投影证据（`ref.name ∈ parse_projection_evidence` 即强制校验，账本 role/sha256 与投影证据不一致即拒绝）并补 role 降级与条目 sha256 伪造两个攻击回归即可关闭。
- 最终裁决：不通过（仅因新重要①；上轮致命项与密钥轮换项确认关闭）。关闭条件：①diff 证据校验以投影证据为触发锚，并增加 role 降级、条目 sha256 伪造两个攻击回归证明 D/G 被拒；②重跑完整无付费后端（≥327 passed）与前端 test/lint 闸门全绿。完成并经独立复核前，阶段 C/D、发布与真实花销继续暂停。
- 回流：developer 按 A 路径小修；memory-keeper 核验本记录与修复回流。

### REVIEW-003 · 2026-08-19 · 关闭复核（reviewer，路径 A 修复验证）
- 审查对象：修复 commit `fc190ae`（上轮重要①关闭路径 A）+ 上轮关闭条件①②。
- 产出复述：`_verify_approval_material`（`atlas/engine.py`）的 diff 交叉验证触发改为双侧一致——`ref.name ∈ parse_projection_evidence`（哈希锚定投影）或账本 `artifact.role == "diff"` 任一侧声明即强制校验，投影缺证据或账本 role 降级/条目 sha256 伪造（匹配落空）均拒绝；新增 `tests/test_human_gate.py::test_approve_rejects_role_downgrade_of_diff_entry` 与 `::test_approve_rejects_forged_diff_entry_sha256` 两个攻击回归。
- 四维结果：正确性有条件——上轮唯一遗留重要项①确认关闭（变体 D/G 均在写 `run_approval` 之前以 `IntegrityError` 拒绝、账本字节级未追加、锁正常释放），正向路径完好（未篡改暂停 run 批准后 `run_approval.approved_diffs` 完整含 baseline/result/patch 三摘要并与账本 metadata、投影锚逐一相符，`approved_consumed` 三项齐全），账本单方面 role 升级（H）与追加伪造 metadata 的重复 node_done（K）也被拒绝；但新发现重要②（consumed 摘除/改名绕过，见下）。完整性通过——后端 329 passed、1 skipped、5 deselected（327+2 新回归），前端 16 passed、oxlint 0 错误；两个新增攻击回归真实存在且通过；`fc190ae` 归因清晰（3 文件 +75/−4），HEAD 即 `fc190ae`、工作树 clean。回归性通过——上轮已关闭项无回归：伪造 metadata 摘要拒绝、投影哈希锚（E：篡改投影本体 → 投影哈希不符拒绝、账本未追加）、patch 篡改拒绝、`credentialRevision` 轮换（`test_llm_credential_rotation_changes_execution_identity`）、SourceBaselineToken 竞态（`test_agent_diff_security.py` 全绿）、保守预算/成本熔断（`test_costs_breaker.py` 全绿）、稳定锁/tombstone（`test_run_locking.py` 全绿）、notice CAS（`test_web_api.py` 24 项全绿）、MCP task 校验（`test_task_validation_matches_web_and_never_allocates_run` 6 参数化全绿）；定向批 61 passed、1 skipped。优越性通过（有保留）——投影证据交叉验证确实优于 `2c30560` 版本，但触发域不完整（见重要②），"任一侧单方面声明 diff 即拒绝"的承诺未完全落地。
- 已验证：未读取 `config/.env`、未调用真实模型或供应商（仅 FakeProvider/Stub 路径）、未修改生产代码。独立红队复现（reviewer 自建脚本，非项目测试，从生产 API `prepare_execution`+`execute_graph` 构建真实暂停 run：production-shaped runner + `_require_clean_git_workdir` SourceBaselineToken，真 git 仓库）：D role 降级 `""` → `IntegrityError`（账本条目缺失或 role 被降级）且账本字节不变、无 `run_approval`/`run_resumed`，还原后同 run 批准成功（锁已释放）；G 条目 sha256 伪造 → 同分支拒绝；H 把 `coder.output` 账本 role 升级为 `diff` 并伪造三摘要 → `IntegrityError`（审批投影缺少 Diff 产物证据摘要）；K 追加伪造 metadata 的重复 coder node_done → `IntegrityError`（baseline_digest 与投影证据不符）；正向批准 → `approved_diffs` 单项含 `name/artifact_sha256/三摘要` 且三值互异、`approved_projection_sha256` 等于 gate node_input 投影哈希；**I 从 gate `node_input.consumed` 摘除 `coder.diff` → 批准意外成功（status=done），`run_approval.approved_diffs=[]`、`approved_consumed` 缺 `coder.diff`**——而哈希锚定投影仍单方面持有 `coder.diff` 的证据标记与 diff 字节（锚定哈希完好），恢复后 gate 重建的 node_input 又包含全部三项 consumed，账本内部不一致却无人标记；J 把 consumed 条目改名（保留 path/sha256）→ 同样批准成功且 `approved_diffs=[]`（新发现）。
- 问题汇总：重要②（新）`atlas/engine.py` `_verify_approval_material` 的校验循环遍历 `node_input.get("consumed", [])`——同为可变账本字段，投影证据键集只是"consumed 内名字"的过滤器而非独立触发域；暂停期把 diff 条目从 consumed 摘除或改名，即可让哈希锚定投影中明示的 diff 证据完全脱离审批校验，`run_approval.approved_diffs` 为空、`approved_consumed` 缺项，与恢复后账本矛盾，静默且不可机检——与上轮重要①同类同界，且直接证伪 `fc190ae` "触发锚来自哈希锚定投影/任一侧单方面声明即拒绝"的核心声明。缓解事实（与上轮重要①相同）：投影内容绑定与产物哈希校验不受影响，无法注入伪造摘要（A/B/C/K 均拒），`approved_diffs` 无下游安全决策消费，故非致命。它不属于已接受的"全账本一致性重写"残余边界——该边界针对自洽伪造，而这里是制造内部不一致账本且校验层未标记，正是 REVIEW-002 关闭条件②"锁内复核其与待批 diff artifact/event 一致"要求覆盖的形态。次要：无新增（上轮次要① credentialRevision 权衡维持原判）。
- 决策路径：A。与 D/G 同根因的触发域不完整：把触发域改为哈希锚定投影证据键集的完整覆盖——`parse_projection_evidence` 的每个 diff 名必须仍出现在 `node_input.consumed` 并通过全部三摘要交叉校验（consumed 摘除/改名即"投影有、consumed 无"，拒绝），补 consumed 摘除、consumed 改名两个攻击回归（本轮 reviewer 脚本变体 I/J 即现成蓝本）即可关闭。
- 最终裁决：不通过（仅因新发现重要②；上轮唯一遗留重要项①确认关闭，关闭条件②闸门全绿）。关闭条件：①diff 证据校验以投影证据键集为完整触发域，consumed 摘除/改名被拒，并以两个攻击回归证明 I/J 被拒；②重跑完整无付费后端与前端 test/lint 闸门全绿。完成并经独立复核前，阶段 C/D、发布与真实花销继续暂停。
- 回流：developer 按 A 路径小修；memory-keeper 核验本记录与修复回流。

### REVIEW-003 · 2026-08-19 · 最终关闭复核（reviewer，重要②修复验证）
- 审查对象：修复 commit `56be2fe`（上轮重要②关闭路径 A）+ 上轮关闭条件①②。
- 产出复述：`_verify_approval_material`（`atlas/engine.py`）在 consumed 校验循环后新增完整覆盖校验——`set(projection_evidence) - evidence_covered` 非空即以 `IntegrityError` 拒绝，触发域由"consumed 内名字"改为哈希锚定投影证据键集的完整覆盖；新增 `tests/test_human_gate.py::test_approve_rejects_diff_removed_from_consumed` 与 `::test_approve_rejects_diff_renamed_in_consumed` 两个攻击回归（变体 I/J 蓝本）。
- 四维结果：正确性通过——重要②确认关闭：reviewer 独立脚本（非项目测试，从生产 API `prepare_execution`+`execute_graph` 构建真实暂停 run：production-shaped runner + `_require_clean_git_workdir` SourceBaselineToken + 真实 git 仓库）复现 I（consumed 摘除 coder.diff）与 J（consumed 改名），两者均在写 `run_approval` 之前以 `IntegrityError`（"审批投影声明了 Diff 证据…不在暂停节点的 consumed 清单中"）拒绝、账本字节级零追加、无 `run_approval`/`run_resumed`，拒绝后锁正常释放（同一 run 还原账本即批准成功）；正向路径完好（`approved_diffs` 单项含 name/artifact_sha256/baseline/result/patch 三摘要且三值互异、与账本 metadata 和投影锚逐一相符，`approved_consumed` 三项齐全，`approved_projection_sha256` 等于 gate 投影哈希）。其余变体（V3 名大小写、V4 consumed 清空、V5 consumed 键删除、V6 path 改指他产物+sha 同步伪造、V7 只改 path、V9 consumed+账本条目同时改名、V10 条目非 dict、V11 sha 置空、V12 path/条目 sha/patch_digest 三重伪造指向他产物）全部在 `run_approval` 前以 `IntegrityError` 拒绝且零追加——分别落入覆盖校验、投影↔账本双向声明校验、`read_artifact` 哈希断言或 patch_digest↔产物哈希/投影证据交叉校验；V8 精确重复条目与 V13 追加伪造条目（coder.output→diff 哈希）虽获批准，但 `approved_diffs` 的 diff 证据仍完整且逐字段正确（V8 仅多一条完全一致的重复记录），无证据静默丢失。完整性通过——后端 331 passed、1 skipped、5 deselected（329+2 新回归），前端 16 passed、oxlint 0 错误；两个新增攻击回归真实存在且通过；`56be2fe` 归因聚焦（3 文件 +71/−0：engine.py +10、PLAN +10、tests +51），HEAD 即 `56be2fe`、工作树 clean，5 commit 链完整（`9d1ed48`→`2c30560`→`5cd4c5d`→`fc190ae`→`56be2fe`）。回归性通过——定向抽查全绿无回归：伪造 metadata/role 降级/伪造条目 sha256/投影与 patch 篡改拒绝（test_human_gate.py 全绿）、`credentialRevision` 轮换（test_prepared_execution.py 全绿）、SourceBaselineToken 竞态（test_agent_diff_security.py 全绿）、保守预算/成本熔断（test_costs_breaker.py 全绿）、稳定锁/tombstone（test_run_locking.py 全绿）、notice CAS（test_web_api.py 24 项全绿）、MCP task 校验（test_mcp.py 全绿）。优越性通过——触发域从"consumed 内名字"升为投影证据键集的完整覆盖，与 `fc190ae` 的"任一侧声明即强制校验"构成双层防线：本类绕过的全部合理变体（摘除/改名/大小写/清空/删键/改指/置空/三重伪造）均无法在不动锚定物的前提下让投影声明的证据静默脱离审批；纵深额外证实 V14a——即使重写投影去证据标记并同步伪造账本哈希锚，只要 consumed 未摘除，账本 role=diff 单侧声明仍触发拒绝。
- 已验证：未读取 `config/.env`、未调用真实模型或供应商（仅 FakeProvider/Stub 路径与本地真实 git 仓库，5 个 `real_api` 全程 deselected）、未修改生产代码（唯一文件变更为本条目）。后端 `uv run pytest`：331 passed、1 skipped、5 deselected；前端 `npm --prefix web test`：16 passed，`npm run lint`（oxlint）：0 警告 0 错误；`git status` clean、`git log`/`git show --stat` 归因完整。独立脚本 19/19 断言通过：I/J/P0×2 + V3–V7、V8×2、V9–V13×2、V14a/V14b（残余边界特征化：V14b 需同时重写投影本体、账本 `projection_sha256` 锚与 consumed 摘除才获批，此时 `approved_diffs=[]`、锚定哈希即伪造值——该攻击必须摧毁哈希锚定物本身，属已裁定"同用户可重写全部审批材料、事件账本无哈希链"的残余边界，与 I/J 无需触碰锚定物即绕过有本质区别，维持原判、非新发现）。
- 问题汇总：无新增致命/重要。次要①（新，次要，不阻碍关闭）：consumed 追加伪造条目（V13，如 name=coder.output 而 path/sha=diff 产物）或精确重复条目（V8）可让 `approved_consumed`/`approved_diffs` 出现冗余或错标条目且校验层不标记——但 diff 证据绑定不被削弱、无证据静默丢失，且需与残余边界同级的账本写权限；后续批次可考虑 consumed 键集与投影"上游产物"节段名集的一致性/去重校验。次要②（既有权衡维持）：credentialRevision 离线猜测预言机与事件账本无哈希链残余边界均维持原判。
- 决策路径：无需回流。关闭条件①（投影证据键集完整触发域 + I/J 两个攻击回归）与②（闸门全绿）均经独立验证满足。
- 最终裁决：**通过**（REVIEW-003 关闭）。重要②确认关闭，上轮全部已关闭项无回归，无新的致命/重要发现。阶段 C 发布闸门与阶段 D 前置中的"独立复核"要求就 REVIEW-003 而言已满足；D 仍须待所有者预算确认与 preview/dry-run 红线。
- 回流：memory-keeper 核验本记录与关闭状态归档。

### REVIEW-004 · 2026-08-19 · 发布前实施复盘（reviewer）
- 审查对象：实施结果（P0min、P1、P6、首轮发布 blocker 修复与最终发布闸门）。
- 产出复述：有成本帽的未知费率 LLM attempt 在派发前保守预留全部剩余额度；SSE 控制通知不污染持久游标并拒绝旧连接迟到回调；YAML 拒绝重复键、anchor/alias/merge、资源炸弹与非法 Unicode；resume 在权威锁内先判持久状态再准备后端；发布工作流、sdist 扫描、harness 忽略规则和文档合同已同步。
- 四维结果：正确性、完整性、回归性与优越性均通过；未发现可复现的致命或重要发布 blocker。
- 已验证：同一工作树下 Python 3.14.6 与隔离 Python 3.12.9 均 425 passed、1 skipped、5 real_api deselected；前端 22 passed、lint 0、build 成功；六工作流离线闸门与 clean-init 全绿；sdist 173 条目、0 发现，Python 3.12 离线安装与六 MCP 工具探针通过。
- 问题汇总：无致命/重要问题。远端 GitHub Actions、受保护 release environment、tag、上传、SHA256SUMS、SBOM、provenance 与下载后验证仍属于尚未执行的操作性闸门；阶段 D 仍未授权。
- 决策路径：无需回流。
- 最终裁决：**GO，可进入远端 RC 发布流程**；不得在远端闸门完成前宣称发布完成，也不得自动启动阶段 D。
- 回流：developer 形成可复现提交并推送；远端闸门结果再同步检查单。

### 2026-08-19 · 阶段 A/B 修复 · REVIEW-003 关闭里程碑（developer）
- 实际改动：REVIEW-002/003 全部 blocker 经三轮路径 A 修复关闭——`2c30560`（审批投影证据交叉验证 + LLM credentialRevision）、`5cd4c5d`（diff 补丁 git 惯例头，修复前端 0 文件解析）、`fc190ae`（diff 校验触发锚改投影证据、role 降级/伪造 sha256 拒绝）、`56be2fe`（投影证据键集完整覆盖、consumed 摘除/改名拒绝）。仓库初始化为本地 Git（`9d1ed48` 基线，白名单审计 163 文件 0 禁项），5 commit 归因链完整。
- 验证证据：后端 331 passed/1 skipped/5 real_api deselected；前端 16 passed/lint 0/build 成功；sdist 重建 161 文件禁项 0、绝对路径 0；浏览器验收：diff 工作区三文件渲染、审批条、fail-closed 错误展示、守卫计入成本显示、初始化提示确认；HTTP 删除含 tombstone。REVIEW-003 最终关闭复核独立红队 19/19 断言通过。
- 计划偏差：无。阶段 C 的"独立复核"要求已满足；阶段 D 待所有者预算确认。
- 遗留问题：次要①consumed 追加/重复条目可造成 approved 列表冗余（无证据丢失，后续批次）；credentialRevision 离线猜测与账本无哈希链为已裁定残余边界。

### 2026-08-19 · 阶段 C · 本地发布闸门关闭（developer）

- 实际改动：
  - `allowed_paths` 契约固化为三层拒绝（YAML/spec 结构校验、快照恢复校验、生产 preflight/runner 纵深），并修正 `NodeSpec.allow_web` 注释漂移（默认关）；六个用户可见表面（README×2、SECURITY、skill、指南 concepts/safety）同步，文档契约测试锁定。
  - 新增三个共享无付费发布闸门脚本并由 CI/release 共用：`scripts/release_workflow_gate.py`（严格六图 validate+dry-run：显式假模型覆盖、6 次 registry/6 次 runner 预检计数、`execution_sha256` 非空、0 供应商调用、0 agent 调用、0 run 目录、网络审计钩子）、`scripts/release_clean_init_gate.py`（无 active config 起、两次真实 `atlas init`、模板逐字节、二次幂等、`agents.runner=fail_closed`、MCP stdout 0 字节）、`scripts/release_sdist_gate.py`（168 项归档扫描含禁项/占位符/私有路径、Python 3.12 离线安装完整锁定依赖、核心模块导入、最小 spec 解析、五 MCP 工具、配置初始化）。
  - CI/release 工作流删除手工 active config 复制与重复 web 安装/构建，前端闸门改为完整 `npm test`/lint/build 一次；Hatch 排除清单扩充（agents.json、初始化状态、缓存、构建产物、密钥类文件）。
  - B1/B3 前端可重放测试：`runCleanup.ts`（删除当前 run 后取消订阅→清空详情/事件/选中/工作区→回观测台→刷新列表的受测顺序）、`nodeDetailPresentation.ts`（runner/allow_web/同用户非 OS 沙箱边界文案），App/Settings 接线，单条删除等待清理完成；21 项前端测试。
  - MCP 工具文档撤回“错误带行号”的过度承诺（行列定位属 benchmark P6）。
- 验证证据（同一工作树，未提交，基于 HEAD `8c71b6b`）：
  - `uv lock --check` 通过；`compileall atlas scripts` 通过。
  - `uv run pytest`（Python 3.14.6）：**343 passed, 1 skipped, 5 real_api deselected**；`uv run --isolated --python 3.12 pytest`（Python 3.12.9）：**343 passed, 1 skipped, 5 deselected**。
  - `uv run python scripts/release_workflow_gate.py`：6/6 通过，registry/runner 预检各 6，provider_calls=0，agent_calls=0，run_directories=0。
  - `uv run python scripts/release_clean_init_gate.py`：两次 init 退出码 0，六文件逐字节一致，幂等，fail_closed，MCP stdout 0 字节。
  - `uv build --sdist` + sdist gate：当时归档 168 条目、0 发现；Python 3.12 离线安装锁定依赖成功，版本/入口/当时五工具（P1 前历史证据）/spec 解析/初始化全部通过。此数字仅为该次历史重建，不作为当前最终发布条目数。
  - 前端：`npm test` 21 passed、oxlint 0/0、build 成功（仅既有 chunk 大小提示）。
  - 浏览器 GUI 验收（隔离临时数据+假 runner，无供应商调用）：①implementer 详情执行后端/同用户非 OS 沙箱/allow_web 关闭/联网边界 + diff 工作区（1 文件 +1/−1、sha256 校验、文件树、逐行 old→fixed）DOM+截图证据通过；②gui-paused 审批材料（四项消费产物哈希校验、三摘要 prompt、批准/驳回+必填说明）DOM 证据通过——截图通道在会话后段故障，视觉证据不可用，如实记录；③单条删除确认→取消保持选中、确认→列表移除/详情清空/URL 回 `#/observe`、暂停态无删除按钮；④设置页批量清理遇真实 OS 锁：`已清理 1 条，失败 1 条：gui-locked 正被其他操作占用(.locks)`。
  - 隔离测试数据与临时服务已全部删除。
- 计划偏差：远端 GitHub Actions、tag `v0.1.0-rc.1`、发布产物上传与 provenance 未执行（本地仅验证工作流 YAML 与共享脚本）；fresh-clone README 全程走查未做（以 clean-init gate + sdist 冒烟覆盖其自动化部分）——RELEASE_CHECKLIST 中对应项保持未勾。阶段 D 按所有者决定推迟。
- 遗留问题：无新增致命/重要。GUI 验收测试点②③④的截图证据缺失（工具通道故障，DOM 证据完整）。
- 补充（同日）：补做 fresh-source README 全路径走查——隔离复制当前树（剔除 .venv/node_modules/.git/runs/dist/活动配置）后按 README 执行 `uv sync --locked --all-groups`（73 包）、`npm --prefix web ci`（0 漏洞）、`npm --prefix web run build`；升级路径二次 sync 幂等；`python -m atlas.web` 服务 `/api/workflows` 返回 200/6 工作流，六个活动配置由真实入口自动创建且 `agents.runner=fail_closed`。RELEASE_CHECKLIST 对应项已勾。至此阶段 C 的全部本地项收口；仅剩远端 CI、tag 与发布产物上传（等待所有者 push 决定）。

### 2026-08-19 · benchmark 精简批次一（P0min + P1 + P6）· 本地关闭

- 实际改动：LLM 在存在 `max_cost_usd` 时按 projected 预留；费率未知时调用 `reserve_remaining` 预留全部剩余额度，并持久化独立 `cost_reserved`，以同一 reservation id 结算 actual/accounted/unknown/usage；未知费用和崩溃窗口按预留额保守占用。无 cap 时不创建 reservation、不写预留事件或虚构金额。新增动态 `interrupted` 状态、Web 恢复按钮与 `POST /api/runs/{rid}/resume`、MCP `atlas_resume_run`；Web/MCP 共享 `derive_run_status` 与领域级锁内 resume 准入，恢复在稳定 OS run lock 内复核事件状态、规格快照、完整执行身份和 checkpoint，paused/终态/活跃运行均拒绝且不追加账本。共享 launcher、MCP 异步与同构 summary 未纳入 P1，留给发布后 P4。YAML 解析增加 parse-local 路径到 mark 旁路表，`SpecError` 统一携带 path/一基 line/column，位置不进入快照或指纹。
- 故障修复：真实 Windows 子进程强杀暴露 SQLite WAL 句柄退出后的短暂 `SQLITE_IOERR_TRUNCATE`。恢复准入仅在已经持有权威 run lock 时，对该明确瞬态错误及 busy/locked 做有界的新连接重试；不删除 WAL/SHM，不重试 CORRUPT/NOTADB/其他 I/O 错误，损坏 checkpoint 继续 fail-closed。
- 验证证据：Python 3.14.6 与隔离 Python 3.12.9 均为 **374 passed、1 skipped、5 real_api deselected**；真实子进程强杀恢复连续三次通过，已完成节点没有重跑，事件 seq 单调且有 cap 的 reservation 重放保守；无 cap 不生成 reservation/金额的分支有回归。Web/MCP 跨入口、paused/终态/重复恢复拒绝、YAML 行列返回均有回归。前端 21 passed、oxlint 0/0、build 成功。六工作流离线闸门 6/6、0 provider/agent 调用、0 run 目录；clean-init 两次成功且幂等；当时重建的 sdist 扫描为 0 发现，Python 3.12 离线安装、核心导入、六 MCP 工具、spec 解析和配置初始化通过。此后仍有文件变化，最终条目数以发布前最终重建扫描结果为准，不沿用旧数字。
- 未执行事项：未读取真实密钥，未调用真实供应商或产生付费；远端 GitHub Actions、tag、上传与 provenance 未执行。阶段 D 不因本地前置收口而自动启动，仍须所有者重新确认时点、预算，并逐条先 preview/dry-run。本轮 reviewer 文档 blocker 正在修复，待最终复核，不能据此宣称最终完成。


### 2026-08-19 · 文档事实漂移 blocker · 修复进行中
- 实际改动：校正 P0min cap/reservation 分支、P1 已完成边界、P3/P8 关系、真实 agent 结果说明、阶段 C/精简批次/剩余发布工作、B5 五→六工具历史口径与最终 sdist 口径；仅修改文档。
- 验证证据：`uv run pytest tests/test_docs_agent_contract.py tests/test_mcp_docs_contract.py tests/test_release_gates.py` 为 32 passed；目标文档 `git diff --check` 通过；陈旧事实扫描无命中（保留的命中均是否定“最终完成”或明确留给 P4 的正确表述）。reviewer 最终复核尚未发生。
- 计划偏差：无功能范围变化；只纠正文档事实漂移。本文已超过 400 行，已触发膨胀阈值，回流 project-planner 评估是否拆分后继 PLAN，本轮不自行拆分。
- 遗留问题：developer 修复与自审已完成，reviewer blocker 仍待最终复核，不能宣称最终关闭；远端 CI、tag、upload、provenance 与阶段 D 尚未执行。

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
