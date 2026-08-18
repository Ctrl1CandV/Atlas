# -*- coding: utf-8 -*-
"""A3 · 假成功即降级(含 A3b 截断哨兵、A3c 无 usage 警告、A3d 输出打顶)。

实测形态:7 次调用 3 次失败,三分之二不抛异常——空返回、只回 "OK"、
缺必填字段、prompt 只送达 1%。这类失败返回 200、读起来正常,
必须与传输错误走同一条降级路径。
"""
from pathlib import Path

import pytest

from atlas.adapters import AllCandidatesFailed, FakeProvider, Usage
from atlas.engine import execute_graph
from atlas.events import EventReader

from conftest import (
    TASK_TEXT,
    good_review_text,
    good_writer_text,
    load_graph,
    make_registry,
    standard_fake,
)


@pytest.mark.parametrize("bad_response", [
    "",                          # 空
    "OK",                        # 只回两个字母(真实发生过)
    '{"conclusion": "..."}',     # 缺 required 里的其他字段
    "   \n  ",                   # 只有空白
])
def test_a3_degraded_output_triggers_fallback(tmp_path, bad_response):
    fake = FakeProvider()
    fake.configure("primary", text=bad_response)
    fallback_text = good_writer_text(200)
    fake.configure("fallback", text=fallback_text)
    fake.configure("other", text=good_review_text())

    run = execute_graph(load_graph("two_node"), task=TASK_TEXT,
                        runs_root=tmp_path, registry=make_registry(fake))

    # 必须降级到备用模型,而不是接受这份退化输出
    failed = run.events.find(type="model_failed", model="Fake:primary")
    assert failed is not None, "假成功必须记 model_failed 事件"
    assert "DegradedOutput" in failed["reason"]

    done = run.events.find(type="node_done", node="node_a")
    assert done["model_used"] == "Fake:fallback"
    assert done["degraded"] is True

    # 落盘的产物是备用模型的原文,不是退化输出
    assert Path(done["output_path"]).read_bytes() == fallback_text.encode("utf-8")

    # node_b 不受影响
    done_b = run.events.find(type="node_done", node="node_b")
    assert done_b["model_used"] == "Fake:other"
    assert done_b["degraded"] is False


def test_a3_transport_error_also_falls_back(tmp_path):
    """传输错误(会抛异常的那一类)与假成功同路降级。"""
    fake = FakeProvider()
    fake.configure("primary", transport_error="exit code 1, stderr 为空(真实形态)")
    fake.configure("fallback", text=good_writer_text(200))
    fake.configure("other", text=good_review_text())

    run = execute_graph(load_graph("two_node"), task=TASK_TEXT,
                        runs_root=tmp_path, registry=make_registry(fake))

    failed = run.events.find(type="model_failed", model="Fake:primary")
    assert failed is not None and "TransportError" in failed["reason"]
    done = run.events.find(type="node_done", node="node_a")
    assert done["model_used"] == "Fake:fallback" and done["degraded"] is True


def test_a3_all_candidates_failed_raises(tmp_path):
    """主模型和备用全挂:必须大声失败,并保留每次尝试的原因。"""
    fake = FakeProvider()
    fake.configure("primary", text="")
    fake.configure("fallback", text="OK")
    fake.configure("other", text=good_review_text())  # 白名单要求注册;不会被执行到

    with pytest.raises(AllCandidatesFailed) as e:
        execute_graph(load_graph("two_node"), task=TASK_TEXT,
                      runs_root=tmp_path, registry=make_registry(fake))

    reasons = {a.model: a.reason for a in e.value.attempts}
    assert "返回内容为空" in reasons["Fake:primary"]

    logs = list(tmp_path.glob("*/events.jsonl"))
    reader = EventReader(logs[0])
    assert reader.find(type="model_failed", model="Fake:primary")
    assert reader.find(type="model_failed", model="Fake:fallback")
    failed = reader.find(type="run_failed")
    assert failed["error_type"] == "AllCandidatesFailed"
    # node_a 失败后,下游不许再跑
    assert reader.find(type="node_started", node="node_b") is None


def test_a3b_truncation_sentinel(tmp_path):
    """模拟 prompt 只送达 1%(真实发生过:11154 字符 → 108 tokens)。"""
    fake = FakeProvider()
    truncated_usage = Usage(input_tokens=108, output_tokens=10)
    fake.configure("primary", text="短回复", usage=truncated_usage)
    fake.configure("fallback", text="短回复", usage=truncated_usage)

    big_task = "数据载荷" * 4000  # 16k 字符 → 投影 ≈16k 字符 ≈ 5.4k tokens 预期
    with pytest.raises(AllCandidatesFailed) as e:
        execute_graph(load_graph("big_prompt"), task=big_task,
                      runs_root=tmp_path, registry=make_registry(fake))

    assert "疑似 prompt 未完整送达" in str(e.value)
    assert any(a.error_type == "TruncationError" for a in e.value.attempts)

    # 两个候选的失败都记了事件
    logs = list(tmp_path.glob("*/events.jsonl"))
    reader = EventReader(logs[0])
    assert len(reader.filter(type="model_failed")) == 2


def test_a3c_missing_usage_warns_not_passes(tmp_path):
    """网关不返回 usage 时,必须记警告,不能当「检查通过」。"""
    fake = FakeProvider()
    fake.configure("primary", text=good_writer_text(200), auto_usage=False)
    fake.configure("fallback", text=good_writer_text(200), auto_usage=False)
    fake.configure("other", text=good_review_text(), auto_usage=False)

    run = execute_graph(load_graph("two_node"), task=TASK_TEXT,
                        runs_root=tmp_path, registry=make_registry(fake))

    skipped = run.events.filter(type="sentinel_skipped")
    assert len(skipped) == 2, "每个节点的每次成功调用都应记 sentinel_skipped"
    assert "usage" in skipped[0]["reason"]
    # 运行本身照常完成
    assert run.events.find(type="node_done", node="node_b") is not None
    assert run.events.find(type="run_done") is not None


def test_a3d_output_cap_is_visible(tmp_path):
    """输出打满 max_tokens 上限必须记账并切换到完整的备用结果。"""
    fake = FakeProvider()  # max_output_tokens 默认 8192,与真实适配器一致
    capped_usage = Usage(input_tokens=1000, output_tokens=8192)  # 正好打满
    fake.configure("primary", text=good_writer_text(200), usage=capped_usage)
    fake.configure("fallback", text=good_writer_text(200))
    fake.configure("other", text=good_review_text())

    run = execute_graph(load_graph("two_node"), task=TASK_TEXT,
                        runs_root=tmp_path, registry=make_registry(fake))

    warn = run.events.find(type="output_truncated", model="Fake:primary")
    assert warn is not None, "输出打满上限必须记 output_truncated 事件"
    done = run.events.find(type="node_done", node="node_a")
    assert done["output_truncated"] is False
    assert done["model_used"] == "Fake:fallback"
    assert done["degraded"] is True


def test_a3_guard_violation_stops_loop(tmp_path):
    """循环超过 max_iterations:大声失败,不烧钱跑下去。"""
    from atlas.engine import GuardViolation

    fake = FakeProvider()
    fake.configure("looper", text="第 N 版方案")
    # judge 永远要求 repair → maker 第 4 次执行前被守卫拦下(max_iterations=3)
    fake.configure("judge", text='{"verdict": "repair"}')

    with pytest.raises(GuardViolation) as e:
        execute_graph(load_graph("loop"), task=TASK_TEXT,
                      runs_root=tmp_path, registry=make_registry(fake))
    assert "max_iterations" in str(e.value)

    logs = list(tmp_path.glob("*/events.jsonl"))
    reader = EventReader(logs[0])
    assert len(reader.filter(type="node_done", node="maker")) == 3
    assert reader.find(type="run_failed")["error_type"] == "GuardViolation"
