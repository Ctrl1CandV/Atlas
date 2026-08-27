# YAML 字段与图结构

## 顶层字段

| 字段 | 含义 |
|---|---|
| `name` | 必填工作流名称 |
| `description` | 可选说明 |
| `meta` | 展示元数据：title/kind/category/tags/estimated_calls/requires/example_task |
| `nodes` | 节点数组 |
| `entry` | 可选入口 id 或 id 数组；有环时必须显式给出 |
| `edges` | `from`、`to`，条件边另有 `when` |
| `guards` | `max_iterations`、`max_cost_usd`、`timeout_s` |
| `summary` | 可选总结配置 `{model, prompt_hint?}`：run 结束前用指定模型做一次回顾调用，产物与成本进账本（S1）；默认不总结 |
| `fork` | 可选 `{run: <源 run id>}`（P13）：从静稳终态 run 再跑这张（通常改过的）图。Atlas 静态比较两侧调用身份得出 changed 集，changed + 全部后代构成失效闭包（循环按强连通分量整体失效，join 命中 changed 分支必重跑）；闭包外且源事件证明产物完整的节点自动合成导入、走与显式 imports 相同的准入与身份复核链。闭包内不允许声明 imports。计划以 `fork_planned` 事件全量入账（changed/closure/import map），dry-run 预演可见 |

## 节点字段

所有节点需要 `id`、封闭 `type`、非空 `prompt` 和 `consumes`。`consumes` 接受 `task`、`<node>.output`、`<coding-node>.diff`，以及 `on_error: branch` 节点的 `<node>.error`（软失败的错误上下文产物）。

- `llm`：`model`、`fallback`、`thinking`、`max_output_tokens`、`temperature`、`seed`、`timeout_s`、`retry`、`output_schema`、`route_field`、`on_error`、`imports`（从终态 run 字节导入上游产物,身份全等时免费复用）。
- `research`：agent 模型与 `max_turns`、`allow_web`、`allowed_paths`、`timeout_s`、`retry`。
- `coding_agent`：agent 字段以及必填 `workdir`、`writable`；`allowed_paths` 仅在 `writable: false` 时合法。可写节点比较冻结 baseline 与 agent 结果的普通文件字节清单，生成完整文本 unified diff。
- `human`：暂停并等待本机界面的批准或驳回。

条件路由按 `route_field` 查找边的 `when`；该字段必须列入 `output_schema.required`，prompt 必须明确合法值。环必须有条件出口和 `max_iterations`。`llm` 节点可声明 `on_error: stop|continue|branch`（默认 stop）：内容类失败（候选全部失败）可让图继续（continue，不能带条件出边），或走 `when: __failed__` 的失败分支（branch，校验期必须接线，下游可消费 `<node>.error`）；费用、守卫、取消、完整性等治理异常任何策略都不可吞。

## Agent 字段事实

生产执行要求 `config/agents.json` 显式 `runner: local_cli`，且所选模型的供应商配置 `anthropicBaseUrl` 与当前凭据；默认 fail-closed。`allow_web` 默认 `false`，开启时只增加 `WebSearch`/`WebFetch`，不是网络隔离；coding `Bash` 仍可能联网。当前 Claude CLI 不支持硬轮次参数，因此 `max_turns` 是保留的规格元数据，硬限制由 deadline 和已配置预算承担。

## workdir 事实

解析器要求目录已存在，但不会展开 `${ATLAS_HOME}`。示例的相对 `demo-project` 仅在从源码根目录启动时解析正确；其他场景用本次运行覆盖传入绝对路径。Atlas 不写原目录；执行前冻结 baseline，随后比较 baseline/result 的普通文件字节清单并生成完整文本 unified diff。采集不执行 Git add/filter/hook/attributes/textconv/external diff；二进制变更 fail-loud，审批绑定 `baseline_digest`、`result_digest` 与 `patch_digest`。

该副本不是 OS 沙箱。Claude CLI 与 Atlas 同用户运行，理论上可访问或攻击当前用户可访问的其他宿主路径。

→ [示例](#/guide/examples) · [模型与覆盖](#/guide/models)
