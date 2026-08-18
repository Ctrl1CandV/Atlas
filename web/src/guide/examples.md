# 六个随附示例

所有示例都不预填用户机器的模型。六个文件都应能静态校验和 dry-run；真实运行需要完整模型绑定，含 agent 的图还要求 `config/agents.json` 显式设置 `runner: local_cli`。

1. `parallel-research-synthesis`：三个 LLM 分支并行分析后综合；文件名的 research 表示任务形态，不是 `research` 节点。
2. `multi-vendor-debate-judge`：辩论拓扑；只有实际绑定到真实不同供应商/底模后才可称为独立意见。
3. `proposal-review-repair-loop`：作者、审查者与有上限的条件回路。
4. `human-approval-pipeline`：草稿、人工门和终稿。
5. `code-change-review-approve`：coding-agent 在 worktree 副本中实施修改，审查由冻结 baseline/result 普通文件字节清单生成的完整文本 unified diff，有限回修后进入人工门。Atlas 不写原目录；二进制变更 fail-loud，审批绑定 baseline/result/patch 三类摘要；运行要求 agent 模型供应商具有 `anthropicBaseUrl` 与当前凭据。
6. `map-reduce-document-analysis`：多个 LLM 视角分析同一材料后汇总。

Agent worktree 不是 OS 沙箱，Claude CLI 与当前用户权限相同。选择原则：单次调用足够时不要加图；审查节点必须消费真实待审产物；新图总是先 validate 和 dry-run。

→ [MCP 与人工审批](#/guide/mcp-human)
