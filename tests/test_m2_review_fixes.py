# -*- coding: utf-8 -*-
"""M2 独立模型审查(deepseek)发现问题的回归测试。

🔴1 agent_runner 生产接线 🟠2 原子锁 🟠3 只许批复暂停中的运行
🟠4 MCP id 白名单 (🟡11 spec 快照)
"""
import json

import pytest

from atlas import mcp as m
from atlas.adapters import FakeProvider
from atlas.engine import (
    HumanRejected,
    _NodeCtx,
    acquire_run_lock,
    approve_run,
    execute_graph,
    release_run_lock,
)
from atlas.events import EventLog, EventReader
from atlas.nodes.sandbox import sandbox_runner
from atlas.spec import SpecError, spec_from_snapshot, spec_to_snapshot

from conftest import TASK_TEXT, load_graph, make_registry


def test_agent_runner_defaults_to_sandbox(tmp_path):
    """生产默认 runner 只能是 fail-closed OS 沙箱入口。"""
    log = EventLog(tmp_path)
    ctx = _NodeCtx(run_dir=tmp_path, log=log, registry=make_registry(FakeProvider()),
                   reader=EventReader(tmp_path / "events.jsonl"))
    assert ctx._agent_runner_raw is sandbox_runner


def test_agent_node_without_injection_fails_before_run_files(tmp_path):
    """runner 未启用时在分配 run/复制 worktree之前拒绝。"""
    from atlas.config import ConfigError
    from atlas.spec import spec_from_yaml
    spec = spec_from_yaml("""
name: r
nodes:
  - id: scout
    type: research
    model: Fake:agent-model
    prompt: p
    consumes: [task]
    allow_web: false
edges:
  - from: scout
    to: END
""")
    fake = FakeProvider()
    fake.configure("agent-model", text="不会被调用")
    with pytest.raises(ConfigError, match="AGENT_RUNNER_DISABLED"):
        execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                      registry=make_registry(fake))
    assert list(tmp_path.iterdir()) == []


def test_approve_rejected_when_run_not_paused(tmp_path):
    """🟠3:对已完成的 run 批复 → 明确拒绝,账本不被污染。"""
    fake = FakeProvider()
    fake.configure("primary", text="方案")
    fake.configure("other", text="终稿")

    from atlas.spec import spec_from_yaml
    spec = spec_from_yaml("""
name: plain
nodes:
  - id: a
    type: llm
    model: Fake:primary
    prompt: p
    consumes: [task]
edges:
  - from: a
    to: END
""")
    run = execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                        registry=make_registry(fake))
    assert run.status == "done"

    with pytest.raises(SpecError, match="不在暂停状态"):
        approve_run(run.run_id, decision="approve", comment="",
                    spec=spec, runs_root=tmp_path,
                    registry=make_registry(fake))
    # 账本没有多出任何事件
    reader = EventReader(run.dir / "events.jsonl")
    assert reader.find(type="run_approval") is None
    assert reader.find(type="run_failed") is None


def test_concurrent_approve_only_one_wins(tmp_path):
    """🟠2:并发批复,锁只放行一个(原子创建,无 TOCTOU 窗口)。"""
    fake = FakeProvider()
    fake.configure("primary", text="方案")
    fake.configure("other", text="终稿")
    registry = make_registry(fake)
    run = execute_graph(load_graph("human_gate"), task=TASK_TEXT,
                        runs_root=tmp_path, registry=registry)
    assert run.status == "paused"

    # 权威状态是稳定 .locks 文件上的 OS 锁；文件存在本身不代表占用。
    from atlas.engine import acquire_run_lock, release_run_lock
    acquire_run_lock(run.run_id, runs_root=tmp_path)
    try:
        with pytest.raises(Exception, match="运行锁"):
            approve_run(run.run_id, decision="approve", comment="",
                        spec=load_graph("human_gate"), runs_root=tmp_path,
                        registry=registry)
    finally:
        release_run_lock(run.run_id, runs_root=tmp_path)


def test_mcp_ids_validated(tmp_path, monkeypatch):
    """🟠4:MCP 的 workflow_id/run_id 白名单(路径穿越拒绝)。"""
    workflows = tmp_path / "workflows"
    runs = tmp_path / "runs"
    workflows.mkdir()
    monkeypatch.setattr(m, "WORKFLOWS_DIR", workflows)
    monkeypatch.setattr(m, "RUNS_DIR", runs)

    for bad in ("../../config/pricing", "a/b", "..\\..\\x", "a..b"):
        with pytest.raises(ValueError, match="id 只允许"):
            m.validate_workflow_impl(workflow_id=bad)
        with pytest.raises(ValueError, match="id 只允许"):
            m.summarize_run(bad)
        with pytest.raises(ValueError, match="id 只允许"):
            m.run_workflow_impl(bad, "task")


def test_spec_snapshot_roundtrip(tmp_path):
    """🟡11:spec 快照可完整重建,指纹一致;web 批复不再依赖 workflows/。"""
    spec = load_graph("human_gate")
    snapshot = spec_to_snapshot(spec)
    rebuilt = spec_from_snapshot(json.loads(json.dumps(snapshot)))
    assert rebuilt.entry == spec.entry
    assert [n.id for n in rebuilt.nodes] == [n.id for n in spec.nodes]
    from atlas.spec import spec_fingerprint
    assert spec_fingerprint(rebuilt) == spec_fingerprint(spec)

    # execute_graph 落盘了快照
    fake = FakeProvider()
    fake.configure("primary", text="方案")
    fake.configure("other", text="终稿")
    run = execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                        registry=make_registry(fake))
    assert (run.dir / "spec.snapshot.json").exists()
    on_disk = spec_from_snapshot(json.loads(
        (run.dir / "spec.snapshot.json").read_text(encoding="utf-8")))
    assert spec_fingerprint(on_disk) == spec_fingerprint(spec)
