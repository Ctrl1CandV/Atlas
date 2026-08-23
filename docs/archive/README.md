# Atlas 历史文档索引

本目录只保存历史快照、旧计划、调研和验证日志。**它们不是当前产品、发布或安全合同。** 路径、链接、工具数、测试数和状态可能已经过时。

当前入口：

- [`../STATUS.md`](../STATUS.md)：当前版本、能力、限制和发布事实。
- [`../ROADMAP.md`](../ROADMAP.md)：尚未实施项目的顺序与验收标准。
- [`../release-v0.1.0.md`](../release-v0.1.0.md)：v0.1.0 tag、资产和 provenance。
- [`../rfcs/node-io-files.md`](../rfcs/node-io-files.md)：仍处于 proposed 状态的节点通讯文件 RFC。

| 文件 | 时间/范围 | 使用方式 |
|---|---|---|
| `ARCHITECTURE.md` | 早期 v1 架构快照 | 仅解释最初分层；四工具、观察-only Web 等已过时 |
| `PLAN-v2.md` | 2026-08-16/17 的 M3–M5 计划与决定 | 历史决策，不代表当前 backlog |
| `PLAN-v3.md` | 2026-08-17 的原始提案及后续实施追加 | 提案头与后部完成记录并存，已 superseded |
| `DESIGN-round5-optimizations.md` | 2026-08-18 第五轮设计 | 已实施历史；链接和视觉验收状态可能过时 |
| `HANDOFF-next-round.md` | 2026-08-18 当时的交接 | 已被当前 STATUS/ROADMAP 取代 |
| `VERIFICATION.md` | 多轮历史验证日志 | 只证明当时源状态，不证明当前 HEAD |
| `RESEARCH.md` | 2026-08-16 外部调研 | 时间敏感，外部结论需重新核验；本机路径已脱敏 |
| `PLAN-rc1-followup-2026-08-19.md` | v0.1.0 收尾全过程 | 中间“待执行”状态已失效，保留审查历史 |
| `PLAN-benchmark-optimizations-2026-08-19.md` | benchmark 调研及旧 P0–P14 路线 | 当前采用/移除状态以 ROADMAP 为准 |

归档纪律：

1. 不从归档复制“当前”“已通过”“待发布”等状态到活动文档，必须先核对代码、Git 与 Release。
2. 归档中的本机绝对路径、密钥、运行内容不能公开；发现后应脱敏，而不是依赖 Git ignore。
3. 新实现的验收证据应进入对应测试、CHANGELOG 或独立 release/status 记录，不再无限追加到旧计划。
