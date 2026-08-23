# -*- coding: utf-8 -*-
"""M1 闸门项:条件边、循环(见 test_a5)、并行各跑通一次。

并行:fan 无条件扇出 → left/right 同超步执行 → join 在下一超步
消费两者。join 的投影必须同时包含 left 和 right 的完整原文。"""
from pathlib import Path

from atlas.adapters import FakeProvider
from atlas.engine import execute_graph
from atlas.integrity import sha256_bytes

from conftest import TASK_TEXT, load_graph, make_registry


def test_parallel_fan_out_and_join(tmp_path):
    fake = FakeProvider()
    fake.configure("primary", text="任务拆解:左看性能,右看成本。")
    fake.configure("left", text="左方向结论:性能瓶颈在锁。")
    fake.configure("right", text="右方向结论:成本瓶颈在流量。")
    fake.configure("joiner", text="汇总:左右已合并。")

    run = execute_graph(load_graph("parallel"), task=TASK_TEXT,
                        runs_root=tmp_path, registry=make_registry(fake))

    # 三个工作节点全部完成(join 最后)
    assert len(run.events.filter(type="node_done")) == 4
    assert run.folded()["status"] == "done"

    # join 的投影同时包含 left 与 right 的完整产物(A1 语义,双份)
    join_input = run.events.find(type="node_input", node="join")
    projection = Path(join_input["projection_path"]).read_bytes()
    consumed = {c["name"]: c for c in join_input["consumed"]}
    for name in ("left.output", "right.output"):
        src = Path(consumed[name]["path"]).read_bytes()
        assert src in projection, f"{name} 没有完整到达 join"
        assert consumed[name]["sha256"] == sha256_bytes(src)

    # 汇合产物存在且未降级
    done = run.events.find(type="node_done", node="join")
    assert done["degraded"] is False
    assert "join.output" in run.artifacts
