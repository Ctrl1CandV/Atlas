# -*- coding: utf-8 -*-
"""模型适配层:五家供应商、失败链、假成功检测。

实测数据(RESEARCH 第 9 节):7 次节点调用 3 次失败,其中三分之二
**不抛异常**——返回成功但内容为空、只回一个 "OK"、必填字段缺失。
所以「调用成功 ≠ 节点成功」,必须过三道检查:
  检查一:内容非空;
  检查二:prompt 真的送达了(截断哨兵);
  检查三:必填字段齐全。
DegradedOutput 与 TransportError 走同一条降级路径——这就是「假成功即降级」。
"""
import hashlib
import json
from dataclasses import dataclass, field
from typing import Iterable

from atlas.config import ConfigError, ProviderConfig, load_env, load_provider_configs
from atlas.events import EventLog


class TransportError(Exception):
    """网络/HTTP/SDK 层错误。会抛异常,但同样触发降级。"""


class DegradedOutput(Exception):
    """返回 200 但内容退化:空、缺必填字段、不是要求的格式。"""


class TruncationError(Exception):
    """截断哨兵:实际 input_tokens 远小于 prompt 的预期 token 数。"""


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class ModelResponse:
    text: str
    usage: Usage | None = None
    # 输出 token 达到 max_tokens 上限——内容很可能在句中被截断。
    # 首次真实运行(M0)观察到:deepseek 打满 8192,报告在表格中间戛然而止,
    # 下游模型因拿到完整产物才语义上发现。必须机器可见,不能靠运气。
    output_truncated: bool = False
    # 思考痕迹:OpenAI 的 reasoning_tokens;Anthropic 的 thinking 块(存在即计 1)。
    # 设了思考深度却为 0 → effort_ineffective 警告(A10)。
    reasoning_tokens: int = 0
    # 思考证据的类型(PLAN-v3 M6-D):usage_reasoning_tokens = 精确 token 数;
    # thinking_block = 只有存在性(Anthropic 响应不给 thinking 的 token 用量),
    # 此时 reasoning_tokens 是哨兵 1,不是数量。空串 = 协议无证据。
    reasoning_kind: str = ""


class OpenAICompatAdapter:
    """OpenAI 兼容端点(/chat/completions)。"""

    protocol = "openai"
    nonsecret_execution_descriptor = True

    def __init__(self, provider_id: str, base_url: str, api_key: str,
                 timeout_s: float = 300.0, max_output_tokens: int = 8192,
                 credential_ref: str = "") -> None:
        from openai import OpenAI
        self.provider_id = provider_id
        self.base_url = base_url
        self.default_timeout_s = timeout_s
        self.credential_ref = credential_ref
        self._client = OpenAI(base_url=base_url, api_key=api_key,
                              timeout=timeout_s, max_retries=0)
        self._max_output_tokens = max_output_tokens

    def execution_descriptor(self) -> dict:
        return {
            "version": 1, "kind": "openai_compat", "protocol": self.protocol,
            "provider_id": self.provider_id, "base_url": self.base_url,
            "default_timeout_s": self.default_timeout_s,
            "default_max_output_tokens": self._max_output_tokens,
            "credential_ref": self.credential_ref,
        }

    def call(self, model_id: str, prompt: str, extra_body: dict | None = None,
             timeout_s: float | None = None) -> ModelResponse:
        body: dict = {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self._max_output_tokens,
        }
        if extra_body:
            body.update(extra_body)   # thinking / temperature / seed / max_tokens(M4)
        kwargs: dict = {"timeout": timeout_s} if timeout_s is not None else {}
        try:
            resp = self._client.chat.completions.create(**body, **kwargs)
        except Exception as e:  # openai 的异常族不稳定,统一包成 TransportError
            raise TransportError(f"[{self.provider_id}/{model_id}] {type(e).__name__}: {e}") from e
        text = resp.choices[0].message.content or ""
        finish_reason = getattr(resp.choices[0], "finish_reason", None)
        usage = None
        reasoning = 0
        reasoning_kind = ""
        if resp.usage is not None:
            usage = Usage(input_tokens=resp.usage.prompt_tokens,
                          output_tokens=resp.usage.completion_tokens)
            det = getattr(resp.usage, "completion_tokens_details", None)
            reasoning = getattr(det, "reasoning_tokens", 0) or 0
            if reasoning > 0:
                reasoning_kind = "usage_reasoning_tokens"
        # 首选协议信号 finish_reason=="length";网关漏报时退回 token 数启发式。
        # 节点级 max_tokens 覆盖供应商默认时,截断阈值必须跟着本次调用值。
        effective_max = body["max_tokens"]
        truncated = finish_reason == "length" or (
            usage is not None and usage.output_tokens is not None
            and usage.output_tokens >= effective_max - 16
        )
        return ModelResponse(text=text, usage=usage, output_truncated=truncated,
                             reasoning_tokens=reasoning,
                             reasoning_kind=reasoning_kind)


class AnthropicCompatAdapter:
    """Anthropic 兼容端点(/v1/messages)。"""

    protocol = "anthropic"
    nonsecret_execution_descriptor = True

    def __init__(self, provider_id: str, base_url: str, api_key: str,
                 timeout_s: float = 300.0, max_output_tokens: int = 8192,
                 credential_ref: str = "") -> None:
        from anthropic import Anthropic
        self.provider_id = provider_id
        self.base_url = base_url
        self.default_timeout_s = timeout_s
        self.credential_ref = credential_ref
        self._client = Anthropic(base_url=base_url, api_key=api_key,
                                 timeout=timeout_s, max_retries=0)
        self._max_output_tokens = max_output_tokens

    def execution_descriptor(self) -> dict:
        return {
            "version": 1, "kind": "anthropic_compat", "protocol": self.protocol,
            "provider_id": self.provider_id, "base_url": self.base_url,
            "default_timeout_s": self.default_timeout_s,
            "default_max_output_tokens": self._max_output_tokens,
            "credential_ref": self.credential_ref,
        }

    def call(self, model_id: str, prompt: str, extra_body: dict | None = None,
             timeout_s: float | None = None) -> ModelResponse:
        body: dict = {
            "model": model_id,
            "max_tokens": self._max_output_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if extra_body:
            body.update(extra_body)
        kwargs: dict = {"timeout": timeout_s} if timeout_s is not None else {}
        try:
            msg = self._client.messages.create(**body, **kwargs)
        except Exception as e:
            raise TransportError(f"[{self.provider_id}/{model_id}] {type(e).__name__}: {e}") from e
        has_thinking = False
        text_parts = []
        for b in msg.content:
            if getattr(b, "type", "") == "text":
                text_parts.append(getattr(b, "text", ""))
            elif getattr(b, "type", "") == "thinking":
                has_thinking = True
        text = "".join(text_parts)
        usage = None
        if msg.usage is not None:
            usage = Usage(input_tokens=getattr(msg.usage, "input_tokens", None),
                          output_tokens=getattr(msg.usage, "output_tokens", None))
        stop_reason = getattr(msg, "stop_reason", None)
        effective_max = body["max_tokens"]
        truncated = stop_reason == "max_tokens" or (
            usage is not None and usage.output_tokens is not None
            and usage.output_tokens >= effective_max - 16
        )
        return ModelResponse(text=text, usage=usage, output_truncated=truncated,
                             reasoning_tokens=1 if has_thinking else 0,
                             reasoning_kind="thinking_block" if has_thinking else "")


class AdapterRegistry:
    """供应商 id → 适配器列表；可冻结并生成稳定、无秘密的执行身份。"""

    DESCRIPTOR_VERSION = 1

    def __init__(self) -> None:
        self._adapters: dict[str, list[tuple[set[str], object]]] = {}
        self._frozen = False

    def register(self, provider_id: str, models: Iterable[str], adapter: object) -> None:
        if self._frozen:
            raise ConfigError("AdapterRegistry 已冻结，不能再 register")
        entries = self._adapters.setdefault(provider_id, [])
        replaced = [(m, a) for m, a in entries
                    if getattr(a, "protocol", None) != getattr(adapter, "protocol", None)]
        self._adapters[provider_id] = replaced + [(set(models), adapter)]

    @staticmethod
    def _adapter_descriptor(adapter: object) -> dict:
        # 只信任本模块显式标记为安全的描述器。任意注入对象绝不走 repr、
        # __dict__ 或响应状态，避免测试替身/插件把密钥或响应混入身份。
        if getattr(adapter, "nonsecret_execution_descriptor", False):
            descriptor = adapter.execution_descriptor()
            if not isinstance(descriptor, dict):
                raise ConfigError("adapter execution_descriptor 必须返回对象")
            return descriptor
        cls = type(adapter)
        descriptor = {
            "version": 1,
            "kind": "injected",
            "class": f"{cls.__module__}.{cls.__qualname__}",
            "protocol": str(getattr(adapter, "protocol", "injected")),
        }
        cap = getattr(adapter, "max_output_tokens", None)
        if not isinstance(cap, int):
            cap = getattr(adapter, "_max_output_tokens", None)
        if isinstance(cap, int) and cap > 0:
            descriptor["default_max_output_tokens"] = cap
        return descriptor

    def execution_descriptor(self) -> dict:
        providers = []
        # provider 注册顺序不影响身份；同 provider 内列表保留协议优先级。
        for provider_id in sorted(self._adapters):
            adapters = []
            for models, adapter in self._adapters[provider_id]:
                adapters.append({
                    "models": sorted(models),
                    "adapter": self._adapter_descriptor(adapter),
                })
            providers.append({"provider_id": provider_id, "adapters": adapters})
        return {"version": self.DESCRIPTOR_VERSION, "providers": providers}

    def fingerprint(self) -> str:
        payload = json.dumps(self.execution_descriptor(), ensure_ascii=False,
                             sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def default_max_output_tokens(self, model_ref: str) -> int:
        adapter, _ = self.resolve(model_ref)
        descriptor = self._adapter_descriptor(adapter)
        cap = descriptor.get("default_max_output_tokens")
        return cap if isinstance(cap, int) and cap > 0 else 8192

    def freeze(self) -> "AdapterRegistry":
        self._frozen = True
        return self

    def resolve(self, model_ref: str) -> tuple[object, str]:
        """'供应商id:模型id' → (首选 adapter, model_id)。不合法/不在白名单 → ConfigError。"""
        provider_id, sep, model_id = model_ref.partition(":")
        if not sep or not model_id:
            raise ConfigError(
                f"模型引用 {model_ref!r} 不是 '供应商id:模型id' 形式"
            )
        entries = self._adapters.get(provider_id)
        if not entries:
            known = ", ".join(sorted(self._adapters)) or "(空)"
            raise ConfigError(f"未知的供应商 {provider_id!r}(来自 {model_ref!r})。已注册:{known}")
        for models, adapter in entries:
            if model_id in models:
                return adapter, model_id
        all_models = set().union(*(m for m, _ in entries))
        raise ConfigError(
            f"模型 {model_id!r} 不在供应商 {provider_id} 的白名单里。"
            f"可用:{', '.join(sorted(all_models))}"
        )

    def resolve_for_protocol(self, model_ref: str, protocol: str) -> tuple[object, str] | None:
        """指定协议的适配器;该协议没注册 → None(由调用方决定降级路径)。"""
        provider_id, _, model_id = model_ref.partition(":")
        for models, adapter in self._adapters.get(provider_id, []):
            if model_id in models and getattr(adapter, "protocol", None) == protocol:
                return adapter, model_id
        return None


def build_real_registry(
    provider_ids: Iterable[str],
    *,
    prefer_transport: dict[str, str] | None = None,
    max_output_tokens: dict[str, int] | None = None,
) -> AdapterRegistry:
    """按图里实际用到的供应商构建真实适配器。缺密钥在这里失败(fail-closed)。

    prefer_transport / max_output_tokens 的调用级覆盖优先,
    providers.json 里 per-provider 的 preferTransport / maxOutputTokens 次之,
    都没有则默认 openai 端点 + 8192。

    推理型模型会把输出预算烧在隐性思考上——M0 真实运行两次观察到
    glm-5.3 思考打满 8192、可见文本为空(被「返回内容为空」检查拦下)。
    这类模型用 providers.json 的 maxOutputTokens 给更大预算。
    """
    import os

    prefer_transport = dict(prefer_transport or {})
    max_output_tokens = dict(max_output_tokens or {})
    configs: dict[str, ProviderConfig] = load_provider_configs()
    load_env()
    registry = AdapterRegistry()
    for pid in provider_ids:
        cfg = configs.get(pid)
        if cfg is None:
            raise ConfigError(
                f"providers.json 里没有供应商 {pid!r}。"
                f"已有:{', '.join(sorted(configs))}"
            )
        api_key = os.environ.get(cfg.api_key_env)
        if not api_key:
            raise ConfigError(
                f"供应商 {pid} 需要环境变量 {cfg.api_key_env}(config/.env),但它没有值。"
                f"不猜、不降级——先补齐配置再跑。"
            )
        cap = max_output_tokens.get(
            pid, cfg.max_output_tokens if cfg.max_output_tokens is not None else 8192)
        if pid not in prefer_transport and cfg.prefer_transport:
            prefer_transport[pid] = cfg.prefer_transport
        primary = prefer_transport.get(pid, "openai" if cfg.openai_base_url else "anthropic")
        secondary = "anthropic" if primary == "openai" else "openai"
        for transport in (primary, secondary):
            if transport == "openai" and cfg.openai_base_url:
                registry.register(pid, cfg.models, OpenAICompatAdapter(
                    pid, cfg.openai_base_url, api_key, max_output_tokens=cap,
                    credential_ref=cfg.api_key_env))
            elif transport == "anthropic" and cfg.anthropic_base_url:
                registry.register(pid, cfg.models, AnthropicCompatAdapter(
                    pid, cfg.anthropic_base_url, api_key, max_output_tokens=cap,
                    credential_ref=cfg.api_key_env))
        if pid not in registry._adapters:
            raise ConfigError(f"供应商 {pid} 没有 {primary} 协议的 base URL")
    return registry


# ─────────────────────────── 假成功检测 ───────────────────────────


def assert_not_truncated(prompt: str, usage: Usage | None, *,
                         node: str, model: str, log: EventLog) -> None:
    """截断哨兵。实测抓到过:11154 字符的 prompt 只送达 108 tokens。

    网关不返回 usage 时记 sentinel_skipped 警告——拿不到证据不等于检查通过。
    """
    if usage is None or usage.input_tokens is None:
        log.emit("sentinel_skipped", node=node, model=model,
                 reason="网关未返回 usage,截断哨兵未执行(记警告,不算通过)")
        return
    expected_tokens = len(prompt) / 3  # 中英文混排的粗估
    if usage.input_tokens < expected_tokens * 0.3:
        raise TruncationError(
            f"疑似 prompt 未完整送达:预期约 {len(prompt)} 字符"
            f"(≈{expected_tokens:.0f} tokens),实际 input_tokens={usage.input_tokens}。"
            f"这通常意味着传参链路断了。"
        )


def check_required_fields(text: str, required: list[str] | None) -> dict | None:
    """检查三:必填字段齐全。没配 output_schema 的节点返回 None(不检查)。"""
    if not required:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise DegradedOutput(
            f"要求 JSON 输出(必填字段 {required}),但返回不是合法 JSON:{e.msg}"
        ) from e
    if not isinstance(parsed, dict):
        raise DegradedOutput(f"要求 JSON 对象(必填字段 {required}),返回的是 {type(parsed).__name__}")
    missing = [f for f in required if f not in parsed]
    if missing:
        raise DegradedOutput(f"缺少必填字段:{missing}")
    return parsed


@dataclass(frozen=True)
class CallAttempt:
    model: str
    error_type: str
    reason: str


@dataclass(frozen=True)
class CallOutcome:
    text: str
    parsed: dict | None
    model_requested: str
    model_used: str
    usage: Usage | None
    output_truncated: bool = False
    reasoning_tokens: int = 0
    reasoning_kind: str = ""
    attempts: tuple[CallAttempt, ...] = field(default_factory=tuple)


class AllCandidatesFailed(Exception):
    """主模型与全部备用都失败。attempts 里是每一次的失败原因。"""

    def __init__(self, model_ref: str, attempts: list[CallAttempt]) -> None:
        self.model_ref = model_ref
        self.attempts = tuple(attempts)
        detail = "; ".join(f"{a.model} → {a.error_type}: {a.reason}" for a in attempts)
        super().__init__(f"主模型 {model_ref} 及全部备用模型均失败。{detail}")


# ─────────────────────────── 熔断 ───────────────────────────


class CircuitBreaker:
    """只对「模型不可用」类错误计数(架构第 5 节)。

    TransportError(网络/鉴权/超时)计入;DegradedOutput(内容不合格)
    不计——那可能只是这一次提问的问题,熔断它会把好模型冤枉死。
    连续 N 次传输失败 → 打开 M 秒,打开期间直接跳过该候选。
    进程内状态:重启即清零(本地单机,刻意不持久化)。
    并发说明:Web 多线程共享同一实例,读-改-写在锁里做。
    """

    THRESHOLD = 3
    OPEN_SECONDS = 600.0

    def __init__(self) -> None:
        import threading
        self._lock = threading.Lock()
        self._fails: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    def is_open(self, model_ref: str) -> bool:
        import time
        with self._lock:
            return time.monotonic() < self._open_until.get(model_ref, 0.0)

    def record_transport_failure(self, model_ref: str) -> None:
        import time
        with self._lock:
            n = self._fails.get(model_ref, 0) + 1
            self._fails[model_ref] = n
            if n >= self.THRESHOLD:
                self._open_until[model_ref] = time.monotonic() + self.OPEN_SECONDS
                self._fails[model_ref] = 0

    def record_success(self, model_ref: str) -> None:
        with self._lock:
            self._fails.pop(model_ref, None)

    def reset(self) -> None:
        with self._lock:
            self._fails.clear()
            self._open_until.clear()


breaker = CircuitBreaker()


def call_with_fallback(
    *,
    registry: AdapterRegistry,
    log: EventLog,
    node_id: str,
    iteration: int,
    model_ref: str,
    fallback_refs: list[str],
    prompt: str,
    required_fields: list[str] | None,
    thinking_tier: str | None = None,
    temperature: float | None = None,
    seed: int | None = None,
    max_output_tokens: int | None = None,
    timeout_s: float | None = None,
    retry: int = 0,
    before_attempt=None,
    after_attempt=None,
    remaining_timeout=None,
    sleep_fn=None,
) -> CallOutcome:
    """失败链主入口。假成功与传输错误同路降级;全部失败则抛 AllCandidatesFailed。

    M4 节点参数:thinking_tier 按候选各自的真实能力映射(不支持的候选跳过
    并记账,不静默丢掉意图);temperature/seed 原样进请求体。
    """
    from atlas.thinking import ThinkingUnsupported, thinking_request_params

    attempts: list[CallAttempt] = []
    candidates = [model_ref, *fallback_refs]
    cand_retries_left = {}   # 每个候选的同模型重试余额(retry 只对传输失败)
    idx = -1
    while True:
        idx += 1
        if idx >= len(candidates):
            break
        cand = candidates[idx]
        adapter, model_id = registry.resolve(cand)
        if breaker.is_open(cand):
            attempts.append(CallAttempt(cand, "BreakerOpen", "熔断中(连续传输失败),跳过"))
            log.emit("breaker_open", node=node_id, iteration=iteration, model=cand,
                     reason=f"连续 {CircuitBreaker.THRESHOLD} 次传输失败,"
                            f"熔断 {int(CircuitBreaker.OPEN_SECONDS)}s 内跳过")
            continue
        extra_body: dict = {}
        if temperature is not None:
            extra_body["temperature"] = temperature
        if seed is not None:
            extra_body["seed"] = seed
        if max_output_tokens is not None:
            extra_body["max_tokens"] = max_output_tokens   # 覆盖供应商默认
        if thinking_tier is not None:
            try:
                extra_body.update(thinking_request_params(
                    cand, thinking_tier, adapter.protocol))
            except ThinkingUnsupported as e:
                # 能力在另一协议端点上时,切到那个端点发(Kiro 形态:
                # thinking 只在 anthropic 端点生效,默认传输是 openai)
                alt_protocol = "anthropic" if adapter.protocol == "openai" else "openai"
                alt = registry.resolve_for_protocol(cand, alt_protocol)
                if alt is not None:
                    try:
                        extra_body.update(thinking_request_params(
                            cand, thinking_tier, alt_protocol))
                        adapter, model_id = alt
                    except ThinkingUnsupported:
                        alt = None
                if alt is None:
                    attempts.append(CallAttempt(cand, "ThinkingUnsupported", str(e)))
                    log.emit("model_failed", node=node_id, iteration=iteration,
                             model=cand, reason=f"ThinkingUnsupported: {e}")
                    continue   # 换下一个候选,思考意图不静默降级
        reservation = None
        attempt_timeout = timeout_s
        if remaining_timeout is not None:
            remaining = remaining_timeout()
            if remaining != float("inf"):
                attempt_timeout = (remaining if timeout_s is None
                                   else min(timeout_s, remaining))
        if before_attempt is not None:
            reservation = before_attempt(cand)
        try:
            resp = adapter.call(model_id, prompt,
                                extra_body=extra_body or None,
                                timeout_s=attempt_timeout)
        except TransportError as e:
            if after_attempt is not None:
                after_attempt(reservation, cand, None)
            reason = str(e)
            attempts.append(CallAttempt(cand, "TransportError", reason))
            log.emit("model_failed", node=node_id, iteration=iteration, model=cand,
                     reason=f"TransportError: {reason}")
            # 同模型重试(retry):瞬时网络故障重跑同模型有意义;
            # 假成功不重试(内容问题重跑同模型没意义,直接换候选)
            left = cand_retries_left.get(cand, retry)
            if left > 0:
                cand_retries_left[cand] = left - 1
                idx -= 1   # 重跑当前候选
                sleep_s = 0.5
                if remaining_timeout is not None:
                    sleep_s = min(sleep_s, remaining_timeout())
                (sleep_fn or __import__("time").sleep)(sleep_s)
                continue
            breaker.record_transport_failure(cand)
            continue
        if after_attempt is not None:
            after_attempt(reservation, cand, resp.usage)
        try:
            # 检查一:内容非空
            if not resp.text.strip():
                raise DegradedOutput("返回内容为空")
            # 检查二:截断哨兵
            assert_not_truncated(prompt, resp.usage, node=node_id, model=cand, log=log)
            # 检查三:必填字段
            parsed = check_required_fields(resp.text, required_fields)
            # 输出打顶是内容不完整,与其他假成功一样换候选;没有候选则显式失败。
            if resp.output_truncated:
                log.emit("output_truncated", node=node_id, iteration=iteration, model=cand,
                         reason="输出 token 达到本次 max_tokens 上限,拒绝不完整内容")
                raise DegradedOutput("输出达到本次 max_tokens 上限,内容被截断")
        except (DegradedOutput, TruncationError) as e:
            reason = str(e)
            attempts.append(CallAttempt(cand, type(e).__name__, reason))
            log.emit("model_failed", node=node_id, iteration=iteration, model=cand,
                     reason=f"{type(e).__name__}: {reason}")
            continue
        if thinking_tier is not None and resp.reasoning_tokens == 0:
            # A10 生效核对:设了思考深度却没有任何思考痕迹。
            # kind=none 的模型已被上面拒绝;这里抓的是「探测表说支持/未探测,
            # 实际这次没生效」的形态——可见,不假装。
            log.emit("effort_ineffective", node=node_id, iteration=iteration,
                     model=cand, tier=thinking_tier,
                     reason=f"设了思考深度 {thinking_tier} 但响应中没有思考痕迹;"
                            f"可能该模型/网关不支持(建议重测能力),"
                            f"也可能只是本题简单到不需要思考——后者可忽略")
        breaker.record_success(cand)
        return CallOutcome(
            text=resp.text,
            parsed=parsed,
            model_requested=model_ref,
            model_used=cand,
            usage=resp.usage,
            output_truncated=resp.output_truncated,
            reasoning_tokens=resp.reasoning_tokens,
            reasoning_kind=resp.reasoning_kind,
            attempts=tuple(attempts),
        )
    raise AllCandidatesFailed(model_ref, attempts)


# ─────────────────────────── 假供应商 ───────────────────────────


@dataclass
class _FakeModelSpec:
    text: str = ""
    sequence: list[str] | None = None   # 逐次弹出的应答序列(最后一条重复)
    usage: Usage | None = None          # None 且 auto_usage=False → 响应里不带 usage
    auto_usage: bool = True             # 默认造一个合理的 usage(哨兵可通过)
    transport_error: str | None = None  # 非空则调用时抛 TransportError
    reasoning_tokens: int = 0           # >0 模拟"思考真实生效"(A10 用例)


class FakeProvider:
    """可编程的假模型。不花钱,可复现,能精确构造真实世界难复现的失败。"""

    protocol = "openai"   # 与真实适配器同构;测试可改为 anthropic 协议

    def __init__(self, max_output_tokens: int = 8192) -> None:
        self.models: dict[str, _FakeModelSpec] = {}
        self.calls: list[dict] = []       # 每次调用的 {model, prompt_len, extra_body}
        self.max_output_tokens = max_output_tokens

    def configure(self, model_id: str, *, text: str = "",
                  sequence: list[str] | None = None,
                  usage: Usage | None = None, auto_usage: bool = True,
                  transport_error: str | None = None,
                  reasoning_tokens: int = 0) -> None:
        self.models[model_id] = _FakeModelSpec(
            text=text, sequence=list(sequence) if sequence else None,
            usage=usage, auto_usage=auto_usage,
            transport_error=transport_error,
            reasoning_tokens=reasoning_tokens,
        )

    def register_into(self, registry: AdapterRegistry, provider_id: str = "Fake") -> None:
        registry.register(provider_id, self.models.keys(), self)

    def call(self, model_id: str, prompt: str, extra_body: dict | None = None,
             timeout_s: float | None = None) -> ModelResponse:
        spec = self.models[model_id]
        # extra_body 全量记录:A9 断言"参数真的进了请求体"靠它
        self.calls.append({"model": model_id, "prompt_len": len(prompt),
                           "extra_body": dict(extra_body or {}),
                           "timeout_s": timeout_s})
        if spec.transport_error is not None:
            raise TransportError(spec.transport_error)
        text = spec.text
        if spec.sequence:
            text = spec.sequence.pop(0) if len(spec.sequence) > 1 else spec.sequence[0]
        usage = spec.usage
        if usage is None and spec.auto_usage:
            usage = Usage(input_tokens=max(1, len(prompt) // 3),
                          output_tokens=max(1, len(text) // 3))
        effective_max = (extra_body or {}).get("max_tokens", self.max_output_tokens)
        truncated = usage is not None and usage.output_tokens is not None \
            and usage.output_tokens >= effective_max
        return ModelResponse(text=text, usage=usage, output_truncated=truncated,
                             reasoning_tokens=spec.reasoning_tokens,
                             reasoning_kind=("usage_reasoning_tokens"
                                             if spec.reasoning_tokens > 0 else ""))
