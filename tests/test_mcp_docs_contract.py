# -*- coding: utf-8 -*-
"""MCP harness 文档的可执行配置契约。"""
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "mcp.md"
EXPECTED_ARGS = ["--directory", "<ATLAS_HOME>", "run", "atlas-mcp"]


def _json_blocks() -> list[dict]:
    text = DOC.read_text(encoding="utf-8")
    blocks = re.findall(r"```json\s*(.*?)\s*```", text, flags=re.DOTALL)
    return [json.loads(block) for block in blocks]


def test_mcp_doc_has_three_parseable_harness_configs():
    configs = _json_blocks()
    assert len(configs) == 3

    zcode = configs[0]["mcp"]["servers"]["atlas"]
    claude = configs[1]["mcpServers"]["atlas"]
    cursor = configs[2]["mcpServers"]["atlas"]
    assert [zcode["command"], claude["command"], cursor["command"]] == [
        "uv", "uv", "uv"]
    assert zcode["args"] == EXPECTED_ARGS
    assert claude["args"] == EXPECTED_ARGS
    assert cursor["args"] == EXPECTED_ARGS
    assert zcode["type"] == claude["type"] == "stdio"


def test_mcp_doc_contains_no_machine_specific_path():
    text = DOC.read_text(encoding="utf-8")
    assert not re.search(r"\b[A-Za-z]:[\\/]", text)
    assert "C:\\Users\\" not in text
    assert "D:\\" not in text


def test_public_entry_docs_link_to_harness_guide():
    for relative in (
        "README.md",
        "README.zh-CN.md",
        "web/src/guide/quickstart.md",
        "web/src/guide/mcp-human.md",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "docs/mcp.md" in text or "mcp.md" in text, relative
