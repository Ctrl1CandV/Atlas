# 安装、升级与启动

## 环境

Windows 10/11 x64、Python 3.12、Git、[uv](https://docs.astral.sh/uv/)、Node.js 22.12+ 与 npm。Atlas 未发布到 PyPI；请使用可信源码压缩包或已有源码检出，本文不猜仓库 URL。

## 安装

在源码根目录运行：

```powershell
uv sync --locked --all-groups
npm --prefix web ci
npm --prefix web run build
```

首次启动 `atlas-web` 或 `atlas-mcp` 时，Atlas 会从通用模板创建所有缺失的本机配置；已有文件永不覆盖。也可以先显式运行：

```powershell
uv run atlas init
```

只编辑被忽略的运行文件。密钥只放 `config/.env`，未知价格保持 `null`；自动生成的 `agents.json` 默认保持 `fail_closed`。

## 升级

替换或更新源码，阅读 `CHANGELOG.md`，比较新版 `config/*.example.json`，然后重新执行 locked sync、`npm ci` 和 Web build。不要用示例文件覆盖活动配置。

## 启动

```powershell
uv run atlas-web
```

一条命令同时提供 Web 界面（`http://127.0.0.1:8321`）与 MCP streamable-http 端点（`http://127.0.0.1:8321/mcp`）。harness 里填该 URL 即可连接六个 MCP 工具，无需另开终端。

备用方式：仓库根目录自带 `.mcp.json`，Claude Code 等读取项目级 MCP 配置的客户端会通过 stdio 自动拉起 `atlas-mcp` 子进程。不要绑定或代理到外部接口。

## 第一次预演

为所有 LLM 与 agent 节点明确选择模型，然后按 `validate` → `save`（需要时）→ `dry_run` → 人工确认 → 真实运行。校验和 dry-run 不调用供应商，也不创建运行目录。Agent 真实执行还要求本机显式启用 `local_cli`，并通过 CLI、Anthropic 兼容端点与凭据预检。

→ [YAML 字段](#/guide/concepts) · [MCP 与人工审批](#/guide/mcp-human)
