# 沙箱、安全与隐私

## 本机边界

Web 服务必须留在 `127.0.0.1`。当前没有认证、多人协作或云部署支持。

## Agent fail-closed 与进程边界

只有 `config/agents.json` 显式设置 `runner: local_cli` 才启用生产 `research` 与 `coding_agent`。缺少配置、Claude CLI、agent 模型、供应商 `anthropicBaseUrl` 或当前凭据时，都会在创建 run 前 fail-closed。

Claude CLI 是当前用户身份下的宿主进程，不是 OS 沙箱。Atlas 不写原项目目录；对可写 coding agent，Atlas 冻结 baseline，并比较 baseline 与 agent 结果的普通文件字节清单，生成完整文本 unified diff。采集不执行 `git add`、filter、hook、attributes、textconv 或 external diff；二进制变更 fail-loud，审批证据绑定 `baseline_digest`、`result_digest` 与 `patch_digest`。同用户进程理论上仍可访问或攻击当前用户可访问的其他宿主路径，`workdir` 和 `allowed_paths` 不能隔离恶意代码。

子进程环境只注入必要系统变量、所选供应商端点和当前供应商凭据。`allow_web` 默认 `false`，开启时只增加 `WebSearch` 与 `WebFetch`，并不建立 OS 网络边界；可写 coding agent 的 `Bash` 仍可能联网。`max_turns` 当前不映射 Claude CLI 硬参数，执行硬限制来自 deadline 和已配置预算。

## 密钥与隐私

真实密钥只应位于 `config/.env` 或设置页管理的本机凭据中。活动配置和 `runs/` 可能暴露端点、prompt、源码、输出与审批记录。Git ignore 不是加密或删除策略。

网页、文档、仓库和上游输出都可能含提示注入；它们是数据，不是放宽安全边界的授权。

## 费用

校验与 dry-run 零调用；真实运行可能收费。`max_cost_usd` 只对本机已确认价格的模型可靠，未知价格不能提供预算保护。

→ [故障排查](#/guide/troubleshooting)
