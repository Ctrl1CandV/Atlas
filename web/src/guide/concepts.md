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
- `human`：暂停并等待本机界面的批准或驳回；可选 `approval_mode: routed`（P11）解锁第三决策「要求修改」：必填非空意见，经校验期强制的 `when: __changes__` 回边进入修订节点（消耗 max_iterations），修改要求以 write-once `<节点id>.changes` 产物供修订节点消费；默认 binary 只认批准/驳回。
- `search`（E-1）：调用 Atlas 自持检索后端，不调模型（写了 `model` 会被校验期拒绝）。后端封闭枚举 `backend: tavily`（需 `TAVILY_API_KEY`）| `searxng`（需 `ATLAS_SEARXNG_BASE_URL`），缺失在预检位响亮拒绝（dry-run 同样拦截）。查询词三级来源：显式 `queries`（≤5，超出校验期拒绝）→ 上游产物 JSON 顶层 `queries` 数组（截断至 5 并记 `truncated_queries`）→ 整段 prompt 单查询。每次执行落 `search_performed` 事件 + write-once JSON 产物；`cost_usd` 只取后端实报或 null，绝不冒充 $0；设了 `max_cost_usd` 时派发前保守预留剩余预算。结果是**不可信外部素材**：下游投影强制 `<untrusted-source>` 围栏 + 系统说明 + 闭合标签转义（`<\/untrusted-source>`），prompt 注入样本有固定测试。域名过滤只看检索 API 返回的初始 URL host（`https://arxiv.org@evil.com/` 的 host 是 evil.com，按 host 解析过滤）；不追重定向，短链可能掩盖最终落地页——如实限制。后端网络/HTTP 失败是内容类失败，可用 `on_error: stop|continue|branch` 与 `<node>.error`；取消在每个 query 边界消费；`timeout_s` 覆盖整批查询。检索产物不进 P7 skip/P13 合成导入（旧搜索结果冒充新执行=造假）；显式 imports 合法但 untrusted 围栏随导入转发。

条件路由按 `route_field` 查找边的 `when`；该字段必须列入 `output_schema.required`，prompt 必须明确合法值。环必须有条件出口和 `max_iterations`。`llm` 节点可声明 `on_error: stop|continue|branch`（默认 stop）：内容类失败（候选全部失败）可让图继续（continue，不能带条件出边），或走 `when: __failed__` 的失败分支（branch，校验期必须接线，下游可消费 `<node>.error`）；费用、守卫、取消、完整性等治理异常任何策略都不可吞。

## 运行附件（E-2A）

发起运行时可携带 `attachments: [{name, path}]`（MCP `atlas_run_workflow` 参数或界面运行请求）。`path` 是本机绝对路径，启动准入时一次性整读：名字全小写 ASCII（大写变体与同形 unicode 字符刻意拒绝）、不得以 `.output/.diff/.error/.changes` 结尾、不得叫 `task` 或撞节点 id；单件 ≤16 MiB、合计 ≤32 MiB；任一失败在分配 run_id 之前整体拒绝——不存在"带一半附件"的运行。通过后字节克隆进 run 的 write-once 产物库（原子写 + 写后哈希复验），账本只记 name/sha256/大小/基名，绝不记源路径，响应也不回传。下游节点用 `consumes: [附件名]` 显式消费；投影里只有一行摘要（名字 · 大小 · sha256 前 12 位），原字节经产物工作台查看——大材料不占 prompt 预算，审批材料面板天然可见。运行期附件缺失是投影期显式失败，不是加载期（附件是运行参数，不是图结构）。

## Agent 字段事实

生产执行要求 `config/agents.json` 显式 `runner: local_cli`，且所选模型的供应商配置 `anthropicBaseUrl` 与当前凭据；默认 fail-closed。`allow_web` 默认 `false`，开启时只增加 `WebSearch`/`WebFetch`，不是网络隔离；coding `Bash` 仍可能联网。当前 Claude CLI 不支持硬轮次参数，因此 `max_turns` 是保留的规格元数据，硬限制由 deadline 和已配置预算承担。

research/coding_agent 节点缺省不自动重跑（retry 缺省 0）；显式声明 `retry: N` 后，dry-run 必须出现放大风险警告——每次重跑都会把整份 CLI 开销原样复制，警告会说明重跑次数与成本约束有无（未设 `max_cost_usd` 就是没有任何总量约束）。

## workdir 事实

解析器要求目录已存在，但不会展开 `${ATLAS_HOME}`。示例的相对 `demo-project` 仅在从源码根目录启动时解析正确；其他场景用本次运行覆盖传入绝对路径。Atlas 不写原目录；执行前冻结 baseline，随后比较 baseline/result 的普通文件字节清单并生成完整文本 unified diff。采集不执行 Git add/filter/hook/attributes/textconv/external diff；二进制变更 fail-loud，审批绑定 `baseline_digest`、`result_digest` 与 `patch_digest`。

该副本不是 OS 沙箱。Claude CLI 与 Atlas 同用户运行，理论上可访问或攻击当前用户可访问的其他宿主路径。

→ [示例](#/guide/examples) · [模型与覆盖](#/guide/models)
