# Benchmark 优化计划：关闭与迁移记录

状态：**原计划已关闭；2026-08-19 迁移到当前路线图。**

已实施：P0min（LLM reservation 持久化）、P1（动态 interrupted 与受控恢复）、P6（YAML path/line/column）。

保留但未实施：P2、P3、P4、P7、P9、P10、P11、P13。它们的价值、缺口、依赖、推荐顺序、实现合同和验收标准统一见 [`ROADMAP.md`](ROADMAP.md)。推荐首批是 P4 shared launcher/MCP async，然后 P2 cancel/成本停损，而不是并行复制两套运行控制逻辑。

已移除、不排期：P5、P8、P12、P14。没有新的真实需求与独立 RFC，不从历史计划复活。

原始竞品调研、P0–P14 设计推演和阶段性数字保存在 [`archive/PLAN-benchmark-optimizations-2026-08-19.md`](archive/PLAN-benchmark-optimizations-2026-08-19.md)。其中关于 HEAD、测试数、tag、发布与阶段 D 的陈述只反映写作过程中的时间点，不是当前产品事实。
