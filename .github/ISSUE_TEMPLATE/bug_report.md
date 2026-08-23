---
name: Bug 报告
about: 报告 Atlas 的异常行为(校验、运行、恢复、Web、MCP)
title: "[bug] "
labels: bug
---

## 现象

一句话描述哪里不符合预期。

## 复现步骤

1. 使用的 workflow YAML(可脱敏,但请保留节点类型/边/守卫结构)
2. 执行的命令或 MCP 调用(validate / dry_run / 真跑 / resume / approve)
3. 看到的结果

## 证据

- run_id 与 `runs/<run_id>/events.jsonl` 中的相关事件(至少含 run_started 与失败/异常事件);**不要粘贴完整账本或产物内容**——它们可能包含你的 prompt、源码与路径。
- 报错原文(含 YAML path/line/column,如果有)。

## 环境

- Windows 版本、Python 版本、Atlas 版本(tag 或 commit)
- 供应商与模型(可只写厂商名)
- 是否启用了生产 agent(`config/agents.json` 的 `runner: local_cli`)

## 补充

- 涉及凭据、密钥或私有路径的问题请先阅读 [SECURITY.md](../../SECURITY.md),不要在 issue 里粘贴 `config/.env` 或活动配置。
