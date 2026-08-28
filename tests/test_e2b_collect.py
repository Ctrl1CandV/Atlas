# -*- coding: utf-8 -*-
"""E-2B · agent collect(多命名收集)。

合同来源:PLAN-stage-e-2026-08-27.md 的 E-2B 章。collect 是 agents.json
runner 配置里的只读收集清单;CLI 成功后按相对 glob 扫描执行目录,命中
文件 write-once 入库并追加到 node_done.artifacts 尾部(相对路径字典序)。

安全/诚实合同:glob 禁 .. 与绝对路径与反斜杠;symlink/junction 全拒;
上限(≤20 文件/≤64 MiB)触发治理失败、绝不静默截断清单(partial
collect = 假完整);同输入两跑 artifacts 顺序逐项相同。全部用注入
runner 与临时目录,零真实 CLI、零供应商调用。
"""
import json
import os
import stat

import pytest

from atlas.adapters import FakeProvider
from atlas.config import (AgentCollectSpec, AgentRunnerConfig, ConfigError,
                          load_agent_config)
from atlas.engine import execute_graph
from atlas.events import fold_events
from atlas.integrity import sha256_bytes
from atlas.nodes.agent import (COLLECT_MAX_FILES,
                               COLLECT_MAX_TOTAL_BYTES,
                               collect_agent_artifacts)
from atlas.spec import spec_from_yaml

from conftest import make_registry


# ─────────────────── agents.json 配置校验 ───────────────────


def _write_config(tmp_path, raw):
    path = tmp_path / "agents.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return load_agent_config(path)


BASE_CONFIG = {"runner": "local_cli",
               "cli": {"kind": "claude", "command": "claude",
                       "extra_args": []}}


def test_collect_config_parses_closed_schema(tmp_path):
    cfg = _write_config(tmp_path, {**BASE_CONFIG, "collect": [
        {"pattern": "patches/*.patch", "name_prefix": "patch",
         "role": "output", "ext": ".patch"},
        {"pattern": "notes/**", "name_prefix": "notes", "role": "raw"},
    ], "collect_exclude_dirs": ["custom-out"]})
    assert len(cfg.collect) == 2
    assert cfg.collect[0].ext == ".patch"
    assert cfg.collect[1].role == "raw"
    assert cfg.collect_exclude_dirs == ("custom-out",)


def test_collect_config_rejects_role_misuse_and_patterns(tmp_path):
    bad_cases = [
        ({"collect": [{"pattern": "x", "name_prefix": "p",
                       "role": "diff"}]}, "diff 由系统"),
        ({"collect": [{"pattern": "x", "name_prefix": "p",
                       "role": "changes"}]}, "不开放"),
        ({"collect": [{"pattern": "x", "name_prefix": "p",
                       "role": "input"}]}, "不开放"),
        ({"collect": [{"pattern": "../escape", "name_prefix": "p"}]},
         "'..'"),
        ({"collect": [{"pattern": "/abs/path", "name_prefix": "p"}]},
         "相对执行目录"),
        ({"collect": [{"pattern": "C:/abs", "name_prefix": "p"}]},
         "相对执行目录"),
        ({"collect": [{"pattern": "a\\b", "name_prefix": "p"}]},
         "反斜杠"),
        ({"collect": [{"pattern": "//", "name_prefix": "p"}]},
         "相对执行目录"),
        ({"collect": [{"pattern": "x", "name_prefix": "Patch"}]},
         "name_prefix"),
        ({"collect": [{"pattern": "x", "name_prefix": "p"},
                      {"pattern": "y", "name_prefix": "p"}]}, "重复"),
        ({"collect": [{"pattern": "x", "name_prefix": "p",
                       "unknown": 1}]}, "未知字段"),
        ({"collect": [{"pattern": "x", "name_prefix": "p", "ext": "patch"}]},
         "ext"),
        ({"collect_exclude_dirs": ["a/b"]}, "collect_exclude_dirs"),
        ({"collect_exclude_dirs": ["Upper"]}, "collect_exclude_dirs"),
    ]
    for override, fragment in bad_cases:
        with pytest.raises(ConfigError, match=fragment):
            _write_config(tmp_path, {**BASE_CONFIG, **override})


def test_collect_config_missing_stays_disabled(tmp_path):
    cfg = _write_config(tmp_path, BASE_CONFIG)
    assert cfg.collect == ()
    assert cfg.collect_exclude_dirs == ()


# ─────────────────── 扫描与入库(单元,确定性) ───────────────────


SPECS = (AgentCollectSpec(pattern="patches/*.patch", name_prefix="patch",
                          role="output", ext=".patch"),
         AgentCollectSpec(pattern="notes/**", name_prefix="notes",
                          role="raw"))


def _build_tree(root):
    (root / "patches").mkdir(parents=True)
    (root / "patches" / "fix1.patch").write_text("fix1 diff", encoding="utf-8")
    (root / "patches" / "fix2.patch").write_text("fix2 diff", encoding="utf-8")
    (root / "notes" / "deep").mkdir(parents=True)
    (root / "notes" / "调研.md").write_text("# 笔记", encoding="utf-8")
    (root / "notes" / "deep" / "more.md").write_text("more", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.patch").write_text("junk", encoding="utf-8")
    (root / "unrelated.txt").write_text("no match", encoding="utf-8")


def test_collect_scan_matches_sorts_and_names_deterministically(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    scan_root = tmp_path / "cwd"
    _build_tree(scan_root)
    entries = collect_agent_artifacts(
        run_dir=run_dir, node_id="coder", iteration=1,
        scan_root=scan_root, collect_specs=SPECS, exclude_extra=())
    # 相对路径字典序,排除目录命中不收;node_modules/junk.patch 不出现;
    # 逻辑名 = prefix.清洗后的相对路径(分隔符/点号折叠连字,保留 CJK)
    assert [e["name"] for e in entries] == [
        "notes.notes-deep-more-md", "notes.notes-调研-md",
        "patch.patches-fix1", "patch.patches-fix2"]
    assert all(e["role"] in ("output", "raw") for e in entries)
    assert entries[0]["media_type"] == "text/markdown"
    patch_entry = next(e for e in entries if e["name"] == "patch.patches-fix1")
    assert patch_entry["media_type"] == "text/x-diff"
    assert patch_entry["metadata"]["collected_from"] == "patches/fix1.patch"
    # 落盘字节可复验
    assert sha256_bytes(open(entries[0]["path"], "rb").read()) \
        == entries[0]["sha256"]
    # 同输入两跑:顺序与名字逐项相同(确定性合同)
    run_dir2 = tmp_path / "run2"
    run_dir2.mkdir()
    entries2 = collect_agent_artifacts(
        run_dir=run_dir2, node_id="coder", iteration=1,
        scan_root=scan_root, collect_specs=SPECS, exclude_extra=())
    assert [e["name"] for e in entries2] == [e["name"] for e in entries]


def test_collect_config_exclude_dirs_appended(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    scan_root = tmp_path / "cwd"
    (scan_root / "custom-out").mkdir(parents=True)
    (scan_root / "custom-out" / "x.patch").write_text("x", encoding="utf-8")
    (scan_root / "patches").mkdir()
    (scan_root / "patches" / "keep.patch").write_text("k", encoding="utf-8")
    entries = collect_agent_artifacts(
        run_dir=run_dir, node_id="coder", iteration=1,
        scan_root=scan_root, collect_specs=SPECS[:1],
        exclude_extra=["custom-out"])
    assert [e["name"] for e in entries] == ["patch.patches-keep"]


def test_collect_caps_fail_governance_with_facts(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    scan_root = tmp_path / "cwd"
    (scan_root / "patches").mkdir(parents=True)
    for i in range(COLLECT_MAX_FILES + 1):
        (scan_root / "patches" / f"f{i}.patch").write_text(
            f"diff {i}", encoding="utf-8")
    with pytest.raises(Exception, match=f"命中 {COLLECT_MAX_FILES + 1} 个文件"):
        collect_agent_artifacts(
            run_dir=run_dir, node_id="coder", iteration=1,
            scan_root=scan_root, collect_specs=SPECS[:1], exclude_extra=())
    # 20 个恰好合法(边界下侧)
    (scan_root / "patches" / f"f{COLLECT_MAX_FILES}.patch").unlink()
    entries = collect_agent_artifacts(
        run_dir=run_dir, node_id="coder", iteration=1,
        scan_root=scan_root, collect_specs=SPECS[:1], exclude_extra=())
    assert len(entries) == COLLECT_MAX_FILES


def test_collect_total_bytes_cap_boundary(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    scan_root = tmp_path / "cwd"
    (scan_root / "patches").mkdir(parents=True)
    per = 16 * 1024 * 1024   # 每件恰好卡产物上限(合计帽的约束场景)
    for i in range(4):
        (scan_root / "patches" / f"big{i}.patch").write_bytes(b"\0" * per)
    # 16MiB×4 = 64MiB 恰好合法(≤);再添 1 字节即越界
    entries = collect_agent_artifacts(
        run_dir=run_dir, node_id="coder", iteration=1,
        scan_root=scan_root, collect_specs=SPECS[:1], exclude_extra=())
    assert len(entries) == 4
    (scan_root / "patches" / "tip.patch").write_bytes(b"\0")
    with pytest.raises(Exception, match=f"超过上限 {COLLECT_MAX_TOTAL_BYTES}"):
        collect_agent_artifacts(
            run_dir=run_dir, node_id="coder", iteration=1,
            scan_root=scan_root, collect_specs=SPECS[:1], exclude_extra=())


def test_collect_logical_name_collision_fails_governance(tmp_path):
    """清洗同形(notes/a.b 与 notes/a-b → notes-a-b):state 与 fold 都是
    后者胜,静默覆盖=变相假完整,治理失败并点名两条相对路径。"""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    scan_root = tmp_path / "cwd"
    (scan_root / "notes").mkdir(parents=True)
    (scan_root / "notes" / "a.b").write_text("one", encoding="utf-8")
    (scan_root / "notes" / "a-b").write_text("two", encoding="utf-8")
    with pytest.raises(Exception, match="逻辑名冲突") as exc_info:
        collect_agent_artifacts(
            run_dir=run_dir, node_id="coder", iteration=1,
            scan_root=scan_root, collect_specs=SPECS[1:], exclude_extra=())
    assert "notes/a.b" in str(exc_info.value)
    assert "notes/a-b" in str(exc_info.value)


def test_collect_rejects_symlink_inside_scan_root(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    scan_root = tmp_path / "cwd"
    (scan_root / "patches").mkdir(parents=True)
    (scan_root / "patches" / "ok.patch").write_text("ok", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    try:
        os.symlink(outside, scan_root / "patches" / "leak.patch")
    except OSError:
        pytest.skip("当前账户无 symlink 权限")
    with pytest.raises(Exception, match="符号链接"):
        collect_agent_artifacts(
            run_dir=run_dir, node_id="coder", iteration=1,
            scan_root=scan_root, collect_specs=SPECS[:1], exclude_extra=())


# ─────────────────── 端到端:注入 runner(研究节点,实报 cwd) ───────────────────


class CollectingStubRunner:
    """研究节点注入替身:自带 collect 配置,把文件写进自己的执行目录,
    并按 local_cli 的合同在结果对象上实报 cwd。"""

    production_runner = False
    runner_name = "stub_collect"

    def __init__(self, config: AgentRunnerConfig, seed_files: dict):
        self.config = config
        self.seed_files = seed_files

    def __call__(self, attachment, *, node_type, max_turns, cwd=None, **kw):
        exec_dir = Path_type(attachment).parent / "exec-dir"
        exec_dir.mkdir(parents=True, exist_ok=True)
        for rel, content in self.seed_files.items():
            target = exec_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        from atlas.adapters import Usage
        from atlas.nodes.local_cli import AgentRunResult
        return AgentRunResult(text="调研报告正文", usage=Usage(input_tokens=1),
                              cost_usd=None, runner="stub_collect",
                              cwd=exec_dir)


def Path_type(p):
    from pathlib import Path
    return Path(p)


def _research_spec():
    return spec_from_yaml("""
name: e2b-collect
nodes:
  - id: scout
    type: research
    model: Fake:primary
    prompt: 调研并产出补丁与笔记。
    consumes: [task]
  - id: reviewer
    type: llm
    model: Fake:primary
    prompt: 审阅收集到的补丁。
    consumes: [task, scout.output, patch.patches-fix1]
edges:
  - {from: scout, to: reviewer}
  - {from: reviewer, to: END}
""")


def test_collect_flows_through_node_done_and_downstream(tmp_path):
    config = AgentRunnerConfig(
        runner="local_cli",
        collect=(AgentCollectSpec(pattern="patches/*.patch",
                                  name_prefix="patch", role="output",
                                  ext=".patch"),))
    runner = CollectingStubRunner(config, {
        "patches/fix1.patch": "fix1 diff body",
        "patches/fix2.patch": "fix2 diff body",
        "notes/readme.md": "readme",
    })
    fake = FakeProvider()
    fake.configure("primary", text=json.dumps({"verdict": "pass"}))
    result = execute_graph(
        _research_spec(), task="任务", runs_root=tmp_path,
        registry=make_registry(fake), agent_runner=runner)
    assert result.status == "done"
    events = result.events.all()
    done = next(e for e in events if e["type"] == "node_done"
                and e["node"] == "scout")
    names = [a["name"] for a in done["artifacts"]]
    # report 在前,collect 条目按相对路径字典序追加在尾部
    assert names == ["scout.output", "patch.patches-fix1", "patch.patches-fix2"]
    patch_entry = next(a for a in done["artifacts"]
                       if a["name"] == "patch.patches-fix1")
    assert patch_entry["role"] == "output"
    assert patch_entry["metadata"]["collected_from"] == "patches/fix1.patch"
    assert patch_entry["media_type"] == "text/x-diff"
    # 下游消费 collect 名:patch 正文按产物语义内联(定向审批的材料)
    reviewer_projection = next(e["projection_path"] for e in events
                               if e["type"] == "node_input"
                               and e["node"] == "reviewer")
    assert b"fix1 diff body" in open(reviewer_projection, "rb").read()
    # fold 重放含 collect 产物(node_done 携带,与附件不同)
    folded = result.folded()
    assert folded["artifacts"]["patch.patches-fix1"]["sha256"] \
        == patch_entry["sha256"]


def test_collect_skipped_loudly_when_runner_reports_no_cwd(tmp_path):
    class NoCwdStub(CollectingStubRunner):
        def __call__(self, attachment, *, node_type, max_turns, cwd=None, **kw):
            return "调研报告正文"   # str 结果:无 cwd、无 usage

    config = AgentRunnerConfig(collect=(AgentCollectSpec(
        pattern="**", name_prefix="all"),))
    runner = NoCwdStub(config, {})
    fake = FakeProvider()
    fake.configure("primary", text="调研报告正文")
    result = execute_graph(
        spec_from_yaml("""
name: e2b-skip
nodes:
  - id: scout
    type: research
    model: Fake:primary
    prompt: 调研。
    consumes: [task]
edges:
  - {from: scout, to: END}
"""), task="任务", runs_root=tmp_path, registry=make_registry(fake),
        agent_runner=runner)
    assert result.status == "done"
    progress = [e for e in result.events.all()
                if e["type"] == "node_progress"
                and e.get("phase") == "collect_skipped"]
    assert len(progress) == 1
    done = next(e for e in result.events.all()
                if e["type"] == "node_done")
    assert len(done["artifacts"]) == 1   # 只有报告,没有假装的收集


# ─────────────────── coding_agent worktree 扫描路径 ───────────────────


def test_collect_scans_worktree_for_coding_agent(tmp_path):
    """非可写 coding_agent:worktree 从冻结 baseline 派生(无 git 要求);
    注入 runner 返回 str(无 cwd)→ 扫描根回退到 worktree。"""
    from pathlib import Path as _P
    workdir = tmp_path / "project"
    (workdir / "patches").mkdir(parents=True)
    (workdir / "patches" / "base.patch").write_text("base", encoding="utf-8")

    class WorktreeAwareStub:
        production_runner = False
        runner_name = "stub_wt"

        def __init__(self, config):
            self.config = config

        def __call__(self, attachment, *, node_type, max_turns, cwd=None, **kw):
            # 在 worktree 里写一个新补丁(非可写 agent 用工具生成文件的场景)
            target = _P(cwd) / "patches" / "new.patch"
            target.write_text("new patch", encoding="utf-8")
            return "完成报告"

    config = AgentRunnerConfig(collect=(AgentCollectSpec(
        pattern="patches/*.patch", name_prefix="patch", role="report",
        ext=".patch"),))
    result = execute_graph(
        spec_from_yaml(f"""
name: e2b-worktree
nodes:
  - id: coder
    type: coding_agent
    model: Fake:primary
    workdir: {workdir.as_posix()}
    writable: false
    prompt: 产出补丁文件。
    consumes: [task]
edges:
  - {{from: coder, to: END}}
"""), task="任务", runs_root=tmp_path,
        registry=make_registry(FakeProvider()), agent_runner=WorktreeAwareStub(config))
    assert result.status == "done"
    done = next(e for e in result.events.all()
                if e["type"] == "node_done" and e["node"] == "coder")
    names = [a["name"] for a in done["artifacts"]]
    # baseline 里就有的 base.patch 与新写的 new.patch 都被收集
    assert names == ["coder.output", "patch.patches-base", "patch.patches-new"]
    assert done["artifacts"][1]["role"] == "report"
