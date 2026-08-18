# 故障排查

## 模型或供应商未配置

从 `config/*.example.json` 创建被忽略的本机副本，在 `config/.env` 设置对应凭据。示例故意留空模型；用节点详情或本次运行覆盖绑定模型。Agent 模型还要求供应商配置 `anthropicBaseUrl`。不要修改示例来保存密钥。

## 校验失败

常见原因：未知字段/类型、`consumes` 引用不存在产物、条件值不匹配、环缺入口/出口/`max_iterations`、`workdir` 不存在。校验和 dry-run 不调用供应商。

## Agent 报 AGENT_RUNNER_DISABLED 或预检失败

生产 agent 默认 fail-closed。仅在确认同用户、非 OS 沙箱的风险后，复制 `config/agents.example.json` 到被忽略的 `config/agents.json`，并显式设置 `runner: local_cli`。还需 Claude Code 2.1.0+、已绑定 agent 模型、该供应商的 `anthropicBaseUrl`、模型 allowlist 和当前凭据。不要通过放宽工具或环境注入绕过预检。

`allow_web: true` 只增加 `WebSearch`/`WebFetch`，不是网络隔离；coding `Bash` 仍可能联网。`max_turns` 当前不会传给 Claude CLI 形成硬轮次限制，请使用 `timeout_s`、整图 deadline 和已配置预算。

## 成本未知或候选全部失败

未知价格时不要依赖 `max_cost_usd`。检查本机 allowlist、凭据、端点、每次失败和输出结构；空响应、截断或缺字段也会失败。

## Web 变更未出现

```powershell
npm --prefix web ci
npm --prefix web run build
uv run python -m atlas.web
```

确认 Node.js 至少为 22.12，并从源码根目录启动。

## 安全问题

不要公开提交密钥、私有路径、prompt、运行目录或漏洞细节。按根目录 `SECURITY.md` 请求私密报告渠道。

→ [开发者构建](#/guide/development)
