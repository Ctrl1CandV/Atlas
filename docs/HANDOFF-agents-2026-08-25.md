# Atlas 双 Agent 实施交接文档(2026-08-25)

> **读者与用法**:本文档写给两个即将接手的 Agent——**实施 Agent**(按批次写代码)与**审查 Agent**(每批次提交前独立审查)。两个 Agent 都没有此前的会话记忆;本文 + 仓库就是全部上下文。实施合同的权威来源是 [`ROADMAP.md`](ROADMAP.md)(含 2026-08-23 深化的各期"落地锚点"),排期入口是 [`PLAN-post-audit-2026-08-22.md`](PLAN-post-audit-2026-08-22.md);本文负责:现状快照、硬性纪律、双 Agent 协作协议、批次清单与顺序建议、用户决策登记、已知坑备忘。与仓库实际状态冲突时,以仓库为准并回报用户。

---

## 一、现状快照(截至 2026-08-25,HEAD `8f454c2`)

- **已完成梯队**:一(P0 审查必修)、二(拒绝性错误削减 B1/A1/A2/C1/B3,含 A1 真实运行验收)、三(R0 仓库治理 100% 闭环)、四(P4 + P2 核心范围)。
- **测试基线**:`uv run pytest` → **497 passed / 1 skipped / 5 deselected**;Web `npm --prefix web run lint / test / build` → 0 告警 / 22 passed / 构建成功;公开 CI 两分支双绿(`.github/workflows/ci.yml`)。
- **MCP 工具面:8 个**——validate、save、run(`wait=false` 异步)、list workflows、list runs、get run、**cancel run**、resume。契约锁定在 `tests/test_mcp.py`、`tests/test_release_gates.py`、`scripts/release_sdist_gate.py`;改工具数必须同步这四处 + skill/指南/STATUS/docs/mcp.md。
- **运行终态集合**:done / failed / **cancelled** / paused(等待)/ running / interrupted(动态派生)。fold 在 `atlas/events.py::fold_events`。
- **关键新模块**:`atlas/launcher.py`(进程内 ControllerRegistry,Web/MCP 共用启动路径)、`atlas/runs.py::build_run_summary / list_run_summaries`(Web/MCP 共用摘要)、`atlas/engine.py::request_cancel / write_cancel_request`(协作式取消)、`atlas/adapters.py::recover_json_object`(B1 宽容提取,路由器同源消费)。
- **本地环境注意**:当前检出分支为 `main`;`docs/post-v0.1.0-release-hardening` 与 `main` 保持同点,推送时两分支一起推;推送必须走 `git -c http.proxy=socks5h://127.0.0.1:7890 -c http.version=HTTP/1.1 push origin main docs/post-v0.1.0-release-hardening`。

## 二、硬性纪律(违反任何一条都算事故)

1. **git**:提交与推送必须经用户明确允许(本文档的批次流程已含"批次完成即提交推送"的授权,但**发布/tag/release 操作始终需要用户单独批准**);推送走上面的 socks5h 命令;绝不提交 `config/.env`、config/ 活动文件(providers.json / capabilities.json / pricing.json / agents.json / models.reference.json)、`runs/`、`runs-archive/`、`workflows/mcp-custom-pipeline.yaml`、本机绝对路径。
2. **成本红线**:任何真实供应商调用前必须先 validate + dry-run;显式预算或结构性约束控本(retry=0、timeout_s、max_iterations、便宜模型);本机 pricing 全 null 时多节点图**不可设 max_cost_usd**(只放行首个计费节点);agent 真跑必须配预算;自动 retry 已于 2026-08-27 裁决——采纳 A(缺省不重跑的书面承诺)+C(dry-run 警告),否决 B 准入硬拦,实施=批次 K(见 `docs/rfcs/agent-retry-budget.md` 决议节);K 落地前现状(retry 缺省 0)不变。
3. **账本纪律**:append-only 事件账本是唯一真相;新事件类型必须有测试,且 `fold_events` 终态语义不得改变(删掉新事件后 fold 结果必须与旧事件流一致);产物 write-once + SHA-256;不吞异常、不静默降级;fail-closed。
4. **文档纪律**:用户可见变更必须进 CHANGELOG;文档不得声称未验证的能力;数字(测试基线、工具数)必须与代码一致——改了就同步全部出现点。
5. **界面纪律**:Web 只观测 + 改环境 + 审批/取消类控制,图的读写闭环留给 MCP,不加图编辑器。
6. **审查纪律**:每个实施批次在提交前必须经记忆干净的审查 Agent 独立审查(协议见下节),阻塞项清零后才可提交。

## 三、双 Agent 工作协议

### 实施 Agent(每批次)

1. 从第五节批次清单取当前批次,读 ROADMAP 对应期的"落地锚点"(模块/事件/表面/测试级方案)——锚点是设计意图,实现时可微调,但**事件语义、锁纪律、终态唯一性不可妥协**。
2. 小步实施;每完成一个可验证单元就跑局部测试;批次内自洽后跑全量:`uv run pytest -q` + Web 三连(改了前端必须 `npm --prefix web run build`,atlas-web 服务 `web/dist`)。
3. 竞态/时序类测试的铁律(两次 CI 事故的教训):**断言必须接受所有合法时序**。例:取消可落在节点入口(未开始即终止)或节点在途完成后,两者都是正确行为;写死单一时序的测试在 CI 上必炸。
4. 文档同步:CHANGELOG + 该批次涉及的 skill/指南/STATUS/契约测试。
5. 自测通过后,产出"批次报告"交审查 Agent:改动文件清单、声称的行为清单、验证命令与输出摘要。**不提交**。
6. 审查通过(阻塞清零)→ 按批次提交(英文小写前缀:feat/fix/docs/test/chore)→ 推送两分支 → 等 CI(约 7 分钟)确认双绿。CI 红了先查失败步骤与日志(API 见第八节),区分真回归与 flake(见第七节)。

### 审查 Agent(每批次)

1. 你没有实施会话的记忆。只依据仓库本身:diff、被改文件的上下游、测试。**不轻信注释与测试自述**——逐条核对面。
2. 核查顺序:①声称行为与代码一致;②既有路径无回归(特别是锁契约、fold 终态、事件顺序、HTTP 语义);③边界与竞态(并发、崩溃窗口、取消时序、空/坏输入);④测试是否真的锁定行为(而非复述);⑤契约面/文档数字一致性(grep 全仓,排除 docs/archive 与历史段落);⑥实际跑测试(局部 + 全量)。
3. 输出格式:按严重度分【阻塞/建议/无关紧要】;每条给 文件:行号 与理由;核实无问题的维度明确写"已核实无问题";最后一句话结论:**可否提交**。
4. 阻塞项必须修复后重审;建议项由实施 Agent 权衡(采纳或不采纳都要在批次报告里说明理由);审查发现的事实错误(如文档承诺不存在的功能)一律修。

### 提交与推送授权边界

- 本文档定义的"批次完成"流程内:正常 commit/push 已获用户授权(2026-08-23/25 两次确认)。
- **未授权**:打 tag、发 Release、改 GitHub 仓库设置、删除远端分支、任何 `release-assets.yml` 触发、真实付费的大额运行(单批真跑预算超过 $0.50 需先问用户)。

## 四、用户决策登记(已拍板,不翻案)

| 决策 | 内容 | 时间 |
|---|---|---|
| tests/scripts 公开 | tests/、scripts/、ci.yml、real-api.yml 随仓库公开;CI 徽章可挂 | 2026-08-23 |
| 默认分支与保护 | 默认分支 `main`;deletion/non-fast-forward 规则集强制启用 | 2026-08-23 |
| S1 终局可视化 | run 结束必须有最终可视化 + opt-in 总结节点(回顾各节点工作);**不做离线报告导出**(原 Stage E 条目已移除) | 2026-08-23 |
| MCP 主路径 | HTTP 端点(`atlas-web` 单命令)是主路径;随仓 `.mcp.json` 指向 HTTP,stdio 是显式备选(绝对路径);契约测试锁定 | 2026-08-23 |
| 双 Agent 模式 | 剩余阶段交给实施 Agent + 审查 Agent;每批次审查通过才提交 | 2026-08-25 |
| agent retry 策略 | 先 RFC(`docs/rfcs/agent-retry-budget.md`)后动;建议路径"先警告后默认 retry=0";决策权在用户 | 2026-08-23 |
| C2 真实单价 | pricing.json 只能由用户填确认过的数字,Agent 永不猜 | 2026-08-23 |
| D4 retry 裁决 | 采纳 A(缺省 0 升格为书面承诺)+C(dry-run 组合警告),否决 B 准入硬拦;实施=批次 K | 2026-08-27 |
| C2 状态更新 | **舍弃**——pricing.json 保持可选补充,不再作为待办/提醒;控本以结构性约束为准(retry=0/max_iterations/timeout);provider-reported cost 方向仅存档于 BACKLOG 搁置区 | 2026-08-27 |
| Stage E 全量立项 | 五项全做,顺序 K → E-1 web_search → E-2 通讯文件(A/B)→ E-3 内置前端 → E-4 浏览器矩阵(先冒烟)→ E-5 沙箱调研;设计与验收详见 `docs/PLAN-stage-e-2026-08-27.md` | 2026-08-27 |

## 五、剩余批次清单(建议顺序;合同细节见 ROADMAP 对应节)

> 顺序原则:先小后大、先低风险后高风险、依赖前置。每批的"验收门"= ROADMAP 该期验收标准 + 双绿 CI。

**批次 A:P9 controller heartbeat(3–5 人日,低风险,建议首发热身)**
合同:ROADMAP §5 + 落地锚点。要点:engine 每 attempt 挂 watchdog 线程写 `node_progress`(node/iteration/attempt/candidate/elapsed_ms/phase);间隔下限 30s 可配;fold 显式忽略该类型(回归测试:删事件 fold 不变);终态后停止、迟到 heartbeat 拒绝;事件容量代价如实写进文档。

**批次 B:S1 执行终局可视化 + 总结节点(5–8 人日,用户定案)**
合同:ROADMAP §6b。要点:①零成本终局卡片(Web 运行页顶部,纯账本派生:最终结果摘要 + 每节点一句话回顾 + 时间线/成本,复用 `build_run_summary`);②opt-in 总结节点:图级 `summary: {model, prompt_hint?}`,run_done 前一次总结调用,write-once 产物 + `run_summary_written` 事件,失败记 `run_summary_failed` 不改终态;成本入 ledger 受 guards;dry-run 明示;内容标注"LLM 叙述,事实以账本为准"。**不做导出**。

**批次 C:P3 异常 taxonomy + 节点 `on_error`(7–11 人日,中风险)**
合同:ROADMAP §6 + 锚点。要点:新建 `atlas/exc.py` 分类层;治理类永不可吞(含 P2 的 RunCancelled);内容类可 `on_error: stop|continue|branch`;branch 需 `__failed__` 边(校验期强制);`node_failed_soft` + write-once error artifact;fold 新旧事件同终态(含反例);Web/MCP 同源展示。

**批次 D:P2 残余强化(3–5 人日,中高风险,建议在 C 后)**
- D1 Web 界面取消按钮(运行页,cancelled 状态已就绪;走 `POST /api/runs/{id}/cancel`,需 X-Atlas-Request 头);
- D2 CLI 进程树终止:local_cli 保存受控进程句柄,取消/超时终止整树(**必须含真实子进程 kill 测试**,复用 `tests/test_p1_kill_resume.py` 基建);
- D3 `--max-budget-usd` 映射进 Claude CLI 预检(桩 CLI 测有效值/缺失/拒绝/超支报告);
- D4 agent retry RFC 决策落地(**先拿用户对 RFC 的裁决**,再实施;默认行为变更需 CHANGELOG 显著声明)。

**批次 E:体验债小件打包(2–4 人日,可穿插)** —— PLAN 2b 表:预留额展示、跨入口运行列表自动刷新、账本"查看完整"入口、agents.json 状态卡片、seed/temperature 回显核对、claude --help 契约加固、`_resume_graph_replay` 收敛、`_NODE_FACTORIES` 死占位清理。熔断持久化与 16MiB 账本治理跟随 P10。

**批次 F–I:第六梯队(顺序固定:P7 → P13 → P10 → P11)**
合同:ROADMAP §7–§10 + 各自锚点。P7(artifact import + invocation hash,高风险,SHA 血缘合同是命门)→ P13(fork 失效闭包)→ P10(retention/star/索引,与账本治理合并设计)→ P11(request_changes 三分支)。

**批次 K:D4 收官小批次(先行)**:①retry 默认承诺落档(README×2/skill/concepts/CHANGELOG 固定句式);②`_dry_run_warnings` 对 retry>0 agent 节点的组合警告+反向验证测试。实施清单见 `rfcs/agent-retry-budget.md` 决议节。

**Stage E 各批(E-1…E-5)**:依 `docs/PLAN-stage-e-2026-08-27.md` 滚动立项,顺序 E-1 → E-2A/B → E-3 → E-4(冒烟先) → E-5;每批仍走「报告→审查→提交」协议。

**常备任务(非批次)**:~~C2 用户填真实单价~~(2026-08-27 舍弃,pricing.json 不再作为待办);发现新 flake 按"根因修复优先"处理(先例:锁种子字节)。

## 六、设计文档要求(每批次产出的文档义务)

1. 批次报告(实施 Agent 交审查用,会话内工件,不必提交)。
2. **CHANGELOG 条目必须写"动机 + 实证"**(先例:`.mcp.json` 条目写明了 ZCode 实证;`agent-retry-budget.md` 记录了 $10.5 事故背景)。
3. 引入新事件/新配置/新 YAML 字段的批次,必须同步:ROADMAP 该节(如有偏差注明决策)、STATUS 能力表、skill、指南、契约测试。
4. 设计争议(事件命名、语义分叉)记录进 `docs/rfcs/`,决策前不改默认行为。

## 七、已知坑与技术备忘(两次 CI 事故 + 现场排障的沉淀)

1. **锁文件不写种子字节**:区域锁可锁空文件;"先写后锁"在并发进程下互撞 → `PermissionError [Errno 13]` flake(config_init 与 engine 两处已修;新增锁时照此)。
2. **时序测试写法**:见第三节第 3 条;本地 5 连绿不代表 CI 绿。
3. **uv 缓存**:`uv sync` 不填注册表元数据层,离线 pip 解析在干净机器必失败;sdist 门是 lock 约束 + 默认联网(`--offline` 可选)。本机 uv 0.12.1,CI 钉 0.8.13;本地 venv 是 Python 3.14(CI 3.12)。
4. **cp1252**:西文 Windows 控制台打中文会崩;控制台入口统一走 `atlas/console.py::force_utf8_stdio`。
5. **Node 22**:web 测试脚本已带 `--experimental-strip-types`;升级 Node 注意。
6. **端口冲突**:atlas-web 有 fail-loud 预检;排障入口 `docs/mcp.md` Troubleshooting 节(端口占用/旧实例/旧 stdio 条目/Session not found)。
7. **模型怪癖**:deepseek 推理需 `maxOutputTokens: 16384`(已配供应商级默认);glm 系列 JSON 不可靠(宽容提取只救围栏包装);qwen/kimi 稳定;结构化节点默认配跨厂商 fallback。
8. **flake 处理原则**:同提交两分支一绿一红、重跑即绿 → 高度疑似 flake,但**必须找根因修掉**,不许只重跑(先例两次)。
9. **墙钟敏感测试(2026-08-25 实证,同日已修复;教训保留)**:`tests/test_a9_node_params.py::test_graph_deadline_caps_node_timeout` 曾用 `timeout_s=1.0` 断言绝对墙钟,共享 runner 高负载(实测整跑 1.79s)即失败——已改为断言封顶**关系**(节点 120 被 run 级 30 压住);`tests/test_m6_artifacts_thinking.py` 三处无睡眠紧凑轮询在高负载下先耗尽、索引空 nodes 报 IndexError——已统一为 `_wait_done` 助手(0.02s 间隔轮询 + 终态断言 + 超时大声失败)。**新写测试的规则**:不断言绝对墙钟;轮询必须带睡眠与终态断言。

## 八、验证与排查命令集

```bash
# 全量测试与 Web
uv run pytest -q
npm --prefix web run lint && npm --prefix web run test && npm --prefix web run build

# 推送(两分支同点)
git branch -f docs/post-v0.1.0-release-hardening HEAD   # 若当前在 main
git -c http.proxy=socks5h://127.0.0.1:7890 -c http.version=HTTP/1.1 push origin main docs/post-v0.1.0-release-hardening

# CI 状态与失败日志(匿名可查状态;日志需凭据,见下)
curl -s --proxy socks5h://127.0.0.1:7890 "https://api.github.com/repos/Ctrl1CandV/Atlas/actions/runs?per_page=2"
# 日志:用 git credential fill 取 token 后带 Authorization 调 /actions/jobs/<id>/logs(先例可抄本仓库会话记录)

# MCP 端点探针(atlas-web 须在跑)
curl -s -X POST http://127.0.0.1:8321/mcp -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
```

---

**交接仪式**:两个 Agent 各自开新会话,以本文档为唯一入口文档;实施 Agent 从批次 A 开始;审查 Agent 每批次被召唤。用户是唯一仲裁者:RFC 裁决、发布批准、成本超标放行都找用户。
