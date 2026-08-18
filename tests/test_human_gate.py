# -*- coding: utf-8 -*-
"""human 节点(HITL):暂停—批准/驳回—继续。

interrupt 的「暂停—重启进程—恢复」语义由 scripts/interrupt_smoke.py
先行验证;这里测 Atlas 层的完整闭环。
"""
import json

import pytest

from atlas.adapters import FakeProvider
from atlas.engine import (
    HumanRejected,
    approve_run,
    execute_graph,
    prepare_execution,
)
from atlas.events import EventReader
from atlas.integrity import IntegrityError

from conftest import TASK_TEXT, load_graph, make_registry


def _fake():
    fake = FakeProvider()
    fake.configure("primary", text="方案:分两步走,先验证再推广。")
    fake.configure("other", text="终稿:方案已获人工批准,按此执行。")
    return fake


def _only_run_dir(tmp_path):
    return next(path.parent for path in tmp_path.glob("*/events.jsonl"))


def test_human_gate_pause_and_approve(tmp_path):
    fake = _fake()
    run = execute_graph(load_graph("human_gate"), task=TASK_TEXT,
                        runs_root=tmp_path, registry=make_registry(fake))
    assert run.status == "paused"
    assert run.folded()["status"] == "paused"

    # 暂停账本:proposer 完成,gate 有输入与开始、没有完成,finalizer 未动
    assert run.events.find(type="node_done", node="proposer") is not None
    assert run.events.find(type="node_input", node="gate") is not None
    assert run.events.find(type="node_done", node="gate") is None
    assert run.events.find(type="node_started", node="finalizer") is None
    paused = run.events.find(type="run_paused")
    assert paused["node"] == "gate"

    before_sha = run.events.find(type="node_done", node="proposer")["output_sha256"]

    approved = approve_run(run.run_id, decision="approve", comment="同意",
                           spec=load_graph("human_gate"),
                           runs_root=tmp_path, registry=make_registry(fake))
    assert approved.status == "done"

    # 批准后:proposer 没有重跑(哈希不变、只完成一次),finalizer 跑完
    dones = approved.events.filter(type="node_done", node="proposer")
    assert len(dones) == 1 and dones[0]["output_sha256"] == before_sha
    assert approved.events.find(type="node_done", node="finalizer") is not None

    # 批准记录是产物,finalizer 的投影里包含它
    gate_done = approved.events.find(type="node_done", node="gate")
    assert "approve" in open(gate_done["output_path"], encoding="utf-8").read()
    fin_in = approved.events.find(type="node_input", node="finalizer")
    consumed = {c["name"] for c in fin_in["consumed"]}
    assert consumed == {"task", "proposer.output", "gate.output"}

    # 账本单调,含批复事件
    seqs = [e["seq"] for e in approved.events.all()]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
    assert approved.events.find(type="run_approval") is not None


def test_human_gate_reject_fails_loudly(tmp_path):
    fake = _fake()
    run = execute_graph(load_graph("human_gate"), task=TASK_TEXT,
                        runs_root=tmp_path, registry=make_registry(fake))
    assert run.status == "paused"

    with pytest.raises(HumanRejected) as e:
        approve_run(run.run_id, decision="reject", comment="方向不对",
                    spec=load_graph("human_gate"),
                    runs_root=tmp_path, registry=make_registry(fake))
    assert "方向不对" in str(e.value)

    reader = EventReader(_only_run_dir(tmp_path) / "events.jsonl")
    failed = reader.find(type="run_failed")
    assert failed["error_type"] == "HumanRejected"
    # 驳回后下游不许跑
    assert reader.find(type="node_started", node="finalizer") is None


def test_approve_rejects_modified_spec(tmp_path):
    from atlas.spec import SpecError

    fake = _fake()
    run = execute_graph(load_graph("human_gate"), task=TASK_TEXT,
                        runs_root=tmp_path, registry=make_registry(fake))
    from dataclasses import replace
    spec = load_graph("human_gate")
    modified = replace(spec, nodes=[
        replace(n, prompt=n.prompt + " (改)") if n.id == "finalizer" else n
        for n in spec.nodes])
    with pytest.raises(SpecError, match="spec_sha256"):
        approve_run(run.run_id, decision="approve", comment="",
                    spec=modified, runs_root=tmp_path,
                    registry=make_registry(fake))


def test_approve_requires_valid_decision(tmp_path):
    from atlas.spec import SpecError

    fake = _fake()
    run = execute_graph(load_graph("human_gate"), task=TASK_TEXT,
                        runs_root=tmp_path, registry=make_registry(fake))
    with pytest.raises(SpecError):
        approve_run(run.run_id, decision="maybe", comment="",
                    spec=load_graph("human_gate"),
                    runs_root=tmp_path, registry=make_registry(fake))


def test_approve_rejects_tampered_coding_diff_before_approval_event(tmp_path):
    import subprocess

    from atlas.spec import spec_from_yaml

    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "baseline"], cwd=project, check=True)

    spec = spec_from_yaml(f"""
name: coding-approval-integrity
nodes:
  - id: coder
    type: coding_agent
    model: Stub:agent
    prompt: 修改文件。
    consumes: [task]
    workdir: {project.as_posix()}
  - id: gate
    type: human
    prompt: 审阅报告和补丁。
    consumes: [task, coder.output, coder.diff]
edges:
  - from: coder
    to: gate
  - from: gate
    to: END
""")

    from atlas.nodes.local_cli import _require_clean_git_workdir

    class ProductionRunner:
        production_runner = True
        runner_name = "local_cli"

        def __init__(self):
            self.source_baseline_tokens = (
                _require_clean_git_workdir(project, "coder"),)

        def __call__(self, _attachment, *, cwd=None, **_kwargs):
            (cwd / "app.py").write_text("value = 2\n", encoding="utf-8")
            return "已修改 app.py"

    prepared = prepare_execution(
        spec, make_registry(FakeProvider()), agent_runner=ProductionRunner())
    run = execute_graph(
        spec, task=TASK_TEXT, runs_root=tmp_path / "runs", prepared=prepared)
    assert run.status == "paused"
    diff_done = run.events.find(type="node_done", node="coder")
    diff_artifact = next(
        artifact for artifact in diff_done["artifacts"]
        if artifact["role"] == "diff")
    metadata = diff_artifact["metadata"]
    gate_input = run.events.find(type="node_input", node="gate")
    projection = open(gate_input["projection_path"], encoding="utf-8").read()
    for key in ("baseline_digest", "result_digest", "patch_digest"):
        assert key in projection
        assert metadata[key] in projection

    diff = run.artifacts["coder.diff"].path
    original_patch = diff.read_bytes()
    diff.write_text("tampered patch\n", encoding="utf-8")
    before = run.events.all()

    with pytest.raises(IntegrityError, match="哈希不符"):
        approve_run(
            run.run_id, decision="approve", comment="",
            spec=spec, runs_root=tmp_path / "runs", prepared=prepared)

    after = EventReader(run.dir / "events.jsonl").all()
    assert after == before
    assert not any(event["type"] in {"run_approval", "run_resumed"}
                   for event in after)

    diff.write_bytes(original_patch)
    approved = approve_run(
        run.run_id, decision="approve", comment="evidence checked",
        spec=spec, runs_root=tmp_path / "runs", prepared=prepared)
    approval = approved.events.find(type="run_approval")
    assert approval["approved_projection_sha256"] == gate_input["projection_sha256"]
    assert approval["approved_diffs"] == [{
        "name": "coder.diff",
        "artifact_sha256": diff_artifact["sha256"],
        "baseline_digest": metadata["baseline_digest"],
        "result_digest": metadata["result_digest"],
        "patch_digest": metadata["patch_digest"],
    }]


def _pause_coding_run(tmp_path):
    import subprocess

    from atlas.nodes.local_cli import _require_clean_git_workdir
    from atlas.spec import spec_from_yaml

    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "baseline"], cwd=project, check=True)

    spec = spec_from_yaml(f"""
name: coding-metadata-forgery
nodes:
  - id: coder
    type: coding_agent
    model: Stub:agent
    prompt: 修改文件。
    consumes: [task]
    workdir: {project.as_posix()}
  - id: gate
    type: human
    prompt: 审阅报告和补丁。
    consumes: [task, coder.output, coder.diff]
edges:
  - from: coder
    to: gate
  - from: gate
    to: END
""")

    class ProductionRunner:
        production_runner = True
        runner_name = "local_cli"

        def __init__(self):
            self.source_baseline_tokens = (
                _require_clean_git_workdir(project, "coder"),)

        def __call__(self, _attachment, *, cwd=None, **_kwargs):
            (cwd / "app.py").write_text("value = 2\n", encoding="utf-8")
            return "已修改 app.py"

    prepared = prepare_execution(
        spec, make_registry(FakeProvider()), agent_runner=ProductionRunner())
    run = execute_graph(
        spec, task=TASK_TEXT, runs_root=tmp_path / "runs", prepared=prepared)
    assert run.status == "paused"
    return spec, prepared, run


def test_approve_rejects_forged_metadata_digests_against_projection(tmp_path):
    """暂停后只改账本 metadata 的 baseline/result 摘要 → 与哈希锚定投影不符,拒绝。"""
    spec, prepared, run = _pause_coding_run(tmp_path)
    events_path = run.dir / "events.jsonl"
    lines = events_path.read_text(encoding="utf-8").splitlines()
    forged = []
    for line in lines:
        event = json.loads(line)
        if event.get("type") == "node_done" and event.get("node") == "coder":
            diff_entry = next(item for item in event["artifacts"]
                              if item.get("role") == "diff")
            diff_entry["metadata"]["baseline_digest"] = "f" * 64
            diff_entry["metadata"]["result_digest"] = "e" * 64
            forged.append(json.dumps(event, ensure_ascii=False))
        else:
            forged.append(line)
    events_path.write_text("\n".join(forged) + "\n", encoding="utf-8")
    before = EventReader(events_path).all()

    with pytest.raises(IntegrityError, match="投影中的证据不符"):
        approve_run(run.run_id, decision="approve", comment="",
                    spec=spec, runs_root=tmp_path / "runs", prepared=prepared)

    after = EventReader(events_path).all()
    assert after == before
    assert not any(event["type"] in {"run_approval", "run_resumed"}
                   for event in after)


def _tamper_coder_done(events_path, mutate):
    lines = events_path.read_text(encoding="utf-8").splitlines()
    rewritten = []
    for line in lines:
        event = json.loads(line)
        if event.get("type") == "node_done" and event.get("node") == "coder":
            mutate(event)
            rewritten.append(json.dumps(event, ensure_ascii=False))
        else:
            rewritten.append(line)
    events_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


def test_approve_rejects_role_downgrade_of_diff_entry(tmp_path):
    """变体 D:把账本 diff 条目 role 降级,跳过投影证据校验 → 拒绝。"""
    spec, prepared, run = _pause_coding_run(tmp_path)
    events_path = run.dir / "events.jsonl"

    def downgrade(event):
        for item in event["artifacts"]:
            if item.get("role") == "diff":
                item["role"] = ""

    _tamper_coder_done(events_path, downgrade)

    with pytest.raises(IntegrityError, match="role 被降级|与审批投影证据不一致"):
        approve_run(run.run_id, decision="approve", comment="",
                    spec=spec, runs_root=tmp_path / "runs", prepared=prepared)
    after = EventReader(events_path).all()
    assert not any(event["type"] in {"run_approval", "run_resumed"}
                   for event in after)


def test_approve_rejects_forged_diff_entry_sha256(tmp_path):
    """变体 G:伪造账本条目 sha256,让投影证据匹配落空 → 拒绝。"""
    spec, prepared, run = _pause_coding_run(tmp_path)
    events_path = run.dir / "events.jsonl"

    def forge_sha(event):
        for item in event["artifacts"]:
            if item.get("role") == "diff":
                item["sha256"] = "0" * 64

    _tamper_coder_done(events_path, forge_sha)

    with pytest.raises(IntegrityError, match="role 被降级|与审批投影证据不一致"):
        approve_run(run.run_id, decision="approve", comment="",
                    spec=spec, runs_root=tmp_path / "runs", prepared=prepared)
    after = EventReader(events_path).all()
    assert not any(event["type"] in {"run_approval", "run_resumed"}
                   for event in after)
