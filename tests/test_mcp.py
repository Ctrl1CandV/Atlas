# -*- coding: utf-8 -*-
"""MCP 六工具:全部走内部实现(与工具封装同一份代码),假供应商,零花费。"""
import json
import shutil
from pathlib import Path

import pytest

from atlas import mcp as m
from atlas.adapters import FakeProvider

from conftest import TASK_TEXT, good_review_text, good_writer_text, make_registry

GOOD_YAML = """
name: mcp_demo
description: mcp 测试图
nodes:
  - id: a
    type: llm
    model: Fake:primary
    prompt: 分析任务。
    consumes: [task]
  - id: b
    type: llm
    model: Fake:other
    prompt: 审查上游。
    consumes: [task, a.output]
edges:
  - from: a
    to: b
  - from: b
    to: END
""".lstrip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    workflows = tmp_path / "workflows"
    runs = tmp_path / "runs"
    workflows.mkdir()
    (workflows / "demo.yaml").write_text(GOOD_YAML, encoding="utf-8")
    monkeypatch.setattr(m, "WORKFLOWS_DIR", workflows)
    monkeypatch.setattr(m, "RUNS_DIR", runs)

    fake = FakeProvider()
    fake.configure("primary", text=good_writer_text(30))
    fake.configure("other", text=good_review_text())
    factory = lambda pids: make_registry(fake)
    return {"workflows": workflows, "runs": runs, "factory": factory}


def test_validate_rejects_and_points_to_fix(env):
    bad = m.validate_workflow_impl(yaml_text="name: x\nnodes:\n  - id: a\n    type: magic\n    model: Fake:x\n    prompt: p\n    consumes: [task]\n")
    assert bad["valid"] is False
    assert "封闭清单" in bad["error"]
    assert "next" in bad

    good = m.validate_workflow_impl(yaml_text=GOOD_YAML)
    assert good["valid"] is True
    assert good["entry"] == "a"
    assert good["heterogeneity"]["providers_used"] == ["Fake"]
    assert good["next"]


def test_validate_flags_same_vendor_pair(env):
    result = m.validate_workflow_impl(yaml_text=GOOD_YAML)
    assert result["heterogeneity"]["same_vendor_node_pairs"] == [["a", "b"]]


def test_dry_run_renders_without_spending(env):
    out = m.run_workflow_impl("demo", "长任务" * 100, dry_run=True,
                              registry_factory=env["factory"])
    assert out["dry_run"] is True
    assert out["nodes"][0]["prompt"] == "分析任务。"
    assert out["nodes"][0]["chain"] == ["Fake:primary"]
    assert out["nodes"][1]["upstream_outputs_inline"] == ["a.output"]
    assert out["nodes"][0]["est_prompt_tokens"] > 0
    # dry_run 不产生任何运行目录
    assert not list(env["runs"].glob("*"))


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("case", ["empty", "blank", "oversized"])
def test_task_validation_matches_web_and_never_allocates_run(env, dry_run, case):
    task = {"empty": "", "blank": "   "}.get(case, "甲" * (1024 * 1024))
    out = m.run_workflow_impl(
        "demo", task, dry_run=dry_run, registry_factory=env["factory"])
    assert "task" in out["error"]
    assert "非空" in out["error"] or "超过上限" in out["error"]
    assert not env["runs"].exists() or not list(env["runs"].iterdir())


def test_run_blocks_until_done(env):
    out = m.run_workflow_impl("demo", TASK_TEXT, registry_factory=env["factory"])
    assert out["status"] == "done"
    assert out["node_details"]["a"]["model_used"] == "Fake:primary"
    assert out["totals"]["input_tokens"] > 0
    assert Path(out["run_dir"]).exists()
    # get_run 与 run 输出一致
    again = m.summarize_run(out["run_id"])
    assert again["status"] == "done"
    assert again["node_details"]["b"]["degraded"] is False


def test_run_failure_visible(env):
    fake = FakeProvider()
    fake.configure("primary", text="")  # 假成功:空返回,无备用 → 失败
    fake.configure("other", text=good_review_text())
    out = m.run_workflow_impl(
        "demo", TASK_TEXT, registry_factory=lambda p: make_registry(fake))
    assert out["status"] == "failed"
    assert "返回内容为空" in out["error"]


def test_tool_wrappers_registered():
    # 6 个工具都注册上了；resume 只接受动态 interrupted 运行。
    import asyncio
    from mcp.server.mcpserver.server import MCPServer

    async def tools():
        return await m.server.list_tools()
    names = {t.name for t in asyncio.run(tools())}
    assert names == {"atlas_validate_workflow", "atlas_save_workflow",
                     "atlas_run_workflow", "atlas_list_workflows",
                     "atlas_get_run", "atlas_resume_run"}, names


def test_validate_and_save_errors_remain_strings_with_source_location(env):
    bad_yaml = (
        "name: bad\nnodes:\n  - id: a\n    type: magic\n"
        "    model: Fake:x\n    prompt: p\n    consumes: [task]\n")
    expected = "path nodes[0].type, line 4, column 5"

    validation = m.validate_workflow_impl(yaml_text=bad_yaml)
    assert validation["valid"] is False
    assert isinstance(validation["error"], str)
    assert expected in validation["error"]

    saved = m.save_workflow_impl(
        "bad-location", bad_yaml, workflows_dir=env["workflows"])
    assert saved["saved"] is False
    assert isinstance(saved["error"], str)
    assert expected in saved["error"]
    assert not (env["workflows"] / "bad-location.yaml").exists()


def test_summarize_run_reports_interrupted_but_held_lock_reports_running(env):
    from atlas.engine import acquire_run_lock, release_run_lock

    run_id = "dynamic-run"
    run_dir = env["runs"] / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text(
        json.dumps({"seq": 1, "ts": "t", "type": "run_started",
                    "run_id": run_id, "graph": "demo"}) + "\n",
        encoding="utf-8")

    assert m.summarize_run(run_id)["status"] == "interrupted"
    acquire_run_lock(run_id, runs_root=env["runs"])
    try:
        assert m.summarize_run(run_id)["status"] == "running"
    finally:
        release_run_lock(run_id, runs_root=env["runs"])


def test_resume_run_impl_rejects_invalid_missing_paused_and_done(env):
    from conftest import load_graph, standard_fake
    from atlas.engine import execute_graph, prepare_execution

    with pytest.raises(ValueError, match="id 只允许"):
        m.resume_run_impl("../invalid")

    fake = standard_fake(100)
    registry = make_registry(fake)
    paused_spec = load_graph("human_gate")
    paused = execute_graph(
        paused_spec, task=TASK_TEXT, runs_root=env["runs"],
        prepared=prepare_execution(paused_spec, registry))
    assert paused.status == "paused"

    done_spec = load_graph("two_node")
    done = execute_graph(
        done_spec, task=TASK_TEXT, runs_root=env["runs"],
        prepared=prepare_execution(done_spec, registry))
    assert done.status == "done"

    def forbidden_factory(_):
        raise AssertionError("终态 resume 不得预检当前后端")

    missing = m.resume_run_impl("missing-run", registry_factory=forbidden_factory)
    paused_error = m.resume_run_impl(paused.run_id, registry_factory=forbidden_factory)
    done_error = m.resume_run_impl(done.run_id, registry_factory=forbidden_factory)

    for result in (missing, paused_error, done_error):
        assert isinstance(result["error"], str)
    assert "不存在(没有 events.jsonl)" in missing["error"]
    assert "paused" in paused_error["error"]
    assert "done" in done_error["error"]


def test_resume_run_impl_completes_interrupted_checkpointed_run(env):
    from atlas.adapters import AllCandidatesFailed
    from atlas.engine import execute_graph, prepare_execution
    from atlas.events import EventReader
    from conftest import load_graph, standard_fake

    spec = load_graph("three_node")
    fake = standard_fake(100)
    fake.configure("third", transport_error="simulated process loss")
    registry = make_registry(fake)
    prepared = prepare_execution(spec, registry)
    with pytest.raises(AllCandidatesFailed):
        execute_graph(spec, task=TASK_TEXT, runs_root=env["runs"],
                      prepared=prepared)
    run_dir = next(env["runs"].glob("*/events.jsonl")).parent
    events_path = run_dir / "events.jsonl"
    events = EventReader(events_path).all()
    assert events[-1]["type"] == "run_failed"
    events_path.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n"
                for event in events[:-1]), encoding="utf-8")
    fake.configure("third", text="recovered")

    resumed = m.resume_run_impl(
        run_dir.name, registry_factory=lambda _: registry)
    assert resumed["status"] == "done", resumed.get("failed_error")
    final_events = EventReader(events_path).all()
    assert sum(event["type"] == "run_resumed" for event in final_events) == 1
    assert sum(event.get("type") == "node_done"
               and event.get("node") == "node_a" for event in final_events) == 1


def test_mcp_streamable_http_mounted_in_web_app(tmp_path):
    """atlas-web 单进程同时暴露 /mcp(streamable-http)与 /api。

    用真实 HTTP 栈(uvicorn + urllib)验证 initialize → tools/list →
    tools/call 全链路,以及 Web 中间件对 MCP 的豁免边界:
    - Host 校验对 /mcp 仍然生效(回环红线不放松);
    - X-Atlas-Request 只豁免 /mcp,/api 写操作仍必须带头。
    """
    import threading
    import time
    import urllib.error
    import urllib.request

    import uvicorn
    from fastapi.testclient import TestClient  # noqa: F401 (语义参照)
    from atlas.web import create_app

    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "demo.yaml").write_text(GOOD_YAML, encoding="utf-8")
    fake = FakeProvider()
    fake.configure("primary", text=good_writer_text(10))
    fake.configure("other", text=good_review_text())
    app = create_app(workflows_dir=workflows, runs_dir=tmp_path / "runs",
                     registry_factory=lambda _: make_registry(fake),
                     api_only=True)

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    assert server.started
    base_url = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"

    def post(payload, session_id=None):
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        request = urllib.request.Request(
            f"{base_url}/mcp", data=json.dumps(payload).encode(),
            headers=headers)
        response = urllib.request.urlopen(request, timeout=15)
        raw = response.read().decode()
        parsed = json.loads(raw) if raw.strip() else None
        return response.status, dict(response.headers), parsed

    try:
        status, headers, init = post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "0"}}})
        assert status == 200
        assert init["result"]["serverInfo"]["name"] == "atlas"
        session_id = headers.get("mcp-session-id")

        post({"jsonrpc": "2.0", "method": "notifications/initialized"},
             session_id)
        _, _, listing = post({"jsonrpc": "2.0", "id": 2,
                              "method": "tools/list"}, session_id)
        names = {t["name"] for t in listing["result"]["tools"]}
        assert names == {"atlas_validate_workflow", "atlas_save_workflow",
                         "atlas_run_workflow", "atlas_list_workflows",
                         "atlas_get_run", "atlas_resume_run"}

        _, _, call = post({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "atlas_validate_workflow",
                       "arguments": {"yaml": GOOD_YAML}}}, session_id)
        result = json.loads(call["result"]["content"][0]["text"])
        assert result["valid"] is True

        # 同一进程的 /api 不受挂载影响;Host 红线与写头要求保持不变。
        with urllib.request.urlopen(f"{base_url}/api/workflows",
                                    timeout=15) as resp:
            assert resp.status == 200
        evil = urllib.request.Request(
            f"{base_url}/api/workflows", headers={"Host": "evil.example.com"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(evil, timeout=15)
        assert exc.value.code == 403
        no_header = urllib.request.Request(
            f"{base_url}/api/workflows/demo/run",
            data=json.dumps({"task": "x"}).encode(),
            headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(no_header, timeout=15)
        assert exc.value.code == 403
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_run_workflow_impl_requires_yaml_or_workflow_id(env):
    out = m.run_workflow_impl("", "任务", registry_factory=lambda p: None)
    assert "要么传 yaml" in out["error"]


def test_run_workflow_impl_accepts_adhoc_yaml_without_persisting(env):
    """自定义图直跑:yaml 全文进 run,不写 workflows/。"""
    adhoc = GOOD_YAML.replace("name: mcp_demo", "name: adhoc_once")
    out = m.run_workflow_impl("", "临时任务", registry_factory=env["factory"],
                              yaml=adhoc)
    assert out["status"] == "done", out.get("failed_error")
    assert out["graph"] == "adhoc_once"
    assert out["nodes_done"] == ["a", "b"]
    assert list(env["workflows"].glob("*.yaml")) == [env["workflows"] / "demo.yaml"]


def test_run_workflow_impl_persist_as_saves_after_completion(env):
    adhoc = GOOD_YAML.replace("name: mcp_demo", "name: adhoc_keep")
    out = m.run_workflow_impl("", "要固化的任务", registry_factory=env["factory"],
                              yaml=adhoc, persist_as="kept-custom")
    assert out["status"] == "done"
    saved = out["persisted"]
    assert saved["saved"] is True and saved["created"] is True
    persisted = env["workflows"] / "kept-custom.yaml"
    assert persisted.read_text(encoding="utf-8") == adhoc


def test_run_workflow_impl_persist_as_collision_reports_not_overwrites(env):
    (env["workflows"] / "taken.yaml").write_text(
        "name: taken\nnodes:\n  - id: a\n    type: llm\n    model: Fake:primary\n"
        "    prompt: p\n    consumes: [task]\nedges:\n  - from: a\n    to: END\n",
        encoding="utf-8")
    adhoc = GOOD_YAML.replace("name: mcp_demo", "name: adhoc_collide")
    out = m.run_workflow_impl("", "任务", registry_factory=env["factory"],
                              yaml=adhoc, persist_as="taken")
    assert out["status"] == "done"
    assert out["persisted"]["saved"] is False
    assert "expected_sha256" in out["persisted"]["error"]
    assert "taken" in (env["workflows"] / "taken.yaml").read_text(encoding="utf-8")


def test_run_workflow_impl_rejects_invalid_persist_as_before_running(env):
    for bad in ("CON", "..", "no/slash"):
        out = m.run_workflow_impl("", "任务", registry_factory=env["factory"],
                                  yaml=GOOD_YAML, persist_as=bad)
        assert "error" in out
    assert not list(env["runs"].glob("*/events.jsonl"))


def test_dry_run_impl_accepts_adhoc_yaml(env):
    adhoc = GOOD_YAML.replace("name: mcp_demo", "name: adhoc_preview")
    out = m.run_workflow_impl("", "预演", dry_run=True,
                              registry_factory=env["factory"], yaml=adhoc,
                              persist_as="never-written")
    assert out["dry_run"] is True
    assert out["name"] == "adhoc_preview"
    assert out["persist_note"]
    assert not (env["workflows"] / "never-written.yaml").exists()
