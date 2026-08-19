# -*- coding: utf-8 -*-
"""Reusable phase-C release gates stay offline and enforce their contracts."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tarfile
from pathlib import Path
from subprocess import CompletedProcess

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


workflow_gate = _load_script("release_workflow_gate")
clean_init_gate = _load_script("release_clean_init_gate")
sdist_gate = _load_script("release_sdist_gate")


def test_workflow_gate_configures_all_six_without_calls_or_runs(tmp_path):
    shutil.copytree(ROOT / "workflows", tmp_path / "workflows")
    shutil.copytree(ROOT / "demo-project", tmp_path / "demo-project")

    result = workflow_gate.run_workflow_gate(tmp_path)

    assert result["workflow_count"] == 6
    assert result["registry_preflights"] == 6
    assert result["runner_preflights"] == 6
    assert result["provider_calls"] == 0
    assert result["agent_calls"] == 0
    assert result["run_directories"] == 0
    assert not (tmp_path / "runs").exists()
    assert all(item["configured_nodes"] for item in result["workflows"])
    assert all(len(item["execution_sha256"]) == 64 for item in result["workflows"])


def test_clean_init_gate_uses_exact_templates_and_silent_mcp():
    result = clean_init_gate.run_clean_init_gate(ROOT)

    assert set(result["templates_checked"]) == {
        active for _, active in clean_init_gate.CONFIG_TEMPLATES}
    assert result["first_init_returncode"] == 0
    assert result["second_init_returncode"] == 0
    assert result["agents_runner"] == "fail_closed"
    assert result["mcp_stdout_bytes"] == 0


def _write_sdist(path: Path, *, extra: dict[str, bytes] | None = None) -> None:
    files = {
        name: b"release-gate fixture\n"
        for name in sdist_gate._REQUIRED | sdist_gate._EXPECTED_WORKFLOWS
    }
    files["pyproject.toml"] = (
        b'[project]\nname = "atlas"\nversion = "0.1.0rc1"\n')
    files["config/agents.example.json"] = b'{"runner":"fail_closed"}\n'
    files.update(extra or {})
    with tarfile.open(path, "w:gz") as bundle:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(f"atlas-0.1.0rc1/{name}")
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))


def test_sdist_scan_accepts_required_release_surface_and_rejects_active_config(tmp_path):
    good = tmp_path / "good.tar.gz"
    _write_sdist(good)
    assert sdist_gate.scan_sdist(good)["workflows"] == 6

    bad = tmp_path / "bad.tar.gz"
    _write_sdist(bad, extra={"config/providers.json": b'{"providers":[]}\n'})
    with pytest.raises(AssertionError, match="active configuration"):
        sdist_gate.scan_sdist(bad)


@pytest.mark.parametrize("name", [
    "state.sqlite-wal",
    "state.sqlite-shm",
    "state.db-wal",
    "state.db-shm",
    "config/.env.local",
    "build.log",
    "coverage/coverage.json",
    "htmlcov/index.html",
    ".coverage.worker",
    "coverage.xml",
])
def test_sdist_scan_rejects_runtime_and_coverage_artifacts(tmp_path, name):
    archive = tmp_path / "bad.tar.gz"
    _write_sdist(archive, extra={name: b"generated\n"})

    with pytest.raises(AssertionError, match="banned files in sdist"):
        sdist_gate.scan_sdist(archive)


def test_sdist_scan_allows_explicit_env_template(tmp_path):
    archive = tmp_path / "template.tar.gz"
    _write_sdist(archive)

    assert sdist_gate.scan_sdist(archive)["findings"] == 0


@pytest.mark.parametrize(("label", "value"), [
    ("OpenAI API key", b"sk-proj-" + b"A" * 48),
    ("Anthropic API key", b"sk-ant-api03-" + b"B" * 48),
    ("Google API key", b"AI" + b"za" + b"C" * 35),
    ("Slack token", b"xoxb-123456789012-" + b"D" * 32),
])
def test_sdist_scan_rejects_high_confidence_provider_secrets(tmp_path, label, value):
    archive = tmp_path / "secret.tar.gz"
    _write_sdist(archive, extra={"notes.txt": value + b"\n"})

    with pytest.raises(AssertionError, match=label):
        sdist_gate.scan_sdist(archive)


@pytest.mark.parametrize("value", [
    b"sk-test",
    b"sk-ant-fake",
    b"AIzaFakeKey",
    b"xoxb-fake",
])
def test_sdist_scan_does_not_flag_short_fake_provider_keys(tmp_path, value):
    archive = tmp_path / "fake.tar.gz"
    _write_sdist(archive, extra={"notes.txt": value + b"\n"})

    assert sdist_gate.scan_sdist(archive)["findings"] == 0


def test_ci_and_release_use_shared_gates_without_manual_active_config():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    for workflow in (ci, release):
        assert "scripts/release_workflow_gate.py" in workflow
        assert "scripts/release_clean_init_gate.py" in workflow
        assert "scripts/release_sdist_gate.py" in workflow
        assert "Copy-Item config/" not in workflow
        assert "cp config/" not in workflow
        assert "test:diff" not in workflow
    assert release.count("npm --prefix web ci") == 1
    assert release.count("npm --prefix web test") == 1
    assert release.count("npm --prefix web run lint") == 1
    assert release.count("npm --prefix web run build") == 1
    assert "environment: release" in release
    assert "concurrency:" in release
    assert "Verify SHA256 manifest" in release


def test_remote_jobs_build_web_before_python_tests():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    windows, ubuntu = ci.split("  ubuntu-compatibility-signal:", 1)

    for job in (windows, ubuntu, release):
        assert job.index("npm --prefix web run build") < job.index("uv run pytest")


def test_all_actions_are_pinned_to_full_commit_shas_and_jobs_have_timeouts():
    action_ref = re.compile(r"^\s*-?\s*uses:\s*[^@\s]+@([^\s#]+)", re.MULTILINE)
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        workflow = path.read_text(encoding="utf-8")
        refs = action_ref.findall(workflow)
        assert refs, f"no action references found in {path.name}"
        assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in refs), path.name
        assert "timeout-minutes:" in workflow, path.name


def test_sdist_smoke_is_python312_offline_with_core_contracts(tmp_path, monkeypatch):
    archive = tmp_path / "atlas.tar.gz"
    _write_sdist(archive)
    commands: list[list[str]] = []

    monkeypatch.setattr(sdist_gate.shutil, "which", lambda name: "uv")

    def fake_run(command, *, cwd, env):
        commands.append(command)
        if "venv" in command:
            venv = Path(command[-1])
            python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python.parent.mkdir(parents=True)
            python.touch()
            return CompletedProcess(command, 0, "", "")
        if "pip" in command:
            return CompletedProcess(command, 0, "", "")
        output = json.dumps({
            "version": "0.1.0rc1",
            "python": [3, 12],
            "scripts": ["atlas", "atlas-mcp", "atlas-web"],
            "tools": [
                "atlas_get_run", "atlas_list_workflows", "atlas_resume_run",
                "atlas_run_workflow", "atlas_save_workflow",
                "atlas_validate_workflow",
            ],
            "spec": "smoke",
            "initialized": [
                "providers.json", "models.reference.json", "capabilities.json",
                "pricing.json", "agents.json", ".env",
            ],
        })
        return CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(sdist_gate, "_run", fake_run)
    result = sdist_gate.smoke_install_sdist(archive)

    assert result["offline"] is True
    assert result["dependencies_installed"] is True
    assert result["spec_parser"] is True
    assert result["config_init"] is True
    assert len(result["mcp_tools"]) == 6
    assert "--no-python-downloads" in commands[0]
    install = commands[1]
    assert "--offline" in install and "--no-deps" not in install
    assert commands[2][1:3] == ["-I", "-c"]
