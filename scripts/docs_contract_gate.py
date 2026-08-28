# -*- coding: utf-8 -*-
"""发布面文档契约门（CI 运行；取代随内部测试树移除的 docs contract 测试）。

自证内置:脚本对已知"宣称句/免责句"正反样例自检后,再扫描真实发布面。
任何一条不过 → 非零退出。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAIL: list[str] = []


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAIL.append(msg)


# ── 1. 过时声明（agent 曾 preview-only 的旧口径）───────────────────
ACTIVE = ("README.md", "README.en.md", "SECURITY.md", "CONTRIBUTING.md",
          "CHANGELOG.md", "RELEASE_CHECKLIST.md", "config/README.md",
          "skill/SKILL.md", "docs/mcp.md", "docs/VERIFICATION-2026-08-28.md",
          *sorted(str(p) for p in Path("workflows").glob("*.yaml")),
          *sorted(str(p) for p in Path("web/src/guide").glob("*.md")))
OBSOLETE = ("preview-only", "Agent nodes remain preview-only",
            "cannot execute in the production RC backend",
            "当前只能校验和预演", "仅可校验/预演", "仅可预演，不能执行",
            "暂不可真实执行", "当前没有受支持的生产 agent 执行路径",
            "sandbox backend unavailable")
for rel in ACTIVE:
    text = read(rel)
    hits = [c for c in OBSOLETE if c in text]
    check(not hits, f"{rel}: 过时 agent 声明 {hits}")

# ── 2. 反 OS 隔离宣称绊线（E-5；否定语境放行）────────────────────
_SANDBOX = re.compile(r"isolat|sandbox|沙箱|隔离", re.IGNORECASE)
_NEGATE = re.compile(
    r"\bnot\b|\bnever\b|do(es)? not|不是|不得|不提供|不写|不能|没有", re.IGNORECASE)


def claim_lines(text: str) -> list[str]:
    return [line.strip()[:90] for line in text.splitlines()
            if _SANDBOX.search(line) and not _NEGATE.search(line)]


# 自证(正反样例)
check(claim_lines("A directory copy is not an OS sandbox: it can reach host paths.") == [],
      "自证失败: 否定语境免责句被误报")
check(claim_lines("目录副本不是 OS 沙箱。") == [], "自证失败: 中文免责句被误报")
check(claim_lines("Atlas runs agents in an isolated OS sandbox."),
      "自证失败: 英文宣称句未被抓获")
check(claim_lines("提供 OS 级沙箱隔离,安全可靠。"), "自证失败: 中文宣称句未被抓获")
for rel in ("README.md", "README.en.md", "skill/SKILL.md"):
    hits = claim_lines(read(rel))
    check(not hits, f"{rel}: 疑似 OS 隔离宣称(调研结论 NO-GO): {hits}")

# ── 3. 双语 agent 边界事实必须在场 ────────────────────────────────
zh, en, sec = read("README.md"), read("README.en.md"), read("SECURITY.md")
for fact in ("runner", "local_cli", "anthropicBaseUrl", "WebSearch", "WebFetch", "Bash"):
    check(fact in en and fact in zh and fact in sec, f"agent 边界事实缺失: {fact}")
check("same-user host process" in en and "same-user host process" in sec,
      "英文/SECURITY 缺 same-user host process")
check("every host path available to that user" in sec, "SECURITY 缺宿主路径边界句")
check("当前用户身份下的宿主进程" in zh, "中文 README 缺宿主进程表述")
check("not an OS sandbox" in en and "不是 OS 沙箱" in zh, "缺 OS 沙箱免责表述")

# ── 4. skill 生产契约关键事实 ────────────────────────────────────
skill = read("skill/SKILL.md")
for fact in ("config/agents.json", "local_cli", "fail-closed", "retry",
             "baseline_digest", "result_digest", "patch_digest",
             "binary changes fail loudly", "not an OS sandbox",
             "allow_web", "writable: false", "max_turns"):
    check(fact in skill, f"skill 缺生产契约事实: {fact}")

# ── 5. MCP 工具数口径 ────────────────────────────────────────────
mcp_doc = read("docs/mcp.md")
check(re.search(r"\beight\b|八个", mcp_doc), "docs/mcp.md 缺 eight/八个 工具数口径")
check(not re.search(r"\bseven tools\b|七个工具", mcp_doc), "docs/mcp.md 残留七工具口径")

if FAIL:
    print("docs-contract gate FAILED:")
    for f in FAIL:
        print(f"  - {f}")
    sys.exit(1)
print(f"docs-contract gate OK ({len(ACTIVE)} surfaces + self-checks)")
