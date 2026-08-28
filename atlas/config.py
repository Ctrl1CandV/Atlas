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
class AgentCollectSpec:
    """E-2B:agent 执行后的只读收集清单条目(封闭字段)。

    pattern 是相对执行目录的 glob(禁 .. / 绝对路径 / 反斜杠);role 封闭
    于 {output, raw, report}——diff 由系统采集器专管,error/changes/input
    不开放,防语义滥用;ext 可选,命中文件的扩展名过滤;逻辑名合成
    {name_prefix}.{清洗后的相对路径}。
    """
    pattern: str = ""
    name_prefix: str = ""
    role: str = "output"
    ext: str | None = None


@dataclass(frozen=True)
class AgentRunnerConfig:
    runner: str = "fail_closed"
    cli: AgentCliConfig = field(default_factory=AgentCliConfig)
    collect: tuple[AgentCollectSpec, ...] = ()
    # 与系统硬编码排除目录(.git/node_modules/.venv/dist/build/__pycache__/
    # .trash)取并集;可追加不可删减
    collect_exclude_dirs: tuple[str, ...] = ()


_AGENT_CONFIG_KEYS = frozenset({"runner", "cli", "collect",
                                "collect_exclude_dirs"})
_AGENT_CLI_KEYS = frozenset({"kind", "command", "extra_args"})
_AGENT_SAFE_EXTRA_ARGS = frozenset({"--verbose"})
_COLLECT_ENTRY_KEYS = frozenset({"pattern", "name_prefix", "role", "ext"})
_COLLECT_ROLES = frozenset({"output", "raw", "report"})
_COLLECT_PREFIX_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_COLLECT_EXT_RE = re.compile(r"^\.[A-Za-z0-9_-]{1,16}$")


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

    collect = _parse_collect(raw.get("collect"))
    exclude_raw = raw.get("collect_exclude_dirs", [])
    if not isinstance(exclude_raw, list) or not all(
            isinstance(d, str) and d and d == d.lower()
            and "/" not in d and "\\" not in d and "." not in d
            for d in exclude_raw):
        raise ConfigError(
            "agents.json 的 collect_exclude_dirs 必须是不带路径分隔符与点号"
            "的小写目录名数组(与系统硬编码排除目录取并集,不可删减)")
    return AgentRunnerConfig(
        runner=runner,
        cli=AgentCliConfig(kind=kind, command=command.strip(),
                           extra_args=tuple(extra_args)),
        collect=collect,
        collect_exclude_dirs=tuple(exclude_raw),
    )


def _parse_collect(raw) -> tuple[AgentCollectSpec, ...]:
    """E-2B collect 清单解析:封闭字段、封闭角色、glob 安全校验。"""
    if raw is None or raw == []:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("agents.json 的 collect 必须是数组")
    if len(raw) > 8:
        raise ConfigError("agents.json 的 collect 最多 8 条")
    specs: list[AgentCollectSpec] = []
    prefixes_seen: set[str] = set()
    for index, entry in enumerate(raw):
        where = f"agents.json 的 collect[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} 必须是对象")
        unknown = set(entry) - _COLLECT_ENTRY_KEYS
        if unknown:
            raise ConfigError(f"{where} 有未知字段:{sorted(unknown)}。"
                              f"可用:{sorted(_COLLECT_ENTRY_KEYS)}")
        pattern = entry.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            raise ConfigError(f"{where}.pattern 必须是非空字符串")
        if "\\" in pattern:
            raise ConfigError(
                f"{where}.pattern 禁用反斜杠(分隔符一律用 /)")
        if pattern.startswith(("/", "~")) or (len(pattern) >= 2
                                              and pattern[1] == ":"):
            raise ConfigError(
                f"{where}.pattern 必须是相对执行目录的 glob,得到绝对路径")
        segments = pattern.split("/")
        for segment in segments:
            if not segment or segment == ".":
                raise ConfigError(
                    f"{where}.pattern 含空段或 '.':{pattern!r}")
            if segment == "..":
                raise ConfigError(
                    f"{where}.pattern 禁止 '..'(逃出执行目录)")
        prefix = entry.get("name_prefix")
        if not isinstance(prefix, str) or _COLLECT_PREFIX_RE.match(prefix) is None:
            raise ConfigError(
                f"{where}.name_prefix {prefix!r} 不合法:全小写字母开头,"
                "只含小写字母/数字/连字符/下划线,≤32 字符")
        if prefix in prefixes_seen:
            raise ConfigError(f"{where}.name_prefix {prefix!r} 重复")
        prefixes_seen.add(prefix)
        role = entry.get("role", "output")
        if role not in _COLLECT_ROLES:
            raise ConfigError(
                f"{where}.role 必须是 {sorted(_COLLECT_ROLES)} 之一,"
                f"得到 {role!r}(diff 由系统采集器专管,error/changes/"
                "input 不开放)")
        ext = entry.get("ext")
        if ext is not None and (not isinstance(ext, str)
                                or _COLLECT_EXT_RE.match(ext) is None):
            raise ConfigError(
                f"{where}.ext 必须形如 '.patch'(可选;命中文件按此过滤)")
        specs.append(AgentCollectSpec(pattern=pattern, name_prefix=prefix,
                                      role=role, ext=ext))
    return tuple(specs)


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
