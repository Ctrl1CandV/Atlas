# -*- coding: utf-8 -*-
"""Web API:列表/详情/运行/SSE/产物服务/路径穿越防护,全部用假供应商。"""
import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from atlas import __version__
from atlas.adapters import FakeProvider
from atlas.engine import (acquire_run_lock, execute_graph, release_approval_run_lock,
                          release_run_lock, run_lock_path)
from atlas.web import create_app
import atlas.engine as engine_module
import atlas.web as web_module

from conftest import (GRAPHS, TASK_TEXT, good_review_text, good_writer_text,
                      load_graph, make_registry)


@pytest.fixture
def app(tmp_path):
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "demo.yaml").write_text(
        """
name: demo
description: web 测试用两节点图
nodes:
  - id: node_a
    type: llm
    model: Fake:primary
    fallback: [Fake:fallback]
    prompt: 分析任务。
    consumes: [task]
    output_schema:
      required: [summary]
  - id: node_b
    type: llm
    model: Fake:other
    prompt: 审查上游。
    consumes: [task, node_a.output]
edges:
  - from: node_a
    to: node_b
  - from: node_b
    to: END
""".lstrip(), encoding="utf-8")
    (workflows / "broken.yaml").write_text(
        "name: broken\nnodes:\n  - id: x\n    type: sorcery\n    model: Fake:x\n"
        "    prompt: p\n    consumes: [task]\n", encoding="utf-8")

    fake = FakeProvider()
    fake.configure("primary", text=good_writer_text(50))
    fake.configure("fallback", text=good_writer_text(10))
    fake.configure("other", text=good_review_text())

    return create_app(workflows_dir=workflows, runs_dir=tmp_path / "runs",
                      registry_factory=lambda pids: _reg(fake), api_only=True)


def _reg(fake):
    from conftest import make_registry
    return make_registry(fake)


def test_list_workflows_marks_invalid(app):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        items = client.get("/api/workflows").json()
        by_id = {i["id"]: i for i in items}
        assert by_id["demo"]["valid"] is True
        assert by_id["broken"]["valid"] is False
        assert "封闭清单" in by_id["broken"]["error"]


def test_get_workflow_shape(app):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        spec = client.get("/api/workflows/demo").json()
        assert spec["entry"] == "node_a"
        assert len(spec["nodes"]) == 2
        assert spec["nodes"][0]["fallback"] == ["Fake:fallback"]
        assert spec["edges"][0] == {"from": "node_a", "to": "node_b", "when": None}
        assert client.get("/api/workflows/nope").status_code == 404


def test_run_flow_and_summary(app):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        resp = client.post("/api/workflows/demo/run",
                           json={"task": "Web 层测试任务"},
                           headers={"X-Atlas-Request": "1"})
        assert resp.status_code == 200
        run_id = resp.json()["run_id"]

        # 轮询到完成(假供应商毫秒级)
        for _ in range(50):
            summary = client.get(f"/api/runs/{run_id}").json()
            if summary["status"] in ("done", "failed"):
                break
            time.sleep(0.1)
        assert summary["status"] == "done", summary.get("failed_error")

        nodes = {n["id"]: n for n in summary["nodes"]}
        assert nodes["node_a"]["status"] == "done"
        assert nodes["node_a"]["model_used"] == "Fake:primary"
        assert summary["totals"]["input_tokens"] > 0
        assert summary["artifacts"]["node_b.output"]["sha256"]

        # 产物原文可下载
        fname = nodes["node_a"]["output_path"].split("\\")[-1].split("/")[-1]
        text = client.get(f"/api/runs/{run_id}/artifacts/{fname}")
        assert text.status_code == 200
        assert "summary" in text.text

        # 投影原文可下载(界面「完整输入」的数据来源)
        proj = nodes["node_b"]["projection_path"].split("\\")[-1].split("/")[-1]
        resp = client.get(f"/api/runs/{run_id}/projections/{proj}")
        assert resp.status_code == 200
        # 投影里必须包含 node_a 产物原文(A1 语义在 Web 层同样成立)
        source = client.get(
            f"/api/runs/{run_id}/artifacts/{fname}").text
        assert source in resp.text

        # runs 列表
        runs = client.get("/api/runs").json()
        assert any(r["run_id"] == run_id and r["status"] == "done" for r in runs)


def _write_run(runs, rid, terminal_type=None):
    run_dir = runs / rid
    run_dir.mkdir(parents=True)
    records = [
        {"seq": 1, "ts": "t", "type": "run_started", "run_id": rid,
         "graph": "demo"},
    ]
    if terminal_type == "paused":
        records.append({"seq": 2, "ts": "t", "type": "run_paused",
                        "run_id": rid, "node": "gate"})
    elif terminal_type:
        records.append({"seq": 2, "ts": "t", "type": terminal_type,
                        "run_id": rid})
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8")
    (run_dir / "artifacts").mkdir()
    (run_dir / "artifacts" / "result.txt").write_text("result", encoding="utf-8")
    (run_dir / "projections").mkdir()
    (run_dir / "checkpoint.sqlite").write_bytes(b"checkpoint")
    (run_dir / "worktrees").mkdir()
    return run_dir


def _delete_headers():
    return {"X-Atlas-Request": "1"}


def test_delete_done_and_failed_runs_removes_entire_directory(tmp_path):
    runs = tmp_path / "runs"
    done_dir = _write_run(runs, "done-run", "run_done")
    failed_dir = _write_run(runs, "failed-run", "run_failed")
    api = create_app(workflows_dir=tmp_path / "workflows", runs_dir=runs,
                     registry_factory=lambda _: make_registry(FakeProvider()),
                     api_only=True)

    with TestClient(api, base_url="http://127.0.0.1") as client:
        for rid, run_dir in (("done-run", done_dir), ("failed-run", failed_dir)):
            response = client.delete(f"/api/runs/{rid}", headers=_delete_headers())
            assert response.status_code == 200
            assert response.json() == {"deleted": rid}
            assert not run_dir.exists()
            assert client.get(f"/api/runs/{rid}").status_code == 404
            assert client.get(f"/api/runs/{rid}/events").status_code == 404
        assert client.get("/api/runs").json() == []


def test_delete_rejects_paused_running_and_active_lock(tmp_path):
    runs = tmp_path / "runs"
    paused = _write_run(runs, "paused-run", "paused")
    running = _write_run(runs, "running-run")
    locked = _write_run(runs, "locked-run", "run_done")
    api = create_app(workflows_dir=tmp_path / "workflows", runs_dir=runs,
                     registry_factory=lambda _: make_registry(FakeProvider()),
                     api_only=True)
    acquire_run_lock("locked-run", runs_root=runs)
    try:
        with TestClient(api, base_url="http://127.0.0.1") as client:
            paused_response = client.delete("/api/runs/paused-run", headers=_delete_headers())
            running_response = client.delete("/api/runs/running-run", headers=_delete_headers())
            locked_response = client.delete("/api/runs/locked-run", headers=_delete_headers())
    finally:
        release_run_lock("locked-run", runs_root=runs)

    assert paused_response.status_code == 409
    assert "paused" in paused_response.json()["detail"]
    assert running_response.status_code == 409
    assert "running" in running_response.json()["detail"]
    assert locked_response.status_code == 423
    assert ".locks" in locked_response.json()["detail"]
    assert paused.exists() and running.exists() and locked.exists()


def test_old_lock_mtime_never_controls_acquisition(tmp_path):
    runs = tmp_path / "runs"
    run_dir = _write_run(runs, "old-lock-run", "run_done")
    acquire_run_lock("old-lock-run", runs_root=runs)
    lock = run_lock_path("old-lock-run", runs_root=runs)
    old = time.time() - 7200
    os.utime(lock, (old, old))
    api = create_app(workflows_dir=tmp_path / "workflows", runs_dir=runs,
                     registry_factory=lambda _: make_registry(FakeProvider()),
                     api_only=True)
    try:
        with TestClient(api, base_url="http://127.0.0.1") as client:
            response = client.delete("/api/runs/old-lock-run", headers=_delete_headers())
        assert response.status_code == 423
        assert run_dir.exists()
    finally:
        release_run_lock("old-lock-run", runs_root=runs)

    with TestClient(api, base_url="http://127.0.0.1") as client:
        assert client.delete("/api/runs/old-lock-run", headers=_delete_headers()).status_code == 200
        assert client.delete("/api/runs/old-lock-run", headers=_delete_headers()).status_code == 404
    assert lock.is_file()


def test_delete_rename_failures_are_controlled_and_leave_run(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    run_dir = _write_run(runs, "rename-run", "run_done")
    api = create_app(workflows_dir=tmp_path / "workflows", runs_dir=runs,
                     registry_factory=lambda _: make_registry(FakeProvider()),
                     api_only=True)
    original_replace = type(run_dir).replace

    def sharing_replace(path, target):
        if path == run_dir:
            exc = PermissionError("sharing violation")
            exc.winerror = 32
            raise exc
        return original_replace(path, target)

    monkeypatch.setattr(type(run_dir), "replace", sharing_replace)
    with TestClient(api, base_url="http://127.0.0.1") as client:
        sharing = client.delete("/api/runs/rename-run", headers=_delete_headers())
    assert sharing.status_code == 423
    assert run_dir.exists()

    def failed_replace(path, target):
        if path == run_dir:
            raise OSError("rename failed")
        return original_replace(path, target)

    monkeypatch.setattr(type(run_dir), "replace", failed_replace)
    with TestClient(api, base_url="http://127.0.0.1") as client:
        failed = client.delete("/api/runs/rename-run", headers=_delete_headers())
    assert failed.status_code == 500
    assert run_dir.exists()
    assert not (runs / ".trash" / "rename-run").exists()


def test_delete_tombstone_does_not_follow_external_directory_link(tmp_path):
    runs = tmp_path / "runs"
    run_dir = _write_run(runs, "linked-run", "run_done")
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("must survive", encoding="utf-8")
    link = run_dir / "worktrees" / "external"
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(external)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if completed.returncode != 0:
            pytest.skip("cannot create Windows junction")
        assert link.is_junction()
    else:
        link.symlink_to(external, target_is_directory=True)
        assert link.is_symlink()

    api = create_app(workflows_dir=tmp_path / "workflows", runs_dir=runs,
                     registry_factory=lambda _: make_registry(FakeProvider()),
                     api_only=True)
    with TestClient(api, base_url="http://127.0.0.1") as client:
        response = client.delete("/api/runs/linked-run", headers=_delete_headers())

    assert response.status_code == 200
    assert not run_dir.exists()
    assert sentinel.read_text(encoding="utf-8") == "must survive"


def test_delete_cleanup_failure_leaves_retryable_hidden_tombstone(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    run_dir = _write_run(runs, "cleanup-run", "run_done")
    api = create_app(workflows_dir=tmp_path / "workflows", runs_dir=runs,
                     registry_factory=lambda _: make_registry(FakeProvider()),
                     api_only=True)
    real_rmtree = web_module.shutil.rmtree
    calls = 0

    def fail_once(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("cleanup failed")
        real_rmtree(path)

    monkeypatch.setattr(web_module.shutil, "rmtree", fail_once)
    with TestClient(api, base_url="http://127.0.0.1") as client:
        first = client.delete("/api/runs/cleanup-run", headers=_delete_headers())
        assert first.status_code == 500
        assert not run_dir.exists()
        assert (runs / ".trash" / "cleanup-run").exists()
        assert client.get("/api/runs").json() == []
        assert client.get("/api/runs/cleanup-run").status_code == 404
        second = client.delete("/api/runs/cleanup-run", headers=_delete_headers())
    assert second.status_code == 200
    assert not (runs / ".trash" / "cleanup-run").exists()


def test_concurrent_duplicate_delete_has_one_success_and_one_404(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    run_dir = _write_run(runs, "race-run", "run_done")
    api = create_app(workflows_dir=tmp_path / "workflows", runs_dir=runs,
                     registry_factory=lambda _: make_registry(FakeProvider()),
                     api_only=True)
    entered = threading.Event()
    release = threading.Event()
    real_rmtree = web_module.shutil.rmtree

    def blocking_rmtree(path):
        entered.set()
        assert release.wait(timeout=5)
        real_rmtree(path)

    monkeypatch.setattr(web_module.shutil, "rmtree", blocking_rmtree)

    def request_delete():
        with TestClient(api, base_url="http://127.0.0.1") as client:
            return client.delete("/api/runs/race-run", headers=_delete_headers()).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(request_delete)
        assert entered.wait(timeout=2)
        second = pool.submit(request_delete)
        release.set()
        statuses = sorted((first.result(timeout=5), second.result(timeout=5)))

    assert statuses == [200, 404]
    assert not run_dir.exists()


def test_sse_replays_events(app):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        run_id = client.post("/api/workflows/demo/run",
                             json={"task": "SSE 测试"},
                             headers={"X-Atlas-Request": "1"}).json()["run_id"]
        for _ in range(50):
            summary = client.get(f"/api/runs/{run_id}").json()
            if summary["status"] in ("done", "failed"):
                break
            time.sleep(0.1)

        with client.stream("GET", f"/api/runs/{run_id}/events") as resp:
            assert resp.status_code == 200
            types = []
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    types.append(event["type"])
            assert types[0] == "run_started"
            assert types[-1] == "run_done"
            assert "node_input" in types and "node_done" in types


def test_empty_task_rejected(app):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post("/api/workflows/demo/run",
                           json={"task": "  "},
                           headers={"X-Atlas-Request": "1"}).status_code == 400


def test_invalid_approval_rejected_synchronously(app):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/runs/does-not-exist/approve",
            json={"decision": "maybe", "comment": ""},
            headers={"X-Atlas-Request": "1"},
        )
        assert response.status_code == 400
        assert "approve/reject" in response.json()["detail"]


def test_reject_without_comment_is_400(tmp_path, monkeypatch):
    """驳回不留理由 = 无法追溯为什么否决;必须在触碰运行锁前同步拒绝。"""
    runs = tmp_path / "runs"
    fake = FakeProvider()
    fake.configure("primary", text="方案")
    fake.configure("other", text="终稿")
    registry = make_registry(fake)
    run = execute_graph(load_graph("human_gate"), task=TASK_TEXT,
                        runs_root=runs, registry=registry)
    assert run.status == "paused"

    called = threading.Event()

    def fake_approve(run_id, **kwargs):
        called.set()
        release_approval_run_lock(run_id, runs_root=kwargs["runs_root"])

    monkeypatch.setattr(web_module, "approve_run", fake_approve)
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "human_gate.yaml").write_text(
        (GRAPHS / "human_gate.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    app = create_app(workflows_dir=workflows, runs_dir=runs,
                     registry_factory=lambda _: registry, api_only=True)
    headers = {"X-Atlas-Request": "1"}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for comment in ("", "   "):
            response = client.post(f"/api/runs/{run.run_id}/approve",
                                   json={"decision": "reject", "comment": comment},
                                   headers=headers)
            assert response.status_code == 400
            assert "驳回必须填写理由" in response.json()["detail"]
        # 批准不带说明仍然合法(理由只在驳回时强制)
        approved = client.post(f"/api/runs/{run.run_id}/approve",
                               json={"decision": "approve", "comment": ""},
                               headers=headers)
        assert approved.status_code == 200
        assert called.wait(timeout=2)
        # 驳回带理由通过校验(执行被桩住,不真正恢复)
        rejected = client.post(f"/api/runs/{run.run_id}/approve",
                               json={"decision": "reject", "comment": "结构缺失"},
                               headers=headers)
        assert rejected.status_code == 200


def test_unknown_and_empty_event_run_ids_are_404(tmp_path):
    runs = tmp_path / "runs"
    empty_run = runs / "empty-ledger"
    empty_run.mkdir(parents=True)
    (empty_run / "events.jsonl").touch()
    app = create_app(workflows_dir=tmp_path / "workflows", runs_dir=runs,
                     registry_factory=lambda _: make_registry(FakeProvider()),
                     api_only=True)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        for rid in ("does-not-exist", "empty-ledger"):
            assert client.get(f"/api/runs/{rid}").status_code == 404
            assert client.get(f"/api/runs/{rid}/events").status_code == 404
            assert client.delete(f"/api/runs/{rid}",
                                 headers=_delete_headers()).status_code == 404
        assert client.get("/api/runs").json() == []


def test_approval_conflict_is_synchronous_409(tmp_path, monkeypatch):
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    spec_path = workflows / "human_gate.yaml"
    spec_path.write_text((GRAPHS / "human_gate.yaml").read_text(encoding="utf-8"),
                         encoding="utf-8")
    runs = tmp_path / "runs"
    fake = FakeProvider()
    fake.configure("primary", text="方案")
    fake.configure("other", text="终稿")
    registry = make_registry(fake)
    run = execute_graph(load_graph("human_gate"), task=TASK_TEXT,
                        runs_root=runs, registry=registry)
    assert run.status == "paused"

    worker_started = threading.Event()
    release_worker = threading.Event()

    def blocking_approve(run_id, **kwargs):
        assert kwargs["_lock_held"] is True
        worker_started.set()
        assert release_worker.wait(timeout=5)
        release_approval_run_lock(run_id, runs_root=kwargs["runs_root"])

    monkeypatch.setattr(web_module, "approve_run", blocking_approve)
    app = create_app(workflows_dir=workflows, runs_dir=runs,
                     registry_factory=lambda _: registry, api_only=True)
    headers = {"X-Atlas-Request": "1"}
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            accepted = client.post(f"/api/runs/{run.run_id}/approve",
                                   json={"decision": "approve"}, headers=headers)
            assert accepted.status_code == 200
            assert worker_started.wait(timeout=2)
            assert run_lock_path(run.run_id, runs_root=runs).is_file()

            conflict = client.post(f"/api/runs/{run.run_id}/approve",
                                   json={"decision": "approve"}, headers=headers)
            assert conflict.status_code == 409
            assert "运行锁" in conflict.json()["detail"]
    finally:
        release_worker.set()


def test_completed_run_approval_is_409(tmp_path):
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "demo.yaml").write_text(
        "name: demo\nnodes:\n  - id: a\n    type: llm\n"
        "    model: Fake:primary\n    prompt: p\n    consumes: [task]\n"
        "edges:\n  - from: a\n    to: END\n", encoding="utf-8")
    runs = tmp_path / "runs"
    spec = load_graph("two_node")
    fake = FakeProvider()
    fake.configure("primary", text=good_writer_text(50))
    fake.configure("fallback", text=good_writer_text(10))
    fake.configure("other", text=good_review_text())
    registry = make_registry(fake)
    run = execute_graph(spec, task=TASK_TEXT, runs_root=runs, registry=registry)
    assert run.status == "done"
    app = create_app(workflows_dir=workflows, runs_dir=runs,
                     registry_factory=lambda _: registry, api_only=True)
    headers = {"X-Atlas-Request": "1"}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(f"/api/runs/{run.run_id}/approve",
                               json={"decision": "approve"}, headers=headers)
        assert response.status_code == 409


def test_allocated_run_without_events_is_starting(tmp_path, monkeypatch):
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    workflow = workflows / "demo.yaml"
    workflow.write_text(
        "name: demo\nnodes:\n  - id: a\n    type: llm\n"
        "    model: Fake:primary\n    prompt: p\n    consumes: [task]\n"
        "edges:\n  - from: a\n    to: END\n", encoding="utf-8")
    fake = FakeProvider()
    fake.configure("primary", text="done")
    release = threading.Event()

    def blocked_execute(*args, **kwargs):
        assert release.wait(timeout=5)

    monkeypatch.setattr(web_module, "execute_graph", blocked_execute)
    app = create_app(workflows_dir=workflows, runs_dir=tmp_path / "runs",
                     registry_factory=lambda _: make_registry(fake), api_only=True)
    headers = {"X-Atlas-Request": "1"}
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            run_id = client.post("/api/workflows/demo/run", json={"task": "t"},
                                 headers=headers).json()["run_id"]
            response = client.get(f"/api/runs/{run_id}")
            assert response.status_code == 200
            assert response.json()["status"] == "starting"
    finally:
        release.set()


def test_sse_reads_incrementally_and_resets_idle(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    run_dir = runs / "scripted"
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text(
        '{"seq":1,"ts":"t","type":"run_started","run_id":"scripted","graph":"g"}\n',
        encoding="utf-8")
    calls = []

    class ScriptedReader:
        def __init__(self, path):
            self.index = 0

        def all(self):
            return [{"seq": 1, "ts": "t", "type": "run_started",
                     "run_id": "scripted", "graph": "g"}]

        def read_from(self, offset=0):
            calls.append(offset)
            # 先累计到关闭阈值，再读到一条被 after 过滤的事件。读侧有进展
            # 必须重置 idle，否则下一轮会错误地产生 stream_closed。
            script = (
                [([], 0)] * 200
                + [([{"seq": 1, "ts": "t", "type": "run_started"}], 11)]
                + [([{"seq": 2, "ts": "t", "type": "run_done"}], 22)]
            )
            result = script[self.index]
            self.index += 1
            return result

    monkeypatch.setattr(web_module, "EventReader", ScriptedReader)
    monkeypatch.setattr(web_module, "derive_run_status",
                        lambda *args, **kwargs: "running")
    monkeypatch.setattr(web_module.time, "sleep", lambda _: None)
    app = create_app(workflows_dir=tmp_path / "workflows", runs_dir=runs,
                     registry_factory=lambda _: make_registry(FakeProvider()),
                     api_only=True)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/api/runs/scripted/events?after=1")
    assert response.status_code == 200
    assert calls[:200] == [0] * 200
    assert calls[200:] == [0, 11]
    assert '"type": "run_done"' in response.text
    assert "stream_closed" not in response.text


def test_sse_interrupted_is_non_persistent_control_event(tmp_path):
    runs = tmp_path / "runs"
    run_dir = _write_run(runs, "interrupted-sse")
    events_path = run_dir / "events.jsonl"
    ledger_before = events_path.read_bytes()
    app = create_app(workflows_dir=tmp_path / "workflows", runs_dir=runs,
                     registry_factory=lambda _: make_registry(FakeProvider()),
                     api_only=True)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/api/runs/interrupted-sse/events?after=1")

    assert response.status_code == 200
    assert response.text == (
        'event: run_interrupted\n'
        'data: {"type": "run_interrupted"}\n\n')
    assert '"seq"' not in response.text
    assert events_path.read_bytes() == ledger_before


def test_missing_web_dist_fails_loudly_but_api_only_is_explicit(tmp_path):
    missing = tmp_path / "missing-dist"
    with pytest.raises(RuntimeError) as exc:
        create_app(workflows_dir=tmp_path / "workflows", runs_dir=tmp_path / "runs",
                   web_dist_dir=missing)
    message = str(exc.value)
    assert "npm ci" in message and "npm run build" in message

    api_app = create_app(workflows_dir=tmp_path / "workflows",
                         runs_dir=tmp_path / "runs", api_only=True,
                         web_dist_dir=missing)
    assert all(route.name != "web" for route in api_app.routes)


def test_fastapi_version_uses_package_version(tmp_path):
    api_app = create_app(workflows_dir=tmp_path / "workflows",
                         runs_dir=tmp_path / "runs", api_only=True)
    assert api_app.version == __version__


def test_main_delegates_to_serve(monkeypatch):
    called = []
    monkeypatch.setattr(web_module, "serve", lambda: called.append(True))
    web_module.main()
    assert called == [True]


def test_serve_initializes_config_before_app(monkeypatch):
    order = []
    from atlas import config_init
    import uvicorn

    monkeypatch.setattr(config_init, "initialize_runtime_config",
                        lambda: order.append("init"))
    monkeypatch.setattr(web_module, "create_app",
                        lambda: order.append("app") or object())
    monkeypatch.setattr(uvicorn, "run",
                        lambda app, **kwargs: order.append("run"))
    web_module.serve()
    assert order == ["init", "app", "run"]


def test_path_traversal_blocked(app):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for bad in ("..%2F..%2Fconfig%2F.env", "..\\..\\config\\.env",
                    "C:secret.txt"):
            resp = client.get(f"/api/runs/x/artifacts/{bad}")
            assert resp.status_code in (400, 404)


def test_serve_refuses_non_localhost():
    from atlas.web import serve
    with pytest.raises(ValueError, match="127.0.0.1"):
        serve(host="0.0.0.0")


def test_invalid_workflow_errors_are_strings_with_source_location(app):
    expected = "path nodes[0].type, line 4, column 5"
    with TestClient(app, base_url="http://127.0.0.1") as client:
        item = next(item for item in client.get("/api/workflows").json()
                    if item["id"] == "broken")
        assert isinstance(item["error"], str)
        assert expected in item["error"]

        response = client.get("/api/workflows/broken")
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert isinstance(detail, str)
        assert expected in detail


def test_persisted_running_run_is_interrupted_only_while_lock_is_free(tmp_path):
    runs = tmp_path / "runs"
    _write_run(runs, "dynamic-run")
    api = create_app(workflows_dir=tmp_path / "workflows", runs_dir=runs,
                     registry_factory=lambda _: make_registry(FakeProvider()),
                     api_only=True)

    with TestClient(api, base_url="http://127.0.0.1") as client:
        assert client.get("/api/runs/dynamic-run").json()["status"] == "interrupted"
        listed = {item["run_id"]: item for item in client.get("/api/runs").json()}
        assert listed["dynamic-run"]["status"] == "interrupted"

        acquire_run_lock("dynamic-run", runs_root=runs)
        try:
            assert client.get("/api/runs/dynamic-run").json()["status"] == "running"
            listed = {item["run_id"]: item
                      for item in client.get("/api/runs").json()}
            assert listed["dynamic-run"]["status"] == "running"
        finally:
            release_run_lock("dynamic-run", runs_root=runs)


def test_web_resume_requires_header_and_rejects_terminal_runs(tmp_path):
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "demo.yaml").write_text(
        "name: demo\nnodes:\n  - id: a\n    type: llm\n"
        "    model: Fake:primary\n    prompt: p\n    consumes: [task]\n"
        "edges:\n  - from: a\n    to: END\n", encoding="utf-8")
    runs = tmp_path / "runs"
    for rid, terminal in (("paused-run", "paused"),
                          ("done-run", "run_done"),
                          ("failed-run", "run_failed")):
        _write_run(runs, rid, terminal)
    def forbidden_registry(_):
        raise AssertionError("终态 resume 不得预检当前后端")

    api = create_app(workflows_dir=workflows, runs_dir=runs,
                     registry_factory=forbidden_registry, api_only=True)

    with TestClient(api, base_url="http://127.0.0.1") as client:
        missing_header = client.post("/api/runs/done-run/resume")
        assert missing_header.status_code == 403
        assert "X-Atlas-Request" in missing_header.json()["detail"]

        for rid, status in (("paused-run", "paused"), ("done-run", "done"),
                            ("failed-run", "failed")):
            response = client.post(f"/api/runs/{rid}/resume",
                                   headers={"X-Atlas-Request": "1"})
            assert response.status_code == 409
            assert status in response.json()["detail"]


def test_web_resume_accepts_interrupted_and_rejects_duplicate_live_resume(
        tmp_path, monkeypatch):
    from atlas.adapters import AllCandidatesFailed
    from atlas.engine import prepare_execution
    from atlas.events import EventReader
    from conftest import standard_fake

    runs = tmp_path / "runs"
    spec = load_graph("three_node")
    fake = standard_fake(100)
    fake.configure("third", transport_error="simulated process loss")
    registry = make_registry(fake)
    prepared = prepare_execution(spec, registry)
    with pytest.raises(AllCandidatesFailed):
        execute_graph(spec, task=TASK_TEXT, runs_root=runs, prepared=prepared)
    run_dir = next(runs.glob("*/events.jsonl")).parent
    events_path = run_dir / "events.jsonl"
    events = EventReader(events_path).all()
    assert events[-1]["type"] == "run_failed"
    events_path.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n"
                for event in events[:-1]), encoding="utf-8")
    fake.configure("third", text="recovered")

    entered = threading.Event()
    release = threading.Event()
    real_resume = web_module.resume_graph

    def blocking_resume(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return real_resume(*args, **kwargs)

    monkeypatch.setattr(web_module, "resume_graph", blocking_resume)
    api = create_app(workflows_dir=tmp_path / "workflows", runs_dir=runs,
                     registry_factory=lambda _: registry, api_only=True)
    headers = {"X-Atlas-Request": "1"}
    try:
        with TestClient(api, base_url="http://127.0.0.1") as client:
            accepted = client.post(f"/api/runs/{run_dir.name}/resume",
                                   headers=headers)
            assert accepted.status_code == 202
            assert accepted.json() == {"run_id": run_dir.name,
                                       "status": "running"}
            assert entered.wait(timeout=2)

            duplicate = client.post(f"/api/runs/{run_dir.name}/resume",
                                    headers=headers)
            assert duplicate.status_code == 409
            assert "活跃本地控制器" in duplicate.json()["detail"]
            release.set()

            for _ in range(50):
                summary = client.get(f"/api/runs/{run_dir.name}").json()
                if summary["status"] in ("done", "failed"):
                    break
                time.sleep(0.1)
            assert summary["status"] == "done", summary.get("failed_error")
    finally:
        release.set()

    final_events = EventReader(events_path).all()
    assert sum(event["type"] == "run_resumed" for event in final_events) == 1
    assert sum(event.get("type") == "node_done" and event.get("node") == "node_a"
               for event in final_events) == 1


def test_delete_workflow_endpoint_custom_vs_example(app):
    headers = {"X-Atlas-Request": "1"}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        # demo 未标 meta.kind=example,属于普通图,应可删
        resp = client.delete("/api/workflows/demo", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        # 再删 → 404 如实
        again = client.delete("/api/workflows/demo", headers=headers)
        assert again.status_code == 404
        # 无写头 → 403
        assert client.delete("/api/workflows/broken").status_code == 403


def test_delete_workflow_example_requires_flag(tmp_path):
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "shipped.yaml").write_text(
        "name: shipped\nmeta:\n  kind: example\nnodes:\n  - id: a\n    type: llm\n"
        "    model: Fake:primary\n    prompt: p\n    consumes: [task]\n"
        "edges:\n  - from: a\n    to: END\n", encoding="utf-8")
    api = create_app(workflows_dir=workflows, runs_dir=tmp_path / "runs",
                     registry_factory=lambda _: make_registry(FakeProvider()),
                     api_only=True)
    headers = {"X-Atlas-Request": "1"}
    with TestClient(api, base_url="http://127.0.0.1") as client:
        blocked = client.delete("/api/workflows/shipped", headers=headers)
        assert blocked.status_code == 400
        assert "内置示例" in blocked.json()["detail"]
        forced = client.delete("/api/workflows/shipped?allow_example=1",
                               headers=headers)
        assert forced.status_code == 200
        assert not (workflows / "shipped.yaml").exists()
