# -*- coding: utf-8 -*-
"""M6(PLAN-v3):类型化产物数据链 + 思考三层语义 + diff 采集净化。

全部假供应商/假 CLI,不花钱。覆盖:
- node_done 携带 artifacts 数组(role/bytes/media_type),state 与 fold 同构;
- diff 采集:numstat 元数据、stat 不混进 patch、任意深度 __pycache__ 排除、
  超限截断标 complete=False;
- 旧账本兼容:只有 output_path/diff_path 的 node_done 也能 fold 出类型化条目;
- Web:get_run 透出 artifacts 与三层思考块;get_workflow 透出节点静态参数;
  /api/thinking-capabilities 区分 unprobed。
"""
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas.engine import execute_graph
from atlas.events import fold_events
from atlas.spec import spec_from_yaml

from conftest import TASK_TEXT, good_review_text, make_registry
from atlas.adapters import FakeProvider


def _registry():
    fake = FakeProvider()
    fake.configure("primary", text=good_review_text())
    return make_registry(fake)


def _git(project: Path, *args: str):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   cwd=project, check=True, capture_output=True)


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "target"
    (project / "pkg").mkdir(parents=True)
    (project / "app.py").write_text("def add(a, b):\n    return a + b\n",
                                    encoding="utf-8")
    _git(project, "init", "-q")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    return project


def _coding_spec(project: Path) -> str:
    return f"""
name: coding_m6
nodes:
  - id: implementer
    type: coding_agent
    prompt: 改 app.py 并自测。
    consumes: [task]
    workdir: {project.as_posix()}
edges:
  - from: implementer
    to: END
"""


def test_node_done_carries_typed_artifacts(tmp_path):
    """coding_agent 的 node_done 带类型化产物:report + diff,含元数据。"""
    project = _make_project(tmp_path)

    def runner(attachment, *, node_type, max_turns, cwd=None, **kw):
        (cwd / "app.py").write_text(
            "def add(a, b):\n    return a + b + 1\n", encoding="utf-8")
        (cwd / "new_file.py").write_text("x = 1\n", encoding="utf-8")
        return "改动摘要:两处修改。"

    run = execute_graph(spec_from_yaml(_coding_spec(project)), task=TASK_TEXT,
                        runs_root=tmp_path, registry=_registry(),
                        agent_runner=runner)
    done = run.events.find(type="node_done", node="implementer")
    arts = done["artifacts"]
    by_role = {a["role"]: a for a in arts}
    assert set(by_role) == {"report", "diff"}
    # report:markdown、字节数真实
    assert by_role["report"]["media_type"] == "text/markdown"
    assert by_role["report"]["bytes"] == len("改动摘要:两处修改。".encode("utf-8"))
    # diff:纯 patch、完整、numstat 元数据
    d = by_role["diff"]
    assert d["media_type"] == "text/x-diff" and d["complete"] is True
    assert d["metadata"]["files_changed"] == 2
    assert d["metadata"]["additions"] >= 2
    assert d["metadata"]["baseline_digest"]
    assert d["metadata"]["result_digest"] != d["metadata"]["baseline_digest"]
    assert d["metadata"]["patch_digest"] == d["sha256"]
    assert d["bytes"] == Path(d["path"]).stat().st_size
    # 事件与 fold 后的状态同构(role 保留;A6 全等断言在 fold 测试里)
    assert run.folded()["artifacts"]["implementer.diff"]["role"] == "diff"
    assert run.folded()["artifacts"]["implementer.output"]["role"] == "report"


def test_diff_pure_patch_without_stat_and_pycache(tmp_path):
    """patch 顶部不混 stat 文本;任意深度的 __pycache__/*.pyc 不进 patch。"""
    project = _make_project(tmp_path)

    def runner(attachment, *, node_type, max_turns, cwd=None, **kw):
        (cwd / "app.py").write_text(
            "def add(a, b):\n    return a + b + 2\n", encoding="utf-8")
        # 嵌套缓存噪音:pkg/__pycache__ 与根 __pycache__ 都产生
        (cwd / "pkg" / "__pycache__").mkdir(exist_ok=True)
        (cwd / "pkg" / "__pycache__" / "m.pyc").write_bytes(b"\x00\x01")
        (cwd / "__pycache__").mkdir(exist_ok=True)
        (cwd / "__pycache__" / "n.pyc").write_bytes(b"\x00\x02")
        return "改完了。"

    run = execute_graph(spec_from_yaml(_coding_spec(project)), task=TASK_TEXT,
                        runs_root=tmp_path, registry=_registry(),
                        agent_runner=runner)
    done = run.events.find(type="node_done", node="implementer")
    patch = Path(done["diff_path"]).read_text(encoding="utf-8")
    assert "file changed" not in patch and "files changed" not in patch, \
        "stat 摘要不得混进 patch 本体"
    assert "__pycache__" not in patch and ".pyc" not in patch, \
        "任意深度的缓存噪音必须被排除"
    assert "+ 2" in patch
    meta = next(a for a in done["artifacts"] if a["role"] == "diff")["metadata"]
    assert meta["files_changed"] == 1


def test_diff_truncation_fails_node(tmp_path, monkeypatch):
    """patch 超限必须失败，不能把 numstat 摘要当作可审阅 diff 继续运行。"""
    from atlas.nodes import agent as agent_mod
    monkeypatch.setattr(agent_mod, "DIFF_MAX_BYTES", 64)
    project = _make_project(tmp_path)

    def runner(attachment, *, node_type, max_turns, cwd=None, **kw):
        (cwd / "app.py").write_text("def add(a, b):\n" + "    # pad\n" * 40 +
                                    "    return a + b\n", encoding="utf-8")
        return "改完了。"

    with pytest.raises(agent_mod.AgentCliError, match="超过 diff 上限"):
        execute_graph(spec_from_yaml(_coding_spec(project)), task=TASK_TEXT,
                      runs_root=tmp_path, registry=_registry(),
                      agent_runner=runner)


def test_legacy_node_done_folds_to_typed(tmp_path):
    """旧账本兼容:只有 output_path(+diff_path) 的 node_done 也能 fold。"""
    legacy = {
        "seq": 1, "type": "node_done", "node": "imp",
        "output_path": "artifacts/imp.output.1.txt",
        "output_sha256": "aaa",
        "diff_path": "artifacts/imp.diff.1.patch",
        "diff_sha256": "bbb",
    }
    state = fold_events([legacy])
    assert state["artifacts"]["imp.output"]["role"] == "output"
    assert state["artifacts"]["imp.diff"]["role"] == "diff"
    assert state["artifacts"]["imp.diff"]["media_type"] == "text/x-diff"


# ── Web 层 ─────────────────────────────────────────────────────


def _web_app(tmp_path):
    from atlas.web import create_app
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "w.yaml").write_text("""
name: w
nodes:
  - id: a
    type: llm
    model: Fake:primary
    prompt: 分析。
    consumes: [task]
    thinking: high
    max_output_tokens: 20000
edges:
  - from: a
    to: END
""".lstrip(), encoding="utf-8")
    fake = FakeProvider()
    fake.configure("primary", text=good_review_text(), reasoning_tokens=1371)
    return create_app(workflows_dir=workflows, runs_dir=tmp_path / "runs",
                      registry_factory=lambda pids: make_registry(fake))



def _wait_done(client, rid, timeout_s: float = 30.0):
    """轮询到终态;超时大声失败,而不是索引空 nodes。"""
    import time as _time
    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        data = client.get(f"/api/runs/{rid}").json()
        if data.get("status") in ("done", "failed", "cancelled"):
            assert data["status"] == "done", data
            return data
        _time.sleep(0.02)
    raise AssertionError(f"run {rid} 在 {timeout_s}s 内未到终态")

def _client(app) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1")


def test_get_run_artifacts_and_thinking_layers(tmp_path):
    app = _web_app(tmp_path)
    with _client(app) as client:
        rid = client.post("/api/workflows/w/run", json={"task": TASK_TEXT},
                          headers={"X-Atlas-Request": "1"}).json()["run_id"]
        data = _wait_done(client, rid)
    node = data["nodes"][0]
    arts = node["artifacts"]
    assert any(a["role"] == "output" for a in arts)
    th = node["thinking"]
    assert th["requested_tier"] == "high"
    # 证据类型必须来自事件里的 reasoning_kind(真实链路),不是 web 层兜底推断
    assert node.get("reasoning_kind") == "usage_reasoning_tokens"
    assert th["evidence"] == {"kind": "reasoning_tokens", "value": 1371}
    assert th["capability"] in ("effort", "budget", "none", "unprobed")


def test_get_run_provider_default_and_presence(tmp_path):
    """未指定档位 → provider_default;thinking_block 不冒充 token 数。"""
    app = _web_app(tmp_path)
    with _client(app) as client:
        rid = client.post("/api/workflows/w/run", json={"task": TASK_TEXT},
                          headers={"X-Atlas-Request": "1"}).json()["run_id"]
        data = _wait_done(client, rid)
    node = data["nodes"][0]
    assert node["thinking"]["requested_tier"] in ("high", "provider_default")
    assert set(node["thinking"]) == {"capability", "requested_tier", "evidence"}
    # presence 语义:kind=thinking_block 时 value 必须是 None
    if node["thinking"]["evidence"]["kind"] == "thinking_block":
        assert node["thinking"]["evidence"]["value"] is None


def test_get_workflow_returns_static_params(tmp_path):
    app = _web_app(tmp_path)
    with _client(app) as client:
        wf = client.get("/api/workflows/w").json()
    assert wf["nodes"][0]["thinking"] == "high"
    assert wf["nodes"][0]["max_output_tokens"] == 20000
    assert "retry" in wf["nodes"][0] and "timeout_s" in wf["nodes"][0]


def test_thinking_capabilities_endpoint(tmp_path):
    app = _web_app(tmp_path)
    with _client(app) as client:
        caps = client.get("/api/thinking-capabilities").json()
    entry = caps.get("Fake:primary")
    # Fake:primary 不在真实探测表里 → unprobed;白名单里其他模型亦须合法枚举
    assert entry is None or entry["kind"] == "unprobed"
    for v in caps.values():
        assert v["kind"] in ("unprobed", "effort", "budget", "none")


def test_fold_skips_none_output_path():
    """审查 M6-minor4:artifacts 缺 output 条目且无 output_path 时,
    不得注入 {path: None}(下游 from_dict 会 TypeError)。"""
    ev = {"seq": 1, "type": "node_done", "node": "x",
          "artifacts": [{"name": "x.diff", "role": "diff",
                         "path": "a", "sha256": "b", "bytes": 1}]}
    state = fold_events([ev])
    assert "x.output" not in state["artifacts"]
    assert state["artifacts"]["x.diff"]["path"] == "a"


def test_fold_skips_malformed_entries():
    """审查 M6-minor5:缺 name 的畸形条目跳过,fold 永远可完成。"""
    ev = {"seq": 1, "type": "node_done", "node": "x",
          "artifacts": [{"role": "diff"}, "junk",
                        {"name": "x.output", "role": "output",
                         "path": "p", "sha256": "s", "bytes": 2}]}
    state = fold_events([ev])
    assert set(state["artifacts"]) == {"x.output"}


def test_nongit_project_diff_fails_loudly(tmp_path):
    """非 git 仓库无法提供完整 diff，必须终止而不是生成部分成功产物。"""
    project = tmp_path / "plain"
    project.mkdir()
    (project / "f.txt").write_text("x", encoding="utf-8")

    def runner(attachment, *, node_type, max_turns, cwd=None, **kw):
        return "报告。"

    from atlas.spec import spec_from_yaml as sfy
    spec = sfy(f"""
name: nongit
nodes:
  - id: imp
    type: coding_agent
    prompt: p
    consumes: [task]
    workdir: {project.as_posix()}
edges:
  - from: imp
    to: END
""")
    from atlas.nodes.agent import AgentCliError
    with pytest.raises(AgentCliError, match="不是 git 仓库"):
        execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                      registry=_registry(), agent_runner=runner)


def test_capability_enum_unified(tmp_path):
    """审查 M6-minor8:get_run 与 capabilities 端点同用 unprobed 枚举。"""
    from atlas.web import create_app
    from fastapi.testclient import TestClient
    from conftest import good_review_text
    fake = FakeProvider()
    fake.configure("primary", text=good_review_text(), reasoning_tokens=300)
    wf = tmp_path / "workflows"
    wf.mkdir()
    (wf / "w.yaml").write_text("""
name: w
nodes:
  - id: a
    type: llm
    model: Fake:primary
    prompt: p
    consumes: [task]
edges:
  - from: a
    to: END
""".lstrip(), encoding="utf-8")
    app = create_app(workflows_dir=wf, runs_dir=tmp_path / "runs",
                     registry_factory=lambda pids: make_registry(fake))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        rid = c.post("/api/workflows/w/run", json={"task": TASK_TEXT},
                     headers={"X-Atlas-Request": "1"}).json()["run_id"]
        data = _wait_done(c, rid)
    assert data["nodes"][0]["thinking"]["capability"] == "unprobed"
