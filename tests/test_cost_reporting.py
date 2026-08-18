# -*- coding: utf-8 -*-
"""失败调用的成本账本仍必须在 Web/MCP 中可见。"""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from atlas import mcp as mcp_module
from atlas.adapters import FakeProvider
from atlas.web import create_app

from conftest import make_registry


def _write_failed_cost_run(runs: Path, rid: str = "failed-cost") -> Path:
    run_dir = runs / rid
    run_dir.mkdir(parents=True)
    records = [
        {"seq": 1, "ts": "2026-08-18T00:00:00+00:00", "type": "run_started",
         "run_id": rid, "graph": "cost-demo"},
        {"seq": 2, "ts": "2026-08-18T00:00:01+00:00", "type": "cost_reserved",
         "node": "agent", "iteration": 1, "reservation_id": "reservation-1",
         "reserved_usd": 0.5},
        {"seq": 3, "ts": "2026-08-18T00:00:02+00:00", "type": "cost_settled",
         "node": "agent", "iteration": 1, "reservation_id": "reservation-1",
         "actual_cost_usd": 0.7, "accounted_cost_usd": 0.7,
         "cost_unknown": False, "cost_usd": 0.7,
         "input_tokens": 12, "output_tokens": 4},
        {"seq": 4, "ts": "2026-08-18T00:00:03+00:00", "type": "run_failed",
         "run_id": rid, "error_type": "CostExceeded", "error": "over cap"},
    ]
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records),
        encoding="utf-8")
    return run_dir


def test_web_and_mcp_report_settled_cost_without_node_done(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    _write_failed_cost_run(runs)

    app = create_app(
        workflows_dir=workflows, runs_dir=runs,
        registry_factory=lambda _: make_registry(FakeProvider()), api_only=True)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        web = client.get("/api/runs/failed-cost")
    assert web.status_code == 200
    assert web.json()["totals"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "known_actual_cost_usd": 0.7,
        "accounted_cost_usd": 0.7,
        "actual_cost_unknown_count": 0,
        "outstanding_reserved_usd": 0.0,
        "cost_usd": 0.7,
    }

    monkeypatch.setattr(mcp_module, "RUNS_DIR", runs)
    mcp = mcp_module.summarize_run("failed-cost")
    assert mcp["totals"]["known_actual_cost_usd"] == 0.7
    assert mcp["totals"]["accounted_cost_usd"] == 0.7
    assert mcp["totals"]["actual_cost_unknown_count"] == 0
    assert mcp["totals"]["cost_usd"] == 0.7
