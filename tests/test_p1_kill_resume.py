# -*- coding: utf-8 -*-
"""P1 acceptance: a force-killed workflow is detected and resumes safely."""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from atlas.adapters import AdapterRegistry, ModelResponse, Usage
from atlas.engine import (RunConflictError, acquire_run_lock, execute_graph,
                          prepare_execution, release_run_lock, resume_graph,
                          run_lock_path)
from atlas.events import EventReader
from atlas.runs import derive_run_status
from atlas.spec import EdgeSpec, NodeSpec, WorkflowSpec


class DeterministicKillAdapter:
    """Filesystem-controlled adapter with one identity in both processes."""

    protocol = "p1-kill"
    nonsecret_execution_descriptor = True
    max_output_tokens = 128

    def __init__(self, control_dir: Path, *, block_node_b: bool) -> None:
        self.control_dir = Path(control_dir)
        self.block_node_b = block_node_b

    def execution_descriptor(self) -> dict:
        return {
            "version": 1,
            "kind": "p1-kill-acceptance",
            "protocol": self.protocol,
            "default_max_output_tokens": self.max_output_tokens,
        }

    def call(self, model_id: str, prompt: str, extra_body=None,
             timeout_s=None) -> ModelResponse:
        self.control_dir.mkdir(parents=True, exist_ok=True)
        with open(self.control_dir / "calls.log", "a", encoding="utf-8") as log:
            log.write(model_id + "\n")
            log.flush()
            os.fsync(log.fileno())

        if model_id == "a":
            text = "node A completed"
        elif model_id == "b" and self.block_node_b:
            with open(self.control_dir / "node-b-blocked", "w",
                      encoding="utf-8") as marker:
                marker.write("blocked\n")
                marker.flush()
                os.fsync(marker.fileno())
            threading.Event().wait()
            raise AssertionError("unreachable after an infinite wait")
        elif model_id == "b":
            text = "node B completed after resume"
        else:  # pragma: no cover - malformed test setup
            raise AssertionError(f"unexpected model {model_id!r}")

        return ModelResponse(
            text=text,
            usage=Usage(input_tokens=max(1, len(prompt) // 3), output_tokens=2),
        )


def _workflow() -> WorkflowSpec:
    return WorkflowSpec(
        name="p1-kill-resume",
        nodes=[
            NodeSpec(id="node_a", type="llm", model="Kill:a",
                     prompt="complete A", consumes=["task"]),
            NodeSpec(id="node_b", type="llm", model="Kill:b",
                     prompt="complete B", consumes=["task", "node_a.output"]),
        ],
        edges=[EdgeSpec("node_a", "node_b"), EdgeSpec("node_b", "END")],
        entry="node_a",
    )


def _prepared(control_dir: Path, *, block_node_b: bool):
    registry = AdapterRegistry()
    registry.register(
        "Kill", ["a", "b"],
        DeterministicKillAdapter(control_dir, block_node_b=block_node_b),
    )
    return prepare_execution(_workflow(), registry)


def _child_main(runs_root: Path, run_id: str, control_dir: Path) -> None:
    prepared = _prepared(control_dir, block_node_b=True)
    execute_graph(
        _workflow(), task="force-kill acceptance", runs_root=runs_root,
        run_id=run_id, prepared=prepared,
    )


def _wait_for_persisted_block(process: subprocess.Popen, events_path: Path,
                              checkpoint_path: Path, marker_path: Path) -> list[dict]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        records = EventReader(events_path).all()
        node_a_done = any(
            event.get("type") == "node_done" and event.get("node") == "node_a"
            for event in records
        )
        node_b_started = any(
            event.get("type") == "node_started" and event.get("node") == "node_b"
            for event in records
        )
        if node_a_done and node_b_started and checkpoint_path.exists() \
                and checkpoint_path.stat().st_size > 0 and marker_path.exists():
            return records
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=5)
            raise AssertionError(
                f"child exited before persisted kill milestones "
                f"({process.returncode})\nstdout:\n{stdout}\nstderr:\n{stderr}"
            )
        time.sleep(0.02)
    raise AssertionError("timed out waiting for node A checkpoint and blocked node B")


def _wait_for_lock_free(run_id: str, runs_root: Path) -> None:
    deadline = time.monotonic() + 10
    last_conflict = None
    while time.monotonic() < deadline:
        try:
            acquire_run_lock(run_id, runs_root=runs_root)
        except RunConflictError as exc:
            last_conflict = exc
            time.sleep(0.02)
            continue
        release_run_lock(run_id, runs_root=runs_root)
        return
    raise AssertionError(f"OS run lock did not become free: {last_conflict}")


def test_force_killed_subprocess_is_interrupted_and_resumes_once(tmp_path):
    runs_root = tmp_path / "runs"
    control_dir = tmp_path / "control"
    run_id = "p1-force-killed"
    run_dir = runs_root / run_id
    events_path = run_dir / "events.jsonl"
    checkpoint_path = run_dir / "checkpoint.sqlite"
    marker_path = control_dir / "node-b-blocked"

    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), str(runs_root), run_id,
         str(control_dir)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child_output = None
    try:
        before_kill = _wait_for_persisted_block(
            process, events_path, checkpoint_path, marker_path)
        assert run_lock_path(run_id, runs_root=runs_root).is_file()
        with pytest.raises(RunConflictError):
            acquire_run_lock(run_id, runs_root=runs_root)
        assert derive_run_status(
            before_kill, run_id=run_id, runs_root=runs_root) == "running"

        process.kill()
        try:
            child_output = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive cleanup
            process.kill()
            child_output = process.communicate(timeout=5)
        assert process.returncode != 0, child_output
    finally:
        if process.poll() is None:
            process.kill()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - OS failure
                process.kill()
                process.communicate(timeout=5)

    _wait_for_lock_free(run_id, runs_root)
    assert run_lock_path(run_id, runs_root=runs_root).is_file()

    interrupted_events = EventReader(events_path).all()
    assert derive_run_status(
        interrupted_events, run_id=run_id, runs_root=runs_root
    ) == "interrupted"
    assert not any(
        event.get("type") in {"run_done", "run_failed"}
        for event in interrupted_events
    )

    prepared = _prepared(control_dir, block_node_b=False)
    started = next(event for event in interrupted_events
                   if event.get("type") == "run_started")
    assert started["backend_sha256"] == prepared.backend_sha256
    assert started["execution_sha256"] == prepared.execution_sha256

    resumed = resume_graph(
        run_id, spec=_workflow(), runs_root=runs_root, prepared=prepared)
    events = resumed.events.all()
    assert resumed.status == "done"
    assert resumed.folded()["status"] == "done"
    assert sum(event.get("type") == "run_resumed" for event in events) == 1
    assert sum(
        event.get("type") == "node_done" and event.get("node") == "node_a"
        for event in events
    ) == 1
    assert sum(
        event.get("type") == "node_done" and event.get("node") == "node_b"
        for event in events
    ) == 1

    calls = (control_dir / "calls.log").read_text(encoding="utf-8").splitlines()
    assert calls.count("a") == 1
    assert calls.count("b") == 2

    seqs = [event["seq"] for event in events]
    assert all(left < right for left, right in zip(seqs, seqs[1:]))
    assert len(seqs) == len(set(seqs))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: test_p1_kill_resume.py RUNS_ROOT RUN_ID CONTROL_DIR")
    _child_main(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]))
