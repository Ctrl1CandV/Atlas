# -*- coding: utf-8 -*-
"""Run lifecycle views derived from the ledger plus live controller/lock evidence."""
import json
from pathlib import Path

from atlas.costs import fold_cost_accounting
from atlas.engine import (RunConflictError, acquire_run_lock,
                          release_run_lock)
from atlas.spec import SpecError
from atlas.events import EventReader, fold_events
from atlas.spec import spec_from_snapshot, spec_to_snapshot


def derive_run_status(records: list[dict], *, run_id: str, runs_root: Path,
                      active_controller: bool = False) -> str:
    """Return the dynamic run status without persisting synthetic events.

    A persisted ``running`` status becomes ``interrupted`` only when the caller
    has no active local controller and the stable per-run OS lock is provably
    free. Held locks and all probe errors fail closed as ``running``.
    """
    persisted = fold_events(records)["status"]
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


def list_run_summaries(runs_root: Path, *, limit: int = 20,
                       cursor: str | None = None,
                       active_ids: set[str] | None = None) -> dict:
    """按 run_id 降序的稳定分页运行列表(P4:Web 与 atlas_list_runs 共用)。

    cursor 语义:传上一页的 next_cursor,返回严格小于它的后续条目。
    active_ids 是当前进程的活跃 controller 集合(launcher.REGISTRY),
    用于 interrupted 动态判定的 fail-closed 方向修正。
    """
    root = Path(runs_root)
    active = active_ids or set()
    entries: list[tuple[str, dict]] = []
    if root.exists():
        for d in sorted(root.iterdir(), reverse=True):
            name = d.name
            if name in (".locks", ".trash"):
                continue
            if cursor is not None and name >= cursor:
                continue
            if not (d / "events.jsonl").exists():
                continue
            events = EventReader(d / "events.jsonl").all()
            if not events:
                continue
            folded = fold_events(events)
            status = derive_run_status(
                events, run_id=name, runs_root=root,
                active_controller=name in active)
            entries.append((name, {
                "run_id": name,
                "graph": folded["graph"],
                "status": status,
                "nodes_done": folded["nodes_done"],
                "started": next((e["ts"] for e in events
                                 if e["type"] == "run_started"), None),
            }))
            if len(entries) >= max(1, limit):
                break
    next_cursor = entries[-1][0] if len(entries) == max(1, limit) else None
    return {
        "runs": [entry for _, entry in entries],
        "next_cursor": next_cursor,
    }


# ─────────────────────────── P7 artifact import ───────────────────────────


_IMPORTABLE_STATUSES = ("done", "failed", "cancelled", "paused")


def _latest_artifact_entry(events: list[dict], logical: str) -> dict | None:
    """倒序找最近一条携带该逻辑名产物的事件(node_done 的 output/diff、
    node_failed_soft 的 error 各归其位);返回统一条目。"""
    for e in reversed(events):
        for item in e.get("artifacts") or []:
            if isinstance(item, dict) and item.get("name") == logical:
                return item
        if e.get("type") == "node_done" and logical.endswith(".output")                 and e.get("output_sha256"):
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
            })

        results: list[dict] = []
        for plan in staged:   # 校验全部通过,锁仍握着:现在才动字节
            ref = copy_imported_artifact(
                source_path=Path(plan.pop("path_str")),
                source_sha256=plan["source_sha256"],
                run_dir=run_dir, name=plan["source_name"])
            results.append({**plan, "ref": ref.as_dict()})
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
