# -*- coding: utf-8 -*-
"""P11 · request_changes 与 routed approval。

合同(ROADMAP §10):
① 旧图兼容:binary 默认、指纹零变化,approve/reject 行为照旧;
② 三分支 decision 共用同一 _verify_approval_material 锁内链,binary 图
   对 request_changes 在写事件前拒绝;
③ request_changes 必填非空 comment(engine/web/mcp 同一领域函数),
   经保留键 __changes__ 回边返回修订节点,消耗 guards.max_iterations;
④ 篡改矩阵:投影字节篡改对三种 decision 都在落账前拒绝;
⑤ route_facts 通道互相覆写,request_changes 回边不污染后续审批路由。
"""
import json

import pytest

from atlas.adapters import FakeProvider
from atlas.engine import (HumanRejected, approve_run, execute_graph,
                          prepare_execution, validate_approval_decision)
from atlas.events import EventReader, fold_events
from atlas.spec import (APPROVAL_DECISIONS, SpecError, spec_fingerprint,
                        spec_from_snapshot, spec_from_yaml)

from conftest import TASK_TEXT, make_registry

# 生产者 a → human(routed) --__changes__--> rev --> a;approve 时 h → END
ROUTED_YAML = """
name: p11_routed
entry: a
guards:
  max_iterations: 4
nodes:
  - id: a
    type: llm
    model: Fake:primary
    prompt: 产出草案。
    consumes: [task]
    output_schema:
      required: [summary]
  - id: h
    type: human
    prompt: 审批草案。
    consumes: [a.output]
    approval_mode: routed
  - id: rev
    type: llm
    model: Fake:other
    prompt: 按意见修订。
    consumes: [task, a.output, h.changes]
edges:
  - from: a
    to: h
  - from: h
    to: END
  - from: h
    to: rev
    when: __changes__
  - from: rev
    to: a
"""

BINARY_YAML = """
name: p11_binary
entry: a
guards:
  max_iterations: 4
nodes:
  - id: a
    type: llm
    model: Fake:primary
    prompt: 产出草案。
    consumes: [task]
    output_schema:
      required: [summary]
  - id: h
    type: human
    prompt: 审批草案。
    consumes: [a.output]
  - id: rev
    type: llm
    model: Fake:other
    prompt: 常规修订。
    consumes: [task, a.output]
edges:
  - from: a
    to: h
  - from: h
    to: END
  - from: h
    to: rev
"""


def _fake() -> FakeProvider:
    fake = FakeProvider()
    fake.configure("primary",
                   text=json.dumps({"summary": "草案"}, ensure_ascii=False))
    fake.configure("other",
                   text=json.dumps({"summary": "已修订"}, ensure_ascii=False))
    return fake


def _run_to_pause(yaml_text: str, runs_root) -> str:
    result = execute_graph(spec_from_yaml(yaml_text), task=TASK_TEXT,
                           runs_root=runs_root, registry=make_registry(_fake()))
    assert result.folded()["status"] == "paused"
    return result.run_id


def _approve(runs_root, run_id, yaml_text, decision, comment=""):
    spec = spec_from_yaml(yaml_text)
    prepared = prepare_execution(spec, make_registry(_fake()),
                                 agent_runner=None)
    return approve_run(run_id, decision=decision, comment=comment,
                       spec=spec, runs_root=runs_root, prepared=prepared)


def _events_of(runs_root, run_id):
    return EventReader(runs_root / run_id / "events.jsonl").all()


# ───────────── ① 旧图兼容 ─────────────


def test_explicit_binary_keeps_default_fingerprint():
    """缺省与显式 binary 同指纹;加 routed 才变化(P11 兼容锚点)。"""
    base = BINARY_YAML
    explicit = base.replace(
        "    consumes: [a.output]\n", 
        "    consumes: [a.output]\n    approval_mode: binary\n")
    assert spec_fingerprint(spec_from_yaml(explicit)) == \
        spec_fingerprint(spec_from_yaml(base))
    routed = base.replace(
        "    consumes: [a.output]\n",
        "    consumes: [a.output]\n    approval_mode: routed\n").replace(
        "  - from: h\n    to: END\n",
        "  - from: h\n    to: END\n  - from: h\n    to: rev\n"
        "    when: __changes__\n").replace(
        "consumes: [task, a.output]", "consumes: [task, a.output, h.changes]")
    assert spec_fingerprint(spec_from_yaml(routed)) != \
        spec_fingerprint(spec_from_yaml(base))


def test_binary_reject_still_terminal(tmp_path):
    """旧 reject 路径零变化:HumanRejected → failed 终态。"""
    run_id = _run_to_pause(BINARY_YAML, tmp_path)
    with pytest.raises(HumanRejected):
        _approve(tmp_path, run_id, BINARY_YAML, "reject", "不合格")
    assert fold_events(_events_of(tmp_path, run_id))["status"] == "failed"


def test_binary_graph_rejects_request_changes_before_emit(tmp_path):
    """binary 图收到 request_changes:锁内、写任何事件之前拒绝。"""
    run_id = _run_to_pause(BINARY_YAML, tmp_path)
    before = len([e for e in _events_of(tmp_path, run_id)
                  if e["type"] == "run_approval"])
    with pytest.raises(SpecError, match="binary"):
        _approve(tmp_path, run_id, BINARY_YAML, "request_changes", "改改")
    events = _events_of(tmp_path, run_id)
    approvals = [e for e in events if e["type"] == "run_approval"]
    assert len(approvals) == before == 0


# ───────────── ③ 领域校验(三分支共用同一函数) ─────────────


@pytest.mark.parametrize("decision", APPROVAL_DECISIONS)
def test_domain_validator_accepts_legal(decision):
    validate_approval_decision(decision, "" if decision != "request_changes"
                               else "请补充数据来源说明")


def test_domain_validator_rules():
    with pytest.raises(SpecError):
        validate_approval_decision("maybe", "")
    with pytest.raises(SpecError, match="非空"):
        validate_approval_decision("request_changes", "   ")
    # approve/reject 不强制意见(既有口径保持)
    validate_approval_decision("approve", "")
    validate_approval_decision("reject", "")


# ───────────── routed 全循环 ─────────────


def test_routed_full_cycle_request_changes_then_approve(tmp_path):
    """核心验收:request_changes → __changes__ 回边修订 → 再次暂停 →
    approve 完成 done;修改要求产物真实落盘且被消费;决策审计链完整。"""
    run_id = _run_to_pause(ROUTED_YAML, tmp_path)

    r2 = _approve(tmp_path, run_id, ROUTED_YAML, "request_changes",
                  "方法部分缺少数据来源说明")
    assert r2.folded()["status"] == "paused"      # 回边后再次等审批

    events = _events_of(tmp_path, run_id)
    decisions = [e["decision"] for e in events if e["type"] == "run_approval"]
    assert decisions == ["request_changes"]

    ledger_text = json.dumps(events, ensure_ascii=False)
    assert "h.changes" in ledger_text              # 变更产物有账本记录
    changes_done = [e for e in events if e.get("type") == "node_done"
                    and any(c.get("name") == "rev.output"
                            for c in (e.get("artifacts") or []))]
    assert changes_done, "修订节点必须真实执行"

    r3 = _approve(tmp_path, run_id, ROUTED_YAML, "approve")
    assert r3.folded()["status"] == "done"
    final_decisions = [e["decision"] for e in
                       _events_of(tmp_path, run_id)
                       if e["type"] == "run_approval"]
    assert final_decisions == ["request_changes", "approve"]


def test_routed_bounded_by_max_iterations(tmp_path):
    """反复 request_changes 消耗 max_iterations;超限治理失败终局。"""
    strict = ROUTED_YAML.replace("max_iterations: 4", "max_iterations: 2")
    run_id = _run_to_pause(strict, tmp_path)

    terminal_exc = None
    for i in range(5):
        try:
            result = _approve(tmp_path, run_id, strict, "request_changes",
                              f"还要再改第 {i + 1} 次")
            if result.folded()["status"] != "paused":
                break
        except Exception as exc:      # GuardViolation 以治理异常冒出
            terminal_exc = exc
            break
    folded = fold_events(_events_of(tmp_path, run_id))
    assert folded["status"] == "failed", "超限后必须到达失败终态"
    ledger_text = json.dumps(_events_of(tmp_path, run_id),
                             ensure_ascii=False)
    assert ("GuardViolation" in ledger_text) or (terminal_exc is not None)


# ───────────── ④ 篡改矩阵(三 decision 同一验证链) ─────────────


def _pause_then_tamper_projection(runs_root) -> str:
    run_id = _run_to_pause(ROUTED_YAML, runs_root)
    events = _events_of(runs_root, run_id)
    paused = next(e for e in reversed(events)
                  if e.get("type") == "run_paused")
    node_input = next(e for e in reversed(events)
                      if e.get("type") == "node_input"
                      and e.get("node") == paused["node"])
    rel = node_input["projection_path"].replace("\\", "/").split("/projections/")[-1]
    proj = runs_root / run_id / "projections" / rel
    data = bytearray(proj.read_bytes())
    data[len(data) // 2] ^= 0x20          # 翻转中间一字节
    proj.write_bytes(bytes(data))
    return run_id


@pytest.mark.parametrize("decision", ["approve", "reject", "request_changes"])
def test_tampered_projection_rejected_for_every_decision_pre_emit(
        tmp_path, decision):
    run_id = _pause_then_tamper_projection(tmp_path)
    before = len([e for e in _events_of(tmp_path, run_id)
                  if e["type"] == "run_approval"])
    comment = "意见" if decision == "request_changes" else ""
    with pytest.raises(Exception):
        _approve(tmp_path, run_id, ROUTED_YAML, decision, comment)
    approvals = [e for e in _events_of(tmp_path, run_id)
                 if e["type"] == "run_approval"]
    assert len(approvals) == before       # 验证先于持久化,零落账


# ───────────── Web API 同源 ─────────────


def test_web_approve_endpoint_uses_same_validator(tmp_path):
    """/api/runs/{rid}/approve 与 engine 共用领域函数:request_changes
    缺意见 → 400;binary 图 request_changes → 上游 SpecError 映射。"""
    from fastapi.testclient import TestClient
    from atlas.web import create_app

    run_id = _run_to_pause(ROUTED_YAML, tmp_path)
    api = create_app(workflows_dir=tmp_path / "workflows", runs_dir=tmp_path,
                     registry_factory=lambda _: make_registry(_fake()),
                     api_only=True)
    client = TestClient(api, base_url="http://127.0.0.1")

    missing = client.post(f"/api/runs/{run_id}/approve",
                          json={"decision": "request_changes", "comment": "  "},
                          headers={"X-Atlas-Request": "1"})
    assert missing.status_code == 400
    assert "非空" in missing.json()["detail"]
