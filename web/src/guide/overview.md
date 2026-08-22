# Atlas 概览

Atlas 是 Windows 本机、YAML 驱动的可审计工作流引擎。MCP 工具负责校验、保存、预演和运行，Web 界面负责观察已记录的结构、状态、输入、输出与产物。

> 产品版本 `0.1.0`，Python 版本 `0.1.0`，tag `v0.1.0`。仅提供源码，不发布 PyPI 包或预编译安装器。

## 当前可以依赖的能力

- `llm`、`research`、`coding_agent` 节点，线性/并行/汇合、条件路由和有界循环；
- 六个 MCP 工具的校验、保存、dry-run、运行、查询与 interrupted-only 恢复流程；
- 动态 interrupted 状态、Web 恢复入口，以及保持独立语义的 `human` 审批门；
- YAML 语法/语义错误的字段路径与行列、输出结构检查、降级记录、有效规格快照与产物哈希；
- 显式配置后的 Claude CLI agent 执行，以及冻结 baseline/result 普通文件字节清单比较生成的完整文本 unified diff；采集不执行 Git add/filter/hook/attributes/textconv/external diff，二进制变更 fail-loud，审批绑定 baseline/result/patch 三类摘要。

## Agent 执行边界

只有 `config/agents.json` 明确设置 `runner: local_cli` 时才启用生产 agent；缺少配置或预检条件不完整会在创建 run 前 fail-closed。Agent 模型的供应商必须配置 `anthropicBaseUrl` 和当前凭据。

Claude CLI 是当前用户身份下的宿主进程，不是 OS 沙箱。可写 coding agent 使用完整 worktree 副本；Atlas 不写原目录，但进程理论上可访问当前用户可访问的其他宿主路径。`allow_web` 默认关闭且只控制 `WebSearch`/`WebFetch`，不能隔离 coding `Bash` 的网络访问。`max_turns` 当前只是规格字段，硬限制来自 deadline 与已配置预算。

Ubuntu CI 只是兼容性信号，不代表支持平台。

→ [安装、升级与启动](#/guide/quickstart)
