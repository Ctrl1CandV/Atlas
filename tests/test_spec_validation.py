# -*- coding: utf-8 -*-
"""零成本校验:幻觉 YAML 必须在校验期被拒绝,不花一分钱。

每条用例对应一类真实会发生的模型幻觉/手写错误,断言错误消息
指向具体问题(M2 的 validate 工具会把这些消息原样给 agent)。"""
import pytest

from atlas.spec import SpecError, spec_from_yaml

from conftest import load_graph

GOOD_NODE_A = """
  - id: node_a
    type: llm
    model: Fake:primary
    prompt: 干活。
    consumes: [task]
"""

BAD_CASES = [
    ("未知节点类型", f"""
name: bad
nodes:
  - id: node_a
    type: magic_script
    model: Fake:primary
    prompt: 干活。
    consumes: [task]
""", "封闭清单"),

    ("边指向不存在的节点", f"""
name: bad
nodes:{GOOD_NODE_A}
edges:
  - from: node_a
    to: ghost
""", "不是任何节点"),

    ("条件边与无条件边混用", f"""
name: bad
nodes:
  - id: node_a
    type: llm
    model: Fake:primary
    prompt: 干活。
    consumes: [task]
    output_schema:
      required: [verdict]
  - id: node_b
    type: llm
    model: Fake:other
    prompt: 干活。
    consumes: [task, node_a.output]
edges:
  - from: node_a
    to: node_b
  - from: node_a
    when: go
    to: END
""", "混用"),

    ("when 值重复", """
name: bad
nodes:
  - id: node_a
    type: llm
    model: Fake:primary
    prompt: 干活。
    consumes: [task]
    output_schema:
      required: [verdict]
  - id: node_b
    type: llm
    model: Fake:other
    prompt: 干活。
    consumes: [task, node_a.output]
edges:
  - from: node_a
    when: go
    to: node_b
  - from: node_a
    when: go
    to: END
""", "when 值重复"),

    ("死环:没有条件出口", """
name: bad
nodes:
  - id: node_a
    type: llm
    model: Fake:primary
    prompt: 干活。
    consumes: [task]
  - id: node_b
    type: llm
    model: Fake:other
    prompt: 干活。
    consumes: [task]
entry: node_a
edges:
  - from: node_a
    to: node_b
  - from: node_b
    to: node_a
guards:
  max_iterations: 3
""", "没有条件出口"),

    ("有环但没设 max_iterations", """
name: bad
nodes:
  - id: node_a
    type: llm
    model: Fake:primary
    prompt: 干活。
    consumes: [task]
    output_schema:
      required: [verdict]
entry: node_a
edges:
  - from: node_a
    when: again
    to: node_a
  - from: node_a
    when: stop
    to: END
""", "max_iterations"),

    ("不可达节点(非根且从入口到不了)", """
name: bad
nodes:
  - id: node_a
    type: llm
    model: Fake:primary
    prompt: 干活。
    consumes: [task]
  - id: node_b
    type: llm
    model: Fake:other
    prompt: 干活。
    consumes: [task]
  - id: node_c
    type: llm
    model: Fake:third
    prompt: 干活。
    consumes: [task]
edges:
  - from: node_a
    to: END
  - from: node_b
    to: node_c
  - from: node_c
    to: node_b
""", "不可达"),

    ("entry 指向不存在的节点", """
name: bad
entry: ghost
nodes:
""" + GOOD_NODE_A + """
edges:
  - from: node_a
    to: END
""", "不在节点清单里"),

    ("条件边的路由字段不在 required 里", """
name: bad
nodes:
  - id: node_a
    type: llm
    model: Fake:primary
    prompt: 干活。
    consumes: [task]
    output_schema:
      required: [conclusion]
    route_field: verdict
edges:
  - from: node_a
    when: go
    to: END
  - from: node_a
    when: stop
    to: END
""", "路由字段"),

    ("consumes 引用不存在的产物名", """
name: bad
nodes:
  - id: node_a
    type: llm
    model: Fake:primary
    prompt: 干活。
    consumes: [task, ghost.output]
""", "不存在能产出它的节点"),

    ("模型引用格式错误", """
name: bad
nodes:
  - id: node_a
    type: llm
    model: just-a-model-name
    prompt: 干活。
    consumes: [task]
""", "供应商id:模型id"),

    ("顶层未知字段", """
name: bad
magic: true
nodes:
""" + GOOD_NODE_A, "未知字段"),

    ("YAML 语法错误", """
name: bad
nodes: [unclosed
""", "不是合法 YAML"),

    ("缺 name", """
nodes:
""" + GOOD_NODE_A, "name"),

    ("consumes 为空", """
name: bad
nodes:
  - id: node_a
    type: llm
    model: Fake:primary
    prompt: 干活。
    consumes: []
""", "非空"),
]


@pytest.mark.parametrize("case, yaml_text, fragment", BAD_CASES,
                         ids=[c[0] for c in BAD_CASES])
def test_bad_yaml_rejected_at_zero_cost(case, yaml_text, fragment):
    with pytest.raises(SpecError) as e:
        spec_from_yaml(yaml_text)
    assert fragment in str(e.value), (
        f"{case}:错误消息应包含 {fragment!r},实际是:{e.value}"
    )


@pytest.mark.parametrize("value", ["true", ".nan", ".inf", "-.inf"])
def test_guard_timeout_rejects_bool_and_non_finite(value):
    yaml_text = f"""
name: bad-timeout
nodes:{GOOD_NODE_A}
guards:
  timeout_s: {value}
"""
    with pytest.raises(SpecError, match="timeout_s.*有限数"):
        spec_from_yaml(yaml_text)


def test_good_fixtures_load():
    for name in ("two_node", "three_node", "loop", "parallel",
                 "broken_wiring", "big_prompt"):
        spec = load_graph(name)
        assert spec.name and spec.nodes
    loop = load_graph("loop")
    assert loop.entry == "maker"
    assert loop.guards.max_iterations == 3
