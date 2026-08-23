# -*- coding: utf-8 -*-
"""P0min：真实进程崩溃后，已派发 LLM 的预算预留仍可重放。"""
import subprocess
import sys
import time
from pathlib import Path

import pytest

from atlas.costs import CostLedger, CostLimitError, fold_cost_accounting
from atlas.events import EventReader


ROOT = Path(__file__).resolve().parents[1]


def test_killed_llm_process_keeps_persisted_reservation(tmp_path):
    runs_root = tmp_path / "runs"
    run_id = "p0min-killed-provider"
    child_code = r'''
import sys
import threading
from pathlib import Path

from atlas import costs
from atlas.adapters import AdapterRegistry
from atlas.engine import execute_graph
from atlas.spec import EdgeSpec, Guards, NodeSpec, WorkflowSpec


class BlockingAdapter:
    protocol = "blocking"
    max_output_tokens = 1

    def call(self, model_id, prompt, extra_body=None, timeout_s=None):
        Path(sys.argv[3]).write_text("entered", encoding="utf-8")
        threading.Event().wait()


costs._cache = {"prices": {}}
registry = AdapterRegistry()
registry.register("Block", ["model"], BlockingAdapter())
spec = WorkflowSpec(
    name="p0min-crash",
    nodes=[NodeSpec(
        id="solo", type="llm", model="Block:model", prompt="执行",
        consumes=["task"], max_output_tokens=1,
    )],
    edges=[EdgeSpec("solo", "END")],
    entry="solo",
    guards=Guards(max_cost_usd=1.0),
)
execute_graph(
    spec, task="crash-window", runs_root=Path(sys.argv[1]),
    run_id=sys.argv[2], registry=registry,
)
'''
    entered_path = tmp_path / "provider-entered"
    process = subprocess.Popen(
        [sys.executable, "-c", child_code, str(runs_root), run_id,
         str(entered_path)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    events_path = runs_root / run_id / "events.jsonl"
    deadline = time.monotonic() + 15
    reserved = None
    try:
        while time.monotonic() < deadline:
            events = EventReader(events_path).all()
            reserved = next(
                (event for event in events
                 if event.get("type") == "cost_reserved"
                 and event.get("node") == "solo"),
                None,
            )
            if reserved is not None and entered_path.exists():
                break
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=5)
                raise AssertionError(
                    f"子进程在预留落盘前退出 ({process.returncode})\n"
                    f"stdout:\n{stdout}\nstderr:\n{stderr}"
                )
            time.sleep(0.02)
        assert reserved is not None, "未在期限内观察到已 flush 的 cost_reserved"
        assert isinstance(reserved.get("reservation_id"), str)
        assert reserved["reservation_id"]
        assert not any(
            event.get("type") == "cost_settled"
            and event.get("reservation_id") == reserved["reservation_id"]
            for event in EventReader(events_path).all()
        )
    finally:
        if process.poll() is None:
            process.kill()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)

    events = EventReader(events_path).all()
    accounting = fold_cost_accounting(events)
    assert reserved["reserved_usd"] == 1.0
    assert accounting.known_actual_usd == 0.0
    assert accounting.accounted_usd == reserved["reserved_usd"]
    assert accounting.outstanding_reserved_usd == reserved["reserved_usd"]
    assert accounting.unknown_count == 1
    replayed = CostLedger(1.0, spent=accounting.accounted_usd)
    with pytest.raises(CostLimitError):
        replayed.reserve_remaining(description="重放后的下一次派发")
    assert not any(event.get("type") in {"run_done", "run_failed"}
                   for event in events)
