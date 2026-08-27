# -*- coding: utf-8 -*-
"""批次 K(D4 收官)· agent retry 放大风险警告。

RFC 决议(2026-08-27,docs/rfcs/agent-retry-budget.md):采纳 A(缺省
retry=0 升格为书面产品承诺)+C(dry-run 组合警告),否决 B(准入硬拦)。
触发条件刻意排除 llm——单次 API 调用重试近乎免费,warn 会稀释注意力;
agent 的执行单元是 CLI 自主多轮循环,重跑一次等于把整份开销原样复制
(2026-08-19 阶段 D 的 $10.508 事故路径)。

措辞红线:警告是建议性信息,只能"提示/警示",不得表述成已阻止;
准入行为零变化。
"""
from fastapi.testclient import TestClient

from atlas import mcp as mcp_module
from atlas.adapters import FakeProvider
from atlas.mcp import _dry_run_warnings
from atlas.spec import spec_from_yaml
from atlas.web import create_app

from conftest import make_registry

_NO_CONFIG = dict(provider_cfgs={}, capabilities={},
                  rates_known_fn=lambda _: False)


def _warnings(yaml_text: str, **kwargs):
    spec = spec_from_yaml(yaml_text)
    return _dry_run_warnings(spec, **{**_NO_CONFIG, **kwargs})


def _stub_runner(attachment, **kwargs):
    """dry-run 不执行它;只需一个非 None 的 runner 对象通过预检身份冻结。"""
    return "ok"


# ─────────────────── 正例:agent 节点 retry>0 ───────────────────


def test_k_research_retry_warns_with_three_required_elements():
    warnings = _warnings("""
name: k-research-retry
nodes:
  - id: scout
    type: research
    model: Fake:cli
    prompt: 调研并汇总。
    retry: 2
    consumes: [task]
edges:
  - from: scout
    to: END
""")
    assert len(warnings) == 1
    w = warnings[0]
    # 要素1:重跑次数(「失败后将自动重跑至多 N 次」)
    assert "scout 失败后将自动重跑至多 2 次" in w
    # 要素2:未设 max_cost_usd → 没有任何总量约束
    assert "没有任何总量约束" in w
    # 要素3:结构性替代建议
    assert "max_iterations" in w and "timeout_s" in w
    # 措辞红线:提示/警示,不是阻止
    assert "已阻止" not in w
    assert "拒绝运行" not in w


def test_k_coding_agent_retry_with_cap_states_conservative_accounting(tmp_path):
    warnings = _warnings(f"""
name: k-coding-retry
nodes:
  - id: coder
    type: coding_agent
    workdir: {tmp_path.as_posix()}
    model: Fake:cli
    prompt: 实施改动并自测。
    retry: 1
    consumes: [task]
edges:
  - from: coder
    to: END
guards:
  max_cost_usd: 0.5
""")
    assert len(warnings) == 1
    w = warnings[0]
    assert "coder 失败后将自动重跑至多 1 次" in w
    # 要素2(设了帽):保守口径占用 + 不能证明未超帽,不是"已精确封顶"
    assert "max_cost_usd" in w
    assert "保守口径" in w
    assert "不能证明" in w


def test_k_multiple_agent_hits_merge_into_one_warning(tmp_path):
    """同图多个命中节点合并成一条列表型警告,避免刷屏稀释。"""
    warnings = _warnings(f"""
name: k-merge
nodes:
  - id: scout
    type: research
    model: Fake:cli
    prompt: 调研。
    retry: 1
    consumes: [task]
  - id: coder
    type: coding_agent
    workdir: {tmp_path.as_posix()}
    model: Fake:cli
    prompt: 实施。
    retry: 3
    consumes: [task, scout.output]
edges:
  - from: scout
    to: coder
  - from: coder
    to: END
""")
    assert len(warnings) == 1
    assert "scout 失败后将自动重跑至多 1 次" in warnings[0]
    assert "coder 失败后将自动重跑至多 3 次" in warnings[0]


# ─────────────────── 反例:llm / 未显式声明 retry ───────────────────


def test_k_llm_retry_never_triggers_the_warning():
    """llm 的 retry 不触发(单次成本低,warn 会稀释注意力)——防误伤断言。"""
    warnings = _warnings("""
name: k-llm-retry
nodes:
  - id: only
    type: llm
    model: Fake:primary
    prompt: 产出结论。
    retry: 3
    consumes: [task]
edges:
  - from: only
    to: END
""")
    assert warnings == []


def test_k_agent_without_explicit_retry_stays_silent():
    """Q1 裁决:缺省 0 是书面承诺;旧图(未写 retry)不加追溯警告也不拒绝。"""
    warnings = _warnings("""
name: k-default-retry
nodes:
  - id: scout
    type: research
    model: Fake:cli
    prompt: 调研。
    consumes: [task]
edges:
  - from: scout
    to: END
""")
    assert warnings == []


def test_k_human_retry_field_is_not_writable_so_never_triggers():
    """human 节点没有 retry 字段(spec 层封闭),任何触发路径都不存在。"""
    warnings = _warnings("""
name: k-human
nodes:
  - id: gate
    type: human
    prompt: 审阅后批准或驳回。
    consumes: [task]
edges:
  - from: gate
    to: END
""")
    assert warnings == []


# ─────────────────── 端到端:MCP dry-run 与 Web preview 同源 ───────────────────

RETRY_WORKFLOW_YAML = """
name: k-e2e
nodes:
  - id: scout
    type: research
    model: Fake:cli
    prompt: 调研。
    retry: 2
    consumes: [task]
edges:
  - from: scout
    to: END
"""


def _write_workflow(tmp_path: "object") -> "object":
    workflows = tmp_path / "workflows"
    workflows.mkdir(exist_ok=True)
    (workflows / "k.yaml").write_text(RETRY_WORKFLOW_YAML.strip(), encoding="utf-8")
    return workflows


def test_k_warning_flows_through_mcp_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_module, "WORKFLOWS_DIR", _write_workflow(tmp_path))
    monkeypatch.setattr(mcp_module, "RUNS_DIR", tmp_path / "runs")
    dry = mcp_module.dry_run_impl(
        "k", "task",
        registry_factory=lambda _: make_registry(FakeProvider()),
        agent_runner_factory=lambda spec: _stub_runner)
    assert dry["dry_run"] is True
    k_warnings = [w for w in dry["warnings"]
                  if "自动重跑至多 2 次" in w]
    assert len(k_warnings) == 1
    assert "没有任何总量约束" in k_warnings[0]


def test_k_warning_flows_through_web_preview_same_source(tmp_path):
    app = create_app(
        workflows_dir=_write_workflow(tmp_path), runs_dir=tmp_path / "runs",
        registry_factory=lambda _: make_registry(FakeProvider()),
        agent_runner_factory=lambda spec: _stub_runner)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        preview = client.post(
            "/api/workflows/k/preview", headers={"X-Atlas-Request": "1"},
            json={})
    assert preview.status_code == 200
    payload = preview.json()
    # 同源纪律:preview 的 warnings 与 MCP dry-run 出自同一构建函数
    k_warnings = [w for w in payload["warnings"]
                  if "自动重跑至多 2 次" in w]
    assert len(k_warnings) == 1
    assert "没有任何总量约束" in k_warnings[0]
