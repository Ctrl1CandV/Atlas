# MCP 与人工审批

## 接入 harness

建议把 Atlas 的 stdio server 直接配置进 harness，由客户端按需启动。ZCode、Cursor 与 Claude Code 的完整配置见 `docs/mcp.md`，三者统一使用：

```text
uv --directory <ATLAS_HOME> run atlas-mcp
```

必须把 `<ATLAS_HOME>` 替换为 Atlas 源码目录的绝对路径；配置文件不会展开该占位符或 `${ATLAS_HOME}`。仍可在 Atlas 源码目录手动运行 `uv run atlas-mcp`，并在客户端连接期间保持该终端运行。

## 六个工具

1. `atlas_validate_workflow`：校验 YAML 或保存的 workflow id；语法和语义错误在有源码位置时带字段路径、行与列。
2. `atlas_save_workflow`：保存已校验 YAML；更新需 `expected_sha256`。
3. `atlas_run_workflow`：`dry_run: true` 只渲染；`false` 才执行支持的节点。
4. `atlas_list_workflows`：列出工作流和校验状态。
5. `atlas_get_run`：查询已创建运行的动态状态与产物位置。
6. `atlas_resume_run`：仅恢复事件仍为 running、没有活跃控制器且稳定 OS run lock 可取得的 interrupted 运行。

固定顺序是 validate → save（需要时）→ dry-run → 人工确认 → run → get-run。运行因控制器退出而显示 interrupted 时才使用 resume；不要跳过零成本预演。

## Human 节点

`human` 节点在 Web 界面暂停，等待批准或驳回。批准记录可被下游消费；驳回应终止相应路径。人需要审阅真实上游输出，不能把节点标题当成证据。

paused 与 interrupted 不是同一状态：paused 只能走现有摘要与完整性校验后的批准/驳回，`atlas_resume_run` 不能绕过 human gate。

→ [沙箱、安全与隐私](#/guide/safety)
