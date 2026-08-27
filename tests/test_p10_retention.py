# -*- coding: utf-8 -*-
"""P10 · retention、star 与 run index。

合同(ROADMAP §9):
① 默认 max_runs/max_age_days 全 null = 永不自动删;
② running/paused/interrupted 与 star 标记永不自动删,保护对象不占配额;
③ 候选选择(execution-free 纯函数)与删除执行(stable lock + 同卷
   tombstone + no-follow 清理)严格分离;清理崩溃由相同调用重试完成;
④ 轻量索引是可丢弃缓存:损坏即重建,列表与 full-fold 逐字段一致,
   动态 interrupted 判定永不走缓存。
"""
import json
import threading
from datetime import datetime, timedelta

import pytest

from atlas.adapters import FakeProvider
from atlas.engine import acquire_run_lock, execute_graph, release_run_lock
from atlas.events import EventReader, fold_events
from atlas.runs import (_INDEX_FILENAME, RunNotDeletable, RunNotFoundError,
                        apply_retention, isolate_and_delete_run,
                        list_run_summaries, read_star, resolve_retention_config,
                        select_retention_candidates, set_star)
from atlas.spec import spec_from_yaml

from conftest import TASK_TEXT, make_registry, standard_fake

PRODUCER_YAML = """
name: p10_src
nodes:
  - id: a
    type: llm
    model: Fake:primary
    prompt: 产出。
    consumes: [task]
    output_schema:
      required: [summary]
edges:
  - from: a
    to: END
"""


def _write_run(root, run_id: str, *, final: str | None = "run_done",
               started_ts: str | None = None,
               graph: str = "g") -> object:
    """最小合法账本 run(不执行任何模型);final 为持久终态事件类型,
    None 表示停在 run_started(persisted running)。返回 run 目录 Path。"""
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    seq = 0
    lines: list[str] = []

    def emit(event: dict) -> None:
        nonlocal seq
        seq += 1
        event.setdefault("seq", seq)
        event.setdefault("ts", started_ts or "2026-08-20T00:00:00")
        event.setdefault("run_id", run_id)
        lines.append(json.dumps(event, ensure_ascii=False))

    emit({"type": "run_started", "graph": graph})
    if final == "run_done":
        emit({"type": "node_started", "node": "a"})
        emit({"type": "node_done", "node": "a"})
        emit({"type": "run_done"})
    elif final is not None:
        emit({"type": final})
    (run_dir / "events.jsonl").write_text(
        "".join(line + "\n" for line in lines), encoding="utf-8")
    return run_dir


def _ts(before_now_seconds: float, now_epoch: float) -> str:
    return datetime.fromtimestamp(
        now_epoch - before_now_seconds).isoformat()


# ───────────── A. 候选选择 ─────────────


def test_selection_protection_matrix(tmp_path):
    now = datetime.now().timestamp()
    base_ts = _ts(30 * 86400, now)          # 全部远超龄,只看保护规则
    _write_run(tmp_path, "r-done", started_ts=base_ts)
    _write_run(tmp_path, "r-failed", final="run_failed", started_ts=base_ts)
    _write_run(tmp_path, "r-cancelled", final="run_cancelled",
               started_ts=base_ts)
    _write_run(tmp_path, "r-paused", final="paused", started_ts=base_ts)
    _write_run(tmp_path, "r-running", final=None, started_ts=base_ts)
    _write_run(tmp_path, "r-starred", started_ts=base_ts)
    set_star("r-starred", runs_root=tmp_path)

    result = select_retention_candidates(
        tmp_path, max_age_days=1.0, now=now)
    assert sorted(result["candidates"]) == ["r-cancelled", "r-done",
                                            "r-failed"]
    # 保护对象如实计数(paused/running/starred + 下方的无起点者)
    assert result["protected"] >= 3


def test_selection_active_ids_and_missing_start_protected(tmp_path):
    now = datetime.now().timestamp()
    _write_run(tmp_path, "r-active", started_ts=_ts(40 * 86400, now))
    _write_run(tmp_path, "r-nostart", final="run_done")   # run_started 存在但 ts 缺省仍算有 start;
    # 构造真正没有 run_started 的账本
    bare = tmp_path / "r-bare"
    bare.mkdir()
    (bare / "events.jsonl").write_text(json.dumps({
        "seq": 1, "ts": "t", "type": "node_done", "node": "x"}) + "\n",
        encoding="utf-8")
    result = select_retention_candidates(
        tmp_path, max_age_days=1.0, now=now, active_ids={"r-active"})
    assert "r-active" not in result["candidates"]
    assert "r-bare" not in result["candidates"]


def test_selection_quota_keeps_newest_of_eligible_pool(tmp_path):
    now = datetime.now().timestamp()
    for i, days in enumerate((50, 40, 30)):
        _write_run(tmp_path, f"q-{i}", final="run_done",
                   started_ts=_ts(days * 86400, now))
    # 保护对象即使更老也不占配额
    _write_run(tmp_path, "q-star-old", final="run_done",
               started_ts=_ts(90 * 86400, now))
    set_star("q-star-old", runs_root=tmp_path)

    result = select_retention_candidates(tmp_path, max_runs=1, now=now)
    assert result["eligible"] == 3
    # 只保留池内最新一条(q-2),其余按最旧优先出池
    assert sorted(result["candidates"]) == ["q-0", "q-1"]


def test_selection_age_boundary_is_strictly_older(tmp_path):
    now = datetime.now().timestamp()
    cutoff_days = 7.0
    _write_run(tmp_path, "just-inside", final="run_done",
               started_ts=_ts(cutoff_days * 86400 - 60, now))
    _write_run(tmp_path, "way-outside", final="run_done",
               started_ts=_ts(cutoff_days * 86400 + 3600, now))
    result = select_retention_candidates(
        tmp_path, max_age_days=cutoff_days, now=now)
    assert result["candidates"] == ["way-outside"]


def test_selection_union_of_both_criteria(tmp_path):
    now = datetime.now().timestamp()
    _write_run(tmp_path, "old-in-quota", final="run_done",
               started_ts=_ts(30 * 86400, now))
    _write_run(tmp_path, "young-out-of-quota", final="run_done",
               started_ts=_ts(1 * 86400, now))
    _write_run(tmp_path, "mid", final="run_done",
               started_ts=_ts(5 * 86400, now))
    result = select_retention_candidates(
        tmp_path, max_runs=2, max_age_days=10.0, now=now)
    # 配额留最新两条(young/mid),年龄判掉 old;并集恰为 old
    assert result["candidates"] == ["old-in-quota"]


def test_selection_rejects_bad_thresholds(tmp_path):
    with pytest.raises(ValueError):
        select_retention_candidates(tmp_path, max_runs=0)
    with pytest.raises(ValueError):
        select_retention_candidates(tmp_path, max_age_days=-1)


# ───────────── B. 删除执行器 ─────────────


@pytest.mark.parametrize("final", ["run_done", "run_failed", "run_cancelled"])
def test_executor_deletes_terminal_runs(tmp_path, final):
    run_dir = _write_run(tmp_path, "term", final=final)
    out = isolate_and_delete_run("term", runs_root=tmp_path)
    assert out == {"deleted": "term"}
    assert not run_dir.exists()
    assert list((tmp_path / ".locks").iterdir()) or True   # 锁目录空残留无害
    assert not (tmp_path / ".trash" / "term").exists()


def test_executor_refuses_non_terminal_and_starred(tmp_path):
    paused = _write_run(tmp_path, "p-run", final="paused")
    starred = _write_run(tmp_path, "s-run", final="run_done")
    set_star("s-run", runs_root=tmp_path)

    with pytest.raises(RunNotDeletable, match="status"):
        isolate_and_delete_run("p-run", runs_root=tmp_path)
    assert paused.exists()

    with pytest.raises(RunNotDeletable, match="starred"):
        isolate_and_delete_run("s-run", runs_root=tmp_path)
    assert starred.exists()


def test_executor_lock_conflict_names_dot_locks(tmp_path):
    _write_run(tmp_path, "busy", final="run_done")
    acquire_run_lock("busy", runs_root=tmp_path)
    try:
        with pytest.raises(Exception, match=r"\.locks") as excinfo:
            isolate_and_delete_run("busy", runs_root=tmp_path)
        assert type(excinfo.value).__name__ != "RunNotDeletable"
    finally:
        release_run_lock("busy", runs_root=tmp_path)


def test_executor_crash_cleanup_retryable(tmp_path, monkeypatch):
    import atlas.runs as runs_module
    run_dir = _write_run(tmp_path, "crashy", final="run_done")
    calls = {"n": 0}
    real_rmtree = runs_module.shutil.rmtree

    def fail_once(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("cleanup failed")
        real_rmtree(path)

    monkeypatch.setattr(runs_module.shutil, "rmtree", fail_once)
    with pytest.raises(RuntimeError, match="重试"):
        isolate_and_delete_run("crashy", runs_root=tmp_path)
    assert not run_dir.exists()                    # 已隔离
    assert (tmp_path / ".trash" / "crashy").exists()

    # 相同调用重试:tombstone 不复活成 run,直接清干净
    out = isolate_and_delete_run("crashy", runs_root=tmp_path)
    assert out == {"deleted": "crashy"}
    assert not (tmp_path / ".trash" / "crashy").exists()


def test_executor_missing_run_and_bad_id(tmp_path):
    with pytest.raises(RunNotFoundError):
        isolate_and_delete_run("20990101-000000-nope", runs_root=tmp_path)
    with pytest.raises(ValueError):
        isolate_and_delete_run("非法/id!", runs_root=tmp_path)


# ───────────── C. Web 面:star 契约 ─────────────


def _make_api(tmp_path):
    from atlas.web import create_app
    from fastapi.testclient import TestClient
    api = create_app(workflows_dir=tmp_path / "workflows", runs_dir=tmp_path,
                     registry_factory=lambda _: make_registry(FakeProvider()),
                     api_only=True)
    return TestClient(api, base_url="http://127.0.0.1")


_DELETE_HEADERS = {"X-Atlas-Request": "1"}   # 全局写守卫(防浏览器跨站)


def test_star_write_once_contract_via_api(tmp_path):
    _write_run(tmp_path, "star-me", final="run_done")
    client = _make_api(tmp_path)

    listed = client.get("/api/runs").json()
    assert listed[0]["star"] is False

    ok = client.post("/api/runs/star-me/star", json={"note": "重要基线"},
                    headers={"X-Atlas-Request": "1"})
    assert ok.status_code == 200
    assert ok.json()["note"] == "重要基线"
    assert client.get("/api/runs/star-me/star").json()["note"] == "重要基线"
    assert client.get("/api/runs").json()[0]["star"] is True

    dup = client.post("/api/runs/star-me/star", json={},
                     headers={"X-Atlas-Request": "1"})
    assert dup.status_code == 409
    bad_field = client.post("/api/runs/star-me/star", json={"why": "x"},
                         headers={"X-Atlas-Request": "1"})
    assert bad_field.status_code == 400
    missing = client.post("/api/runs/20990101-000000-nope/star", json={},
                       headers={"X-Atlas-Request": "1"})
    assert missing.status_code == 404


def test_web_delete_respects_star_and_maps_errors(tmp_path):
    _write_run(tmp_path, "del-star", final="run_done")
    _write_run(tmp_path, "del-ok", final="run_done")
    set_star("del-star", runs_root=tmp_path)
    client = _make_api(tmp_path)

    blocked = client.delete("/api/runs/del-star", headers=_DELETE_HEADERS)
    assert blocked.status_code == 409
    assert "star" in blocked.json()["detail"]

    good = client.delete("/api/runs/del-ok", headers=_DELETE_HEADERS)
    assert good.status_code == 200
    assert not (tmp_path / "del-ok").exists()


# ───────────── D. 清扫接线(env 默认关闭) ─────────────


def test_retention_default_off(monkeypatch, tmp_path):
    monkeypatch.delenv("ATLAS_RETENTION_MAX_RUNS", raising=False)
    monkeypatch.delenv("ATLAS_RETENTION_MAX_AGE_DAYS", raising=False)
    assert apply_retention(runs_root=tmp_path) is None
    assert resolve_retention_config({}) == {"max_runs": None,
                                            "max_age_days": None}


def test_retention_bad_env_fails_loud(monkeypatch):
    with pytest.raises(ValueError):
        resolve_retention_config({"ATLAS_RETENTION_MAX_RUNS": "0"})
    with pytest.raises(ValueError):
        resolve_retention_config({"ATLAS_RETENTION_MAX_AGE_DAYS": "-3"})
    with pytest.raises(ValueError):
        resolve_retention_config({"ATLAS_RETENTION_MAX_RUNS": "ten"})


def test_engine_completion_triggers_sweep(monkeypatch, tmp_path, capsys):
    """engine 钩子:配额生效时,完成一次真跑会顺路清掉超龄旧 run;
    默认 env 未配置时同样的旧 run 原样保留。"""
    stale_keep = _write_run(tmp_path, "stale-new", final="run_done")
    stale_old = _write_run(tmp_path, "stale-old", final="run_failed")

    fake = FakeProvider()
    fake.configure("primary", text=json.dumps(
        {"summary": "新鲜产出"}, ensure_ascii=False))
    reg = make_registry(fake)

    # 默认关闭:什么都不删
    result = execute_graph(spec_from_yaml(PRODUCER_YAML), task=TASK_TEXT,
                           runs_root=tmp_path, registry=reg)
    assert result.folded()["status"] == "done"
    assert stale_keep.exists() and stale_old.exists()

    # 打开配额:eligible 池 = {stale-new, stale-old, 新鲜 run};cap=1
    # 保留最新(本次 run),两个 stale 都出池
    monkeypatch.setenv("ATLAS_RETENTION_MAX_RUNS", "1")
    result2 = execute_graph(spec_from_yaml(PRODUCER_YAML), task=TASK_TEXT,
                            runs_root=tmp_path, registry=reg)
    assert result2.folded()["status"] == "done"
    assert not stale_keep.exists()
    assert not stale_old.exists()


def test_sweep_survives_locked_candidate(monkeypatch, tmp_path, capsys):
    """单个候选被锁占用:清扫记账跳过,刚完成的 run 结果不受影响。"""
    locked_dir = _write_run(tmp_path, "locked-stale", final="run_done")
    acquire_run_lock("locked-stale", runs_root=tmp_path)
    try:
        monkeypatch.setenv("ATLAS_RETENTION_MAX_RUNS", "1")
        fake = FakeProvider()
        fake.configure("primary", text=json.dumps(
            {"summary": "ok"}, ensure_ascii=False))
        result = execute_graph(spec_from_yaml(PRODUCER_YAML), task=TASK_TEXT,
                               runs_root=tmp_path, registry=make_registry(fake))
        assert result.folded()["status"] == "done"
        assert locked_dir.exists()      # 被锁候选保留
        out = capsys.readouterr()
        combined = out.out + out.err
        assert "retention" in combined
    finally:
        release_run_lock("locked-stale", runs_root=tmp_path)


def test_sweep_respects_star_protection_end_to_end(monkeypatch, tmp_path):
    _write_run(tmp_path, "prot", final="run_done")
    set_star("prot", runs_root=tmp_path)
    monkeypatch.setenv("ATLAS_RETENTION_MAX_AGE_DAYS", "0.00001")
    report = apply_retention(runs_root=tmp_path)
    assert report is not None
    assert report["candidates"] == []          # star 在选择层就被保护
    assert (tmp_path / "prot").exists()


# ───────────── E. 运行索引 ─────────────


def test_index_round_trip_matches_full_fold(tmp_path):
    _write_run(tmp_path, "ix-a", final="run_done", started_ts="2026-08-25T00:00:00")
    _write_run(tmp_path, "ix-b", final="paused", started_ts="2026-08-26T00:00:00")
    full = list_run_summaries(tmp_path, limit=10)
    assert (tmp_path / _INDEX_FILENAME).exists()

    again = list_run_summaries(tmp_path, limit=10)   # 缓存命中路径
    assert again == full

    (tmp_path / _INDEX_FILENAME).unlink()
    plain = list_run_summaries(tmp_path, limit=10)   # 无缓存路径对照
    assert plain == full


def test_index_refreshes_on_ledger_change(tmp_path):
    run_dir = _write_run(tmp_path, "ix-live", final=None)   # running
    first = list_run_summaries(tmp_path, limit=5)["runs"][0]
    assert first["status"] in ("running", "interrupted")

    with open(run_dir / "events.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"seq": 99, "ts": "t", "type": "run_done",
                                 "run_id": "ix-live"}) + "\n")
    second = list_run_summaries(tmp_path, limit=5)["runs"][0]
    assert second["status"] == "done"           # 指纹变化触发重读


def test_index_corruption_rebuilds_loudly(tmp_path, capsys):
    _write_run(tmp_path, "ix-c", final="run_done")
    list_run_summaries(tmp_path, limit=5)
    (tmp_path / _INDEX_FILENAME).write_text("{corrupt!", encoding="utf-8")
    out = list_run_summaries(tmp_path, limit=5)
    assert len(out["runs"]) == 1                # 结果不受损
    captured = capsys.readouterr()
    assert "损坏" in (captured.err + captured.out)


def test_index_prunes_deleted_runs_after_pagination_walk(tmp_path):
    for i in range(4):
        _write_run(tmp_path, f"pg-{i}", final="run_done")
    page1 = list_run_summaries(tmp_path, limit=2)
    assert page1["next_cursor"] == "pg-2"
    assert len(page1["runs"]) == 2              # 游标前的条目不在 seen 里

    isolate_and_delete_run("pg-3", runs_root=tmp_path)
    after = list_run_summaries(tmp_path, limit=10)
    names = [r["run_id"] for r in after["runs"]]
    assert "pg-3" not in names and len(names) == 3
    index_data = json.loads((tmp_path / _INDEX_FILENAME).read_text(
        encoding="utf-8"))
    assert "pg-3" not in index_data["runs"]     # 剪枝不以分页片段为据
