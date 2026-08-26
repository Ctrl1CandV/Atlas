# -*- coding: utf-8 -*-
"""P9 · controller heartbeat。

合同(ROADMAP §5):每次 attempt 的派发窗口内定时写 node_progress
(node/iteration/attempt/model/elapsed_ms/phase);只证明 controller 在等待,
不声称模型内部进度;attempt 结束、失败、取消和终态后停止,迟到 tick 拒绝;
fold 不因心跳改变终态;间隔 run 级可配(环境变量下限 30s,大声拒绝)。

时序纪律(交接文档第七节):轮询带睡眠与终态断言,不断言绝对墙钟,
不断言精确条数——只断言「≥1 条」与单调性这类所有合法时序都满足的性质。
"""
import threading
import time

import pytest

from atlas.adapters import AllCandidatesFailed, FakeProvider, TransportError
from atlas.engine import (HEARTBEAT_INTERVAL_ENV, NodeHeartbeat, execute_graph,
                          resolve_heartbeat_interval, write_cancel_request)
from atlas.events import EventLog, EventReader, fold_events
from atlas.spec import spec_from_yaml

from conftest import TASK_TEXT, load_graph, make_registry, standard_fake

INTERVAL = 0.05     # 测试驱动用的小间隔(显式参数是可信进程内输入)
HEARTBEAT_FIELDS = {"seq", "ts", "type", "node", "iteration", "attempt",
                    "model", "phase", "elapsed_ms"}


class _GatedFake(FakeProvider):
    """指定模型的调用阻塞到放行——确定性制造「controller 在等一个冻结调用」."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.gates: dict[str, threading.Event] = {}

    def gate(self, model_id: str) -> threading.Event:
        event = threading.Event()
        self.gates[model_id] = event
        return event

    def call(self, model_id: str, *args, **kwargs):
        gate = self.gates.get(model_id)
        if gate is not None:
            assert gate.wait(timeout=15), f"{model_id} 的门没有放行"
        return super().call(model_id, *args, **kwargs)


class _FlakyFake(FakeProvider):
    """前 fail_times 次调用先睡 delay_s 再抛 TransportError,之后正常."""

    def __init__(self, delay_s: float, fail_times: int = 1, **kwargs) -> None:
        super().__init__(**kwargs)
        self.delay_s = delay_s
        self.fail_times = fail_times

    def call(self, model_id: str, *args, **kwargs):
        if self.fail_times > 0:
            self.fail_times -= 1
            time.sleep(self.delay_s)
            raise TransportError("模拟瞬时网络故障")
        return super().call(model_id, *args, **kwargs)


def _from_template(cls, **kwargs) -> FakeProvider:
    """以 standard_fake 的合格输出规格为底,换用指定的 fake 子类。"""
    template = standard_fake()
    fake = cls(max_output_tokens=template.max_output_tokens, **kwargs)
    fake.models.update(template.models)
    return fake


_RETRY_YAML = """
name: heartbeat_retry
nodes:
  - id: node_a
    type: llm
    model: Fake:primary
    retry: 1
    prompt: 分析任务材料。
    consumes: [task]
    output_schema:
      required: [summary]
edges:
  - from: node_a
    to: END
"""


def _wait_heartbeats(runs_root, run_id, *, minimum: int, timeout_s: float = 10.0):
    """轮询直到该 run 的 node_progress 达到 minimum 条;超时大声失败。"""
    path = runs_root / run_id / "events.jsonl"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            got = EventReader(path).filter(type="node_progress")
            if len(got) >= minimum:
                return got
        time.sleep(0.02)
    raise AssertionError(
        f"等待 {minimum} 条 node_progress 超时({timeout_s}s):"
        f"现有 {EventReader(path).filter(type='node_progress') if path.exists() else []}")


def test_frozen_call_heartbeats_controller_wait(tmp_path):
    """冻结调用窗口内:递增 elapsed、attempt/model/phase 语境、措辞只说
    controller 在等待(字段集就是全部词汇,没有任何进度百分比)。"""
    fake = _from_template(_GatedFake)
    gate = fake.gate("primary")

    def _run() -> None:
        execute_graph(load_graph("two_node"), task=TASK_TEXT, runs_root=tmp_path,
                      registry=make_registry(fake), heartbeat_interval_s=INTERVAL)

    thread = threading.Thread(target=_run)
    thread.start()
    run_dir = _wait_single_run_dir(tmp_path)
    _wait_heartbeats(tmp_path, run_dir.name, minimum=3)
    gate.set()
    thread.join(timeout=15)
    assert not thread.is_alive(), "controller 线程没有在门放行后结束"

    events = EventReader(run_dir / "events.jsonl")
    all_beats = events.filter(type="node_progress", node="node_a")
    elapsed = [b["elapsed_ms"] for b in all_beats]
    assert len(all_beats) >= 3
    assert elapsed == sorted(elapsed) and elapsed[-1] > elapsed[0]
    for b in all_beats:
        assert set(b) <= HEARTBEAT_FIELDS          # 措辞边界:只有这些字段
        assert b["phase"] == "waiting"             # 冻结期间只说 controller 在等
        assert b["attempt"] == 1 and b["model"] == "Fake:primary"
        assert b["iteration"] == 1
        assert isinstance(b["elapsed_ms"], int) and b["elapsed_ms"] > 0
    # 快节点没有心跳;心跳全部先于该节点的落账事件
    assert events.filter(type="node_progress", node="node_b") == []
    done_a = events.find(type="node_done", node="node_a")
    assert max(b["seq"] for b in all_beats) < done_a["seq"]
    assert fold_events(events.all())["status"] == "done"


def _wait_single_run_dir(runs_root, timeout_s: float = 10.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        dirs = [d for d in runs_root.iterdir()
                if d.is_dir() and not d.name.startswith(".")]
        if len(dirs) == 1:
            return dirs[0]
        time.sleep(0.02)
    raise AssertionError(f"runs_root 下没有出现唯一的 run 目录:{list(runs_root.iterdir())}")


def test_no_late_heartbeat_after_terminal(tmp_path):
    """终态之后账本静止:等数倍间隔后没有新事件,run_done 之后没有心跳。"""
    fake = _from_template(_FlakyFake, delay_s=0.3)   # 有等待窗口,产生真实心跳
    result = execute_graph(load_graph("two_node"), task=TASK_TEXT,
                           runs_root=tmp_path, registry=make_registry(fake),
                           heartbeat_interval_s=INTERVAL)
    events_path = result.dir / "events.jsonl"
    before = EventReader(events_path).all()
    terminal_seq = max(e["seq"] for e in before)
    time.sleep(0.3)   # 6 个心跳周期,迟到线程若泄漏必写新事件
    after = EventReader(events_path).all()
    assert [e["seq"] for e in after] == [e["seq"] for e in before]
    done = next(e for e in after if e["type"] == "run_done")
    assert all(e["seq"] < done["seq"] for e in after
               if e["type"] == "node_progress")


def test_retry_phase_and_attempt_context(tmp_path):
    """传输失败重试:等待期 phase=retry;重新派发后 attempt 递进;
    全部失败落 run_failed,且终态后没有心跳。"""
    fake = _from_template(_FlakyFake, delay_s=0.3, fail_times=99)
    with pytest.raises(AllCandidatesFailed):
        execute_graph(spec_from_yaml(_RETRY_YAML), task=TASK_TEXT,
                      runs_root=tmp_path, registry=make_registry(fake),
                      heartbeat_interval_s=INTERVAL)
    run_dir = _wait_single_run_dir(tmp_path)
    time.sleep(0.2)   # 若有泄漏线程,给它们写迟到的机会
    events = EventReader(run_dir / "events.jsonl")
    beats = events.filter(type="node_progress")
    phases = {b["phase"] for b in beats}
    assert "waiting" in phases and "retry" in phases
    assert all(b["attempt"] in (1, 2) for b in beats)
    elapsed = [b["elapsed_ms"] for b in beats]
    assert elapsed == sorted(elapsed)
    failed = next(e for e in events.all() if e["type"] == "run_failed")
    assert all(e["seq"] < failed["seq"] for e in beats)
    assert fold_events(events.all())["status"] == "failed"


def test_retry_redispatch_heartbeat_then_success(tmp_path):
    """一次传输失败后重试成功:第二次派发真实发生(失败一次后 node_done),
    fold 终态 done。"""
    fake = _from_template(_FlakyFake, delay_s=0.3, fail_times=1)
    result = execute_graph(spec_from_yaml(_RETRY_YAML), task=TASK_TEXT,
                           runs_root=tmp_path, registry=make_registry(fake),
                           heartbeat_interval_s=INTERVAL)
    events = EventReader(result.dir / "events.jsonl")
    transport_failures = events.filter(type="model_failed")
    assert len(transport_failures) == 1
    assert "TransportError" in transport_failures[0]["reason"]
    done = events.find(type="node_done", node="node_a")
    assert done is not None and done["model_used"] == "Fake:primary"
    assert fold_events(events.all())["status"] == "done"


def test_cancel_stops_heartbeat_before_terminal(tmp_path):
    """取消:在途调用完成后由消费点终止;run_cancelled 之后没有心跳。"""
    fake = _from_template(_GatedFake)
    gate = fake.gate("primary")

    def _run() -> None:
        execute_graph(load_graph("two_node"), task=TASK_TEXT, runs_root=tmp_path,
                      registry=make_registry(fake), heartbeat_interval_s=INTERVAL)

    thread = threading.Thread(target=_run)
    thread.start()
    run_dir = _wait_single_run_dir(tmp_path)
    _wait_heartbeats(tmp_path, run_dir.name, minimum=1)
    write_cancel_request(run_dir, reason="心跳取消测试")
    gate.set()
    thread.join(timeout=15)
    assert not thread.is_alive(), "取消后 controller 线程没有结束"

    events = EventReader(run_dir / "events.jsonl")
    cancelled = events.find(type="run_cancelled")
    assert cancelled is not None
    assert all(e["seq"] < cancelled["seq"] for e in events.all()
               if e["type"] == "node_progress")
    assert fold_events(events.all())["status"] == "cancelled"


def test_fold_regression_stripping_heartbeat_changes_nothing(tmp_path):
    """回归锁:删掉全部 node_progress 后,fold 结果必须逐字段一致。"""
    fake = _from_template(_FlakyFake, delay_s=0.3)
    result = execute_graph(load_graph("two_node"), task=TASK_TEXT,
                           runs_root=tmp_path, registry=make_registry(fake),
                           heartbeat_interval_s=INTERVAL)
    records = EventReader(result.dir / "events.jsonl").all()
    assert any(r["type"] == "node_progress" for r in records)
    stripped = [r for r in records if r["type"] != "node_progress"]
    assert fold_events(records) == fold_events(stripped)


def test_agent_cli_dispatch_heartbeats(tmp_path):
    """agent 节点的 CLI 派发窗口同样有心跳,语境带 runner。"""

    class _SlowRunner:
        runner_name = "slow_test_runner"

        def __call__(self, attachment, *, node_type, max_turns, cwd=None, **kw):
            time.sleep(0.3)
            return f"{node_type} 的报告"

    spec = spec_from_yaml("""
name: agent_heartbeat
nodes:
  - id: scout
    type: research
    prompt: 调研任务涉及的材料与方向。
    consumes: [task]
    max_turns: 8
edges:
  - from: scout
    to: END
""")
    result = execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                           registry=make_registry(FakeProvider()),
                           agent_runner=_SlowRunner(),
                           heartbeat_interval_s=INTERVAL)
    beats = EventReader(result.dir / "events.jsonl").filter(
        type="node_progress", node="scout")
    assert len(beats) >= 3
    elapsed = [b["elapsed_ms"] for b in beats]
    assert elapsed == sorted(elapsed) and elapsed[-1] > elapsed[0]
    for b in beats:
        assert b["model"] == "agent:research"
        assert b["runner"] == "slow_test_runner"
        assert b["phase"] == "waiting" and b["attempt"] == 1
    assert fold_events(EventReader(result.dir / "events.jsonl").all())[
        "status"] == "done"


def test_late_tick_rejected_and_restart(tmp_path):
    """单元合同:end() 后 tick 拒绝写;幂等;重新 begin 可再开窗。"""
    log = EventLog(tmp_path)
    hb = NodeHeartbeat(log, node="n1", iteration=1, interval_s=30.0,
                       started_mono=time.monotonic())
    hb.set_context(attempt=1, model="primary")
    hb.begin()
    assert hb._tick() is True
    hb.end()
    hb.end()   # 幂等
    assert hb._tick() is False          # 迟到 tick 被拒绝
    time.sleep(0.02)
    assert len(EventReader(tmp_path / "events.jsonl").all()) == 1

    hb.mark_retry_wait()
    hb.begin()                          # 重试退避窗口重开
    assert hb._tick() is True
    record = EventReader(tmp_path / "events.jsonl").all()[-1]
    assert record["phase"] == "retry" and record["attempt"] == 1
    hb.set_context(attempt=2, model="fallback")
    assert hb._tick() is True
    record = EventReader(tmp_path / "events.jsonl").all()[-1]
    assert record["phase"] == "waiting" and record["attempt"] == 2
    hb.end()


def test_env_interval_resolution(monkeypatch):
    """环境变量解析:缺省 30s;合法值生效;低于下限/非数大声拒绝。"""
    monkeypatch.delenv(HEARTBEAT_INTERVAL_ENV, raising=False)
    assert resolve_heartbeat_interval() == 30.0
    assert resolve_heartbeat_interval(0.05) == 0.05   # 显式参数可信
    monkeypatch.setenv(HEARTBEAT_INTERVAL_ENV, "45")
    assert resolve_heartbeat_interval() == 45.0
    monkeypatch.setenv(HEARTBEAT_INTERVAL_ENV, "10")
    with pytest.raises(ValueError):
        resolve_heartbeat_interval()
    monkeypatch.setenv(HEARTBEAT_INTERVAL_ENV, "abc")
    with pytest.raises(ValueError):
        resolve_heartbeat_interval()
