# -*- coding: utf-8 -*-
"""E-2A · 运行附件(attachments)。

合同来源:PLAN-stage-e-2026-08-27.md 的 E-2A 章。核心合同:
- 两阶段准入:read→size→SHA 全部通过(阶段一,run_id 之前)→ 统一原子
  落盘(阶段二);任一失败不允许"一半附件进来的 run"。
- 名字即 consumed 逻辑名:全小写 ASCII 正则(大写变体/unicode 同形字符
  一律拒绝,这是刻意的)、保留后缀拒绝、不得叫 task、不得撞节点 id。
- 附件不内联进投影正文,投影只有摘要行;完整性断言照常。
- 账本只记 name/sha256/bytes/basename,响应绝不回传原始绝对路径。
"""
import json

import pytest
from fastapi.testclient import TestClient

from atlas import mcp as mcp_module
from atlas.adapters import FakeProvider
from atlas.engine import execute_graph
from atlas.events import fold_events
from atlas.integrity import sha256_bytes
from atlas.runs import (ATTACHMENT_MAX_BYTES, ATTACHMENT_TOTAL_MAX_BYTES,
                        parse_attachments, stage_attachments)
from atlas.spec import SpecError, spec_from_yaml
from atlas.web import create_app

from conftest import make_registry


# ─────────────────── 名字/形状拒绝矩阵(校验期,零成本) ───────────────────


def _parse(raw, node_ids=frozenset({"lit"})):
    return parse_attachments(raw, node_ids=node_ids)


def test_attachment_name_rejection_matrix(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    good = {"name": "materials", "path": str(target)}
    # 合法名字解析通过
    assert [name for name, _ in _parse([good])] == ["materials"]
    # 大写变体:Task / Report——正则只收全小写,刻意挡掉保留名大小写欺骗
    for name in ("Task", "Task1", "Report"):
        with pytest.raises(SpecError, match="全小写"):
            _parse([{**good, "name": name}])
    # unicode 同形字符:正则字符类只含 ASCII,同形欺骗在正则层挡住(刻意)
    with pytest.raises(SpecError, match="全小写"):
        _parse([{**good, "name": "报告"}])
    # 保留逻辑名 task
    with pytest.raises(SpecError, match="task"):
        _parse([{**good, "name": "task"}])
    # 保留后缀(节点产物命名空间)
    for suffix in (".output", ".diff", ".error", ".changes"):
        with pytest.raises(SpecError, match="保留后缀"):
            _parse([{**good, "name": f"report{suffix}"}])
    # 与节点 id 冲突
    with pytest.raises(SpecError, match="节点 id 冲突"):
        _parse([{**good, "name": "lit"}])
    # 相对路径:解析基准含糊,fail-closed
    with pytest.raises(SpecError, match="绝对路径"):
        _parse([{**good, "path": "relative/file.txt"}])
    # 重复声明
    with pytest.raises(SpecError, match="重复"):
        _parse([good, dict(good)])
    # 形状:未知字段 / 缺 path / 空 path
    with pytest.raises(SpecError, match="未知字段"):
        _parse([{**good, "content_base64": "xx"}])
    with pytest.raises(SpecError, match="path"):
        _parse([{"name": "materials"}])
    with pytest.raises(SpecError, match="path"):
        _parse([{**good, "path": "  "}])
    # 单字符名字不满足正则的 {1,63} 尾段
    with pytest.raises(SpecError, match="全小写"):
        _parse([{**good, "name": "a"}])
    # 尾随换行:$ 锚点会放过 "\n",须用 fullmatch 整串匹配——否则
    # "ghost.output\n" 同时绕过保留后缀护栏(审查 2026-08-27 建议 1)。
    # 两种变体都在正则层被拒(fullmatch 整串匹配,消息归"全小写"法域)
    with pytest.raises(SpecError, match="全小写"):
        _parse([{**good, "name": "materials\n"}])
    with pytest.raises(SpecError, match="全小写"):
        _parse([{**good, "name": "ghost.output\n"}])
    # spec 层 consumes 的裸名回退同一口径:带换行的保留后缀名不放行
    with pytest.raises(SpecError, match="不存在能产出它的节点"):
        spec_from_yaml("""
name: bad
nodes:
  - id: node_a
    type: llm
    model: Fake:primary
    prompt: 干活。
    consumes: [task, "ghost.output\\n"]
edges:
  - {from: node_a, to: END}
""")


# ─────────────────── 两阶段准入:阶段一拒绝不留 run 目录 ───────────────────


def test_stage_rejects_missing_and_oversize(tmp_path):
    missing = _parse([{"name": "materials",
                       "path": str(tmp_path / "nope.txt")}])
    with pytest.raises(SpecError, match="无法读取"):
        stage_attachments(missing)

    big = tmp_path / "big.bin"
    big.write_bytes(b"\0" * (ATTACHMENT_MAX_BYTES + 1))
    with pytest.raises(SpecError, match="单件"):
        stage_attachments(_parse([{"name": "materials",
                                   "path": str(big)}]))

    ok = tmp_path / "ok.bin"
    ok.write_bytes(b"\0" * ATTACHMENT_MAX_BYTES)
    ok2 = tmp_path / "ok2.bin"
    ok2.write_bytes(b"\0" * ATTACHMENT_MAX_BYTES)
    tiny = tmp_path / "tiny.bin"
    tiny.write_bytes(b"\0")
    # 16MiB×2 = 32MiB 恰好合法(≤);再添 1 字节即越过合计上限
    stage_attachments(_parse([
        {"name": "materials", "path": str(ok)},
        {"name": "more", "path": str(ok2)}]))
    with pytest.raises(SpecError, match="合计"):
        stage_attachments(_parse([
            {"name": "materials", "path": str(ok)},
            {"name": "more", "path": str(ok2)},
            {"name": "tiny", "path": str(tiny)}]))


def test_second_attachment_failure_leaves_no_partial_run(
        tmp_path, monkeypatch):
    """审查点 2:构造"第 2 个附件超限"场景,阶段一在 run_id 分配前失败,
    runs 树里没有任何 run 目录、也没有第 1 个附件的落盘残留。"""
    workflows = tmp_path / "workflows"
    runs = tmp_path / "runs"
    workflows.mkdir()
    (workflows / "att.yaml").write_text("""
name: att
nodes:
  - id: only
    type: llm
    model: Fake:primary
    prompt: 汇总材料。
    consumes: [task, materials]
edges:
  - {from: only, to: END}
""", encoding="utf-8")
    ok = tmp_path / "ok.txt"
    ok.write_text("材料", encoding="utf-8")
    big = tmp_path / "big.bin"
    big.write_bytes(b"\0" * (ATTACHMENT_MAX_BYTES + 1))
    monkeypatch.setattr(mcp_module, "WORKFLOWS_DIR", workflows)
    monkeypatch.setattr(mcp_module, "RUNS_DIR", runs)

    result = mcp_module.run_workflow_impl(
        "att", "任务",
        registry_factory=lambda _: make_registry(FakeProvider()),
        agent_runner_factory=lambda spec: None,
        attachments=[{"name": "materials", "path": str(ok)},
                     {"name": "more", "path": str(big)}])
    # 第 2 个附件单件超限(合计帽需 ≥3 个附件才可能成为约束,已在
    # test_stage_rejects_missing_and_oversize 覆盖);阶段一在 run_id
    # 分配前失败,第一个附件绝无落盘残留。
    assert "error" in result and "单件" in result["error"]
    assert "more" in result["error"]
    assert not runs.exists() or not any(runs.iterdir())   # 无 run 目录残留


# ─────────────────── 执行闭环:事件/产物/投影摘要行 ───────────────────

ATT_EXEC_YAML = """
name: e2a-exec
nodes:
  - id: only
    type: llm
    model: Fake:primary
    prompt: 汇总附件材料。
    consumes: [task, materials]
edges:
  - {from: only, to: END}
"""

ATTACHMENT_CONTENT = "季度数据,100,200,300\n" * 40


def _fake_registry():
    fake = FakeProvider()
    fake.configure("primary", text=json.dumps({"summary": "已读附件", "verdict": "pass"}))
    return make_registry(fake)


def _write_workflow(tmp_path):
    workflows = tmp_path / "workflows"
    workflows.mkdir(exist_ok=True)
    (workflows / "att.yaml").write_text(ATT_EXEC_YAML.strip(), encoding="utf-8")
    return workflows


def _attachment_file(tmp_path, *, name="materials.txt",
                     content=ATTACHMENT_CONTENT):
    path = tmp_path / name
    # write_bytes:绕开 Windows 文本模式的 \n→\r\n 转换,保证盘上字节
    # 与断言用的 encode("utf-8") 完全一致
    path.write_bytes(content.encode("utf-8"))
    return path


def test_attachment_admission_ledger_and_projection_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_module, "WORKFLOWS_DIR", _write_workflow(tmp_path))
    monkeypatch.setattr(mcp_module, "RUNS_DIR", tmp_path / "runs")
    path = _attachment_file(tmp_path)

    result = mcp_module.run_workflow_impl(
        "att", "任务",
        registry_factory=lambda _: _fake_registry(),
        agent_runner_factory=lambda spec: None,
        attachments=[{"name": "materials", "path": str(path)}],
        wait=True)
    assert result.get("status") == "done", result

    run_dir = tmp_path / "runs" / result["run_id"]
    events = [json.loads(line) for line in
              run_dir.joinpath("events.jsonl").read_text(encoding="utf-8").splitlines()
              if line.strip()]
    types = [e["type"] for e in events]
    # 事件顺序锁:run_started → attachment_admitted → 首个 node_*
    assert types.index("run_started") < types.index("attachment_admitted") \
        < next(i for i, t in enumerate(types) if t.startswith("node_"))
    admitted = next(e for e in events if e["type"] == "attachment_admitted")
    assert admitted["name"] == "materials"
    assert admitted["sha256"] == sha256_bytes(
        ATTACHMENT_CONTENT.encode("utf-8"))
    assert admitted["bytes"] == len(ATTACHMENT_CONTENT.encode("utf-8"))
    assert admitted["basename"] == "materials.txt"
    assert "path" not in admitted and str(tmp_path) not in json.dumps(admitted)

    # 投影:摘要行存在,内容字节不内联
    projection_path = next(e["projection_path"] for e in events
                           if e["type"] == "node_input")
    projection = open(projection_path, "rb").read()
    assert "运行附件 materials ·".encode("utf-8") in projection
    assert admitted["sha256"][:12].encode("utf-8") in projection
    assert ATTACHMENT_CONTENT.encode("utf-8") not in projection
    # 消费记录含附件引用;产物实体落盘且哈希与 admitted 一致。
    # (fold 按合同显式忽略该事件、不重建附件产物——回归锁在下方测试。)
    node_input = next(e for e in events if e["type"] == "node_input")
    consumed_names = [c["name"] for c in node_input["consumed"]]
    assert "materials" in consumed_names
    artifact_file = run_dir / "artifacts" / "materials.imported0.bin"
    assert sha256_bytes(artifact_file.read_bytes()) == admitted["sha256"]
    folded = fold_events(events)
    assert folded["status"] == "done"


def test_fold_ignores_attachment_admitted(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_module, "WORKFLOWS_DIR", _write_workflow(tmp_path))
    monkeypatch.setattr(mcp_module, "RUNS_DIR", tmp_path / "runs")
    path = _attachment_file(tmp_path)
    result = mcp_module.run_workflow_impl(
        "att", "任务", registry_factory=lambda _: _fake_registry(),
        agent_runner_factory=lambda spec: None,
        attachments=[{"name": "materials", "path": str(path)}], wait=True)
    events = [json.loads(line) for line in
              (tmp_path / "runs" / result["run_id"] / "events.jsonl")
              .read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(e["type"] == "attachment_admitted" for e in events)
    stripped = [e for e in events if e["type"] != "attachment_admitted"]
    assert fold_events(stripped) == fold_events(events)


# ─────────────────── 隐私边界与 wait=false 时序 ───────────────────


def test_response_never_echoes_source_path(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_module, "WORKFLOWS_DIR", _write_workflow(tmp_path))
    monkeypatch.setattr(mcp_module, "RUNS_DIR", tmp_path / "runs")
    path = _attachment_file(tmp_path)
    result = mcp_module.run_workflow_impl(
        "att", "任务", registry_factory=lambda _: _fake_registry(),
        agent_runner_factory=lambda spec: None,
        attachments=[{"name": "materials", "path": str(path)}], wait=True)
    assert str(tmp_path) not in json.dumps(result, ensure_ascii=False)
    assert str(path) not in json.dumps(result, ensure_ascii=False)


def test_wait_false_admits_before_first_node_event(tmp_path, monkeypatch):
    """wait=false:阶段一同步完成后才返回 starting;账本里
    attachment_admitted 先于首个 node_* 事件。"""
    import time as time_module
    monkeypatch.setattr(mcp_module, "WORKFLOWS_DIR", _write_workflow(tmp_path))
    monkeypatch.setattr(mcp_module, "RUNS_DIR", tmp_path / "runs")
    path = _attachment_file(tmp_path)
    result = mcp_module.run_workflow_impl(
        "att", "任务", registry_factory=lambda _: _fake_registry(),
        agent_runner_factory=lambda spec: None,
        attachments=[{"name": "materials", "path": str(path)}], wait=False)
    assert result["status"] == "starting"
    run_dir = tmp_path / "runs" / result["run_id"]
    deadline = time_module.monotonic() + 10
    while True:
        events = [json.loads(line) for line in
                  run_dir.joinpath("events.jsonl").read_text(encoding="utf-8")
                  .splitlines() if line.strip()] if \
            run_dir.joinpath("events.jsonl").exists() else []
        types = [e["type"] for e in events]
        if "node_input" in types or types[-1:] in (["run_failed"], ["run_done"]):
            break
        if time_module.monotonic() > deadline:
            raise AssertionError("run 未在期限内出现首个节点事件")
        time_module.sleep(0.05)
    assert types.index("attachment_admitted") \
        < next(i for i, t in enumerate(types) if t.startswith("node_"))


# ─────────────────── Web 端点接线 ───────────────────


def test_web_start_run_accepts_attachments_and_maps_errors(tmp_path):
    workflows = _write_workflow(tmp_path)
    app = create_app(
        workflows_dir=workflows, runs_dir=tmp_path / "runs",
        registry_factory=lambda _: _fake_registry(),
        agent_runner_factory=lambda spec: None)
    headers = {"X-Atlas-Request": "1"}
    good = _attachment_file(tmp_path)
    big = tmp_path / "big.bin"
    big.write_bytes(b"\0" * (ATTACHMENT_MAX_BYTES + 1))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        ok = client.post("/api/workflows/att/run", headers=headers, json={
            "task": "任务",
            "attachments": [{"name": "materials", "path": str(good)}]})
        assert ok.status_code == 200
        assert "run_id" in ok.json()
        # 名字非法 → 400(零成本拒绝,未分配 run_id)
        bad_name = client.post("/api/workflows/att/run", headers=headers, json={
            "task": "任务",
            "attachments": [{"name": "Task", "path": str(good)}]})
        assert bad_name.status_code == 400
        # 超限 → 400
        oversize = client.post("/api/workflows/att/run", headers=headers, json={
            "task": "任务",
            "attachments": [{"name": "materials", "path": str(big)}]})
        assert oversize.status_code == 400
        # 未知字段仍被白名单拒绝
        unknown = client.post("/api/workflows/att/run", headers=headers, json={
            "task": "任务", "attachments_b64": "xx"})
        assert unknown.status_code == 400


# ─────────────────── P13 fork 正交性 ───────────────────


def test_fork_closure_with_attachments_is_conservative_and_orthogonal(tmp_path):
    """审查点 7:附件属 run 输入侧。消费附件的节点在 fork 时无法静态证明
    输入相等(新 run 可能带不同附件字节)→ 诚实归 changed 重跑;未消费
    附件的上游节点不受影响,仍走合成导入复用——闭包比较本身不被附件
    干扰(不炸、不误伤无关节点)。"""
    from atlas.fork import compute_fork_plan

    spec = spec_from_yaml("""
name: e2a-fork
nodes:
  - id: planner
    type: llm
    model: Fake:primary
    prompt: 规划。
    consumes: [task]
  - id: only
    type: llm
    model: Fake:primary
    prompt: 汇总附件材料。
    consumes: [task, materials]
edges:
  - {from: planner, to: only}
  - {from: only, to: END}
""")
    path = _attachment_file(tmp_path)
    parsed = parse_attachments([{"name": "materials", "path": str(path)}],
                               node_ids=frozenset(n.id for n in spec.nodes))
    staged = stage_attachments(parsed)
    result = execute_graph(
        spec, task="任务", runs_root=tmp_path,
        registry=_fake_registry(),
        attachments=staged, run_id="20260201-000000-srcfork")
    assert result.status == "done"

    plan = compute_fork_plan(
        spec=spec, source_run="20260201-000000-srcfork",
        runs_root=tmp_path, task_sha256=sha256_bytes("任务".encode("utf-8")),
        backend_sha256=_backend_sha_of(result))
    # 消费附件的节点:静态判定"说不清"→ 诚实重跑(保守方向)
    assert plan["changed"] == ["only"]
    # 未消费附件的 planner:身份可静态证明相等 → 合成导入复用
    imported_names = [item["name"] for item in plan["imports"]]
    assert "planner.output" in imported_names
    assert plan["closure"] == ["only"]


def _backend_sha_of(result):
    started = next(e for e in result.events.all()
                   if e["type"] == "run_started")
    return started["backend_sha256"]
