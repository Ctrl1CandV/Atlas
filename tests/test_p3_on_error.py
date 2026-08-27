# -*- coding: utf-8 -*-
"""P3 · 异常 taxonomy 与节点 on_error。

合同(ROADMAP §6):治理类异常永不可吞(含 RunCancelled);内容类失败
(候选全部失败)可 stop/continue/branch;branch 只走保留键 __failed__,
校验期强制接线;soft failure 写 write-once 错误产物 + node_failed_soft,
fold 从新旧事件都得到同一终态;Web/MCP 同源展示错误类与产物入口。

时序纪律:不写死单一时序;轮询带睡眠与终态断言;不碰绝对墙钟。
"""
import hashlib
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas import mcp as m
from atlas.adapters import (AllCandidatesFailed, FakeProvider, RunCancelled,
                            TransportError)
from atlas.engine import (CostExceeded, GuardViolation, HumanRejected,
                          NoRouteError, TimeoutViolation, execute_graph)
from atlas.events import EventReader, fold_events
from atlas.exc import (AGENT_CLI, CONTENT, GOVERNANCE, can_soft_fail,
                       classify, error_class_name)
from atlas.integrity import WiringError
from atlas.nodes.agent import AgentCliError
from atlas.runs import build_run_summary
from atlas.spec import SpecError, spec_from_yaml

from conftest import TASK_TEXT, good_review_text, good_writer_text, make_registry

# ─────────────────────────── taxonomy ───────────────────────────


def test_taxonomy_governance_never_soft_failable():
    """每类治理异常:分类为 governance 且 can_soft_fail=False。"""
    cases = [
        CostExceeded("超预算"),
        GuardViolation("循环超限"),
        RunCancelled("取消"),
        TimeoutViolation("deadline"),
        NoRouteError("路由不可判定"),
        HumanRejected("驳回"),
        WiringError("缺产物"),
        SpecError("规格错误"),
        TransportError("传输"),          # 候选内部错误,节点边界不单独出现
        ValueError("未登记类型"),        # fail-closed:未登记按治理处理
    ]
    for exc in cases:
        assert classify(exc) == GOVERNANCE, type(exc).__name__
        assert can_soft_fail(exc) is False, type(exc).__name__
        assert error_class_name(exc) == type(exc).__name__


def test_taxonomy_content_and_agent_cli():
    failed = AllCandidatesFailed("Fake:primary", [])
    assert classify(failed) == CONTENT
    assert can_soft_fail(failed) is True
    cli = AgentCliError("退出码 1")
    assert classify(cli) == AGENT_CLI
    # 白名单为空:AgentCliError 默认不可 soft-fail(baseline/diff/安全
    # 扫描错误都是治理错误;放开必须子类化 + 白名单 + 正反测试)
    assert can_soft_fail(cli) is False


# ─────────────────────────── spec 校验 ───────────────────────────

_CONTINUE_JOIN_YAML = """
name: continue_demo
nodes:
  - id: s
    type: llm
    model: Fake:primary
    prompt: 起点任务,输出含 summary 的 JSON。
    consumes: [task]
    output_schema:
      required: [summary]
  - id: a
    type: llm
    model: Fake:bad
    prompt: 会失败的分支。
    consumes: [task]
    on_error: continue
  - id: b
    type: llm
    model: Fake:other
    prompt: 健康分支。
    consumes: [task]
  - id: j
    type: llm
    model: Fake:other
    prompt: 汇总健康分支。
    consumes: [task, b.output]
edges:
  - from: s
    to: a
  - from: s
    to: b
  - from: a
    to: j
  - from: b
    to: j
  - from: j
    to: END
"""

_BRANCH_YAML = """
name: branch_demo
nodes:
  - id: a
    type: llm
    model: Fake:bad
    prompt: 会失败的主任务。
    consumes: [task]
    on_error: branch
  - id: handler
    type: llm
    model: Fake:other
    prompt: 失败处理器:阅读错误上下文并给出补救建议。
    consumes: [task, a.error]
edges:
  - from: a
    to: handler
    when: __failed__
  - from: a
    to: END
  - from: handler
    to: END
"""


def _mutation(yaml_text, old, new):
    assert old in yaml_text, f"变异锚点不存在:{old!r}"
    return yaml_text.replace(old, new)


def test_on_error_spec_validation_matrix():
    spec_from_yaml(_CONTINUE_JOIN_YAML)      # 基线合法
    spec_from_yaml(_BRANCH_YAML)
    rejects = [
        (_mutation(_CONTINUE_JOIN_YAML, "on_error: continue", "on_error: sideways"),
         "非法枚举值"),
        (_mutation(_CONTINUE_JOIN_YAML, "on_error: continue", "on_error: branch"),
         "branch 缺 __failed__ 边"),
        (_mutation(_BRANCH_YAML, "on_error: branch", "on_error: stop"),
         "__failed__ 边在非 branch 节点上"),
        (_mutation(_BRANCH_YAML + "  - from: a\n    to: END\n    when: __failed__\n",
                   "", ""),
         "重复 __failed__ 边"),
        (_mutation(_CONTINUE_JOIN_YAML, "  - from: a\n    to: j\n",
                   "  - from: a\n    to: j\n    when: go\n"),
         "continue 带条件出边"),
        (_mutation(_CONTINUE_JOIN_YAML, "consumes: [task, b.output]",
                   "consumes: [task, a.error]"),
         "消费非 branch 节点的 .error"),
        (_mutation(_CONTINUE_JOIN_YAML, "    prompt: 会失败的分支。\n    consumes: [task]",
                   "    prompt: 会失败的分支。\n    consumes: [task]\n    type: human"),
         "非 llm 节点声明 on_error(human)"),
    ]
    for bad, why in rejects:
        try:
            spec_from_yaml(bad)
            raise AssertionError(f"未被拒绝:{why}")
        except SpecError:
            pass


def test_default_on_error_keeps_fingerprint():
    """旧图零变化:显式 on_error: stop 与缺省的指纹一致。"""
    plain = spec_from_yaml(_CONTINUE_JOIN_YAML.replace("    on_error: continue\n", ""))
    from atlas.spec import spec_fingerprint
    explicit = spec_from_yaml(_CONTINUE_JOIN_YAML.replace(
        "on_error: continue", "on_error: stop"))
    assert spec_fingerprint(plain) == spec_fingerprint(explicit)
    assert spec_fingerprint(plain) != spec_fingerprint(
        spec_from_yaml(_CONTINUE_JOIN_YAML))


# ─────────────────────────── 三策略执行 ───────────────────────────


def _registry_with_bad(tmp_placeholder=None) -> FakeProvider:
    fake = FakeProvider()
    fake.configure("primary", text=good_writer_text(50))
    fake.configure("other", text=good_review_text())
    fake.configure("bad", transport_error="候选全部失败(模拟)")
    return fake


def test_stop_is_default_content_failure_fails_run(tmp_path):
    """默认 stop:内容失败照旧落 run_failed,没有软失败事件。"""
    yaml_stop = _CONTINUE_JOIN_YAML.replace("    on_error: continue\n", "") \
        .replace("model: Fake:bad", "model: Fake:bad")
    with pytest.raises(AllCandidatesFailed):
        execute_graph(spec_from_yaml(yaml_stop), task=TASK_TEXT,
                      runs_root=tmp_path, registry=make_registry(_registry_with_bad()))
    run_dir = next(d for d in tmp_path.iterdir()
                   if d.is_dir() and not d.name.startswith("."))
    events = EventReader(run_dir / "events.jsonl")
    assert events.find(type="node_failed_soft") is None
    assert fold_events(events.all())["status"] == "failed"


def test_continue_soft_failure_parallel_join(tmp_path):
    """continue:失败分支软失败,健康分支与 join 照常完成,run done。"""
    result = execute_graph(spec_from_yaml(_CONTINUE_JOIN_YAML), task=TASK_TEXT,
                           runs_root=tmp_path,
                           registry=make_registry(_registry_with_bad()))
    events = EventReader(result.dir / "events.jsonl")
    soft = events.find(type="node_failed_soft", node="a")
    assert soft is not None
    assert soft["error_class"] == "AllCandidatesFailed"
    assert soft["on_error"] == "continue"
    # write-once 错误产物:事件哈希与磁盘字节一致,内容含错误类与节点上下文
    artifact = Path(soft["output_path"])
    payload = artifact.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == soft["output_sha256"]
    doc = json.loads(payload.decode("utf-8"))
    assert doc["node"] == "a" and doc["error_class"] == "AllCandidatesFailed"
    assert doc["attempts"] and doc["attempts"][0]["model"] == "Fake:bad"
    # a 没有 node_done;b/j 正常;j 只消费健康分支
    assert events.find(type="node_done", node="a") is None
    assert events.find(type="node_done", node="j") is not None
    assert fold_events(events.all())["status"] == "done"
    # fold 反例回归:删掉 node_failed_soft 后终态不变
    stripped = [r for r in events.all() if r["type"] != "node_failed_soft"]
    assert fold_events(events.all()) == fold_events(stripped)


def test_continue_cannot_swallow_governance(tmp_path):
    """continue 配置遇治理异常(费用守卫)仍终止整图,不落软失败。"""
    yaml_cost = _CONTINUE_JOIN_YAML.replace(
        "nodes:", "guards:\n  max_cost_usd: 0.001\nnodes:")
    with pytest.raises(CostExceeded):
        execute_graph(spec_from_yaml(yaml_cost), task=TASK_TEXT,
                      runs_root=tmp_path,
                      registry=make_registry(_registry_with_bad()))
    run_dir = next(d for d in tmp_path.iterdir()
                   if d.is_dir() and not d.name.startswith("."))
    events = EventReader(run_dir / "events.jsonl")
    assert events.find(type="node_failed_soft") is None
    assert fold_events(events.all())["status"] == "failed"


class _RecordingFake(FakeProvider):
    """记录每个模型收到的 prompt 原文(断言错误上下文进了失败处理器)。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.prompts: list[tuple[str, str]] = []

    def call(self, model_id: str, prompt: str, *args, **kwargs):
        self.prompts.append((model_id, prompt))
        return super().call(model_id, prompt, *args, **kwargs)


def test_branch_routes_failure_to_handler_with_error_context(tmp_path):
    """branch:软失败走 __failed__,处理器拿到 a.error 的错误上下文。"""
    fake = _RecordingFake()
    fake.configure("bad", transport_error="主任务失败(模拟)")
    fake.configure("other", text=good_review_text())
    result = execute_graph(spec_from_yaml(_BRANCH_YAML), task=TASK_TEXT,
                           runs_root=tmp_path, registry=make_registry(fake))
    events = EventReader(result.dir / "events.jsonl")
    assert events.find(type="node_failed_soft", node="a") is not None
    done = events.find(type="node_done", node="handler")
    assert done is not None and done["model_used"] == "Fake:other"
    handler_prompt = next(p for mid, p in fake.prompts if mid == "other")
    assert "error_class" in handler_prompt and "AllCandidatesFailed" in handler_prompt
    assert fold_events(events.all())["status"] == "done"


def test_branch_success_routes_normally(tmp_path):
    """branch 的成功路径照常按 when 路由。"""
    yaml_ok = """
name: branch_ok
nodes:
  - id: a
    type: llm
    model: Fake:primary
    prompt: 输出含 route 的 JSON,值为 go。
    consumes: [task]
    route_field: route
    output_schema:
      required: [route]
    on_error: branch
  - id: handler
    type: llm
    model: Fake:other
    prompt: 失败处理器。
    consumes: [task, a.error]
  - id: normal
    type: llm
    model: Fake:other
    prompt: 正常下游。
    consumes: [task, a.output]
edges:
  - from: a
    to: normal
    when: go
  - from: a
    to: handler
    when: __failed__
  - from: normal
    to: END
  - from: handler
    to: END
"""
    fake = FakeProvider()
    fake.configure("primary", text=json.dumps({"route": "go"}))
    fake.configure("other", text=good_review_text())
    result = execute_graph(spec_from_yaml(yaml_ok), task=TASK_TEXT,
                           runs_root=tmp_path, registry=make_registry(fake))
    events = EventReader(result.dir / "events.jsonl")
    assert events.find(type="node_done", node="normal") is not None
    assert events.find(type="node_done", node="handler") is None
    assert events.find(type="node_failed_soft") is None
    assert fold_events(events.all())["status"] == "done"


def test_reserved_route_value_rejected_on_success(tmp_path):
    """成功输出保留键字面量:不可判定路由,治理错误终止。"""
    yaml_reserved = """
name: reserved_route
entry: a
nodes:
  - id: a
    type: llm
    model: Fake:bad
    prompt: 输出 route 字段值为 __failed__。
    consumes: [task]
    route_field: route
    output_schema:
      required: [route]
    on_error: branch
  - id: handler
    type: llm
    model: Fake:other
    prompt: 失败处理器:阅读错误上下文与上游产物并给出补救建议。
    consumes: [task, a.error, a.output]
edges:
  - from: a
    to: END
    when: go
  - from: a
    to: handler
    when: __failed__
  - from: handler
    to: END
"""
    fake = FakeProvider()
    fake.configure("bad", text=json.dumps({"route": "__failed__"}))
    fake.configure("other", text=good_review_text())
    with pytest.raises(NoRouteError):
        execute_graph(spec_from_yaml(yaml_reserved), task=TASK_TEXT,
                      runs_root=tmp_path, registry=make_registry(fake))
    run_dir = next(d for d in tmp_path.iterdir()
                   if d.is_dir() and not d.name.startswith("."))
    events = EventReader(run_dir / "events.jsonl")
    assert fold_events(events.all())["status"] == "failed"


def test_failed_only_edge_success_ends_graph(tmp_path):
    yaml_only = _BRANCH_YAML.replace(
        "model: Fake:bad", "model: Fake:primary").replace(
        "  - from: a\n    to: END\n", "")
    yaml_only = yaml_only.replace(
        "prompt: 会失败的主任务。", "prompt: 直接完成的任务。").replace(
        "consumes: [task, a.error]", "consumes: [task]")
    yaml_only = yaml_only.replace(
        "    on_error: branch\n", "    output_schema:\n      required: [summary]\n    on_error: branch\n")
    fake = FakeProvider()
    fake.configure("primary", text=good_writer_text(30))
    fake.configure("other", text=good_review_text())
    result = execute_graph(spec_from_yaml(yaml_only), task=TASK_TEXT,
                           runs_root=tmp_path, registry=make_registry(fake))
    events = EventReader(result.dir / "events.jsonl")
    assert events.find(type="node_done", node="a") is not None
    assert fold_events(events.all())["status"] == "done"


class _TransientFailFake(FakeProvider):
    """每模型可配「前 N 次调用抛 TransportError,之后正常」——确定性制造
    「第一次软失败、重入后成功」的重入场景。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.remaining: dict[str, int] = {}

    def fail_first(self, model_id: str, times: int = 1) -> None:
        self.remaining[model_id] = times

    def call(self, model_id: str, prompt: str, *args, **kwargs):
        if self.remaining.get(model_id, 0) > 0:
            self.remaining[model_id] -= 1
            raise TransportError(f"{model_id} 瞬态故障")
        return super().call(model_id, prompt, *args, **kwargs)


def test_reentered_branch_node_success_after_soft_failure(tmp_path):
    """审查阻塞项回归锁(2026-08-26):branch 节点先软失败、经失败处理器
    回边重入后成功——路由必须按「最近一次执行结局」走成功路径,不能被
    残留的旧 .error 产物误判成再次软失败(旧实现按产物存在性判定,
    merge_dicts 键只增不清,会把这种拓扑打成死循环直至 max_iterations)。"""
    yaml_reentry = """
name: branch_reentry
entry: r
guards:
  max_iterations: 5
nodes:
  - id: r
    type: llm
    model: Fake:primary
    prompt: 主任务,输出含 route 的 JSON。
    consumes: [task]
    route_field: route
    output_schema:
      required: [route]
    on_error: branch
  - id: handler
    type: llm
    model: Fake:other
    prompt: 失败处理器:阅读错误上下文并补救,然后让主任务重试。
    consumes: [task, r.error]
  - id: out
    type: llm
    model: Fake:other
    prompt: 成功下游。
    consumes: [task, r.output]
edges:
  - from: r
    to: out
    when: go
  - from: r
    to: handler
    when: __failed__
  - from: handler
    to: r
  - from: out
    to: END
"""
    fake = _TransientFailFake()
    fake.configure("primary", text=json.dumps({"route": "go"}))
    fake.configure("other", text=good_review_text())
    fake.fail_first("primary", times=1)
    result = execute_graph(spec_from_yaml(yaml_reentry), task=TASK_TEXT,
                           runs_root=tmp_path, registry=make_registry(fake))
    events = EventReader(result.dir / "events.jsonl")
    # 第一次软失败存在,且只有一次;第二次执行成功
    softs = events.filter(type="node_failed_soft", node="r")
    assert len(softs) == 1 and softs[0]["error_class"] == "AllCandidatesFailed"
    dones = events.filter(type="node_done", node="r")
    assert len(dones) == 1 and json.loads(
        Path(dones[0]["output_path"]).read_text(encoding="utf-8"))["route"] == "go"
    assert events.find(type="node_done", node="out") is not None
    assert fold_events(events.all())["status"] == "done"   # 不是 GuardViolation/failed


def test_loop_graph_branch_exit_on_soft_failure(tmp_path):
    """循环图 × branch:回边存在,软失败从循环里经 __failed__ 出去。"""
    yaml_loop = """
name: loop_branch
entry: r
guards:
  max_iterations: 3
nodes:
  - id: r
    type: llm
    model: Fake:bad
    prompt: 循环体,输出 route 为 retry 或 ok。
    consumes: [task]
    route_field: route
    output_schema:
      required: [route]
    on_error: branch
  - id: handler
    type: llm
    model: Fake:other
    prompt: 失败处理器。
    consumes: [task, r.error]
edges:
  - from: r
    to: r
    when: retry
  - from: r
    to: END
    when: ok
  - from: r
    to: handler
    when: __failed__
  - from: handler
    to: END
"""
    result = execute_graph(spec_from_yaml(yaml_loop), task=TASK_TEXT,
                           runs_root=tmp_path,
                           registry=make_registry(_registry_with_bad()))
    events = EventReader(result.dir / "events.jsonl")
    assert events.find(type="node_failed_soft", node="r") is not None
    assert events.find(type="node_done", node="handler") is not None
    assert fold_events(events.all())["status"] == "done"


# ─────────────────────────── 表面同源 ───────────────────────────


def test_dry_run_lists_non_default_on_error(tmp_path):
    fake = _registry_with_bad()
    out = m.dry_run_impl("", TASK_TEXT,
                         registry_factory=lambda pids: make_registry(fake),
                         yaml=_CONTINUE_JOIN_YAML)
    by_node = {n["node"]: n for n in out["nodes"]}
    assert by_node["a"]["on_error"] == "continue"
    assert "on_error" not in by_node["b"]      # 默认 stop 不占渲染位
    assert "on_error" not in by_node["j"]


def test_web_and_summary_same_source_soft_failure(tmp_path):
    """Web 节点槽与 MCP 共用的 build_run_summary 同源展示软失败。"""
    from atlas.web import create_app

    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "demo.yaml").write_text(_CONTINUE_JOIN_YAML.lstrip(),
                                         encoding="utf-8")
    fake = _registry_with_bad()
    app = create_app(workflows_dir=workflows, runs_dir=tmp_path / "runs",
                     registry_factory=lambda pids: make_registry(fake),
                     api_only=True)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        resp = client.post("/api/workflows/demo/run",
                           json={"task": "软失败同源测试"},
                           headers={"X-Atlas-Request": "1"})
        run_id = resp.json()["run_id"]
        for _ in range(50):
            summary = client.get(f"/api/runs/{run_id}").json()
            if summary["status"] in ("done", "failed"):
                break
            time.sleep(0.1)
        assert summary["status"] == "done"
        slot = next(n for n in summary["nodes"] if n["id"] == "a")
        assert slot["status"] == "failed_soft"
        assert slot["error_class"] == "AllCandidatesFailed"
        assert slot["on_error"] == "continue"

    detail = build_run_summary(run_id, runs_root=tmp_path / "runs")
    entry = detail["node_details"]["a"]
    assert entry["soft_failed"] is True
    assert entry["error_class"] == "AllCandidatesFailed"
    assert entry["output_path"].endswith(".json")
