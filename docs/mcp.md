# MCP harness setup / MCP harness 接入

Atlas exposes its five MCP tools over stdio. Configure the server in your harness so it starts and stops with the client; running it manually remains supported.

Atlas 通过 stdio 暴露五个 MCP 工具。建议把 server 配置进 harness，由客户端按需启动和停止；仍可保留手动启动方式。

All three configurations below represent the same command:

```text
uv --directory <ATLAS_HOME> run atlas-mcp
```

Before saving a configuration, replace every `<ATLAS_HOME>` with the absolute path to your Atlas source directory. `<ATLAS_HOME>` is only a documentation placeholder: these configuration files do not expand it, `${ATLAS_HOME}`, or other path templates. Do not copy a machine-specific absolute path into a shared configuration.

保存配置前，必须把每个 `<ATLAS_HOME>` 替换为 Atlas 源码目录的绝对路径。`<ATLAS_HOME>` 只是文档占位符：这些配置文件不会展开它、`${ATLAS_HOME}` 或其他路径模板。不要把某台机器的绝对路径提交到共享配置。

## ZCode

Add the server to the workspace file `.zcode/config.json`, or use the same `mcp.servers` entry in the user configuration. Existing top-level settings can remain alongside `mcp`.

将 server 加到工作区的 `.zcode/config.json`；也可以把同一 `mcp.servers` 条目放进用户配置。已有顶层设置可以与 `mcp` 并存。

```json
{
  "mcp": {
    "servers": {
      "atlas": {
        "type": "stdio",
        "command": "uv",
        "args": ["--directory", "<ATLAS_HOME>", "run", "atlas-mcp"]
      }
    }
  }
}
```

Restart the session, then check **Settings → MCP** for the `atlas` connection and its five tools.

重启会话后，在 **Settings → MCP** 检查 `atlas` 连接及五个工具。

## Claude Code

Add this project-scoped server to `.mcp.json` in the project where Claude Code should use Atlas:

在需要使用 Atlas 的项目中，把以下 project-scoped server 加到 `.mcp.json`：

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

Restart Claude Code after changing the file and verify that the `atlas` tools are listed.

修改后重启 Claude Code，并确认工具列表中出现 `atlas`。

## Cursor

Add the server to `.cursor/mcp.json` in the project where Cursor should use Atlas:

在需要使用 Atlas 的项目中，把 server 加到 `.cursor/mcp.json`：

```json
{
  "mcpServers": {
    "atlas": {
      "command": "uv",
      "args": ["--directory", "<ATLAS_HOME>", "run", "atlas-mcp"]
    }
  }
}
```

Reload Cursor after changing the file and confirm that the server is enabled in its MCP settings.

修改后重新加载 Cursor，并在 MCP 设置中确认该 server 已启用。

## Manual alternative / 手动方式

From the Atlas source directory, the equivalent manual command is:

在 Atlas 源码目录中，等价的手动命令是：

```powershell
uv run atlas-mcp
```

Keep that terminal open while the MCP client is connected. Do not start both the manual process and a harness-managed process for the same connection; stdio servers are launched per client process.

MCP 客户端连接期间需保持该终端运行。不要为同一连接同时启动手动进程和 harness 管理的进程；stdio server 由客户端逐进程启动。
