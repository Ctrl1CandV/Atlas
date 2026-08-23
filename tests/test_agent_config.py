# -*- coding: utf-8 -*-
"""Worktree Runner 配置契约：显式启用，默认 fail-closed。"""
import json

import pytest

from atlas.config import ConfigError, load_agent_config


def test_missing_agent_config_is_fail_closed(tmp_path):
    config = load_agent_config(tmp_path / "missing.json")
    assert config.runner == "fail_closed"
    assert config.cli.kind == "claude"


def test_valid_local_cli_config(tmp_path):
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({
        "runner": "local_cli",
        "cli": {"kind": "claude", "command": "claude", "extra_args": []},
    }), encoding="utf-8")
    config = load_agent_config(path)
    assert config.runner == "local_cli"
    assert config.cli.command == "claude"
    assert config.cli.extra_args == ()


@pytest.mark.parametrize("payload, message", [
    ({"runner": "other"}, "未知 agent runner"),
    ({"runner": "local_cli", "cli": {"kind": "other"}}, "未知 CLI 后端"),
    ({"runner": "local_cli", "cli": {"command": ""}}, "cli.command"),
    ({"runner": "local_cli", "cli": {"extra_args": "--verbose"}}, "extra_args"),
    ({"runner": "local_cli", "cli": {"extra_args": ["--tools"]}}, "审核过的参数"),
    ({"runner": "local_cli", "secret": "do-not-echo"}, "未知字段"),
])
def test_invalid_agent_config_fails_loudly_without_echoing_values(
        tmp_path, payload, message):
    path = tmp_path / "agents.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ConfigError, match=message) as exc:
        load_agent_config(path)
    assert "do-not-echo" not in str(exc.value)


def test_malformed_agent_config_fails(tmp_path):
    path = tmp_path / "agents.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ConfigError, match="不是合法 JSON"):
        load_agent_config(path)
