# -*- coding: utf-8 -*-
"""运行前把 YAML 图具体化为引擎可执行的有效规格。

产品语义(主人定):YAML 是模型真相,所有 kind 一视同仁——仓库示例的
模型字段留空(未配置),不在运行时按本机供应商静默自动绑定。模型只有
两个来源:用户在节点里显式选择(node_overrides,仅本次运行),或让 AI
经 MCP 把模型写进 YAML。

覆盖白名单第五轮加入 ``prompt`` 与(coding_agent 的)``workdir``:
prompt 是**完整替换**本次运行该节点的职责文本——不是追加,审计以
runs/<id>/ 的有效规格快照为真相。consumes、outputs、拓扑、权限字段
永远不可覆盖:它们决定下游接线,改它们等于改图。
所有错误都在分配 run_id 和模型调用之前抛出。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Any

from atlas.spec import (NodeSpec, SpecError, WorkflowSpec, spec_fingerprint,
                        spec_to_snapshot, validate_node_spec, validate_spec)

_LLM_OVERRIDE_FIELDS = frozenset({
    "model", "fallback", "thinking", "max_output_tokens", "temperature",
    "seed", "timeout_s", "retry", "prompt",
})
_AGENT_OVERRIDE_FIELDS = frozenset({"model", "max_turns", "timeout_s", "retry",
                                    "prompt", "workdir"})
_HUMAN_OVERRIDE_FIELDS = frozenset({"prompt"})

# 每种节点类型可覆盖的字段(封闭清单,未知类型没有任何可覆盖字段)。
_OVERRIDABLE_FIELDS: dict[str, frozenset[str]] = {
    "llm": _LLM_OVERRIDE_FIELDS,
    "research": _AGENT_OVERRIDE_FIELDS,
    "coding_agent": _AGENT_OVERRIDE_FIELDS,
    "human": _HUMAN_OVERRIDE_FIELDS,
}


@dataclass(frozen=True)
class EffectiveWorkflow:
    """完全具体的运行规格及其不含秘密的审计摘要。"""

    spec: WorkflowSpec
    base_fingerprint: str
    effective_fingerprint: str
    bindings: tuple[dict, ...]
    overrides: tuple[dict, ...]
    unconfigured_nodes: tuple[str, ...] = ()
    prompt_overridden: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def public_summary(self) -> dict:
        return {
            "base_fingerprint": self.base_fingerprint,
            "effective_fingerprint": self.effective_fingerprint,
            "bindings": [dict(item) for item in self.bindings],
            "overrides": [dict(item) for item in self.overrides],
            "unconfigured_nodes": list(self.unconfigured_nodes),
            "prompt_overridden": list(self.prompt_overridden),
            "warnings": list(self.warnings),
            "effective_spec": spec_to_snapshot(self.spec),
        }


def provider_ids_for_spec(spec: WorkflowSpec) -> list[str]:
    """只收集真正由 AdapterRegistry 执行的 LLM 模型。"""
    refs = ({n.model for n in spec.nodes if n.type == "llm"}
            | {f for n in spec.nodes if n.type == "llm" for f in n.fallback})
    return sorted({ref.partition(":")[0] for ref in refs if ref})


def agent_provider_ids_for_spec(spec: WorkflowSpec) -> list[str]:
    """收集由本机 CLI runner 执行的 agent 供应商，不构造 SDK adapter。"""
    refs = {n.model for n in spec.nodes
            if n.type in {"research", "coding_agent"} and n.model}
    return sorted({ref.partition(":")[0] for ref in refs})


def unconfigured_model_nodes(spec: WorkflowSpec) -> tuple[str, ...]:
    """模型仍为空(待选择)的 LLM/agent 节点；human 不需要模型。"""
    return tuple(n.id for n in spec.nodes
                 if n.type in {"llm", "research", "coding_agent"} and not n.model)


def _redacted_summary_fields(fields: dict) -> dict:
    """审计摘要里的字段值:prompt 只记长度与哈希前缀,不进全文。

    摘要会进事件账本与界面响应;prompt 全文在 runs/<id>/ 的有效规格
    快照里,摘要只要能回答"哪些节点的职责被换成了什么形状"。
    """
    redacted = dict(fields)
    text = redacted.get("prompt")
    if isinstance(text, str):
        redacted["prompt"] = {
            "changed": True,
            "chars": len(text),
            "sha256_prefix": hashlib.sha256(
                text.encode("utf-8")).hexdigest()[:12],
        }
    return redacted


def _normalize_overrides(
        spec: WorkflowSpec, raw: Any) -> tuple[dict[str, dict], tuple[dict, ...]]:
    if raw is None:
        return {}, ()
    if not isinstance(raw, dict):
        raise SpecError("node_overrides 必须是以节点 id 为键的映射")

    by_id = {n.id: n for n in spec.nodes}
    normalized: dict[str, dict] = {}
    summary: list[dict] = []
    for node_id, values in raw.items():
        if not isinstance(node_id, str) or node_id not in by_id:
            raise SpecError(f"node_overrides 引用了未知节点 {node_id!r}")
        if not isinstance(values, dict):
            raise SpecError(f"node_overrides.{node_id} 必须是映射")
        node = by_id[node_id]
        allowed = _OVERRIDABLE_FIELDS.get(node.type, frozenset())
        unknown = set(values) - allowed
        if unknown:
            raise SpecError(
                f"node_overrides.{node_id} 有禁止或未知字段:{sorted(unknown)}。"
                f"该 {node.type} 节点可用:{sorted(allowed)}")
        if not values:
            continue
        copied = dict(values)
        if "fallback" in copied and isinstance(copied["fallback"], list):
            copied["fallback"] = list(copied["fallback"])
        normalized[node_id] = copied
        summary.append({"node": node_id,
                        "fields": _redacted_summary_fields(copied)})
    return normalized, tuple(summary)


def _apply_overrides(spec: WorkflowSpec, overrides: dict[str, dict]) -> WorkflowSpec:
    nodes: list[NodeSpec] = []
    for node in spec.nodes:
        values = overrides.get(node.id)
        candidate = replace(node, **values) if values else node
        candidate = validate_node_spec(
            candidate,
            where=(f"node_overrides.{node.id}" if values
                   else f"effective node {node.id}"))
        nodes.append(candidate)
    effective = replace(spec, nodes=nodes)
    validate_spec(effective, source=f"effective workflow {spec.name!r}")
    return effective


def build_effective_spec(
        base_spec: WorkflowSpec, node_overrides: Any = None) -> EffectiveWorkflow:
    """生成唯一有效规格;调用方只能把返回的 ``spec`` 交给引擎。

    example/custom/template 统一语义:严格保留 YAML 模型,只有显式
    node_overrides 能改;不做任何静默自动绑定。仍缺模型的节点进
    ``unconfigured_nodes``——预览据此显示"待选择",运行据此拒绝。
    """
    overrides, override_summary = _normalize_overrides(base_spec, node_overrides)
    base_fingerprint = spec_fingerprint(base_spec)
    effective = _apply_overrides(base_spec, overrides)
    bindings = tuple({
        "node": n.id,
        "source": ("override" if "model" in overrides.get(n.id, {})
                   else "yaml"),
        "base_model": base_spec.node(n.id).model,
        "model": n.model,
        "fallback": list(n.fallback),
    } for n in effective.nodes
      if n.type in {"llm", "research", "coding_agent"})

    # 最后一遍全节点字段校验 + 图校验,防未来修改绕过具体化边界。
    effective = replace(effective, nodes=[
        validate_node_spec(n, where=f"effective node {n.id}")
        for n in effective.nodes
    ])
    validate_spec(effective, source=f"effective workflow {base_spec.name!r}")
    prompt_overridden = tuple(
        node_id for node_id, values in overrides.items() if "prompt" in values)
    return EffectiveWorkflow(
        spec=effective,
        base_fingerprint=base_fingerprint,
        effective_fingerprint=spec_fingerprint(effective),
        bindings=bindings,
        overrides=override_summary,
        unconfigured_nodes=unconfigured_model_nodes(effective),
        prompt_overridden=prompt_overridden,
    )
