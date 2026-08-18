# -*- coding: utf-8 -*-
"""真实运行验证 M4 思考深度映射(real_api,每个调用几美分)。

覆盖两种能力形态:
- effort(Deepseek,openai 端点):thinking: high → reasoning_effort=high,
  响应 reasoning_tokens > 0,无 effort_ineffective
- budget(Kiro:claude-opus-4-8,anthropic 端点):thinking: low → budget 1024,
  响应出现 thinking 块(reasoning_tokens=1)
"""
import pytest

from atlas.adapters import build_real_registry
from atlas.config import PROJECT_ROOT
from atlas.engine import execute_graph
from atlas.spec import spec_from_yaml

EFFORT_YAML = """
name: real-thinking-effort
nodes:
  - id: solo
    type: llm
    model: Deepseek:deepseek-v4-flash
    prompt: 一句话回答:把 0.1 加 0.2 的浮点误差解释清楚。
    consumes: [task]
    thinking: high
edges:
  - from: solo
    to: END
"""

BUDGET_YAML = """
name: real-thinking-budget
nodes:
  - id: solo
    type: llm
    model: Kiro:claude-opus-4-8
    prompt: 一句话回答:把 0.1 加 0.2 的浮点误差解释清楚。
    consumes: [task]
    thinking: low
edges:
  - from: solo
    to: END
"""


@pytest.mark.real_api
def test_real_effort_tier_effective():
    run = execute_graph(spec_from_yaml(EFFORT_YAML), task="思考档位真实验证",
                        runs_root=PROJECT_ROOT / "runs",
                        registry=build_real_registry(["Deepseek"]))
    done = run.events.find(type="node_done", node="solo")
    assert run.folded()["status"] == "done"
    assert run.events.find(type="effort_ineffective") is None, \
        "effort 能力的模型设了档位却没生效——映射或能力表错了"
    print(f"reasoning_tokens={done['output_tokens']}(含思考),"
          f"model={done['model_used']}")


@pytest.mark.real_api
def test_real_budget_tier_effective():
    run = execute_graph(spec_from_yaml(BUDGET_YAML), task="思考档位真实验证",
                        runs_root=PROJECT_ROOT / "runs",
                        registry=build_real_registry(["Kiro"]))
    assert run.folded()["status"] == "done"
    assert run.events.find(type="effort_ineffective") is None, \
        "budget 能力的模型设了档位却没生效——映射或能力表错了"
