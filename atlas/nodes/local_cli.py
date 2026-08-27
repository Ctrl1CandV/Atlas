# -*- coding: utf-8 -*-
"""Claude CLI 的受控本机执行后端。

这是同用户进程的目录 staging 与工具策略，不是 OS 沙箱。缺配置、CLI、
Anthropic 兼容端点或凭据时全部 fail-closed。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from atlas.adapters import RunCancelled, Usage
from atlas.config import (CONFIG_DIR, AgentRunnerConfig, ConfigError,
                          ProviderConfig, load_agent_config,
                          load_provider_configs)
from atlas.credentials import EnvStore
from atlas.nodes.agent import (AgentCliError, SourceBaselineToken, _canonical_path,
                               _file_digest, _scan_tree)

_STDOUT_MAX_BYTES = 16 * 1024 * 1024
_STDERR_MAX_BYTES = 1024 * 1024
_MIN_CLAUDE_VERSION = (2, 1, 0)
_REQUIRED_CLAUDE_FLAGS = (
    "--print", "--safe-mode", "--bare", "--no-session-persistence",
    "--no-chrome", "--output-format", "--permission-mode", "--model",
    "--tools", "--allowedTools", "--max-budget-usd",
)
_SYSTEM_ENV_ALLOWLIST = (
    "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
)


@dataclass(frozen=True)
class AgentRunResult:
    text: str
    usage: Usage | None = None
    cost_usd: float | None = None
    runner: str = "local_cli"


def _resolve_program(command: str) -> str:
    candidate = Path(command)
    if candidate.is_absolute():
        if not candidate.is_file():
            raise ConfigError(f"agent CLI 不存在:{candidate}")
        return str(candidate)
    resolved = shutil.which(command)
    if not resolved:
        raise ConfigError(
            f"找不到 agent CLI {command!r};请安装 Claude Code 或在 "
            "config/agents.json 设置 cli.command")
    return resolved


def _base_child_env(source: dict[str, str] | None = None) -> dict[str, str]:
    host = source if source is not None else os.environ
    return {key: host[key] for key in _SYSTEM_ENV_ALLOWLIST if host.get(key)}


def _git_preflight_env() -> dict[str, str]:
    """最小 Git 环境；保留配置定位以沿用提交索引的行尾语义。"""
    env = _base_child_env()
    for key in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
                "XDG_CONFIG_HOME", "PROGRAMDATA"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    return env


def _provider_for_model(model_ref: str, providers: dict[str, ProviderConfig]
                        ) -> tuple[ProviderConfig, str]:
    provider_id, sep, model_id = model_ref.partition(":")
    if not sep or not model_id:
        raise ConfigError(f"agent 模型 {model_ref!r} 不是 '供应商id:模型id' 形式")
    provider = providers.get(provider_id)
    if provider is None:
        raise ConfigError(f"agent 模型引用了未知供应商 {provider_id!r}")
    if model_id not in provider.models:
        raise ConfigError(f"agent 模型 {model_ref!r} 不在供应商白名单")
    if not provider.anthropic_base_url:
        raise ConfigError(
            f"供应商 {provider_id!r} 没有 anthropicBaseUrl;Claude CLI 后端"
            "只支持 Anthropic 兼容端点")
    return provider, model_id


def _read_version(program: str, env: dict[str, str]) -> tuple[int, int, int]:
    try:
        proc = subprocess.run([program, "--version"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=10, env=env, shell=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ConfigError(f"无法检查 agent CLI 版本:{type(e).__name__}") from e
    if proc.returncode != 0:
        raise ConfigError(f"agent CLI --version 失败(退出码 {proc.returncode})")
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", proc.stdout or "")
    if not match:
        raise ConfigError("agent CLI --version 未返回可识别的语义版本")
    version = tuple(int(part) for part in match.groups())
    if version < _MIN_CLAUDE_VERSION:
        raise ConfigError(
            f"Claude Code {'.'.join(map(str, version))} 过旧;至少需要 "
            f"{'.'.join(map(str, _MIN_CLAUDE_VERSION))}")
    return version


def _check_cli_contract(program: str, env: dict[str, str]) -> None:
    try:
        proc = subprocess.run([program, "--help"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=15, env=env, shell=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ConfigError(f"无法检查 agent CLI 参数:{type(e).__name__}") from e
    if proc.returncode != 0:
        raise ConfigError(f"agent CLI --help 失败(退出码 {proc.returncode})")
    help_text = proc.stdout or ""
    # rf 原始字符串里必须用单反斜杠:[\\s] 是字面反斜杠,会把真实 --help
    # 里"参数后跟空格"的排版误判成缺参。
    missing = [
        flag for flag in _REQUIRED_CLAUDE_FLAGS
        if re.search(rf"(?<![\w-]){re.escape(flag)}(?=[\s,=<]|$)", help_text)
        is None
    ]
    if missing:
        raise ConfigError(
            f"当前 Claude CLI 缺少 Atlas 必需参数:{missing};请升级 Claude Code")


def _require_clean_git_workdir(path: Path, node_id: str) -> SourceBaselineToken:
    git_dir = path / ".git"
    if not git_dir.exists():
        raise ConfigError(
            f"agent 节点 {node_id} 的 workdir 不是 git 仓库;"
            "writable 执行必须先 git init 并提交基线")
    flags = [
        "-c", "core.fsmonitor=false",
        "-c", "core.untrackedCache=false",
        "-c", f"core.hooksPath={os.devnull}",
    ]
    try:
        head = subprocess.run(
            ["git", "-C", str(path), *flags, "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, env=_git_preflight_env(), shell=False)
        index = subprocess.run(
            ["git", "-C", str(path), *flags, "rev-parse", "--git-path", "index"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, env=_git_preflight_env(), shell=False)
        status = subprocess.run(
            ["git", "-C", str(path), *flags, "status", "--porcelain=v1",
             "--untracked-files=all"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, env=_git_preflight_env(), shell=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ConfigError(
            f"无法检查 agent 节点 {node_id} 的 git 基线:{type(e).__name__}") from e
    if head.returncode != 0:
        raise ConfigError(
            f"agent 节点 {node_id} 的 workdir 没有 HEAD;请先提交基线")
    if index.returncode != 0 or not index.stdout.strip():
        raise ConfigError(
            f"agent 节点 {node_id} 的 git index 路径解析失败")
    if status.returncode != 0:
        raise ConfigError(
            f"agent 节点 {node_id} 的 git status 失败(退出码 {status.returncode})")
    if status.stdout.strip():
        raise ConfigError(
            f"agent 节点 {node_id} 的 workdir 有未提交改动;"
            "为保证 diff 只归因于本次 agent，必须先提交或清理")
    oid = head.stdout.strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", oid) is None:
        raise ConfigError(f"agent 节点 {node_id} 的 HEAD 不是可识别对象 id")
    try:
        source_path = _canonical_path(path, label="source")
        raw_index = Path(index.stdout.strip())
        index_path = raw_index if raw_index.is_absolute() else path / raw_index
        canonical_index = _canonical_path(index_path, label="Git index")
        tree_digest = _scan_tree(path, require_git=True)[1]
        index_digest = _file_digest(Path(canonical_index), label="Git index")
    except AgentCliError as e:
        raise ConfigError(f"agent 节点 {node_id} 的源基线无法冻结:{e}") from e
    try:
        final_status = subprocess.run(
            ["git", "-C", str(path), *flags, "status", "--porcelain=v1",
             "--untracked-files=all"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, env=_git_preflight_env(), shell=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ConfigError(
            f"无法复核 agent 节点 {node_id} 的 git 基线:{type(e).__name__}") from e
    if final_status.returncode != 0:
        raise ConfigError(
            f"agent 节点 {node_id} 的 git status 失败(退出码 {final_status.returncode})")
    if final_status.stdout.strip():
        raise ConfigError(
            f"agent 节点 {node_id} 的 workdir 有未提交改动;"
            "为保证 diff 只归因于本次 agent，必须先提交或清理")
    return SourceBaselineToken(
        node_id=node_id, source_path=source_path, tree_digest=tree_digest,
        head=oid, index_path=canonical_index, index_digest=index_digest)


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    tree_error: Exception | None = None
    if os.name == "nt":
        try:
            killed = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, check=False)
            if killed.returncode != 0 and proc.poll() is None:
                tree_error = RuntimeError(
                    f"taskkill 退出码 {killed.returncode}")
        except (OSError, subprocess.TimeoutExpired) as e:
            tree_error = e
    else:
        try:
            os.killpg(proc.pid, 15)
        except (ProcessLookupError, PermissionError) as e:
            tree_error = e
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    if tree_error is not None:
        raise AgentCliError(
            f"无法确认 Claude CLI 整个进程树已终止:{type(tree_error).__name__}") \
            from tree_error


def _parse_result(data: bytes) -> AgentRunResult:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise AgentCliError("Claude CLI 未返回合法 JSON 结果") from e
    if not isinstance(payload, dict):
        raise AgentCliError("Claude CLI JSON 结果必须是对象")
    text = payload.get("result")
    if not isinstance(text, str) or not text.strip():
        raise AgentCliError("Claude CLI 返回空报告(假成功形态)")
    raw_usage = payload.get("usage")
    usage = None
    if isinstance(raw_usage, dict):
        inp = raw_usage.get("input_tokens")
        out = raw_usage.get("output_tokens")
        usage = Usage(
            input_tokens=(inp if isinstance(inp, int) and not isinstance(inp, bool)
                          and inp >= 0 else None),
            output_tokens=(out if isinstance(out, int) and not isinstance(out, bool)
                           and out >= 0 else None),
        )
    raw_cost = payload.get("total_cost_usd")
    cost = None
    if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool):
        try:
            candidate = float(raw_cost)
        except (OverflowError, ValueError):
            candidate = None
        if candidate is not None and math.isfinite(candidate) and candidate >= 0:
            cost = candidate
    return AgentRunResult(text=text, usage=usage, cost_usd=cost)


class LocalCliRunner:
    production_runner = True
    runner_name = "local_cli"

    nonsecret_execution_descriptor = True

    def __init__(self, config: AgentRunnerConfig, providers: dict[str, ProviderConfig],
                 env_store: EnvStore, program: str,
                 cli_version: tuple[int, int, int] | None = None) -> None:
        self.config = config
        self.providers = providers
        self.env_store = env_store
        self.program = str(Path(program).resolve())
        self.cli_version = tuple(cli_version or _MIN_CLAUDE_VERSION)
        self._frozen_credentials: dict[str, str] = {}
        self._source_baseline_tokens: tuple[SourceBaselineToken, ...] = ()

    @property
    def source_baseline_tokens(self) -> tuple[SourceBaselineToken, ...]:
        return self._source_baseline_tokens

    def freeze_provider_credential(self, provider: ProviderConfig) -> None:
        """预检时把本次执行所需密钥冻结在内存；不进入 descriptor/事件。"""
        secret = self.env_store.read_value(provider.api_key_env)
        if not secret:
            raise ConfigError(
                f"供应商 {provider.id!r} 的凭据 {provider.api_key_env} 未配置")
        self._frozen_credentials[provider.id] = secret

    def _credential_revision(self, provider: ProviderConfig) -> str | None:
        """单向版本标识：检测轮换，但不记录或返回凭据值。"""
        secret = self._frozen_credentials.get(provider.id)
        if secret is None:
            return None
        material = (
            b"atlas-agent-credential/v1\0"
            + provider.id.encode("utf-8") + b"\0"
            + provider.api_key_env.encode("ascii") + b"\0"
            + secret.encode("utf-8")
        )
        return hashlib.sha256(material).hexdigest()

    def execution_descriptor(self, provider_ids=None) -> dict:
        used = set(self.providers if provider_ids is None else provider_ids)
        providers = []
        for provider_id in sorted(used):
            provider = self.providers.get(provider_id)
            if provider is None:
                continue
            providers.append({
                "id": provider.id,
                "anthropicBaseUrl": provider.anthropic_base_url,
                "apiKeyEnv": provider.api_key_env,
                "credentialRevision": self._credential_revision(provider),
                "models": sorted(provider.models),
            })
        return {
            "version": 1,
            "kind": self.config.cli.kind,
            "runner": self.runner_name,
            "program": os.path.normcase(self.program).replace("\\", "/"),
            "cli_version": ".".join(map(str, self.cli_version)),
            "extra_args": list(self.config.cli.extra_args),
            "providers": providers,
        }

    def __call__(self, attachment: Path, *, node_type: str, max_turns: int,
                 cwd: Path | None = None, writable: bool = True,
                 allow_web: bool = False, allowed_paths: list[str] | None = None,
                 timeout_s: float | None = None, model_ref: str = "",
                 node_id: str = "agent", max_budget_usd: float | None = None,
                 cancel_requested=None) -> AgentRunResult:
        del max_turns  # 当前 Claude CLI 无 turn 上限；Atlas 以 deadline/预算硬限制。
        provider, model_id = _provider_for_model(model_ref, self.providers)
        secret = self._frozen_credentials.get(provider.id)
        if not secret:
            raise AgentCliError(
                f"供应商 {provider.id!r} 的凭据未在 PreparedExecution 预检中冻结")
        paths = list(allowed_paths or [])
        effective_writable = node_type == "coding_agent" and writable
        if effective_writable and paths:
            raise AgentCliError(
                "writable coding_agent 暂不接受 allowed_paths;--add-dir 不是只读边界")

        run_cwd = Path(cwd) if cwd is not None else Path(tempfile.mkdtemp(
            prefix=f"atlas-research-{node_id}-", dir=Path(attachment).parent.parent))
        run_cwd.mkdir(parents=True, exist_ok=True)
        tools = ["Read", "Glob", "Grep"]
        if node_type == "coding_agent" and writable:
            tools += ["Edit", "Write", "Bash"]
        if allow_web:
            tools += ["WebSearch", "WebFetch"]

        command = [
            self.program, "--print", "--safe-mode", "--bare",
            "--no-session-persistence", "--no-chrome",
            "--output-format", "json", "--permission-mode", "acceptEdits",
            "--model", model_id,
            "--tools", *tools,
            "--allowedTools", *tools,
            *self.config.cli.extra_args,
        ]
        for path in paths:
            command += ["--add-dir", path]
        if max_budget_usd is not None:
            if max_budget_usd <= 0:
                raise AgentCliError("agent 可用预算已耗尽")
            command += ["--max-budget-usd", f"{max_budget_usd:.6f}"]

        child_env = _base_child_env()
        child_env["ANTHROPIC_BASE_URL"] = provider.anthropic_base_url or ""
        child_env["ANTHROPIC_API_KEY"] = secret
        # CLI 用户 settings(~/.claude/settings.json 的 env 块)优先于进程
        # 环境变量,会把调用静默改道到用户个人网关与凭据;空配置目录强制
        # CLI 只使用上面注入的端点与密钥(阶段 D 实测发现)。
        config_dir = tempfile.mkdtemp(prefix="atlas-claude-config-")
        child_env["CLAUDE_CONFIG_DIR"] = config_dir
        creationflags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                         if os.name == "nt" else 0)
        start_new_session = os.name != "nt"
        try:
            with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
                try:
                    proc = subprocess.Popen(
                        command, cwd=run_cwd, env=child_env, stdin=subprocess.PIPE,
                        stdout=stdout_file, stderr=stderr_file, shell=False,
                        creationflags=creationflags, start_new_session=start_new_session)
                    # P2/D2:取消请求终止在途 CLI 的整棵进程树。watcher 轮询
                    # 取消触发器(clear=True 结束),杀树后 communicate 因管道
                    # 关闭返回;「是否因取消而终止」在 communicate 之后统一判定,
                    # 树杀失败保持 AgentCliError 大声失败(fail-closed),不被
                    # 取消语境吞掉。
                    stop_watcher = threading.Event()
                    kill_for_cancel = threading.Event()

                    def _watch_cli_tree() -> None:
                        while not stop_watcher.wait(0.2):
                            if cancel_requested is not None and cancel_requested():
                                kill_for_cancel.set()
                                _terminate_process_tree(proc)
                                return

                    if cancel_requested is not None:
                        threading.Thread(
                            target=_watch_cli_tree,
                            name=f"atlas-cli-cancel-{node_id}",
                            daemon=True).start()
                    try:
                        proc.communicate(input=Path(attachment).read_bytes(),
                                         timeout=timeout_s)
                    except subprocess.TimeoutExpired as e:
                        _terminate_process_tree(proc)
                        raise AgentCliError(
                            f"节点 {node_id} 的 Claude CLI 超过 {timeout_s}s,进程树已终止") from e
                    finally:
                        stop_watcher.set()
                    if kill_for_cancel.is_set():
                        raise RunCancelled(
                            f"节点 {node_id} 收到取消请求,Claude CLI 进程树已终止"
                            f"(退出码 {proc.returncode})")
                except OSError as e:
                    raise AgentCliError(f"无法启动 Claude CLI:{type(e).__name__}") from e
                stdout_size = stdout_file.tell()
                stderr_size = stderr_file.tell()
                if stdout_size > _STDOUT_MAX_BYTES or stderr_size > _STDERR_MAX_BYTES:
                    raise AgentCliError(
                        "Claude CLI 输出超过上限;拒绝静默截断"
                        f"(stdout={stdout_size}, stderr={stderr_size})")
                stdout_file.seek(0)
                stderr_file.seek(0)
                output = stdout_file.read()
                error = stderr_file.read().decode("utf-8", errors="replace")
            if proc.returncode != 0:
                stdout_summary = output.decode("utf-8", errors="replace").strip()
                stderr_summary = error.strip()
                summary = " | ".join(part for part in (
                    f"stdout:{stdout_summary}" if stdout_summary else "",
                    f"stderr:{stderr_summary}" if stderr_summary else "",
                ) if part)
                summary = summary.replace(secret, "[REDACTED]")[:300]
                raise AgentCliError(
                    f"Claude CLI 失败(退出码 {proc.returncode}):"
                    f"{summary or '(无 stdout/stderr)'}")
            return _parse_result(output)
        finally:
            shutil.rmtree(config_dir, ignore_errors=True)


def prepare_local_cli_runner(*, agent_config_path: Path | None = None,
                             providers_path: Path | None = None,
                             env_store: EnvStore | None = None) -> LocalCliRunner:
    config = load_agent_config(agent_config_path)
    if config.runner != "local_cli":
        raise ConfigError(
            "AGENT_RUNNER_DISABLED:请在 config/agents.json 显式设置 "
            "runner='local_cli';缺失时保持 fail-closed")
    providers = load_provider_configs(providers_path)
    program = _resolve_program(config.cli.command)
    child_env = _base_child_env()
    version = _read_version(program, child_env)
    _check_cli_contract(program, child_env)
    return LocalCliRunner(config, providers, env_store or EnvStore(CONFIG_DIR / ".env"),
                          program, version)


def preflight_agent_nodes(nodes, runner: LocalCliRunner) -> tuple[SourceBaselineToken, ...]:
    pending_credentials: dict[str, str] = {}
    tokens: list[SourceBaselineToken] = []
    for node in nodes:
        if not node.model:
            raise ConfigError(f"agent 节点 {node.id} 未配置模型")
        provider, _ = _provider_for_model(node.model, runner.providers)
        secret = runner.env_store.read_value(provider.api_key_env)
        if not secret:
            raise ConfigError(
                f"供应商 {provider.id!r} 的凭据 {provider.api_key_env} 未配置")
        pending_credentials[provider.id] = secret
        if node.type == "coding_agent" and node.writable and node.allowed_paths:
            raise ConfigError(
                f"agent 节点 {node.id}:writable 与 allowed_paths 不能同时使用;"
                "--add-dir 不是只读边界")
        if node.type == "coding_agent" and node.writable:
            tokens.append(_require_clean_git_workdir(Path(node.workdir), node.id))
    # 只有全部节点都验证成功，才把本轮凭据和 token 原子发布给 runner。
    runner._frozen_credentials.update(pending_credentials)
    runner._source_baseline_tokens = tuple(tokens)
    return runner.source_baseline_tokens
