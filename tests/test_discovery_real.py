# -*- coding: utf-8 -*-
"""真实拉取(标记 real_api,零成本:/models 端点不计 token 费)。

价值:monkeypatch 测试验证的是"我们解析我们以为的格式",这一条验证
"五家网关真的返回这个格式"。零成本,所以每个里程碑都值得跑。
"""
import pytest

from atlas.config import load_env, load_provider_configs
from atlas.credentials import EnvStore
from atlas.discovery import discover_models


@pytest.mark.real_api
def test_all_providers_discoverable():
    load_env()
    providers = load_provider_configs()
    failures = []
    for pid, cfg in providers.items():
        key = EnvStore().read_value(cfg.api_key_env) or ""
        if not key:
            failures.append(f"{pid}:密钥未配置({cfg.api_key_env})")
            continue
        result = discover_models(
            {"openai": cfg.openai_base_url, "anthropic": cfg.anthropic_base_url},
            key,
        )
        if not result.ok:
            failures.append(f"{pid}:{result.error_kind}:{result.message}")
        else:
            print(f"{pid}: {len(result.models)} 个模型可见")
    assert not failures, failures
