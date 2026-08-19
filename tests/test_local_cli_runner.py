# -*- coding: utf-8 -*-
"""local_cli 生产形态：桩 Claude CLI 的子进程、安全环境与结果契约。"""
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from atlas.config import (AgentCliConfig, AgentRunnerConfig, ConfigError,
                          ProviderConfig)
from atlas.credentials import EnvStore
from atlas.nodes.agent import AgentCliError
from atlas.nodes.local_cli import (
    LocalCliRunner,
    _base_child_env,
    _check_cli_contract,
    _parse_result,
    _require_clean_git_workdir,
)


@pytest.fixture
def provider():
    return ProviderConfig(
        id="Stub", openai_base_url=None,
        anthropic_base_url="https://stub.invalid/v1",
        api_key_env="STUB_KEY", models=("model-a",),
    )


def _stub_program(tmp_path: Path) -> Path:
    script = tmp_path / "stub_cli.py"
    script.write_text(r'''# -*- coding: utf-8 -*-
import json, os, pathlib, sys
if "--help" in sys.argv:
    print("--print --safe-mode --bare --no-session-persistence --no-chrome --output-format --permission-mode --model --tools --allowedTools --max-budget-usd")
    raise SystemExit(0)
if "--version" in sys.argv:
    print("2.1.228 (Claude Code)")
    raise SystemExit(0)
raw = sys.stdin.buffer.read()
record = {
    "argv": sys.argv[1:],
    "cwd": str(pathlib.Path.cwd()),
    "stdin": raw.decode("utf-8"),
    "secret": os.environ.get("ANTHROPIC_API_KEY"),
    "base": os.environ.get("ANTHROPIC_BASE_URL"),
    "sentinel": os.environ.get("ATLAS_TEST_SENTINEL"),
}
(pathlib.Path.cwd() / "stub-record.json").write_text(
    json.dumps(record, ensure_ascii=False), encoding="utf-8")
print(json.dumps({
    "result": "stub report",
    "usage": {"input_tokens": 17, "output_tokens": 5},
    "total_cost_usd": 0.0123,
}))
''', encoding="utf-8")
    launcher = tmp_path / ("stub.cmd" if os.name == "nt" else "stub")
    if os.name == "nt":
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        launcher.write_text(f'#!{sys.executable}\nexec "{sys.executable}" "{script}" "$@"\n',
                            encoding="utf-8")
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    return launcher


def _runner(tmp_path: Path, provider: ProviderConfig) -> LocalCliRunner:
    env = tmp_path / ".env"
    env.write_text("STUB_KEY=temporary-test-key\n", encoding="utf-8")
    config = AgentRunnerConfig(
        runner="local_cli",
        cli=AgentCliConfig(kind="claude", command="stub", extra_args=()),
    )
    runner = LocalCliRunner(config, {provider.id: provider}, EnvStore(env),
                            str(_stub_program(tmp_path)))
    runner.freeze_provider_credential(provider)
    return runner


def test_child_environment_is_allowlisted():
    env = _base_child_env({"PATH": "x", "SYSTEMROOT": "y",
                           "UNRELATED_SECRET": "must-not-leak"})
    assert env == {"PATH": "x", "SYSTEMROOT": "y"}


def test_local_cli_receives_stdin_cwd_tools_env_and_usage(
        tmp_path, provider, monkeypatch):
    monkeypatch.setenv("ATLAS_TEST_SENTINEL", "must-not-leak")
    runner = _runner(tmp_path, provider)
    (tmp_path / ".env").write_text(
        "STUB_KEY=changed-after-preflight\n", encoding="utf-8")
    descriptor = json.dumps(runner.execution_descriptor())
    assert "temporary-test-key" not in descriptor
    assert "changed-after-preflight" not in descriptor
    attachment = tmp_path / "projection.txt"
    attachment.write_text("projection payload", encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    result = runner(
        attachment, node_type="coding_agent", max_turns=12,
        cwd=worktree, writable=True, allow_web=False, allowed_paths=[],
        timeout_s=10, model_ref="Stub:model-a", node_id="coder",
        max_budget_usd=0.5)

    record = json.loads((worktree / "stub-record.json").read_text(encoding="utf-8"))
    assert record["cwd"] == str(worktree)
    assert record["stdin"] == "projection payload"
    assert record["secret"] == "temporary-test-key"
    assert record["base"] == "https://stub.invalid/v1"
    assert record["sentinel"] is None
    argv = record["argv"]
    assert argv[argv.index("--model") + 1] == "model-a"
    expected_tools = ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]
    tools = argv[argv.index("--tools") + 1:argv.index("--allowedTools")]
    allowed = argv[argv.index("--allowedTools") + 1:argv.index("--max-budget-usd")]
    assert tools == expected_tools
    assert allowed == expected_tools
    assert "WebSearch" not in argv and "WebFetch" not in argv
    assert result.text == "stub report"
    assert result.usage.input_tokens == 17
    assert result.usage.output_tokens == 5
    assert result.cost_usd == 0.0123


def test_allow_web_adds_only_web_tools(tmp_path, provider):
    runner = _runner(tmp_path, provider)
    projections = tmp_path / "projections"
    projections.mkdir()
    attachment = projections / "projection.txt"
    attachment.write_text("x", encoding="utf-8")
    extra = tmp_path / "read-only-input"
    extra.mkdir()
    result = runner(
        attachment, node_type="research", max_turns=12,
        writable=False, allow_web=True, allowed_paths=[str(extra)], timeout_s=10,
        model_ref="Stub:model-a", node_id="research")
    assert result.text == "stub report"
    record = next(tmp_path.glob("atlas-research-research-*/stub-record.json"))
    argv = json.loads(record.read_text(encoding="utf-8"))["argv"]
    assert "WebSearch" in argv and "WebFetch" in argv
    assert "Edit" not in argv and "Write" not in argv and "Bash" not in argv
    add_dir = argv.index("--add-dir")
    assert argv[add_dir + 1] == str(extra)


def test_writable_agent_rejects_allowed_paths(tmp_path, provider):
    runner = _runner(tmp_path, provider)
    attachment = tmp_path / "projection.txt"
    attachment.write_text("x", encoding="utf-8")
    with pytest.raises(AgentCliError, match="allowed_paths"):
        runner(attachment, node_type="coding_agent", max_turns=12,
               cwd=tmp_path, writable=True, allowed_paths=[str(tmp_path)],
               model_ref="Stub:model-a")


def test_result_parser_rejects_invalid_and_empty_json():
    with pytest.raises(AgentCliError, match="合法 JSON"):
        _parse_result(b"not-json")
    with pytest.raises(AgentCliError, match="空报告"):
        _parse_result(b'{"result":""}')


def test_cli_contract_rejects_missing_required_flags(tmp_path):
    from atlas.nodes.local_cli import _check_cli_contract

    script = tmp_path / "bad-cli.cmd"
    script.write_text('@echo --print --model\r\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="缺少 Atlas 必需参数"):
        _check_cli_contract(str(script), _base_child_env())


def _runner_for_source(tmp_path: Path, provider: ProviderConfig,
                       source: str, name: str = "custom") -> LocalCliRunner:
    script = tmp_path / f"{name}.py"
    script.write_text(source, encoding="utf-8")
    launcher = tmp_path / (f"{name}.cmd" if os.name == "nt" else name)
    if os.name == "nt":
        launcher.write_text(
            f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        launcher.write_text(
            f'#!{sys.executable}\nexec "{sys.executable}" "{script}" "$@"\n',
            encoding="utf-8")
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    env = tmp_path / f"{name}.env"
    env.write_text("STUB_KEY=temporary-test-key\n", encoding="utf-8")
    config = AgentRunnerConfig(
        runner="local_cli",
        cli=AgentCliConfig(kind="claude", command=str(launcher), extra_args=()),
    )
    runner = LocalCliRunner(
        config, {provider.id: provider}, EnvStore(env), str(launcher))
    runner.freeze_provider_credential(provider)
    return runner


def test_nonzero_exit_reports_stdout_and_redacts_stderr(tmp_path, provider):
    runner = _runner_for_source(tmp_path, provider, r'''
import os, sys
print("useful stdout evidence")
print("secret=" + os.environ["ANTHROPIC_API_KEY"], file=sys.stderr)
raise SystemExit(7)
''', "nonzero")
    attachment = tmp_path / "projection.txt"
    attachment.write_text("x", encoding="utf-8")
    cwd = tmp_path / "nonzero-cwd"
    cwd.mkdir()

    with pytest.raises(AgentCliError) as exc:
        runner(attachment, node_type="research", max_turns=1, cwd=cwd,
               writable=False, timeout_s=5, model_ref="Stub:model-a")
    message = str(exc.value)
    assert "退出码 7" in message and "useful stdout evidence" in message
    assert "[REDACTED]" in message and "temporary-test-key" not in message


@pytest.mark.parametrize(("stream", "limit_name"), [
    ("stdout", "_STDOUT_MAX_BYTES"),
    ("stderr", "_STDERR_MAX_BYTES"),
])
def test_cli_output_over_limit_fails_loudly(
        tmp_path, provider, monkeypatch, stream, limit_name):
    from atlas.nodes import local_cli as local_cli_mod
    monkeypatch.setattr(local_cli_mod, limit_name, 32)
    write = "sys.stdout.write('x' * 33)" if stream == "stdout" \
        else "sys.stderr.write('x' * 33)"
    runner = _runner_for_source(
        tmp_path, provider, f"import sys\n{write}\n", f"large-{stream}")
    attachment = tmp_path / f"{stream}.txt"
    attachment.write_text("x", encoding="utf-8")
    cwd = tmp_path / f"{stream}-cwd"
    cwd.mkdir()

    with pytest.raises(AgentCliError, match="输出超过上限"):
        runner(attachment, node_type="research", max_turns=1, cwd=cwd,
               writable=False, timeout_s=5, model_ref="Stub:model-a")


@pytest.mark.skipif(os.name != "nt", reason="Windows taskkill /T contract")
def test_timeout_terminates_descendant_process(tmp_path, provider):
    marker = tmp_path / "descendant-survived.txt"
    child_source = (
        "import pathlib,time;time.sleep(1);"
        f"pathlib.Path({str(marker)!r}).write_text('survived')"
    )
    runner = _runner_for_source(tmp_path, provider, f'''
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", {child_source!r}])
time.sleep(20)
''', "timeout-tree")
    attachment = tmp_path / "timeout.txt"
    attachment.write_text("x", encoding="utf-8")
    cwd = tmp_path / "timeout-cwd"
    cwd.mkdir()

    with pytest.raises(AgentCliError, match="进程树已终止"):
        runner(attachment, node_type="research", max_turns=1, cwd=cwd,
               writable=False, timeout_s=0.2, model_ref="Stub:model-a",
               node_id="timeout")
    import time
    time.sleep(1.2)
    assert not marker.exists(), "taskkill /T 必须终止孙进程"


def test_taskkill_failure_is_not_reported_as_success(monkeypatch):
    from atlas.nodes import local_cli as local_cli_mod

    class FakeProcess:
        pid = 123
        def poll(self): return None
        def wait(self, timeout): return 0
        def kill(self): return None

    def failed_taskkill(*args, **kwargs):
        raise local_cli_mod.subprocess.TimeoutExpired("taskkill", 10)

    monkeypatch.setattr(local_cli_mod.os, "name", "nt")
    monkeypatch.setattr(local_cli_mod.subprocess, "run", failed_taskkill)
    with pytest.raises(AgentCliError, match="无法确认.*进程树"):
        local_cli_mod._terminate_process_tree(FakeProcess())


def test_preflight_rejects_dirty_writable_workdir(tmp_path, provider):
    import subprocess
    from types import SimpleNamespace
    from atlas.nodes.local_cli import preflight_agent_nodes

    project = tmp_path / "project"
    project.mkdir()
    (project / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "base"], cwd=project, check=True)
    (project / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    runner = _runner(tmp_path, provider)
    node = SimpleNamespace(
        id="coder", model="Stub:model-a", writable=True,
        allowed_paths=(), type="coding_agent", workdir=str(project))
    with pytest.raises(ConfigError, match="未提交改动"):
        preflight_agent_nodes([node], runner)
