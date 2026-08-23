# -*- coding: utf-8 -*-
"""REV-001 稳定运行锁的跨线程、跨进程与双向竞争回归。"""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import atlas.engine as engine_module
import atlas.web as web_module
from atlas.adapters import FakeProvider
from atlas.engine import (RunConflictError, acquire_run_lock, approve_run,
                          execute_graph, release_run_lock, resume_graph,
                          run_lock_path)
from atlas.web import create_app

from conftest import TASK_TEXT, load_graph, make_registry, standard_fake


def _write_terminal_run(runs: Path, rid: str) -> Path:
    run_dir = runs / rid
    run_dir.mkdir(parents=True)
    records = [
        {"seq": 1, "ts": "t", "type": "run_started", "run_id": rid,
         "graph": "two_node"},
        {"seq": 2, "ts": "t", "type": "run_done", "run_id": rid},
    ]
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8")
    (run_dir / "checkpoint.sqlite").write_bytes(b"checkpoint")
    return run_dir


def _api(runs: Path):
    return create_app(
        workflows_dir=runs.parent / "workflows",
        runs_dir=runs,
        registry_factory=lambda _: make_registry(FakeProvider()),
        api_only=True,
    )


def test_live_cross_process_lock_is_never_stolen_by_old_mtime(tmp_path):
    runs = tmp_path / "runs"
    rid = "cross-process"
    code = "\n".join([
        "import sys",
        "from pathlib import Path",
        "from atlas.engine import acquire_run_lock, release_run_lock",
        f"root = Path({str(runs)!r})",
        f"rid = {rid!r}",
        "acquire_run_lock(rid, runs_root=root)",
        "print('READY', flush=True)",
        "sys.stdin.readline()",
        "release_run_lock(rid, runs_root=root)",
    ])
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "READY"
        lock = run_lock_path(rid, runs_root=runs)
        os.utime(lock, (1, 1))
        with pytest.raises(RunConflictError, match="其他进程"):
            acquire_run_lock(rid, runs_root=runs)
    finally:
        if process.stdin:
            process.stdin.write("release\n")
            process.stdin.flush()
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stdout + stderr

    acquire_run_lock(rid, runs_root=runs)
    release_run_lock(rid, runs_root=runs)
    assert lock.is_file()


def test_initial_execute_holds_lock_until_invoke_returns(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    rid = "executing-run"
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def blocking_invoke(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return object()

    monkeypatch.setattr(engine_module, "_invoke", blocking_invoke)
    fake = standard_fake(100)

    def execute():
        try:
            execute_graph(load_graph("two_node"), task=TASK_TEXT,
                          runs_root=runs, registry=make_registry(fake), run_id=rid)
        except Exception as exc:  # pragma: no cover - diagnostic on thread failure
            errors.append(exc)

    worker = threading.Thread(target=execute)
    worker.start()
    assert entered.wait(timeout=3)
    try:
        with TestClient(_api(runs), base_url="http://127.0.0.1") as client:
            response = client.delete(
                f"/api/runs/{rid}", headers={"X-Atlas-Request": "1"})
        assert response.status_code == 423
        assert (runs / rid).is_dir()
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert errors == []


def test_delete_lock_blocks_execute_resume_and_approve(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    rid = "deleting-run"
    run_dir = _write_terminal_run(runs, rid)
    entered = threading.Event()
    release = threading.Event()
    real_rmtree = web_module.shutil.rmtree

    def blocking_rmtree(path):
        entered.set()
        assert release.wait(timeout=5)
        real_rmtree(path)

    monkeypatch.setattr(web_module.shutil, "rmtree", blocking_rmtree)
    result = []

    def delete():
        with TestClient(_api(runs), base_url="http://127.0.0.1") as client:
            result.append(client.delete(
                f"/api/runs/{rid}", headers={"X-Atlas-Request": "1"}).status_code)

    worker = threading.Thread(target=delete)
    worker.start()
    assert entered.wait(timeout=3)
    assert not run_dir.exists()
    assert (runs / ".trash" / rid).exists()

    fake = standard_fake(100)
    registry = make_registry(fake)
    try:
        with pytest.raises(RunConflictError, match="运行锁"):
            execute_graph(load_graph("two_node"), task=TASK_TEXT,
                          runs_root=runs, registry=registry, run_id=rid)
        with pytest.raises(RunConflictError, match="运行锁"):
            resume_graph(rid, spec=load_graph("two_node"), runs_root=runs,
                         registry=registry)
        with pytest.raises(RunConflictError, match="运行锁"):
            approve_run(rid, decision="approve", comment="",
                        spec=load_graph("human_gate"), runs_root=runs,
                        registry=registry)
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert result == [200]
