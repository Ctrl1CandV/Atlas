# -*- coding: utf-8 -*-
"""有效运行规格：示例待配置语义、封闭覆盖、审计快照与历史显示。"""
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas import mcp as mcp_module
from atlas.adapters import FakeProvider
from atlas.effective import build_effective_spec, provider_ids_for_spec
from atlas.engine import execute_graph, validate_executable_spec
from atlas.spec import SpecError, spec_from_snapshot, spec_from_yaml
from atlas.web import create_app

from conftest import make_registry


EXAMPLE_YAML = """
name: portable-example
meta:
  kind: example
nodes:
  - id: analyst
    type: llm
    prompt: 分析。
    consumes: [task]
  - id: reviewer
    type: llm
    prompt: 审查。
    consumes: [analyst.output]
edges:
  - from: analyst
    to: reviewer
  - from: reviewer
    to: END
"""

CUSTOM_YAML = """
name: strict-custom
meta:
  kind: custom
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


def test_example_keeps_empty_models_and_reports_unconfigured_nodes():
    base = spec_from_yaml(EXAMPLE_YAML)
    effective = build_effective_spec(base)

    assert [node.model for node in effective.spec.nodes] == ["", ""]
    assert effective.unconfigured_nodes == ("analyst", "reviewer")
    assert effective.effective_fingerprint == effective.base_fingerprint
    assert [binding["source"] for binding in effective.bindings] == ["yaml", "yaml"]
    assert all(binding["model"] == "" for binding in effective.bindings)
    assert all("independence" not in binding for binding in effective.bindings)


def test_example_explicit_model_overrides_are_audited_and_executable():
    overrides = {
        "analyst": {"model": "Fake:analyst"},
        "reviewer": {"model": "Fake:reviewer", "temperature": 0.25},
    }
    effective = build_effective_spec(spec_from_yaml(EXAMPLE_YAML), overrides)

    assert effective.unconfigured_nodes == ()
    assert effective.spec.node("analyst").model == "Fake:analyst"
    assert effective.spec.node("reviewer").model == "Fake:reviewer"
    assert effective.spec.node("reviewer").temperature == 0.25
    assert all(binding["source"] == "override" for binding in effective.bindings)
    assert effective.overrides == (
        {"node": "analyst", "fields": {"model": "Fake:analyst"}},
        {"node": "reviewer", "fields": {
            "model": "Fake:reviewer", "temperature": 0.25}},
    )

    fake = FakeProvider()
    fake.configure("analyst", text="分析")
    fake.configure("reviewer", text="审查")
    validate_executable_spec(effective.spec, make_registry(fake))


def test_example_rejects_fallback_equal_to_primary():
    with pytest.raises(SpecError, match="fallback.*主模型"):
        build_effective_spec(
            spec_from_yaml(EXAMPLE_YAML),
            {"analyst": {"model": "Fake:analyst",
                         "fallback": ["Fake:analyst"]}},
        )


def test_example_never_silently_binds_available_models():
    effective = build_effective_spec(spec_from_yaml(EXAMPLE_YAML))

    assert provider_ids_for_spec(effective.spec) == []
    assert effective.public_summary()["unconfigured_nodes"] == [
        "analyst", "reviewer"]
    assert json.dumps(effective.public_summary(), ensure_ascii=False).count(
        "Fake:") == 0


def test_partial_example_override_keeps_other_nodes_unconfigured():
    effective = build_effective_spec(
        spec_from_yaml(EXAMPLE_YAML),
        {"analyst": {"model": "Fake:analyst"}},
    )

    assert effective.spec.node("analyst").model == "Fake:analyst"
    assert effective.spec.node("reviewer").model == ""
    assert effective.unconfigured_nodes == ("reviewer",)


def test_web_previews_unconfigured_example_and_rejects_before_run_id(tmp_path):
    workflows = tmp_path / "workflows"
    runs = tmp_path / "runs"
    workflows.mkdir()
    (workflows / "example.yaml").write_text(EXAMPLE_YAML, encoding="utf-8")
    registry_calls = []

    def factory(provider_ids):
        registry_calls.append(list(provider_ids))
        return make_registry(FakeProvider())

    app = create_app(workflows_dir=workflows, runs_dir=runs,
                     registry_factory=factory)
    headers = {"X-Atlas-Request": "1"}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        preview = client.post(
            "/api/workflows/example/preview", headers=headers,
            json={"node_overrides": {}})
        assert preview.status_code == 200
        assert preview.json()["unconfigured_nodes"] == ["analyst", "reviewer"]
        assert preview.json()["effective_workflow"]["nodes"][0]["model"] == ""

        rejected = client.post(
            "/api/workflows/example/run", headers=headers,
            json={"task": "x", "node_overrides": {}})
        assert rejected.status_code == 400
        assert "未配置模型" in rejected.json()["detail"]

    assert registry_calls == []
    assert not runs.exists() or not list(runs.iterdir())


def test_mcp_dry_run_shows_unconfigured_and_paid_run_is_zero_cost_rejected(
        tmp_path, monkeypatch):
    workflows = tmp_path / "workflows"
    runs = tmp_path / "runs"
    workflows.mkdir()
    (workflows / "example.yaml").write_text(EXAMPLE_YAML, encoding="utf-8")
    monkeypatch.setattr(mcp_module, "WORKFLOWS_DIR", workflows)
    monkeypatch.setattr(mcp_module, "RUNS_DIR", runs)

    dry = mcp_module.dry_run_impl("example", "task")
    assert dry["dry_run"] is True
    assert dry["unconfigured_nodes"] == ["analyst", "reviewer"]
    assert dry["nodes"][0]["model"] == "未配置(待选择)"

    rejected = mcp_module.run_workflow_impl("example", "task")
    assert "未配置模型" in rejected["error"]
    assert "零成本拒绝" in rejected["error"]
    assert not runs.exists() or not list(runs.iterdir())


def test_custom_keeps_yaml_model_and_override_schema_is_closed(tmp_path):
    base = spec_from_yaml(CUSTOM_YAML)
    effective = build_effective_spec(base)
    assert effective.spec.node("only").model == "Fake:primary"
    assert effective.bindings[0]["source"] == "yaml"

    thinking_base = spec_from_yaml(CUSTOM_YAML.replace(
        "    consumes: [task]\n", "    consumes: [task]\n    thinking: high\n"))
    cleared = build_effective_spec(thinking_base, {"only": {"thinking": None}})
    assert cleared.spec.node("only").thinking is None
    assert cleared.overrides[0]["fields"]["thinking"] is None

    with pytest.raises(SpecError, match="禁止或未知字段"):
        build_effective_spec(base, {"only": {"consumes": ["别的节点.output"]}})
    with pytest.raises(SpecError, match="禁止或未知字段"):
        build_effective_spec(base, {"only": {"allow_web": True}})
    with pytest.raises(SpecError, match="禁止或未知字段"):
        build_effective_spec(base, {"only": {"workdir": "x"}})
    with pytest.raises(SpecError, match="未知节点"):
        build_effective_spec(base, {"missing": {"retry": 1}})
    with pytest.raises(SpecError, match="temperature"):
        build_effective_spec(base, {"only": {"temperature": 9}})


def test_provider_ids_only_include_llm_nodes(tmp_path):
    agent_spec = spec_from_yaml(f"""
name: provider-filter
nodes:
  - id: research
    type: research
    model: AgentVendor:model
    prompt: 调研。
    consumes: [task]
  - id: llm
    type: llm
    model: Fake:primary
    fallback: [Other:backup]
    prompt: 汇总。
    consumes: [research.output]
edges:
  - from: research
    to: llm
  - from: llm
    to: END
""")
    assert provider_ids_for_spec(agent_spec) == ["Fake", "Other"]


def test_engine_snapshot_and_event_record_effective_audit(tmp_path):
    spec = spec_from_yaml(CUSTOM_YAML)
    fake = FakeProvider()
    fake.configure("primary", text="完成")
    run = execute_graph(
        spec, task="审计", runs_root=tmp_path / "runs",
        registry=make_registry(fake),
        base_spec_sha256="base-fingerprint",
        binding_summary=({"node": "only", "model": "Fake:primary"},),
        override_summary=({"node": "only", "fields": {"retry": 1}},),
    )

    started = run.events.find(type="run_started")
    assert started["base_spec_sha256"] == "base-fingerprint"
    assert started["effective_spec_sha256"] == started["spec_sha256"]
    assert started["bindings"][0]["model"] == "Fake:primary"
    done = run.events.find(type="node_done", node="only")
    assert done["artifacts"][0]["media_type"] == "text/markdown"
    snapshot = spec_from_snapshot(json.loads(
        (run.dir / "spec.snapshot.json").read_text(encoding="utf-8")))
    assert snapshot.node("only").model == "Fake:primary"


def test_web_rejects_closed_contract_before_run_id_and_returns_history(tmp_path):
    workflows = tmp_path / "workflows"
    runs = tmp_path / "runs"
    workflows.mkdir()
    workflow_path = workflows / "demo.yaml"
    workflow_path.write_text(CUSTOM_YAML, encoding="utf-8")

    fake = FakeProvider()
    fake.configure("primary", text="primary")
    fake.configure("other", text="overridden")
    seen_provider_ids = []

    def factory(provider_ids):
        seen_provider_ids.append(list(provider_ids))
        return make_registry(fake)

    app = create_app(workflows_dir=workflows, runs_dir=runs,
                     registry_factory=factory)
    headers = {"X-Atlas-Request": "1"}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        rejected = client.post(
            "/api/workflows/demo/run", headers=headers,
            json={"task": "x", "node_overrides": {"only": {"workdir": "x"}}})
        assert rejected.status_code == 400
        assert not runs.exists() or not list(runs.iterdir())

        unknown = client.post(
            "/api/workflows/demo/run", headers=headers,
            json={"task": "x", "surprise": True})
        assert unknown.status_code == 400
        assert not runs.exists() or not list(runs.iterdir())

        run_overrides = {"only": {"model": "Fake:other", "retry": 1}}
        preview = client.post(
            "/api/workflows/demo/preview", headers=headers,
            json={"node_overrides": run_overrides})
        assert preview.status_code == 200
        preview_data = preview.json()
        assert preview_data["effective_workflow"]["nodes"][0]["model"] == "Fake:other"
        assert preview_data["effective_workflow"]["nodes"][0]["retry"] == 1
        assert not runs.exists() or not list(runs.iterdir())

        started = client.post(
            "/api/workflows/demo/run", headers=headers,
            json={"task": "x", "node_overrides": run_overrides})
        assert started.status_code == 200
        run_id = started.json()["run_id"]
        for _ in range(50):
            summary = client.get(f"/api/runs/{run_id}").json()
            if summary["status"] in ("done", "failed"):
                break
            time.sleep(0.05)

    assert summary["status"] == "done"
    assert summary["effective_workflow"]["nodes"][0]["model"] == "Fake:other"
    assert seen_provider_ids[-1] == ["Fake"]
    started_event = json.loads((runs / run_id / "events.jsonl").read_text(
        encoding="utf-8").splitlines()[0])
    assert started_event["base_spec_sha256"] != started_event["effective_spec_sha256"]
    assert started_event["effective_spec_sha256"] == preview_data["effective_spec_sha256"]
    assert started_event["bindings"] == preview_data["bindings"]
    assert started_event["overrides"] == preview_data["overrides"]
    assert started_event["overrides"][0]["fields"]["retry"] == 1


def test_mcp_dry_run_and_history_share_effective_spec(tmp_path, monkeypatch):
    workflows = tmp_path / "workflows"
    runs = tmp_path / "runs"
    workflows.mkdir()
    (workflows / "demo.yaml").write_text(CUSTOM_YAML, encoding="utf-8")
    monkeypatch.setattr(mcp_module, "WORKFLOWS_DIR", workflows)
    monkeypatch.setattr(mcp_module, "RUNS_DIR", runs)

    dry_fake = FakeProvider()
    dry_fake.configure("other", text="不会被调用")
    dry = mcp_module.dry_run_impl(
        "demo", "task", {"only": {"model": "Fake:other", "retry": 2}},
        registry_factory=lambda _: make_registry(dry_fake))
    assert dry["nodes"][0]["model"] == "Fake:other"
    assert dry["overrides"][0]["fields"]["retry"] == 2
    assert not runs.exists()

    fake = FakeProvider()
    fake.configure("other", text="ok")
    result = mcp_module.run_workflow_impl(
        "demo", "task", node_overrides={"only": {"model": "Fake:other"}},
        registry_factory=lambda _: make_registry(fake))
    assert result["status"] == "done"
    assert result["effective_workflow"]["nodes"][0]["model"] == "Fake:other"
    again = mcp_module.summarize_run(result["run_id"])
    assert again["effective_spec_sha256"]
    assert again["effective_workflow"]["nodes"][0]["model"] == "Fake:other"


# ── 第五轮 P1:prompt / workdir 运行时覆盖 ─────────────────────────


def test_prompt_override_replaces_duty_text_for_this_run_only():
    base = spec_from_yaml(EXAMPLE_YAML)
    marker = "机密职责文本-8f2a-只应出现在快照不应出现在摘要"
    effective = build_effective_spec(
        base, {"analyst": {"prompt": marker, "model": "Fake:analyst"},
               "reviewer": {"model": "Fake:reviewer"}})

    assert effective.spec.node("analyst").prompt == marker
    assert effective.prompt_overridden == ("analyst",)
    assert effective.effective_fingerprint != effective.base_fingerprint
    # YAML 真相不动:不带覆盖重新具体化仍是原文
    assert build_effective_spec(base).spec.node("analyst").prompt != marker
    # 摘要脱敏:只有长度+哈希前缀,没有全文
    assert marker not in json.dumps(effective.overrides, ensure_ascii=False)
    meta = effective.overrides[0]["fields"]["prompt"]
    assert meta["changed"] is True
    assert meta["chars"] == len(marker)
    assert len(meta["sha256_prefix"]) == 12


def test_prompt_override_rejects_empty_and_non_whitelisted_fields():
    base = spec_from_yaml(EXAMPLE_YAML)
    with pytest.raises(SpecError, match="缺少非空的 prompt"):
        build_effective_spec(base, {"analyst": {"prompt": "   "}})
    # 接线与权限字段仍然不可覆盖:改它们等于改图
    with pytest.raises(SpecError, match="禁止或未知字段"):
        build_effective_spec(base, {"analyst": {"consumes": ["reviewer.output"]}})
    with pytest.raises(SpecError, match="禁止或未知字段"):
        build_effective_spec(base, {"analyst": {"writable": False}})


def test_human_node_accepts_only_prompt_override():
    base = spec_from_yaml("""
name: human-gate
nodes:
  - id: gate
    type: human
    prompt: 请审批。
    consumes: [task]
edges:
  - from: gate
    to: END
""")
    effective = build_effective_spec(base, {"gate": {"prompt": "本次重点看安全。"}})
    assert effective.spec.node("gate").prompt == "本次重点看安全。"
    assert effective.prompt_overridden == ("gate",)
    with pytest.raises(SpecError, match="禁止或未知字段"):
        build_effective_spec(base, {"gate": {"model": "Fake:x"}})
    with pytest.raises(SpecError, match="禁止或未知字段"):
        build_effective_spec(base, {"gate": {"timeout_s": 60}})


def test_agent_workdir_override_uses_same_validation_as_yaml(tmp_path):
    project_a = tmp_path / "project-a"
    project_a.mkdir()
    project_b = tmp_path / "project-b"
    project_b.mkdir()
    base = spec_from_yaml(f"""
name: workdir-override
nodes:
  - id: coder
    type: coding_agent
    prompt: 改码。
    consumes: [task]
    workdir: {json.dumps(str(project_a))}
edges:
  - from: coder
    to: END
""")

    effective = build_effective_spec(base, {"coder": {"workdir": str(project_b)}})
    assert effective.spec.node("coder").workdir == str(project_b)
    # 不存在的目录与 YAML 同一条校验路径拒绝,先于 run_id 与任何调用
    with pytest.raises(SpecError, match="不是存在的目录"):
        build_effective_spec(
            base, {"coder": {"workdir": str(tmp_path / "nope")}})
    # 权限字段仍然不可覆盖
    with pytest.raises(SpecError, match="禁止或未知字段"):
        build_effective_spec(base, {"coder": {"writable": False}})


def test_engine_runs_workdir_override_in_isolated_copy(tmp_path):
    project_a = tmp_path / "project-a"
    project_a.mkdir()
    project_b = tmp_path / "project-b"
    project_b.mkdir()
    (project_b / "seed.txt").write_text("b", encoding="utf-8")
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=project_b, check=True,
                   capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=project_b, check=True,
                   capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=project_b, check=True,
                   capture_output=True)
    base = spec_from_yaml(f"""
name: workdir-iso
nodes:
  - id: coder
    type: coding_agent
    prompt: 改码。
    consumes: [task]
    workdir: {json.dumps(str(project_a))}
edges:
  - from: coder
    to: END
""")
    effective = build_effective_spec(base, {"coder": {"workdir": str(project_b)}})

    seen_cwds = []

    def fake_runner(proj_path, *, node_type, max_turns, cwd, writable,
                    allow_web, allowed_paths, timeout_s, model_ref):
        seen_cwds.append(Path(cwd))
        return "改码完成:新增一个文件并通过自测"

    run = execute_graph(
        effective.spec, task="t", runs_root=tmp_path / "runs",
        registry=make_registry(FakeProvider()), agent_runner=fake_runner,
        base_spec_sha256=effective.base_fingerprint,
        override_summary=effective.overrides)

    assert run.status == "done"
    # agent 只跑在 runs/<id>/worktrees/ 的隔离副本里,副本来自被覆盖的 project_b
    assert len(seen_cwds) == 1
    assert "worktrees" in seen_cwds[0].parts
    assert (seen_cwds[0] / "seed.txt").read_text(encoding="utf-8") == "b"
    # 被覆盖的目标目录原样:没有副本落在里面,种子文件没被动
    assert sorted(p.name for p in project_b.iterdir()) == [".git", "seed.txt"]
    assert (project_b / "seed.txt").read_text(encoding="utf-8") == "b"


def test_web_prompt_override_flows_into_projection_and_ledger(tmp_path):
    workflows = tmp_path / "workflows"
    runs = tmp_path / "runs"
    workflows.mkdir()
    (workflows / "demo.yaml").write_text(CUSTOM_YAML, encoding="utf-8")

    marker = "覆盖后的职责-PROMPT-OVERRIDE"
    fake = FakeProvider()
    fake.configure("primary", text="完成")

    app = create_app(workflows_dir=workflows, runs_dir=runs,
                     registry_factory=lambda _: make_registry(fake))
    headers = {"X-Atlas-Request": "1"}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        preview = client.post(
            "/api/workflows/demo/preview", headers=headers,
            json={"node_overrides": {"only": {"prompt": marker}}})
        assert preview.status_code == 200
        data = preview.json()
        assert data["prompt_overridden"] == ["only"]
        assert data["effective_workflow"]["nodes"][0]["prompt"] == marker
        assert marker not in json.dumps(data["overrides"], ensure_ascii=False)
        assert not runs.exists() or not list(runs.iterdir())

        started = client.post(
            "/api/workflows/demo/run", headers=headers,
            json={"task": "x", "node_overrides": {"only": {"prompt": marker}}})
        assert started.status_code == 200
        run_id = started.json()["run_id"]
        for _ in range(50):
            summary = client.get(f"/api/runs/{run_id}").json()
            if summary["status"] in ("done", "failed"):
                break
            time.sleep(0.05)

    assert summary["status"] == "done"
    run_dir = runs / run_id
    # 账本摘要脱敏,有效规格快照与投影存全文——审计有真相,日志无冗余
    started_event = json.loads(
        (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert marker not in json.dumps(started_event["overrides"],
                                    ensure_ascii=False)
    snapshot = json.loads(
        (run_dir / "spec.snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["nodes"][0]["prompt"] == marker
    projection = (run_dir / "projections" / "only.input.1.txt").read_text(
        encoding="utf-8")
    assert projection.startswith(marker)


def test_web_preview_param_defaults_match_engine_defaults(tmp_path):
    workflows = tmp_path / "workflows"
    runs = tmp_path / "runs"
    workflows.mkdir()
    (workflows / "demo.yaml").write_text(CUSTOM_YAML, encoding="utf-8")

    fake = FakeProvider()
    fake.configure("primary", text="完成")
    app = create_app(workflows_dir=workflows, runs_dir=runs,
                     registry_factory=lambda _: make_registry(fake))
    headers = {"X-Atlas-Request": "1"}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        preview = client.post(
            "/api/workflows/demo/preview", headers=headers,
            json={"node_overrides": {}})
        assert preview.status_code == 200
        defaults = preview.json()["param_defaults"]
        # llm 节点:Fake 供应商没配上限 → 8192;timeout 300;retry 0
        assert defaults["only"]["max_output_tokens"] == 8192
        assert defaults["only"]["timeout_s"] == 300
        assert defaults["only"]["retry"] == 0
        assert defaults["only"]["temperature"] is None
        assert defaults["only"]["seed"] is None
        # 摘要里没有配置对象本身,只有标量
        assert "apiKeyEnv" not in json.dumps(defaults)


def test_web_preview_rejects_unconfigured_agent_before_run_id(tmp_path):
    workflows = tmp_path / "workflows"
    runs = tmp_path / "runs"
    workflows.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    (workflows / "demo.yaml").write_text(f"""
name: agent-defaults
nodes:
  - id: coder
    type: coding_agent
    prompt: 改码。
    consumes: [task]
    workdir: {json.dumps(str(project))}
edges:
  - from: coder
    to: END
""", encoding="utf-8")

    app = create_app(workflows_dir=workflows, runs_dir=runs,
                     registry_factory=lambda _: make_registry(FakeProvider()))
    headers = {"X-Atlas-Request": "1"}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        preview = client.post(
            "/api/workflows/demo/preview", headers=headers,
            json={"node_overrides": {}})
        assert preview.status_code == 200
        assert preview.json()["unconfigured_nodes"] == ["coder"]
        assert not runs.exists() or not list(runs.iterdir())
