# -*- coding: utf-8 -*-
"""测试共享设施:A1–A6 全部用假供应商,不花钱、毫秒级、可精确构造失败形态。

所有图定义走 tests/graphs/*.yaml → spec_from_yaml_file 的真实解析路径,
不绕过被测代码(postmortem 0001 的教训)。
"""
import json
from pathlib import Path

import pytest

from atlas.adapters import AdapterRegistry, FakeProvider, breaker
from atlas.spec import WorkflowSpec, spec_from_yaml_file

GRAPHS = Path(__file__).parent / "graphs"

TASK_TEXT = "测试任务:验证数据从节点 A 完整到达节点 B,任何截断都算失败。"


@pytest.fixture(autouse=True)
def _reset_breaker():
    """熔断器是进程内全局状态:每个测试都从关闭状态开始,
    否则前一个测试的传输失败会让后一个测试的候选被静默跳过。"""
    breaker.reset()
    yield
    breaker.reset()


def load_graph(name: str) -> WorkflowSpec:
    return spec_from_yaml_file(GRAPHS / f"{name}.yaml")


def good_writer_text(detail_chars: int = 60_000) -> str:
    """node_a 的合格输出:JSON、必填字段齐全、正文 ≥60k 字符(A1 的规模要求)。"""
    return json.dumps(
        {"summary": "分析完成", "verdict": "ok", "detail": "甲" * detail_chars},
        ensure_ascii=False,
    )


def good_review_text() -> str:
    return json.dumps(
        {"review": "已核对上游材料的完整性,内容齐整。", "ok": True},
        ensure_ascii=False,
    )


def make_registry(fake: FakeProvider) -> AdapterRegistry:
    registry = AdapterRegistry()
    fake.register_into(registry)
    return registry


def standard_fake(good_detail: int = 60_000) -> FakeProvider:
    """two_node.yaml 需要的三个模型全部配成合格输出。"""
    primary_text = good_writer_text(good_detail)
    fake = FakeProvider(max_output_tokens=max(8192, len(primary_text) // 3 + 1))
    fake.configure("primary", text=primary_text)
    fake.configure("fallback", text=good_writer_text(100))
    fake.configure("other", text=good_review_text())
    return fake
