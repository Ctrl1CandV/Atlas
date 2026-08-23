# Atlas 下一轮交接（历史快照）

> **历史快照，不是当前交接。** 本文停留在 2026-08-18 第五轮结束时，测试数字、待办与真实运行状态已被后续工作取代。当前事实见 [`../STATUS.md`](../STATUS.md)，下一轮优先级见 [`../ROADMAP.md`](../ROADMAP.md)。

## 一、当时状态（2026-08-18 第五轮结束）

第五轮（优化轮）七项在当时已实施并验收：自动化回归（pytest 203 绿、前端 build/lint/diff 6/6）、MCP 零成本验证、付费真跑和浏览器视觉验收。对应历史证据保留在本目录的 [`VERIFICATION.md`](VERIFICATION.md)；不得用这些数字证明当前版本。

浏览器验收过程还抓出并修复了两个真实缺陷(都已重跑回归):
1. 回边分类把环上的无条件边也判成回边——修正为「带 `when` 且回到祖先」
   才算回边(与引擎的条件路由语义一致);
2. END 节点缺入边句柄导致 →END 的边一直被静默隐藏(存量缺陷,
   第四轮的 pass→END 其实从未画出来过)。

## 二、本轮交付摘要

- **P1 后端覆盖契约**:`prompt`(所有节点类型,完整替换语义)与
  coding_agent 的 `workdir` 进入 `node_overrides` 白名单;覆盖摘要脱敏
  (只记 chars+sha256 前缀),全文只在 `runs/<id>/spec.snapshot.json`;
  `consumes`/权限/拓扑永远不可覆盖。preview/dry_run 返回
  `prompt_overridden` 清单与 `param_defaults`(真实生效默认值)。
- **P2 前端参数区**:fallback 有序 chips(↑/↓ 排序、结构上不可能重复或
  包含主模型、跨厂商分组);prompt 覆盖文本域(并排对照 YAML 原文、
  恢复继承);workdir 覆盖;数字框留空灰显后端默认值;每个参数 hover 说明。
- **P3 产物工作台**:`DiffWorkSpace` 泛化为 `ArtifactWorkSpace`(按
  media_type 分派);报告/JSON/投影都加「放大查看」;`TextViewer` 增
  `fill` 模式;内嵌预览 320px 改 `clamp()` 自适应。
- **P4 示例 8→6**:删 `fix-calculator`、`sqlite-checkpoint-analysis` 与
  `scripts/run_demo_fix.py`,引用按清单清理(只剩历史证据段落);
  两张并行图文案差异化。
- **P5 回边渲染**:节点补底部 source/顶部 target 专用句柄,回边
  smoothstep 绕行,补齐从未生效过的 `.edge-back` 虚线样式。
- **P6 验收**:pytest 203 绿、前端 build/lint/diff 6/6、MCP dry_run
  prompt 覆盖可见、付费真跑 `20260818-134023-c5d183`(repair→pass 两轮
  收敛、9/9 sidecar 哈希、prompt 覆盖进投影)。

## 三、本轮真跑过程发现的真问题(已修,留痕)

1. **示例 reviewer prompt 的 JSON 字段歧义**:
   `proposal-review-repair-loop` 的原 prompt 把 `severity` 写得像每条
   issue 的属性,三个不同模型(Deepseek v4-flash/v4-pro、qwen3.8-max)
   全都漏掉顶层 `severity` 字段。这不是模型问题,是 prompt 问题。
   已改 YAML:三个顶层字段显式列出。同类隐患:`code-change-review-approve`
   的 reviewer prompt 风格类似但字段名明确,暂未动——下次真跑它时留意。
2. **qwen3.8-max + thinking: medium 输出非 JSON**(两次独立复现)。
   已把该示例 reviewer 节点预置的 `thinking: medium` 移除(思考档位用户
   仍可在节点里自选)。
3. **不收敛被大声拦停是正确行为**:`20260818-132825-3eb8d4` 两次
   repair 后第 3 次执行被 `max_iterations=2` 拒绝(GuardViolation),
   账本明说"循环未收敛,停止"。这些失败 run 全部留在 `runs/` 里。

## 四、下一轮:节点间通讯文件层

设计已完成:`docs/DESIGN-node-io-files.md`,分三阶段:

- **阶段 A 多产物基座**:llm 节点 `outputs` 声明 + `atlas:file` 围栏块
  解析 + `data` 角色 + 前端产物页签动态化。
- **阶段 B agent 产物回流**:`collect_files` 从隔离副本的 `.atlas-out/`
  收集(路径安全校验)。
- **阶段 C 人的材料入口**:运行请求 `attachments` → `attachment.<名字>`
  产物,加载期校验引用完整性。

红线提醒:`outputs`/`collect_files` 属于产物契约,**不进** `node_overrides`
白名单(它们决定下游接线);判据是"会不会影响接线——影响接线的只能改 YAML"。
P3 的 ArtifactWorkSpace 与 P2 的"后端给有效值、前端只显示"模式会被直接复用。

## 五、已验证边界(红线现状)

- YAML 仍是图真相;没有任意代码节点,没有画布结构编辑。
- 覆盖白名单:llm 九项(+prompt)、agent 四项(+workdir 仅 coding_agent)、
  human 仅 prompt;`consumes`/`outputs`/拓扑/节点类型永不进白名单。
- Web 仍只监听 `127.0.0.1:8321`,写接口要 `X-Atlas-Request` 头。
- `config/.env` 未读取到输出、未写入响应或文档。
- 所有新增拒绝路径都在 run_id 分配之前(校验先于花钱)。

## 六、后续非本轮 blocker

- `pricing.json` 费率仍为 `null`,成本列与成本守卫待确认价格。
- Windows `icacls` 子进程测试编码 warning(既有,不影响结果)。
- 长时间运行资源泄漏、固定 seed 可复现性、10 张幻觉 YAML 批量拒收。
- `code-change-review-approve` 尚未真跑过(它是唯一含 agent+human 的图,
  真跑它一次可以把 workdir 覆盖与 human 门也一起验收)。
