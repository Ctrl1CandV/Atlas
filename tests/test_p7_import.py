# -*- coding: utf-8 -*-
"""P7 · artifact import 与 invocation hash。

合同(ROADMAP §7):
① 准入在源 run stable lock 内校验(静稳终态/provenance/哈希)后字节复制,
   写后复验;复制中途 kill 无半产物;
② artifact_imported lineage(source run/name/hash、新 path/hash、算法版本);
③ invocation_sha256 覆盖节点执行字段/有效 prompt/有序输入/后端身份,
   记入 node_started;只有身份完全相等才自动 skip(node_imported_reused);
④ 删源 run 后新 run 仍可完成;prompt/model/输入/后端任一改变都不复用;
   与源删除/锁竞争时行为确定(fail-closed,不等待)。
"""
import json
import shutil
from pathlib import Path

import pytest

from atlas.adapters import FakeProvider
from atlas.engine import execute_graph
from atlas.events import EventReader, fold_events
from atlas.integrity import IntegrityError, sha256_bytes
from atlas.spec import spec_from_yaml

from conftest import TASK_TEXT, make_registry, standard_fake

PRODUCER_PROMPT = "产出上游结果。"

PRODUCER_YAML = """
name: producer
nodes:
  - id: a
    type: llm
    model: Fake:primary
    prompt: 产出上游结果。
    consumes: [task]
    output_schema:
      required: [summary]
edges:
  - from: a
    to: END
"""


def _run_producer(runs_root):
    return execute_graph(spec_from_yaml(PRODUCER_YAML), task=TASK_TEXT,
                         runs_root=runs_root,
                         registry=make_registry(standard_fake()))


def _importer_yaml(source_run: str, *, prompt: str = PRODUCER_PROMPT,
                   model: str = "Fake:primary") -> str:
    return f"""
name: importer
nodes:
  - id: a
    type: llm
    model: {model}
    prompt: {prompt}
    consumes: [task]
    output_schema:
      required: [summary]
    imports:
      - run: {source_run}
        name: a.output
edges:
  - from: a
    to: END
"""


def _run_ids(root) -> list:
    root = Path(root)
    return sorted(d.name for d in root.iterdir()
                  if d.is_dir() and not d.name.startswith("."))


# ───────────── ① 复制与 lineage ─────────────


def test_import_copies_bytes_and_writes_lineage(tmp_path):
    src = _run_producer(tmp_path)
    result = execute_graph(spec_from_yaml(_importer_yaml(src.run_id)),
                           task=TASK_TEXT, runs_root=tmp_path,
                           registry=make_registry(standard_fake()))
    events = EventReader(result.dir / "events.jsonl")
    lineage = events.find(type="artifact_imported")
    assert lineage is not None
    assert lineage["source_run"] == src.run_id
    assert lineage["source_name"] == "a.output"
    assert lineage["algo_version"] == "p7-import-v1"
    src_done = EventReader(src.dir / "events.jsonl").find(
        type="node_done", node="a")
    cloned = Path(lineage["path"]).read_bytes()
    assert sha256_bytes(cloned) == src_done["output_sha256"] == lineage["sha256"]
    assert Path(lineage["path"]).is_relative_to(result.dir)


def test_import_then_delete_source_clone_survives(tmp_path):
    """验收④:删除源 run,克隆副本仍可哈希复验(不再指向源路径)。"""
    src = _run_producer(tmp_path)
    result = execute_graph(spec_from_yaml(_importer_yaml(src.run_id)),
                           task=TASK_TEXT, runs_root=tmp_path,
                           registry=make_registry(standard_fake()))
    assert result.folded()["status"] == "done"
    shutil.rmtree(Path(src.dir))
    lineage = EventReader(result.dir / "events.jsonl").find(
        type="artifact_imported")
    cloned_path = Path(lineage["path"])
    assert cloned_path.exists()
    assert sha256_bytes(cloned_path.read_bytes()) == lineage["sha256"]


# ───────────── ③ skip 语义 ─────────────


def test_invocation_equal_skips_execution_without_model_calls(tmp_path):
    """invocation 完全相等:被顶替节点零调用,账本记 node_imported_reused。"""
    src = _run_producer(tmp_path)
    result = execute_graph(spec_from_yaml(_importer_yaml(src.run_id)),
                           task=TASK_TEXT, runs_root=tmp_path,
                           registry=make_registry(standard_fake()))
    events = EventReader(result.dir / "events.jsonl")
    reused = events.find(type="node_imported_reused", node="a")
    assert reused is not None
    src_started = EventReader(src.dir / "events.jsonl").find(
        type="node_started", node="a")
    assert src_started["invocation_sha256"] is not None
    assert reused["invocation_sha256"] == src_started["invocation_sha256"]
    # 没有真实执行:无 node_done、无候选失败;图正常收尾
    assert events.find(type="node_done", node="a") is None
    assert events.filter(type="model_failed") == []
    assert result.folded()["status"] == "done"
    # reused 事件 fold 反例:删掉后终态不变
    stripped = [r for r in events.all()
                if r["type"] not in ("artifact_imported", "node_imported_reused")]
    assert fold_events(events.all()) == fold_events(stripped)


def test_any_change_defeats_reuse(tmp_path):
    """验收④反面:prompt/task/model 任一改变 → invocation 不等 → 不复用。"""
    src = _run_producer(tmp_path)

    # ① prompt 改变(同 runs_root:run id 唯一,不会撞)
    r1 = execute_graph(
        spec_from_yaml(_importer_yaml(src.run_id, prompt="换个说法的任务。")),
        task=TASK_TEXT, runs_root=tmp_path,
        registry=make_registry(standard_fake()))
    assert EventReader(r1.dir / "events.jsonl").find(
        type="node_imported_reused") is None
    assert EventReader(r1.dir / "events.jsonl").find(
        type="node_done", node="a") is not None

    # ② task 改变(输入哈希变)
    r2 = execute_graph(spec_from_yaml(_importer_yaml(src.run_id)),
                       task="完全不同的任务文本",
                       runs_root=tmp_path,
                       registry=make_registry(standard_fake()))
    assert EventReader(r2.dir / "events.jsonl").find(
        type="node_imported_reused") is None

    # ③ model 改变
    r3 = execute_graph(
        spec_from_yaml(_importer_yaml(src.run_id, model="Fake:fallback")),
        task=TASK_TEXT, runs_root=tmp_path,
        registry=make_registry(standard_fake()))
    assert EventReader(r3.dir / "events.jsonl").find(
        type="node_imported_reused") is None


# ───────────── ① 反例与竞争 ─────────────


def test_schema_change_defeats_reuse(tmp_path):
    """审查阻塞项回归锁(2026-08-27):required_fields 是执行等价性的
    决定因子——源用 [summary] 验收产出的产物,导入方把 schema 改成
    [summary, verdict] 后 invocation 必须不等,不得复用(同一模型输出在
    更严 schema 下可能本应 DegradedOutput,复用会把校验失败记成成功)。"""
    src = _run_producer(tmp_path)
    stricter = _importer_yaml(src.run_id).replace(
        "required: [summary]", "required: [summary, verdict]")
    result = execute_graph(spec_from_yaml(stricter), task=TASK_TEXT,
                           runs_root=tmp_path,
                           registry=make_registry(standard_fake()))
    events = EventReader(result.dir / "events.jsonl")
    assert events.find(type="node_imported_reused") is None
    # schema 更严的节点真实执行了(standard_fake 的输出恰好带 verdict,
    # 会成功——但成功是它自己跑出来的,不是复用来的)
    assert events.find(type="node_done", node="a") is not None


def test_multi_node_source_imports_producer_own_bytes(tmp_path):
    """2026-08-27 P13 多节点源 fork 实测逼出的回归锁:_latest_artifact_entry
    的兜底分支曾只查"事件是 node_done 且逻辑名 .output 结尾",没核对事件
    节点就是生产者——多节点源里倒序扫到别的节点的 node_done,会把别人
    的产物哈希/字节当成目标返回(单节点源测试从未踩中)。"""
    two_node_yaml = """
name: two_node
nodes:
  - id: a
    type: llm
    model: Fake:primary
    prompt: 第一步。
    consumes: [task]
    output_schema:
      required: [summary]
  - id: z
    type: llm
    model: Fake:other
    prompt: 第二步。
    consumes: [task, a.output]
    output_schema:
      required: [summary]
edges:
  - from: a
    to: z
  - from: z
    to: END
"""
    from conftest import make_registry as _mk
    fake = FakeProvider()
    fake.configure("primary", text=json.dumps(
        {"summary": "第一步产出"}, ensure_ascii=False))
    fake.configure("other", text=json.dumps(
        {"summary": "第二步产出,内容不同"}, ensure_ascii=False))
    src = execute_graph(spec_from_yaml(two_node_yaml), task=TASK_TEXT,
                        runs_root=tmp_path, registry=_mk(fake))
    result = execute_graph(spec_from_yaml(_importer_yaml(src.run_id)),
                           task=TASK_TEXT, runs_root=tmp_path,
                           registry=_mk(fake))
    events = EventReader(result.dir / "events.jsonl")
    lineage = events.find(type="artifact_imported")
    src_done = EventReader(src.dir / "events.jsonl").find(
        type="node_done", node="a")
    src_z = EventReader(src.dir / "events.jsonl").find(
        type="node_done", node="z")
    assert lineage["sha256"] == src_done["output_sha256"]
    assert lineage["sha256"] != src_z["output_sha256"]
    assert Path(lineage["path"]).read_bytes() == Path(src_done["output_path"]).read_bytes()


def test_consumes_order_enters_invocation(tmp_path):
    """2026-08-27 审查建议采纳(v3):build_projection 按 consumes 列表顺序
    内联上游产物字节——[task, a.output] 与 [a.output, task] 的投影布局
    不同,是两次不同执行,invocation 必须不等(此前按 name 排序会误判
    相等并跳过布局不同的执行)。"""
    two_node = """
name: ordered_src
nodes:
  - id: a
    type: llm
    model: Fake:primary
    prompt: 第一步。
    consumes: [task]
    output_schema:
      required: [summary]
  - id: b
    type: llm
    model: Fake:other
    prompt: 第二步。
    consumes: [task, a.output]
    output_schema:
      required: [summary]
edges:
  - from: a
    to: b
  - from: b
    to: END
"""
    fake = FakeProvider()
    fake.configure("primary", text=json.dumps(
        {"summary": "第一步产出"}, ensure_ascii=False))
    fake.configure("other", text=json.dumps(
        {"summary": "第二步产出"}, ensure_ascii=False))
    reg = make_registry(fake)
    src = execute_graph(spec_from_yaml(two_node), task=TASK_TEXT,
                        runs_root=tmp_path, registry=reg)

    def importer(consumes_line: str) -> str:
        return f"""
name: ordered_importer
nodes:
  - id: a
    type: llm
    model: Fake:primary
    prompt: 第一步。
    consumes: [task]
    output_schema:
      required: [summary]
    imports:
      - run: {src.run_id}
        name: a.output
  - id: b
    type: llm
    model: Fake:other
    prompt: 第二步。
    {consumes_line}
    output_schema:
      required: [summary]
    imports:
      - run: {src.run_id}
        name: b.output
edges:
  - from: a
    to: b
  - from: b
    to: END
"""

    # 同布局([task, a.output]):与源 b 身份完全相等 → 合法复用(a 同理)
    same = execute_graph(
        spec_from_yaml(importer("consumes: [task, a.output]")),
        task=TASK_TEXT, runs_root=tmp_path, registry=reg)
    events_same = EventReader(same.dir / "events.jsonl")
    assert events_same.find(type="node_imported_reused", node="a") is not None
    assert events_same.find(type="node_imported_reused", node="b") is not None

    # 反序布局([a.output, task]):投影布局不同 → 不同执行 → b 真实重跑
    # (a 的身份不受影响,仍合法复用)
    flipped = execute_graph(
        spec_from_yaml(importer("consumes: [a.output, task]")),
        task=TASK_TEXT, runs_root=tmp_path, registry=reg)
    events = EventReader(flipped.dir / "events.jsonl")
    assert events.find(type="node_imported_reused", node="a") is not None
    assert events.find(type="node_imported_reused", node="b") is None
    assert events.find(type="node_done", node="b") is not None


def test_rejects_missing_and_running_source(tmp_path):
    """缺失源/运行中源 fail-closed;失败不留下 run 目录。"""
    with pytest.raises(Exception):
        execute_graph(spec_from_yaml(_importer_yaml("20990101-000000-nope")),
                      task=TASK_TEXT, runs_root=tmp_path,
                      registry=make_registry(standard_fake()))
    assert _run_ids(tmp_path) == []

    # 持久 running 的源:只有 run_started、无终态
    running_dir = tmp_path / "20260827-000000-running"
    running_dir.mkdir()
    (running_dir / "events.jsonl").write_text(json.dumps({
        "seq": 1, "ts": "t", "type": "run_started", "run_id": "x",
        "graph": "g"}) + "\n", encoding="utf-8")
    with pytest.raises(Exception, match="静稳|终态"):
        execute_graph(spec_from_yaml(_importer_yaml(running_dir.name)),
                      task=TASK_TEXT, runs_root=tmp_path,
                      registry=make_registry(standard_fake()))
    assert _run_ids(tmp_path) == ["20260827-000000-running"]


def test_source_lock_conflict_fails_deterministically(tmp_path):
    """验收④:源锁被并发持有时启动当场失败,不等待、不留半导入。"""
    from atlas.engine import acquire_run_lock, release_run_lock

    src = _run_producer(tmp_path)
    acquire_run_lock(src.run_id, runs_root=tmp_path)   # 模拟并发持有
    try:
        with pytest.raises(Exception):
            execute_graph(spec_from_yaml(_importer_yaml(src.run_id)),
                          task=TASK_TEXT, runs_root=tmp_path / "blocked",
                          registry=make_registry(standard_fake()))
    finally:
        release_run_lock(src.run_id, runs_root=tmp_path)
    blocked = tmp_path / "blocked"
    assert not blocked.exists() or _run_ids(blocked) == []


def test_copy_integrity_drift_leaves_no_partial(tmp_path):
    """验收①:源字节在锁内漂移 → 复验拦截,无 .partial/半产物残留。"""
    from atlas.artifacts import copy_imported_artifact

    src = _run_producer(tmp_path)
    done = EventReader(src.dir / "events.jsonl").find(type="node_done", node="a")
    Path(done["output_path"]).write_bytes(b"tampered")
    target_run = tmp_path / "crash-target"
    target_run.mkdir()
    with pytest.raises(IntegrityError, match="不符"):
        copy_imported_artifact(
            source_path=Path(done["output_path"]),
            source_sha256=done["output_sha256"],
            run_dir=target_run, name="a.output")
    artifacts = target_run / "artifacts"
    leftovers = list(artifacts.glob("*")) if artifacts.exists() else []
    assert leftovers == [], f"不应有残留文件:{leftovers}"
