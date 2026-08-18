# -*- coding: utf-8 -*-
"""多入口(M4)+ A11 图必能终止(静态+动态双保险)。

扇入等待(M1 已有,LangGraph 天然等待全部入边)在这里一并覆盖:
双入口 → join 汇合,join 的投影必须含两条腿的完整产物。
"""
import pytest

from atlas.adapters import FakeProvider
from atlas.engine import GuardViolation, execute_graph
from atlas.spec import SpecError, spec_from_yaml

from conftest import TASK_TEXT, make_registry

DUAL_ENTRY = """
name: dual
nodes:
  - id: left
    type: llm
    model: Fake:left
    prompt: 左腿。
    consumes: [task]
  - id: right
    type: llm
    model: Fake:right
    prompt: 右腿。
    consumes: [task]
  - id: join
    type: llm
    model: Fake:joiner
    prompt: 汇总。
    consumes: [task, left.output, right.output]
edges:
  - from: left
    to: join
  - from: right
    to: join
  - from: join
    to: END
"""


def _fake():
    fake = FakeProvider()
    fake.configure("left", text="左腿结论")
    fake.configure("right", text="右腿结论")
    fake.configure("joiner", text="汇总完成")
    return fake


def test_multi_entry_inferred_from_roots(tmp_path):
    """不写 entry、两个根节点:自动成为双入口,并行开跑。"""
    spec = spec_from_yaml(DUAL_ENTRY)
    assert set(spec.all_entries()) == {"left", "right"}

    run = execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                        registry=make_registry(_fake()))
    assert run.folded()["status"] == "done"
    assert len(run.events.filter(type="node_done")) == 3

    # 扇入:join 的投影含两条腿的完整产物(A1 语义 × 2)
    join_in = run.events.find(type="node_input", node="join")
    names = {c["name"] for c in join_in["consumed"]}
    assert names == {"task", "left.output", "right.output"}


def test_explicit_entry_list(tmp_path):
    spec = spec_from_yaml("entry: [right, left]\n" + DUAL_ENTRY)
    assert spec.all_entries() == ("right", "left")   # 显式顺序保留
    run = execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                        registry=make_registry(_fake()))
    assert run.folded()["status"] == "done"


def test_explicit_entry_unknown_rejected():
    with pytest.raises(SpecError, match="不在节点清单里"):
        spec_from_yaml("entry: [ghost]\n" + DUAL_ENTRY)


def test_snapshot_preserves_multi_entry(tmp_path):
    from atlas.spec import spec_fingerprint, spec_from_snapshot, spec_to_snapshot
    import json
    spec = spec_from_yaml(DUAL_ENTRY)
    rebuilt = spec_from_snapshot(json.loads(json.dumps(spec_to_snapshot(spec))))
    assert set(rebuilt.all_entries()) == {"left", "right"}
    assert spec_fingerprint(rebuilt) == spec_fingerprint(spec)


# ── A11:图必能终止(静态 + 动态)─────────────────────────────

NON_CONVERGING_LOOP = """
name: stuck
entry: maker
nodes:
  - id: maker
    type: llm
    model: Fake:looper
    prompt: 产出方案。
    consumes: [task]
  - id: judge
    type: llm
    model: Fake:judge
    prompt: 审查。
    consumes: [task, maker.output]
    output_schema:
      required: [verdict]
edges:
  - from: maker
    to: judge
  - from: judge
    when: repair
    to: maker
  - from: judge
    when: done
    to: END
guards:
  max_iterations: 3
"""


def test_a11_static_guards():
    """静态双保险的一半:无出口环拒收、有环必设上限。"""
    # 死环:环上节点的全部出边都在环内且无条件 → 拒收
    dead_loop = """
name: dead
entry: maker
nodes:
  - id: maker
    type: llm
    model: Fake:looper
    prompt: p
    consumes: [task]
  - id: judge
    type: llm
    model: Fake:judge
    prompt: p
    consumes: [task]
edges:
  - from: maker
    to: judge
  - from: judge
    to: maker
guards:
  max_iterations: 3
"""
    with pytest.raises(SpecError, match="没有条件出口"):
        spec_from_yaml(dead_loop)
    # 有环没设 max_iterations → 拒收
    with pytest.raises(SpecError, match="max_iterations"):
        spec_from_yaml(NON_CONVERGING_LOOP.replace(
            "guards:\n  max_iterations: 3\n", ""))


def test_a11_dynamic_never_converging_loop_stops():
    """动态双保险:裁决模型永远要求返工 → max_iterations 拦停并记账。"""
    fake = FakeProvider()
    fake.configure("looper", text="第 N 版")
    fake.configure("judge", text='{"verdict": "repair"}')   # 永不收敛
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        with pytest.raises(GuardViolation):
            execute_graph(spec_from_yaml(NON_CONVERGING_LOOP),
                          task=TASK_TEXT, runs_root=Path(td),
                          registry=make_registry(fake))
        from atlas.events import EventReader
        events_path = next(Path(td).glob("*/events.jsonl"))
        reader = EventReader(events_path)
        assert reader.find(type="run_failed")["error_type"] == "GuardViolation"
        assert len(reader.filter(type="node_done", node="maker")) == 3
