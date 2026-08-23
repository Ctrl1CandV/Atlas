# RFC · agent 自动 retry 的预算约束(草案,待评审)

> 状态:2026-08-23 草案。**决策前不改任何默认行为**(硬性纪律:自动 retry
> 放大 agent 花销的策略必须先经评审,不能静默改)。背景:2026-08-19 阶段 D
> 的 $10.508 事故——Kiro agent 首次 attempt 自报高额花销,自动 retry 紧随
> 其后,人工强制终止才止损。

## 问题

`coding_agent`/`research` 节点的 `retry` 参数允许 agent 失败后自动重跑。
agent 单次 attempt 的花销可以比 LLM 节点高几个数量级(CLI 自主循环,
token 量不可预测)。当前 retry 与预算的关系没有任何约束:

1. pricing 全 `null`(本机现状)时,`max_cost_usd` 对 agent 的费率投影是
   未知 → 按"剩余预算全额"保守预留;首次 attempt 结算后,retry 常常仍能
   通过(结算把预留转为已花,剩余为 0 时下一个 attempt 会被拦——但若首次
   结算金额本身被低估,放大已经发生)。
2. 图作者显式设了 `retry: N` 却没设 `max_cost_usd` 时,没有任何机制阻止
   N 次全额重跑。
3. Claude CLI 的 `--max-budget-usd` 映射尚未实现(P2 范围),agent 进程
   侧没有第二道闸。

## 选项

### A. agent 节点默认 `retry=0`(推荐起点)

未显式写 `retry` 的 agent 节点一律不自动重跑;要 retry 必须在 YAML 里
显式声明,并在 dry-run 警告里如实显示"该 agent 节点将自动重跑至多 N 次"。

- 优点:零配置下绝无放大;显式声明把放大风险变成图作者的决定。
- 缺点:瞬时故障(网络/CLI 启动失败)也要人工重跑;改变现有显式依赖
  retry 的图的行为(需要 major-minor 版本说明)。

### B. 只有存在可执行预算时才允许 retry

`retry > 0` 的 agent 节点在准入时要求:(a) 图设了 `max_cost_usd`,且
(b) agent 模型费率已知,或 CLI `--max-budget-usd` 可传递且生效。

- 优点:retry 永远受可执行上限约束,语义最强。
- 缺点:pricing 全 null 的本机现状下等于禁用所有 agent retry(与 A 的
  实际效果趋同,但拒绝点在准入而非执行);依赖 B 项前置(预算映射)落地。

### C. 现状 + 警告

dry-run 对 `retry > 0` 且无可执行预算的 agent 节点发醒目警告,不阻止。

- 优点:零行为变化。
- 缺点:只是提示;事故重演路径完整保留。

## 建议

分两步:**先 C 后 A**。本批(P2)已落地 C 的基础设施(dry-run 警告框架
与 A2/C1 同源);A 作为下一个 minor 版本的行为变更,在 CHANGELOG 与
示例 YAML 中同步声明。B 与 Claude CLI `--max-budget-usd` 映射(ROADMAP
P2 实施合同第 5 条)一起实施,作为 A 的强化路径。

## 开放问题(评审时定)

1. A 落地时,已保存的旧图(未写 retry 的 agent 节点)按旧语义跑还是
   拒绝?(倾向:按旧语义跑 + 警告,图版本语义随快照冻结。)
2. `--max-budget-usd` 的金额是节点级还是 run 级分摊?(倾向:节点级,
   取该节点 retry 总次数 × 单次预估的上界,做不到就 fail-loud。)
3. agent 的 usage 上报不可信时(CLI 没回 usage),结算按什么口径?
   (倾向:按预留全额计入,已知限制如实文档化。)
