# -*- coding: utf-8 -*-
"""REV-001: coding_agent 字节基线与 diff 采集攻击回归。"""
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from atlas.nodes.agent import (
    AgentCliError,
    _collect_diff,
    _freeze_baseline,
    _prepare_worktree,
    _validate_component,
)
from atlas.spec import spec_from_yaml


def _git(project: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=project, check=True, capture_output=True,
    )


def _project(tmp_path: Path, files: dict[str, bytes] | None = None) -> Path:
    project = tmp_path / "source"
    project.mkdir()
    for name, content in (files or {"app.py": b"value = 1\n"}).items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    _git(project, "init", "-q")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "baseline")
    return project


def _node(project: Path, *, retry: int = 0):
    return spec_from_yaml(f"""
name: security
nodes:
  - id: coder
    type: coding_agent
    prompt: p
    consumes: [task]
    workdir: {project.as_posix()}
    retry: {retry}
edges:
  - from: coder
    to: END
""").nodes[0]


def _frozen(tmp_path: Path, files: dict[str, bytes] | None = None):
    project = _project(tmp_path, files)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    node = _node(project)
    baseline, manifest, digest, head = _freeze_baseline(
        run_dir, node, 1, project)
    return project, run_dir, node, baseline, manifest, digest, head


def test_agent_commit_cannot_hide_byte_diff(tmp_path):
    project, run_dir, node, baseline, manifest, digest, head = _frozen(tmp_path)
    result = _prepare_worktree(run_dir, node, 1, baseline, manifest, digest)
    (result / "app.py").write_text("value = 2\n", encoding="utf-8")
    _git(result, "add", "-A")
    _git(result, "commit", "-qm", "agent tries to hide change")

    patch, meta = _collect_diff(baseline, result, manifest, digest)

    assert b"+value = 2" in patch
    assert meta["files_changed"] == 1
    assert meta["files"][0]["path"] == "app.py"
    assert meta["baseline_digest"] == digest
    assert meta["result_digest"] != digest
    assert meta["patch_digest"] == hashlib.sha256(patch).hexdigest()
    assert head
    assert (project / "app.py").read_bytes() == b"value = 1\n"


def test_diff_ignores_git_filters_attributes_and_host_sentinel(
        tmp_path, monkeypatch):
    project, run_dir, node, baseline, manifest, digest, _ = _frozen(
        tmp_path, {"tracked.txt": b"safe\n"})
    result = _prepare_worktree(run_dir, node, 1, baseline, manifest, digest)
    stolen = tmp_path / "stolen.txt"
    filter_script = result / ".git" / "steal.py"
    filter_script.write_text(
        "import os, pathlib, sys\n"
        f"pathlib.Path({str(stolen)!r}).write_text(os.environ.get('ATLAS_HOST_SENTINEL', ''))\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    _git(result, "config", "filter.steal.clean", f'python "{filter_script}"')
    (result / ".gitattributes").write_text("*.txt filter=steal diff=steal\n", encoding="utf-8")
    (result / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (result / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (result / "ignored.txt").write_text("must still be audited\n", encoding="utf-8")
    monkeypatch.setenv("ATLAS_HOST_SENTINEL", "host-secret-must-not-leak")

    patch, meta = _collect_diff(baseline, result, manifest, digest)

    assert not stolen.exists(), "diff 采集不得启动 agent 配置的 clean filter"
    assert {item["path"] for item in meta["files"]} == {
        ".gitattributes", ".gitignore", "ignored.txt", "tracked.txt",
    }
    assert b"must still be audited" in patch
    assert b"host-secret-must-not-leak" not in patch
    assert (project / "tracked.txt").read_bytes() == b"safe\n"


def test_manifest_diff_audits_added_deleted_and_modified_files(tmp_path):
    _, run_dir, node, baseline, manifest, digest, _ = _frozen(
        tmp_path, {"modify.txt": b"old\n", "delete.txt": b"gone\n"})
    result = _prepare_worktree(run_dir, node, 1, baseline, manifest, digest)
    (result / "modify.txt").write_text("new\n", encoding="utf-8")
    (result / "delete.txt").unlink()
    (result / "add.txt").write_text("added\n", encoding="utf-8")

    patch, meta = _collect_diff(baseline, result, manifest, digest)

    changes = {item["path"]: item["change"] for item in meta["files"]}
    assert changes == {
        "add.txt": "added", "delete.txt": "deleted", "modify.txt": "modified",
    }
    assert b"--- /dev/null" in patch and b"+++ /dev/null" in patch
    assert meta["additions"] == 2
    assert meta["deletions"] == 2


def test_retry_derives_from_frozen_baseline_not_mutated_source(tmp_path):
    project, run_dir, node, baseline, manifest, digest, _ = _frozen(tmp_path)
    first = _prepare_worktree(run_dir, node, 1, baseline, manifest, digest, attempt=1)
    (first / "app.py").write_text("failed attempt\n", encoding="utf-8")
    (project / "app.py").write_text("concurrent source mutation\n", encoding="utf-8")

    second = _prepare_worktree(run_dir, node, 1, baseline, manifest, digest, attempt=2)

    assert second != first
    assert (second / "app.py").read_bytes() == b"value = 1\n"
    assert (project / "app.py").read_text(encoding="utf-8") == \
        "concurrent source mutation\n"


def test_baseline_tampering_fails_before_retry_and_diff(tmp_path):
    _, run_dir, node, baseline, manifest, digest, _ = _frozen(tmp_path)
    result = _prepare_worktree(run_dir, node, 1, baseline, manifest, digest)
    (baseline / "app.py").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(AgentCliError, match="baseline.*篡改"):
        _prepare_worktree(run_dir, node, 1, baseline, manifest, digest, attempt=2)
    with pytest.raises(AgentCliError, match="baseline.*篡改"):
        _collect_diff(baseline, result, manifest, digest)


@pytest.mark.parametrize("nested_name", [".git", ".GIT"])
def test_nested_git_metadata_fails_loudly(tmp_path, nested_name):
    _, run_dir, node, baseline, manifest, digest, _ = _frozen(tmp_path)
    result = _prepare_worktree(run_dir, node, 1, baseline, manifest, digest)
    (result / "nested" / nested_name).mkdir(parents=True)

    with pytest.raises(AgentCliError, match="嵌套 .git"):
        _collect_diff(baseline, result, manifest, digest)


def test_root_git_case_variant_fails_loudly(tmp_path):
    _, run_dir, node, baseline, manifest, digest, _ = _frozen(tmp_path)
    result = _prepare_worktree(run_dir, node, 1, baseline, manifest, digest)
    original = result / ".git"
    temporary = result / "git-metadata-temp"
    original.rename(temporary)
    temporary.rename(result / ".GIT")

    with pytest.raises(AgentCliError, match="大小写变体"):
        _collect_diff(baseline, result, manifest, digest)


def test_hardlink_result_fails_loudly(tmp_path):
    _, run_dir, node, baseline, manifest, digest, _ = _frozen(tmp_path)
    result = _prepare_worktree(run_dir, node, 1, baseline, manifest, digest)
    os.link(result / "app.py", result / "alias.py")

    with pytest.raises(AgentCliError, match="hardlink"):
        _collect_diff(baseline, result, manifest, digest)


@pytest.mark.skipif(os.name != "nt", reason="NTFS alternate streams are Windows-only")
def test_alternate_data_stream_fails_loudly(tmp_path):
    _, run_dir, node, baseline, manifest, digest, _ = _frozen(tmp_path)
    result = _prepare_worktree(run_dir, node, 1, baseline, manifest, digest)
    try:
        (Path(str(result / "app.py") + ":secret")).write_bytes(b"hidden")
    except OSError:
        pytest.skip("当前测试卷不支持 alternate data streams")

    with pytest.raises(AgentCliError, match="alternate data stream|ADS"):
        _collect_diff(baseline, result, manifest, digest)


def test_symlink_result_fails_loudly_when_supported(tmp_path):
    _, run_dir, node, baseline, manifest, digest, _ = _frozen(tmp_path)
    result = _prepare_worktree(run_dir, node, 1, baseline, manifest, digest)
    try:
        (result / "outside-link").symlink_to(tmp_path / "outside")
    except OSError:
        pytest.skip("当前 Windows 账户无创建 symlink 权限")

    with pytest.raises(AgentCliError, match="符号链接|reparse"):
        _collect_diff(baseline, result, manifest, digest)


@pytest.mark.parametrize("name", ["bad:name", "control\x01", "trail.", "trail ", "NUL.txt"])
def test_windows_dangerous_path_components_fail_loudly(name):
    with pytest.raises(AgentCliError, match="危险路径|控制字符|空格或点|设备名"):
        _validate_component(name, name)


def test_empty_file_addition_has_visible_patch_headers(tmp_path):
    _, run_dir, node, baseline, manifest, digest, _ = _frozen(tmp_path)
    result = _prepare_worktree(run_dir, node, 1, baseline, manifest, digest)
    (result / "empty.txt").write_bytes(b"")

    patch, meta = _collect_diff(baseline, result, manifest, digest)

    assert b"--- /dev/null\n+++ b/empty.txt\n" in patch
    assert meta["files"][0]["change"] == "added"


def test_binary_change_fails_loudly_with_auditable_hashes(tmp_path):
    _, run_dir, node, baseline, manifest, digest, _ = _frozen(
        tmp_path, {"image.bin": b"before\x00bytes"})
    result = _prepare_worktree(run_dir, node, 1, baseline, manifest, digest)
    (result / "image.bin").write_bytes(b"after\x00bytes")

    with pytest.raises(AgentCliError, match=r"二进制文件变更.*before_sha256=.*after_sha256="):
        _collect_diff(baseline, result, manifest, digest)


def _prepared_source(tmp_path):
    from atlas.nodes.local_cli import _require_clean_git_workdir

    project = _project(tmp_path)
    node = _node(project)
    token = _require_clean_git_workdir(project, node.id)
    run_dir = tmp_path / "prepared-run"
    run_dir.mkdir()
    return project, node, token, run_dir


def test_source_token_rejects_tracked_and_untracked_mutation(tmp_path):
    project, node, token, run_dir = _prepared_source(tmp_path)
    (project / "app.py").write_text("mutated\n", encoding="utf-8")
    with pytest.raises(AgentCliError, match="SourceBaselineToken|字节树"):
        _freeze_baseline(run_dir, node, 1, project, token=token)

    _git(project, "checkout", "--", "app.py")
    (project / "untracked.txt").write_text("new\n", encoding="utf-8")
    with pytest.raises(AgentCliError, match="SourceBaselineToken|字节树"):
        _freeze_baseline(run_dir, node, 1, project, token=token)


def test_source_token_rejects_head_only_change(tmp_path):
    project, node, token, run_dir = _prepared_source(tmp_path)
    _git(project, "commit", "--allow-empty", "-qm", "new head")
    with pytest.raises(AgentCliError, match="HEAD"):
        _freeze_baseline(run_dir, node, 1, project, token=token)


def test_source_token_rejects_index_only_change(tmp_path):
    project, node, token, run_dir = _prepared_source(tmp_path)
    original = (project / "app.py").read_bytes()
    (project / "app.py").write_text("staged\n", encoding="utf-8")
    _git(project, "add", "app.py")
    (project / "app.py").write_bytes(original)
    with pytest.raises(AgentCliError, match="index"):
        _freeze_baseline(run_dir, node, 1, project, token=token)


def test_source_token_rejects_mutation_during_copy(tmp_path, monkeypatch):
    from atlas.nodes import agent as agent_module

    project, node, token, run_dir = _prepared_source(tmp_path)
    real_copytree = agent_module.shutil.copytree

    mutated = False

    def copy_then_mutate(source, destination, *args, **kwargs):
        nonlocal mutated
        result = real_copytree(source, destination, *args, **kwargs)
        if not mutated and Path(source) == project:
            mutated = True
            (project / "app.py").write_text(
                "changed during copy\n", encoding="utf-8")
        return result

    monkeypatch.setattr(agent_module.shutil, "copytree", copy_then_mutate)
    with pytest.raises(AgentCliError, match="SourceBaselineToken|baseline 冻结期间"):
        _freeze_baseline(run_dir, node, 1, project, token=token)
