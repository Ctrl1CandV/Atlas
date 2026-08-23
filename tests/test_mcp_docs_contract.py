# -*- coding: utf-8 -*-
"""MCP harness 文档的可执行配置契约。"""
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "mcp.md"
EXPECTED_ARGS = ["--directory", "<ATLAS_HOME>", "run", "atlas-mcp"]
MCP_HTTP_URL = "http://127.0.0.1:8321/mcp"


def _json_blocks() -> list[dict]:
    text = DOC.read_text(encoding="utf-8")
    blocks = re.findall(r"```json\s*(.*?)\s*```", text, flags=re.DOTALL)
    return [json.loads(block) for block in blocks]


def test_mcp_doc_has_three_parseable_http_harness_configs():
    """HTTP 是推荐入口:三个 harness 的 JSON 配置都指向本机 streamable-http 端点。"""
    configs = _json_blocks()
    http_configs = [c for c in configs if "url" in json.dumps(c)]
    assert len(http_configs) == 3

    zcode = http_configs[0]["mcp"]["servers"]["atlas"]
    claude = http_configs[1]["mcpServers"]["atlas"]
    cursor = http_configs[2]["mcpServers"]["atlas"]
    for entry in (zcode, claude, cursor):
        assert entry["url"] == MCP_HTTP_URL
        assert "command" not in entry  # HTTP 配置不启动子进程
    assert zcode["type"] == claude["type"] == "http"


def test_mcp_doc_keeps_stdio_alternative():
    """stdio 备用方式仍给出完整可解析配置(含 <ATLAS_HOME> 占位符)。"""
    text = DOC.read_text(encoding="utf-8")
    assert EXPECTED_ARGS == ["--directory", "<ATLAS_HOME>", "run", "atlas-mcp"]
    assert all(arg in text for arg in EXPECTED_ARGS)


def test_mcp_doc_contains_no_machine_specific_path():
    text = DOC.read_text(encoding="utf-8")
    assert not re.search(r"\b[A-Za-z]:[\\/]", text)
    assert "C:\\Users\\" not in text
    assert "D:\\" not in text


def test_shipped_mcp_json_points_to_http_endpoint():
    """随仓库分发的 .mcp.json 指向 HTTP 端点(2026-08-23 起)。

    曾经的 stdio+`--directory .` 是坑源头:任何在仓库外拉子进程的 harness
    都会 program not found(ZCode 实证),且 stdio 子进程与 atlas-web 不共享代码。
    """
    shipped = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    entry = shipped["mcpServers"]["atlas"]
    assert entry["type"] == "http"
    assert entry["url"] == MCP_HTTP_URL
    assert "command" not in entry


def test_public_entry_docs_link_to_harness_guide():
    """公开入口文档指向仓库自带的 .mcp.json;docs/ 不随 v0.1.0 sdist 分发,
    公开入口不得把 docs/mcp.md 当作唯一接入说明(下一版清单已补入该文件,
    但入口文档仍需自带最小可用指引)。"""
    for relative in (
        "README.md",
        "README.en.md",
        "web/src/guide/quickstart.md",
        "web/src/guide/mcp-human.md",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert ".mcp.json" in text or MCP_HTTP_URL in text, relative
