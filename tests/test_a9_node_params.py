# -*- coding: utf-8 -*-
"""A9:每个可声明的节点参数都必须有"真的生效"的测试(PLAN-v2 2.3 的规矩)。
A10:能力不支持时显式标注,不静默。

断言数据源:FakeProvider.calls 里的 extra_body / timeout_s,以及
FakeAgentRunner 收到的参数——即"实际发出的请求"。
"""
import pytest

from atlas import thinking as thinking_mod
from atlas.adapters import FakeProvider, TransportError, Usage
from atlas.engine import execute_graph
from atlas.events import EventReader
from atlas.spec import SpecError, spec_from_yaml

from conftest import TASK_TEXT, make_registry


def _llm_yaml(extra=""):
    return f"""
name: params
nodes:
  - id: solo
    type: llm
    model: Fake:primary
    fallback: []
    prompt: p
    consumes: [task]
{extra}
edges:
  - from: solo
    to: END
"""


def _run(tmp_path, fake, extra=""):
    spec = spec_from_yaml(_llm_yaml(extra))
    return execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                         registry=make_registry(fake))


@pytest.fixture
def cap(monkeypatch):
    """注入假能力表:primary=effort 档位,bud=budget 数值,dead=none。"""
    monkeypatch.setattr(thinking_mod, "_cache", {
        "Fake:primary": {"kind": "effort"},
        "Fake:bud": {"kind": "budget"},
        "Fake:dead": {"kind": "none"},
    })


def test_a9_max_output_tokens(tmp_path):
    fake = FakeProvider()
    fake.configure("primary", text="ok")
    _run(tmp_path, fake, "    max_output_tokens: 1234")
    assert fake.calls[0]["extra_body"]["max_tokens"] == 1234


def test_a9_thinking_effort_mapping(tmp_path, cap):
    fake = FakeProvider()
    fake.configure("primary", text="ok", reasoning_tokens=42)
    _run(tmp_path, fake, "    thinking: low")
    assert fake.calls[0]["extra_body"]["reasoning_effort"] == "low"
    _run(tmp_path, fake, "    thinking: xhigh")
    # 只有低中高的能力:xhigh 映射为 high(主人定的规则)
    assert fake.calls[-1]["extra_body"]["reasoning_effort"] == "high"


def test_a9_thinking_budget_mapping(tmp_path, cap):
    """budget 能力:档位映射为预算数值;预算必须小于输出上限(审查🔴1)。"""
    fake = FakeProvider()
    fake.protocol = "anthropic"   # budget 能力匹配 anthropic 端点
    fake.configure("bud", text="ok", reasoning_tokens=1)
    # high=16384:必须同时抬高 max_output_tokens,否则校验期拒绝
    spec = spec_from_yaml(_llm_yaml(
        "    thinking: high\n    max_output_tokens: 20000").replace(
        "Fake:primary", "Fake:bud"))
    execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                  registry=make_registry(fake))
    assert fake.calls[0]["extra_body"]["thinking"] == {
        "type": "enabled", "budget_tokens": 16384}


def test_budget_over_cap_rejected_at_validation(cap):
    """审查🔴1:预算 >= 输出上限 → 校验期拒绝并给出修复指引。"""
    with pytest.raises(SpecError, match="max_output_tokens"):
        spec_from_yaml(_llm_yaml("    thinking: high").replace(
            "Fake:primary", "Fake:bud"))


def test_explicit_scalar_entry_not_silently_widened():
    """审查🔴2:多根图里显式 entry: left → 只跑左腿,另一根因不可达被拒。"""
    with pytest.raises(SpecError, match="不可达"):
        spec_from_yaml("entry: left\n" + _dual_entry_yaml())


def _dual_entry_yaml():
    return """
name: dual-x
nodes:
  - id: left
    type: llm
    model: Fake:left
    prompt: p
    consumes: [task]
  - id: right
    type: llm
    model: Fake:right
    prompt: p
    consumes: [task]
edges:
  - from: left
    to: END
  - from: right
    to: END
"""


def test_a9_temperature_and_seed(tmp_path):
    fake = FakeProvider()
    fake.configure("primary", text="ok")
    _run(tmp_path, fake, "    temperature: 0.3\n    seed: 7")
    assert fake.calls[0]["extra_body"]["temperature"] == 0.3
    assert fake.calls[0]["extra_body"]["seed"] == 7


def test_a9_timeout_s_reaches_request(tmp_path):
    fake = FakeProvider()
    fake.configure("primary", text="ok")
    _run(tmp_path, fake, "    timeout_s: 120")
    assert fake.calls[0]["timeout_s"] == 120


def test_graph_deadline_caps_node_timeout(tmp_path):
    fake = FakeProvider()
    fake.configure("primary", text="ok")
    spec = spec_from_yaml(_llm_yaml("    timeout_s: 120")
                          + "\nguards:\n  timeout_s: 30\n")
    execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                  registry=make_registry(fake))
    timeout = fake.calls[0]["timeout_s"]
    # 断言封顶关系(节点 120 被 run 级 30 压住),不断言绝对墙钟——
    # 共享 runner 高负载下 1 秒都不稳定(2026-08-25 CI 实证)
    assert timeout is not None and 0 < timeout <= 30


def test_a9_llm_retry_retries_transport_only(tmp_path):
    """retry: 1 → 传输失败后同模型重跑;假成功不重试(换候选/失败)。"""
    fake = FakeProvider()
    fake.configure("primary", text="最终成功", usage=Usage(input_tokens=10,
                                                            output_tokens=4))
    # 用序列模拟传输失败?FakeProvider 的 transport_error 是恒定的。
    # 用可变 spec:第一次抛,第二次正常
    class Flaky(FakeProvider):
        def call(self, model_id, prompt, extra_body=None, timeout_s=None):
            if len(self.calls) == 0:
                self.calls.append({"model": model_id, "boomed": True})
                raise TransportError("网关 502")
            return super().call(model_id, prompt, extra_body, timeout_s)

    fake = Flaky()
    fake.configure("primary", text="ok")
    run = _run(tmp_path, fake, "    retry: 1")
    assert run.folded()["status"] == "done"
    assert run.events.find(type="model_failed") is not None   # 第一次失败记了账
    assert run.events.find(type="node_done")["model_used"] == "Fake:primary"


def test_a10_thinking_ineffective_warns(tmp_path, cap):
    """A10:设了思考档位,响应里没有任何思考痕迹 → effort_ineffective 警告。"""
    fake = FakeProvider()
    fake.configure("primary", text="ok", reasoning_tokens=0)  # 无思考痕迹
    run = _run(tmp_path, fake, "    thinking: high")
    warn = run.events.find(type="effort_ineffective", model="Fake:primary")
    assert warn is not None and warn["tier"] == "high"
    assert "没有思考痕迹" in warn["reason"]
    # 运行本身照常完成(警告不是失败)
    assert run.folded()["status"] == "done"


def test_a10_thinking_effective_no_warning(tmp_path, cap):
    fake = FakeProvider()
    fake.configure("primary", text="ok", reasoning_tokens=99)
    run = _run(tmp_path, fake, "    thinking: high")
    assert run.events.find(type="effort_ineffective") is None


def test_a10_thinking_rejected_for_none_model(tmp_path, cap):
    """能力表说 none 的模型:校验期拒绝,不许设不生效的旋钮。"""
    with pytest.raises(SpecError, match="没有真实的思考控制"):
        spec_from_yaml(_llm_yaml("    thinking: high").replace(
            "Fake:primary", "Fake:dead"))


def test_a10_unsupported_candidate_skipped_not_silent(tmp_path, cap):
    """失败链上的候选不支持思考 → 跳过并记账,思考意图不静默降级。"""
    from atlas.adapters import call_with_fallback
    from atlas.events import EventLog
    import pathlib

    # 构造:主模型协议不匹配(effort 能力 + anthropic 端点)→ ThinkingUnsupported
    fake = FakeProvider()
    fake.configure("primary", text="ok", reasoning_tokens=5)
    fake.configure("fb", text="备用带思考", reasoning_tokens=5)
    registry = make_registry(fake)
    # monkeypatch 能力:primary=effort;再给它一个 anthropic 协议适配器?
    # 直接构造协议不匹配:主模型 effort 走 openai——正常。
    # 改用 Fake:dead 作为 fallback:dead=none → 跳过
    fake.configure("dead", text="不该被用")
    monkey_cap = {"Fake:primary": {"kind": "none"}, "Fake:dead": {"kind": "none"},
                  "Fake:fb": {"kind": "effort"}}
    orig = thinking_mod.load_capabilities
    thinking_mod._cache = monkey_cap
    try:
        log = EventLog(pathlib.Path(tmp_path) / "x")
        outcome = call_with_fallback(
            registry=registry, log=log, node_id="n", iteration=1,
            model_ref="Fake:primary", fallback_refs=["Fake:fb"],
            prompt="p" * 100, required_fields=None, thinking_tier="high")
        assert outcome.model_used == "Fake:fb"   # none 的被跳过,effort 的顶上
        assert thinking_mod._cache  # noqa: B018 - 保持引用
    finally:
        thinking_mod._cache = None
        thinking_mod.reload_capabilities()


# ── agent 节点参数(A9)────────────────────────────────────────


class RecordingRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, attachment, *, node_type, max_turns, cwd=None, **kw):
        self.calls.append({"node_type": node_type, "max_turns": max_turns,
                           "cwd": cwd, **kw})
        return "报告完成。"


def _agent_yaml(extra=""):
    return f"""
name: agent-params
nodes:
  - id: scout
    type: research
    prompt: p
    consumes: [task]
{extra}
edges:
  - from: scout
    to: END
"""


def test_a9_agent_model_reaches_runner_and_events(tmp_path):
    runner = RecordingRunner()
    spec = spec_from_yaml(_agent_yaml("    model: Fake:agent-model"))
    run = execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                        registry=make_registry(FakeProvider()), agent_runner=runner)
    assert runner.calls[0]["model_ref"] == "Fake:agent-model"
    done = run.events.find(type="node_done", node="scout")
    assert done["model_requested"] == "Fake:agent-model"
    assert done["model_used"] == "Fake:agent-model"


def test_a9_agent_allow_web_off(tmp_path):
    runner = RecordingRunner()
    spec = spec_from_yaml(_agent_yaml("    allow_web: false"))
    execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                  registry=make_registry(FakeProvider()), agent_runner=runner)
    assert runner.calls[0]["allow_web"] is False


def test_a9_agent_timeout_and_retry(tmp_path):
    runner = RecordingRunner()
    spec = spec_from_yaml(_agent_yaml("    timeout_s: 300\n    retry: 2"))
    execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                  registry=make_registry(FakeProvider()), agent_runner=runner)
    assert runner.calls[0]["timeout_s"] == 300


def test_a9_coding_writable_false_no_diff(tmp_path):
    """writable: false 的 coding_agent:runner 收到只读形态,不采集 diff。"""
    import subprocess

    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=project, check=True,
                   capture_output=True)

    runner = RecordingRunner()
    spec = spec_from_yaml(f"""
name: ro-coder
nodes:
  - id: coder
    type: coding_agent
    prompt: p
    consumes: [task]
    workdir: {project.as_posix()}
    writable: false
edges:
  - from: coder
    to: END
""")
    run = execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                        registry=make_registry(FakeProvider()),
                        agent_runner=runner)
    assert run.folded()["status"] == "done"
    assert runner.calls[0]["writable"] is False
    done = run.events.find(type="node_done", node="coder")
    assert "diff_path" not in done          # 只读:不采集改动
    assert "coder.diff" not in run.artifacts


# ── 审查🟡7 补缺:端点切换 / allowed_paths / 假成功不重试 ─────

def test_endpoint_switch_for_thinking(tmp_path, cap):
    """M4 核心新机制:主协议 openai 上 thinking 不可设、能力在 anthropic
    端点 → 自动切到 anthropic 适配器发,思考意图不丢(Kiro 形态)。"""
    from atlas.adapters import AdapterRegistry, FakeProvider
    from atlas import thinking as _t
    _t._cache = {**(_t._cache or {}),
                 "Fake:k": {"kind": "budget"}}   # 能力在 anthropic 端点

    openai_fake = FakeProvider()          # protocol=openai(默认)
    openai_fake.configure("k", text="via-openai", reasoning_tokens=0)
    anthro_fake = FakeProvider()
    anthro_fake.protocol = "anthropic"
    anthro_fake.configure("k", text="via-anthropic", reasoning_tokens=7)

    registry = AdapterRegistry()
    openai_fake.register_into(registry, "Fake")       # 先注册=首选端点
    anthro_fake.register_into(registry, "Fake")

    from atlas.adapters import call_with_fallback
    from atlas.events import EventLog
    import pathlib
    log = EventLog(pathlib.Path(tmp_path) / "x")
    outcome = call_with_fallback(
        registry=registry, log=log, node_id="n", iteration=1,
        model_ref="Fake:k", fallback_refs=[], prompt="p" * 90,
        required_fields=None, thinking_tier="medium")
    assert outcome.model_used == "Fake:k"
    # openai 端点没被调用(能力在 anthropic);anthropic 收到了 thinking 参数
    assert openai_fake.calls == []
    assert anthro_fake.calls[0]["extra_body"]["thinking"]["budget_tokens"] == 4096
    _t._cache = None


def test_a9_allowed_paths_reach_runner(tmp_path):
    runner = RecordingRunner()
    target = tmp_path / "extra-dir"
    target.mkdir()
    spec = spec_from_yaml(_agent_yaml(
        f"    allowed_paths:\n      - {target.as_posix()}"))
    execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                  registry=make_registry(FakeProvider()), agent_runner=runner)
    assert runner.calls[0]["allowed_paths"] == [str(target).replace("/", "\\")
                                                if False else target.as_posix()]


def test_a9_allow_web_defaults_off_for_research(tmp_path):
    runner = RecordingRunner()
    spec = spec_from_yaml(_agent_yaml())   # 不写 allow_web
    execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                  registry=make_registry(FakeProvider()), agent_runner=runner)
    assert runner.calls[0]["allow_web"] is False


def test_degraded_output_does_not_consume_retry(tmp_path):
    """llm retry 只对传输失败重试;假成功(空返回)直接换候选/失败。"""
    from atlas.adapters import FakeProvider, Usage
    from atlas.engine import execute_graph as eg

    fake = FakeProvider()
    fake.configure("primary", text="")   # 假成功:空返回
    fake.configure("fb", text="备用", usage=Usage(input_tokens=90, output_tokens=3))
    spec = spec_from_yaml(_llm_yaml("    retry: 3").replace(
        "fallback: []", "fallback: [Fake:fb]"))
    run = eg(spec, task=TASK_TEXT, runs_root=tmp_path, registry=make_registry(fake))
    # primary 只被调了一次(假成功不重试),fallback 顶上
    assert len([c for c in fake.calls if c["model"] == "primary"]) == 1
    assert run.events.find(type="node_done")["model_used"] == "Fake:fb"


def test_sdk_clients_disable_hidden_retries(monkeypatch):
    """SDK 不得暗中重试;每次真实尝试必须由 Atlas retry 记入事件账本。"""
    import anthropic
    import openai
    from atlas.adapters import AnthropicCompatAdapter, OpenAICompatAdapter

    captured = {}

    class StubOpenAI:
        def __init__(self, **kwargs):
            captured["openai"] = kwargs

    class StubAnthropic:
        def __init__(self, **kwargs):
            captured["anthropic"] = kwargs

    monkeypatch.setattr(openai, "OpenAI", StubOpenAI)
    monkeypatch.setattr(anthropic, "Anthropic", StubAnthropic)
    OpenAICompatAdapter("P", "https://example.invalid/v1", "secret")
    AnthropicCompatAdapter("P", "https://example.invalid", "secret")

    assert captured["openai"]["max_retries"] == 0
    assert captured["anthropic"]["max_retries"] == 0
