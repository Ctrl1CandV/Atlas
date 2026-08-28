# -*- coding: utf-8 -*-
"""E-3 · 发布包前端产物的构建端与运行端共享工具。

三方复用同一实现,杜绝"打包者算的哈希"与"启动端算的哈希"漂移:
- release-assets.yml 打包阶段:frontend_dist_digest 写 manifest.json、
  bundle_exclusion_violations 对暂存树做排除名单机检(机器检查,不是
  打包者自觉);
- atlas-web 启动端:manifest_mismatch 做哈希分级(开发态警告继续,
  发布冒烟 job 断言相等否则 fail);
- CI 冒烟 job:同一 digest 函数比对。

digest 口径:dist 内**除 manifest.json 外**的全部普通文件,按 POSIX
相对路径排序,逐行 `sha256(hex)  path` 拼接后再整体 SHA-256。字段只增
不改;升级口径换字段名,旧 manifest 永远可解释。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

MANIFEST_FILENAME = "manifest.json"

# 发布 bundle 的排除名单(机检对象)。git archive 只携带 tracked 文件,
# 本名单是"tracked 保证"之上的第二道防线(防御在深):
# - 凭据与活动配置(设计上被 .gitignore 排除,绝不能因打包脚本失误回流)
# - 运行记录与初始化痕迹
# - 缓存/虚拟环境/构建中间目录
# - 密钥材料与数据库文件
FORBIDDEN_PATH_PATTERNS: tuple[str, ...] = (
    "config/.env",
    "config/providers.json",
    "config/models.reference.json",
    "config/capabilities.json",
    "config/pricing.json",
    "config/agents.json",
    "config/.atlas-init-notice.json",
    "config/.atlas-init-journal.json",
    "config/.atlas-init.lock",
)
FORBIDDEN_DIR_NAMES = frozenset({"__pycache__", ".venv", ".pytest_cache",
                                 "node_modules", ".trash", ".locks"})
# 运行记录目录:精确名单而非前缀匹配——"runners" 之类合法顶层目录不能误伤
FORBIDDEN_TOP_DIRS = frozenset({"runs", "runs-archive"})
FORBIDDEN_SUFFIXES = (".key", ".pem", ".sqlite", ".sqlite-wal",
                      ".sqlite-shm", ".db", ".db-wal", ".db-shm", ".log")


def _dist_files(dist: Path) -> list[Path]:
    return sorted((p for p in dist.rglob("*") if p.is_file()),
                  key=lambda p: p.relative_to(dist).as_posix())


def frontend_dist_digest(dist: Path) -> str:
    """dist 内容的确定性摘要(manifest.json 自身除外,防自引用)。"""
    digest = hashlib.sha256()
    for path in _dist_files(dist):
        rel = path.relative_to(dist).as_posix()
        if rel == MANIFEST_FILENAME:
            continue
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        digest.update(f"{file_hash}  {rel}\n".encode("utf-8"))
    return digest.hexdigest()


def write_manifest(dist: Path, *, git_sha: str, built_at: str,
                   node_version: str) -> dict:
    """打包阶段写入 manifest.json(放 dist 内,哈希集合不含它自身)。"""
    manifest = {
        "frontend_sha256": frontend_dist_digest(dist),
        "git_sha": git_sha,
        "built_at": built_at,
        "node_version": node_version,
        "manifest_version": 1,
    }
    (dist / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True)
        + "\n", encoding="utf-8")
    return manifest


def manifest_mismatch(dist: Path) -> str | None:
    """启动端哈希分级:相符/无 manifest → None;不符或损坏 → 警告文案。

    调用方决定分级:本地开发态打 stderr 警告继续跑;发布冒烟 job 对
    非 None 直接 fail。manifest.json 缺失视为"无发布信息"(开发树常态),
    不警告——警告只在"声明了身份却不符"时出现。
    """
    manifest_path = dist / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = manifest["frontend_sha256"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        return (f"前端产物 manifest.json 存在但无法解读({dist});"
                "发布身份不可证实——若是官方发布包请勿使用,若是本地开发树"
                "请删除该文件或重新构建")
    actual = frontend_dist_digest(dist)
    if actual == expected:
        return None
    return (f"前端产物哈希与 manifest 不符({dist}):manifest 声明 "
            f"{expected[:16]}…,实际 {actual[:16]}…。dist 在构建后被改动——"
            "本地开发可继续,发布环境必须使用未改动的官方包")


def bundle_exclusion_violations(bundle_root: Path) -> list[str]:
    """对组装好的 bundle 暂存树做排除名单机检,返回违规路径清单。

    打包脚本必须在本检查通过后才能压缩;任何违规都是打包事故
    (凭据/运行记录/缓存类目录混进发布包),fail the job。
    """
    violations: list[str] = []
    for path in sorted(bundle_root.rglob("*")):
        rel = path.relative_to(bundle_root).as_posix()
        parts = path.relative_to(bundle_root).parts
        if rel in FORBIDDEN_PATH_PATTERNS:
            violations.append(rel)
            continue
        if any(part in FORBIDDEN_DIR_NAMES for part in parts[:-1]):
            violations.append(rel)
            continue
        if parts[0] in FORBIDDEN_TOP_DIRS:
            violations.append(rel)
            continue
        if path.name.endswith(FORBIDDEN_SUFFIXES):
            violations.append(rel)
    return violations


def resolve_web_dist(cli_arg: str | None = None, *,
                     environ=None, repo_root: Path | None = None,
                     package_dir: Path | None = None) -> Path:
    """四级 dist 解析:CLI 参数 > ATLAS_WEB_DIST > 仓库 sibling web/dist
    > atlas 包内 web-dist(发布包内嵌布局)。全 miss → fail-loud,
    报错列出四个候选与三条可行出路。repo_root/package_dir 供测试注入。"""
    import os
    environ = os.environ if environ is None else environ
    repo_root = (Path(__file__).resolve().parent.parent
                 if repo_root is None else Path(repo_root))
    package_dir = (Path(__file__).resolve().parent
                   if package_dir is None else Path(package_dir))
    candidates: list[tuple[str, Path]] = []
    if cli_arg:
        candidates.append(("CLI 参数", Path(cli_arg)))
    env_value = environ.get("ATLAS_WEB_DIST", "")
    if env_value:
        candidates.append(("环境变量 ATLAS_WEB_DIST", Path(env_value)))
    candidates.append(("仓库 sibling web/dist", repo_root / "web" / "dist"))
    candidates.append(("发布包内嵌 atlas/web-dist",
                       package_dir / "web-dist"))
    for label, path in candidates:
        if (path / "index.html").is_file():
            return path
    listing = "\n".join(f"  - {label}: {path}" for label, path in candidates)
    raise RuntimeError(
        "Web 构建产物缺失,四个候选位置都没有 index.html:\n"
        f"{listing}\n"
        "三条可行出路:①安装 Node.js ≥22.12 后在 web/ 目录运行 "
        "`npm ci && npm run build`;②把已构建的 dist 目录路径设到环境变量 "
        "ATLAS_WEB_DIST;③下载官方 Release 的 bundle 包(内含已构建前端)。")
