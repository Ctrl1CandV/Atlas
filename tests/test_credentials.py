# -*- coding: utf-8 -*-
"""凭据层:A8 的"类型上没有 value 字段"+ 原子写/保留结构/空值拒绝。"""
import dataclasses

import pytest

from atlas.credentials import CredentialError, CredentialView, EnvStore


def test_credential_view_has_no_value_field():
    """A8 核心:视图类型上没有承载密钥值的字段。加参数不配这条不许合并。"""
    fields = {f.name for f in dataclasses.fields(CredentialView)}
    assert fields == {"configured", "source", "writable"}, fields
    # 与密钥值同形的字段名也不许出现
    for banned in ("value", "secret", "key", "token", "api_key"):
        assert not any(banned in f for f in fields)


def test_view_states(tmp_path, monkeypatch):
    store = EnvStore(tmp_path / ".env")
    monkeypatch.delenv("ATLAS_TEST_KEY", raising=False)

    v = store.view("ATLAS_TEST_KEY")
    assert (v.configured, v.source) == (False, "unset")

    store.upsert("ATLAS_TEST_KEY", "sk-test-123")
    v = store.view("ATLAS_TEST_KEY")
    assert (v.configured, v.source, v.writable) == (True, "file", True)
    # read_value 只给引擎方向用
    assert store.read_value("ATLAS_TEST_KEY") == "sk-test-123"

    monkeypatch.setenv("ATLAS_TEST_KEY", "from-env")
    v = store.view("ATLAS_TEST_KEY")
    assert v.source == "file"   # 文件优先于进程环境


def test_upsert_preserves_comments_and_order(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# 头部注释\nDEEPSEEK_API_KEY=old-value\n\n# 另一段\nOTHER=x\n",
        encoding="utf-8",
    )
    EnvStore(env).upsert("DEEPSEEK_API_KEY", "new-value")
    text = env.read_text(encoding="utf-8")
    # 注释、空行、其他键原样;目标键原地替换
    assert "# 头部注释" in text
    assert "# 另一段" in text
    assert "OTHER=x" in text
    assert "new-value" in text and "old-value" not in text
    assert text.index("DEEPSEEK_API_KEY") < text.index("OTHER")


def test_upsert_appends_new_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("EXISTING=1\n", encoding="utf-8")
    EnvStore(env).upsert("BRAND_NEW", "v")
    lines = env.read_text(encoding="utf-8").strip().splitlines()
    assert lines == ["EXISTING=1", "", "BRAND_NEW=v"]


def test_blank_value_rejected(tmp_path):
    store = EnvStore(tmp_path / ".env")
    with pytest.raises(CredentialError, match="纯空格"):
        store.upsert("K", "   ")
    assert not (tmp_path / ".env").exists()   # 拒绝时不创建文件


def test_atomic_write_leaves_no_temp(tmp_path):
    env = tmp_path / ".env"
    EnvStore(env).upsert("K", "v")
    leftovers = [p.name for p in tmp_path.iterdir()
                 if p.name.startswith(".env-")]
    assert leftovers == []


def test_bom_and_quotes_tolerated_on_read(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes('KEY="quoted-value"\n'.encode("utf-8"))
    store = EnvStore(env)
    assert store.read_value("KEY") == "quoted-value"
