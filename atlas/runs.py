# -*- coding: utf-8 -*-
"""Run lifecycle views derived from the ledger plus live controller/lock evidence."""
import json
import os
import shutil
import sys
import time
from pathlib import Path

from atlas.costs import fold_cost_accounting
from atlas.engine import (RunConflictError, RunNotFoundError,
                          acquire_run_lock, release_run_lock)
from atlas.spec import SpecError
from atlas.events import EventReader, fold_events
from atlas.spec import spec_from_snapshot, spec_to_snapshot


def _resolve_live_status(persisted: str, *, run_id: str, runs_root: Path,
                         active_controller: bool = False) -> str:
    """从**已算出的持久状态**解析对外可见状态(LIST 索引与 derive 共用):
    非 running 原样透出;持久 running 且无活跃 controller 时锁探针裁决
    interrupted/running(fail-closed 方向与原实现一致)。"""
    if persisted != "running" or active_controller:
        return persisted
    acquired = False
    try:
        acquire_run_lock(run_id, runs_root=Path(runs_root))
        acquired = True
    except (RunConflictError, OSError):
        return "running"
    except Exception:
        return "running"
    finally:
        if acquired:
            release_run_lock(run_id, runs_root=Path(runs_root))
    return "interrupted"


def derive_run_status(records: list[dict], *, run_id: str, runs_root: Path,
                      active_controller: bool = False) -> str:
    """Return the dynamic run status without persisting synthetic events.

    A persisted ``running`` status becomes ``interrupted`` only when the caller
    has no active local controller and the stable per-run OS lock is provably
    free. Held locks and all probe errors fail closed as ``running``.
    """
    return _resolve_live_status(fold_events(records)["status"],
                                run_id=run_id, runs_root=runs_root,
                                active_controller=active_controller)


def _recap_text(path_str, *, cap: int = 200) -> str:
    """产物首段单行回顾(终局视图原料);读头部,不做全量 IO。"""
    if not path_str:
        return "(无输出)"
    try:
        with open(path_str, "rb") as f:
            raw = f.read(cap * 8)
    except OSError:
        return "(产物不可读)"
    text = raw.decode("utf-8", errors="replace").strip()
    cut = text.find("\n\n")
    if cut != -1:
        text = text[:cut]
    text = " ".join(text.split())
    if len(text) > cap:
        text = text[:cap].rstrip() + "…"
    return text or "(空输出)"


def build_finale(events: list[dict], run_dir: Path) -> dict | None:
    """S1 零成本终局视图:纯事件账本派生,无 LLM、无新事件。

    Web 终局卡片与 MCP atlas_get_run 的同源数据。run 未到终态时返回
    None。llm_summary 是 opt-in 总结调用的产物回顾,始终标注
    「LLM 叙述,事实以账本为准」。
    """
    folded = fold_events(events)
    if folded["status"] not in ("done", "failed", "cancelled"):
        return None
    nodes = []
    for e in events:
        if e["type"] != "node_done":
            continue
        nodes.append({
            "node": e["node"],
            "model_used": e.get("model_used"),
            "duration_s": e.get("duration_s"),
            "input_tokens": e.get("input_tokens"),
            "output_tokens": e.get("output_tokens"),
            "cost_usd": e.get("cost_usd"),
            "ts": e.get("ts"),
            "recap": _recap_text(e.get("output_path")),
        })
    llm_summary = None
    written = next((e for e in reversed(events)
                    if e["type"] == "run_summary_written"), None)
    if written is not None:
        llm_summary = {
            "model": written.get("model"),
            "sha256": written.get("sha256"),
            "path": written.get("path"),
            "text": _recap_text(written.get("path"), cap=4000),
            "input_tokens": written.get("input_tokens"),
            "output_tokens": written.get("output_tokens"),
            "cost_usd": written.get("cost_usd"),
            "note": "LLM 叙述,事实以账本为准",
        }
    summary_failed = next((e for e in reversed(events)
                           if e["type"] == "run_summary_failed"), None)
    return {
        "status": folded["status"],
        "started_ts": events[0].get("ts") if events else None,
        "finished_ts": events[-1].get("ts") if events else None,
        "nodes": nodes,
        "llm_summary": llm_summary,
        "llm_summary_error": (
            {"error_type": summary_failed.get("error_type"),
             "error": summary_failed.get("error")}
            if summary_failed is not None else None),
    }


def build_run_summary(run_id: str, *, runs_root: Path) -> dict:
    """单个 run 的完整摘要(P4 起 Web/MCP 共用的领域函数)。

    只读事件账本与规格快照;不存在或账本为空时返回 error 字典。
    """
    path = Path(runs_root) / run_id / "events.jsonl"
    if not path.exists():
        return {"error": f"没有这个运行:{run_id}", "next": "用运行列表查询"}
    events = EventReader(path).all()
    if not events:
        return {"error": f"运行 {run_id} 的账本是空的", "next": "等待 run_started 落账"}
    folded = fold_events(events)
    dynamic_status = derive_run_status(events, run_id=run_id, runs_root=runs_root)
    nodes = {}
    for e in events:
        if e["type"] == "node_done":
            nodes[e["node"]] = {
                "model_used": e["model_used"],
                "degraded": e["degraded"],
                "input_tokens": e.get("input_tokens"),
                "output_tokens": e.get("output_tokens"),
                "duration_s": e.get("duration_s"),
                "output_path": e["output_path"],
            }
        elif e["type"] == "node_failed_soft":
            # P3:内容类失败按 on_error 策略继续/分支——同源展示错误类与
            # 错误产物入口(MCP atlas_get_run 与 Web 共用本函数)
            nodes[e["node"]] = {
                "model_used": None,
                "degraded": False,
                "input_tokens": None,
                "output_tokens": None,
                "duration_s": None,
                "output_path": e.get("output_path"),
                "soft_failed": True,
                "error_class": e.get("error_class"),
                "on_error": e.get("on_error"),
            }
        elif e["type"] == "run_paused":
            nodes.setdefault(e.get("node"), {})["status"] = "等待批准"
    failed = next((e for e in reversed(events) if e["type"] == "run_failed"), None)
    started = next((e for e in events if e["type"] == "run_started"), {})
    effective_workflow = None
    snapshot = Path(runs_root) / run_id / "spec.snapshot.json"
    if snapshot.is_file():
        try:
            effective_workflow = spec_to_snapshot(spec_from_snapshot(
                json.loads(snapshot.read_text(encoding="utf-8")),
                source=str(snapshot)))
        except Exception as e:
            effective_workflow = {"error": f"有效规格快照损坏:{e}"}
    accounting = fold_cost_accounting(events)
    return {
        "run_id": run_id,
        "status": dynamic_status,
        "graph": folded["graph"],
        "nodes_done": folded["nodes_done"],
        "node_details": nodes,
        "totals": {
            "input_tokens": sum(e.get("input_tokens") or 0 for e in events
                                if e["type"] == "node_done"),
            "output_tokens": sum(e.get("output_tokens") or 0 for e in events
                                 if e["type"] == "node_done"),
            "known_actual_cost_usd": accounting.known_actual_usd,
            "accounted_cost_usd": accounting.accounted_usd,
            "actual_cost_unknown_count": accounting.unknown_count,
            "outstanding_reserved_usd": accounting.outstanding_reserved_usd,
            "cost_usd": (accounting.known_actual_usd
                         if accounting.unknown_count == 0 else None),
        },
        "run_dir": str(Path(runs_root) / run_id),
        "base_spec_sha256": started.get("base_spec_sha256"),
        "effective_spec_sha256": (started.get("effective_spec_sha256")
                                  or started.get("spec_sha256")),
        "bindings": started.get("bindings", []),
        "overrides": started.get("overrides", []),
        "effective_workflow": effective_workflow,
        "failed_error": failed.get("error") if failed else None,
        "finale": build_finale(events, Path(runs_root) / run_id),
    }


_INDEX_FILENAME = ".runs-index.json"
_INDEX_VERSION = "p10-index-v1"


def _events_fingerprint(events_path: Path) -> list:
    """账本字节指纹:append-only 契约保证 size+mtime_ns 变化 ⇔ 出现新事件。
    stat 成本 O(1),替代整本重读——这是索引存在的前提。"""
    st = events_path.stat()
    return [st.st_size, st.st_mtime_ns]


def _load_runs_index(root: Path) -> dict:
    """可丢弃缓存读取:缺失/损坏/版本不符一律按空处理(重建由调用方
    完成)。事件仍是唯一真相,索引错顶多多读几次账本,绝不错报。"""
    path = root / _INDEX_FILENAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != _INDEX_VERSION:
            return {}
        runs = data.get("runs")
        return runs if isinstance(runs, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        print(f"[atlas] 运行索引 {_INDEX_FILENAME} 损坏,忽略并重建",
              file=sys.stderr)
        return {}


def _save_runs_index(root: Path, index: dict) -> None:
    """原子写回;失败不影响列表结果(缓存可丢),向 stderr 记账。"""
    try:
        (root / ".trash").mkdir(parents=True, exist_ok=True)
        tmp = root / ".trash" / (_INDEX_FILENAME + ".partial")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"version": _INDEX_VERSION, "runs": index}, handle,
                      ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, root / _INDEX_FILENAME)
    except OSError as exc:
        print(f"[atlas] 运行索引写回失败(仅影响下次列表速度):{exc}",
              file=sys.stderr)


def list_run_summaries(runs_root: Path, *, limit: int = 20,
                       cursor: str | None = None,
                       active_ids: set[str] | None = None) -> dict:
    """按 run_id 降序的稳定分页运行列表(P4:Web 与 atlas_list_runs 共用)。

    cursor 语义:传上一页的 next_cursor,返回严格小于它的后续条目。
    active_ids 是当前进程的活跃 controller 集合(launcher.REGISTRY),
    用于 interrupted 动态判定的 fail-closed 方向修正。

    P10:每个条目的 fold 结果缓存在 .runs-index.json(键=run_id,值带
    events.jsonl 的 size+mtime 指纹);指纹不符/缺失才整本重读。动态
    interrupted 判定永远不走缓存——liveness 探针每次现查。索引是纯
    加速:损坏即重建,列表结果与 full-fold 逐字段一致。
    """
    root = Path(runs_root)
    active = active_ids or set()
    entries: list[tuple[str, dict]] = []
    index = _load_runs_index(root) if root.exists() else {}
    dirty = False
    if root.exists():
        for d in sorted(root.iterdir(), reverse=True):
            name = d.name
            if name in (".locks", ".trash"):
                continue
            if cursor is not None and name >= cursor:
                continue
            events_path = d / "events.jsonl"
            if not events_path.exists():
                continue
            cache = index.get(name)
            graph = persisted = started = None
            nodes_done: list = []
            fp = None
            try:
                fp = _events_fingerprint(events_path)
            except OSError:
                pass   # 竞态窗口(如 retention 正隔离):下轮列表自然收敛
            if fp is not None and isinstance(cache, dict) \
                    and cache.get("fp") == fp \
                    and all(k in cache for k in
                            ("graph", "persisted", "nodes_done", "started")):
                graph = cache["graph"]
                persisted = cache["persisted"]
                nodes_done = list(cache["nodes_done"])
                started = cache["started"]
            else:
                events = EventReader(events_path).all()
                if not events:
                    continue
                folded = fold_events(events)
                graph = folded["graph"]
                persisted = folded["status"]
                nodes_done = folded["nodes_done"]
                started = next((e["ts"] for e in events
                                if e["type"] == "run_started"), None)
                if fp is not None:
                    index[name] = {"fp": fp, "graph": graph,
                                   "persisted": persisted,
                                   "nodes_done": nodes_done,
                                   "started": started}
                    dirty = True
            entries.append((name, {
                "run_id": name,
                "graph": graph,
                "status": _resolve_live_status(
                    persisted, run_id=name, runs_root=root,
                    active_controller=name in active),
                "nodes_done": nodes_done,
                "started": started,
            }))
            if len(entries) >= max(1, limit):
                break
    # 剪枝必须对照 runs_root 的**全量**当前成员(分页的 seen 只覆盖
    # 游标之后的片段,不能作为删除依据)
    live_ids = ({name for name, _ in _scan_run_dirs(root)}
                if root.exists() else set())
    if dirty or set(index) - live_ids:
        for stale in set(index) - live_ids:
            del index[stale]
        _save_runs_index(root, index)
    next_cursor = entries[-1][0] if len(entries) == max(1, limit) else None
    return {
        "runs": [entry for _, entry in entries],
        "next_cursor": next_cursor,
    }


# ─────────────────────────── P7 artifact import ───────────────────────────


_IMPORTABLE_STATUSES = ("done", "failed", "cancelled", "paused")


def _latest_artifact_entry(events: list[dict], logical: str) -> dict | None:
    """倒序找最近一条携带该逻辑名产物的事件(node_done 的 output/diff、
    node_failed_soft 的 error 各归其位);返回统一条目。

    兜底分支必须核对事件的 node 就是该逻辑名的生产者——否则多节点源
    里倒序扫到别的节点的 node_done 会把别人的产物当成目标返回
    (2026-08-27 P13 多节点源 fork 实测逼出;单节点源测试从未踩中)。
    """
    producer = logical.rsplit(".", 1)[0]
    for e in reversed(events):
        for item in e.get("artifacts") or []:
            if isinstance(item, dict) and item.get("name") == logical:
                return item
        if (e.get("type") == "node_done" and e.get("node") == producer
                and logical.endswith(".output")
                and e.get("output_sha256")):
            return {"name": logical, "path": e["output_path"],
                    "sha256": e["output_sha256"]}
    return None


def resolve_imports(*, run_dir: Path, imports_spec, runs_root: Path) -> list[dict]:
    """P7 启动准入:在每个源 run 的 stable lock **全程持锁**下核验并复制。

    两阶段都发生在锁内——先集中校验(存在性/静稳终态/最新 provenance/
    producer invocation),再逐条字节复制+写后复验。"与源删除竞争时锁
    行为确定"由这里保证:锁被其他 controller 占用 → SpecError 当场
    fail-closed(稍后重试),绝不半持有或等待。任何一条失败都让启动
    fail-closed,不存在部分导入后继续跑的运行。

    返回 engine 需要的 lineage 计划列表(含 skip 判定所需的源 invocation)。
    """
    from atlas.artifacts import IMPORT_ALGO_VERSION, copy_imported_artifact
    from atlas.engine import RunNotFoundError
    from atlas.events import EventReader

    held_locks: list[str] = []
    try:
        sources: dict[str, tuple[Path, list[dict]]] = {}
        staged: list[dict] = []
        for imp in imports_spec:
            src_id = imp.run
            if src_id not in sources:
                src_dir = Path(runs_root) / src_id
                if not (src_dir / "events.jsonl").exists():
                    raise RunNotFoundError(
                        f"导入源 {src_id!r} 不存在(没有 events.jsonl)")
                acquire_run_lock(src_id, runs_root=Path(runs_root))
                held_locks.append(src_id)
                events = EventReader(src_dir / "events.jsonl").all()
                status = fold_events(events)["status"]
                if status not in _IMPORTABLE_STATUSES or status == "running":
                    raise SpecError(
                        f"导入源 {src_id!r} 持久状态是 {status!r}:只有静稳"
                        f"终态({', '.join(_IMPORTABLE_STATUSES)} 除 running 外)"
                        f"可作为导入来源;运行中/中断的 run 拒绝引用")
                sources[src_id] = (src_dir, events)
            _, events = sources[src_id]

            entry = _latest_artifact_entry(events, imp.name)
            if entry is None:
                raise SpecError(
                    f"导入源 {src_id!r} 的事件里找不到 {imp.name!r} 的"
                    f"最新产物记录")
            sha256 = entry.get("sha256")
            path_str = entry.get("path")
            if not sha256 or not path_str:
                raise SpecError(
                    f"导入源 {src_id!r} 的 {imp.name!r} 记录缺少 sha256/path")
            producer = imp.name.rsplit(".", 1)[0]
            started = next((e for e in reversed(events)
                            if e.get("type") == "node_started"
                            and e.get("node") == producer), None)
            staged.append({
                "source_run": src_id,
                "source_name": imp.name,
                "source_sha256": sha256,
                "path_str": path_str,
                "producer": producer,
                "algo_version": IMPORT_ALGO_VERSION,
                "source_invocation": (
                    started.get("invocation_sha256") if started else None),
                # E-1:search 产物是外部网页素材,导入后仍须在下游投影里
                # 围栏——untrusted 标记随导入转发,绝不裸内联。
                "untrusted": bool(entry.get("untrusted")),
            })

        results: list[dict] = []
        for plan in staged:   # 校验全部通过,锁仍握着:现在才动字节
            ref = copy_imported_artifact(
                source_path=Path(plan.pop("path_str")),
                source_sha256=plan["source_sha256"],
                run_dir=run_dir, name=plan["source_name"])
            ref_dict = ref.as_dict()
            if plan.pop("untrusted"):
                ref_dict["untrusted"] = True
            results.append({**plan, "ref": ref_dict})
        return results
    finally:
        for src_id in reversed(held_locks):
            release_run_lock(src_id, runs_root=Path(runs_root))


def precheck_imports(*, imports_spec, runs_root: Path) -> None:
    """P7 只读预检:源存在且为静稳终态,否则启动失败且不留 run 目录。

    与 resolve_imports 的完整锁内校验互补:这里只查"值不值得开 run"
    (缺源/running 源立刻拒绝);哈希/provenance/字节复制仍由
    resolve_imports 在源锁内完成——两段之间源可能变化,完整校验兜底。
    """
    from atlas.engine import RunNotFoundError

    seen: set[str] = set()
    for imp in imports_spec:
        if imp.run in seen:
            continue
        seen.add(imp.run)
        events_path = Path(runs_root) / imp.run / "events.jsonl"
        if not events_path.exists():
            raise RunNotFoundError(
                f"导入源 {imp.run!r} 不存在(没有 events.jsonl)")
        status = fold_events(EventReader(events_path).all())["status"]
        if status not in _IMPORTABLE_STATUSES or status == "running":
            raise SpecError(
                f"导入源 {imp.run!r} 持久状态是 {status!r}:只有静稳终态"
                f"({', '.join(_IMPORTABLE_STATUSES)} 除 running 外)可作为"
                f"导入来源;运行中/中断的 run 拒绝引用")


# ─────────────────────────── P10 retention / star / 删除执行 ───────────────────────────

# 与 Web DELETE 端点同一口径:只有这三个持久终态可删;running/paused/
# interrupted 永不自动删(P10 保护名单),star 标记同样挡刀。
_DELETABLE_STATUSES = ("done", "failed", "cancelled")
_STAR_FILENAME = "star.json"
_RETENTION_MAX_RUNS_ENV = "ATLAS_RETENTION_MAX_RUNS"
_RETENTION_MAX_AGE_DAYS_ENV = "ATLAS_RETENTION_MAX_AGE_DAYS"


class RunNotDeletable(Exception):
    """run 因终态不符或 star 标记而拒绝删除;reason ∈ {status, starred}。"""

    def __init__(self, run_id: str, reason: str, detail: str = "") -> None:
        self.run_id = run_id
        self.reason = reason
        super().__init__(
            f"运行 {run_id!r} 不可删除({reason})"
            + (f":{detail}" if detail else ""))


def is_sharing_violation(exc: OSError) -> bool:
    """Windows 文件占用/共享冲突;调用方映射为可重试路径。"""
    return getattr(exc, "winerror", None) in (5, 32, 33)


def rmtree_no_follow(path: Path) -> None:
    """删除 tombstone,但绝不沿 symlink/junction 进入运行目录之外。"""
    if path.is_symlink():
        path.unlink()
        return
    if path.is_junction():
        path.rmdir()
        return
    for root, dirs, _files in os.walk(path, topdown=True, followlinks=False):
        root_path = Path(root)
        for name in list(dirs):
            child = root_path / name
            if child.is_symlink():
                child.unlink()
                dirs.remove(name)
            elif child.is_junction():
                child.rmdir()
                dirs.remove(name)
    shutil.rmtree(path)


def star_marker_path(runs_root: Path, run_id: str) -> Path:
    return Path(runs_root) / run_id / _STAR_FILENAME


def has_star(runs_root: Path, run_id: str) -> bool:
    return star_marker_path(runs_root, run_id).is_file()


def set_star(run_id: str, *, runs_root: Path, note: str = "") -> dict:
    """P10 write-once star 标记:一旦存在拒绝覆写(改注记走删除重建,
    而删除又被 star 挡住——取消 star 是显式手工动作 rm 该文件,不设 API,
    防止自动化误清保护标记)。任何有账本的 run 都可 star(含 running:
    先标记再跑长任务正是典型用法)。"""
    _check_like_run_id(run_id)
    run_dir = Path(runs_root) / run_id
    if not (run_dir / "events.jsonl").is_file():
        raise RunNotFoundError(f"run {run_id!r} 不存在(没有 events.jsonl)")
    marker = star_marker_path(runs_root, run_id)
    if marker.exists():
        raise FileExistsError(f"run {run_id!r} 已有 star 标记(write-once)")
    payload = {"starred_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "note": note}
    tmp = marker.with_suffix(".json.partial")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, marker)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return payload


def read_star(run_id: str, *, runs_root: Path) -> dict | None:
    marker = star_marker_path(runs_root, run_id)
    if not marker.is_file():
        return None
    try:
        return json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        # 标记文件存在即保护成立;内容损坏如实报缺失但不放行删除
        return {"corrupt": True}


def isolate_and_delete_run(run_id: str, *, runs_root: Path) -> dict:
    """P10 共享删除执行器(Web DELETE 端点与 retention sweep 同一实现)。

    合同:持权威 stable lock → 同卷 replace 进 .trash tombstone →
    no-follow 清理;tombstone 残留由相同调用重试完成,绝不复活成 run;
    非 {done,failed,cancelled} 终态或带 star 标记的 run 在锁内拒删。
    运行中锁被占 → RunConflictError 当场失败(retention 不等待)。
    """
    _check_like_run_id(run_id)
    runs_root = Path(runs_root)
    run_dir = runs_root / run_id
    trash_dir = runs_root / ".trash"
    tombstone = trash_dir / run_id
    try:
        acquire_run_lock(run_id, runs_root=runs_root)
    except RunConflictError as exc:
        raise RunConflictError(
            f"运行 {run_id!r} 正被其他操作占用(.locks),请稍后重试") from exc
    try:
        # 清理失败留下的 tombstone 可由相同调用重试,但绝不恢复成 run。
        if tombstone.exists():
            if run_dir.exists():
                raise RuntimeError(
                    f"运行 {run_id!r} 同时存在活动目录和删除 tombstone")
        else:
            events_path = run_dir / "events.jsonl"
            if not events_path.is_file():
                raise RunNotFoundError(f"没有这个运行:{run_id}")
            records = EventReader(events_path).all()
            if not records:
                raise RunNotFoundError(f"没有这个运行:{run_id}")
            status = fold_events(records)["status"]
            if status not in _DELETABLE_STATUSES:
                raise RunNotDeletable(
                    run_id, "status",
                    f"当前状态 {status!r};只有 done/failed/cancelled 可以删除")
            if has_star(runs_root, run_id):
                raise RunNotDeletable(run_id, "starred", "star 标记的运行受保护")
            trash_dir.mkdir(parents=True, exist_ok=True)
            try:
                run_dir.replace(tombstone)
            except OSError as exc:
                if is_sharing_violation(exc):
                    raise RunConflictError(
                        f"运行 {run_id!r} 的文件仍被占用,请稍后重试") from exc
                raise RuntimeError(
                    f"隔离运行 {run_id!r} 失败:{exc}") from exc
        try:
            rmtree_no_follow(tombstone)
        except OSError as exc:
            if is_sharing_violation(exc):
                raise RunConflictError(
                    f"运行 {run_id!r} 已隔离但文件仍被占用,可重试删除") from exc
            raise RuntimeError(
                f"运行 {run_id!r} 已隔离但清理失败,可重试:{exc}") from exc
        return {"deleted": run_id}
    finally:
        release_run_lock(run_id, runs_root=runs_root)


def _check_like_run_id(run_id: str) -> None:
    from atlas.spec import _NODE_ID_RE   # 同一字符白名单复用
    if not isinstance(run_id, str) or not _NODE_ID_RE.match(run_id):
        raise ValueError(f"非法 run id:{run_id!r}")


def _scan_run_dirs(runs_root: Path) -> list[tuple[str, Path]]:
    root = Path(runs_root)
    out: list[tuple[str, Path]] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue           # .locks / .trash 等治理目录不参与
        if (entry / "events.jsonl").is_file():
            out.append((entry.name, entry))
    return out


def select_retention_candidates(runs_root: Path, *, max_runs: int | None = None,
                                max_age_days: float | None = None,
                                now: float | None = None,
                                active_ids=()) -> dict:
    """P10 候选选择(纯函数,不动磁盘状态):在可删除池里按"最旧优先"
    挑出超龄者与超出配额者并集。

    决策确定性合同:
    - 只有持久 fold 为 {done,failed,cancelled} 且无 star、不在
      active_ids 的 run 进入可删除池(running/paused/interrupted/starred
      全部保护);
    - max_runs 是**可删除池内保留的最新 N 条**,保护对象不占配额;
    - max_age_days 按 started 时间戳相对 now 计算(缺 started 的 run
      无从判龄,保守归入保护侧——绝不凭空猜年龄);
    - 两个阈值都给时取两者并集,各自独立判定。
    """
    if max_runs is not None and max_runs < 1:
        raise ValueError("max_runs 必须 ≥1")
    if max_age_days is not None and max_age_days <= 0:
        raise ValueError("max_age_days 必须是正数")
    now = time.time() if now is None else now
    active = set(active_ids)

    eligible: list[dict] = []          # 可删除池,按 started 升序
    protected_count = 0
    for run_id, run_dir in _scan_run_dirs(runs_root):
        records = EventReader(run_dir / "events.jsonl").all()
        folded = fold_events(records)
        started = next((e["ts"] for e in records
                        if e.get("type") == "run_started"), None)
        starred = has_star(runs_root, run_id)
        deletable_status = folded["status"] in _DELETABLE_STATUSES
        if (not deletable_status or starred or run_id in active
                or started is None):
            protected_count += 1
            continue
        eligible.append({"run_id": run_id, "started": started})

    eligible.sort(key=lambda item: item["started"])
    by_age: set[str] = set()
    if max_age_days is not None:
        threshold = now - max_age_days * 86400.0
        for item in eligible:
            if _parse_ts(item["started"]) < threshold:
                by_age.add(item["run_id"])
    over_quota: set[str] = set()
    if max_runs is not None and len(eligible) > max_runs:
        evicted = eligible[: len(eligible) - max_runs]   # 最旧的先出池
        over_quota = {item["run_id"] for item in evicted}
    candidates = [item["run_id"] for item in eligible
                  if item["run_id"] in by_age or item["run_id"] in over_quota]
    return {"candidates": candidates, "eligible": len(eligible),
            "protected": protected_count}


def _parse_ts(ts: str) -> float | None:
    import datetime as _dt
    try:
        parsed = _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def resolve_retention_config(environ=None) -> dict:
    """环境变量解析(ATLAS_RETENTION_MAX_RUNS / ATLAS_RETENTION_MAX_AGE_DAYS)。
    默认全 null = 特性关闭,永不自动删;显式配了才生效。坏值大声失败。"""
    environ = os.environ if environ is None else environ

    def _int_env(name: str) -> int | None:
        raw = environ.get(name)
        if raw is None or raw.strip() == "":
            return None
        value = int(raw)          # ValueError 向上抛,坏配置不当静默
        if value < 1:
            raise ValueError(f"{name} 必须 ≥1,得到 {value}")
        return value

    def _float_env(name: str) -> float | None:
        raw = environ.get(name)
        if raw is None or raw.strip() == "":
            return None
        value = float(raw)        # ValueError 向上抛
        if value <= 0:
            raise ValueError(f"{name} 必须是正数,得到 {value}")
        return value

    return {"max_runs": _int_env(_RETENTION_MAX_RUNS_ENV),
            "max_age_days": _float_env(_RETENTION_MAX_AGE_DAYS_ENV)}


def apply_retention(*, runs_root: Path, active_ids=(), environ=None) -> dict | None:
    """P10 清扫入口:解析 env 配置 → 选候选 → 逐个调共享执行器。

    未配置(max_runs 与 max_age_days 全空)→ 直接返回 None(永不自动删,
    这是默认)。单个候选删除失败只记账不中断本轮(下一轮重试),全部结果
    如实回传给调用方记录——不吞异常,但也不让清理失败毁掉已完成的 run。
    """
    config = resolve_retention_config(environ)
    if config["max_runs"] is None and config["max_age_days"] is None:
        return None
    selection = select_retention_candidates(
        runs_root, max_runs=config["max_runs"],
        max_age_days=config["max_age_days"], active_ids=active_ids)
    results: list[dict] = []
    for run_id in selection["candidates"]:
        try:
            isolate_and_delete_run(run_id, runs_root=runs_root)
            results.append({"run_id": run_id, "deleted": True})
        except (RunConflictError, RunNotDeletable, RunNotFoundError,
                OSError, RuntimeError) as exc:
            results.append({"run_id": run_id, "deleted": False,
                            "error": f"{type(exc).__name__}: {exc}"})
    return {**selection, "results": results,
            "config": {k: v for k, v in config.items()}}
