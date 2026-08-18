# -*- coding: utf-8 -*-
"""供应商配置加载。

纪律(config/README.md):
- providers.json 里只有密钥的【变量名】,真实密钥只在 config/.env;
- apiKeyEnv 用 ^[A-Z][A-Z0-9_]*$ 校验——真实密钥含小写/连字符,
  粘错位置在结构上就通不过,不依赖任何人记得别粘错;
- 引用不存在的供应商/白名单外的模型,在花钱之前拒绝(fail-closed)。
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class ConfigError(Exception):
    """配置不合法:格式、变量名、白名单、缺密钥。全部在花钱之前抛出。"""


@dataclass(frozen=True)
class ProviderConfig:
    id: str
    openai_base_url: str | None
    anthropic_base_url: str | None
    api_key_env: str
    models: tuple[str, ...]
    prefer_transport: str | None = None     # "openai" | "anthropic";None=自动(有 openai 用 openai)
    max_output_tokens: int | None = None    # 推理型模型要给更大预算(见 M0 实测)
    # 未来版本/手工添加的未知字段:load 收纳、save 原样回写。
    # 界面保存一次就把手工字段丢了,是静默破坏用户配置(M3 审查🟠4)
    extra: dict = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class AgentCliConfig:
    kind: str = "claude"
    command: str = "claude"
    extra_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentRunnerConfig:
    runner: str = "fail_closed"
    cli: AgentCliConfig = field(default_factory=AgentCliConfig)


_AGENT_CONFIG_KEYS = frozenset({"runner", "cli"})
_AGENT_CLI_KEYS = frozenset({"kind", "command", "extra_args"})
_AGENT_SAFE_EXTRA_ARGS = frozenset({"--verbose"})


def load_agent_config(path: Path | None = None) -> AgentRunnerConfig:
    """加载 agent runner 配置；缺文件保持 fail-closed。"""
    target = Path(path) if path is not None else CONFIG_DIR / "agents.json"
    try:
        raw = json.loads(target.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return AgentRunnerConfig()
    except json.JSONDecodeError as e:
        raise ConfigError(f"{target} 不是合法 JSON:{e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{target} 顶层必须是 JSON 对象")
    unknown = set(raw) - _AGENT_CONFIG_KEYS
    if unknown:
        raise ConfigError(f"{target} 有未知字段:{sorted(unknown)}")
    runner = raw.get("runner", "fail_closed")
    if runner not in {"fail_closed", "local_cli"}:
        raise ConfigError(f"未知 agent runner:{runner!r}")
    cli_raw = raw.get("cli", {})
    if not isinstance(cli_raw, dict):
        raise ConfigError("agents.json 的 cli 必须是对象")
    unknown_cli = set(cli_raw) - _AGENT_CLI_KEYS
    if unknown_cli:
        raise ConfigError(f"agents.json 的 cli 有未知字段:{sorted(unknown_cli)}")
    kind = cli_raw.get("kind", "claude")
    if kind != "claude":
        raise ConfigError(f"未知 CLI 后端:{kind!r};v1 仅支持 'claude'")
    command = cli_raw.get("command", "claude")
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        raise ConfigError("agents.json 的 cli.command 必须是非空字符串")
    extra_args = cli_raw.get("extra_args", [])
    if not isinstance(extra_args, list) or not all(
            isinstance(arg, str) and arg for arg in extra_args):
        raise ConfigError("agents.json 的 cli.extra_args 必须是非空字符串数组")
    unsafe = [arg for arg in extra_args if arg not in _AGENT_SAFE_EXTRA_ARGS]
    if unsafe:
        raise ConfigError(
            "cli.extra_args 只能使用 Atlas 审核过的参数 ['--verbose'];"
            "模型、工具、权限、设置、会话与输出参数由 Atlas 固定")
    return AgentRunnerConfig(
        runner=runner,
        cli=AgentCliConfig(kind=kind, command=command.strip(),
                           extra_args=tuple(extra_args)),
    )


def load_provider_configs(path: Path | None = None) -> dict[str, ProviderConfig]: 
    """读 providers.json 并校验结构。文件本身不合法直接抛 ConfigError。"""
    path = path or (CONFIG_DIR / "providers.json")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"找不到供应商配置 {path}") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path} 不是合法 JSON:{e}") from e

    providers: dict[str, ProviderConfig] = {}
    for entry in raw.get("providers", []):
        pid = entry.get("id")
        if not pid or not isinstance(pid, str):
            # 不放整条 entry:万一有人把真密钥贴进 providers.json,
            # 别让错误响应把它回显出去(M3 审查🟡11)
            raise ConfigError(f"{path} 里第 {raw.get('providers', []).index(entry) + 1} "
                              f"条供应商记录缺 id 或 id 不是字符串")
        if pid in providers:
            raise ConfigError(f"供应商 id 重复:{pid}")
        key_env = entry.get("apiKeyEnv")
        if not isinstance(key_env, str) or not _ENV_NAME_RE.match(key_env):
            raise ConfigError(
                f"供应商 {pid} 的 apiKeyEnv {key_env!r} 不匹配 ^[A-Z][A-Z0-9_]*$。"
                f"这里只允许环境变量名——真实密钥属于 config/.env。"
            )
        models = entry.get("models")
        if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
            raise ConfigError(f"供应商 {pid} 的 models 必须是字符串数组")
        prefer = entry.get("preferTransport")
        if prefer is not None and prefer not in ("openai", "anthropic"):
            raise ConfigError(f"供应商 {pid} 的 preferTransport 只能是 'openai'/'anthropic',得到 {prefer!r}")
        cap = entry.get("maxOutputTokens")
        if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or cap < 1):
            raise ConfigError(f"供应商 {pid} 的 maxOutputTokens 必须是正整数,得到 {cap!r}")
        providers[pid] = ProviderConfig(
            id=pid,
            openai_base_url=entry.get("openaiBaseUrl"),
            anthropic_base_url=entry.get("anthropicBaseUrl"),
            api_key_env=key_env,
            models=tuple(models),
            prefer_transport=prefer,
            max_output_tokens=cap,
            extra={k: v for k, v in entry.items() if k not in {
                "id", "openaiBaseUrl", "anthropicBaseUrl", "apiKeyEnv",
                "models", "preferTransport", "maxOutputTokens"}},
        )
    if not providers:
        raise ConfigError(f"{path} 里没有任何供应商")
    return providers


def load_env(env_path: Path | None = None) -> None:
    """把 config/.env 装进环境变量。绝不打印任何值。"""
    load_dotenv(env_path or (CONFIG_DIR / ".env"), override=False)


def atomic_write_text(path: Path, content: str) -> None:
    """同目录临时文件 + os.replace:写一半崩溃不留半份配置。"""
    import os
    import tempfile

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + "-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


_CONFIG_WRITE_LOCK = __import__("threading").Lock()


def save_provider_configs(providers: "dict[str, ProviderConfig]",
                          path: Path | None = None,
                          lock: bool = True) -> None:
    """把供应商配置写回 providers.json(原子写,保持既有键名格式)。

    未知字段(extra)原样回写;与 configapi 的 load→modify→save 循环
    共用 _CONFIG_WRITE_LOCK 防并发丢更新(M3 审查🟠5)。
    """
    rows = []
    for cfg in providers.values():
        row: dict = {
            "id": cfg.id,
            "apiKeyEnv": cfg.api_key_env,
            "models": list(cfg.models),
        }
        if cfg.openai_base_url:
            row["openaiBaseUrl"] = cfg.openai_base_url
        if cfg.anthropic_base_url:
            row["anthropicBaseUrl"] = cfg.anthropic_base_url
        if cfg.prefer_transport:
            row["preferTransport"] = cfg.prefer_transport
        if cfg.max_output_tokens:
            row["maxOutputTokens"] = cfg.max_output_tokens
        row.update(cfg.extra)   # 手工/未来字段:load 收纳的,原样还回去
        rows.append(row)
    target = path or (CONFIG_DIR / "providers.json")
    content = json.dumps({"providers": rows}, ensure_ascii=False, indent=4) + "\n"
    if lock:
        with _CONFIG_WRITE_LOCK:
            atomic_write_text(target, content)
    else:
        atomic_write_text(target, content)
