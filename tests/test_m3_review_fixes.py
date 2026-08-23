# -*- coding: utf-8 -*-
"""M3 独立模型审查(deepseek)发现问题的回归测试。

🟠1 重定向不跟随(密钥头不外发) 🟠2 拒删最后一个供应商
🟠3 密钥先写(失败无半成品) 🟠4 未知字段写回保留
🟠6 派生名碰撞检测 + 删除引用扫描 🟡9 source 区分
🟡10 422 不回显请求体
"""
import json

import pytest
from fastapi.testclient import TestClient

from atlas.credentials import EnvStore
from atlas.web import create_app

from atlas.adapters import FakeProvider
from conftest import make_registry

HEADER = {"X-Atlas-Request": "1"}


@pytest.fixture
def env(tmp_path):
    providers_path = tmp_path / "providers.json"
    env_path = tmp_path / ".env"
    providers_path.write_text(json.dumps({"providers": [
        {"id": "Deepseek", "openaiBaseUrl": "https://api.deepseek.com",
         "apiKeyEnv": "DEEPSEEK_API_KEY", "models": ["m1"]},
        {"id": "SuperAI", "anthropicBaseUrl": "https://a.example",
         "apiKeyEnv": "SUPER_API_KEY", "models": ["m2"]},
    ]}), encoding="utf-8")
    env_path.write_text("DEEPSEEK_API_KEY=sk-x\n", encoding="utf-8")
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    app = create_app(workflows_dir=workflows, runs_dir=tmp_path / "runs",
                     registry_factory=lambda p: make_registry(FakeProvider()),
                     providers_path=providers_path,
                     env_store=EnvStore(env_path))
    return {"client": TestClient(app, base_url="http://127.0.0.1"),
            "providers": providers_path, "env": env_path}


def test_no_redirect_following_so_key_header_never_forwarded(monkeypatch):
    """🟠1:3xx 一律不跟随(x-api-key 会随跨源重定向外发)。"""
    import httpx
    from atlas import discovery

    sent_headers: list[dict] = []
    requested: list[str] = []

    class RedirectStream:
        def __init__(self, status):
            self.status_code = status

        def iter_bytes(self):
            yield b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Client:
        def __init__(self, **kw):
            assert kw.get("follow_redirects") is False, \
                "必须 follow_redirects=False,否则 x-api-key 会被转发给重定向目标"

        def stream(self, method, url, headers=None):
            requested.append(url)
            sent_headers.append(headers or {})
            return RedirectStream(302)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(discovery.httpx, "Client", Client)
    r = discovery.discover_models({"openai": "https://x/v1"}, "sk-secret")
    assert not r.ok and r.error_kind == "transport"
    assert len(requested) == 1   # 302 没有被跟随


def test_cannot_delete_last_provider(env):
    c = env["client"]
    assert c.delete("/api/providers/Deepseek", headers=HEADER).status_code == 200
    resp = c.delete("/api/providers/SuperAI", headers=HEADER)
    assert resp.status_code == 400
    assert "至少保留一个" in resp.json()["detail"]
    # 配置面仍然可用,没有瘫痪
    assert c.get("/api/providers").status_code == 200


def test_blank_api_key_creates_no_half_config(env):
    """🟠3:apiKey 是纯空格 → 400,且供应商没有写进 providers.json。"""
    c = env["client"]
    resp = c.post("/api/providers", headers=HEADER, json={
        "id": "Halfway", "openaiBaseUrl": "https://h.example",
        "apiKey": "   "})
    assert resp.status_code == 400
    names = [p["id"] for p in c.get("/api/providers").json()]
    assert "Halfway" not in names
    assert "HALFWAY_API_KEY" not in env["env"].read_text(encoding="utf-8")


def test_unknown_fields_survive_ui_save(env):
    """🟠4:手工加的未知字段在界面保存(white-list 写回)后原样保留。"""
    c = env["client"]
    data = json.loads(env["providers"].read_text(encoding="utf-8"))
    data["providers"][0]["futurePolicy"] = {"retry": 2}
    env["providers"].write_text(json.dumps(data), encoding="utf-8")
    # 模拟界面动作:改一次白名单
    resp = c.put("/api/providers/Deepseek/models",
                 headers=HEADER, json={"models": ["m1", "m3"]})
    assert resp.status_code == 200
    saved = json.loads(env["providers"].read_text(encoding="utf-8"))
    row = next(p for p in saved["providers"] if p["id"] == "Deepseek")
    assert row["futurePolicy"] == {"retry": 2}
    assert row["models"] == ["m1", "m3"]


def test_derived_env_collision_rejected(env):
    """🟠6:A-B 与 A_B 派生同一个变量名——第二个创建必须被拒。"""
    c = env["client"]
    r1 = c.post("/api/providers", headers=HEADER, json={
        "id": "Team-A", "openaiBaseUrl": "https://a.example"})
    assert r1.status_code == 200
    r2 = c.post("/api/providers", headers=HEADER, json={
        "id": "TeamA", "openaiBaseUrl": "https://b.example"})
    # TEAM_A_API_KEY 已被 Team-A 占用:TeamA 派生 TEAMA_API_KEY,不撞。
    # 真正的撞车形态:Team-A vs TeamA 不撞;要构造撞车用相同字母+分隔符差异
    # 在当前 id 字符集(- 与 _ 都合法)下:Team-X 与 Team_X 都派生 TEAM_X_API_KEY
    r3 = c.post("/api/providers", headers=HEADER, json={
        "id": "Team-X", "openaiBaseUrl": "https://c.example"})
    assert r3.status_code == 200
    r4 = c.post("/api/providers", headers=HEADER, json={
        "id": "Team_X", "openaiBaseUrl": "https://d.example"})
    assert r4.status_code == 400
    assert "派生出同一个变量名" in r4.json()["detail"]


def test_delete_keeps_credential_still_referenced(env):
    """🟠6:两家共用同一变量名(碰撞残留/手工共享)时,删一家不删密钥。"""
    c = env["client"]
    # Team-Y 与 Team_Y 都创建于旧版本(碰撞守卫之前的形态):直接手改文件模拟
    data = json.loads(env["providers"].read_text(encoding="utf-8"))
    data["providers"].append({"id": "Team-Y", "openaiBaseUrl": "https://y.example",
                              "apiKeyEnv": "TEAM_Y_API_KEY", "models": []})
    data["providers"].append({"id": "Team_Y", "openaiBaseUrl": "https://yy.example",
                              "apiKeyEnv": "TEAM_Y_API_KEY", "models": []})
    env["providers"].write_text(json.dumps(data), encoding="utf-8")
    env["env"].write_text("DEEPSEEK_API_KEY=sk-x\nTEAM_Y_API_KEY=sk-shared\n",
                          encoding="utf-8")

    resp = c.delete("/api/providers/Team-Y", headers=HEADER)
    assert resp.status_code == 200
    assert "还被其他供应商引用" in resp.json()["note"]
    assert "TEAM_Y_API_KEY=sk-shared" in env["env"].read_text(encoding="utf-8")
    # 第二家删掉时才真正清理
    resp = c.delete("/api/providers/Team_Y", headers=HEADER)
    assert "已连带删除" in resp.json()["note"]
    assert "TEAM_Y_API_KEY" not in env["env"].read_text(encoding="utf-8")


def test_delete_reports_env_source_honestly(env, monkeypatch):
    """🟡9:密钥来自进程环境(而非 .env 文件)时,不谎称"已删除文件行"。"""
    # Deepseek 的变量名恰好是派生名;把文件里的行移走,只留进程环境来源
    env["env"].write_text("", encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-process")
    resp = env["client"].delete("/api/providers/Deepseek", headers=HEADER)
    assert resp.status_code == 200
    assert "进程环境" in resp.json()["note"]


def test_422_does_not_echo_request_body(env):
    """🟡10:坏请求体的 422 响应不回显 input(防提交中的密钥被弹回)。"""
    resp = env["client"].post(
        "/api/providers/Deepseek/key",
        headers=HEADER, content=b'"this-is-not-a-dict-maybe-a-key"')
    assert resp.status_code in (400, 422)
    assert "this-is-not-a-dict" not in resp.text


def test_concurrent_key_writes_do_not_lose_updates(tmp_path):
    """🟠5:并发 upsert 两把不同的键,最终两把都在(锁住读-改-写整体)。"""
    import threading
    store = EnvStore(tmp_path / ".env")
    store.upsert("BASE", "keep")
    errors = []

    def worker(i):
        try:
            store.upsert(f"KEY_{i}", f"value-{i}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    for i in range(16):
        assert f"KEY_{i}=value-{i}" in text
    assert "BASE=keep" in text
