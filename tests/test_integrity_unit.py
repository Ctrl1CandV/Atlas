# -*- coding: utf-8 -*-
"""M0 清单第 5 条:read_artifact 的哈希断言与缺失显式失败,单元级验证。"""
import pytest

from atlas.integrity import (
    IntegrityError,
    ResourceLimitError,
    WiringError,
    build_projection,
    read_artifact,
    sha256_bytes,
    store_artifact,
)


def test_store_and_read_roundtrip(tmp_path):
    ref = store_artifact(tmp_path, name="a.output", filename="a.output.1.txt",
                         content=b"hello atlas")
    assert read_artifact(ref) == b"hello atlas"
    # 旁车哈希文件是给人审计的,内容必须与引用一致
    sidecar = (tmp_path / "artifacts" / "a.output.1.txt.sha256").read_text()
    assert sidecar == ref.sha256 == sha256_bytes(b"hello atlas")


def test_read_artifact_detects_tampering(tmp_path):
    ref = store_artifact(tmp_path, name="a.output", filename="a.output.1.txt",
                         content="原始内容".encode("utf-8"))
    ref.path.write_bytes("被改动的内容".encode("utf-8"))  # 模拟落盘后被改
    with pytest.raises(IntegrityError) as e:
        read_artifact(ref)
    assert "哈希不符" in str(e.value)


def test_read_artifact_missing_file_is_integrity_error(tmp_path):
    ref = store_artifact(tmp_path, name="a.output", filename="a.output.1.txt",
                         content=b"x")
    ref.path.unlink()  # 模拟文件被删
    with pytest.raises(IntegrityError):
        read_artifact(ref)


def test_read_artifact_none_is_wiring_error():
    # 兜底:绝不给空串继续跑
    with pytest.raises(WiringError):
        read_artifact(None)


def test_artifact_limits_fail_without_partial_files(tmp_path):
    with pytest.raises(ResourceLimitError, match="拒绝截断"):
        store_artifact(tmp_path, name="big", filename="big.bin",
                       content=b"12345", max_bytes=4)
    assert not (tmp_path / "artifacts").exists()


def test_read_rejects_oversized_artifact_before_loading(tmp_path, monkeypatch):
    from atlas import integrity

    ref = store_artifact(tmp_path, name="a.output", filename="a.txt", content=b"12345")
    monkeypatch.setattr(integrity, "ARTIFACT_MAX_BYTES", 4)
    with pytest.raises(ResourceLimitError, match="超过上限"):
        read_artifact(ref)


def test_projection_limit_leaves_no_partial_projection(tmp_path, monkeypatch):
    from atlas import integrity

    task = store_artifact(tmp_path, name="task", filename="task.txt", content=b"12345")
    monkeypatch.setattr(integrity, "PROJECTION_MAX_BYTES", 4)
    with pytest.raises(ResourceLimitError, match="输入投影"):
        build_projection(tmp_path, node_id="n", iteration=1, prompt="p",
                         consumes=["task"], artifacts={"task": task.as_dict()})
    assert not (tmp_path / "projections").exists()
