# -*- coding: utf-8 -*-
"""零成本校验:幻觉 YAML 必须在校验期被拒绝,不花一分钱。

每条用例对应一类真实会发生的模型幻觉/手写错误,断言错误消息
指向具体问题(M2 的 validate 工具会把这些消息原样给 agent)。"""
import json
import time

import pytest

import atlas.spec as spec_module
from atlas.spec import (
    SpecError,
    spec_fingerprint,
    spec_from_snapshot,
    spec_from_yaml,
    spec_to_snapshot,
)

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


def test_allowed_paths_require_a_read_only_agent(tmp_path):
    extra = tmp_path / "extra"
    extra.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    yaml_text = f"""
name: bad-writable-paths
nodes:
  - id: coder
    type: coding_agent
    model: Fake:agent
    prompt: p
    consumes: [task]
    workdir: {workdir.as_posix()}
    writable: true
    allowed_paths:
      - {extra.as_posix()}
"""
    with pytest.raises(SpecError, match="writable.*allowed_paths"):
        spec_from_yaml(yaml_text)


@pytest.mark.parametrize("value", ["''", "false", "0", ""])
def test_allowed_paths_reject_falsey_non_lists(value):
    yaml_text = f"""
name: bad-path-type
nodes:
  - id: scout
    type: research
    prompt: p
    consumes: [task]
    allowed_paths: {value}
"""
    with pytest.raises(SpecError, match="allowed_paths.*路径数组"):
        spec_from_yaml(yaml_text)


def _spec_error(yaml_text):
    with pytest.raises(SpecError) as caught:
        spec_from_yaml(yaml_text, source="workflow.yaml")
    return caught.value


def test_semantic_errors_carry_canonical_path_and_exact_source_mark():
    top = _spec_error("""name: bad
magic: true
nodes: []
""")
    assert (top.path, top.line, top.column) == ("magic", 2, 1)
    assert "未知字段" in top.message
    assert "path magic, line 2, column 1" in str(top)

    node = _spec_error("""name: bad
nodes:
  - id: node_a
    type: magic_script
    prompt: p
""")
    assert (node.path, node.line, node.column) == ("nodes[0].type", 4, 5)
    assert "封闭清单" in node.message

    guard = _spec_error("""name: bad
nodes:
  - id: node_a
    type: llm
    prompt: p
guards:
  timeout_s: false
""")
    assert (guard.path, guard.line, guard.column) == ("guards.timeout_s", 7, 3)
    assert "timeout_s" in guard.message

    unknown_node = _spec_error("""name: bad
nodes:
  - id: node_a
    type: llm
    prompt: p
    surprise: true
""")
    assert (unknown_node.path, unknown_node.line, unknown_node.column) == (
        "nodes[0].surprise", 6, 5)

    unknown_guard = _spec_error("""name: bad
nodes:
  - id: node_a
    type: llm
    prompt: p
guards:
  surprise: true
""")
    assert (unknown_guard.path, unknown_guard.line, unknown_guard.column) == (
        "guards.surprise", 7, 3)

    guard_shape = _spec_error("""name: bad
nodes:
  - id: node_a
    type: llm
    prompt: p
guards: invalid
""")
    assert (guard_shape.path, guard_shape.line, guard_shape.column) == (
        "guards", 6, 1)


@pytest.mark.parametrize("field", ["from", "to", "surprise"])
def test_edge_errors_point_to_the_specific_edge_field(field):
    edge_line = {
        "from": "  - from: ghost\n    to: END",
        "to": "  - from: node_a\n    to: ghost",
        "surprise": "  - from: node_a\n    to: END\n    surprise: true",
    }[field]
    error = _spec_error(f"""name: bad
nodes:
  - id: node_a
    type: llm
    prompt: p
edges:
{edge_line}
""")
    expected_line = 7 if field == "from" else (8 if field == "to" else 9)
    assert (error.path, error.line, error.column) == (
        f"edges[0].{field}", expected_line, 5)


def test_consumes_and_conditional_routing_use_best_source_path():
    consumes = _spec_error("""name: bad
nodes:
  - id: node_a
    type: llm
    prompt: p
    consumes:
      - task
      - ghost.output
""")
    assert (consumes.path, consumes.line, consumes.column) == (
        "nodes[0].consumes[1]", 8, 9)

    routing = _spec_error("""name: bad
nodes:
  - id: node_a
    type: llm
    prompt: p
    output_schema:
      required: [conclusion]
    route_field: verdict
edges:
  - from: node_a
    when: done
    to: END
""")
    assert (routing.path, routing.line, routing.column) == (
        "nodes[0].output_schema.required", 7, 7)
    assert "路由字段" in routing.message


@pytest.mark.parametrize("yaml_text, expected", [
    ("""name: duplicate
nodes:
  - id: node_a
    type: llm
    type: human
    prompt: p
""", ("nodes[0].type", 5, 5)),
    ("""name: duplicate
nodes:
  - id: node_a
    type: research
    prompt: p
    allow_web: false
    allow_web: true
""", ("nodes[0].allow_web", 7, 5)),
    ("""name: duplicate
nodes:
  - id: node_a
    type: coding_agent
    prompt: p
    writable: false
    writable: true
""", ("nodes[0].writable", 7, 5)),
    ("""name: duplicate
nodes:
  - id: node_a
    type: llm
    prompt: p
guards:
  timeout_s: 1
  timeout_s: 2
""", ("guards.timeout_s", 8, 3)),
    ("""name: duplicate
guards: {}
nodes:
  - id: node_a
    type: llm
    prompt: p
guards:
  timeout_s: 2
""", ("guards", 7, 1)),
])
def test_duplicate_mapping_keys_are_rejected_at_second_key(yaml_text, expected):
    error = _spec_error(yaml_text)
    assert (error.path, error.line, error.column) == expected
    assert "重复键" in error.message


def test_duplicate_keys_use_yaml_key_equality():
    error = _spec_error("""name: duplicate
nodes:
  - id: node_a
    type: llm
    prompt: p
meta:
  true: first
  yes: second
""")
    assert (error.path, error.line, error.column) == ("meta.yes", 8, 3)
    assert "重复键" in error.message


@pytest.mark.parametrize("yaml_text, fragment, position", [
    ("name: &shared bad\nnodes: []\n", "anchor", (1, 7)),
    ("name: bad\nnodes: [*missing]\n", "alias", (2, 9)),
    ("""name: bad
nodes:
  - id: node_a
    <<: {type: llm}
    prompt: p
""", "merge key", (4, 5)),
])
def test_yaml_references_and_merge_keys_are_rejected(yaml_text, fragment, position):
    started = time.monotonic()
    error = _spec_error(yaml_text)
    elapsed = time.monotonic() - started
    assert fragment in error.message
    assert (error.line, error.column) == position
    assert elapsed < 1.0
    if fragment == "merge key":
        assert error.path == 'nodes[0]["<<"]'


def test_alias_bomb_fails_closed_before_expansion():
    yaml_text = "seed: &seed [x, x]\n" + "\n".join(
        f"level{i}: &level{i} [*seed, *seed]" for i in range(200)
    )
    started = time.monotonic()
    error = _spec_error(yaml_text)
    assert time.monotonic() - started < 1.0
    assert "anchor" in error.message
    assert (error.line, error.column) == (1, 7)


def test_yaml_input_byte_limit_rejects_before_compose():
    yaml_text = "name: big\n" + "#" * (spec_module._MAX_YAML_BYTES + 1)
    started = time.monotonic()
    error = _spec_error(yaml_text)
    assert time.monotonic() - started < 1.0
    assert "字节上限" in error.message
    assert error.line is None and error.column is None


def test_yaml_rejects_non_utf8_unicode_as_spec_error():
    error = _spec_error("name: bad\ud800\nnodes: []\n")
    assert "无法编码为 UTF-8" in error.message
    assert (error.line, error.column) == (1, 10)


def test_yaml_compose_node_limit(monkeypatch):
    monkeypatch.setattr(spec_module, "_MAX_COMPOSE_NODES", 8)
    error = _spec_error("""name: nodes
nodes:
  - id: node_a
    type: llm
    prompt: p
""")
    assert "compose 节点数超过上限 8" in error.message
    assert error.line is not None and error.column is not None


def test_yaml_depth_limit_rejects_plain_nested_structure():
    depth = spec_module._MAX_YAML_DEPTH + 2
    yaml_text = "name: deep\nnodes:\n  - " + "[" * depth + "x" + "]" * depth
    started = time.monotonic()
    error = _spec_error(yaml_text)
    assert time.monotonic() - started < 1.0
    assert "嵌套深度超过上限" in error.message
    assert error.line is not None and error.column is not None


def test_yaml_collection_size_limit_rejects_plain_large_sequence():
    values = ", ".join("x" for _ in range(spec_module._MAX_COLLECTION_ITEMS + 1))
    started = time.monotonic()
    error = _spec_error(f"name: wide\nnodes: [{values}]\n")
    assert time.monotonic() - started < 1.0
    assert "集合规模超过上限" in error.message
    assert (error.line, error.column) == (2, 8)


def test_nested_validation_errors_point_to_leaf_paths():
    required = _spec_error("""name: bad
nodes:
  - id: node_a
    type: llm
    prompt: p
    output_schema:
      required:
        - verdict
        - 0
""")
    assert (required.path, required.line, required.column) == (
        "nodes[0].output_schema.required[1]", 9, 11)

    requires = _spec_error("""name: bad
nodes:
  - id: node_a
    type: llm
    prompt: p
meta:
  requires:
    workdir: "yes"
""")
    assert (requires.path, requires.line, requires.column) == (
        "meta.requires.workdir", 8, 5)

    unknown = _spec_error("""name: bad
nodes:
  - id: node_a
    type: llm
    prompt: p
meta:
  requires:
    surprise: true
""")
    assert (unknown.path, unknown.line, unknown.column) == (
        "meta.requires.surprise", 8, 5)


def test_syntax_error_is_normalized_with_one_based_mark():
    error = _spec_error("name: bad\nnodes: [unclosed\n")
    assert error.path is None
    assert (error.line, error.column) == (3, 1)
    assert "不是合法 YAML" in error.message
    assert "line 3, column 1" in str(error)


def test_aggregate_error_has_path_but_does_not_invent_source_location():
    error = _spec_error("""name: bad
entry: node_a
nodes:
  - id: node_a
    type: llm
    prompt: p
  - id: node_b
    type: llm
    prompt: p
  - id: node_c
    type: llm
    prompt: p
edges:
  - from: node_a
    to: END
  - from: node_b
    to: node_c
  - from: node_c
    to: node_b
""")
    assert error.path == "nodes"
    assert error.line is None
    assert error.column is None
    assert "不可达" in error.message


def test_snapshot_validation_degrades_without_source_coordinates():
    snapshot = {
        "name": "bad-snapshot",
        "nodes": [{
            "id": "node_a", "type": "llm", "model": "", "prompt": "p",
            "consumes": ["ghost.output"],
        }],
        "edges": [],
        "entry": "node_a",
        "guards": {},
    }
    with pytest.raises(SpecError) as caught:
        spec_from_snapshot(snapshot)
    error = caught.value
    assert error.path == "nodes[0].consumes[0]"
    assert error.line is None
    assert error.column is None


def test_source_marks_do_not_change_snapshot_or_fingerprint():
    yaml_text = """name: stable
nodes:
  - id: node_a
    type: llm
    model: Fake:primary
    prompt: p
    consumes: [task]
edges:
  - from: node_a
    to: END
"""
    parsed = spec_from_yaml(yaml_text)
    snapshot = spec_to_snapshot(parsed)
    rebuilt = spec_from_snapshot(json.loads(json.dumps(snapshot)))

    assert spec_to_snapshot(rebuilt) == snapshot
    assert spec_fingerprint(rebuilt) == spec_fingerprint(parsed)
    assert "line" not in json.dumps(snapshot)
    assert "column" not in json.dumps(snapshot)


def test_good_fixtures_load():
    for name in ("two_node", "three_node", "loop", "parallel",
                 "broken_wiring", "big_prompt"):
        spec = load_graph(name)
        assert spec.name and spec.nodes
    loop = load_graph("loop")
    assert loop.entry == "maker"
    assert loop.guards.max_iterations == 3


def test_windows_reserved_node_id_rejected_at_validation():
    """节点 id 会成为产物文件名;con/prn/com1 等保留名在 Windows 上是设备,
    exists() 恒 True,write-once 命名会误判冲突。必须在校验期拒绝。"""
    from atlas.spec import SpecError, spec_from_yaml
    for reserved in ("con", "CON", "prn.writer", "com1", "lpt9.x"):
        yaml_text = (f"name: g\nnodes:\n  - id: {reserved}\n    type: llm\n"
                     "    model: Fake:x\n    prompt: p\n    consumes: [task]\n"
                     "edges:\n  - from: " + reserved + "\n    to: END\n")
        with pytest.raises(SpecError, match="保留设备名"):
            spec_from_yaml(yaml_text)
    # 非保留的正常 id(含保留名作为后缀段)不受影响
    ok = spec_from_yaml(
        "name: g\nnodes:\n  - id: my.con\n    type: llm\n    model: Fake:x\n"
        "    prompt: p\n    consumes: [task]\nedges:\n  - from: my.con\n    to: END\n")
    assert ok.node("my.con") is not None
