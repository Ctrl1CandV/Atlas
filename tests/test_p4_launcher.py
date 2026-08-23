# -*- coding: utf-8 -*-
"""P4 · 共享 launcher、MCP 异步与 atlas_list_runs。

合同:wait=false 只在完整预检、执行身份断言与运行锁准入后返回 run_id;
registry 只是"谁在跑"的登记表,状态与摘要仍由账本派生;Web 与 MCP
共用 runs.list_run_summaries。
"""
import threading
import time

import pytest

from atlas import launcher
from atlas.adapters import FakeProvider
from atlas.engine import execute_graph
from atlas.events import EventReader, fold_events

from conftest import TASK_TEXT, good_review_text, good_writer_text, load_graph, make_registry

CUSTOM_YAML = """
name: p4_demo
nodes:
  - id: only
    type: llm
    model: Fake:other
    prompt: 回应任务
    consumes: [task]
edges:
  - from: only
    to: END
"""


def _two_node_fake() -> FakeProvider:
    fake = FakeProvider()
    fake.configure("primary", text=good_writer_text(200))
    fake.configure("fallback", text=good_writer_text(100))
    fake.configure("other", text=good_review_text())
    return fake


def _wait_status(runs_root, run_id, timeout=10.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        path = runs_root / run_id / "events.jsonl"
        if path.exists():
            status = fold_events(EventReader(path).all())["status"]
            if status != "running":
                return status
        time.sleep(0.05)
    return "timeout"


# ─────────────────── registry 语义 ───────────────────


def test_registry_registers_unregisters_and_rejects_second_live_controller():
    reg = launcher.ControllerRegistry()
    stop = threading.Event()
    thread = threading.Thread(target=stop.wait, daemon=True)
    thread.start()
    assert reg.register("r1", thread) is True
    assert reg.is_active("r1") is True
    assert reg.register("r1", threading.Thread()) is False   # 拒绝双 controller
    assert reg.active_ids() == ["r1"]
    reg.unregister("r1")
    assert reg.is_active("r1") is False and reg.active_ids() == []
    stop.set()
    thread.join(timeout=5)


# ─────────────────── start_background_run ───────────────────


def test_start_background_run_completes_and_releases(tmp_path):
    fake = _two_node_fake()
    spec = load_graph("two_node")

    run_id = launcher.start_background_run(
        spec, task=TASK_TEXT, runs_root=tmp_path, registry=make_registry(fake))

    assert launcher.REGISTRY.is_active(run_id) is True
    assert _wait_status(tmp_path, run_id) == "done"
    deadline = time.time() + 5
    while launcher.REGISTRY.is_active(run_id) and time.time() < deadline:
        time.sleep(0.05)
    assert launcher.REGISTRY.is_active(run_id) is False
    # 运行锁已随执行结束释放:可再次取得(拿到即证明空闲)
    from atlas.engine import acquire_run_lock, release_run_lock
    acquire_run_lock(run_id, runs_root=tmp_path)
    release_run_lock(run_id, runs_root=tmp_path)


# ─────────────────── MCP wait=false 与 atlas_list_runs ───────────────────


def test_run_workflow_wait_false_returns_run_id_then_completes(tmp_path, monkeypatch):
    import atlas.mcp as mcp_module

    workflows = tmp_path / "workflows"
    runs = tmp_path / "runs"
    workflows.mkdir()
    (workflows / "demo.yaml").write_text(CUSTOM_YAML.strip() + "\n", encoding="utf-8")
    monkeypatch.setattr(mcp_module, "WORKFLOWS_DIR", workflows)
    monkeypatch.setattr(mcp_module, "RUNS_DIR", runs)

    fake = FakeProvider()
    fake.configure("other", text="异步完成")
    result = mcp_module.run_workflow_impl(
        "demo", TASK_TEXT, registry_factory=lambda _: make_registry(fake),
        wait=False)

    assert result.get("async") is True
    assert result.get("status") == "starting"
    run_id = result["run_id"]
    assert _wait_status(runs, run_id) == "done"
    summary = mcp_module.summarize_run(run_id)
    assert summary["status"] == "done"
    assert summary["nodes_done"] == ["only"]


def test_wait_false_rejects_persist_as(tmp_path, monkeypatch):
    import atlas.mcp as mcp_module

    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "demo.yaml").write_text(CUSTOM_YAML.strip() + "\n", encoding="utf-8")
    monkeypatch.setattr(mcp_module, "WORKFLOWS_DIR", workflows)
    monkeypatch.setattr(mcp_module, "RUNS_DIR", tmp_path / "runs")

    fake = FakeProvider()
    fake.configure("other", text="不会被启动")
    result = mcp_module.run_workflow_impl(
        "demo", TASK_TEXT, yaml=CUSTOM_YAML, persist_as="keepme",
        registry_factory=lambda _: make_registry(fake), wait=False)
    assert "互斥" in result["error"]
    assert not (tmp_path / "runs").exists() or not list((tmp_path / "runs").iterdir())


def test_wait_false_after_sha_assertion_creates_no_run(tmp_path, monkeypatch):
    """sha 不符 + wait=false:必须在启动前零成本拒绝(核心合同:wait=false
    只在全部预检与身份断言之后才返回 run_id)。"""
    import atlas.mcp as mcp_module

    workflows = tmp_path / "workflows"
    runs = tmp_path / "runs"
    workflows.mkdir()
    (workflows / "demo.yaml").write_text(CUSTOM_YAML.strip() + "\n", encoding="utf-8")
    monkeypatch.setattr(mcp_module, "WORKFLOWS_DIR", workflows)
    monkeypatch.setattr(mcp_module, "RUNS_DIR", runs)

    fake = FakeProvider()
    fake.configure("other", text="不会被启动")
    result = mcp_module.run_workflow_impl(
        "demo", TASK_TEXT, registry_factory=lambda _: make_registry(fake),
        expected_execution_sha256="0" * 64, wait=False)
    assert "零成本拒绝" in result["error"]
    assert "run_id" not in result
    assert not runs.exists() or not list(runs.iterdir())


def test_spawn_controller_start_failure_releases_lock(tmp_path, monkeypatch):
    """线程启动失败:注销登记并释放运行锁(锁可再次取得)。"""
    from atlas.engine import acquire_run_lock

    def boom(*args, **kwargs):
        raise RuntimeError("cannot start")

    run_id = "20990101-000000-deadbe"
    acquire_run_lock(run_id, runs_root=tmp_path)
    monkeypatch.setattr(launcher.threading, "Thread", boom)
    with pytest.raises(RuntimeError):
        launcher.spawn_controller(run_id, lambda: None, runs_root=tmp_path)
    assert launcher.REGISTRY.is_active(run_id) is False
    acquire_run_lock(run_id, runs_root=tmp_path)   # 证明锁已释放
    from atlas.engine import release_run_lock
    release_run_lock(run_id, runs_root=tmp_path)


def test_spawn_controller_double_register_raises_and_releases_lock(tmp_path):
    """登记被拒(双 controller):fail-loud 且不留滞留锁(审查 2026-08-23 发现 1)。"""
    from atlas.engine import acquire_run_lock, release_run_lock

    run_id = "20990101-000001-cafe00"
    stop = threading.Event()
    holder = threading.Thread(target=stop.wait, daemon=True)
    holder.start()
    assert launcher.REGISTRY.register(run_id, holder) is True

    acquire_run_lock(run_id, runs_root=tmp_path)   # start_background_run 的前置
    with pytest.raises(RuntimeError, match="拒绝双跑"):
        launcher.spawn_controller(run_id, lambda: None, runs_root=tmp_path)
    # 锁被 spawn 的失败路径释放:可再次取得
    acquire_run_lock(run_id, runs_root=tmp_path)
    release_run_lock(run_id, runs_root=tmp_path)
    stop.set()
    holder.join(timeout=5)


def test_summarize_run_reports_starting_before_ledger(tmp_path, monkeypatch):
    """wait=false 刚返回、账本未落:首查返回 starting 而非"没有这个运行"。"""
    import atlas.mcp as mcp_module

    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(mcp_module, "RUNS_DIR", runs)
    stop = threading.Event()
    holder = threading.Thread(target=stop.wait, daemon=True)
    holder.start()
    assert launcher.REGISTRY.register("20990101-000002-abba00", holder)
    try:
        summary = mcp_module.summarize_run("20990101-000002-abba00")
        assert summary["status"] == "starting"
        assert "error" not in summary
    finally:
        stop.set()
        holder.join(timeout=5)
        launcher.REGISTRY.unregister("20990101-000002-abba00")


def test_list_runs_impl_pages_desc_with_cursor(tmp_path, monkeypatch):
    import atlas.mcp as mcp_module

    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(mcp_module, "RUNS_DIR", runs)
    for _ in range(3):
        execute_graph(load_graph("two_node"), task=TASK_TEXT, runs_root=runs,
                      registry=make_registry(_two_node_fake()))

    first = mcp_module.list_runs_impl(limit=2)
    ids = [r["run_id"] for r in first["runs"]]
    assert len(ids) == 2 and ids[0] > ids[1]           # run_id 降序
    assert first["next_cursor"] == ids[1]
    second = mcp_module.list_runs_impl(limit=2, cursor=first["next_cursor"])
    rest = [r["run_id"] for r in second["runs"]]
    assert len(rest) == 1 and rest[0] < ids[1]
    assert second["next_cursor"] is None

    with pytest.raises(ValueError):
        mcp_module.list_runs_impl(limit=0)
    with pytest.raises(ValueError):
        mcp_module.list_runs_impl(limit=201)


def test_summarize_run_delegates_shared_builder(tmp_path, monkeypatch):
    import atlas.mcp as mcp_module

    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(mcp_module, "RUNS_DIR", runs)
    execute_graph(load_graph("two_node"), task=TASK_TEXT, runs_root=runs,
                  registry=make_registry(_two_node_fake()))
    run_id = next(d.name for d in runs.iterdir() if d.name not in (".locks",))

    summary = mcp_module.summarize_run(run_id)
    from atlas.runs import build_run_summary
    shared = build_run_summary(run_id, runs_root=runs)
    assert summary["status"] == shared["status"] == "done"
    assert summary["totals"] == shared["totals"]
    assert summary["effective_spec_sha256"] == shared["effective_spec_sha256"]
    assert "next" in summary          # MCP 面的提示语仍在领域函数之外
