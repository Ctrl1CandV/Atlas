# -*- coding: utf-8 -*-
"""research / coding_agent 节点：显式注入测试 runner，不启动生产沙箱。

这里验证引擎侧的参数、隔离副本、diff 产物、A1 语义与失败契约；
生产默认路径的 fail-closed 行为在安全回归测试中单独覆盖。
"""
import json
from pathlib import Path

import pytest

from atlas.adapters import FakeProvider
from atlas.engine import execute_graph
from atlas.integrity import sha256_bytes
from atlas.nodes.agent import AgentCliError
from atlas.spec import SpecError, spec_from_yaml

from conftest import TASK_TEXT, make_registry


class FakeAgentRunner:
    """记录调用参数、可编程输出的假 CLI。"""

    def __init__(self, outputs=None, fail_times=0):
        self.calls: list[dict] = []
        self.outputs = outputs or {}
        self.fail_times = fail_times

    def __call__(self, attachment, *, node_type, max_turns, cwd=None, **kw):
        self.calls.append({
            "attachment": Path(attachment),
            "node_type": node_type,
            "max_turns": max_turns,
            "cwd": cwd,
            "kw": kw,   # M4 参数(writable/allow_web/allowed_paths/timeout_s)
        })
        if self.fail_times > 0:
            self.fail_times -= 1
            raise AgentCliError("CLI 退出码 1,无输出。stderr:(空)")
        text = self.outputs.get(node_type, f"{node_type} 的报告")
        return text


def _registry():
    fake = FakeProvider()
    fake.configure("primary", text="上游分析完成。")
    return make_registry(fake)


def test_research_node_reads_only_and_produces_artifact(tmp_path):
    runner = FakeAgentRunner(outputs={"research": "# 调研报告\n三条发现。"})
    spec = spec_from_yaml("""
name: research_demo
nodes:
  - id: scout
    type: research
    prompt: 调研任务涉及的材料与方向。
    consumes: [task]
    max_turns: 8
  - id: judge
    type: llm
    model: Fake:primary
    prompt: 评审调研报告。
    consumes: [task, scout.output]
edges:
  - from: scout
    to: judge
  - from: judge
    to: END
""")
    run = execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                        registry=_registry(), agent_runner=runner)

    assert run.folded()["status"] == "done"
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["node_type"] == "research"
    assert call["max_turns"] == 8
    assert call["cwd"] is None          # research 没有工作目录(只读)
    # A1 语义:投影附件里含 task 原文;产物是 runner 的原文
    attachment = call["attachment"].read_bytes()
    assert TASK_TEXT.encode("utf-8") in attachment
    done = run.events.find(type="node_done", node="scout")
    assert done["model_used"] == "agent:research"
    assert Path(done["output_path"]).read_bytes() == "# 调研报告\n三条发现。".encode()
    # 下游拿到了完整产物
    judge_in = run.events.find(type="node_input", node="judge")
    src = {c["name"]: c for c in judge_in["consumed"]}["scout.output"]
    assert sha256_bytes(Path(src["path"]).read_bytes()) == src["sha256"]


def test_coding_agent_isolated_copy_and_diff(tmp_path):
    # 目标项目:一个带 git 的小仓库
    project = tmp_path / "target"
    project.mkdir()
    (project / "app.py").write_text("def add(a, b):\n    return a + b\n",
                                    encoding="utf-8")
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True,
                   capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=project, check=True,
                   capture_output=True)

    def fake_runner(attachment, *, node_type, max_turns, cwd=None, **kw):
        # 模拟 agent 在副本里改文件
        assert cwd is not None and cwd != project, "必须在隔离副本里执行"
        (cwd / "app.py").write_text(
            "def add(a, b):\n    return a + b + 1  # 改动\n", encoding="utf-8")
        return "改动摘要:app.py 加了 +1,测试通过。"

    spec = spec_from_yaml(f"""
name: coding_demo
nodes:
  - id: implementer
    type: coding_agent
    prompt: 把 add 改成带 +1 的版本并自测。
    consumes: [task]
    workdir: {project.as_posix()}
    max_turns: 6
edges:
  - from: implementer
    to: END
""")
    run = execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                        registry=_registry(), agent_runner=fake_runner)

    assert run.folded()["status"] == "done"
    # 原目录分毫未动(隔离红线)
    assert (project / "app.py").read_text(encoding="utf-8") == \
        "def add(a, b):\n    return a + b\n"
    # 副本在 run 目录下
    done = run.events.find(type="node_done", node="implementer")
    worktrees = list((run.dir / "worktrees").iterdir())
    assert len(worktrees) == 1
    assert "+ 1" in (worktrees[0] / "app.py").read_text(encoding="utf-8")
    # diff 是第二产物,git diff 抓到了改动
    diff_path = Path(done["diff_path"])
    assert diff_path.exists()
    assert "app.py" in diff_path.read_text(encoding="utf-8")
    assert sha256_bytes(diff_path.read_bytes()) == done["diff_sha256"]
    # 两个产物都在产物库里,下游可引用
    assert "implementer.output" in run.artifacts
    assert "implementer.diff" in run.artifacts


def test_coding_agent_requires_workdir(tmp_path):
    with pytest.raises(SpecError, match="workdir"):
        spec_from_yaml("""
name: bad
nodes:
  - id: x
    type: coding_agent
    prompt: 改代码。
    consumes: [task]
""")


def test_agent_model_optional_and_validated(tmp_path):
    """agent 的 model 可选(驱动 claude 网关);格式不合法仍拒绝。"""
    # 合法引用:通过
    spec = spec_from_yaml("""
name: ok
nodes:
  - id: x
    type: research
    model: Kiro:claude-sonnet-5
    prompt: p
    consumes: [task]
edges:
  - from: x
    to: END
""")
    assert spec.nodes[0].model == "Kiro:claude-sonnet-5"
    # 非法格式:拒绝
    with pytest.raises(SpecError, match="供应商id:模型id"):
        spec_from_yaml("""
name: bad
nodes:
  - id: x
    type: research
    model: not-a-ref
    prompt: p
    consumes: [task]
""")
    # human 仍然禁止 model
    with pytest.raises(SpecError, match="model 必须省略"):
        spec_from_yaml("""
name: bad2
nodes:
  - id: x
    type: human
    model: Fake:primary
    prompt: p
    consumes: [task]
edges:
  - from: x
    to: END
""")


def test_agent_empty_output_fails_without_retry(tmp_path):
    """retry 默认 0(M4 起):失败即失败,不内置重试。"""
    runner = FakeAgentRunner(fail_times=5)
    spec = spec_from_yaml("""
name: flaky
nodes:
  - id: solo
    type: research
    prompt: p
    consumes: [task]
edges:
  - from: solo
    to: END
""")
    with pytest.raises(AgentCliError):
        execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                      registry=_registry(), agent_runner=runner)
    logs = list(tmp_path.glob("*/events.jsonl"))
    from atlas.events import EventReader
    reader = EventReader(logs[0])
    assert len(reader.filter(type="model_failed")) == 1   # retry=0:只试一次
    assert reader.find(type="run_failed")["error_type"] == "AgentCliError"


def test_agent_retry_param_drives_attempts(tmp_path):
    """A9(retry):retry: 1 → 同一节点尝试两次,第一次失败第二次成功。"""
    runner = FakeAgentRunner(fail_times=1)
    spec = spec_from_yaml("""
name: flaky-r
nodes:
  - id: solo
    type: research
    prompt: p
    consumes: [task]
    retry: 1
edges:
  - from: solo
    to: END
""")
    run = execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                        registry=_registry(), agent_runner=runner)
    assert run.folded()["status"] == "done"
    assert len(runner.calls) == 2
    assert run.events.find(type="model_failed") is not None


def test_agent_transient_failure_recovered_on_retry(tmp_path):
    runner = FakeAgentRunner(fail_times=1)  # 第一次失败,重试成功
    spec = spec_from_yaml("""
name: flaky2
nodes:
  - id: solo
    type: research
    prompt: p
    consumes: [task]
    retry: 1
edges:
  - from: solo
    to: END
""")
    run = execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                        registry=_registry(), agent_runner=runner)
    assert run.folded()["status"] == "done"
    assert len(runner.calls) == 2
    assert run.events.find(type="model_failed") is not None
    assert run.events.find(type="node_done", node="solo") is not None
