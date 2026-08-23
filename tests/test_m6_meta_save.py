# -*- coding: utf-8 -*-
"""M6(PLAN-v3):meta schema 校验 + atlas_save_workflow 受限保存。"""
import pytest

from atlas.mcp import save_workflow_impl
from atlas.spec import SpecError, spec_from_yaml

GOOD_YAML = """
name: m6-save-demo
description: 保存测试图
meta:
  title: 保存测试
  kind: custom
  category: research
  tags: [测试]
  estimated_calls: 1
nodes:
  - id: a
    type: llm
    model: Fake:primary
    prompt: 做事。
    consumes: [task]
edges:
  - from: a
    to: END
"""


def test_meta_rejects_unknown_field():
    with pytest.raises(SpecError, match="未知字段"):
        spec_from_yaml(GOOD_YAML.replace("  tags: [测试]\n",
                                         "  tags: [测试]\n  bogus: 1\n"))


def test_meta_rejects_bad_kind_and_category():
    with pytest.raises(SpecError, match="kind"):
        spec_from_yaml(GOOD_YAML.replace("kind: custom", "kind: demo"))
    with pytest.raises(SpecError, match="category"):
        spec_from_yaml(GOOD_YAML.replace("category: research", "category: 杂项"))


def test_meta_requires_consistency_with_graph():
    """声明需人工批准但图里没有 human 节点 → 拒绝(示例卡不许骗人)。"""
    with pytest.raises(SpecError, match="human 节点"):
        spec_from_yaml(GOOD_YAML.replace(
            "  estimated_calls: 1\n",
            "  estimated_calls: 1\n  requires:\n    human_approval: true\n"))


def test_meta_not_in_fingerprint():
    """meta 是展示信息:改 meta 不改指纹,不影响旧 run 语义。"""
    from atlas.spec import spec_fingerprint
    a = spec_from_yaml(GOOD_YAML)
    b = spec_from_yaml(GOOD_YAML.replace("title: 保存测试", "title: 改了标题"))
    assert spec_fingerprint(a) == spec_fingerprint(b)


def test_save_creates_valid_workflow(tmp_path):
    r = save_workflow_impl("demo-save", GOOD_YAML, workflows_dir=tmp_path)
    assert r["saved"] is True and r["created"] is True
    assert (tmp_path / "demo-save.yaml").exists()
    # 保存的文件能被正常解析器读回
    spec = spec_from_yaml((tmp_path / "demo-save.yaml").read_text(encoding="utf-8"))
    assert spec.meta.kind == "custom"


def test_save_rejects_invalid_yaml(tmp_path):
    r = save_workflow_impl("bad-save",
                           GOOD_YAML.replace("type: llm", "type: sorcery"),
                           workflows_dir=tmp_path)
    assert r["saved"] is False
    assert not (tmp_path / "bad-save.yaml").exists()


def test_save_update_requires_expected_sha(tmp_path):
    save_workflow_impl("occ", GOOD_YAML, workflows_dir=tmp_path)
    r = save_workflow_impl("occ", GOOD_YAML.replace("做事。", "做别的事。"),
                           workflows_dir=tmp_path)
    assert r["saved"] is False and "expected_sha256" in r["error"]


def test_save_update_rejects_stale_hash(tmp_path):
    first = save_workflow_impl("occ2", GOOD_YAML, workflows_dir=tmp_path)
    stale = first["file_sha256"]
    # 别人(或人工)先改了一版
    p = tmp_path / "occ2.yaml"
    p.write_text(GOOD_YAML.replace("做事。", "人工改过。"), encoding="utf-8")
    r = save_workflow_impl("occ2", GOOD_YAML.replace("做事。", "又一版。"),
                           expected_sha256=stale, workflows_dir=tmp_path)
    assert r["saved"] is False and "current_sha256" in r
    assert "人工改过" in p.read_text(encoding="utf-8")   # 人工修改未被覆盖


def test_save_update_with_fresh_hash_succeeds(tmp_path):
    import hashlib
    first = save_workflow_impl("occ3", GOOD_YAML, workflows_dir=tmp_path)
    fresh = hashlib.sha256((tmp_path / "occ3.yaml").read_bytes()).hexdigest()
    r = save_workflow_impl("occ3", GOOD_YAML.replace("做事。", "正经更新。"),
                           expected_sha256=fresh, workflows_dir=tmp_path)
    assert r["saved"] is True and r["created"] is False


def test_save_rejects_traversal_and_reserved_ids(tmp_path):
    for bad in ["../evil", "a/b", "CON", "com1", "..."]:
        r = save_workflow_impl(bad, GOOD_YAML, workflows_dir=tmp_path)
        assert r["saved"] is False, bad
    assert list(tmp_path.glob("*.yaml")) == []   # 什么都没落盘


def test_save_does_not_warn_for_unconfigured_example_models(tmp_path):
    y = """
name: unconfigured-example
meta:
  kind: example
nodes:
  - id: a
    type: llm
    prompt: 第一件事。
    consumes: [task]
  - id: b
    type: llm
    prompt: 第二件事。
    consumes: [a.output]
edges:
  - from: a
    to: b
  - from: b
    to: END
"""
    r = save_workflow_impl("unconfigured-example", y, workflows_dir=tmp_path)
    assert r["saved"] is True
    assert not any("同厂商" in w for w in r["warnings"])


def test_save_warns_same_vendor(tmp_path):
    y = """
name: same-vendor
nodes:
  - id: a
    type: llm
    model: Fake:primary
    prompt: 第一件事。
    consumes: [task]
  - id: b
    type: llm
    model: Fake:primary
    prompt: 第二件事。
    consumes: [a.output]
edges:
  - from: a
    to: b
  - from: b
    to: END
"""
    r = save_workflow_impl("same-vendor", y, workflows_dir=tmp_path)
    assert r["saved"] is True
    assert any("同厂商" in w for w in r["warnings"])
