# -*- coding: utf-8 -*-
"""配置面 API:供应商 CRUD、密钥写入、拉取、白名单写回 + A8 密钥不出界。"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas import configapi
from atlas.config import load_provider_configs
from atlas.credentials import EnvStore
from atlas.web import create_app

from conftest import make_registry
from atlas.adapters import FakeProvider

HEADER = {"X-Atlas-Request": "1"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    providers_path = tmp_path / "providers.json"
    env_path = tmp_path / ".env"
    providers_path.write_text(json.dumps({"providers": [
        {"id": "Deepseek", "openaiBaseUrl": "https://api.deepseek.com",
         "apiKeyEnv": "DEEPSEEK_API_KEY", "models": ["deepseek-v4-flash"]},
    ]}), encoding="utf-8")
    env_path.write_text("DEEPSEEK_API_KEY=sk-real-secret-value-123\n",
                        encoding="utf-8")
    workflows = tmp_path / "workflows"
    workflows.mkdir()

    app = create_app(workflows_dir=workflows, runs_dir=tmp_path / "runs",
                     registry_factory=lambda pids: make_registry(FakeProvider()),
                     providers_path=providers_path,
                     env_store=EnvStore(env_path))
    return TestClient(app, base_url="http://127.0.0.1")


def test_list_providers_shows_credential_state_not_value(client):
    rows = client.get("/api/providers").json()
    assert rows[0]["id"] == "Deepseek"
    cred = rows[0]["credential"]
    assert cred == {"configured": True, "source": "file", "writable": True}
    assert "value" not in cred and "sk-real" not in json.dumps(rows)


def test_a8_no_secret_in_any_get_response(client):
    """A8:遍历全部 GET 端点,响应里不许出现密钥子串。"""
    secret = "sk-real-secret-value-123"
    get_endpoints = [
        "/api/workflows", "/api/workflows/demo", "/api/runs",
        "/api/providers", "/api/providers/Deepseek",
        "/api/providers/Deepseek/models",   # 不存在也无妨:404 响应同样要扫
        "/api/runs/nonexistent-run",
    ]
    for url in get_endpoints:
        resp = client.get(url)
        assert secret not in resp.text, f"密钥泄漏于 {url}"
        # 部分匹配也不许(取密钥中段)
        assert secret[3:20] not in resp.text, f"密钥片段泄漏于 {url}"


def test_create_provider_requires_letter_start(client):
    resp = client.post("/api/providers", json={"id": "1bad", "openaiBaseUrl": "https://x"},
                       headers=HEADER)
    assert resp.status_code == 400
    assert "字母开头" in resp.json()["detail"]


def test_create_provider_needs_base_url(client):
    resp = client.post("/api/providers", json={"id": "Newone"}, headers=HEADER)
    assert resp.status_code == 400 and "base URL" in resp.json()["detail"]


def test_create_provider_with_key_roundtrip(client, tmp_path):
    resp = client.post("/api/providers", json={
        "id": "Newone", "openaiBaseUrl": "https://api.newone.example/v1",
        "apiKey": "sk-newone-key"},
        headers=HEADER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"]["apiKeyEnv"] == "NEWONE_API_KEY"
    assert body["provider"]["credential"]["configured"] is True
    # 密钥进了 .env(引用名),不在任何响应里
    assert "NEWONE_API_KEY=sk-newone-key" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert "sk-newone-key" not in json.dumps(body)


def test_update_provider_keeps_identity(client):
    resp = client.put("/api/providers/Deepseek",
                      json={"preferTransport": "openai"}, headers=HEADER)
    assert resp.status_code == 200
    detail = client.get("/api/providers/Deepseek").json()
    assert detail["apiKeyEnv"] == "DEEPSEEK_API_KEY"   # 身份键不可改


def test_set_key_rejects_blank(client):
    resp = client.post("/api/providers/Deepseek/key",
                       json={"value": "   "}, headers=HEADER)
    assert resp.status_code == 400


def test_set_key_updates_env(client, tmp_path):
    resp = client.post("/api/providers/Deepseek/key",
                       json={"value": "sk-rotated"}, headers=HEADER)
    assert resp.status_code == 200
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "sk-rotated" in text and "sk-real-secret-value-123" not in text


def test_discover_uses_form_values_and_existing_key(client, monkeypatch):
    calls = []

    def fake_discover(base_urls, api_key):
        calls.append((base_urls, api_key))
        from atlas.discovery import DiscoveryResult
        return DiscoveryResult(True, models=("m1", "m2"))

    monkeypatch.setattr(configapi, "discover_models", fake_discover)
    # 不带任何覆盖:用已存的 URL 与密钥
    resp = client.post("/api/providers/Deepseek/discover", json={}, headers=HEADER)
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert calls[0][1] == "sk-real-secret-value-123"
    # 带表单值:未保存的 URL 与新密钥都要用上(先保存才能测会把半成品写盘)
    resp = client.post("/api/providers/Deepseek/discover", json={
        "openaiBaseUrl": "https://unsaved.example/v1", "apiKey": "sk-form"},
        headers=HEADER)
    assert calls[1][0]["openai"] == "https://unsaved.example/v1"
    assert calls[1][1] == "sk-form"
    assert "sk-form" not in resp.text and "sk-real" not in resp.text


def test_discover_without_key_400(client, tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("", encoding="utf-8")
    resp = client.post("/api/providers/Deepseek/discover", json={}, headers=HEADER)
    assert resp.status_code == 400
    assert "DEEPSEEK_API_KEY" in resp.json()["detail"]


def test_set_models_whitelist(client, tmp_path):
    resp = client.put("/api/providers/Deepseek/models",
                      json={"models": ["deepseek-v4-flash", "deepseek-v4-pro"]},
                      headers=HEADER)
    assert resp.status_code == 200
    assert resp.json()["models"] == ["deepseek-v4-flash", "deepseek-v4-pro"]
    # 落盘且顺序保留
    saved = json.loads((tmp_path / "providers.json").read_text(encoding="utf-8"))
    assert saved["providers"][0]["models"] == ["deepseek-v4-flash", "deepseek-v4-pro"]


def test_set_models_rejects_duplicates(client):
    resp = client.put("/api/providers/Deepseek/models",
                      json={"models": ["a", "a"]}, headers=HEADER)
    assert resp.status_code == 400


def test_delete_provider_narrows_credential_cleanup(client, tmp_path):
    # 新建一家(派生名)再删:.env 里那行应被连带清理
    client.post("/api/providers", json={
        "id": "Tempv", "openaiBaseUrl": "https://t.example", "apiKey": "sk-tmp"},
        headers=HEADER)
    resp = client.delete("/api/providers/Tempv", headers=HEADER)
    assert resp.status_code == 200
    assert "已连带删除" in resp.json()["note"]
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "TEMPV_API_KEY" not in text
    # Deepseek 的密钥不受影响
    assert "DEEPSEEK_API_KEY" in text


def test_delete_keeps_non_derived_credential_names(client, tmp_path, monkeypatch):
    # 一家 apiKeyEnv 不是 <ID>_API_KEY 派生名的供应商(手改过配置的形态)
    p = tmp_path / "providers.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["providers"].append({
        "id": "Custom", "openaiBaseUrl": "https://c.example",
        "apiKeyEnv": "SHARED_KEY", "models": []})
    p.write_text(json.dumps(data), encoding="utf-8")
    resp = client.delete("/api/providers/Custom", headers=HEADER)
    assert "已保留" in resp.json()["note"]
    # 没写过的 SHARED_KEY 行本来就不存在;断言不抛错即可
    assert resp.status_code == 200


def test_initialization_notice_is_one_shot_and_contains_only_names(client, tmp_path):
    from atlas.config_init import _write_notice

    _write_notice(tmp_path, ("providers.json", "agents.json"))
    response = client.get("/api/config/initialization")
    assert response.status_code == 200
    notice = response.json()
    assert set(notice["created"]) == {"providers.json", "agents.json"}
    assert "sk-real-secret-value-123" not in response.text

    stale = client.post(
        "/api/config/initialization/ack", headers=HEADER,
        json={"event_id": "stale"})
    assert stale.status_code == 409
    assert client.get("/api/config/initialization").json()["event_id"] == notice["event_id"]

    acknowledged = client.post(
        "/api/config/initialization/ack", headers=HEADER,
        json={"event_id": notice["event_id"]})
    assert acknowledged.status_code == 200
    assert client.get("/api/config/initialization").json() is None


def test_initialization_api_ack_only_removes_matching_queued_event(client, tmp_path):
    from atlas.config_init import _write_notice

    _write_notice(tmp_path, ("providers.json",), event_id="first")
    _write_notice(tmp_path, ("agents.json",), event_id="second")

    assert client.get("/api/config/initialization").json() == {
        "event_id": "first", "created": ["providers.json"]}
    acknowledged = client.post(
        "/api/config/initialization/ack", headers=HEADER,
        json={"event_id": "first"})
    assert acknowledged.status_code == 200
    assert client.get("/api/config/initialization").json() == {
        "event_id": "second", "created": ["agents.json"]}

    stale = client.post(
        "/api/config/initialization/ack", headers=HEADER,
        json={"event_id": "first"})
    assert stale.status_code == 409
    assert client.get("/api/config/initialization").json()["event_id"] == "second"


def test_corrupt_initialization_queue_returns_controlled_errors(client, tmp_path):
    from atlas import config_init

    (tmp_path / config_init._NOTICE_NAME).write_text("{broken", encoding="utf-8")

    get_response = client.get("/api/config/initialization")
    assert get_response.status_code == 500
    assert "合法 JSON" in get_response.json()["detail"]

    ack_response = client.post(
        "/api/config/initialization/ack", headers=HEADER,
        json={"event_id": "any"})
    assert ack_response.status_code == 500
    assert "合法 JSON" in ack_response.json()["detail"]


def test_post_without_header_rejected_on_config_endpoints(client):
    """配置写操作同样受 X-Atlas-Request 保护(跨站驱动防护)。"""
    resp = client.post("/api/providers", json={"id": "Evil", "openaiBaseUrl": "https://e"})
    assert resp.status_code == 403
    resp = client.post("/api/providers/Deepseek/discover", json={})
    assert resp.status_code == 403
