# -*- coding: utf-8 -*-
"""A2 · 缺失即失败:消费不存在产物的图,必须大声失败,绝不给空串继续跑。

夹具是并行图:node_c 消费 node_b.output,但两者被调度进同一个并行超步——
b 还没产出,c 就要读。这是名字校验拦不住的真错(拓扑与执行顺序不一致),
只能在运行期抓,而这正是 WiringError 的职责。
"""
import pytest

from atlas.adapters import FakeProvider
from atlas.engine import execute_graph
from atlas.events import EventReader
from atlas.integrity import WiringError

from conftest import TASK_TEXT, load_graph, make_registry


def test_a2_missing_artifact_fails_loudly(tmp_path):
    fake = FakeProvider()
    fake.configure("primary", text="拆解完成:方向一与方向二。")
    fake.configure("other", text="方向一处理完毕。")
    fake.configure("right", text="不应该被消费到")

    with pytest.raises(WiringError) as e:
        execute_graph(load_graph("broken_wiring"), task=TASK_TEXT,
                      runs_root=tmp_path, registry=make_registry(fake))

    # 关键:不能是「跑完了但结果是空的」
    assert "产物库里没有它" in str(e.value)

    # 失败必须可见:事件流里记了 run_failed
    logs = list(tmp_path.glob("*/events.jsonl"))
    assert len(logs) == 1, f"应恰好有一次运行记录,实际 {len(logs)}"
    reader = EventReader(logs[0])
    failed = reader.find(type="run_failed")
    assert failed is not None
    assert failed["error_type"] == "WiringError"
    assert "产物库里没有它" in failed["error"]

    # node_a 已正常完成且产物真实存在——失败发生在 c 的输入校验,不是没跑
    assert reader.find(type="node_done", node="node_a") is not None

    # node_c 在花钱之前就被拦下:没有 node_input 事件、没有 node_done
    # (并行同伴 node_b 的完成与否取决于线程时序,不对它做断言)
    assert reader.find(type="node_input", node="node_c") is None
    assert reader.find(type="node_done", node="node_c") is None
