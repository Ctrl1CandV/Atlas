# -*- coding: utf-8 -*-
"""E-3 · 组装发布 bundle:干净代码树 + web/dist + manifest.json。

合同(PLAN-stage-e E-3):
- 干净代码树来自 `git archive HEAD`——只携带 tracked 文件,凭据/运行
  记录/活动配置(全部 gitignored)天然不在;
- web/dist 覆盖其上,manifest 由与运行端/冒烟 job 同一函数写入
  (atlas.distbundle.write_manifest,digest 不含 manifest 自身);
- 排除名单机检是机器检查,不是打包者自觉:先查暂存树,压缩后**再解包
  复验一次**(staging 与 zip 内容必须同构),并断言 dist 完整
  (index.html 在、digest 与 manifest 相符);
- 任何违规都让本脚本非零退出,fail the release job。

本地可测:`python scripts/release_bundle.py --repo <repo> --out /tmp/x.zip`
(零网络、零发布动作)。
"""
import argparse
import json
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atlas.distbundle import (bundle_exclusion_violations,  # noqa: E402
                              frontend_dist_digest, write_manifest)


def _git_archive_extract(repo: Path, stage: Path) -> None:
    archive = stage.parent / "code-tree.zip"
    with archive.open("wb") as handle:
        subprocess.run(["git", "archive", "--format=zip", "HEAD"],
                       cwd=repo, stdout=handle, check=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(stage)
    archive.unlink()


def _violations_or_fail(root: Path, label: str) -> None:
    violations = bundle_exclusion_violations(root)
    if violations:
        raise SystemExit(
            f"{label} 排除名单违规({len(violations)} 项,打包事故,fail):"
            f"{violations}")


def assemble(*, repo: Path, web_dist: Path, out_zip: Path,
             git_sha: str, node_version: str) -> dict:
    web_dist = web_dist.resolve()
    if not (web_dist / "index.html").is_file():
        raise SystemExit(
            f"web/dist 缺少 index.html({web_dist});先 npm run build")
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "bundle"
        stage.mkdir()
        _git_archive_extract(repo, stage)
        staged_dist = stage / "web" / "dist"
        staged_dist.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [sys.executable, "-c",
             "import shutil, sys; shutil.copytree(sys.argv[1], sys.argv[2])",
             str(web_dist), str(staged_dist)],
            check=True)
        built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        manifest = write_manifest(
            staged_dist, git_sha=git_sha, built_at=built_at,
            node_version=node_version)
        _violations_or_fail(stage, "bundle 暂存树")
        out_zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(stage).as_posix())
        # 压缩后解包复验:staging 与 zip 内容必须同构,digest 必须相符
        verify = Path(tmp) / "verify"
        verify.mkdir()
        with zipfile.ZipFile(out_zip) as zf:
            zf.extractall(verify)
        _violations_or_fail(verify, "bundle zip 内容")
        extracted_dist = verify / "web" / "dist"
        if not (extracted_dist / "index.html").is_file():
            raise SystemExit("bundle zip 内 web/dist 缺少 index.html")
        actual = frontend_dist_digest(extracted_dist)
        if actual != manifest["frontend_sha256"]:
            raise SystemExit(
                "bundle zip 内前端 digest 与 manifest 不符:"
                f"{actual} != {manifest['frontend_sha256']}")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="release_bundle",
        description="组装发布 bundle(干净代码树 + web/dist + manifest)")
    parser.add_argument("--repo", default=".", help="仓库根(git archive 源)")
    parser.add_argument("--web-dist", default=str(Path("web") / "dist"),
                        help="已构建的 web/dist 目录")
    parser.add_argument("--out", required=True, help="输出 zip 路径")
    parser.add_argument("--git-sha", default="", help="构建 commit(git rev-parse HEAD 可得)")
    parser.add_argument("--node-version", default="", help="构建用 Node 版本")
    args = parser.parse_args()
    git_sha = args.git_sha or subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=args.repo, capture_output=True,
        text=True, check=True).stdout.strip()
    manifest = assemble(
        repo=Path(args.repo).resolve(), web_dist=Path(args.web_dist),
        out_zip=Path(args.out), git_sha=git_sha,
        node_version=args.node_version)
    print(json.dumps({"out": str(args.out), **manifest}, ensure_ascii=False))


if __name__ == "__main__":
    main()
