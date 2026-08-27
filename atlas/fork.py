"""P13:fork 与失效闭包。

fork = 拿同一张(通常改过的)图从源 run 再跑一次。本模块是纯计划层:
只读源 run 的事件账本与 spec 快照,静态重放两侧的 invocation 身份得到
changed 集,在静态图上取 changed + 全部后代为失效闭包(循环按强连通
分量整体失效,不做循环内部分保留),闭包外且源事件证明产物完整的
skip 候选节点合成 P7 导入声明。字节复制仍由 runs.resolve_imports 在
源 run 的 stable lock 内完成,最终跳过由既有 reuse 门槛 + 运行时输入
复核决定——本模块不写任何文件,失败发生在 run 目录创建之前。

保守性方向:静态判定"说不清"的一律归 changed(诚实重跑),静态判定
出错的代价只是多跑;运行时复核保证绝不把过期身份当等价跳过。
"""

from __future__ import annotations

import json
from pathlib import Path

from .artifacts import sha256_bytes
from .events import EventReader, fold_events
from .spec import SpecError

FORK_ALGO_VERSION = "p13-fork-v1"

# 与 runs._IMPORTABLE_STATUSES 同源:fork 只接受静稳终态源。running/
# interrupted 的账本还可能变化,闭包比较没有稳定基础,拒绝。
FORKABLE_STATUSES = ("done", "failed", "cancelled", "paused")


def _sccs(node_ids: list[str],
          adjacency: dict[str, list[str]]) -> list[set[str]]:
    """迭代 Tarjan 强连通分量。返回顺序是凝聚图的**反向拓扑序**
    (下游分量先出),调用方 reversed() 即得上游在先。

    不用递归实现:节点数上限 2 万,线性链会爆默认递归栈。
    """
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    result: list[set[str]] = []
    counter = 0
    for root in node_ids:
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, next_i = work[-1]
            if next_i == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            descended = False
            neighbors = adjacency.get(node, ())
            for i in range(next_i, len(neighbors)):
                nxt = neighbors[i]
                if nxt not in index:
                    work[-1] = (node, i + 1)
                    work.append((nxt, 0))
                    descended = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if descended:
                continue
            if low[node] == index[node]:
                scc: set[str] = set()
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    scc.add(member)
                    if member == node:
                        break
                result.append(scc)
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return result


def _latest_output_sha256(events: list[dict], logical: str) -> str | None:
    """源账本里该逻辑名最近一次携带完整哈希的产物记录,没有则 None。

    与 runs._latest_artifact_entry 同一抽取口径(node_done 的 artifacts
    列表 / output 字段兜底,兜底必须核对事件节点是生产者),但只关心
    哈希;源 run 自己导入的产物(artifact_imported)不在任何 artifacts
    列表里,视为不可再导出——保守侧,不赌字节还在。
    """
    producer = logical.rsplit(".", 1)[0]
    for e in reversed(events):
        for item in e.get("artifacts") or []:
            if (isinstance(item, dict) and item.get("name") == logical
                    and item.get("sha256")):
                return item["sha256"]
        if (e.get("type") == "node_done" and e.get("node") == producer
                and logical.endswith(".output")
                and e.get("output_sha256")):
            return e["output_sha256"]
    return None


def compute_fork_plan(*, spec, source_run: str, runs_root: Path,
                      task_sha256: str, backend_sha256: str) -> dict:
    """计算 fork 失效闭包与合成导入清单。

    changed 的语义是"静态 invocation 身份无法证明相等":包括直接不等
    (模型/prompt/参数/schema/prompt 提示等任一因子变了)、源里没有
    该节点或该节点没有账本身份、以及上游已变导致输入哈希无法静态
    判定。closure = changed + 静态图上全部后代(条件边也算——可能的
    数据流都失效;join 命中 changed 分支时自然落进闭包,满足"join
    依赖 changed 分支必须重跑"的合同)。

    只给能进 P7 skip 门槛的闭包外节点合成导入(llm/stop/无条件出边/
    源账本有完整产物与 invocation 记录);其余节点诚实重跑。合成导入
    交给 resolve_imports 与 _compute_reuse_plans,和显式 imports 走
    完全相同的锁纪律与复核链。
    """
    from atlas.engine import RunNotFoundError, compute_node_invocation_sha256
    from atlas.spec import spec_from_snapshot

    src_dir = Path(runs_root) / source_run
    events_path = src_dir / "events.jsonl"
    if not events_path.exists():
        raise RunNotFoundError(
            f"fork 源 {source_run!r} 不存在(没有 events.jsonl)")
    events = EventReader(events_path).all()
    status = fold_events(events)["status"]
    if status not in FORKABLE_STATUSES:
        raise SpecError(
            f"fork 源 {source_run!r} 持久状态是 {status!r}:只有静稳终态"
            f"({', '.join(FORKABLE_STATUSES)})可 fork;运行中/中断的"
            " run 拒绝引用")

    started = next((e for e in events if e.get("type") == "run_started"), None)
    if (not started or not started.get("backend_sha256")
            or not started.get("task_sha256")):
        raise SpecError(
            f"fork 源 {source_run!r} 账本缺 run_started 身份字段"
            "(task_sha256/backend_sha256),无法做闭包比较")
    backend_equal = backend_sha256 == started["backend_sha256"]
    task_equal = task_sha256 == started["task_sha256"]

    snapshot_path = src_dir / "spec.snapshot.json"
    if not snapshot_path.exists():
        raise SpecError(
            f"fork 源 {source_run!r} 缺少 spec.snapshot.json,无法比较节点身份")
    try:
        source_spec = spec_from_snapshot(
            json.loads(snapshot_path.read_text(encoding="utf-8")),
            source=f"run {source_run!r} spec snapshot")
    except (OSError, UnicodeError, json.JSONDecodeError, SpecError) as exc:
        raise SpecError(
            f"fork 源 {source_run!r} 的 spec snapshot 无效:{exc}") from exc

    source_nodes = {n.id: n for n in source_spec.nodes}
    # 同节点多次 started(重试/循环迭代)取最近一次的账本身份
    source_invocations: dict[str, str | None] = {}
    for e in events:
        if e.get("type") == "node_started" and e.get("node"):
            source_invocations[e["node"]] = e.get("invocation_sha256")
    source_outputs: dict[str, str] = {}
    for n in source_spec.nodes:
        sha = _latest_output_sha256(events, n.output_name)
        if sha:
            source_outputs[n.output_name] = sha

    conditional_sources = {e.source for e in spec.edges if e.when is not None}
    adjacency: dict[str, list[str]] = {n.id: [] for n in spec.nodes}
    for e in spec.edges:
        if e.target != "END" and e.source in adjacency:
            adjacency[e.source].append(e.target)

    changed: set[str] = set()

    def _static_inputs(node) -> list[dict] | None:
        """静态可判定的输入哈希清单;None = 无法证明与源相等。

        task 变了而本节点消费 task → 无法判定;上游已 changed → 该输入
        必变;源没有该产物的完整哈希记录(含显式导入名——新 spec 的
        imports 在锁外没有稳定字节)→ 无法判定。全部诚实归 changed。
        """
        if not task_equal and "task" in node.consumes:
            return None
        out: list[dict] = []
        for name in node.consumes:
            if name == "task":
                out.append({"name": "task", "sha256": task_sha256})
                continue
            producer = name.rsplit(".", 1)[0]
            if producer in changed:
                return None
            sha = source_outputs.get(name)
            if not sha:
                return None
            out.append({"name": name, "sha256": sha})
        return out

    # 凝聚图按上游在先的顺序处理:判定 N 的输入可否静态取自源账本时,
    # 其上游的 changed 与否已经决定。同分量任一成员 changed → 整个
    # 分量 changed(循环整体失效,不做内部分保留)。
    for scc in reversed(_sccs([n.id for n in spec.nodes], adjacency)):
        scc_changed = False
        for node in spec.nodes:
            if node.id not in scc:
                continue
            if (not backend_equal or node.id not in source_nodes
                    or not source_invocations.get(node.id)):
                scc_changed = True
                continue
            inputs = _static_inputs(node)
            if inputs is None:
                scc_changed = True
                continue
            local = compute_node_invocation_sha256(
                node=node,
                prompt_sha256=sha256_bytes(node.prompt.encode("utf-8")),
                inputs=inputs, backend_sha256=backend_sha256)
            if local != source_invocations[node.id]:
                scc_changed = True
        if scc_changed:
            changed |= scc

    closure = set(changed)
    frontier = list(changed)
    while frontier:
        current = frontier.pop()
        for nxt in adjacency.get(current, ()):
            if nxt not in closure:
                closure.add(nxt)
                frontier.append(nxt)

    # 合同:闭包内禁止 import/skip——闭包的语义是"必须重算",与导入
    # 旧字节自相矛盾,当场拒绝而不是静默忽略用户的声明
    conflicting = sorted(n.id for n in spec.nodes
                         if n.id in closure and n.imports)
    if conflicting:
        raise SpecError(
            f"fork 闭包内节点 {conflicting} 声明了 imports:闭包节点必须"
            "重算,不允许导入旧字节;请移除这些 imports 或还原对应节点的"
            "改动")

    imports: list[dict] = []
    for node in spec.nodes:
        if node.id in closure or node.id in conditional_sources:
            continue
        if node.type != "llm" or node.on_error != "stop":
            continue
        if not source_invocations.get(node.id):
            continue
        if node.output_name not in source_outputs:
            continue
        imports.append({"run": source_run, "name": node.output_name})

    plan = {
        "algo_version": FORK_ALGO_VERSION,
        "source_run": source_run,
        "source_status": status,
        "backend_equal": backend_equal,
        "task_equal": task_equal,
        "changed": sorted(changed),
        "closure": sorted(closure),
        "imports": imports,
    }
    plan["fork_plan_sha256"] = sha256_bytes(json.dumps(
        plan, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return plan
