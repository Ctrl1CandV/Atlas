# -*- coding: utf-8 -*-
"""活动文档中的生产 agent 事实契约；明确排除计划、归档与历史材料。"""
import re
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


# ─────────────────── E-5 反宣传红线 ───────────────────
# 合同(docs/PLAN-stage-e-2026-08-27.md E-5):OS 级沙箱调研若给不出 GO,
# README/skill 不得出现任何 isolated/secure/隔离"宣称"。目前调研结论是
# NO-GO(docs/RESEARCH-os-sandbox.md §6),这条 grep 绊线永久把守措辞。
# 已知局限:逐行启发式,不承诺语义完备——带否定词的巧言宣称拦不住;
# 它防的是无意的措辞漂移,不是有意的撒谎(那是审查人的职责)。

SANDBOX_CLAIM_DOCS = ("README.md", "README.en.md", "skill/SKILL.md")
_SANDBOX_TERMS = re.compile(r"isolat|sandbox|沙箱|隔离", re.IGNORECASE)
_NEGATION_CUES = re.compile(
    r"\bnot\b|\bnever\b|do(es)? not|不是|不得|不提供|不写|不能|没有", re.IGNORECASE)


def _isolation_claim_lines(text: str) -> list[str]:
    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if _SANDBOX_TERMS.search(line) and not _NEGATION_CUES.search(line):
            findings.append(f"{lineno}: {line.strip()[:100]}")
    return findings


def test_readme_and_skill_make_no_os_isolation_claims():
    findings = []
    for relative in SANDBOX_CLAIM_DOCS:
        for finding in _isolation_claim_lines(_read(Path(relative))):
            findings.append(f"{relative}:{finding}")
    assert not findings, ("README/skill 出现疑似 OS 隔离宣称(调研结论 NO-GO,"
                          "见 docs/RESEARCH-os-sandbox.md §6):\n" + "\n".join(findings))


def test_isolation_claim_checker_flags_claims_and_spares_disclaimers():
    # 正样例:现行文档的免责句(否定语境)必须放行。
    assert _isolation_claim_lines(
        "A directory copy is not an OS sandbox: the process can reach host paths.") == []
    assert _isolation_claim_lines("目录副本不是 OS 沙箱。") == []
    # 反样例:无否定语境的宣称句必须被抓到。
    assert _isolation_claim_lines("Atlas runs agents in an isolated OS sandbox.")
    assert _isolation_claim_lines("提供 OS 级沙箱隔离,安全可靠。")
