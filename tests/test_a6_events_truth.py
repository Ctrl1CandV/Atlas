# -*- coding: utf-8 -*-
"""A6 · 事件流是唯一真相:从事件重放出的状态 == 运行时最终状态;
派生缓存删掉重算,结果不变。"""
import shutil

from atlas.adapters import FakeProvider
from atlas.engine import execute_graph
from atlas.events import fold_events

from conftest import TASK_TEXT, load_graph, make_registry


def test_a6_state_is_fold_of_events(tmp_path):
    fake = FakeProvider()
    fake.configure("primary", text="第一步完成。")
    fake.configure("other", text="第二步完成。")
    fake.configure("third", text="第三步完成。")
    run = execute_graph(load_graph("three_node"), task=TASK_TEXT,
                        runs_root=tmp_path, registry=make_registry(fake))

    # 从事件流重放出的状态,必须等于运行时的最终状态
    replayed = fold_events(run.events.all())
    assert replayed["artifacts"] == run.final_state["artifacts"]
    assert replayed["nodes_done"] == ["node_a", "node_b", "node_c"]
    assert replayed["status"] == "done"
    assert replayed["task_sha256"] == run.events.find(type="run_started")["task_sha256"]

    # fold 是纯函数:同一份事件流重放两次,结果一致
    assert fold_events(run.events.all()) == replayed
    # RunResult.folded() 与手工 fold 一致
    assert run.folded() == replayed

    # 删掉派生缓存后重新计算,结果不变
    # (projections/、*.sha256 旁车、checkpoint 都是派生物;events.jsonl 不是)
    shutil.rmtree(run.dir / "projections")
    for sidecar in (run.dir / "artifacts").glob("*.sha256"):
        sidecar.unlink()
    (run.dir / "checkpoint.sqlite").unlink(missing_ok=True)
    assert fold_events(run.events.all()) == replayed
