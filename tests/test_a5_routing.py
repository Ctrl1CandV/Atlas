# -*- coding: utf-8 -*-
"""A5 · 路由是纯查表:过程零模型调用;匹配不到就报错,不猜。"""
import pytest

from atlas.adapters import FakeProvider
from atlas.engine import NoRouteError, execute_graph, resolve_route

from conftest import TASK_TEXT, load_graph, make_registry


def test_a5_routing_is_pure_lookup():
    spec = load_graph("loop")
    assert resolve_route(spec, "judge", {"verdict": "repair"}) == "maker"
    assert resolve_route(spec, "judge", {"verdict": "done"}) == "END"


def test_a5_missing_route_field_fails():
    spec = load_graph("loop")
    with pytest.raises(NoRouteError) as e:
        resolve_route(spec, "judge", {"comment": "没有路由字段"})
    assert "路由字段" in str(e.value)


def test_a5b_unknown_verdict_fails():
    # 输出里的 verdict 匹配不到任何边 → 报错,不猜
    spec = load_graph("loop")
    with pytest.raises(NoRouteError) as e:
        resolve_route(spec, "judge", {"verdict": "???"})
    assert "匹配不到任何出边" in str(e.value)


def test_a5_runtime_loop_and_call_count(tmp_path):
    """循环跑通 + 路由零模型调用:总调用数恰等于节点执行数。"""
    fake = FakeProvider()
    fake.configure("looper", sequence=["第一版方案", "第二版方案(修复后)"])
    fake.configure("judge", sequence=['{"verdict": "repair"}', '{"verdict": "done"}'])

    run = execute_graph(load_graph("loop"), task=TASK_TEXT,
                        runs_root=tmp_path, registry=make_registry(fake))

    # maker 两次、judge 两次
    assert len(run.events.filter(type="node_done", node="maker")) == 2
    assert len(run.events.filter(type="node_done", node="judge")) == 2

    # 每轮产物都留档,不覆盖(第 2 轮是最新)
    assert (run.dir / "artifacts" / "maker.output.1.txt").exists()
    assert (run.dir / "artifacts" / "maker.output.2.txt").exists()
    assert run.artifacts["maker.output"].sha256 == \
        run.events.filter(type="node_done", node="maker")[-1]["output_sha256"]

    # 每次执行都有独立的投影(maker 第 2 轮的投影含 judge 第 1 轮的意见)
    assert (run.dir / "projections" / "maker.input.2.txt").exists()

    # A5 核心:4 次节点执行 = 4 次模型调用,路由过程零调用
    assert len(fake.calls) == 4, (
        f"路由过程调用了模型:4 次节点执行产生了 {len(fake.calls)} 次模型调用"
    )
    assert run.folded()["status"] == "done"


def test_a5b_runtime_unknown_verdict_fails_run(tmp_path):
    from atlas.events import EventReader

    fake = FakeProvider()
    fake.configure("looper", text="方案")
    fake.configure("judge", text='{"verdict": "???"}')  # 匹配不到任何边

    with pytest.raises(NoRouteError) as e:
        execute_graph(load_graph("loop"), task=TASK_TEXT,
                      runs_root=tmp_path, registry=make_registry(fake))
    assert "匹配不到任何出边" in str(e.value)

    log = next(tmp_path.glob("*/events.jsonl"))
    reader = EventReader(log)
    assert reader.find(type="run_failed")["error_type"] == "NoRouteError"
