# MCP 与人工审批

## 接入 harness

推荐方式：`uv run atlas-web` 单进程同时提供 Web 界面与 MCP streamable-http 端点，在 harness 里填 `http://127.0.0.1:8321/mcp` 即可（Claude Code 用 `claude mcp add --transport http atlas http://127.0.0.1:8321/mcp`；ZCode/Cursor 的 JSON 配置见 `docs/mcp.md`）。

备用方式：不想保持 `atlas-web` 运行时,可显式配一条 stdio 条目（`uv --directory <ATLAS_HOME> run atlas-mcp`，`<ATLAS_HOME>` 替换为 Atlas 源码目录的绝对路径，配置文件不会展开占位符）。仓库自带的 `.mcp.json` 已指向 HTTP 端点,不再使用 stdio（随仓库分发相对路径的 stdio 配置在仓库外拉子进程时必坏）。

两个入口共用同一份工具实现。每个客户端会话选一种入口，不要为同一连接同时接 stdio 子进程与 HTTP 端点。

## 八个工具

1. `atlas_validate_workflow`：校验 YAML 或保存的 workflow id；语法和语义错误在有源码位置时带字段路径、行与列。
2. `atlas_save_workflow`：保存已校验 YAML；更新需 `expected_sha256`。
3. `atlas_run_workflow`：`dry_run: true` 只渲染；`false` 才执行支持的节点。`workflow_id` 与 `yaml` 二选一——传 `yaml` 全文即可直接运行自定义图，不写 workflows/；`persist_as` 在真实运行结束后把它固化为已保存工作流。`wait=false` 通过全部预检后立即返回 run_id（长任务不再占住会话），用 `atlas_get_run` 轮询；与 `persist_as` 互斥。
4. `atlas_list_workflows`：列出工作流和校验状态。
5. `atlas_list_runs`：按 run_id 降序分页列出运行（`limit`/`cursor`），状态含 running/interrupted/paused/done/failed/cancelled（starting 只在落账前的短暂窗口由 Web 单 run 查询可见）。
6. `atlas_get_run`：查询已创建运行的动态状态与产物位置。
7. `atlas_cancel_run`：请求协作式取消——running 的运行在下一节点边界终止（在途模型调用与 agent CLI 执行会跑完当次）；paused/interrupted 直接落 `cancelled` 终态；幂等，终态运行返回冲突。请求一经写入不可撤回：若控制器随后死亡，恢复会在首个节点边界消费该请求（等效恢复后立即取消）。
8. `atlas_resume_run`：仅恢复事件仍为 running、没有活跃控制器且稳定 OS run lock 可取得的 interrupted 运行。

固定顺序是 validate → save（需要时）→ dry-run → 人工确认 → run → get-run（或 wait=false 后轮询 / list-runs）。自定义图优先走 `yaml` 参数而不是直接写文件。运行因控制器退出而显示 interrupted 时才使用 resume；不要跳过零成本预演。取消用 `atlas_cancel_run`（HTTP API 同款端点 `POST /api/runs/{id}/cancel`；Web 界面按钮属后续批次）。

## Human 节点

`human` 节点在 Web 界面暂停，等待批准或驳回。批准记录可被下游消费；驳回应终止相应路径。人需要审阅真实上游输出，不能把节点标题当成证据。

paused 与 interrupted 不是同一状态：paused 只能走现有摘要与完整性校验后的批准/驳回，`atlas_resume_run` 不能绕过 human gate。

→ [沙箱、安全与隐私](#/guide/safety)
