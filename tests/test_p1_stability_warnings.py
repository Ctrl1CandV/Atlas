# -*- coding: utf-8 -*-
"""第二梯队 A2/C1 · dry-run 醒目警告。

A2:已探测推理型模型 + max_output_tokens 生效值低于 16384 → 警告
   (2026-08-22 实测 Deepseek 在默认 8192 下被隐性思考截断)。
C1:设了 max_cost_usd 但计费候选费率未知 → 警告(守卫只放行首个计费节点,
   run 20260822-113908-531dab 实证)。
只用已探测数据:能力表里没有/费率读不到就不警告,准入仍由预检 fail-closed。
"""
import pytest

from atlas.config import ProviderConfig
from atlas.costs import rates_known
from atlas.mcp import REASONING_OUTPUT_TOKEN_FLOOR, _dry_run_warnings
from atlas.spec import spec_from_yaml

WARN_YAML = """
name: warn
nodes:
  - id: only
    type: llm
    model: Fake:primary
    fallback: [Fake:backup]
    prompt: 产出结论
    consumes: [task]
    output_schema:
      required: [verdict]
edges:
  - from: only
    to: END
"""

CAPS = {"Fake:primary": {"kind": "effort", "reasoning_tokens": 336},
        "Fake:backup": {"kind": "none"}}


def _spec(max_cost_usd=None):
    yaml = WARN_YAML
    if max_cost_usd is not None:
        yaml = yaml.replace("edges:", f"guards:\n  max_cost_usd: {max_cost_usd}\nedges:")
    return spec_from_yaml(yaml)


def _cfg(max_output_tokens=None):
    return {"Fake": ProviderConfig(
        id="Fake", openai_base_url=None, anthropic_base_url=None,
        api_key_env="FAKE_KEY", models=("primary", "backup"),
        max_output_tokens=max_output_tokens)}


# ─────────────────── A2:推理型输出预算 ───────────────────


def test_a2_warns_when_probed_reasoning_cap_below_floor():
    warnings = _dry_run_warnings(_spec(), provider_cfgs={}, capabilities=CAPS,
                                 rates_known_fn=lambda _: True)
    assert len(warnings) == 1
    assert "Fake:primary" in warnings[0] and "kind=effort" in warnings[0]
    assert f"生效值 8192 低于 {REASONING_OUTPUT_TOKEN_FLOOR}" in warnings[0]


def test_a2_silent_when_vendor_cap_or_node_cap_reaches_floor():
    assert _dry_run_warnings(_spec(), provider_cfgs=_cfg(16384),
                             capabilities=CAPS, rates_known_fn=lambda _: True) == []
    yaml = WARN_YAML.replace("prompt: 产出结论",
                             "prompt: 产出结论\n    max_output_tokens: 16384")
    node_spec = spec_from_yaml(yaml)
    assert _dry_run_warnings(node_spec, provider_cfgs={}, capabilities=CAPS,
                             rates_known_fn=lambda _: True) == []


def test_a2_unprobed_or_non_reasoning_models_stay_silent():
    assert _dry_run_warnings(_spec(), provider_cfgs={}, capabilities={},
                             rates_known_fn=lambda _: True) == []
    only_none = {"Fake:primary": {"kind": "none"}}
    assert _dry_run_warnings(_spec(), provider_cfgs={}, capabilities=only_none,
                             rates_known_fn=lambda _: True) == []


# ─────────────────── C1:费率未知的成本帽 ───────────────────


def test_c1_warns_when_cap_set_and_rates_unknown():
    warnings = _dry_run_warnings(_spec(max_cost_usd=0.5), provider_cfgs={},
                                 capabilities={}, rates_known_fn=lambda _: False)
    assert len(warnings) == 1
    assert "max_cost_usd=0.5" in warnings[0]
    assert "only 的候选 Fake:primary" in warnings[0]
    assert "Fake:backup" in warnings[0]  # fallback 候选也计入


def test_c1_silent_without_cap_or_with_known_rates():
    assert _dry_run_warnings(_spec(), provider_cfgs={}, capabilities={},
                             rates_known_fn=lambda _: False) == []
    assert _dry_run_warnings(_spec(max_cost_usd=0.5), provider_cfgs={},
                             capabilities={}, rates_known_fn=lambda _: True) == []


# ─────────────────── A2 跨厂商 fallback:按候选解析 cap(审查 2026-08-23) ───────────────────


CROSS_YAML = WARN_YAML.replace("model: Fake:primary\n    fallback: [Fake:backup]",
                               "model: VendorA:think\n    fallback: [VendorB:think]")
CROSS_CAPS = {"VendorA:think": {"kind": "effort"},
              "VendorB:think": {"kind": "effort"}}


def _vendor_cfgs(**per_provider):
    return {pid: ProviderConfig(
        id=pid, openai_base_url=None, anthropic_base_url=None,
        api_key_env=f"{pid.upper()}_KEY", models=("think",),
        max_output_tokens=cap)
        for pid, cap in per_provider.items()}


def test_a2_fallback_vendor_cap_resolved_per_candidate():
    """主模型供应商没设 cap、fallback 供应商设了 16384:只警主模型,别误导用户去改 B。"""
    warnings = _dry_run_warnings(spec_from_yaml(CROSS_YAML),
                                 provider_cfgs=_vendor_cfgs(VendorB=16384),
                                 capabilities=CROSS_CAPS, rates_known_fn=lambda _: True)
    assert len(warnings) == 1
    assert "VendorA:think" in warnings[0] and "VendorA 条目" in warnings[0]


def test_a2_primary_vendor_cap_does_not_mask_low_fallback():
    """主模型供应商 16384 不能遮蔽 fallback 供应商仍是 8192 的事实(原实现漏报)。"""
    warnings = _dry_run_warnings(spec_from_yaml(CROSS_YAML),
                                 provider_cfgs=_vendor_cfgs(VendorA=16384),
                                 capabilities=CROSS_CAPS, rates_known_fn=lambda _: True)
    assert len(warnings) == 1
    assert "VendorB:think" in warnings[0] and "VendorB 条目" in warnings[0]
    assert "生效值 8192" in warnings[0]


def test_broken_capabilities_table_does_not_break_rendering(monkeypatch):
    """能力表损坏只是少警告,不让 dry-run 渲染失败(审查 2026-08-23 发现 2)。"""
    import atlas.mcp as mcp_module
    import atlas.thinking as thinking

    def raise_json_error():
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    monkeypatch.setattr(thinking, "load_capabilities", raise_json_error)
    assert mcp_module._dry_run_warnings(_spec()) == []

    def raise_unsupported():
        raise thinking.ThinkingUnsupported("kind 非法")

    monkeypatch.setattr(thinking, "load_capabilities", raise_unsupported)
    assert mcp_module._dry_run_warnings(_spec()) == []


# ─────────────────── rates_known 与接线 ───────────────────


def test_rates_known_matches_compute_cost_resolution(tmp_path, monkeypatch):
    import atlas.costs as costs
    pricing = {"prices": {
        "Fake:priced": {"input_per_m": 1.0, "output_per_m": 2.0},
        "Fake:*": {"input_per_m": 0.5, "output_per_m": 0.5},
        "Fake:nullish": {"input_per_m": None, "output_per_m": None},
    }}
    path = tmp_path / "pricing.json"
    path.write_text(__import__("json").dumps(pricing), encoding="utf-8")
    monkeypatch.setattr(costs, "_PRICING_PATH", path)
    costs.reload_pricing()
    try:
        assert costs.rates_known("Fake:priced") is True
        assert costs.rates_known("Fake:wild") is True        # 供应商通配
        assert costs.rates_known("Fake:nullish") is False    # 显式 null = 未知
        assert costs.rates_known("Other:absent") is False
    finally:
        costs.reload_pricing()


def test_dry_run_impl_returns_warnings_key(tmp_path, monkeypatch):
    import atlas.mcp as mcp_module
    from conftest import make_registry
    from atlas.adapters import FakeProvider

    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "warn.yaml").write_text(WARN_YAML.strip(), encoding="utf-8")
    monkeypatch.setattr(mcp_module, "WORKFLOWS_DIR", workflows)
    monkeypatch.setattr(mcp_module, "RUNS_DIR", tmp_path / "runs")

    fake = FakeProvider()
    fake.configure("primary", text="ok")
    fake.configure("backup", text="ok")
    dry = mcp_module.dry_run_impl("warn", "task",
                                  registry_factory=lambda _: make_registry(fake))
    assert dry["dry_run"] is True
    assert dry["warnings"] == []   # 接线存在;真实配置下 Fake 未探测/无帽 → 空
