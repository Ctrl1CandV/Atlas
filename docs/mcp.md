# MCP harness setup / MCP harness 接入

Atlas exposes its eight MCP tools two ways:

1. **Streamable HTTP (recommended)** — the tools live inside the Web process. Start `atlas-web` once and point your harness at `http://127.0.0.1:8321/mcp`. One command, no extra terminal.
2. **stdio** — an explicit per-harness config launches `atlas-mcp` as a child process. No `atlas-web` needed, but tool calls block synchronously in that child.

Both entries share the same tool implementations; behavior is identical. `atlas_resume_run` resumes only a dynamically confirmed interrupted run; a paused human gate must still be approved or rejected in Web.

Atlas 通过两种方式暴露八个 MCP 工具：

1. **Streamable HTTP（推荐）**——工具内嵌在 Web 进程里。启动一次 `atlas-web`，在 harness 里填 `http://127.0.0.1:8321/mcp` 即可。一条命令，不用另开终端。
2. **stdio**——在各 harness 里显式配置，把 `atlas-mcp` 作为子进程拉起。无需 `atlas-web`，但工具调用会在该子进程里同步阻塞。

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

The repo-root [`.mcp.json`](../.mcp.json) shipped since 2026-08-23 contains exactly this HTTP entry, so a clone picks it up automatically once `atlas-web` is running.

仓库根目录自带的 [`.mcp.json`](../.mcp.json)(2026-08-23 起为 HTTP 形式)就是这条配置:clone 后只要 `atlas-web` 在跑,harness 会自动连上。

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

Add an explicit stdio entry when you want MCP without keeping `atlas-web` running. The shipped repo-root [`.mcp.json`](../.mcp.json) no longer does this — it points to the HTTP endpoint since 2026-08-23, because a shipped `--directory .` is a footgun: any harness that spawns the subprocess outside the repo (field-proven with ZCode) fails with `program not found`, and a stdio child never shares code with your running `atlas-web`. Complete `uv sync --locked --all-groups` first.

想不保持 `atlas-web` 运行就用 MCP 时,显式加一条 stdio 配置。仓库根目录的 [`.mcp.json`](../.mcp.json) 自 2026-08-23 起不再承担这个角色——它指向 HTTP 端点:随仓库分发的 `--directory .` 是个坑(任何在仓库外拉子进程的 harness 都会 `program not found`,2026-08-23 在 ZCode 上实证),而且 stdio 子进程与你正在跑的 `atlas-web` 不共享代码。先完成 `uv sync --locked --all-groups`。

Use the absolute path of your Atlas checkout in `--directory`; configuration files do not expand `${ATLAS_HOME}` or other path templates, and a relative `.` breaks outside the repo. Do not commit machine-specific absolute paths into shared configs.

`--directory` 里写 Atlas 源码目录的绝对路径;配置文件不会展开 `${ATLAS_HOME}` 等路径模板,相对的 `.` 在仓库外会直接失败。不要把机器专属绝对路径提交进共享配置。

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
- **MCP without keeping a terminal**: an explicit stdio entry (see Option 2); the harness manages the process lifecycle for you.
- `atlas_run_workflow(wait=false)` returns a `run_id` right after preflight (P4) so long tasks no longer block the connection; the default synchronous `wait=true` still blocks until the run finishes.

- **日常 harness 使用 + Web 观测**：HTTP。一个 `atlas-web` 进程同时提供界面、SSE 实时视图、审批按钮和 MCP 工具。
- **不想保持终端**：显式配一条 stdio（见方式二）；进程生命周期由 harness 管理。
- `atlas_run_workflow(wait=false)` 通过预检后立即返回 run_id（P4），长任务不再阻塞连接；默认 `wait=true` 仍同步等到运行结束。


## Troubleshooting / 排障

- **ZCode shows the `atlas` server failed even though `atlas-web` is running**: the user configuration still holds an old stdio entry (`uv --directory . run atlas-mcp`). A relative `--directory .` breaks as soon as ZCode spawns the subprocess outside the repo, and a stdio subprocess never talks to your `atlas-web` anyway. Replace the entry with the HTTP form above (`type: http`, `url: http://127.0.0.1:8321/mcp`), restart the session, and check **Settings → MCP**.
- **`atlas-web` exits with “端口 127.0.0.1:8321 已被占用”**: another `atlas-web` instance (possibly running older code) still holds the port — your harness would be talking to stale code. Find and stop it first: `netstat -ano | findstr :8321`, then `taskkill /PID <pid> /F`, then start again. The pre-bind check (2026-08-23) fails loud instead of printing a misleading “started” banner.
- **Tools look outdated after a code update**: restart `atlas-web`; the HTTP endpoint serves the code of the process that owns port 8321.
- **`Session not found` (JSON-RPC error -32600)**: the server restarted and the session id is stale — reconnect (re-initialize) from the harness side.
- **Quick connectivity probe**: `curl -X POST http://127.0.0.1:8321/mcp -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'` — a JSON result means the endpoint is alive.

- **ZCode 里 `atlas` 显示失败,但 `atlas-web` 明明在跑**：用户配置里还是旧的 stdio 条目（`uv --directory . run atlas-mcp`）。相对的 `--directory .` 在 ZCode 于仓库外拉起子进程时直接失败,而且 stdio 子进程本来就不连你的 `atlas-web`。把条目换成上面的 HTTP 形式（`type: http`、`url: http://127.0.0.1:8321/mcp`）,重启会话后在 **Settings → MCP** 确认。
- **`atlas-web` 报“端口 127.0.0.1:8321 已被占用”退出**：还有另一个 `atlas-web` 实例（可能是旧版本代码）占着端口——harness 连到的是旧代码。先找到并关闭它：`netstat -ano | findstr :8321`，再 `taskkill /PID <pid> /F`，然后重新启动。2026-08-23 起绑定前预检会大声失败，不再打印误导性的“已启动”横幅。
- **升级代码后工具面像旧版**：重启 `atlas-web`；HTTP 端点服务的是占用 8321 的那个进程的代码。
- **`Session not found`（JSON-RPC 错误 -32600）**：服务端重启过、会话 id 已失效——harness 侧重新连接（重新 initialize）即可。
- **连通性快查**：用上面那条 curl initialize 探针，返回 JSON result 即端点存活。
