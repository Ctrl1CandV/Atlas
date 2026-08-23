# -*- coding: utf-8 -*-
"""A7 · 多厂商真的成立:三个节点实际用了三个不同的模型;降级必须可见。"""
import json

import pytest

from atlas.adapters import FakeProvider
from atlas.config import PROJECT_ROOT
from atlas.engine import execute_graph
from atlas.spec import spec_from_yaml_file

from conftest import TASK_TEXT, GRAPHS, make_registry


def test_a7_heterogeneity_fake_three_vendors(tmp_path):
    """CI 版:假供应商三家,断言机制(三个不同 model_used + 降级可见)。"""
    spec = spec_from_yaml_file(GRAPHS / "three_vendors.yaml")
    # 把真实模型引用换成假供应商三家
    from dataclasses import replace
    remap = {"Deepseek:deepseek-v4-flash": "Fake:vendor1",
             "SuperAI:glm-5.3": "Fake:vendor2",
             "Minimax:MiniMax-M3": "Fake:vendor3"}
    spec = replace(spec, nodes=[replace(n, model=remap[n.model]) for n in spec.nodes])

    fake = FakeProvider()
    for v, text in (("vendor1", "第一家的理解"),
                    ("vendor2", json.dumps({"角度": "成本"}, ensure_ascii=False)),
                    ("vendor3", "第三家的收束")):
        fake.configure(v, text=text)

    run = execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                        registry=make_registry(fake))
    used = [e["model_used"] for e in run.events.filter(type="node_done")]
    assert len(set(used)) == 3, f"三个节点实际只用了 {len(set(used))} 个模型:{used}"

    for e in run.events.filter(type="node_done"):
        if e["model_used"] != e["model_requested"]:
            assert e["degraded"] is True


def test_a7_degradation_is_visible(tmp_path):
    """降级可见:主模型假成功 → fallback 顶上 → node_done 标 degraded。"""
    from atlas.spec import spec_from_yaml
    spec = spec_from_yaml("""
name: degradable
nodes:
  - id: a
    type: llm
    model: Fake:vendor1
    fallback: [Fake:vendor2]
    prompt: p
    consumes: [task]
edges:
  - from: a
    to: END
""")
    fake = FakeProvider()
    fake.configure("vendor1", text="")          # 假成功
    fake.configure("vendor2", text="备用内容")
    run = execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                        registry=make_registry(fake))
    done = run.events.find(type="node_done")
    assert done["model_used"] == "Fake:vendor2"
    assert done["degraded"] is True
    assert run.events.find(type="model_failed", model="Fake:vendor1")


@pytest.mark.real_api
def test_a7_heterogeneity_real():
    """真实版(手动:uv run pytest -m real_api):三家供应商真金白银。"""
    from atlas.adapters import build_real_registry

    spec = spec_from_yaml_file(GRAPHS / "three_vendors.yaml")
    registry = build_real_registry(["Deepseek", "SuperAI", "Minimax"])
    run = execute_graph(spec, task="Atlas A7 验证:各用一句话给出不同视角的分析。",
                        runs_root=PROJECT_ROOT / "runs", registry=registry)

    used = [e["model_used"] for e in run.events.filter(type="node_done")]
    assert len(set(used)) == 3, f"三个节点实际只用了 {len(set(used))} 个模型:{used}"
    for e in run.events.filter(type="node_done"):
        if e["model_used"] != e["model_requested"]:
            assert e["degraded"] is True
