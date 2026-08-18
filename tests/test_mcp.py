# -*- coding: utf-8 -*-
"""MCP 五工具:全部走内部实现(与工具封装同一份代码),假供应商,零花费。"""
import shutil
from pathlib import Path

import pytest

from atlas import mcp as m
from atlas.adapters import FakeProvider

from conftest import TASK_TEXT, good_review_text, good_writer_text, make_registry

GOOD_YAML = """
name: mcp_demo
description: mcp 测试图
nodes:
  - id: a
    type: llm
    model: Fake:primary
    prompt: 分析任务。
    consumes: [task]
  - id: b
    type: llm
    model: Fake:other
    prompt: 审查上游。
    consumes: [task, a.output]
edges:
  - from: a
    to: b
  - from: b
    to: END
""".lstrip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    workflows = tmp_path / "workflows"
    runs = tmp_path / "runs"
    workflows.mkdir()
    (workflows / "demo.yaml").write_text(GOOD_YAML, encoding="utf-8")
    monkeypatch.setattr(m, "WORKFLOWS_DIR", workflows)
    monkeypatch.setattr(m, "RUNS_DIR", runs)

    fake = FakeProvider()
    fake.configure("primary", text=good_writer_text(30))
    fake.configure("other", text=good_review_text())
    factory = lambda pids: make_registry(fake)
    return {"workflows": workflows, "runs": runs, "factory": factory}


def test_validate_rejects_and_points_to_fix(env):
    bad = m.validate_workflow_impl(yaml_text="name: x\nnodes:\n  - id: a\n    type: magic\n    model: Fake:x\n    prompt: p\n    consumes: [task]\n")
    assert bad["valid"] is False
    assert "封闭清单" in bad["error"]
    assert "next" in bad

    good = m.validate_workflow_impl(yaml_text=GOOD_YAML)
    assert good["valid"] is True
    assert good["entry"] == "a"
    assert good["heterogeneity"]["providers_used"] == ["Fake"]
    assert good["next"]


def test_validate_flags_same_vendor_pair(env):
    result = m.validate_workflow_impl(yaml_text=GOOD_YAML)
    assert result["heterogeneity"]["same_vendor_node_pairs"] == [["a", "b"]]


def test_dry_run_renders_without_spending(env):
    out = m.run_workflow_impl("demo", "长任务" * 100, dry_run=True,
                              registry_factory=env["factory"])
    assert out["dry_run"] is True
    assert out["nodes"][0]["prompt"] == "分析任务。"
    assert out["nodes"][0]["chain"] == ["Fake:primary"]
    assert out["nodes"][1]["upstream_outputs_inline"] == ["a.output"]
    assert out["nodes"][0]["est_prompt_tokens"] > 0
    # dry_run 不产生任何运行目录
    assert not list(env["runs"].glob("*"))


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("case", ["empty", "blank", "oversized"])
def test_task_validation_matches_web_and_never_allocates_run(env, dry_run, case):
    task = {"empty": "", "blank": "   "}.get(case, "甲" * (1024 * 1024))
    out = m.run_workflow_impl(
        "demo", task, dry_run=dry_run, registry_factory=env["factory"])
    assert "task" in out["error"]
    assert "非空" in out["error"] or "超过上限" in out["error"]
    assert not env["runs"].exists() or not list(env["runs"].iterdir())


def test_run_blocks_until_done(env):
    out = m.run_workflow_impl("demo", TASK_TEXT, registry_factory=env["factory"])
    assert out["status"] == "done"
    assert out["node_details"]["a"]["model_used"] == "Fake:primary"
    assert out["totals"]["input_tokens"] > 0
    assert Path(out["run_dir"]).exists()
    # get_run 与 run 输出一致
    again = m.summarize_run(out["run_id"])
    assert again["status"] == "done"
    assert again["node_details"]["b"]["degraded"] is False


def test_run_failure_visible(env):
    fake = FakeProvider()
    fake.configure("primary", text="")  # 假成功:空返回,无备用 → 失败
    fake.configure("other", text=good_review_text())
    out = m.run_workflow_impl(
        "demo", TASK_TEXT, registry_factory=lambda p: make_registry(fake))
    assert out["status"] == "failed"
    assert "返回内容为空" in out["error"]


def test_tool_wrappers_registered():
    # 5 个工具都注册上了(MCPServer 2.x;M6 增加 save)
    import asyncio
    from mcp.server.mcpserver.server import MCPServer

    async def tools():
        return await m.server.list_tools()
    names = {t.name for t in asyncio.run(tools())}
    assert names == {"atlas_validate_workflow", "atlas_save_workflow",
                     "atlas_run_workflow", "atlas_list_workflows",
                     "atlas_get_run"}, names
