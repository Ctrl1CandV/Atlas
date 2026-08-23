# -*- coding: utf-8 -*-
"""Run lifecycle views derived from the ledger plus live controller/lock evidence."""
import json
from pathlib import Path

from atlas.costs import fold_cost_accounting
from atlas.engine import (RunConflictError, acquire_run_lock,
                          release_run_lock)
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
