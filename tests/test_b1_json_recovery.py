# -*- coding: utf-8 -*-
"""B1 · 同一响应宽容提取:严格 JSON 解析失败后剥围栏、取最外层对象。

背景(2026-08-22 实测):glm 系列偶发把合法 JSON 包在 ```json 围栏或
说明文字里返回,严格解析直接判 DegradedOutput 换候选。宽容提取让同一
响应免于一次降级;提取后仍缺必填字段必须照常失败(反例锁定)。
"""
import json

import pytest

from atlas.adapters import (
    AllCandidatesFailed,
    FakeProvider,
    recover_json_object,
)
from atlas.engine import execute_graph
from atlas.events import fold_events

from conftest import (
    TASK_TEXT,
    good_review_text,
    good_writer_text,
    load_graph,
    make_registry,
)

VALID = json.dumps({"summary": "结论", "verdict": "ok"}, ensure_ascii=False)
FENCED = f"```json\n{VALID}\n```"
WITH_PROSE = f"好的,以下是分析结果:\n{VALID}\n希望有帮助。"


# ─────────────────── 提取函数单元行为 ───────────────────


@pytest.mark.parametrize("text,note_part", [
    (FENCED, "剥除代码围栏"),
    ("```\n" + VALID + "\n```", "剥除代码围栏"),
    (WITH_PROSE, "截取最外层 JSON 对象"),
    ("  \n" + VALID + "  \n", "剥除首尾空白"),
])
def test_recover_success_shapes(text, note_part):
    parsed, how = recover_json_object(text)
    assert parsed == {"summary": "结论", "verdict": "ok"}
    assert note_part in how


def test_recover_respects_braces_inside_strings():
    """字符串里的花括号(含转义引号)不算结构边界。"""
    inner = json.dumps(
        {"summary": "含 } 与 { 与 \" 引号", "verdict": "x{y}"},
        ensure_ascii=False)
    parsed, _ = recover_json_object(f"说明:{inner} 结束")
    assert parsed["verdict"] == "x{y}"


@pytest.mark.parametrize("bad", [
    "OK",                      # 根本不是 JSON(真实形态)
    "[1, 2, 3] 好的",          # 数组不是对象
    '{"summary": "x"',         # 花括号没配平
    "```json\n{\"a\": }\n```", # 围栏内也不是合法 JSON
])
def test_recover_returns_none_when_unrecoverable(bad):
    assert recover_json_object(bad) is None


# ─────────────────── 集成:fallback 循环内的行为 ───────────────────


def test_b1_fenced_json_from_primary_needs_no_fallback(tmp_path):
    """围栏包裹的合格 JSON:同一响应被救回,不换候选,产物仍存原始字节。"""
    fake = FakeProvider()
    fake.configure("primary", text=FENCED)
    fake.configure("fallback", text=good_writer_text(200))
    fake.configure("other", text=good_review_text())

    run = execute_graph(load_graph("two_node"), task=TASK_TEXT,
                        runs_root=tmp_path, registry=make_registry(fake))

    assert run.events.find(type="model_failed") is None
    recovered = run.events.find(type="output_json_recovered", model="Fake:primary")
    assert recovered is not None and recovered["node"] == "node_a"
    assert "剥除代码围栏" in recovered["how"]
    done = run.events.find(type="node_done", node="node_a")
    assert done["model_used"] == "Fake:primary"
    assert done["degraded"] is False
    # 落盘的是原始围栏文本,不是提取结果
    with open(done["output_path"], "rb") as f:
        assert f.read() == FENCED.encode("utf-8")
    assert run.events.find(type="run_done") is not None


def test_b1_recovery_but_missing_fields_still_degrades(tmp_path):
    """反例:围栏剥掉了,必填字段还是缺——照常 DegradedOutput 换候选。"""
    fenced_missing = (
        "```json\n"
        + json.dumps({"summary": "只有一半"}, ensure_ascii=False)
        + "\n```")
    fake = FakeProvider()
    fake.configure("primary", text=fenced_missing)
    fake.configure("fallback", text=good_writer_text(200))
    fake.configure("other", text=good_review_text())

    run = execute_graph(load_graph("two_node"), task=TASK_TEXT,
                        runs_root=tmp_path, registry=make_registry(fake))

    failed = run.events.find(type="model_failed", model="Fake:primary")
    assert failed is not None
    assert "宽容提取后仍缺少必填字段" in failed["reason"]
    assert run.events.find(type="output_json_recovered") is None
    done = run.events.find(type="node_done", node="node_a")
    assert done["model_used"] == "Fake:fallback" and done["degraded"] is True


def test_b1_unrecoverable_text_fails_loudly(tmp_path):
    fake = FakeProvider()
    fake.configure("primary", text="OK")
    fake.configure("fallback", text="也坏")
    fake.configure("other", text=good_review_text())  # 白名单要求注册;不会被执行到

    with pytest.raises(AllCandidatesFailed):
        execute_graph(load_graph("two_node"), task=TASK_TEXT,
                      runs_root=tmp_path, registry=make_registry(fake))


def test_b1_fold_terminal_state_ignores_recovery_event(tmp_path):
    """fold_events 不因新事件类型改变终态(A6 纪律)。"""
    fake = FakeProvider()
    fake.configure("primary", text=FENCED)
    fake.configure("fallback", text=good_writer_text(200))
    fake.configure("other", text=good_review_text())

    run = execute_graph(load_graph("two_node"), task=TASK_TEXT,
                        runs_root=tmp_path, registry=make_registry(fake))
    records = run.events.all()
    assert any(r["type"] == "output_json_recovered" for r in records)
    without = [r for r in records if r["type"] != "output_json_recovered"]
    assert fold_events(records) == fold_events(without)


# ─────────────────── 审查补缺(2026-08-23 独立审查发现) ───────────────────


def test_b1_routing_reads_through_recovery(tmp_path):
    """条件路由图的判路节点吃围栏 JSON:验收接受,判路也必须读得出。

    改动前该形态靠 fallback 换干净 JSON 完成;若判路只做严格解析,
    宽容提取反而把可完成的图变成 NoRouteError 硬失败(审查发现的回归)。
    """
    fake = FakeProvider()
    fake.configure("looper", text="第 1 版方案")
    fake.configure("judge", text=f"```json\n{{\"verdict\": \"done\"}}\n```")

    run = execute_graph(load_graph("loop"), task=TASK_TEXT,
                        runs_root=tmp_path, registry=make_registry(fake))

    assert run.events.find(type="model_failed") is None
    recovered = run.events.find(type="output_json_recovered", model="Fake:judge")
    assert recovered is not None and recovered["node"] == "judge"
    assert run.events.find(type="run_done") is not None
    # judge 只执行一次,回边(repair)从未触发
    assert len(run.events.filter(type="node_done", node="judge")) == 1


def test_b1_capped_response_never_records_recovery(tmp_path):
    """打顶响应里就算有完整配平对象也不算恢复:被拒的响应不留"已恢复"事件。"""
    from atlas.adapters import Usage

    fake = FakeProvider()  # max_output_tokens 默认 8192
    capped = Usage(input_tokens=1000, output_tokens=8192)
    fake.configure("primary", text=FENCED, usage=capped)
    fake.configure("fallback", text=good_writer_text(200))
    fake.configure("other", text=good_review_text())

    run = execute_graph(load_graph("two_node"), task=TASK_TEXT,
                        runs_root=tmp_path, registry=make_registry(fake))

    assert run.events.find(type="output_json_recovered") is None
    assert run.events.find(type="output_truncated", model="Fake:primary") is not None
    done = run.events.find(type="node_done", node="node_a")
    assert done["model_used"] == "Fake:fallback" and done["degraded"] is True


def test_recover_takes_first_balanced_object():
    """散文里出现两个对象时取第一个(锁定语义,不猜"最像的")。"""
    first = json.dumps({"summary": "第一份", "verdict": "ok"}, ensure_ascii=False)
    second = json.dumps({"summary": "第二份"}, ensure_ascii=False)
    parsed, _ = recover_json_object(f"两份结果:{first} 以及 {second}")
    assert parsed == {"summary": "第一份", "verdict": "ok"}


def test_recover_nested_multilevel_object():
    leaf = {"leaf": [1, 2, {"x": 3}]}
    nested = json.dumps(
        {"summary": "外", "verdict": "ok", "detail": {"mid": leaf}},
        ensure_ascii=False)
    parsed, _ = recover_json_object(f"结果如下:\n{nested}\n完毕")
    assert parsed["detail"]["mid"]["leaf"][2]["x"] == 3
