# -*- coding: utf-8 -*-
"""A1 · 完整性(命门):从源产物重算,断言字节级相等。

60k 字符的产物走完全程:源产物字节必须完整出现在下游的投影里,
哈希必须对得上。小的证明不了任何事——历史上丢的都是 40k 级的 diff。
"""
from pathlib import Path

from atlas.engine import execute_graph
from atlas.integrity import sha256_bytes

from conftest import TASK_TEXT, load_graph, make_registry, standard_fake


def test_a1_no_silent_loss(tmp_path):
    run = execute_graph(load_graph("two_node"), task=TASK_TEXT,
                        runs_root=tmp_path, registry=make_registry(standard_fake()))

    # 节点 A 的产物原文(≥60k 字符,A1 的规模要求)
    source = (run.dir / "artifacts" / "node_a.output.1.json").read_bytes()
    assert len(source.decode("utf-8")) >= 60_000, "A1 必须用 60k 字符级产物测"

    # 节点 B 实际收到的投影(从 node_input 事件取)
    input_event = run.events.find(type="node_input", node="node_b")
    assert input_event is not None, "node_b 没有 node_input 事件"
    projection = Path(input_event["projection_path"]).read_bytes()
    assert input_event["projection_sha256"] == sha256_bytes(projection)

    # 源产物的全部字节必须出现在投影里
    assert source in projection, "节点 A 的产物没有完整到达节点 B"

    # 哈希也必须对得上
    consumed = {c["name"]: c for c in input_event["consumed"]}
    assert consumed["node_a.output"]["sha256"] == sha256_bytes(source)

    # task 原文同样完整到达(不是只查了一个产物)
    task_bytes = (run.dir / "artifacts" / "task.txt").read_bytes()
    assert task_bytes in projection

    # node_b 正常完成、未降级
    done = run.events.find(type="node_done", node="node_b")
    assert done is not None
    assert done["model_used"] == "Fake:other"
    assert done["degraded"] is False

    # 事件流完整:run_started → node_input → node_started → node_done → run_done
    types = [e["type"] for e in run.events.all()]
    assert types[0] == "run_started" and types[-1] == "run_done"
    assert "node_input" in types and "node_done" in types
