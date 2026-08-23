# -*- coding: utf-8 -*-
"""模型拉取:协议回退/错误分类/4MB 上限/畸形行跳过。monkeypatch httpx,零成本。"""
import json

import httpx
import pytest

from atlas import discovery
from atlas.discovery import DiscoveryResult, discover_models


class FakeStream:
    def __init__(self, status_code=200, body=b"{}", headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def __iter__(self):
        yield self._body

    def iter_bytes(self):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeClient:
    """按 URL 前缀路由的假 httpx 客户端。"""

    def __init__(self, routes: dict, default_status=404):
        # routes: {url子串: (status, body_bytes)}
        self.routes = routes
        self.default_status = default_status
        self.calls: list[str] = []

    def stream(self, method, url, headers=None):
        self.calls.append(url)
        for frag, (status, body) in self.routes.items():
            if frag in url:
                return FakeStream(status, body)
        return FakeStream(self.default_status, b"{}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(discovery.httpx, "Client", lambda **kw: client)


def _body(ids):
    return json.dumps({"data": [{"id": i} for i in ids]}).encode()


def test_openai_endpoint_success(monkeypatch):
    c = FakeClient({"/models": (200, _body(["m2", "m1"]))})
    _patch_client(monkeypatch, c)
    r = discover_models({"openai": "https://gw.example/v1"}, "sk-x")
    assert r.ok and r.models == ("m1", "m2")
    assert c.calls == ["https://gw.example/v1/models"]


def test_falls_back_to_anthropic_when_openai_404(monkeypatch):
    """实测形态:Deepseek 的 anthropic 端点 404,openai 端点 200。"""
    c = FakeClient({
        "anthropic.example": (200, _body(["m1"])),
        "openai.example": (200, _body(["o1"])),
    })
    _patch_client(monkeypatch, c)
    r = discover_models({"openai": "https://openai.example/v1",
                         "anthropic": "https://anthropic.example"}, "sk")
    assert r.ok and "o1" in r.models


def test_only_anthropic_configured(monkeypatch):
    c = FakeClient({"minimaxi.com": (200, _body(["M3"]))})
    _patch_client(monkeypatch, c)
    r = discover_models({"anthropic": "https://api.minimaxi.com/anthropic"}, "k")
    assert r.ok and r.models == ("M3",)
    assert c.calls == ["https://api.minimaxi.com/anthropic/v1/models"]


def test_auth_error_reported_over_404(monkeypatch):
    """一边 404 一边 401:报 auth(对诊断最有用),不是 unsupported。"""
    c = FakeClient({
        "anthropic.example": (404, b""),
        "openai.example": (401, b""),
    })
    _patch_client(monkeypatch, c)
    r = discover_models({"openai": "https://openai.example/v1",
                         "anthropic": "https://anthropic.example"}, "bad")
    assert not r.ok and r.error_kind == "auth"
    assert "密钥" in r.message


def test_unsupported_when_both_404(monkeypatch):
    c = FakeClient({})
    _patch_client(monkeypatch, c)
    r = discover_models({"openai": "https://x.example/v1",
                         "anthropic": "https://x.example"}, "k")
    assert r.error_kind == "unsupported"
    assert "手动填写" in r.message


def test_no_base_urls(monkeypatch):
    c = FakeClient({})
    _patch_client(monkeypatch, c)
    r = discover_models({}, "k")
    assert r.error_kind == "unsupported"


def test_malformed_rows_skipped(monkeypatch):
    body = json.dumps({"data": [
        {"id": "good-1"}, {"no_id": True}, "junk", {"id": ""}, {"id": "good-2"},
    ]}).encode()
    c = FakeClient({"/models": (200, body)})
    _patch_client(monkeypatch, c)
    r = discover_models({"openai": "https://x/v1"}, "k")
    assert r.models == ("good-1", "good-2")


def test_body_over_4mb_rejected(monkeypatch):
    big = b'{"data": [{"id": "m"}], "pad": "' + b"x" * (discovery.MAX_BODY_BYTES + 10) + b'"}'
    c = FakeClient({"/models": (200, big)})
    _patch_client(monkeypatch, c)
    r = discover_models({"openai": "https://x/v1"}, "k")
    assert r.error_kind == "parse" and "4MB" in r.message


def test_transport_error(monkeypatch):
    class Broken:
        def stream(self, *a, **k):
            raise httpx.ConnectError("no route")
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(discovery.httpx, "Client", lambda **kw: Broken())
    r = discover_models({"openai": "https://x/v1"}, "k")
    assert r.error_kind == "transport"
