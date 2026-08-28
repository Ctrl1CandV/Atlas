# -*- coding: utf-8 -*-
"""E-3 · Release 内置已构建前端:dist 解析、manifest 哈希分级、排除机检。

合同来源:PLAN-stage-e-2026-08-27.md 的 E-3 章。
- 启动解析顺序:CLI 参数 > ATLAS_WEB_DIST > 仓库 sibling web/dist
  > 包内 web-dist;全 miss fail-loud 并附三条出路;
- 哈希分级:相符/无 manifest 不警告,不符/损坏给警告文案——本地开发
  打警告继续,发布冒烟 job 对非 None fail;
- bundle 排除名单是机器检查(凭据/运行记录/缓存类目录绝不进发布包);
- dist 切换不得破坏 /api/* 与 /mcp 路由次序(Mount 全捕获的历史坑);
- Git 仍不跟踪 web/dist。
"""
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas.adapters import FakeProvider
from atlas.distbundle import (bundle_exclusion_violations,
                              frontend_dist_digest, manifest_mismatch,
                              resolve_web_dist, write_manifest)
from atlas.web import create_app

from conftest import make_registry


def _make_dist(root: Path) -> Path:
    dist = root / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html>atlas</html>", encoding="utf-8")
    (dist / "assets").mkdir()
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    return dist


# ─────────────────── 四级解析顺序 ───────────────────


def test_resolution_order_cli_beats_env_beats_repo_beats_package(tmp_path):
    repo = tmp_path / "repo"
    package = tmp_path / "pkg"
    cli_dist = _make_dist(tmp_path / "cli")
    env_dist = _make_dist(tmp_path / "env")
    repo_dist = _make_dist(repo / "web")
    # 第四级布局名是 atlas/web-dist(没有 dist 子目录),不能走 _make_dist
    package.mkdir(parents=True)
    package_dist = package / "web-dist"
    package_dist.mkdir()
    (package_dist / "index.html").write_text("<html>pkg</html>",
                                             encoding="utf-8")

    def resolve(cli=None, env=None):
        return resolve_web_dist(cli, environ=({"ATLAS_WEB_DIST": env}
                                              if env else {}),
                                repo_root=repo, package_dir=package)

    assert resolve(cli=str(cli_dist)) == cli_dist
    assert resolve(env=str(env_dist)) == env_dist
    assert resolve() == repo_dist
    # 仓库 sibling 缺席时才落到包内 web-dist
    (repo / "web" / "dist" / "index.html").unlink()
    assert resolve() == package_dist


def test_resolution_all_miss_fails_loud_with_three_remedies(tmp_path):
    with pytest.raises(RuntimeError) as exc_info:
        resolve_web_dist(environ={}, repo_root=tmp_path / "empty-repo",
                         package_dir=tmp_path / "empty-pkg")
    message = str(exc_info.value)
    assert "四个候选位置" in message
    for remedy in ("npm", "ATLAS_WEB_DIST", "Release"):
        assert remedy in message


# ─────────────────── manifest 哈希分级(两个调用侧同一函数) ───────────────────


def test_manifest_match_mismatch_and_missing(tmp_path):
    dist = _make_dist(tmp_path)
    assert manifest_mismatch(dist) is None   # 无 manifest = 开发树常态
    write_manifest(dist, git_sha="abc", built_at="now", node_version="v22")
    assert manifest_mismatch(dist) is None   # 相符
    # dist 被改动 → 警告文案(本地开发侧拿它打 stderr;发布冒烟侧对非
    # None 直接 fail——同一函数,分级在调用方)
    (dist / "assets" / "app.js").write_text("tampered", encoding="utf-8")
    warning = manifest_mismatch(dist)
    assert warning is not None and "不符" in warning
    # digest 排除 manifest 自身:重写 manifest 不改变已声明身份的稳定性
    write_manifest(dist, git_sha="abc", built_at="now2", node_version="v22")
    assert manifest_mismatch(dist) is None
    # 损坏的 manifest → 警告而非崩溃
    (dist / "manifest.json").write_text("{broken", encoding="utf-8")
    assert manifest_mismatch(dist) is not None


def test_digest_is_deterministic_and_content_sensitive(tmp_path):
    dist = _make_dist(tmp_path)
    d1 = frontend_dist_digest(dist)
    assert d1 == frontend_dist_digest(dist)
    (dist / "assets" / "app.js").write_text("changed", encoding="utf-8")
    assert frontend_dist_digest(dist) != d1


# ─────────────────── bundle 排除名单机检 ───────────────────


def test_bundle_exclusion_machine_check(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "config").mkdir(parents=True)
    (bundle / "atlas").mkdir()
    (bundle / "atlas" / "web.py").write_text("#", encoding="utf-8")
    (bundle / "web" / "dist").mkdir(parents=True)
    (bundle / "web" / "dist" / "index.html").write_text("x", encoding="utf-8")
    assert bundle_exclusion_violations(bundle) == []
    # 凭据 / 运行记录 / 缓存目录 / 密钥后缀 → 各自违规
    (bundle / "config" / ".env").write_text("SECRET=1", encoding="utf-8")
    (bundle / "config" / "providers.json").write_text("{}", encoding="utf-8")
    (bundle / "runs").mkdir()
    (bundle / "runs" / "20260101-x" / "events.jsonl").parent.mkdir(
        parents=True)
    (bundle / "runs" / "20260101-x" / "events.jsonl").write_text(
        "{}", encoding="utf-8")
    (bundle / "atlas" / "__pycache__").mkdir()
    (bundle / "atlas" / "__pycache__" / "web.cpython.pyc").write_bytes(b"\0")
    (bundle / "host.key").write_text("k", encoding="utf-8")
    violations = bundle_exclusion_violations(bundle)
    joined = "\n".join(violations)
    assert "config/.env" in joined
    assert "config/providers.json" in joined
    assert any(v.startswith("runs/") for v in violations)
    assert any("__pycache__" in v for v in violations)
    assert "host.key" in violations


def _init_repo(tmp_path: Path, *, with_gitignore: bool) -> Path:
    repo = tmp_path / "repo"
    (repo / "atlas").mkdir(parents=True)
    (repo / "atlas" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "config").mkdir()
    (repo / "config" / ".env").write_text("SECRET=1", encoding="utf-8")
    (repo / "README.md").write_text("demo", encoding="utf-8")
    if with_gitignore:
        (repo / ".gitignore").write_text("config/.env\n", encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["git", "init"], ["git", "add", "-A"],
                ["git", "commit", "-m", "init"]):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True, env=env)
    return repo


def _load_release_bundle():
    sys_path = str(Path(__file__).resolve().parent.parent / "scripts")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "release_bundle", Path(sys_path) / "release_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_bundle_assemble_end_to_end(tmp_path):
    """组装合同全链路:git archive 干净树 + web/dist 覆盖 + manifest +
    双重排除机检 + zip 内 digest 复验;凭据文件被 .gitignore 排除在
    tracked 之外,机检对 zip 内容再验一次(第二道防线)。"""
    repo = _init_repo(tmp_path, with_gitignore=True)
    web_dist = _make_dist(tmp_path / "webbuild")
    out_zip = tmp_path / "bundle.zip"
    module = _load_release_bundle()

    manifest = module.assemble(
        repo=repo, web_dist=web_dist, out_zip=out_zip,
        git_sha="0" * 40, node_version="v22.12.0")
    assert manifest["frontend_sha256"] == frontend_dist_digest(web_dist)
    with zipfile.ZipFile(out_zip) as zf:
        names = zf.namelist()
        assert "web/dist/index.html" in names
        assert "web/dist/manifest.json" in names
        assert "README.md" in names
        # git archive 只携带 tracked 文件:凭据绝不进包
        assert "config/.env" not in names
        # 机检对 zip 内容复验过;这里再独立断言一次排除名单函数对
        # 解包内容为空(防御在深的两道防线各自可证)
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        zf.extractall(extracted)
    assert bundle_exclusion_violations(extracted) == []


def test_release_bundle_assemble_fails_on_tracked_secret(tmp_path):
    """机检的第二道防线必须真实可触发:假仓库没有 .gitignore、.env 被
    tracked 时,组装当场失败并点名违规——打包事故不可能靠自觉拦截。"""
    repo = _init_repo(tmp_path, with_gitignore=False)
    web_dist = _make_dist(tmp_path / "webbuild")
    module = _load_release_bundle()
    with pytest.raises(SystemExit, match="config/.env"):
        module.assemble(repo=repo, web_dist=web_dist,
                        out_zip=tmp_path / "bundle.zip",
                        git_sha="0" * 40, node_version="v22.12.0")
    assert not (tmp_path / "bundle.zip").exists()


# ─────────────────── create_app 与路由探针 ───────────────────


def test_resolved_dist_keeps_api_and_mcp_routes(tmp_path):
    """dist 切换不得破坏路由次序(Mount 全捕获的历史坑):首页、/api、
    /mcp 三条都必须可达。"""
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    dist = _make_dist(tmp_path / "webdist")
    write_manifest(dist, git_sha="abc", built_at="now", node_version="v22")
    app = create_app(workflows_dir=workflows, runs_dir=tmp_path / "runs",
                     registry_factory=lambda _: make_registry(FakeProvider()),
                     web_dist_dir=dist)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/workflows").status_code == 200
        # /mcp 路由存在(GET 无会话非 404 即可达;405/400 都算路由命中)
        response = client.get("/mcp")
        assert response.status_code != 404


def test_web_dist_still_ignored_by_git():
    """审查点:Git 仍不跟踪 web/dist(.gitignore 未松动)。"""
    repo = Path(__file__).resolve().parent.parent
    check = subprocess.run(
        ["git", "check-ignore", "-v", "web/dist/index.html"],
        cwd=repo, capture_output=True, text=True)
    assert check.returncode == 0, "web/dist 必须仍被 .gitignore 忽略"


def test_serve_warns_but_serves_on_manifest_mismatch(tmp_path, monkeypatch, capsys):
    """哈希分级的开发侧合同:manifest 不符时 serve 打 stderr 警告并继续
    启动(不崩溃)——修复过的 sys 缺失回归锁。"""
    dist = _make_dist(tmp_path / "webdist")
    write_manifest(dist, git_sha="abc", built_at="now", node_version="v22")
    (dist / "assets" / "app.js").write_text("tampered", encoding="utf-8")
    assert manifest_mismatch(dist) is not None   # 预置:确实不符

    order = []
    import atlas.web as web_module
    from atlas import config_init
    import uvicorn

    probe_socket = __import__("socket").socket()
    probe_socket.bind(("127.0.0.1", 0))
    port = probe_socket.getsockname()[1]
    probe_socket.close()

    monkeypatch.setattr(config_init, "initialize_runtime_config",
                        lambda: order.append("init"))
    # manifest_mismatch 保持真实实现(不 patch)——被测的就是它的警告路径
    monkeypatch.setattr("atlas.distbundle.resolve_web_dist",
                        lambda *a, **kw: dist)
    monkeypatch.setattr(web_module, "create_app",
                        lambda **kwargs: order.append("app") or object())
    monkeypatch.setattr(uvicorn, "run",
                        lambda app, **kwargs: order.append("run"))
    web_module.serve(port=port)
    captured = capsys.readouterr()
    assert "警告" in captured.err and "不符" in captured.err
    assert order == ["init", "app", "run"]   # 警告后照常启动
