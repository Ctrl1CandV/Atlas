# MCP harness setup / MCP harness 接入

Atlas exposes its eight MCP tools two ways:

1. **Streamable HTTP (recommended)** — the tools live inside the Web process. Start `atlas-web` once and point your harness at `http://127.0.0.1:8321/mcp`. One command, no extra terminal.
2. **stdio** — the harness launches `atlas-mcp` as a child process via project `.mcp.json`. Zero-config after clone, but the tool calls block synchronously in that child.

Both entries share the same tool implementations; behavior is identical. `atlas_resume_run` resumes only a dynamically confirmed interrupted run; a paused human gate must still be approved or rejected in Web.

Atlas 通过两种方式暴露八个 MCP 工具：

1. **Streamable HTTP（推荐）**——工具内嵌在 Web 进程里。启动一次 `atlas-web`，在 harness 里填 `http://127.0.0.1:8321/mcp` 即可。一条命令，不用另开终端。
2. **stdio**——harness 通过项目 `.mcp.json` 把 `atlas-mcp` 作为子进程拉起。clone 后零配置，但工具调用会在该子进程里同步阻塞。

两个入口共用同一份工具实现，行为一致。`atlas_resume_run` 只恢复动态确认的 interrupted 运行；暂停在 human gate 的运行仍必须在 Web 中批准或驳回。

## Option 1: Streamable HTTP / 方式一：Streamable HTTP

Start Atlas (this also serves the Web UI at <http://127.0.0.1:8321>):

先启动 Atlas（同时提供 Web 界面）：

```powershell
uv run atlas-web
```

Then register the URL in your harness. The server must keep running while the harness is connected; everything stays on loopback.

然后在 harness 里登记该 URL。连接期间保持 atlas-web 运行；一切只绑回环地址。

### ZCode

Add to `.zcode/config.json` (workspace) or the user configuration:

加到工作区 `.zcode/config.json` 或用户配置：

```json
{
  "mcp": {
    "servers": {
      "atlas": {
        "type": "http",
        "url": "http://127.0.0.1:8321/mcp"
      }
    }
  }
}
```

Restart the session, then check **Settings → MCP** for the `atlas` connection and its eight tools.

重启会话后，在 **Settings → MCP** 检查 `atlas` 连接及八个工具。

### Claude Code

```powershell
claude mcp add --transport http atlas http://127.0.0.1:8321/mcp
```

Or add a project-scoped entry to `.mcp.json`:

或在项目的 `.mcp.json` 里加：

```json
{
  "mcpServers": {
    "atlas": {
      "type": "http",
      "url": "http://127.0.0.1:8321/mcp"
    }
  }
}
```

### Cursor

Add to `.cursor/mcp.json`:

加到 `.cursor/mcp.json`：

```json
{
  "mcpServers": {
    "atlas": {
      "url": "http://127.0.0.1:8321/mcp"
    }
  }
}
```

Reload Cursor and confirm the server is enabled in its MCP settings.

重新加载 Cursor，并在 MCP 设置中确认该 server 已启用。

## Option 2: stdio / 方式二：stdio

A git clone ships a generic [`.mcp.json`](../.mcp.json) at the repo root, so opening the cloned repository in Claude Code (or any harness with project-level MCP config) auto-starts the eight tools over stdio — no manual terminal needed. The configured command is `uv --directory . run atlas-mcp`, working directory = repo root; complete `uv sync --locked --all-groups` first. The published v0.1.0 sdist does not include this file; the next release's manifest does.

Git clone 自带仓库根目录的通用 [`.mcp.json`](../.mcp.json)：用 Claude Code（或其他支持项目级 MCP 配置的 harness）打开克隆下来的仓库，八个工具会通过 stdio 自动启动，无需手动开终端。配置执行的是 `uv --directory . run atlas-mcp`，以仓库根目录为工作目录；先完成 `uv sync --locked --all-groups`。已发布的 v0.1.0 sdist 不含该文件；下一版清单已补入。

To point stdio at an Atlas checkout elsewhere, replace `.` with the absolute path to it. Configuration files do not expand `${ATLAS_HOME}` or other path templates; do not commit machine-specific absolute paths into shared configs.

若要指向其他位置的 Atlas 源码目录，把 `.` 替换为其绝对路径。配置文件不会展开 `${ATLAS_HOME}` 等路径模板；不要把机器专属绝对路径提交进共享配置。

```json
{
  "mcpServers": {
    "atlas": {
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "<ATLAS_HOME>", "run", "atlas-mcp"]
    }
  }
}
```

Do not connect both a harness-managed stdio process and the HTTP endpoint from the same harness session for the same purpose; pick one entry per client. Running one stdio client alongside the web process remains safe — cross-process safety comes from per-run OS file locks.

不要在同一 harness 会话里为同一目的同时接 stdio 子进程和 HTTP 端点；每个客户端选一种入口。stdio 客户端与 web 进程并存是安全的——跨进程安全由每运行的 OS 文件锁保证。

## Which to choose / 怎么选

- **Daily use with a harness + Web observation**: HTTP. One `atlas-web` process gives you UI, SSE live view, approval buttons, and MCP tools together.
- **Clone-and-go without keeping a terminal**: stdio via `.mcp.json`; the harness manages the process lifecycle for you.
- `atlas_run_workflow(wait=false)` returns a `run_id` right after preflight (P4) so long tasks no longer block the connection; the default synchronous `wait=true` still blocks until the run finishes.

- **日常 harness 使用 + Web 观测**：HTTP。一个 `atlas-web` 进程同时提供界面、SSE 实时视图、审批按钮和 MCP 工具。
- **clone 后不想保持终端**：走 `.mcp.json` 的 stdio；进程生命周期由 harness 管理。
- `atlas_run_workflow(wait=false)` 通过预检后立即返回 run_id（P4），长任务不再阻塞连接；默认 `wait=true` 仍同步等到运行结束。
