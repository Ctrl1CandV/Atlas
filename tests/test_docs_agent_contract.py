# -*- coding: utf-8 -*-
"""活动文档中的生产 agent 事实契约；明确排除计划、归档与历史材料。"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_DOCS = (
    Path("README.md"),
    Path("README.en.md"),
    Path("SECURITY.md"),
    Path("CONTRIBUTING.md"),
    Path("CHANGELOG.md"),
    Path("RELEASE_CHECKLIST.md"),
    Path("config/README.md"),
    Path("skill/SKILL.md"),
    Path("docs/mcp.md"),
    *tuple(sorted(Path("workflows").glob("*.yaml"))),
    *tuple(sorted(Path("web/src/guide").glob("*.md"))),
)


def _read(relative: Path) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_active_docs_have_no_obsolete_agent_preview_only_claims():
    obsolete = (
        "preview-only",
        "Agent nodes remain preview-only",
        "cannot execute in the production RC backend",
        "not executable in the RC production path",
        "当前只能校验和预演",
        "仅可校验/预演",
        "仅可预演，不能执行",
        "暂不可真实执行",
        "当前没有受支持的生产 agent 执行路径",
        "当前不能执行的能力",
        "sandbox backend unavailable",
    )
    findings = []
    for relative in ACTIVE_DOCS:
        text = _read(relative)
        findings.extend(f"{relative}: {claim}" for claim in obsolete if claim in text)
    assert not findings, "活动文档仍有过时 agent 声明:\n" + "\n".join(findings)


def test_skill_records_complete_production_agent_contract():
    text = _read(Path("skill/SKILL.md"))
    required = (
        "config/agents.json",
        "local_cli",
        "fail-closed",
        "same-user host process",
        "freezes a baseline",
        "retry",
        "does not write the original directory",
        "ordinary-file byte manifests",
        "complete textual unified diff",
        "baseline_digest",
        "result_digest",
        "patch_digest",
        "binary changes fail loudly",
        "not an OS sandbox",
        "anthropicBaseUrl",
        "required system variables",
        "allow_web",
        "allowed_paths",
        "writable: false",
        "--add-dir",
        "WebSearch",
        "WebFetch",
        "Bash",
        "max_turns",
        "deadlines",
        "configured budgets",
    )
    missing = [fact for fact in required if fact not in text]
    assert not missing, f"skill 缺少生产 agent 契约事实: {missing}"


def test_bilingual_readmes_and_security_state_agent_boundaries():
    chinese = _read(Path("README.md"))
    english = _read(Path("README.en.md"))
    security = _read(Path("SECURITY.md"))

    for fact in ("runner", "local_cli", "anthropicBaseUrl", "WebSearch", "WebFetch",
                 "Bash", "max_turns"):
        assert fact in english
        assert fact in chinese
        assert fact in security

    assert "same-user host process" in english
    assert "same-user host process" in security
    assert "every host path available to that user" in security
    assert "当前用户身份下的宿主进程" in chinese
    for text in (english, security):
        assert "ordinary-file byte manifests" in text
        assert "complete textual unified diff" in text
        for digest in ("baseline_digest", "result_digest", "patch_digest"):
            assert digest in text
    assert "普通文件字节清单" in chinese
    assert "完整文本 unified diff" in chinese
    assert "does not write the original" in english
    assert "不写原目录" in chinese
    assert "not an OS sandbox" in english
    assert "不是 OS 沙箱" in chinese


def test_coding_workflow_is_an_executable_opt_in_example():
    text = _read(Path("workflows/code-change-review-approve.yaml"))
    assert "runner=local_cli" in text
    assert "anthropicBaseUrl" in text
    assert "allow_web: false" in text
    assert "implementer.diff" in text
    assert "完整文本 unified diff" in text
    assert "普通文件字节清单" in text
    assert "baseline/result/patch" in text
    assert "max_iterations: 2" in text
