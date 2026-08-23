# -*- coding: utf-8 -*-
"""真实 API 两节点图(M0 清单:两个节点绑不同厂商,真实调用)。

标 real_api,默认跳过;里程碑收尾时手动跑一次:
    uv run pytest tests/test_real_two_node.py -m real_api -s
账本(事件流+产物)自动落进 runs/,符合「每个里程碑手工跑一次,账本存档」。
"""
import pytest

from atlas.config import PROJECT_ROOT
from atlas.engine import execute_graph
from atlas.m0_graph import TASK, m0_registry, m0_spec, self_check


@pytest.mark.real_api
def test_real_two_node_distinct_vendors():
    run = execute_graph(m0_spec(), task=TASK,
                        runs_root=PROJECT_ROOT / "runs",
                        registry=m0_registry())
    summary = self_check(run)  # A1 语义在真实数据上的自检

    print(f"\nrun dir: {summary['run_dir']}")
    print(f"writer 产物: {summary['writer_chars']} 字符,投影 {summary['projection_chars']} 字符")
    for n in summary["nodes"]:
        print(f"  {n['node']}: {n['model_used']} degraded={n['degraded']} "
              f"in={n['input_tokens']} out={n['output_tokens']} {n['duration_s']}s")

    assert summary["models_distinct"], "两个节点必须真的用了不同厂商的模型"
    assert all(not n["degraded"] for n in summary["nodes"]), "这次运行不应发生降级"
    assert summary["writer_chars"] > 1000, "真实产物不该是空壳"
