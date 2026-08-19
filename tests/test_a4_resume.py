# -*- coding: utf-8 -*-
"""A4 · 恢复不重复执行:崩溃后续跑,已完成的节点不许再执行,产物哈希不变。

崩溃模拟:node_c 的模型抛传输错误(与 kill 等价——两者都留下
「superstep 2 未完成」的 checkpoint 状态)。恢复粒度是节点边界:
中途崩溃的节点整个重跑,已完成的节点不碰。
"""
import pytest

from atlas.adapters import AllCandidatesFailed, FakeProvider
from atlas.engine import _resume_graph_replay, execute_graph
from atlas.events import EventReader

from conftest import TASK_TEXT, load_graph, make_registry


def _only_run_dir(tmp_path):
    runs = list(tmp_path.glob("*/events.jsonl"))
    assert len(runs) == 1
    return runs[0].parent


def test_a4_resume_does_not_reexecute(tmp_path):
    fake = FakeProvider()
    fake.configure("primary", text="第一步完成:任务已梳理。")
    fake.configure("other", text="第二步完成:结论已扩展。")
    fake.configure("third", transport_error="崩溃模拟:进程在 node_c 中途死亡")

    with pytest.raises(AllCandidatesFailed):
        execute_graph(load_graph("three_node"), task=TASK_TEXT,
                      runs_root=tmp_path, registry=make_registry(fake))

    run_dir = _only_run_dir(tmp_path)
    ledger = EventReader(run_dir / "events.jsonl").all()
    crashed = EventReader(run_dir / "events.jsonl")
    before = crashed.find(type="node_done", node="node_a")
    run_id = run_dir.name
    assert before is not None, "崩溃前 node_a 应已完成"

    # 模拟重启进程后修复故障:node_c 的模型恢复正常
    fake.configure("third", text="第三步完成:终审通过。")

    resumed = _resume_graph_replay(run_id, spec=load_graph("three_node"),
                           runs_root=tmp_path, registry=make_registry(fake))

    # node_a 只能出现一次 node_done
    assert len(resumed.events.filter(type="node_done", node="node_a")) == 1
    # 且它的产物哈希与崩溃前一致
    assert resumed.artifacts["node_a.output"].sha256 == before["output_sha256"]
    # node_b 也未被重跑
    assert len(resumed.events.filter(type="node_done", node="node_b")) == 1
    # node_c 重跑后成功,整图完成
    assert "node_c.output" in resumed.artifacts
    assert resumed.folded()["status"] == "done"

    # 事件流是同一本账:序列号单调且无重复
    seqs = [e["seq"] for e in resumed.events.all()]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
    # 续跑记了 run_resumed
    assert resumed.events.find(type="run_resumed") is not None
    # 崩溃前的旧事件一条没少
    assert all(e in resumed.events.all() for e in ledger)


def test_a4_resume_requires_checkpoint(tmp_path):
    from atlas.engine import RunNotFoundError

    fake = FakeProvider()
    fake.configure("primary", text="x")
    fake.configure("other", text="y")
    fake.configure("third", transport_error="崩溃")
    with pytest.raises(AllCandidatesFailed):
        execute_graph(load_graph("three_node"), task=TASK_TEXT,
                      runs_root=tmp_path, registry=make_registry(fake),
                      checkpoint=False)  # 无 checkpoint 的运行不可恢复

    run_dir = _only_run_dir(tmp_path)
    with pytest.raises(RunNotFoundError):
        _resume_graph_replay(run_dir.name, spec=load_graph("three_node"),
                     runs_root=tmp_path, registry=make_registry(fake))
