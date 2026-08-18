# -*- coding: utf-8 -*-
"""成本守卫与熔断(M2)。"""
import threading

import pytest

from atlas import costs
from atlas.adapters import FakeProvider, Usage, breaker
from atlas.costs import compute_cost_usd
from atlas.engine import CostExceeded, execute_graph
from atlas.events import EventReader
from atlas.nodes.agent import AgentCliError
from atlas.nodes.local_cli import AgentRunResult
from atlas.spec import (EdgeSpec, Guards, NodeSpec, WorkflowSpec,
                        spec_from_yaml)

from conftest import TASK_TEXT, good_review_text, make_registry


@pytest.fixture(autouse=True)
def _fresh_breaker():
    breaker.reset()
    yield
    breaker.reset()


# ── 成本计算 ─────────────────────────────────────────────────


def test_compute_cost_known_price(monkeypatch):
    monkeypatch.setattr(costs, "_cache", {
        "prices": {"Fake:primary": {"input_per_m": 1.0, "output_per_m": 2.0}}})
    assert compute_cost_usd("Fake:primary", 1_000_000, 1_000_000) == 3.0
    assert compute_cost_usd("Fake:primary", 500_000, 0) == 0.5


def test_compute_cost_unknown_is_null(monkeypatch):
    monkeypatch.setattr(costs, "_cache", {"prices": {}})
    assert compute_cost_usd("Fake:primary", 100, 100) is None
    monkeypatch.setattr(costs, "_cache", {
        "prices": {"Fake:primary": {"input_per_m": None, "output_per_m": 2.0}}})
    assert compute_cost_usd("Fake:primary", 100, 100) is None  # 半个费率也不猜


def test_vendor_wildcard_pricing(monkeypatch):
    monkeypatch.setattr(costs, "_cache", {
        "prices": {"Fake:*": {"input_per_m": 0.5, "output_per_m": 0.5}}})
    assert compute_cost_usd("Fake:anything", 1_000_000, 0) == 0.5


# ── 成本守卫 ─────────────────────────────────────────────────


def _cost_guard(max_cost_usd: float) -> WorkflowSpec:
    return WorkflowSpec(
        name="cost_guard",
        nodes=[
            NodeSpec(id="node_a", type="llm", model="Fake:primary",
                     prompt="第一步。", consumes=["task"]),
            NodeSpec(id="node_b", type="llm", model="Fake:other",
                     prompt="第二步。", consumes=["task", "node_a.output"]),
        ],
        edges=[EdgeSpec("node_a", "node_b"), EdgeSpec("node_b", "END")],
        entry="node_a",
        guards=Guards(max_iterations=3, max_cost_usd=max_cost_usd),
    )


def test_cost_guard_stops_before_overspend(tmp_path, monkeypatch):
    """两道查第一道:派发前 spent+projected>cap,连第一步都不开始。"""
    monkeypatch.setattr(costs, "_cache", {
        "prices": {"Fake:primary": {"input_per_m": 1_000_000.0,   # 每百万 token $1M:
                                    "output_per_m": 1_000_000.0},  # 预估一次就爆表
                  "Fake:other": {"input_per_m": 1.0, "output_per_m": 1.0}}})
    fake = FakeProvider()
    fake.configure("primary", text="贵的调用")
    fake.configure("other", text="便宜的调用")

    with pytest.raises(CostExceeded) as e:
        execute_graph(_cost_guard(max_cost_usd=0.01), task=TASK_TEXT,
                      runs_root=tmp_path, registry=make_registry(fake))
    assert "max_cost_usd" in str(e.value)
    assert "预估" in str(e.value)   # 第一道查:带着预估拦,不是花超了才拦

    reader = EventReader(next(tmp_path.glob("*/events.jsonl")))
    assert reader.find(type="node_done", node="node_a") is None  # 一步都没花
    assert reader.find(type="run_failed")["error_type"] == "CostExceeded"


def test_cost_guard_first_done_second_blocked(tmp_path, monkeypatch):
    """第一步便宜跑完,第二步派发前被预估拦住(Quorum:别等花超才停)。"""
    monkeypatch.setattr(costs, "_cache", {
        "prices": {"Fake:primary": {"input_per_m": 1.0, "output_per_m": 1.0},   # 便宜
                  "Fake:other": {"input_per_m": 1_000_000.0,                    # 贵
                                 "output_per_m": 1_000_000.0}}})
    fake = FakeProvider()
    fake.configure("primary", text="便宜的第一步")
    fake.configure("other", text="贵的第二步")

    with pytest.raises(CostExceeded):
        execute_graph(_cost_guard(max_cost_usd=0.01), task=TASK_TEXT,
                      runs_root=tmp_path, registry=make_registry(fake))

    reader = EventReader(next(tmp_path.glob("*/events.jsonl")))
    assert reader.find(type="node_done", node="node_a") is not None  # 第一步完成
    assert reader.find(type="node_started", node="node_b") is None    # 第二步没花钱


def test_cost_unknown_warns_when_guard_set(tmp_path, monkeypatch):
    monkeypatch.setattr(costs, "_cache", {"prices": {}})   # 全部未知
    fake = FakeProvider()
    fake.configure("primary", text="第一步")
    fake.configure("other", text="第二步")

    run = execute_graph(_cost_guard(max_cost_usd=5.0), task=TASK_TEXT,
                        runs_root=tmp_path, registry=make_registry(fake))
    warn = run.events.find(type="cost_unknown")
    assert warn is not None, "设了守卫但费率未知,必须记警告"
    assert "Fake:primary" in warn["models"]
    assert run.folded()["status"] == "done"   # 运行本身照常完成
    assert run.events.find(type="node_done", node="node_a")["cost_usd"] is None


def test_cost_ledger_reservation_is_atomic_under_concurrency():
    """并行候选同时预留时只有一个能占用同一份预算。"""
    ledger = costs.CostLedger(1.0)
    barrier = threading.Barrier(3)
    results = []

    def reserve():
        barrier.wait()
        try:
            results.append(ledger.reserve(0.6, description="并发调用"))
        except costs.CostLimitError:
            results.append("blocked")

    threads = [threading.Thread(target=reserve) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sum(r == "blocked" for r in results) == 1
    assert ledger.totals == (0.0, 0.6)


def test_cost_ledger_settlement_is_idempotent_and_unknown_is_conservative():
    ledger = costs.CostLedger(0.5)
    reservation = ledger.reserve_remaining(description="agent")
    assert reservation is not None
    accounted = ledger.settle(
        reservation, None, description="unknown agent",
        unknown_as_reserved=True)
    assert accounted == 0.5
    assert ledger.totals == (0.5, 0.0)
    assert ledger.settle(
        reservation, 0.1, description="duplicate",
        unknown_as_reserved=True) is None
    assert ledger.totals == (0.5, 0.0)


def test_cost_ledger_reserve_remaining_is_atomic_under_concurrency():
    ledger = costs.CostLedger(0.5)
    barrier = threading.Barrier(3)
    results = []

    def reserve():
        barrier.wait()
        try:
            results.append(ledger.reserve_remaining(description="agent"))
        except costs.CostLimitError:
            results.append("blocked")

    threads = [threading.Thread(target=reserve) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert sum(item == "blocked" for item in results) == 1
    assert ledger.totals == (0.0, 0.5)


def test_cost_event_replay_accounts_crashed_pending_reservation():
    accounting = costs.fold_cost_accounting([
        {"type": "cost_reserved", "reservation_id": "r1",
         "reserved_usd": 0.5},
    ])
    assert accounting.known_actual_usd == 0.0
    assert accounting.accounted_usd == 0.5
    assert accounting.unknown_count == 1
    assert accounting.outstanding_reserved_usd == 0.5


def test_cost_event_replay_is_idempotent_by_reservation_id():
    accounting = costs.fold_cost_accounting([
        {"type": "cost_reserved", "reservation_id": "r1",
         "reserved_usd": 0.5},
        {"type": "cost_reserved", "reservation_id": "r1",
         "reserved_usd": 0.5},
        {"type": "cost_settled", "reservation_id": "r1",
         "actual_cost_usd": 0.1, "accounted_cost_usd": 0.1,
         "cost_unknown": False},
        {"type": "cost_settled", "reservation_id": "r1",
         "actual_cost_usd": 0.1, "accounted_cost_usd": 0.1,
         "cost_unknown": False},
    ])
    assert accounting.known_actual_usd == 0.1
    assert accounting.accounted_usd == 0.1
    assert accounting.unknown_count == 0
    assert accounting.outstanding_reserved_usd == 0.0


class _CostAgentRunner:
    production_runner = True
    runner_name = "local_cli"

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.budgets = []

    def __call__(self, attachment, *, max_budget_usd=None, **kwargs):
        self.budgets.append(max_budget_usd)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _agent_cost_spec(*, retry=0, two_nodes=False) -> WorkflowSpec:
    nodes = [NodeSpec(
        id="agent_a", type="research", model="Stub:a", prompt="调研",
        consumes=["task"], retry=retry)]
    edges = [EdgeSpec("agent_a", "END")]
    if two_nodes:
        nodes.append(NodeSpec(
            id="agent_b", type="research", model="Stub:b", prompt="复核",
            consumes=["agent_a.output"]))
        edges = [EdgeSpec("agent_a", "agent_b"), EdgeSpec("agent_b", "END")]
    return WorkflowSpec(
        name="agent-cost", nodes=nodes, edges=edges, entry="agent_a",
        guards=Guards(max_cost_usd=0.5))


def test_unknown_agent_cost_consumes_budget_and_blocks_next_node(tmp_path):
    runner = _CostAgentRunner([
        AgentRunResult("first", usage=Usage(10, 2), cost_usd=None),
        AgentRunResult("must not run", cost_usd=0.1),
    ])
    with pytest.raises(CostExceeded):
        execute_graph(
            _agent_cost_spec(two_nodes=True), task=TASK_TEXT,
            runs_root=tmp_path, registry=make_registry(FakeProvider()),
            agent_runner=runner)
    assert runner.budgets == [0.5]
    events = EventReader(next(tmp_path.glob("*/events.jsonl"))).all()
    accounting = costs.fold_cost_accounting(events)
    assert accounting.accounted_usd == 0.5
    assert accounting.known_actual_usd == 0.0
    assert accounting.unknown_count == 1


def test_failed_agent_attempt_conservatively_consumes_budget_before_retry(tmp_path):
    runner = _CostAgentRunner([
        AgentCliError("transport failed"),
        AgentRunResult("must not retry", cost_usd=0.1),
    ])
    with pytest.raises(CostExceeded):
        execute_graph(
            _agent_cost_spec(retry=1), task=TASK_TEXT,
            runs_root=tmp_path, registry=make_registry(FakeProvider()),
            agent_runner=runner)
    assert runner.budgets == [0.5]
    events = EventReader(next(tmp_path.glob("*/events.jsonl"))).all()
    assert len([e for e in events if e["type"] == "cost_reserved"]) == 1
    accounting = costs.fold_cost_accounting(events)
    assert accounting.accounted_usd == 0.5
    assert accounting.unknown_count == 1


def test_known_agent_cost_over_cap_is_persisted_before_failure(tmp_path):
    runner = _CostAgentRunner([
        AgentRunResult("over cap", usage=Usage(12, 4), cost_usd=0.7),
    ])
    with pytest.raises(CostExceeded):
        execute_graph(
            _agent_cost_spec(), task=TASK_TEXT, runs_root=tmp_path,
            registry=make_registry(FakeProvider()), agent_runner=runner)
    events = EventReader(next(tmp_path.glob("*/events.jsonl"))).all()
    settled = next(e for e in events if e["type"] == "cost_settled")
    assert settled["actual_cost_usd"] == 0.7
    assert settled["accounted_cost_usd"] == 0.7
    assert any(e["type"] == "run_failed" for e in events)
    accounting = costs.fold_cost_accounting(events)
    assert accounting.known_actual_usd == 0.7
    assert accounting.accounted_usd == 0.7


# ── 熔断 ─────────────────────────────────────────────────────


def test_breaker_opens_after_consecutive_transport_failures(tmp_path):
    fake = FakeProvider()
    fake.configure("primary", transport_error="网关 502")
    fake.configure("fallback", text="备用顶上")

    spec = WorkflowSpec(
        name="breaker",
        nodes=[NodeSpec(id="solo", type="llm", model="Fake:primary",
                        fallback=["Fake:fallback"],
                        prompt="干点活", consumes=["task"])],
        edges=[EdgeSpec("solo", "END")],
        entry="solo",
    )
    # 三次运行:每次 primary 都传输失败、fallback 成功 → primary 连续失败 3 次
    for i in range(3):
        run = execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                            registry=make_registry(fake))
        assert run.events.find(type="node_done")["model_used"] == "Fake:fallback"

    # 第四次:primary 熔断打开,直接跳过(假供应商没收到它的调用)
    calls_before = len(fake.calls)
    run = execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                        registry=make_registry(fake))
    assert run.events.find(type="breaker_open", model="Fake:primary") is not None
    assert len(fake.calls) == calls_before + 1   # 只有 fallback 被调
    assert run.events.find(type="node_done")["model_used"] == "Fake:fallback"


def test_breaker_ignores_degraded_output(tmp_path):
    """内容不合格(DegradedOutput)不计入熔断——那可能只是这次提问的问题。"""
    fake = FakeProvider()
    for i in range(4):
        # 每次新的 FakeProvider 状态?用同一个:primary 每轮都回空,靠 fallback
        fake.configure("primary", text="")
        fake.configure("fallback", text="备用内容")
    spec = WorkflowSpec(
        name="breaker2",
        nodes=[NodeSpec(id="solo", type="llm", model="Fake:primary",
                        fallback=["Fake:fallback"],
                        prompt="干点活", consumes=["task"])],
        edges=[EdgeSpec("solo", "END")],
        entry="solo",
    )
    for _ in range(4):
        run = execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                            registry=make_registry(fake))
        assert run.events.find(type="breaker_open") is None
        # DegradedOutput 不开熔断,primary 每次都真被调用
