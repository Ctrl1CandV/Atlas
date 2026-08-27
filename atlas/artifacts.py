# -*- coding: utf-8 -*-
"""类型化产物模型(PLAN-v3 M6-B)。

此前节点的产物靠一组散落的 *_path 字段传递,前端只能按扩展名猜含义——
coding agent 的 patch 因此从未被稳定暴露。本模块把产物统一为带角色
(role)的类型化条目,事件流仍是唯一真相:node_done 事件写 `artifacts`
数组,旧事件(只有 output_path/diff_path)由 fold 时的兼容路径转换。

角色是封闭枚举,不参与执行语义(consumes 仍按逻辑名引用),只服务于
展示与下载;执行层照旧只认 {name, path, sha256}(ArtifactRef 同构)。
"""
import os
from pathlib import Path

from atlas.integrity import (ArtifactRef, IntegrityError, read_artifact,
                             sha256_bytes)

# 封闭清单:加角色要改这里,不接受 YAML/外部输入发明新角色
ARTIFACT_ROLES = frozenset({"report", "output", "diff", "projection", "raw",
                            "error"})

_ROLE_TITLES = {
    "report": "执行报告",
    "output": "输出",
    "diff": "代码改动",
    "projection": "输入投影",
    "raw": "原始产物",
    "error": "错误上下文",   # P3 soft failure 的 write-once 错误产物
}


def artifact_entry(*, name: str, role: str, path: Path | str, sha256: str,
                   size_bytes: int, complete: bool = True,
                   media_type: str = "text/plain",
                   metadata: dict | None = None) -> dict:
    """构造一条类型化产物记录。name 即逻辑名(consumes 可引用)。"""
    if role not in ARTIFACT_ROLES:
        raise ValueError(f"未知产物角色 {role!r};可用:{sorted(ARTIFACT_ROLES)}")
    entry = {
        "name": name,            # 逻辑名:node.output / node.diff(consumes 引用它)
        "role": role,
        "title": _ROLE_TITLES[role],
        "path": str(path),
        "sha256": sha256,
        "bytes": size_bytes,
        "complete": complete,    # False=超限截断/仅摘要,绝不冒充完整
        "media_type": media_type,
    }
    if metadata:
        entry["metadata"] = metadata
    return entry


def artifacts_from_event(event: dict) -> list[dict]:
    """从 node_done 事件提取类型化产物列表。

    新事件带 artifacts 数组,原样返回(逐条校验角色);
    旧事件只有 output_path/diff_path——按角色合成,保证旧账本可读。
    """
    raw = event.get("artifacts")
    if raw:
        out = []
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue   # 畸形条目跳过:fold 必须永远可完成(审查 M6-minor5)
            entry = dict(item)
            if entry.get("role") not in ARTIFACT_ROLES:
                entry["role"] = "raw"
            entry.setdefault("title", _ROLE_TITLES.get(entry["role"], entry["role"]))
            entry.setdefault("complete", True)
            entry.setdefault("media_type", "text/plain")
            entry.setdefault("bytes", 0)
            out.append(entry)
        return out
    # 兼容路径:旧 run 的 node_done
    legacy = []
    node = event.get("node", "")
    if event.get("output_path"):
        legacy.append(artifact_entry(
            name=f"{node}.output",
            role="diff" if str(event["output_path"]).endswith(".patch") else "output",
            path=event["output_path"], sha256=event.get("output_sha256") or "",
            size_bytes=-1))   # 旧事件没记字节数;-1=未知(前端显示 —)
    if event.get("diff_path"):
        legacy.append(artifact_entry(
            name=f"{node}.diff", role="diff", path=event["diff_path"],
            sha256=event.get("diff_sha256") or "", size_bytes=-1,
            media_type="text/x-diff"))
    return legacy


IMPORT_ALGO_VERSION = "p7-import-v1"


def copy_imported_artifact(*, source_path: Path, source_sha256: str,
                           run_dir: Path, name: str,
                           iteration: int = 0) -> ArtifactRef:
    """P7:把源 run 的产物逐字节复制进本 run 的 write-once 产物库。

    原子性:先写临时文件、fsync、再 os.replace 到终名——复制中途被 kill
    只会留下临时残片,绝不会出现"半份正式产物"(验收合同)。写后用
    read_artifact 带哈希断言读回一次,证明落盘字节与声明的 source sha 一致。
    """
    content = Path(source_path).read_bytes()
    digest = sha256_bytes(content)
    if digest != source_sha256:
        raise IntegrityError(
            f"导入 {name}:源文件内容哈希 {digest[:16]}… 与事件记录的 "
            f"{source_sha256[:16]}… 不符——源在锁内仍发生了漂移,拒绝复制")
    filename = f"{name}.imported{iteration}.bin"
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    final_path = artifacts_dir / filename
    stem, suffix = final_path.stem, final_path.suffix
    counter = 0
    while final_path.exists():          # 与 _unique_path 同法:绝不覆盖既有产物
        counter += 1
        final_path = artifacts_dir / f"{stem}.{counter}{suffix}"
    tmp_path = final_path.with_name(final_path.name + ".partial")
    try:
        with open(tmp_path, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, final_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    ref = ArtifactRef(name=name, path=final_path, sha256=digest)
    read_artifact(ref)   # 写后复验:任何不一致在此响亮失败
    return ref
