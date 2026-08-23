# -*- coding: utf-8 -*-
"""P2 · 协作式取消。

合同:cancel request 是 run 目录内原子 create-if-absent 文件;running 由
controller 在消费点(节点入口/候选切换/重试等待)终止并唯一写 run_cancelled;
paused/interrupted 无 controller 时由取消入口持锁直写终态;done/failed/
cancelled 冲突;幂等;取消后 resume/approve 均拒绝,cancelled 可删除。
"""
import json
import threading
import time

import pytest

from atlas import launcher
from atlas.adapters import (FakeProvider, RunCancelled, TransportError,
                            call_with_fallback)
from atlas.engine import (CANCEL_REQUEST_FILENAME, RunConflictError,
                          RunNotFoundError, execute_graph, request_cancel,
                          resume_graph)
from atlas.events import EventLog, EventReader, fold_events

from conftest import (TASK_TEXT, good_review_text, good_writer_text,
                      load_graph, make_registry)

HUMAN_FAKE = {"primary": good_writer_text(200), "other": good_review_text()}


def _human_fake() -> FakeProvider:
    fake = FakeProvider()
    fake.configure("primary", text=good_writer_text(200))
    fake.configure("other", text=good_review_text())
    return fake


def _wait_terminal(runs_root, run_id, timeout=10.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        path = runs_root / run_id / "events.jsonl"
        if path.exists():
            status = fold_events(EventReader(path).all())["status"]
            if status != "running":
                return status
        time.sleep(0.05)
    return "timeout"


# ─────────────────── fold 与请求文件 ───────────────────


def test_fold_maps_run_cancelled_to_terminal_status():
    records = [
        {"type": "run_started", "run_id": "r"},
        {"type": "run_cancelled", "run_id": "r", "reason": "x"},
    ]
    assert fold_events(records)["status"] == "cancelled"


def test_write_cancel_request_is_create_if_absent(tmp_path):
    from atlas.engine import write_cancel_request
    first = write_cancel_request(tmp_path, reason="第一次")
    assert first["already_requested"] is False
    second = write_cancel_request(tmp_path, reason="第二次")
    assert second["already_requested"] is True
    assert second["request_id"] == first["request_id"]   # 首个请求胜出
    on_disk = json.loads((tmp_path / CANCEL_REQUEST_FILENAME)
                         .read_text(encoding="utf-8"))
    assert on_disk["reason"] == "第一次"
    assert on_disk["request_id"] == first["request_id"]


# ─────────────────── running:controller 消费 ───────────────────


class _GatedFake(FakeProvider):
    """指定模型的调用阻塞到放行——确定性制造"在途调用"窗口。"""

    def __init__(self) -> None:
        super().__init__()
        self.gates: dict[str, threading.Event] = {}

    def gate(self, model_id: str) -> threading.Event:
        event = threading.Event()
        self.gates[model_id] = event
        return event

    def call(self, model_id: str, *args, **kwargs):
        gate = self.gates.get(model_id)
        if gate is not None:
            assert gate.wait(timeout=15), f"{model_id} 的门没有放行"
        return super().call(model_id, *args, **kwargs)


def test_cancel_mid_run_stops_at_node_boundary(tmp_path):
    """在途调用允许完成(node_b 放行后照常落账),下一节点入口消费取消。"""
    fake = _GatedFake()
    fake.configure("primary", text=good_writer_text(200))
    fake.configure("other", text=good_review_text())
    fake.configure("third", text="终审不该发生")
    b_gate = fake.gate("other")

    run_id = launcher.start_background_run(
        load_graph("three_node"), task=TASK_TEXT, runs_root=tmp_path,
        registry=make_registry(fake))

    deadline = time.time() + 15
    while time.time() < deadline:
        path = tmp_path / run_id / "events.jsonl"
        if path.exists() and EventReader(path).find(
                type="node_done", node="node_a"):
            break
        time.sleep(0.05)
    result = request_cancel(run_id, runs_root=tmp_path, reason="测试取消",
                            active_controller=True)
    assert result["status"] == "running" and result["requested"] is True

    b_gate.set()   # 在途的 node_b 完成;取消在 node_c 入口消费
    assert _wait_terminal(tmp_path, run_id) == "cancelled"
    events = EventReader(tmp_path / run_id / "events.jsonl")
    assert events.find(type="run_cancelled") is not None
    assert events.find(type="node_done", node="node_b") is not None  # 在途已放行
    assert events.find(type="node_done", node="node_c") is None      # 未再花钱
    assert events.find(type="run_failed") is None                    # 不是失败


def test_fallback_loop_consumes_cancel_before_dispatch():
    """发起前消费:取消已置位时,一个候选都不打;未置位时 fallback 正常接力。"""
    class _Log:
        def emit(self, *a, **k):
            return {}

    def _transport_fake() -> FakeProvider:
        fake = FakeProvider()
        fake.configure("primary", transport_error="网络断了")
        fake.configure("fallback", text="接力的备用输出")
        return fake

    ok = _transport_fake()
    outcome = call_with_fallback(
        registry=make_registry(ok), log=_Log(), node_id="n", iteration=1,
        model_ref="Fake:primary", fallback_refs=["Fake:fallback"],
        prompt="p", required_fields=None, retry=0)
    assert [c["model"] for c in ok.calls] == ["primary", "fallback"]
    assert outcome.model_used == "Fake:fallback"

    cancelled = _transport_fake()
    with pytest.raises(RunCancelled):
        call_with_fallback(
            registry=make_registry(cancelled), log=_Log(), node_id="n",
            iteration=1, model_ref="Fake:primary",
            fallback_refs=["Fake:fallback"], prompt="p",
            required_fields=None, retry=0,
            cancel_requested=lambda: True)
    assert cancelled.calls == []      # 取消在首次发起前就拦下


def test_transport_retry_wait_is_cancellable():
    """同模型重试等待可唤醒:等待中落请求 → RunCancelled 而非重试。"""
    fake = FakeProvider()
    fake.configure("primary", transport_error="又断了")
    fake.configure("fallback", text="不该被用到")
    calls = []
    original = fake.call

    def counting_call(model_id, *args, **kwargs):
        calls.append(model_id)
        return original(model_id, *args, **kwargs)

    fake.call = counting_call

    class _Log:
        def emit(self, *a, **k):
            return {}

    flags = {"cancel": False}

    def cancel_after_first_wait(_seconds: float) -> None:
        flags["cancel"] = True    # 第一次等待切片就触发取消

    with pytest.raises(RunCancelled):
        call_with_fallback(
            registry=make_registry(fake), log=_Log(), node_id="n",
            iteration=1, model_ref="Fake:primary", fallback_refs=[],
            prompt="p", required_fields=None, retry=2,
            sleep_fn=cancel_after_first_wait,
            cancel_requested=lambda: flags["cancel"])
    assert len(calls) == 1        # 没有第二次调用


# ─────────────────── paused / interrupted:锁内直写终态 ───────────────────


def test_cancel_paused_run_writes_terminal_and_blocks_approval(tmp_path):
    run = execute_graph(load_graph("human_gate"), task=TASK_TEXT,
                        runs_root=tmp_path, registry=make_registry(_human_fake()))
    assert run.status == "paused"

    result = request_cancel(run.run_id, runs_root=tmp_path,
                            reason="不要了", active_controller=False)
    assert result["status"] == "cancelled"
    events = EventReader(tmp_path / run.run_id / "events.jsonl")
    cancelled = events.filter(type="run_cancelled")
    assert len(cancelled) == 1
    assert fold_events(events.all())["status"] == "cancelled"

    # 终态后:重复取消按合同返回冲突(幂等体现在不写第二个终态/请求)
    with pytest.raises(RunConflictError):
        request_cancel(run.run_id, runs_root=tmp_path, active_controller=False)
    assert len(EventReader(tmp_path / run.run_id / "events.jsonl")
               .filter(type="run_cancelled")) == 1


def test_cancel_interrupted_run_without_controller(tmp_path):
    """controller 已死(账本 running、锁空闲):取消入口在锁内直写终态。"""
    run_dir = tmp_path / "20990101-000003-ceee00"
    run_dir.mkdir(parents=True)
    log = EventLog(run_dir)
    log.emit("run_started", run_id=run_dir.name, graph="g")
    log.emit("node_done", node="a", model_used="Fake:primary",
             degraded=False, output_path="artifacts/a.output.1.txt",
             output_sha256="0" * 64, input_tokens=1, output_tokens=1)

    result = request_cancel(run_dir.name, runs_root=tmp_path,
                            active_controller=False)
    assert result["status"] == "cancelled"
    assert fold_events(
        EventReader(run_dir / "events.jsonl").all())["status"] == "cancelled"


def test_cancel_terminal_done_run_conflicts(tmp_path):
    fake = FakeProvider()
    fake.configure("primary", text=good_writer_text(200))
    fake.configure("fallback", text=good_writer_text(100))
    fake.configure("other", text=good_review_text())
    run = execute_graph(load_graph("two_node"), task=TASK_TEXT,
                        runs_root=tmp_path, registry=make_registry(fake))
    assert run.status == "done"
    with pytest.raises(RunConflictError):
        request_cancel(run.run_id, runs_root=tmp_path, active_controller=False)
    with pytest.raises(RunNotFoundError):
        request_cancel("20990101-999999-nope00", runs_root=tmp_path,
                       active_controller=False)


def test_cancelled_run_rejects_resume_and_approve(tmp_path):
    run = execute_graph(load_graph("human_gate"), task=TASK_TEXT,
                        runs_root=tmp_path, registry=make_registry(_human_fake()))
    request_cancel(run.run_id, runs_root=tmp_path, active_controller=False)

    with pytest.raises(RunConflictError):
        resume_graph(run.run_id, spec=load_graph("human_gate"),
                     runs_root=tmp_path, registry=make_registry(_human_fake()))

    from atlas.engine import lock_approval_run, prepare_execution
    from atlas.spec import spec_from_snapshot
    snapshot = json.loads((tmp_path / run.run_id / "spec.snapshot.json")
                          .read_text(encoding="utf-8"))
    spec = spec_from_snapshot(snapshot, source="snapshot")
    prepared = prepare_execution(spec, make_registry(_human_fake()))
    with pytest.raises((RunConflictError, Exception)):
        lock_approval_run(run.run_id, spec=spec, runs_root=tmp_path,
                          prepared=prepared)


# ─────────────────── MCP 与 Web 面 ───────────────────


def test_mcp_cancel_run_impl_and_delete(tmp_path, monkeypatch):
    import atlas.mcp as mcp_module

    monkeypatch.setattr(mcp_module, "RUNS_DIR", tmp_path)
    run = execute_graph(load_graph("human_gate"), task=TASK_TEXT,
                        runs_root=tmp_path, registry=make_registry(_human_fake()))

    result = mcp_module.cancel_run_impl(run.run_id, reason="走 MCP 取消")
    assert result["status"] == "cancelled"

    conflict = mcp_module.cancel_run_impl(run.run_id)
    assert "error" in conflict                     # 终态冲突如实回报
    missing = mcp_module.cancel_run_impl("20990101-999998-void00")
    assert "error" in missing


def test_web_cancel_endpoint_and_delete_cancelled(tmp_path):
    from atlas.web import create_app
    from fastapi.testclient import TestClient

    runs = tmp_path / "runs"
    run = execute_graph(load_graph("human_gate"), task=TASK_TEXT,
                        runs_root=runs, registry=make_registry(_human_fake()))
    app = create_app(runs_dir=runs, api_only=True)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(f"/api/runs/{run.run_id}/cancel",
                               json={"reason": "界面取消"},
                               headers={"X-Atlas-Request": "1"})
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"

        deleted = client.delete(f"/api/runs/{run.run_id}",
                                headers={"X-Atlas-Request": "1"})
        assert deleted.status_code == 200          # cancelled 是可删除终态
