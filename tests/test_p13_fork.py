# -*- coding: utf-8 -*-
"""P13 · fork 与失效闭包。

合同(ROADMAP §8):
① 比较源 snapshot/invocation identities 得到 changed set;静态图上取
   changed + 全部后代为 invalidation closure;循环按强连通分量整体失效;
② 闭包内禁止 import/skip;闭包外只有 identity 相等且依赖完整才复制;
③ join 依赖 changed 分支时必须重跑(不能先 pin 全部再意外跳过目标节点);
④ lineage、changed set、closure、import map 与算法版本进入 dry-run、
   事件(fork_planned)与执行身份(fork.run 进 spec 指纹);
⑤ 五类图(线性/并行/join/条件边/循环)分别验证;failed/paused 源只能
   导入事件证明完整的产物。

附带回归锁(P13 顺带修的 P7 缺口):静态 skip 计划的输入哈希是"导入
克隆"口径的预测——运行时同名产物被真实重跑覆盖时,跳过前必须复核,
过期就委托真实执行。
"""
import json
from pathlib import Path

import pytest

from atlas.adapters import FakeProvider
from atlas.engine import execute_graph
from atlas.events import EventReader, fold_events
from atlas.fork import FORKABLE_STATUSES, compute_fork_plan
from atlas.spec import (SpecError, spec_from_yaml, spec_fingerprint,
                        spec_from_snapshot, spec_to_snapshot)

from conftest import TASK_TEXT, make_registry, standard_fake

LINEAR_YAML = """
name: linear
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
    prompt: 第二步,阅读上游。
    consumes: [task, a.output]
    output_schema:
      required: [summary]
  - id: c
    type: llm
    model: Fake:other
    prompt: 第三步,阅读上游。
    consumes: [task, b.output]
    output_schema:
      required: [summary]
edges:
  - from: a
    to: b
  - from: b
    to: c
  - from: c
    to: END
"""


def _linear_fake() -> FakeProvider:
    fake = FakeProvider()
    fake.configure("primary",
                   text=json.dumps({"summary": "第一步产出"}, ensure_ascii=False))
    fake.configure("other",
                   text=json.dumps({"summary": "后续产出"}, ensure_ascii=False))
    return fake


def _run(yaml_text: str, runs_root, *, task: str = TASK_TEXT,
         fake: FakeProvider | None = None):
    return execute_graph(spec_from_yaml(yaml_text), task=task,
                         runs_root=runs_root,
                         registry=make_registry(fake or _linear_fake()))


def _fork_yaml(base_yaml: str, source_run: str, **edits: str) -> str:
    """在基础图上挂 fork 声明;edits 形如 b='新提示词' 替换该节点的
    prompt(prompt 行在节点块内,按当前节点块顺序定位替换)。"""
    text = base_yaml.rstrip() + f"\nfork:\n  run: {source_run}\n"
    lines = text.splitlines()
    current_node = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("- id: "):
            current_node = stripped[len("- id: "):]
        elif stripped.startswith("prompt:") and current_node in edits:
            lines[i] = (line[: len(line) - len(stripped)]
                        + f"prompt: {edits[current_node]}")
    return "\n".join(lines) + "\n"


def _reused_nodes(result) -> list[str]:
    events = EventReader(Path(result.dir) / "events.jsonl")
    return sorted({e["node"] for e in events.filter(type="node_imported_reused")})


def _planned(result) -> dict:
    events = EventReader(Path(result.dir) / "events.jsonl")
    planned = events.find(type="fork_planned")
    assert planned is not None
    return planned


def _run_ids(root) -> list:
    root = Path(root)
    return sorted(d.name for d in root.iterdir()
                  if d.is_dir() and not d.name.startswith("."))


# ───────────── spec 层:解析 / 指纹 / 快照 ─────────────


def test_fork_spec_shape_errors_and_fingerprint():
    base = LINEAR_YAML

    ok = spec_from_yaml(base + "fork:\n  run: 20260827-000000-aaaaaa\n")
    assert ok.fork is not None and ok.fork.run == "20260827-000000-aaaaaa"
    # 形状与格式错误在解析期拒绝(零成本)
    with pytest.raises(SpecError, match="fork"):
        spec_from_yaml(base + "fork: 20260827-000000-aaaaaa\n")
    with pytest.raises(SpecError, match="未知字段"):
        spec_from_yaml(base + "fork:\n  run: 20260827-000000-aaaaaa\n  why: x\n")
    with pytest.raises(SpecError, match="fork.run"):
        spec_from_yaml(base + "fork:\n  run: 不是合法id!\n")

    plain = spec_from_yaml(base)
    # 兼容合同:未 fork 的旧图指纹零变化;fork.run 是执行身份的一部分
    assert ok.fork is not None
    assert spec_fingerprint(plain) != spec_fingerprint(ok)
    # 快照往返:fork 声明随快照恢复(续跑/批复拿同一 fork 身份)
    restored = spec_from_snapshot(spec_to_snapshot(ok))
    assert restored.fork == ok.fork
    assert spec_fingerprint(restored) == spec_fingerprint(ok)


# ───────────── ①②③ 五类图 ─────────────


def test_linear_unchanged_fork_reuses_everything(tmp_path):
    """线性图原样 fork:三个节点全部 node_imported_reused,零真实执行。"""
    src = _run(LINEAR_YAML, tmp_path)
    result = _run(_fork_yaml(LINEAR_YAML, src.run_id), tmp_path)
    assert result.folded()["status"] == "done"
    assert _reused_nodes(result) == ["a", "b", "c"]
    events = EventReader(Path(result.dir) / "events.jsonl")
    assert events.filter(type="model_failed") == []
    assert events.filter(type="node_done") == []
    # ④ fork_planned 全量入账,run_started 带计划摘要身份
    planned = _planned(result)
    assert planned["source_run"] == src.run_id
    assert planned["source_status"] == "done"
    assert planned["changed"] == [] and planned["closure"] == []
    assert planned["import_map"] == [
        {"run": src.run_id, "name": "a.output"},
        {"run": src.run_id, "name": "b.output"},
        {"run": src.run_id, "name": "c.output"}]
    assert planned["algo_version"] == "p13-fork-v1"
    started = events.find(type="run_started")
    assert started["fork_source_run"] == src.run_id
    assert started["fork_plan_sha256"] == planned["fork_plan_sha256"]
    # fold 反例:删掉 fork_planned 后终态不变
    stripped = [r for r in events.all() if r["type"] != "fork_planned"]
    assert fold_events(events.all()) == fold_events(stripped)


def test_linear_change_middle_reruns_suffix_only(tmp_path):
    """改中间节点 prompt:changed={b,c}(c 输入必变),只有 a 复用。"""
    src = _run(LINEAR_YAML, tmp_path)
    forked = _fork_yaml(LINEAR_YAML, src.run_id,
                        b="第二步,换个说法,阅读上游。")
    result = _run(forked, tmp_path)
    assert result.folded()["status"] == "done"
    assert _reused_nodes(result) == ["a"]
    planned = _planned(result)
    assert planned["changed"] == ["b", "c"]
    assert planned["closure"] == ["b", "c"]
    events = EventReader(Path(result.dir) / "events.jsonl")
    assert {e["node"] for e in events.filter(type="node_done")} == {"b", "c"}


def test_parallel_change_one_branch_protects_join(tmp_path):
    """并行双分支 + join:改左分支 → 左腿与 join 重跑,右腿与公共上游复用。
    ③ join 命中 changed 分支,绝不允许被跳过(P13 核心验收)。"""
    yaml_par = """
name: parallel
nodes:
  - id: base
    type: llm
    model: Fake:primary
    prompt: 公共上游。
    consumes: [task]
    output_schema:
      required: [summary]
  - id: left
    type: llm
    model: Fake:other
    prompt: 左腿。
    consumes: [task, base.output]
    output_schema:
      required: [summary]
  - id: right
    type: llm
    model: Fake:other
    prompt: 右腿。
    consumes: [task, base.output]
    output_schema:
      required: [summary]
  - id: join
    type: llm
    model: Fake:other
    prompt: 合流。
    consumes: [task, left.output, right.output]
    output_schema:
      required: [summary]
edges:
  - from: base
    to: left
  - from: base
    to: right
  - from: left
    to: join
  - from: right
    to: join
  - from: join
    to: END
"""
    src = _run(yaml_par, tmp_path)
    forked = _fork_yaml(yaml_par, src.run_id, left="左腿,本次改写。")
    result = _run(forked, tmp_path)
    assert result.folded()["status"] == "done"
    # 安全的兄弟分支结果被保留;join 必须重跑
    assert _reused_nodes(result) == ["base", "right"]
    planned = _planned(result)
    assert planned["changed"] == ["join", "left"]
    assert planned["closure"] == ["join", "left"]
    events = EventReader(Path(result.dir) / "events.jsonl")
    assert {e["node"] for e in events.filter(type="node_done")} == {"join", "left"}


def test_conditional_router_reruns_and_reuses_deterministic_legs(tmp_path):
    """条件边:路由节点有条件出边 → 永不是 skip 候选,必须真实重跑;
    下游腿拿到合成导入后,按运行时输入复核决定去留(确定性 fake 的
    路由输出字节不变 → 输入哈希相等 → 合法复用)。"""
    yaml_cond = """
name: conditional
nodes:
  - id: u
    type: llm
    model: Fake:primary
    prompt: 判定路由。
    consumes: [task]
    output_schema:
      required: [verdict]
  - id: c
    type: llm
    model: Fake:other
    prompt: ok 腿,阅读上游。
    consumes: [task, u.output]
    output_schema:
      required: [summary]
  - id: t2
    type: llm
    model: Fake:other
    prompt: bad 腿。
    consumes: [task]
    output_schema:
      required: [summary]
edges:
  - from: u
    to: c
    when: ok
  - from: u
    to: t2
    when: bad
  - from: c
    to: END
  - from: t2
    to: END
"""
    fake = FakeProvider()
    fake.configure("primary",
                   text=json.dumps({"verdict": "ok"}, ensure_ascii=False))
    fake.configure("other",
                   text=json.dumps({"summary": "腿产出"}, ensure_ascii=False))
    src = _run(yaml_cond, tmp_path, fake=fake)
    assert src.folded()["status"] == "done"

    result = _run(_fork_yaml(yaml_cond, src.run_id), tmp_path, fake=fake)
    events = EventReader(Path(result.dir) / "events.jsonl")
    # 路由节点真实重跑(条件出边永不是 skip 候选);其下游的输入供给
    # 无法静态证明(u.output 没有导入克隆)→ 按保守纪律诚实重跑,即使
    # 确定性 fake 的字节其实不变——正确性优先于小聪明
    assert events.find(type="node_done", node="u") is not None
    assert events.find(type="node_done", node="c") is not None
    assert _reused_nodes(result) == []
    planned = _planned(result)
    # t2 在源里从未执行(bad 腿没走到)→ 没有账本身份 → 落进 changed:
    # "无法证明与源相等"的诚实口径,不是错误
    assert planned["changed"] == ["t2"]
    assert planned["closure"] == ["t2"]
    assert planned["import_map"] == [{"run": src.run_id, "name": "c.output"}]
    # 改路由节点 → 条件边后代全部失效(可能的数据流都进闭包)
    changed_run = _run(_fork_yaml(yaml_cond, src.run_id,
                                  u="判定路由,换个提示词。"),
                       tmp_path, fake=fake)
    planned2 = _planned(changed_run)
    assert planned2["changed"] == ["c", "t2", "u"]
    assert planned2["closure"] == ["c", "t2", "u"]
    assert _reused_nodes(changed_run) == []


def test_loop_scc_wholesale_invalidation(tmp_path):
    """循环图:SCC 内任一节点 changed → 整个 SCC 失效(不做内部分
    保留),SCC 之外的上游照常复用。单元级直调 compute_fork_plan,
    避免无限循环执行的不稳定。"""
    loop_yaml = """
name: loop
nodes:
  - id: pre
    type: llm
    model: Fake:primary
    prompt: 循环前的稳定上游。
    consumes: [task]
    output_schema:
      required: [summary]
  - id: a
    type: llm
    model: Fake:other
    prompt: 循环体甲。
    consumes: [task, pre.output]
    output_schema:
      required: [summary]
  - id: b
    type: llm
    model: Fake:other
    prompt: 循环体乙。
    consumes: [task, a.output]
    output_schema:
      required: [summary, verdict]
edges:
  - from: pre
    to: a
  - from: a
    to: b
  - from: b
    to: a
    when: loop
  - from: b
    to: END
    when: done
guards:
  max_iterations: 2
"""
    from atlas.integrity import sha256_bytes
    # 源:pre/a/b 线性跑完(pre 与环里 a/b 的节点定义完全一致)
    src_yaml = """
name: loop_src
nodes:
  - id: pre
    type: llm
    model: Fake:primary
    prompt: 循环前的稳定上游。
    consumes: [task]
    output_schema:
      required: [summary]
  - id: a
    type: llm
    model: Fake:other
    prompt: 循环体甲。
    consumes: [task, pre.output]
    output_schema:
      required: [summary]
  - id: b
    type: llm
    model: Fake:other
    prompt: 循环体乙。
    consumes: [task, a.output]
    output_schema:
      required: [summary, verdict]
edges:
  - from: pre
    to: a
  - from: a
    to: b
  - from: b
    to: END
"""
    # 源图的 b 要求 verdict(给环的路由字段留口子),fake 输出跟上
    loop_fake = FakeProvider()
    loop_fake.configure("primary", text=json.dumps(
        {"summary": "pre 产出"}, ensure_ascii=False))
    loop_fake.configure("other", text=json.dumps(
        {"summary": "环体产出", "verdict": "done"}, ensure_ascii=False))
    src = _run(src_yaml, tmp_path, fake=loop_fake)
    spec = spec_from_yaml(_fork_yaml(loop_yaml, src.run_id,
                                     a="循环体甲,本次改写。"))
    from atlas.engine import prepare_execution
    registry = make_registry(loop_fake)
    prepared = prepare_execution(spec, registry)
    plan = compute_fork_plan(
        spec=spec, source_run=src.run_id, runs_root=tmp_path,
        task_sha256=sha256_bytes(TASK_TEXT.encode("utf-8")),
        backend_sha256=prepared.backend_sha256)
    # b 自身身份可证相等,但与 a 同处 SCC → 整体失效;pre 不在环内,
    # 身份相等且产物完整 → 合成导入
    assert plan["changed"] == ["a", "b"]
    assert plan["closure"] == ["a", "b"]
    assert plan["imports"] == [{"run": src.run_id, "name": "pre.output"}]


# ───────────── ② 任务变化与源状态门 ─────────────


def test_task_change_invalidates_task_consumers(tmp_path):
    """换任务文本:消费 task 的节点身份必变,全链失效;task_equal 入账。"""
    src = _run(LINEAR_YAML, tmp_path)
    result = _run(_fork_yaml(LINEAR_YAML, src.run_id), tmp_path,
                  task="完全不同的任务文本")
    assert result.folded()["status"] == "done"
    assert _reused_nodes(result) == []
    planned = _planned(result)
    assert planned["task_equal"] is False
    assert planned["changed"] == ["a", "b", "c"]


def test_failed_source_reuses_only_event_proven_artifacts(tmp_path):
    """⑤ failed 源:只有事件证明完整的产物可复用(a 完成),失败的 b 与
    未跑的 c 诚实重跑;修好模型后 fork 图拿到 done 终态。on_error=stop
    的失败会向调用方抛异常——源 run 用 pytest.raises 接住再取目录。"""
    broken = FakeProvider()
    broken.configure("primary",
                     text=json.dumps({"summary": "第一步产出"}, ensure_ascii=False))
    broken.configure("other", transport_error="上游供应商挂了")
    with pytest.raises(Exception, match="均失败"):
        _run(LINEAR_YAML, tmp_path, fake=broken)
    src_run_id = _run_ids(tmp_path)[0]
    src_events = EventReader(tmp_path / src_run_id / "events.jsonl")
    assert fold_events(src_events.all())["status"] == "failed"

    result = _run(_fork_yaml(LINEAR_YAML, src_run_id), tmp_path)
    assert result.folded()["status"] == "done"
    assert _reused_nodes(result) == ["a"]
    events = EventReader(Path(result.dir) / "events.jsonl")
    assert {e["node"] for e in events.filter(type="node_done")} == {"b", "c"}
    assert _planned(result)["source_status"] == "failed"


def test_paused_source_imports_event_proven_artifacts_only(tmp_path):
    """⑤ paused 源与 failed 同门:把 done 源的账本截到 a 完成后并补一条
    run_paused,构造真实 paused 形态;只有 a 的完整产物可进复用候选。"""
    src = _run(LINEAR_YAML, tmp_path)
    events = EventReader(Path(src.dir) / "events.jsonl").all()
    a_done_at = max(i for i, e in enumerate(events)
                    if e.get("type") == "node_done" and e.get("node") == "a")
    paused_dir = tmp_path / "20260827-000000-paused"
    paused_dir.mkdir()
    (paused_dir / "spec.snapshot.json").write_text(
        Path(src.dir, "spec.snapshot.json").read_text(encoding="utf-8"),
        encoding="utf-8")
    truncated = events[: a_done_at + 1] + [{
        "seq": events[a_done_at]["seq"] + 1, "ts": "t", "type": "run_paused",
        "run_id": "20260827-000000-paused", "reason": "human"}]
    (paused_dir / "events.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in truncated),
        encoding="utf-8")
    assert fold_events(truncated)["status"] == "paused"
    assert "paused" in FORKABLE_STATUSES

    result = _run(_fork_yaml(LINEAR_YAML, paused_dir.name), tmp_path)
    assert result.folded()["status"] == "done"
    assert _reused_nodes(result) == ["a"]


def test_missing_and_running_fork_source_rejected_without_run_dir(tmp_path):
    """缺失源/运行中源 fail-closed;失败发生在目录创建之前。"""
    with pytest.raises(Exception, match="不存在"):
        _run(_fork_yaml(LINEAR_YAML, "20990101-000000-nope"), tmp_path)
    assert _run_ids(tmp_path) == []

    running_dir = tmp_path / "20260827-000000-running"
    running_dir.mkdir()
    (running_dir / "events.jsonl").write_text(json.dumps({
        "seq": 1, "ts": "t", "type": "run_started", "run_id": "x",
        "graph": "g"}) + "\n", encoding="utf-8")
    with pytest.raises(Exception, match="静稳|终态"):
        _run(_fork_yaml(LINEAR_YAML, running_dir.name), tmp_path)
    assert _run_ids(tmp_path) == ["20260827-000000-running"]


def test_closure_forbids_explicit_imports(tmp_path):
    """② 闭包内禁止 import:changed 节点还声明导入 → 启动前 SpecError。"""
    src = _run(LINEAR_YAML, tmp_path)
    forked = _fork_yaml(LINEAR_YAML, src.run_id,
                        b="第二步,换个说法,阅读上游。")
    # 给闭包内的 b 挂一条显式导入(源是真实终态 run,precheck 不会先拦)
    forked = forked.replace(
        "  - id: b\n",
        "  - id: b\n"
        f"    imports:\n      - run: {src.run_id}\n        name: a.output\n", 1)
    spec = spec_from_yaml(forked)
    with pytest.raises(SpecError, match="闭包"):
        execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                      registry=make_registry(_linear_fake()))
    # 失败不创建 run 目录(源目录之外没有新目录)
    assert _run_ids(tmp_path) == [src.run_id]


# ───────────── dry-run 明示 ─────────────


def test_dry_run_shows_fork_plan(tmp_path):
    """④ dry-run 明示闭包与复用清单("将重跑什么、将从哪个 run 复制什么")。
    不传 agent_runner_factory(与 execute_graph 默认同源),保证 backend
    身份与源 run 一致——后端不同会把全部节点判 changed。"""
    from atlas.mcp import dry_run_impl

    src = _run(LINEAR_YAML, tmp_path)
    changed_yaml = _fork_yaml(LINEAR_YAML, src.run_id,
                              b="第二步,换个说法,阅读上游。")
    out = dry_run_impl("", TASK_TEXT, yaml=changed_yaml,
                       registry_factory=lambda ids: make_registry(_linear_fake()),
                       runs_root=tmp_path)
    assert "error" not in out, out.get("error")
    fork_section = out["fork"]
    assert fork_section["source_run"] == src.run_id
    assert fork_section["changed"] == ["b", "c"]
    assert fork_section["closure"] == ["b", "c"]
    assert fork_section["imports"] == [{"run": src.run_id, "name": "a.output"}]


# ───────────── P7 缺口回归:运行时输入复核 ─────────────


_RECHECK_YAML = """
name: recheck
nodes:
  - id: u
    type: llm
    model: Fake:primary
    prompt: 上游判定。
    consumes: [task]
    output_schema:
      required: [verdict]
  - id: c
    type: llm
    model: Fake:other
    prompt: 下游总结,阅读上游。
    consumes: [task, u.output]
    output_schema:
      required: [summary]
  - id: t2
    type: llm
    model: Fake:other
    prompt: bad 腿。
    consumes: [task]
    output_schema:
      required: [summary]
edges:
  - from: u
    to: c
    when: ok
  - from: u
    to: t2
    when: bad
  - from: c
    to: END
  - from: t2
    to: END
"""


def _recheck_fake(summary_text: str) -> FakeProvider:
    fake = FakeProvider()
    fake.configure("primary", text=json.dumps(
        {"summary": summary_text, "verdict": "ok"}, ensure_ascii=False))
    fake.configure("other",
                   text=json.dumps({"summary": "下游产出"}, ensure_ascii=False))
    return fake


def _recheck_importer_yaml(source_run: str) -> str:
    """c 显式自导入 + u 显式导入:静态口径 c 的输入(旧 u 哈希的导入
    克隆)可证与源相等——这正是"运行时复核"要兜住的预测口径。"""
    text = _RECHECK_YAML
    text = text.replace(
        "  - id: u\n",
        "  - id: u\n"
        f"    imports:\n      - run: {source_run}\n        name: u.output\n", 1)
    text = text.replace(
        "  - id: c\n",
        "  - id: c\n"
        f"    imports:\n      - run: {source_run}\n        name: c.output\n", 1)
    return text


def test_runtime_input_drift_defeats_planned_skip(tmp_path):
    """回归锁:u 有条件出边不是 skip 候选(必须真实重跑);c 的静态 skip
    计划按"导入克隆"的旧 u 哈希成立,但运行时 u 重跑产出了不同字节 →
    跳过前复核必须失败 → c 委托真实执行,绝不把过期身份当等价。"""
    src = _run(_RECHECK_YAML, tmp_path, fake=_recheck_fake("第一个说法"))

    # c 显式自导入:静态口径 c 的 invocation(旧 u 哈希)与源相等 → 计划成立
    importer_yaml = _recheck_importer_yaml(src.run_id)
    # 这次运行换一个 u 输出不同的 fake:u 重跑后字节必变
    result = _run(importer_yaml, tmp_path, fake=_recheck_fake("第二个说法"))
    events = EventReader(Path(result.dir) / "events.jsonl")
    assert events.find(type="node_done", node="u") is not None
    # c 被委托真实执行(不是 node_imported_reused),产出反映新 u 字节
    assert events.find(type="node_imported_reused", node="c") is None
    assert events.find(type="node_done", node="c") is not None
    src_u = EventReader(Path(src.dir) / "events.jsonl").find(
        type="node_done", node="u")
    new_u = events.find(type="node_done", node="u")
    assert new_u["output_sha256"] != src_u["output_sha256"]
    assert result.folded()["status"] == "done"

    # 对照组:同样的 fake(确定性,字节不变)→ u 重跑但哈希相等 → c 合法复用
    src2 = _run(_RECHECK_YAML, tmp_path, fake=_recheck_fake("第一组对照"))
    result2 = _run(_recheck_importer_yaml(src2.run_id), tmp_path,
                   fake=_recheck_fake("第一组对照"))
    events2 = EventReader(Path(result2.dir) / "events.jsonl")
    assert events2.find(type="node_done", node="u") is not None
    assert events2.find(type="node_imported_reused", node="c") is not None
    assert result2.folded()["status"] == "done"
