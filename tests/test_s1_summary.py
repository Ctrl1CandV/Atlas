# -*- coding: utf-8 -*-
"""S1 · 执行终局可视化与总结节点。

合同(ROADMAP §6b,2026-08-23 用户定案):
① 零成本终局视图:纯事件账本派生(build_finale),无 LLM 也能渲染,
   Web 终局卡片与 MCP atlas_get_run 同源;
② opt-in 总结节点:图级 summary: {model, prompt_hint?},run_done 前一次
   总结调用;write-once 产物 + run_summary_written;失败记
   run_summary_failed 且不改 run 终态;成本走 CostLedger 受 max_cost_usd
   约束;dry-run 明示「将执行总结」;内容标注「LLM 叙述,事实以账本为准」。
"""
import hashlib
import threading
import time

import pytest

from atlas import mcp as m
from atlas.adapters import FakeProvider
from atlas.engine import execute_graph, write_cancel_request
from atlas.events import EventReader, fold_events
from atlas.runs import build_run_summary
from atlas.spec import (SpecError, spec_fingerprint, spec_from_snapshot,
                        spec_from_yaml, spec_to_snapshot)

from conftest import TASK_TEXT, good_review_text, load_graph, make_registry, standard_fake

_NODE_ONLY_YAML = """
name: summary_demo
nodes:
  - id: node_a
    type: llm
    model: Fake:primary
    prompt: 分析任务材料,输出含 summary 的 JSON。
    consumes: [task]
    output_schema:
      required: [summary]
edges:
  - from: node_a
    to: END
"""

_SUMMARY_YAML = _NODE_ONLY_YAML + """summary:
  model: Fake:other
  prompt_hint: 两句话以内,先结论后过程
"""


class _GatedFake(FakeProvider):
    """指定模型的调用阻塞到放行——制造确定性的在途窗口。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.gates: dict[str, threading.Event] = {}
        self.prompts: list[tuple[str, str]] = []   # (model_id, prompt 原文)

    def gate(self, model_id: str) -> threading.Event:
        event = threading.Event()
        self.gates[model_id] = event
        return event

    def call(self, model_id: str, prompt: str, *args, **kwargs):
        self.prompts.append((model_id, prompt))
        gate = self.gates.get(model_id)
        if gate is not None:
            assert gate.wait(timeout=15), f"{model_id} 的门没有放行"
        return super().call(model_id, prompt, *args, **kwargs)


def _gated_from_template() -> _GatedFake:
    """以 standard_fake 的合格输出规格为底的门控 fake(截断哨兵不误触)。"""
    template = standard_fake()
    fake = _GatedFake(max_output_tokens=template.max_output_tokens)
    fake.models.update(template.models)
    return fake


def _wait_single_run_dir(runs_root, timeout_s: float = 10.0):
    """轮询等待 runs_root 下出现唯一 run 目录(排除 .locks/.trash)。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        dirs = [d for d in runs_root.iterdir()
                if d.is_dir() and not d.name.startswith(".")]
        if len(dirs) == 1:
            return dirs[0]
        time.sleep(0.02)
    raise AssertionError(
        f"runs_root 下没有出现唯一的 run 目录:{list(runs_root.iterdir())}")


# ─────────────────────────── spec 解析与身份 ───────────────────────────


def test_summary_spec_parsing_validation_and_identity():
    spec = spec_from_yaml(_SUMMARY_YAML)
    assert spec.summary is not None
    assert spec.summary.model == "Fake:other"
    assert spec.summary.prompt_hint.startswith("两句话")

    bare = spec_from_yaml(_NODE_ONLY_YAML)
    assert bare.summary is None
    # summary 是语义字段:配置后指纹必须不同;缺省时与旧版指纹一致
    assert spec_fingerprint(spec) != spec_fingerprint(bare)

    # 快照往返(批恢复/续跑的依据)
    restored = spec_from_snapshot(spec_to_snapshot(spec))
    assert restored.summary == spec.summary
    # 旧快照没有 summary 键也能恢复(等于未配置)
    legacy = spec_to_snapshot(bare)
    legacy.pop("summary")
    assert spec_from_snapshot(legacy).summary is None

    with pytest.raises(SpecError, match="未知字段"):
        spec_from_yaml(_SUMMARY_YAML + "  extra: 1\n")
    with pytest.raises(SpecError, match="summary.model"):
        spec_from_yaml(_NODE_ONLY_YAML + "summary:\n  model: ''\n")
    with pytest.raises(SpecError, match="顶层有未知字段"):
        spec_from_yaml(_NODE_ONLY_YAML + "summarize:\n  model: Fake:other\n")


def test_summary_model_resolved_before_spending(tmp_path):
    """总结模型与节点模型同一预检口径:坏引用在创建 run 前拒绝。"""
    fake = standard_fake()          # 只注册 primary/fallback/other
    spec = spec_from_yaml(_SUMMARY_YAML.replace("Fake:other", "Fake:missing"))
    with pytest.raises(Exception):
        execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                      registry=make_registry(fake))
    assert not list(tmp_path.iterdir()) or not [
        d for d in tmp_path.iterdir() if not d.name.startswith(".")]


# ─────────────────────────── 零成本终局视图 ───────────────────────────


def test_zero_cost_finale_renders_without_llm(tmp_path):
    """无 summary 配置的图,终局视图纯账本派生:每节点回顾 + 时间线 + 成本。"""
    result = execute_graph(load_graph("two_node"), task=TASK_TEXT,
                           runs_root=tmp_path, registry=make_registry(standard_fake()))
    summary = build_run_summary(result.dir.name, runs_root=tmp_path)
    finale = summary["finale"]
    assert finale is not None and finale["status"] == "done"
    assert [n["node"] for n in finale["nodes"]] == ["node_a", "node_b"]
    for n in finale["nodes"]:
        assert n["model_used"] and n["duration_s"] is not None
        assert n["recap"]          # 输出首段回顾可读
        assert n["ts"]
    assert finale["started_ts"] and finale["finished_ts"]
    assert finale["llm_summary"] is None and finale["llm_summary_error"] is None


def test_finale_absent_while_running_and_present_on_failure(tmp_path):
    """未到终态返回 None;failed/cancelled 也给终局视图。"""
    fake = standard_fake()
    fake.models["primary"].transport_error = "总失败"
    fake.models["fallback"].transport_error = "总失败"
    with pytest.raises(Exception):
        execute_graph(load_graph("two_node"), task=TASK_TEXT,
                      runs_root=tmp_path, registry=make_registry(fake))
    run_dir = next(d for d in tmp_path.iterdir()
                   if d.is_dir() and not d.name.startswith("."))
    summary = build_run_summary(run_dir.name, runs_root=tmp_path)
    assert summary["finale"]["status"] == "failed"
    assert summary["finale"]["nodes"] == []       # 没有节点完成,视图如实为空


# ─────────────────────────── 总结节点三路径 ───────────────────────────


def test_summary_success_writes_artifact_event_and_cost(tmp_path):
    """成功路径:账本派生的回顾 prompt(含 prompt_hint)、write-once 产物、
    run_summary_written、成本结算、P9 心跳覆盖总结窗口。"""
    fake = _gated_from_template()
    gate = fake.gate("other")      # 总结调用冻结,制造心跳窗口

    def _run() -> None:
        execute_graph(spec_from_yaml(_SUMMARY_YAML), task=TASK_TEXT,
                      runs_root=tmp_path, registry=make_registry(fake),
                      heartbeat_interval_s=0.05)

    thread = threading.Thread(target=_run)
    thread.start()
    run_dir = _wait_single_run_dir(tmp_path)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        path = run_dir / "events.jsonl"
        if path.exists() and EventReader(path).find(
                type="node_done", node="node_a"):
            break
        time.sleep(0.02)
    # 等总结窗口的心跳出现再放门——否则窗口可能短于一个心跳周期
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        beats = EventReader(run_dir / "events.jsonl").filter(
            type="node_progress", node="run_summary")
        if beats:
            break
        time.sleep(0.02)
    gate.set()
    thread.join(timeout=15)
    assert not thread.is_alive()

    events = EventReader(run_dir / "events.jsonl")
    written = events.find(type="run_summary_written")
    assert written is not None
    assert written["model"] == "Fake:other"
    # 产物可复验:事件里的 sha256 与磁盘字节一致
    artifact_bytes = (run_dir / "artifacts" /
                      written["path"].replace("\\", "/").split("/")[-1]).read_bytes()
    assert hashlib.sha256(artifact_bytes).hexdigest() == written["sha256"]
    # prompt 由账本派生:节点回顾 + 用户补充要求都进了总结调用
    summary_prompt = next(p for mid, p in fake.prompts if mid == "other")
    assert "node_a(Fake:primary" in summary_prompt
    assert "用户补充要求:两句话以内" in summary_prompt
    # 成本入账:总结调用有自己的预留与结算(node=run_summary)
    assert events.find(type="cost_settled", node="run_summary") is not None
    # P9 心跳覆盖总结派发窗口
    beats = events.filter(type="node_progress", node="run_summary")
    assert beats and all(b["model"] == "Fake:other" for b in beats)
    # 终态与终局视图
    assert fold_events(events.all())["status"] == "done"
    summary = build_run_summary(run_dir.name, runs_root=tmp_path)
    llm = summary["finale"]["llm_summary"]
    assert llm is not None
    assert llm["note"] == "LLM 叙述,事实以账本为准"
    assert llm["text"] == good_review_text()       # 总结原文(4k 截断内)


def test_summary_failure_keeps_terminal_done(tmp_path):
    """失败路径:总结调用坏了只记 run_summary_failed,run 终态不变。"""
    fake = standard_fake()
    fake.models["other"].transport_error = "总结端点坏了"
    result = execute_graph(spec_from_yaml(_SUMMARY_YAML), task=TASK_TEXT,
                           runs_root=tmp_path, registry=make_registry(fake))
    events = EventReader(result.dir / "events.jsonl")
    failed = events.find(type="run_summary_failed")
    assert failed is not None
    assert failed["error_type"] == "TransportError"
    assert events.find(type="run_done") is not None
    assert fold_events(events.all())["status"] == "done"
    summary = build_run_summary(result.dir.name, runs_root=tmp_path)
    assert summary["finale"]["llm_summary"] is None
    assert summary["finale"]["llm_summary_error"]["error_type"] == "TransportError"
    # 事后重试路径 = 重跑工作流;账本如实记录这次没有总结产物
    assert events.find(type="run_summary_written") is None


def test_summary_budget_exhaustion_fails_soft(tmp_path):
    """预算耗尽:总结预留被 guards 拦下,记 run_summary_failed,不改终态。"""
    yaml_budget = _SUMMARY_YAML.replace(
        "summary:", "guards:\n  max_cost_usd: 0.001\nsummary:")
    fake = standard_fake()
    result = execute_graph(spec_from_yaml(yaml_budget), task=TASK_TEXT,
                           runs_root=tmp_path, registry=make_registry(fake))
    events = EventReader(result.dir / "events.jsonl")
    assert events.find(type="run_summary_failed") is not None
    assert fold_events(events.all())["status"] == "done"
    # 未知费率 + 成本帽:首个计费节点保守占满预算(已知语义),总结被拦


def test_cancel_before_summary_skips_it(tmp_path):
    """取消消费点覆盖总结调用:请求在途时不总结,直落 run_cancelled。"""
    fake = _gated_from_template()
    gate = fake.gate("primary")    # 冻结节点调用,窗口里下取消

    def _run() -> None:
        execute_graph(spec_from_yaml(_SUMMARY_YAML), task=TASK_TEXT,
                      runs_root=tmp_path, registry=make_registry(fake))

    thread = threading.Thread(target=_run)
    thread.start()
    run_dir = _wait_single_run_dir(tmp_path)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if (run_dir / "events.jsonl").exists() and EventReader(
                run_dir / "events.jsonl").find(type="node_started"):
            break
        time.sleep(0.02)
    write_cancel_request(run_dir, reason="总结前取消")
    gate.set()
    thread.join(timeout=15)
    assert not thread.is_alive()
    events = EventReader(run_dir / "events.jsonl")
    assert events.find(type="run_cancelled") is not None
    assert events.find(type="run_summary_written") is None
    assert fold_events(events.all())["status"] == "cancelled"


def test_fold_regression_stripping_summary_events_changes_nothing(tmp_path):
    """回归锁:删掉 run_summary_written/failed 后 fold 结果逐字段一致。"""
    fake = standard_fake()
    result = execute_graph(spec_from_yaml(_SUMMARY_YAML), task=TASK_TEXT,
                           runs_root=tmp_path, registry=make_registry(fake))
    records = EventReader(result.dir / "events.jsonl").all()
    stripped = [r for r in records
                if r["type"] not in ("run_summary_written", "run_summary_failed")]
    assert len(stripped) < len(records)
    assert fold_events(records) == fold_events(stripped)


# ─────────────────────────── 表面:dry-run 与 Web ───────────────────────────


def test_dry_run_lists_summary_call(tmp_path):
    """dry-run 明示「将执行总结(模型 X,预估 1 次调用)」。"""
    fake = standard_fake()
    out = m.dry_run_impl("", TASK_TEXT,
                         registry_factory=lambda pids: make_registry(fake),
                         yaml=_SUMMARY_YAML)
    assert out["summary"] is not None
    assert out["summary"]["model"] == "Fake:other"
    assert "1 次总结调用" in out["summary"]["note"]
    assert not list(tmp_path.iterdir())    # dry-run 不建 run 目录


def test_dry_run_warnings_cover_summary_model():
    """有成本帽且总结模型费率未知:警告口径覆盖总结候选。"""
    spec = spec_from_yaml(_SUMMARY_YAML + "guards:\n  max_cost_usd: 1\n")
    warnings = m._dry_run_warnings(
        spec, provider_cfgs={}, capabilities={}, rates_known_fn=lambda ref: False)
    assert any("run 总结的候选 Fake:other" in w for w in warnings)


def test_web_get_run_serves_same_finale(tmp_path):
    """Web /api/runs/{id} 与 MCP atlas_get_run 同源(build_finale)。"""
    from atlas.web import create_app
    from fastapi.testclient import TestClient

    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "demo.yaml").write_text(_SUMMARY_YAML.lstrip(), encoding="utf-8")
    app = create_app(workflows_dir=workflows, runs_dir=tmp_path / "runs",
                     registry_factory=lambda pids: make_registry(standard_fake()),
                     api_only=True)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        resp = client.post("/api/workflows/demo/run",
                           json={"task": "Web 终局测试任务"},
                           headers={"X-Atlas-Request": "1"})
        assert resp.status_code == 200
        run_id = resp.json()["run_id"]
        for _ in range(50):
            summary = client.get(f"/api/runs/{run_id}").json()
            if summary["status"] in ("done", "failed"):
                break
            time.sleep(0.1)
        assert summary["status"] == "done", summary.get("failed_error")
        finale = summary["finale"]
        assert finale["status"] == "done"
        assert finale["nodes"][0]["node"] == "node_a"
        assert finale["llm_summary"]["model"] == "Fake:other"
        assert finale["llm_summary"]["note"] == "LLM 叙述,事实以账本为准"
