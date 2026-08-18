# -*- coding: utf-8 -*-
"""思考深度:统一四档,按模型真实能力映射(PLAN-v2 4.1.1,主人决定 3)。

实测基础(2026-08-16 逐模型探测,scripts/probe_thinking.py):
- effort 档位(OpenAI reasoning_effort,响应真的出现 reasoning tokens):
  Deepseek 全部、SuperAI 全部
- budget 数值(Anthropic thinking.budget_tokens,响应真的出现 thinking 块):
  Kiro:claude-opus-4-8、Minimax:MiniMax-M3
- 假支持(参数被接受但静默吞掉):Kiro 其余 4 个、RightCode 全部 3 个
  ——对这 7 个,thinking 参数在校验期就拒绝,不许设一个不生效的旋钮

四档映射(主人给的规则):
- effort:low→low medium→medium high→high xhigh→high(只有低中高时极高映射为高)
- budget:低中高极高 → 1024/4096/16384/32768(初值,探测各家上限后修正)
- none:四档全部拒绝
"""
import json
from pathlib import Path

from atlas.config import CONFIG_DIR

CAPABILITIES_PATH = CONFIG_DIR / "capabilities.json"
TIERS = ("low", "medium", "high", "xhigh")

_EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high"}
BUDGET_MAP = {"low": 1024, "medium": 4096, "high": 16384, "xhigh": 32768}
_VALID_KINDS = {"effort", "budget", "none", "unknown"}

_cache: dict | None = None


class ThinkingUnsupported(Exception):
    """该模型没有真实的思考控制。校验期抛出,不静默。"""


def load_capabilities() -> dict:
    global _cache
    if _cache is None:
        try:
            data = json.loads(CAPABILITIES_PATH.read_text(encoding="utf-8-sig"))
            caps = data.get("capabilities", {}) if isinstance(data, dict) else {}
            # 形状与枚举校验:坏文件给可读错误,不缓存坏值(M4 审查🟡8)
            for ref, entry in caps.items():
                kind = entry.get("kind") if isinstance(entry, dict) else None
                if kind not in _VALID_KINDS:
                    raise ThinkingUnsupported(
                        f"capabilities.json 里 {ref} 的 kind {kind!r} 非法"
                        f"(合法:{sorted(_VALID_KINDS)})。修复或删除该条目")
            _cache = caps
        except FileNotFoundError:
            _cache = {}
    return _cache


def provider_default_max_tokens(model_ref: str) -> int | None:
    """模型所属供应商配置的 maxOutputTokens(节点没设时的输出上限)。"""
    from atlas.config import load_provider_configs
    pid = model_ref.partition(":")[0]
    cfg = load_provider_configs().get(pid)
    return cfg.max_output_tokens if cfg else None


def reload_capabilities() -> None:
    global _cache
    _cache = None


def model_capability(model_ref: str) -> str:
    """effort | budget | none | unknown(没探测过的模型)。"""
    return load_capabilities().get(model_ref, {}).get("kind", "unknown")


def thinking_request_params(model_ref: str, tier: str,
                            protocol: str) -> dict:
    """四档 → 请求体片段。不支持时抛 ThinkingUnsupported(校验期已拦,
    这里是执行期的第二道防线)。"""
    if tier not in TIERS:
        raise ThinkingUnsupported(f"思考深度必须是 {TIERS} 之一,得到 {tier!r}")
    kind = model_capability(model_ref)
    if kind == "effort":
        if protocol == "openai":
            return {"reasoning_effort": _EFFORT_MAP[tier]}
        # effort 能力的模型走 anthropic 端点时,网关多半也接受 budget;
        # 探测结论按主协议记录,这里保守降级为不带参数并说明
        raise ThinkingUnsupported(
            f"{model_ref} 的思考能力是 reasoning_effort(OpenAI 协议),"
            f"当前走的是 anthropic 端点,无法设置")
    if kind == "budget":
        if protocol == "anthropic":
            return {"thinking": {"type": "enabled",
                                 "budget_tokens": BUDGET_MAP[tier]}}
        raise ThinkingUnsupported(
            f"{model_ref} 的思考能力是 thinking.budget_tokens(Anthropic 协议),"
            f"当前走的是 openai 端点,无法设置")
    if kind == "none":
        raise ThinkingUnsupported(
            f"{model_ref} 没有真实的思考控制(实测:参数被网关静默忽略)。"
            f"删掉 thinking 配置,或换用支持思考的模型")
    # unknown:没探测过。允许带参数跑(可能生效),但 A10 的生效核对会盯着
    if protocol == "openai":
        return {"reasoning_effort": _EFFORT_MAP[tier]}
    return {"thinking": {"type": "enabled", "budget_tokens": BUDGET_MAP[tier]}}

