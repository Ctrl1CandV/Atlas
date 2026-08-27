# -*- coding: utf-8 -*-
"""REV-001 PreparedExecution：执行规格与后端身份在花钱/落盘前冻结。"""
import json
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from atlas import mcp as mcp_module
from atlas.adapters import (AdapterRegistry, ConfigError, FakeProvider,
                            OpenAICompatAdapter)
from atlas.engine import (_resume_graph_replay, approve_run, execute_graph,
                          prepare_execution, resume_graph)
from atlas.events import EventReader
from atlas.spec import SpecError, spec_from_yaml
from atlas.web import create_app

from conftest import TASK_TEXT, load_graph, make_registry


SIMPLE_YAML = """
name: prepared-demo
nodes:
  - id: only
    type: llm
    model: Fake:primary
    prompt: 执行。
    consumes: [task]
edges:
  - from: only
    to: END
"""


def _registry(fake: FakeProvider, provider: str = "Fake") -> AdapterRegistry:
    registry = AdapterRegistry()
    fake.register_into(registry, provider)
    return registry


def _successful_fake(protocol: str = "openai") -> FakeProvider:
    fake = FakeProvider()
    fake.protocol = protocol
    fake.configure("primary", text="完成")
    fake.configure("other", text="终稿")
    fake.configure("third", text="终审")
    return fake


def test_registry_descriptor_is_nonsecret_stable_and_order_sensitive():
    # 生产 adapter 的描述器只含显式非秘密字段；不通过 SDK 构造器发起任何连接。
    adapter = OpenAICompatAdapter.__new__(OpenAICompatAdapter)
    adapter.provider_id = "P"
    adapter.base_url = "https://gateway.invalid/v1"
    adapter.default_timeout_s = 123.0
    adapter.credential_ref = "P_API_KEY"
    adapter.credential_revision = "revision-placeholder"
    adapter._max_output_tokens = 4096
    adapter._client = object()
    descriptor_text = json.dumps(adapter.execution_descriptor(), sort_keys=True)
    assert "https://gateway.invalid/v1" in descriptor_text
    assert "P_API_KEY" in descriptor_text
    assert "_client" not in descriptor_text and "api_key" not in descriptor_text

    class SecretFake(FakeProvider):
        def __init__(self, secret: str, response: str):
            super().__init__(max_output_tokens=2048)
            self.secret = secret
            self.configure("m", text=response)

        def __repr__(self):
            return f"SECRET:{self.secret}"

    a1 = SecretFake("key-one", "response-one")
    a2 = SecretFake("key-two", "response-two")
    r1, r2 = AdapterRegistry(), AdapterRegistry()
    r1.register("B", ["m"], a1)
    r1.register("A", ["m"], a1)
    r2.register("A", ["m"], a2)
    r2.register("B", ["m"], a2)
    assert r1.fingerprint() == r2.fingerprint()
    rendered = json.dumps(r1.execution_descriptor())
    assert "key-one" not in rendered and "response-one" not in rendered

    openai, anthropic = SecretFake("x", "x"), SecretFake("y", "y")
    openai.protocol, anthropic.protocol = "openai", "anthropic"
    first, second = AdapterRegistry(), AdapterRegistry()
    first.register("P", ["m"], openai)
    first.register("P", ["m"], anthropic)
    second.register("P", ["m"], anthropic)
    second.register("P", ["m"], openai)
    assert first.fingerprint() != second.fingerprint()

    changed = SecretFake("different-secret", "different-response")
    changed.protocol = "different-protocol"
    r3 = AdapterRegistry()
    r3.register("A", ["m"], changed)
    assert r3.fingerprint() != r2.fingerprint()


def test_prepared_rejects_mutated_backend_and_partial_modern_identity(tmp_path):
    spec = spec_from_yaml(SIMPLE_YAML)
    fake = _successful_fake()
    prepared = prepare_execution(spec, _registry(fake))
    fake.protocol = "mutated-after-prepare"
    with pytest.raises(SpecError, match="后端对象"):
        execute_graph(spec, task="x", runs_root=tmp_path / "mutated",
                      prepared=prepared)
    assert not (tmp_path / "mutated").exists()

    stable = prepare_execution(spec, _registry(_successful_fake()))
    run = execute_graph(spec, task="x", runs_root=tmp_path / "partial",
                        prepared=stable)
    path = run.dir / "events.jsonl"
    rows = path.read_text(encoding="utf-8").splitlines()
    started = json.loads(rows[0])
    started.pop("execution_sha256")
    rows[0] = json.dumps(started, ensure_ascii=False)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    with pytest.raises(SpecError, match="身份字段不完整"):
        _resume_graph_replay(run.run_id, _test_only=True,
                         spec=spec, runs_root=tmp_path / "partial",
                     prepared=stable)


def test_registry_freeze_rejects_register_and_keeps_default_cap():
    fake = FakeProvider(max_output_tokens=1234)
    fake.configure("m", text="ok")
    registry = _registry(fake)
    assert registry.default_max_output_tokens("Fake:m") == 1234
    registry.freeze()
    with pytest.raises(ConfigError, match="冻结"):
        registry.register("Other", ["m"], fake)


def test_llm_credential_rotation_changes_execution_identity():
    from atlas.adapters import AnthropicCompatAdapter, OpenAICompatAdapter

    def registry_with(adapter) -> AdapterRegistry:
        registry = AdapterRegistry()
        registry.register("P", ["m"], adapter)
        return registry

    old = OpenAICompatAdapter("P", "https://gateway.invalid/v1", "key-one",
                              credential_ref="P_API_KEY")
    rotated = OpenAICompatAdapter("P", "https://gateway.invalid/v1", "key-two",
                                  credential_ref="P_API_KEY")
    assert registry_with(old).fingerprint() != registry_with(rotated).fingerprint()
    rendered = json.dumps(registry_with(old).execution_descriptor())
    assert "key-one" not in rendered and "key-two" not in rendered

    old_anthropic = AnthropicCompatAdapter(
        "P", "https://anthropic.invalid", "key-one", credential_ref="P_API_KEY")
    rotated_anthropic = AnthropicCompatAdapter(
        "P", "https://anthropic.invalid", "key-two", credential_ref="P_API_KEY")
    assert (registry_with(old_anthropic).fingerprint()
            != registry_with(rotated_anthropic).fingerprint())
    rendered_anthropic = json.dumps(
        registry_with(old_anthropic).execution_descriptor())
    assert "key-one" not in rendered_anthropic

    spec = spec_from_yaml(SIMPLE_YAML.replace("Fake:primary", "P:m"))
    before = prepare_execution(spec, registry_with(old),
                               agent_runner=lambda *a, **k: None)
    after = prepare_execution(spec, registry_with(rotated),
                              agent_runner=lambda *a, **k: None)
    assert before.execution_sha256 != after.execution_sha256


def test_prepared_rejects_backend_object_mutation_before_disk(tmp_path):
    spec = spec_from_yaml(SIMPLE_YAML)
    fake = _successful_fake("openai")
    prepared = prepare_execution(spec, _registry(fake))
    fake.protocol = "anthropic"

    runs = tmp_path / "runs"
    with pytest.raises(SpecError, match="后端对象.*变化"):
        execute_graph(spec, task="x", runs_root=runs, prepared=prepared)
    assert not runs.exists()


def test_modern_run_with_partial_identity_is_not_treated_as_legacy(tmp_path):
    spec = load_graph("human_gate")
    prepared = prepare_execution(spec, _registry(_successful_fake()))
    run = execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path, prepared=prepared)
    path = run.dir / "events.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    started = json.loads(lines[0])
    started.pop("execution_sha256")
    lines[0] = json.dumps(started, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(SpecError, match="身份字段不完整"):
        approve_run(run.run_id, decision="approve", comment="", spec=spec,
                    runs_root=tmp_path, prepared=prepared)
    events = EventReader(path).all()
    assert not any(e["type"] in {"legacy_execution_identity", "run_approval"}
                   for e in events)


def test_prepared_execution_skips_duplicate_preflight_and_rejects_spec_before_disk(
        tmp_path, monkeypatch):
    import atlas.engine as engine

    spec = spec_from_yaml(SIMPLE_YAML)
    fake = _successful_fake()
    calls = 0
    original = engine.validate_executable_spec

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "validate_executable_spec", counted)
    prepared = prepare_execution(spec, _registry(fake), agent_runner=lambda *a, **k: None)
    assert calls == 1
    run = execute_graph(spec, task="x", runs_root=tmp_path / "runs",
                        prepared=prepared)
    assert run.status == "done" and calls == 1
    started = run.events.find(type="run_started")
    assert started["prepared_execution_version"] == prepared.version
    assert started["backend_sha256"] == prepared.backend_sha256
    assert started["execution_sha256"] == prepared.execution_sha256

    modified = replace(spec, nodes=[replace(spec.nodes[0], prompt="改过")])
    untouched = tmp_path / "untouched"
    with pytest.raises(SpecError, match="PreparedExecution"):
        execute_graph(modified, task="x", runs_root=untouched, prepared=prepared)
    assert not untouched.exists()


def test_invalid_writable_allowed_paths_rejected_before_run_artifacts(tmp_path):
    base = spec_from_yaml(SIMPLE_YAML)
    extra = tmp_path / "extra"
    extra.mkdir()
    invalid = replace(base, nodes=[replace(
        base.nodes[0], type="coding_agent", workdir=str(tmp_path),
        writable=True, allowed_paths=[str(extra)])])
    untouched = tmp_path / "runs"

    with pytest.raises(SpecError, match="writable.*allowed_paths"):
        execute_graph(
            invalid, task="x", runs_root=untouched,
            registry=_registry(_successful_fake()),
            agent_runner=lambda *args, **kwargs: "unused",
        )

    assert not untouched.exists()


def test_web_preview_expected_execution_identity_rejects_before_run_artifacts(tmp_path):
    workflows, runs = tmp_path / "workflows", tmp_path / "runs"
    workflows.mkdir()
    (workflows / "demo.yaml").write_text(SIMPLE_YAML, encoding="utf-8")
    fake = _successful_fake()
    app = create_app(workflows_dir=workflows, runs_dir=runs,
                     registry_factory=lambda _: _registry(fake), api_only=True)
    headers = {"X-Atlas-Request": "1"}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        preview = client.post("/api/workflows/demo/preview", headers=headers,
                              json={"node_overrides": {}})
        assert preview.status_code == 200
        execution_sha256 = preview.json()["execution_sha256"]
        assert len(execution_sha256) == 64

        rejected = client.post("/api/workflows/demo/run", headers=headers, json={
            "task": "x", "node_overrides": {},
            "expected_execution_sha256": "0" * 64,
        })
        assert rejected.status_code == 409
        assert not runs.exists()

        started = client.post("/api/workflows/demo/run", headers=headers, json={
            "task": "x", "node_overrides": {},
            "expected_execution_sha256": execution_sha256,
        })
        assert started.status_code == 200
        run_id = started.json()["run_id"]
        event = None
        for _ in range(50):
            path = runs / run_id / "events.jsonl"
            if path.exists():
                event = EventReader(path).find(type="run_started")
                if event is not None:
                    break
            time.sleep(0.02)
    assert event is not None
    assert event["execution_sha256"] == execution_sha256


def test_mcp_dry_run_and_run_use_same_execution_identity(tmp_path, monkeypatch):
    workflows, runs = tmp_path / "workflows", tmp_path / "runs"
    workflows.mkdir()
    (workflows / "demo.yaml").write_text(SIMPLE_YAML, encoding="utf-8")
    monkeypatch.setattr(mcp_module, "WORKFLOWS_DIR", workflows)
    monkeypatch.setattr(mcp_module, "RUNS_DIR", runs)
    factory = lambda _: _registry(_successful_fake())

    dry = mcp_module.dry_run_impl("demo", "x", registry_factory=factory)
    execution_sha256 = dry["execution_sha256"]
    assert len(execution_sha256) == 64
    rejected = mcp_module.run_workflow_impl(
        "demo", "x", registry_factory=factory,
        expected_execution_sha256="f" * 64)
    assert "零成本拒绝" in rejected["error"]
    assert not runs.exists()

    result = mcp_module.run_workflow_impl(
        "demo", "x", registry_factory=factory,
        expected_execution_sha256=execution_sha256)
    assert result["status"] == "done"
    started = EventReader(runs / result["run_id"] / "events.jsonl").find(
        type="run_started")
    assert started["execution_sha256"] == execution_sha256


def test_resume_rejects_backend_drift_without_resume_events(tmp_path):
    spec = load_graph("three_node")
    crashing = _successful_fake("openai")
    crashing.configure("third", transport_error="crash")
    prepared = prepare_execution(spec, _registry(crashing))
    with pytest.raises(Exception):
        execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path, prepared=prepared)
    run_dir = next(path.parent for path in tmp_path.glob("*/events.jsonl"))
    before = EventReader(run_dir / "events.jsonl").all()

    drifted = prepare_execution(spec, _registry(_successful_fake("anthropic")))
    with pytest.raises(SpecError, match="backend_sha256|execution_sha256"):
        _resume_graph_replay(run_dir.name, _test_only=True, spec=spec, runs_root=tmp_path, prepared=drifted)
    after = EventReader(run_dir / "events.jsonl").all()
    assert after == before
    assert not any(e["type"] == "run_resumed" for e in after)


def test_approve_rejects_backend_drift_without_approval_events(tmp_path):
    spec = load_graph("human_gate")
    prepared = prepare_execution(spec, _registry(_successful_fake("openai")))
    run = execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path, prepared=prepared)
    assert run.status == "paused"
    before = run.events.all()

    drifted = prepare_execution(spec, _registry(_successful_fake("anthropic")))
    with pytest.raises(SpecError, match="backend_sha256|execution_sha256"):
        approve_run(run.run_id, decision="approve", comment="", spec=spec,
                    runs_root=tmp_path, prepared=drifted)
    after = EventReader(run.dir / "events.jsonl").all()
    assert after == before
    assert not any(e["type"] in {"run_approval", "run_resumed"} for e in after)


def test_legacy_run_continues_spec_only_and_records_compatibility(tmp_path):
    spec = load_graph("three_node")
    fake = _successful_fake()
    fake.configure("third", transport_error="crash")
    prepared = prepare_execution(spec, _registry(fake))
    with pytest.raises(Exception):
        execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path, prepared=prepared)
    run_dir = next(path.parent for path in tmp_path.glob("*/events.jsonl"))
    path = run_dir / "events.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    started = json.loads(lines[0])
    for key in ("prepared_execution_version", "backend_sha256", "execution_sha256"):
        started.pop(key, None)
    lines[0] = json.dumps(started, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fake.configure("third", text="恢复")
    resumed = _resume_graph_replay(run_dir.name, _test_only=True, spec=spec, runs_root=tmp_path,
                           prepared=prepared)
    assert resumed.status == "done"
    assert resumed.events.find(type="legacy_execution_identity") is not None
    assert resumed.events.find(type="run_resumed") is not None
