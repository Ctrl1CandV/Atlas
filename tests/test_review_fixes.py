# -*- coding: utf-8 -*-
"""M1 peer review(独立模型审查)发现的问题的回归测试。

审查原文的四条 🟠 与关键 🟡,每条修复都固定在这里:
- 撕裂账本续跑不回卷序号
- 并行超步崩溃后续跑不覆盖同名产物(红线 ③ 的边界形态)
- rid/wid 反斜杠穿越、Host 头、跨站 POST 头
- 续跑 spec 指纹校验、并发续跑互斥
"""
import json
import time

import pytest
from fastapi.testclient import TestClient

from atlas.adapters import AllCandidatesFailed, FakeProvider
from atlas.engine import (_resume_graph_replay, execute_graph,
                          resume_graph)
from atlas.events import EventLog, EventReader
from atlas.integrity import sha256_bytes
from atlas.spec import SpecError, spec_from_yaml
from atlas.web import create_app

from conftest import TASK_TEXT, load_graph, make_registry, standard_fake


# ── 🟠2:撕裂账本续跑 ─────────────────────────────────────────


def test_torn_tail_line_does_not_reset_seq(tmp_path):
    """进程写到一半被 kill:最后一行是半行 JSON。续跑序号必须接上,不回卷。"""
    fake = FakeProvider()
    fake.configure("primary", text="第一稿")
    fake.configure("other", transport_error="崩溃于 node_b")
    fake.configure("third", text="第三步")

    with pytest.raises(AllCandidatesFailed):
        execute_graph(load_graph("three_node"), task=TASK_TEXT,
                      runs_root=tmp_path, registry=make_registry(fake))
    run_dir = next(tmp_path.glob("*/events.jsonl")).parent

    # 模拟撕裂写:在账本尾部追加半行(进程被 kill 的形态)
    with open(run_dir / "events.jsonl", "a", encoding="utf-8") as f:
        f.write('{"seq": 99, "ts": "2026-01-01T00:00:00")')  # 没写完的 JSON

    before = EventReader(run_dir / "events.jsonl").all()
    last_seq = before[-1]["seq"]

    fake.configure("other", text="第二稿(修复后)")
    resumed = _resume_graph_replay(run_dir.name, _test_only=True, spec=load_graph("three_node"),
                           runs_root=tmp_path, registry=make_registry(fake))

    events = resumed.events.all()
    seqs = [e["seq"] for e in events]
    # 序号单调、无重复、从撕裂前的最后完整事件继续
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
    assert seqs[0] == 1 and min(s for s in seqs if s > last_seq) == last_seq + 1
    assert resumed.folded()["status"] == "done"
    # 撕裂行已被截掉:文件里每行都是完整 JSON
    for line in open(run_dir / "events.jsonl", encoding="utf-8"):
        if line.strip():
            json.loads(line)


# ── 🟠3:并行分支失败后续跑 ───────────────────────────────────


def test_parallel_sibling_failure_resume_keeps_completed_sibling(tmp_path):
    """实测语义(比审查时的推测更强):right 失败时,left 在同一超步里的
    成果仍被 checkpoint——续跑只重执行失败的分支,left 不重跑、产物不变。
    """
    fake = FakeProvider()
    fake.configure("primary", text="拆解完成")
    fake.configure("left", text="左第一稿")
    fake.configure("right", transport_error="崩溃于并行分支")
    fake.configure("joiner", text="汇总")

    with pytest.raises(AllCandidatesFailed):
        execute_graph(load_graph("parallel"), task=TASK_TEXT,
                      runs_root=tmp_path, registry=make_registry(fake))
    run_dir = next(tmp_path.glob("*/events.jsonl")).parent
    first_done = EventReader(run_dir / "events.jsonl").find(
        type="node_done", node="left")
    first_path, first_sha = first_done["output_path"], first_done["output_sha256"]
    first_bytes = open(first_path, "rb").read()

    fake.configure("right", text="右方向结论(修复后)")
    resumed = _resume_graph_replay(run_dir.name, _test_only=True, spec=load_graph("parallel"),
                           runs_root=tmp_path, registry=make_registry(fake))
    assert resumed.folded()["status"] == "done"

    # left 只有一次 node_done,产物与哈希分毫未动(A4 语义在并行下同样成立)
    assert len(resumed.events.filter(type="node_done", node="left")) == 1
    assert open(first_path, "rb").read() == first_bytes
    assert sha256_bytes(first_bytes) == first_sha
    # join 收到了两边的完整产物(task + left + right)
    join_in = resumed.events.find(type="node_input", node="join")
    assert {c["name"] for c in join_in["consumed"]} == {
        "task", "left.output", "right.output"}


def test_unique_path_never_overwrites(tmp_path):
    """进程被硬杀(超步完全未提交)时,续跑会重跑整个超步——
    这时同名产物必须落新文件而不是覆盖(write-once,红线 ③ 的防御层)。"""
    from atlas.integrity import _unique_path

    d = tmp_path / "artifacts"
    d.mkdir()
    (d / "node_a.output.1.txt").write_bytes("第一次的产物".encode("utf-8"))
    p2 = _unique_path(d, "node_a.output.1.txt")
    assert p2.name == "node_a.output.1.r2.txt"
    p2.write_bytes("重跑的产物".encode("utf-8"))
    p3 = _unique_path(d, "node_a.output.1.txt")
    assert p3.name == "node_a.output.1.r3.txt"
    # 第一次的文件原样还在
    assert (d / "node_a.output.1.txt").read_bytes() == "第一次的产物".encode("utf-8")


# ── 🟠1/🟠4 + 🟡:Web 安全面 ───────────────────────────────────


@pytest.fixture
def secure_client(tmp_path):
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "demo.yaml").write_text(
        "name: demo\nnodes:\n  - id: a\n    type: llm\n    model: Fake:primary\n"
        "    prompt: p\n    consumes: [task]\nedges:\n  - from: a\n    to: END\n",
        encoding="utf-8")
    fake = FakeProvider()
    fake.configure("primary", text="ok")
    app = create_app(workflows_dir=workflows, runs_dir=tmp_path / "runs",
                     registry_factory=lambda pids: make_registry(fake))
    return TestClient(app, base_url="http://127.0.0.1")


def test_post_without_atlas_header_rejected(secure_client):
    """浏览器里恶意网页的 no-cors 简单请求(无自定义头)必须被拒。"""
    resp = secure_client.post("/api/workflows/demo/run",
                              json={"task": "x"},
                              headers={"X-Atlas-Request": ""})
    assert resp.status_code == 403
    assert "X-Atlas-Request" in resp.json()["detail"]


def test_post_with_header_passes(secure_client):
    resp = secure_client.post("/api/workflows/demo/run",
                              json={"task": "hello"},
                              headers={"X-Atlas-Request": "1"})
    assert resp.status_code == 200
    assert resp.json()["run_id"]


def test_bad_host_header_rejected(secure_client):
    """DNS rebinding 防护:Host 不是本机地址 → 403。"""
    resp = secure_client.get("/api/workflows",
                             headers={"Host": "evil.example.com:8321"})
    assert resp.status_code == 403


def test_ipv6_loopback_host_header_accepted(secure_client):
    """[::1]:8321 的端口冒号在方括号内,解析不能把白名单项截成 '[::1'。"""
    resp = secure_client.get("/api/workflows",
                             headers={"Host": "[::1]:8321"})
    assert resp.status_code == 200
    denied = secure_client.get("/api/workflows",
                               headers={"Host": "[::2]:8321"})
    assert denied.status_code == 403


def test_rid_backslash_traversal_rejected(secure_client):
    """Windows 反斜杠也是路径分隔符:rid 里的穿越必须 404,不落盘等待。"""
    for bad in ("..%5C..%5Cconfig", "..\\..\\config", "x/y", "a..b"):
        resp = secure_client.get(f"/api/runs/{bad}/events")
        assert resp.status_code == 404, bad


def test_wid_traversal_rejected(secure_client):
    resp = secure_client.get("/api/workflows/..%5C..%5Cconfig%5Cproviders")
    assert resp.status_code == 404


# ── 🟡7/🟡8:续跑校验与互斥 ───────────────────────────────────


def test_resume_rejects_modified_spec(tmp_path):
    fake = standard_fake(100)
    fake.configure("third", transport_error="崩溃")
    with pytest.raises(AllCandidatesFailed):
        execute_graph(load_graph("three_node"), task=TASK_TEXT,
                      runs_root=tmp_path, registry=make_registry(fake))
    run_dir = next(tmp_path.glob("*/events.jsonl")).parent

    # 用改过的图(prompt 不同)续跑 → 拒绝
    spec = load_graph("three_node")
    from dataclasses import replace
    from atlas.spec import NodeSpec
    modified = replace(
        spec,
        nodes=[replace(n, prompt=n.prompt + " (改过)") if n.id == "node_c" else n
               for n in spec.nodes])
    with pytest.raises(SpecError, match="spec_sha256"):
        _resume_graph_replay(run_dir.name, _test_only=True, spec=modified,
                     runs_root=tmp_path, registry=make_registry(fake))


def test_resume_lock_prevents_concurrent_writers(tmp_path):
    from atlas.engine import (RunConflictError, acquire_run_lock,
                              release_run_lock)

    fake = standard_fake(100)
    fake.configure("third", transport_error="崩溃")
    with pytest.raises(AllCandidatesFailed):
        execute_graph(load_graph("three_node"), task=TASK_TEXT,
                      runs_root=tmp_path, registry=make_registry(fake))
    run_dir = next(tmp_path.glob("*/events.jsonl")).parent
    acquire_run_lock(run_dir.name, runs_root=tmp_path)
    try:
        fake.configure("third", text="ok")
        with pytest.raises(RunConflictError, match="运行锁"):
            _resume_graph_replay(run_dir.name, _test_only=True, spec=load_graph("three_node"),
                         runs_root=tmp_path, registry=make_registry(fake))
    finally:
        release_run_lock(run_dir.name, runs_root=tmp_path)


def test_old_lock_mtime_neither_blocks_nor_grants_ownership(tmp_path):
    from atlas.engine import run_lock_path

    fake = standard_fake(100)
    fake.configure("third", transport_error="崩溃")
    with pytest.raises(AllCandidatesFailed):
        execute_graph(load_graph("three_node"), task=TASK_TEXT,
                      runs_root=tmp_path, registry=make_registry(fake))
    run_dir = next(tmp_path.glob("*/events.jsonl")).parent
    lock = run_lock_path(run_dir.name, runs_root=tmp_path)
    old = time.time() - 7200
    import os
    os.utime(lock, (old, old))

    fake.configure("third", text="ok")
    resumed = _resume_graph_replay(run_dir.name, _test_only=True, spec=load_graph("three_node"),
                           runs_root=tmp_path, registry=make_registry(fake))
    assert resumed.folded()["status"] == "done"
    assert lock.is_file()


def test_event_reader_incremental_offset_and_torn_tail(tmp_path):
    log = EventLog(tmp_path)
    log.emit("first", value="中文")
    reader = EventReader(log.path)
    first, offset = reader.read_from(0)
    assert [e["type"] for e in first] == ["first"]

    with open(log.path, "ab") as f:
        f.write(b'{"seq": 2, "type": "torn"')
    torn, same_offset = reader.read_from(offset)
    assert torn == [] and same_offset == offset

    with open(log.path, "r+b") as f:
        f.truncate(offset)
    continued = EventLog(tmp_path, continue_seq=True)
    continued.emit("second")
    second, final_offset = reader.read_from(offset)
    assert [e["type"] for e in second] == ["second"]
    assert final_offset > offset


def test_event_limits_fail_loudly(tmp_path, monkeypatch):
    from atlas import events as events_mod
    from atlas.events import EventLimitError

    monkeypatch.setattr(events_mod, "EVENT_RECORD_MAX_BYTES", 100)
    log = EventLog(tmp_path / "record")
    with pytest.raises(EventLimitError, match="单事件上限"):
        log.emit("large", payload="x" * 200)
    assert not log.path.exists()

    monkeypatch.setattr(events_mod, "EVENT_RECORD_MAX_BYTES", 1024)
    monkeypatch.setattr(events_mod, "EVENT_FILE_MAX_BYTES", 150)
    log = EventLog(tmp_path / "ledger")
    log.emit("one", payload="x" * 20)
    with pytest.raises(EventLimitError, match="事件账本"):
        log.emit("two", payload="y" * 100)


def test_event_reader_rejects_oversized_ledger(tmp_path, monkeypatch):
    """读侧同样拒绝超限账本:外部写入绕过写侧上限时 fail-loud。"""
    from atlas import events as events_mod
    from atlas.events import EventLimitError

    log = EventLog(tmp_path)
    log.emit("ok")
    reader = EventReader(log.path)
    assert [e["type"] for e in reader.all()] == ["ok"]

    monkeypatch.setattr(events_mod, "EVENT_FILE_MAX_BYTES", 10)
    with pytest.raises(EventLimitError, match="超过上限"):
        reader.all()
    with pytest.raises(EventLimitError, match="超过上限"):
        reader.read_from(0)


def test_resume_replay_rejects_default_call(tmp_path):
    """P3-E:非产品重放路径默认响亮拒绝,_test_only=True 才可用——
    把"误用绕过 interrupted 准入"从能跑变成当场报错(体验债收敛)。"""
    from atlas.engine import RunConflictError, execute_graph

    result = execute_graph(load_graph("two_node"), task=TASK_TEXT,
                           runs_root=tmp_path,
                           registry=make_registry(standard_fake()))
    with pytest.raises(RunConflictError, match="测试专用"):
        # 故意不带 _test_only:默认必须响亮拒绝
        _resume_graph_replay(result.run_id, spec=load_graph("two_node"),
                             runs_root=tmp_path,
                             registry=make_registry(standard_fake()))
