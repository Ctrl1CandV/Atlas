# Atlas

**中文** · [English](README.en.md)

![version](https://img.shields.io/github/v/tag/Ctrl1CandV/Atlas) ![license](https://img.shields.io/badge/license-Apache--2.0-green) ![platform](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgrey) ![python](https://img.shields.io/badge/python-3.12-blue) ![ci](https://github.com/Ctrl1CandV/Atlas/actions/workflows/ci.yml/badge.svg)

Atlas 是一个跑在本机 Windows 上的多模型工作流引擎：用 YAML 画一张有向图，让不同厂商的模型各司其职——并行调研、交叉审查、改代码、人工拍板——每个节点的完整输入输出都实时落在你自己的磁盘上。

它解决一个具体的问题：把多个 LLM 调用串成一条可靠流水线时，"看起来成功了"和"真的成功了"是两回事。Atlas 对每一次调用做假成功检测（空输出、截断、缺字段都过不了关），对每一份产物做哈希断言，对每一笔花销记账。出问题时你能看到是哪一步、哪个模型、为什么。

![Atlas 运行视图](assets/observe-run.png)

点开任意节点,可以看到它的完整输入投影、输出产物、实际使用的模型、token 与耗时;运行暂停在人工门时,审批条会直接列出待审材料与完整投影(带哈希),点开即审——批准绑定的就是这份材料的哈希,驳回则必须写明理由。

![Atlas 节点详情](assets/observe-node.png)

## 它长什么样

一张图 = 一个 YAML 文件。比如让三个视角并行分析、汇总后交给编程 agent 改代码、再由人审批：

```yaml
name: stage-d-custom
nodes:
  - { id: left,  type: llm, model: Deepseek:deepseek-v4-flash,
      prompt: "给出任务的一个侧面要点", consumes: [task] }
  - { id: mid,   type: llm, model: Deepseek:deepseek-v4-flash,
      prompt: "给出另一个侧面要点",     consumes: [task] }
  - { id: right, type: llm, model: Deepseek:deepseek-v4-flash,
      prompt: "给出第三个侧面要点",     consumes: [task] }
  - { id: joiner, type: llm, model: Deepseek:deepseek-v4-flash,
      prompt: "汇总三个要点为执行摘要", consumes: [task, left.output, mid.output, right.output] }
  - { id: coder,  type: coding_agent, workdir: demo-project,
      prompt: "按摘要实施改动并自测", consumes: [task, joiner.output] }
  - { id: checker, type: llm, model: Deepseek:deepseek-v4-flash,
      prompt: "核对 diff 是否精确完成任务,输出 verdict", consumes: [task, coder.diff] }
  - { id: gate, type: human, prompt: "审阅改动与核对结论后批准或驳回" }
edges:
  - { from: left,  to: joiner }
  - { from: mid,   to: joiner }
  - { from: right, to: joiner }
  - { from: joiner, to: coder }
  - { from: coder,  to: checker }
  - { from: checker, to: gate }
  - { from: gate, to: END }
guards:
  timeout_s: 1800
```

这是一次真实运行的记录（2026-08-19，DeepSeek deepseek-v4-flash）：7 个节点全部完成，编程 agent 精确地在 demo 项目 README 追加了要求的一行文本，diff 审查判定 pass，人工批准后结束。全程 4 分 44 秒，已知成本 **$0.0442**——账本里每个节点的 token 数、耗时、费用逐条可查。

## 真实案例

以下均来自发布前用真实供应商 API 跑过的运行，包括失败的（失败也是记录的一部分）。

**修复循环 + 代码审查 + 人工门**（`code-change-review-approve` 示例，SuperAI doubao-seed-2.0-lite）。任务："修复 demo-project 中的示例错误并自测"。第一轮：implementer 修好了 fizzbuzz 的判断顺序错误，但顺手多建了两个无关文件，也没真正跑测试；reviewer 输出结构化判定 `repair`，列出问题清单。第二轮：implementer 清理了多余文件，reviewer 判定 `pass`。人工批准，结束。两轮共 $0.96，两次 diff 都作为产物留档可下载。（如实说明：当前回边不携带审查意见，第二轮是从冻结 baseline 的重新实施——"循环携带反馈"列在 BACKLOG。）

**10 节点自定义图，MCP 直跑**（2026-08-22，DeepSeek + SuperAI 四个模型）。对装了 Atlas skill 的 AI 助手描述目标，它当场写了一张 10 节点图：三路并行调研 → 汇总 → 结构化审查（条件路由）→ 修订分支（消费审查意见）→ 复审 → 终稿 → **人工审批** → 门后收尾。整图作为 `atlas_run_workflow` 的 `yaml` 参数经 MCP HTTP 端点直接运行，不落盘。8 个执行节点全部一次通过（审查首轮 pass，未进修订轮），人工批准后收尾完成：全程 361 秒（其中 294 秒在等人工），约 $0.01，每个产物的哈希事后独立复验一致。

**诚实记录的失败**。同一测试矩阵里也出现过：推理型模型把输出预算烧在思考上导致可见文本为空（被空输出检查拦下）、prompt 只送达 1%（被截断哨兵抓到）、agent 首次尝试自报约 $10.5 后自动 retry 被人工终止——这次事故直接催生了现在的成本预留机制和"所有真实 agent 运行必须配预算"的纪律。完整矩阵见 [`docs/STATUS.md`](docs/STATUS.md)。

## 快速开始

需要 Windows 10/11 x64、Python 3.12、[uv](https://docs.astral.sh/uv/)。Git clone 用户还需 Node.js 22.12+ 构建一次前端；官方 Release 的 bundle 包已内置构建好的前端（`atlas-web` 自动识别，也可用 `--dist` 参数或 `ATLAS_WEB_DIST` 环境变量显式指定）。

```powershell
git clone https://github.com/Ctrl1CandV/Atlas.git
cd Atlas
uv sync --locked --all-groups
npm --prefix web ci
npm --prefix web run build
uv run atlas-web
```

一条命令同时启动 Web 界面（<http://127.0.0.1:8321>）和 MCP 端点（`http://127.0.0.1:8321/mcp`）。在 Claude Code / ZCode / Cursor 里把 MCP server 指向该 URL，你的 AI 助手就能替你写图、校验、预演、运行；仓库自带的 `.mcp.json` 也指向该端点。配置细节见 [`docs/mcp.md`](docs/mcp.md)。

首次启动会从模板生成本机配置（不覆盖已有文件），凭据只放 `config/.env`。六个随附示例（并行综合、辩论裁决、map-reduce、重试循环、人工审批管线、代码实施审查）开箱即可校验和预演；真实运行前给每个节点绑定你配置好的模型。

## 设计原则

- **先预演后付费**：`validate` 和 `dry_run` 零成本，dry-run 与真跑使用同一份有效规格，可用哈希绑定两者身份。
- **完整性优先**：产物按引用传递（文件 + SHA-256），读取时断言哈希；缺失产物显式失败，绝不给下游喂空串；超长不截断而是报错。
- **假成功即降级**：HTTP 200 不算成功。空输出、截断、缺必填字段都会触发跨厂商 fallback 链，降级在界面上显式标注。
- **全程可审计**：append-only JSONL 事件账本是唯一真相，界面显示的一切都能在账本里找到出处；审批证据绑定 baseline/result/patch 三摘要。
- **崩溃可恢复**：控制器被杀后运行自动判定 interrupted，恢复只补未完成节点，成本预留不重算预算。
- **人在环**：`human` 节点把图暂停在界面里；审批条列出待审材料与完整投影（带哈希，点开即审），驳回必须填写理由。

## 能力边界（如实说明）

- 仅支持 Windows 10/11 x64；以源码 sdist 发布，未上 PyPI，无预编译安装器。当前正式版本 `v0.1.0`，Git clone 与 sdist 内容略有差异（见 [Release 说明](https://github.com/Ctrl1CandV/Atlas/releases/tag/v0.1.0)）。
- Web 只绑回环地址，没有多用户认证；不要暴露到网络。
- 编程 agent 通过 Claude CLI 以**当前用户身份下的宿主进程**运行（需 `config/agents.json` 显式 `"runner": "local_cli"`，供应商须提供 `anthropicBaseUrl` 与凭据）。目录副本不是 OS 沙箱：进程理论上能访问当前用户有权访问的任何路径。`allowed_paths`、回环绑定都不是安全边界。Atlas 不写原目录，diff 由冻结 baseline 的普通文件字节清单生成完整文本 unified diff，二进制变更大声失败。
- `allow_web: false` 只是不授予 Claude CLI 的 WebSearch/WebFetch 工具；可写 agent 有 Bash，仍可能联网。`max_turns` 是校验过的规格字段，但当前 Claude CLI 没有硬轮次参数，硬限制来自 deadline 与已配置预算。
- research/coding_agent 节点缺省不自动重跑（retry 缺省 0）；显式声明 `retry: N` 后，dry-run 必须出现放大风险警告。
- 成本帽只在费率已知的调用上精确生效；费率未知时保守占满剩余预算，但不能证明供应商实际账单没超。
- "多厂商辩论"等名称只表达拓扑；只有绑定了真实不同的供应商，意见才独立。
- 审批证据绑定 `baseline_digest`、`result_digest` 与 `patch_digest` 三摘要；可写 coding agent 与 `allowed_paths` 的组合会在创建 run 前被拒绝（Claude `--add-dir` 不是只读边界）。

## 测试与验证

2026-08-27 基线：Python 测试 **664 passed / 2 skipped**（另有 5 个真实供应商测试默认排除、需主动运行且可能收费）；Web 测试 22 passed、lint 0 告警、生产构建通过；公开 CI 在 main 分支双 job 通过。10 节点自定义图经 MCP HTTP 端点真实运行全链路通过（含人工审批，见上文真实案例）。

2026-08-19 发布基线：Python 测试 **427 passed**；Web 测试 22 passed、lint 0 告警、生产构建通过；六个示例工作流严格离线 validate/dry-run，0 次供应商调用；发布 sdist 100 个条目、0 扫描发现。数字对应当时的源状态，详见 [`docs/STATUS.md`](docs/STATUS.md)。

开发验证：

```powershell
uv lock --check
uv run python -m compileall -q atlas
uv run pytest
npm --prefix web test && npm --prefix web run lint && npm --prefix web run build
```

## 文档

- [`docs/STATUS.md`](docs/STATUS.md) — 当前产品与发布事实的入口
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — 未实施功能与排序
- [`docs/mcp.md`](docs/mcp.md) — harness 接入配置
- [`skill/SKILL.md`](skill/SKILL.md) — 给 AI 助手看的操作手册
- [`SECURITY.md`](SECURITY.md) · [`CONTRIBUTING.md`](CONTRIBUTING.md) · [`CHANGELOG.md`](CHANGELOG.md)

## 许可证

Apache License 2.0。见 [`LICENSE`](LICENSE) 与 [`NOTICE`](NOTICE)。
