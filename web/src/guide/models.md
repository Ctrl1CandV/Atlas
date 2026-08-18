# 模型、fallback 与本次运行覆盖

示例不会静默选择模型。模型为空时可校验和预演，真实运行会在创建 run 前拒绝。候选模型只有加入本机供应商 allowlist 后才能使用；agent 模型还要求该供应商配置 `anthropicBaseUrl` 和当前凭据。

`fallback` 是有序尝试链。关键节点宜绑定跨供应商备选，但名称不同不一定代表底模独立；必须按实际配置判断。

思考控制分为能力、请求和响应证据。网关接受参数不证明模型改变了推理深度；能力和价格都应来自本机验证，不应猜测。

## 本次运行覆盖

- `llm`：`model`、`fallback`、`thinking`、`max_output_tokens`、`temperature`、`seed`、`timeout_s`、`retry`、`prompt`
- `research`：`model`、`max_turns`、`timeout_s`、`retry`、`prompt`
- `coding_agent`：agent 字段加 `workdir`
- `human`：`prompt`

`prompt` 是完整替换。覆盖不会修改 YAML，也不能改变拓扑、`consumes`、输出声明或权限字段。Agent 覆盖不会启用生产 runner；仍需 `config/agents.json` 显式 `runner: local_cli`，缺失时默认 fail-closed。

当前 Claude CLI 没有硬 `max_turns` 参数；该字段保留为规格元数据，硬限制由 deadline 与已配置预算承担。`allow_web` 默认关闭，只控制 `WebSearch`/`WebFetch`，不隔离 coding `Bash` 的网络访问。

→ [读取运行结果](#/guide/results)
