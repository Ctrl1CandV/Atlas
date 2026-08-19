# -*- coding: utf-8 -*-
"""编排引擎:spec → LangGraph,执行,事件流落盘,checkpoint 续跑。

节点函数三步,顺序刻意(ARCHITECTURE 第 3 节):
先校验输入(投影+哈希),再调模型(失败链+假成功检测),再落盘。

M1 范围:条件边(查表路由,不调模型)、循环(max_iterations 守卫)、并行
(无条件扇出)、SQLite checkpoint + 崩溃续跑(恢复粒度是节点边界,刻意的)。

路由读的是**产物原文**(带哈希断言),不是内存里的副本——保证「路由依据」
与「落盘的真相」是同一份字节。
"""
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Mapping, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from atlas.adapters import AdapterRegistry, call_with_fallback
from atlas.artifacts import artifact_entry
from atlas.costs import (CostLedger, CostLimitError, compute_cost_usd,
                         fold_cost_accounting)
from atlas.events import EventLog, EventReader, fold_events
from atlas.nodes import make_agent_node_fn
from atlas.nodes.agent import SourceBaselineToken
from atlas.integrity import (
    ArtifactRef,
    IntegrityError,
    PROJECTION_MAX_BYTES,
    ResourceLimitError,
    TASK_MAX_BYTES,
    build_projection,
    parse_projection_evidence,
    store_artifact,
    read_artifact,
    sha256_bytes,
)
from atlas.spec import (EdgeSpec, SpecError, WorkflowSpec, spec_fingerprint,
                        spec_from_snapshot, spec_to_snapshot, validate_node_spec,
                        validate_spec)


class GuardViolation(Exception):
    """循环超过 max_iterations 上限。大声失败,不静默跑下去。"""


class TimeoutViolation(Exception):
    """运行超过 guards.timeout_s(节点边界检查)。"""


class NoRouteError(Exception):
    """路由字段值匹配不到任何出边。路由是查表,不猜。"""


class RunNotFoundError(Exception):
    """续跑的 run 不存在或没有 checkpoint。"""


class HumanRejected(Exception):
    """human 节点被人工驳回。运行以失败终止,理由进账本。"""


class CostExceeded(Exception):
    """累计成本超过 guards.max_cost_usd。大声停止,不烧完预算才说。"""


class RunConflictError(SpecError, RunNotFoundError):
    """运行正被写入或状态与请求冲突；Web 映射为 HTTP 409。"""


PREPARED_EXECUTION_VERSION = 1


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _runner_descriptor(runner: object, provider_ids: set[str]) -> dict:
    if runner is None:
        return {"version": 1, "kind": "none"}
    if getattr(runner, "nonsecret_execution_descriptor", False):
        descriptor = runner.execution_descriptor(provider_ids)
        if not isinstance(descriptor, dict):
            raise SpecError("agent runner execution_descriptor 必须返回对象")
        return descriptor
    # 注入 runner 不读取实例状态、repr 或闭包，避免响应/密钥进入身份。
    if callable(runner):
        module = getattr(runner, "__module__", type(runner).__module__)
        qualname = getattr(runner, "__qualname__", type(runner).__qualname__)
    else:
        module, qualname = type(runner).__module__, type(runner).__qualname__
    return {"version": 1, "kind": "injected",
            "class": f"{module}.{qualname}"}


@dataclass(frozen=True)
class PreparedExecution:
    spec: WorkflowSpec
    entry: str
    registry: AdapterRegistry
    agent_runner: object
    spec_sha256: str
    backend_sha256: str
    execution_sha256: str
    source_baseline_tokens: tuple[SourceBaselineToken, ...] = ()
    version: int = PREPARED_EXECUTION_VERSION


def prepare_execution(spec: WorkflowSpec, registry: AdapterRegistry, *,
                      agent_runner=None, agent_runner_factory=None
                      ) -> PreparedExecution:
    """一次性完成预检并冻结本次执行所依赖的非秘密后端身份。"""
    entry = validate_executable_spec(
        spec, registry, require_agent_sandbox=agent_runner is None)
    source_baseline_tokens = ()
    if agent_runner is None:
        agent_runner = (agent_runner_factory(spec) if agent_runner_factory is not None
                        else prepare_production_agent_runner(spec))
    if getattr(agent_runner, "production_runner", False):
        source_baseline_tokens = tuple(
            getattr(agent_runner, "source_baseline_tokens", ()))
        expected = {
            node.id: os.path.normcase(str(Path(node.workdir).resolve()))
            for node in spec.nodes
            if node.type == "coding_agent" and node.writable
        }
        actual: dict[str, str] = {}
        for token in source_baseline_tokens:
            if token.node_id in actual:
                raise SpecError(
                    f"production runner 为 coding_agent {token.node_id!r} "
                    "提供了重复 SourceBaselineToken")
            actual[token.node_id] = token.source_path
        if set(actual) != set(expected):
            raise SpecError(
                "production runner 的 SourceBaselineToken 节点集合不符:"
                f"需要 {sorted(expected)},得到 {sorted(actual)}")
        for node_id, source_path in expected.items():
            if actual[node_id] != source_path:
                raise SpecError(
                    f"SourceBaselineToken 与 coding_agent {node_id!r} 的 workdir 不匹配")
    registry.freeze()
    spec_sha256 = spec_fingerprint(spec)
    agent_provider_ids = {
        node.model.partition(":")[0] for node in spec.nodes
        if node.type in ("research", "coding_agent") and node.model
    }
    backend_descriptor = {
        "version": PREPARED_EXECUTION_VERSION,
        "registry": registry.execution_descriptor(),
        "agent_runner": _runner_descriptor(agent_runner, agent_provider_ids),
    }
    backend_sha256 = _canonical_sha256(backend_descriptor)
    execution_sha256 = _canonical_sha256({
        "version": PREPARED_EXECUTION_VERSION,
        "spec_sha256": spec_sha256,
        "backend_sha256": backend_sha256,
    })
    return PreparedExecution(
        spec=spec, entry=entry, registry=registry, agent_runner=agent_runner,
        source_baseline_tokens=source_baseline_tokens,
        spec_sha256=spec_sha256, backend_sha256=backend_sha256,
        execution_sha256=execution_sha256)


def _current_prepared_backend_sha256(prepared: PreparedExecution) -> str:
    provider_ids = {
        node.model.partition(":")[0] for node in prepared.spec.nodes
        if node.type in ("research", "coding_agent") and node.model
    }
    return _canonical_sha256({
        "version": prepared.version,
        "registry": prepared.registry.execution_descriptor(),
        "agent_runner": _runner_descriptor(prepared.agent_runner, provider_ids),
    })


def _use_prepared(spec: WorkflowSpec, registry: AdapterRegistry | None,
                  agent_runner, prepared: PreparedExecution | None
                  ) -> PreparedExecution:
    if prepared is None:
        if registry is None:
            raise SpecError("缺少 AdapterRegistry")
        return prepare_execution(spec, registry, agent_runner=agent_runner)
    if registry is not None and registry is not prepared.registry:
        raise SpecError("PreparedExecution 与另行传入的 AdapterRegistry 冲突")
    if agent_runner is not None and agent_runner is not prepared.agent_runner:
        raise SpecError("PreparedExecution 与另行传入的 agent runner 冲突")
    actual = spec_fingerprint(spec)
    if prepared.spec_sha256 != actual or spec_fingerprint(prepared.spec) != actual:
        raise SpecError("PreparedExecution 的 spec_sha256 与请求规格不符")
    if registry is not None and registry is not prepared.registry:
        raise SpecError("传入 registry 与 PreparedExecution 冻结的 registry 冲突")
    if agent_runner is not None and agent_runner is not prepared.agent_runner:
        raise SpecError("传入 agent_runner 与 PreparedExecution 冻结的 runner 冲突")
    current_backend = _current_prepared_backend_sha256(prepared)
    if current_backend != prepared.backend_sha256:
        raise SpecError("PreparedExecution 的后端对象在预检后发生变化,拒绝执行")
    expected_execution = _canonical_sha256({
        "version": prepared.version,
        "spec_sha256": prepared.spec_sha256,
        "backend_sha256": current_backend,
    })
    if expected_execution != prepared.execution_sha256:
        raise SpecError("PreparedExecution 的 execution_sha256 与当前后端不符")
    return prepared


def _check_persisted_execution_identity(run_id: str, started: dict | None,
                                        prepared: PreparedExecution) -> bool:
    expected_spec = started.get("spec_sha256") if started else None
    if expected_spec and expected_spec != prepared.spec_sha256:
        raise SpecError(f"run {run_id!r} 的 spec_sha256 不符,拒绝继续")
    identity_keys = ("prepared_execution_version", "backend_sha256", "execution_sha256")
    present = [bool(started and started.get(key)) for key in identity_keys]
    if any(present) and not all(present):
        raise SpecError(f"run {run_id!r} 的执行身份字段不完整,拒绝按 legacy 继续")
    if not any(present):
        return True
    if started.get("prepared_execution_version") != prepared.version:
        raise SpecError(f"run {run_id!r} 的 prepared_execution_version 不符")
    if started.get("backend_sha256") != prepared.backend_sha256:
        raise SpecError(f"run {run_id!r} 的 backend_sha256 不符,执行后端已漂移")
    if started.get("execution_sha256") != prepared.execution_sha256:
        raise SpecError(
            f"run {run_id!r} 的 execution_sha256 不符,执行后端已漂移,拒绝继续")
    return False


def merge_dicts(base: dict | None, update: dict | None) -> dict:
    merged = dict(base or {})
    merged.update(update or {})
    return merged


def merge_counts(base: dict | None, update: dict | None) -> dict:
    merged = dict(base or {})
    for k, v in (update or {}).items():
        merged[k] = merged.get(k, 0) + v
    return merged


def _settled_spent_usd(events: list[dict]) -> float:
    """按持久 reservation 状态机重建守卫已占用金额。"""
    return fold_cost_accounting(events).accounted_usd


class AtlasState(TypedDict, total=False):
    task: str
    artifacts: Annotated[dict, merge_dicts]    # 逻辑名 → ArtifactRef.as_dict()
    iterations: Annotated[dict, merge_counts]  # node_id → 已完成执行次数


# ─────────────────────────── 路由:纯查表 ───────────────────────────


def _route_value(node, node_id: str, output: dict, candidates) -> str:
    """路由查表的唯一实现:字段值 → 命中的 when 键。不猜,不调模型。

    resolve_route(纯函数,A5 被测对象)与 _make_router(引擎内读盘版)
    共用它,两者不会漂移。
    """
    value = output.get(node.route_field)
    if value is None:
        raise NoRouteError(
            f"节点 {node_id} 的输出里没有路由字段 {node.route_field!r}。"
            f"条件出边依赖它做查表"
        )
    if value not in candidates:
        raise NoRouteError(
            f"节点 {node_id} 的路由字段 {node.route_field}={value!r} 匹配不到任何出边"
            f"(候选:{sorted(candidates)})。路由是查表,不猜"
        )
    return value


def resolve_route(spec: WorkflowSpec, node_id: str, output: dict) -> str:
    """按节点输出的路由字段值查表返回边的 target。

    纯函数,不调模型、不读文件(A5 的被测对象)。
    """
    node = spec.node(node_id)
    edges = spec.outgoing(node_id)
    value = _route_value(node, node_id, output,
                         [e.when for e in edges if e.when is not None])
    for e in edges:
        if e.when == value:
            return e.target
    raise NoRouteError(f"节点 {node_id} 路由命中 {value!r} 但没有对应边")  # 不可达


def _make_router(spec: WorkflowSpec, src: str, path_map: dict):
    """LangGraph 条件边的回调:读产物原文(哈希断言)→ 返回 when 键。

    路由依据与落盘真相是同一份字节(带哈希断言的读回)。
    """
    node = spec.node(src)

    def router(state: AtlasState) -> str:
        ref_dict = state.get("artifacts", {}).get(node.output_name)
        if ref_dict is None:
            raise NoRouteError(
                f"路由需要 {node.output_name},但产物库里没有它。"
                f"节点 {src} 刚执行完,不应发生——这是引擎 bug"
            )
        raw = read_artifact(ArtifactRef.from_dict(ref_dict))
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise NoRouteError(
                f"节点 {src} 的产物不是合法 JSON,无法读取路由字段:{e}"
            ) from e
        if not isinstance(parsed, dict):
            raise NoRouteError(
                f"节点 {src} 的产物不是 JSON 对象,无法读取路由字段"
            )
        return _route_value(node, src, parsed, sorted(path_map))

    return router


# ─────────────────────────── 执行 ───────────────────────────


@dataclass
class _NodeCtx:
    run_dir: Path
    log: EventLog
    registry: AdapterRegistry
    reader: EventReader
    # agent 执行器；生产入口在落盘前冻结已预检的 runner，测试可显式注入替身。
    agent_runner: object = field(default=None, repr=False)
    source_baseline_tokens: Mapping[str, SourceBaselineToken] = field(
        default_factory=lambda: MappingProxyType({}), repr=False)
    timeout_s: float | None = field(default=None, repr=False)
    cost_cap: float | None = field(default=None, repr=False)
    cost_ledger: CostLedger | None = field(default=None, repr=False)
    _wall_start: datetime | None = field(default=None, repr=False)
    _agent_runner_raw: object = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.agent_runner is None:
            from atlas.nodes.sandbox import sandbox_runner
            self.agent_runner = sandbox_runner
        self._agent_runner_raw = self.agent_runner

        def guarded_agent_runner(*args, timeout_s=None, **kwargs):
            node_id = kwargs.get("node_type", "agent")
            effective = self.call_timeout(timeout_s, node_id)
            try:
                return self._agent_runner_raw(*args, timeout_s=effective, **kwargs)
            except Exception as e:
                # agent 工厂会在失败后固定 sleep 2s；deadline 不足时禁止进入该 sleep。
                if self.timeout_s is not None and self.remaining_timeout(
                        node_id=node_id) <= 2.0:
                    raise TimeoutViolation(
                        f"节点 {node_id}:剩余整图时间不足以执行 retry sleep") from e
                raise

        self.agent_runner = guarded_agent_runner
        if self.cost_ledger is None:
            self.cost_ledger = CostLedger(self.cost_cap, spent=self.spent_usd())

    def wall_start(self) -> datetime | None:
        """run_started 的时间戳(续跑后仍指向最初开始时刻)。"""
        if self._wall_start is None:
            ev = self.reader.find(type="run_started")
            if ev is not None:
                self._wall_start = datetime.fromisoformat(ev["ts"])
        return self._wall_start

    def check_timeout(self, timeout_s: float | None, node_id: str) -> None:
        if timeout_s is None:
            return
        remaining = self.remaining_timeout(timeout_s, node_id)
        if remaining <= 0:  # remaining_timeout 已抛；仅为类型/边界兜底
            raise TimeoutViolation(f"节点 {node_id}:整图 deadline 已耗尽")

    def remaining_timeout(self, timeout_s: float | None = None,
                          node_id: str = "节点") -> float:
        """返回整图剩余有效秒数；人工审批等待不计入 deadline。"""
        cap = self.timeout_s if timeout_s is None else timeout_s
        if cap is None:
            return float("inf")
        start = self.wall_start()
        if start is None:
            return cap
        paused = self.paused_seconds()
        elapsed = (datetime.now(timezone.utc) - start).total_seconds() - paused
        remaining = cap - elapsed
        if remaining <= 0:
            raise TimeoutViolation(
                f"节点 {node_id}:有效运行 {elapsed:.3f}s(人工审批等待 "
                f"{paused:.3f}s 不计入),超过 guards.timeout_s={cap}s")
        return remaining

    def call_timeout(self, node_timeout: float | None, node_id: str) -> float | None:
        """实际调用超时 = min(节点上限,整图剩余时间)。"""
        remaining = self.remaining_timeout(node_id=node_id)
        if remaining == float("inf"):
            return node_timeout
        return remaining if node_timeout is None else min(node_timeout, remaining)

    def paused_seconds(self) -> float:
        """人工审批的等待时间——那是人的时间,不算进运行超时。"""
        total = 0.0
        paused_at: datetime | None = None
        for e in self.reader.all():
            t = e["type"]
            if t == "run_paused":
                paused_at = datetime.fromisoformat(e["ts"])
            elif t in ("run_approval", "run_resumed") and paused_at is not None:
                total += (datetime.fromisoformat(e["ts"]) - paused_at).total_seconds()
                paused_at = None
        if paused_at is not None:
            total += (datetime.now(timezone.utc) - paused_at).total_seconds()
        return total

    def spent_usd(self) -> float:
        """账本里已结算的真实调用成本；兼容没有 cost_settled 的旧 run。"""
        return _settled_spent_usd(self.reader.all())

    def warned_cost_unknown(self) -> bool:
        return any(e["type"] == "cost_unknown" for e in self.reader.all())


@dataclass
class RunResult:
    run_id: str
    dir: Path
    events: EventReader
    final_state: dict
    status: str = "done"   # done | paused(在 human 节点等待批准)

    @property
    def artifacts(self) -> dict[str, ArtifactRef]:
        return {name: ArtifactRef.from_dict(d)
                for name, d in self.final_state.get("artifacts", {}).items()}

    def folded(self) -> dict:
        """从事件流重放出的状态(A6:必须与运行时状态一致)。"""
        return fold_events(self.events.all())


def _check_cost_guard(spec: WorkflowSpec, ctx: _NodeCtx, node_id: str,
                      projected_usd: float | None = None) -> None:
    """成本上限守卫,两道查(Quorum 的教训:只查已花,最后一次调用可以无限超支)。

    派发前:已花 + 本次预估 > 上限 → 停(不是等花超了才停);
    projected 为 None(费率未知/agent 节点)时退化为只查已花。
    费率未知的调用成本是 null:守卫对它不生效,但只要设了 max_cost_usd
    就记一次警告——「守卫没盖住全部节点」必须可见,不能默默当没事。
    agent 节点诚实说明:CLI 会话不走供应商费率表,美元守卫盖不住它,
    它的上限靠 timeout_s 墙钟(见 PLAN-v2 4.3 的边界标注)。
    """
    cap = spec.guards.max_cost_usd
    if cap is None:
        return
    spent = ctx.spent_usd()
    if spent + (projected_usd or 0.0) > cap:
        raise CostExceeded(
            f"节点 {node_id} 派发前检查:已花费 ${spent:.4f}"
            + (f" + 本次预估 ${projected_usd:.4f}" if projected_usd else "")
            + f" > guards.max_cost_usd=${cap}。停止烧钱"
        )
    if not ctx.warned_cost_unknown():
        events = ctx.reader.all()
        unknown = [e["model_used"] for e in events
                   if e["type"] == "node_done" and e.get("cost_usd") is None
                   and not (e["model_used"] == "human"
                            or e["model_used"].startswith("agent:"))]
        if unknown:
            ctx.log.emit(
                "cost_unknown",
                run_id=ctx.run_dir.name,
                models=sorted(set(unknown)),
                reason="这些调用没有费率,成本记 null;max_cost_usd 守卫未覆盖它们。"
                       "在 config/pricing.json 填入确认过的单价后生效",
            )


def _project_node_cost(node, projection_chars: int) -> float | None:
    """llm 节点本次调用的成本预估:输入按字符/3,输出按 max_tokens 上界。

    Quorum 的教训(9 样本):守卫必须朝"停下"失败,输出按上界估而不是
    "输入的一倍"。费率未知 → None(守卫退化为只查已花,warning 兜底)。
    """
    from atlas.costs import compute_cost_usd
    if node.type != "llm":
        return None
    est_in = projection_chars // 3
    est_out = node.max_output_tokens or 8192
    return compute_cost_usd(node.model, est_in, est_out)


def _make_node_fn(node, spec: WorkflowSpec, ctx: _NodeCtx):
    def run(state: AtlasState) -> dict:
        started = time.monotonic()
        iteration = state.get("iterations", {}).get(node.id, 0) + 1
        max_iter = spec.guards.effective_max_iterations
        if iteration > max_iter:
            raise GuardViolation(
                f"节点 {node.id} 将第 {iteration} 次执行,"
                f"超过 guards.max_iterations={max_iter}。循环未收敛,停止"
            )
        # guards.timeout_s 是 run 级墙钟(节点边界检查);
        # node.timeout_s 是单次调用超时,已传给 call_with_fallback——
        # 两个语义不混用(混用会让靠后的节点"未执行即超时",M4 审查🟠3)
        ctx.check_timeout(spec.guards.timeout_s, node.id)

        # 1. 输入投影:逐个哈希校验,缺失即 WiringError——先于任何模型调用
        projection, proj_ref, consumed = build_projection(
            ctx.run_dir,
            node_id=node.id,
            iteration=iteration,
            prompt=node.prompt,
            consumes=node.consumes,
            artifacts=state["artifacts"],
        )
        ctx.log.emit(
            "node_input",
            node=node.id,
            iteration=iteration,
            projection_path=str(proj_ref.path),
            projection_sha256=proj_ref.sha256,
            consumed=[r.as_dict() for r in consumed],
        )
        # 投影不花钱；每一次真实 retry/fallback 都在 adapter 钩子中独立预留。
        # node_started 只能在预算预留成功并持久化后记录，不能把被守卫拦截误报为已派发。
        started_emitted = False
        attempt = 0
        attempt_by_reservation: dict[str, int] = {}
        warned_reservations: set[str] = set()

        def _project_candidate(cand: str) -> float | None:
            output_cap = (node.max_output_tokens
                          or ctx.registry.default_max_output_tokens(cand))
            return compute_cost_usd(cand, len(projection) // 3, output_cap)

        def _reserve(cand: str):
            nonlocal attempt, started_emitted
            attempt += 1
            projected = _project_candidate(cand)
            try:
                if projected is None and spec.guards.max_cost_usd is not None:
                    reservation = ctx.cost_ledger.reserve_remaining(
                        description=f"节点 {node.id} 候选 {cand} 派发前检查")
                else:
                    reservation = ctx.cost_ledger.reserve(
                        projected,
                        description=f"节点 {node.id} 候选 {cand} 派发前检查")
            except CostLimitError as e:
                raise CostExceeded(str(e)) from e
            reservation_id = (reservation.reservation_id
                              if reservation is not None else None)
            if reservation is not None:
                attempt_by_reservation[reservation.reservation_id] = attempt
                ctx.log.emit(
                    "cost_reserved", node=node.id, iteration=iteration,
                    attempt=attempt, model=cand,
                    reservation_id=reservation.reservation_id,
                    reserved_usd=reservation.amount,
                )
            if (projected is None and reservation_id is not None
                    and reservation_id not in warned_reservations):
                warned_reservations.add(reservation_id)
                ctx.log.emit(
                    "cost_unknown", run_id=ctx.run_dir.name, models=[cand],
                    attempt=attempt, reservation_id=reservation_id,
                    reason="该候选没有确认费率；有成本帽时按本次预留全额"
                           "占用预算，直到结算获得可信费用。",
                )
            if not started_emitted:
                ctx.log.emit("node_started", node=node.id, iteration=iteration,
                             model_requested=node.model)
                started_emitted = True
            return reservation

        def _settle(reservation, cand: str, usage):
            current_attempt = (attempt_by_reservation.get(reservation.reservation_id)
                               if reservation is not None else attempt)
            actual = compute_cost_usd(
                cand,
                usage.input_tokens if usage else None,
                usage.output_tokens if usage else None)
            unknown = actual is None
            exceeded = None
            try:
                accounted = ctx.cost_ledger.settle(
                    reservation, actual,
                    description=f"节点 {node.id} 候选 {cand} 结算",
                    unknown_as_reserved=unknown)
            except CostLimitError as e:
                accounted = actual
                exceeded = e
            reservation_id = (reservation.reservation_id
                              if reservation is not None else None)
            if (unknown and reservation_id is not None
                    and reservation_id not in warned_reservations):
                warned_reservations.add(reservation_id)
                ctx.log.emit(
                    "cost_unknown", run_id=ctx.run_dir.name, models=[cand],
                    attempt=current_attempt, reservation_id=reservation_id,
                    reason="模型调用已派发但未返回可信费用；有成本帽时按本次"
                           "预留全额计入 guarded/accounted 成本，不再释放重用。",
                )
            # 实际调用已经发生，即使结算后发现超支也必须先把真实成本写入账本。
            ctx.log.emit(
                "cost_settled", node=node.id, iteration=iteration,
                attempt=current_attempt, model=cand,
                reservation_id=reservation_id,
                actual_cost_usd=actual,
                accounted_cost_usd=accounted,
                cost_unknown=unknown,
                cost_usd=actual,
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
            )
            if exceeded is not None:
                raise CostExceeded(str(exceeded)) from exceeded

        # 2. 调模型:失败链 + 假成功检测 + M4 节点参数
        outcome = call_with_fallback(
            registry=ctx.registry,
            log=ctx.log,
            node_id=node.id,
            iteration=iteration,
            model_ref=node.model,
            fallback_refs=node.fallback,
            prompt=projection.decode("utf-8"),
            required_fields=node.required_fields,
            thinking_tier=node.thinking,
            temperature=node.temperature,
            seed=node.seed,
            max_output_tokens=node.max_output_tokens,
            timeout_s=node.timeout_s,
            retry=node.retry,
            before_attempt=_reserve,
            after_attempt=_settle,
            remaining_timeout=lambda: ctx.remaining_timeout(node_id=node.id),
        )

        # 3. 产物落盘 + 事件
        ext = ".json" if node.required_fields else ".txt"
        ref = store_artifact(
            ctx.run_dir,
            name=f"{node.id}.output",
            filename=f"{node.id}.output.{iteration}{ext}",
            content=outcome.text.encode("utf-8"),
        )
        usage = outcome.usage
        cost_usd = compute_cost_usd(
            outcome.model_used,
            usage.input_tokens if usage else None,
            usage.output_tokens if usage else None,
        )
        out_entry = artifact_entry(
            name=ref.name, role="output", path=ref.path, sha256=ref.sha256,
            size_bytes=len(outcome.text.encode("utf-8")),
            media_type="application/json" if node.required_fields else "text/markdown")
        ctx.log.emit(
            "node_done",
            node=node.id,
            iteration=iteration,
            model_requested=outcome.model_requested,
            model_used=outcome.model_used,
            degraded=outcome.model_used != outcome.model_requested,
            output_truncated=outcome.output_truncated,
            output_path=str(ref.path),
            output_sha256=ref.sha256,
            artifacts=[out_entry],
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            reasoning_tokens=outcome.reasoning_tokens,
            reasoning_kind=outcome.reasoning_kind,
            thinking_tier=node.thinking,
            cost_usd=cost_usd,  # 费率未知 → null,不填猜的数字
            duration_s=round(time.monotonic() - started, 3),
        )
        # state 里的产物与事件里的类型化条目同构(A6:重放 == 运行时状态)
        return {"artifacts": {ref.name: out_entry},
                "iterations": {node.id: 1}}

    return run


def _make_human_node_fn(node, spec: WorkflowSpec, ctx: _NodeCtx):
    """human 节点:暂停等人批准(架构第 8 节)。

    interrupt 的暂停—重启进程—恢复语义已实测可行(scripts/interrupt_smoke.py)。
    审批者看到的材料 = 投影原文(与模型节点同一份完整性保障);
    批准记录本身也是产物,下游可以引用它。
    """

    def run(state: AtlasState) -> dict:
        started = time.monotonic()
        iteration = state.get("iterations", {}).get(node.id, 0) + 1
        max_iter = spec.guards.effective_max_iterations
        if iteration > max_iter:
            raise GuardViolation(
                f"节点 {node.id} 将第 {iteration} 次执行,"
                f"超过 guards.max_iterations={max_iter}"
            )
        ctx.check_timeout(spec.guards.timeout_s, node.id)

        projection, proj_ref, consumed = build_projection(
            ctx.run_dir,
            node_id=node.id,
            iteration=iteration,
            prompt=node.prompt,
            consumes=node.consumes,
            artifacts=state["artifacts"],
        )
        ctx.log.emit(
            "node_input",
            node=node.id,
            iteration=iteration,
            projection_path=str(proj_ref.path),
            projection_sha256=proj_ref.sha256,
            consumed=[r.as_dict() for r in consumed],
        )
        ctx.log.emit("node_started", node=node.id, iteration=iteration,
                     model_requested="human")

        # 暂停在这里。恢复时 answer = {"decision": "approve"|"reject", "comment": str}
        answer = interrupt({
            "node": node.id,
            "question": node.prompt,
            "consumed": [r.as_dict() for r in consumed],
        })
        if not isinstance(answer, dict) or answer.get("decision") not in ("approve", "reject"):
            raise HumanRejected(
                f"human 节点 {node.id} 收到无法解析的批复:{answer!r}。"
                f"需要 {{decision: approve|reject, comment: ...}}"
            )
        comment = str(answer.get("comment", "") or "")
        if answer["decision"] == "reject":
            raise HumanRejected(f"human 节点 {node.id} 被人工驳回:{comment or '(无说明)'}")

        record = json.dumps({"decision": "approve", "comment": comment},
                            ensure_ascii=False)
        ref = store_artifact(
            ctx.run_dir,
            name=f"{node.id}.output",
            filename=f"{node.id}.output.{iteration}.json",
            content=record.encode("utf-8"),
        )
        out_entry = artifact_entry(
            name=ref.name, role="output", path=ref.path, sha256=ref.sha256,
            size_bytes=len(record.encode("utf-8")),
            media_type="application/json")
        ctx.log.emit(
            "node_done",
            node=node.id,
            iteration=iteration,
            model_requested="human",
            model_used="human",
            degraded=False,
            output_truncated=False,
            output_path=str(ref.path),
            output_sha256=ref.sha256,
            artifacts=[out_entry],
            input_tokens=None,   # 人读的,没有 token 概念,不冒充
            output_tokens=None,
            cost_usd=None,
            duration_s=round(time.monotonic() - started, 3),
        )
        return {"artifacts": {ref.name: out_entry},
                "iterations": {node.id: 1}}

    return run


_NODE_FACTORIES = {
    "llm": _make_node_fn,
    "human": _make_human_node_fn,
    "research": None,       # 在 import 尾部补(research/coding_agent 共用工厂)
    "coding_agent": None,
}


def _build_compiled_graph(spec: WorkflowSpec, ctx: _NodeCtx, checkpointer):
    builder = StateGraph(AtlasState)
    for n in spec.nodes:
        if n.type in ("research", "coding_agent"):
            builder.add_node(n.id, make_agent_node_fn(n, spec, ctx))
        else:
            builder.add_node(n.id, _NODE_FACTORIES[n.type](n, spec, ctx))
    for e in spec.all_entries():      # 多入口:全部从 START 并行开跑
        builder.add_edge(START, e)

    outgoing: dict[str, list[EdgeSpec]] = {}
    for e in spec.edges:
        outgoing.setdefault(e.source, []).append(e)
    for src, edges in outgoing.items():
        conditional = [e for e in edges if e.when is not None]
        if conditional:
            path_map = {e.when: (END if e.target == "END" else e.target)
                        for e in conditional}
            builder.add_conditional_edges(src, _make_router(spec, src, path_map),
                                          path_map)
        else:
            for e in edges:
                builder.add_edge(src, END if e.target == "END" else e.target)
    for n in spec.nodes:  # 无出边的节点显式接 END
        if n.id not in outgoing:
            builder.add_edge(n.id, END)
    return builder.compile(checkpointer=checkpointer)


def _resolve_models(spec: WorkflowSpec, registry: AdapterRegistry) -> None:
    """白名单与密钥检查——校验在花钱之前。human 等非模型节点跳过。"""
    for n in spec.nodes:
        if n.type == "llm":
            registry.resolve(n.model)
            for f in n.fallback:
                registry.resolve(f)


def validate_executable_spec(
    spec: WorkflowSpec,
    registry: AdapterRegistry,
    *,
    require_agent_sandbox: bool = True,
) -> str:
    """验证有效规格与 LLM 适配器；不建 run 目录。

    ``require_agent_sandbox`` 保留为兼容参数，语义是“生产 agent 必须显式
    配置模型”。实际 runner/CLI/供应商/凭据预检由
    :func:`prepare_production_agent_runner` 完成，避免把 agent 供应商错误地
    塞进只服务 LLM SDK 的 AdapterRegistry。
    """
    for node in spec.nodes:
        validate_node_spec(node, where=f"executable node {node.id}")
        if node.type == "llm" and not node.model:
            raise SpecError(
                f"节点 {node.id} 未配置模型:打开它的节点详情选择模型,"
                f"或让 AI 把模型写入 workflows/*.yaml。示例模型需要先配置再运行")
        if (require_agent_sandbox and node.type in ("research", "coding_agent")
                and not node.model):
            raise SpecError(
                f"agent 节点 {node.id} 未配置模型:本机受控执行必须显式选择模型")
    entry = validate_spec(spec, source=f"workflow {spec.name!r}")
    _resolve_models(spec, registry)
    return entry


def prepare_production_agent_runner(
    spec: WorkflowSpec,
    *,
    agent_config_path: Path | None = None,
    providers_path: Path | None = None,
    env_store=None,
):
    """在 run_id/锁/产物之前冻结生产 agent runner；无 agent 时返回 None。"""
    nodes = [node for node in spec.nodes
             if node.type in ("research", "coding_agent")]
    if not nodes:
        return None
    from atlas.nodes.local_cli import (preflight_agent_nodes,
                                       prepare_local_cli_runner)
    runner = prepare_local_cli_runner(
        agent_config_path=agent_config_path,
        providers_path=providers_path,
        env_store=env_store,
    )
    preflight_agent_nodes(nodes, runner)
    return runner


def new_run_id() -> str:
    """公开给 web/MCP 层:先定 id 再启动执行,调用方能立即拿到它。"""
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def execute_graph(
    spec: WorkflowSpec,
    *,
    task: str,
    runs_root: Path,
    registry: AdapterRegistry | None = None,
    run_id: str | None = None,
    checkpoint: bool = True,
    agent_runner=None,
    prepared: PreparedExecution | None = None,
    base_spec_sha256: str | None = None,
    binding_summary=(),
    override_summary=(),
    _lock_held: bool = False,
) -> RunResult:
    """执行一张已完全具体化的图；prepared 存在时绝不重复预检。"""
    run_id = run_id or new_run_id()
    run_dir = Path(runs_root) / run_id
    try:
        prepared = _use_prepared(spec, registry, agent_runner, prepared)
    except Exception:
        if _lock_held:
            release_run_lock(run_id, runs_root=runs_root)
        raise

    if not _lock_held:
        acquire_run_lock(run_id, runs_root=runs_root)
    try:
        tombstone = Path(runs_root) / ".trash" / run_id
        if run_dir.exists() or tombstone.exists():
            raise RunConflictError(f"run {run_id!r} 已存在或仍在删除清理中,拒绝重复执行")
        log = EventLog(run_dir)
        ctx = _NodeCtx(run_dir=run_dir, log=log, registry=prepared.registry,
                       reader=EventReader(run_dir / "events.jsonl"),
                       agent_runner=prepared.agent_runner,
                       source_baseline_tokens=MappingProxyType({
                           token.node_id: token
                           for token in prepared.source_baseline_tokens
                       }),
                       timeout_s=spec.guards.timeout_s,
                       cost_ledger=CostLedger(spec.guards.max_cost_usd))

        task_ref = store_artifact(
            run_dir, name="task", filename="task.txt", content=task.encode("utf-8"),
            max_bytes=TASK_MAX_BYTES,
        )
        (run_dir / "spec.snapshot.json").write_text(
            json.dumps(spec_to_snapshot(spec), ensure_ascii=False, indent=1),
            encoding="utf-8")
        log.emit("run_started", graph=spec.name, run_id=run_id,
                 task_sha256=task_ref.sha256, task_path=str(task_ref.path),
                 spec_sha256=prepared.spec_sha256,
                 base_spec_sha256=base_spec_sha256 or prepared.spec_sha256,
                 effective_spec_sha256=prepared.spec_sha256,
                 prepared_execution_version=prepared.version,
                 backend_sha256=prepared.backend_sha256,
                 execution_sha256=prepared.execution_sha256,
                 bindings=list(binding_summary),
                 overrides=list(override_summary))

        return _invoke(
            spec, ctx, run_id, entry=prepared.entry, checkpoint=checkpoint,
            task_input={"task": task, "artifacts": {"task": task_ref.as_dict()}},
        )
    finally:
        release_run_lock(run_id, runs_root=runs_root)


def _validate_resume_snapshot(run_id: str, run_dir: Path,
                              prepared: PreparedExecution) -> None:
    snapshot_path = run_dir / "spec.snapshot.json"
    if not snapshot_path.exists():
        raise RunNotFoundError(f"run {run_id!r} 缺少 spec.snapshot.json,无法恢复")
    try:
        snapshot_data = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot_spec = spec_from_snapshot(
            snapshot_data, source=f"run {run_id!r} spec snapshot")
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, SpecError) as exc:
        raise IntegrityError(f"run {run_id!r} 的 spec snapshot 无效:{exc}") from exc
    if spec_fingerprint(snapshot_spec) != prepared.spec_sha256:
        raise SpecError(f"run {run_id!r} 的 spec snapshot 与请求规格不符,拒绝继续")


def _transient_checkpoint_error(exc: sqlite3.Error) -> bool:
    """Only retry lock races and Windows post-kill WAL handle teardown."""
    code = getattr(exc, "sqlite_errorcode", None)
    if not isinstance(code, int):
        return False
    base = code & 0xFF
    if base in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return True
    return os.name == "nt" and code == getattr(
        sqlite3, "SQLITE_IOERR_TRUNCATE", -1)


def _validate_resume_checkpoint(run_id: str, run_dir: Path) -> None:
    checkpoint_path = run_dir / "checkpoint.sqlite"
    if not checkpoint_path.exists():
        raise RunNotFoundError(f"run {run_id!r} 没有 checkpoint.sqlite,无法恢复")

    deadline = time.monotonic() + 2.0
    delay = 0.005
    while True:
        conn = None
        try:
            # 必须普通读写打开，让 SQLite 自己从 WAL 做崩溃恢复；不能删除
            # -wal/-shm，它们可能持有主文件尚未 checkpoint 的唯一已提交状态。
            conn = sqlite3.connect(checkpoint_path, timeout=0.25)
            quick_check = conn.execute("PRAGMA quick_check").fetchone()
            if quick_check != ("ok",):
                raise IntegrityError(
                    f"run {run_id!r} 的 checkpoint.sqlite 完整性检查失败:"
                    f"{quick_check!r}")
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if "checkpoints" not in tables:
                raise IntegrityError(
                    f"run {run_id!r} 的 checkpoint.sqlite 缺少 checkpoints 表")
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(checkpoints)")
            }
            if "thread_id" not in columns:
                raise IntegrityError(
                    f"run {run_id!r} 的 checkpoint.sqlite 缺少 thread_id")
            checkpoint = conn.execute(
                "SELECT 1 FROM checkpoints WHERE thread_id = ? LIMIT 1", (run_id,)
            ).fetchone()
            if checkpoint is None:
                raise RunNotFoundError(
                    f"run {run_id!r} 的 checkpoint.sqlite 没有可恢复状态")
            return
        except sqlite3.Error as exc:
            if not _transient_checkpoint_error(exc) \
                    or time.monotonic() >= deadline:
                name = getattr(exc, "sqlite_errorname", None)
                detail = f"{name}: {exc}" if name else str(exc)
                raise IntegrityError(
                    f"run {run_id!r} 的 checkpoint.sqlite 无效:{detail}") from exc
        finally:
            if conn is not None:
                conn.close()
        time.sleep(delay)
        delay = min(delay * 2, 0.1)


def _resume_graph_locked(
    run_id: str,
    *,
    spec: WorkflowSpec,
    runs_root: Path,
    prepared: PreparedExecution,
    checkpoint: bool,
    interrupted_only: bool,
) -> RunResult:
    """调用方持有 run lock 时校验并重放 checkpoint。"""
    run_dir = Path(runs_root) / run_id
    if not (run_dir / "events.jsonl").exists():
        raise RunNotFoundError(f"run {run_id!r} 不存在(没有 events.jsonl)")
    reader = EventReader(run_dir / "events.jsonl")
    events = reader.all()
    persisted_status = fold_events(events)["status"]
    if interrupted_only and persisted_status != "running":
        raise RunConflictError(
            f"run {run_id!r} 不是可恢复的 interrupted 运行"
            f"(账本状态 {persisted_status!r})")

    started = next((e for e in events if e.get("type") == "run_started"), None)
    legacy = _check_persisted_execution_identity(run_id, started, prepared)
    if interrupted_only:
        if started is None or not started.get("spec_sha256") or legacy:
            raise SpecError(f"run {run_id!r} 缺少完整执行身份,拒绝恢复")
        _validate_resume_snapshot(run_id, run_dir, prepared)
        _validate_resume_checkpoint(run_id, run_dir)
    elif not (run_dir / "checkpoint.sqlite").exists():
        raise RunNotFoundError(f"run {run_id!r} 没有 checkpoint.sqlite,无法恢复")

    # 所有 admission 校验均在 EventLog 构造前完成；拒绝不会截尾或追加账本。
    log = EventLog(run_dir, continue_seq=True)
    ctx = _NodeCtx(run_dir=run_dir, log=log, registry=prepared.registry,
                   reader=reader, agent_runner=prepared.agent_runner,
                   source_baseline_tokens=MappingProxyType({
                       token.node_id: token
                       for token in prepared.source_baseline_tokens
                   }),
                   timeout_s=spec.guards.timeout_s,
                   cost_ledger=CostLedger(spec.guards.max_cost_usd,
                                          spent=_settled_spent_usd(events)))
    if legacy:
        log.emit("legacy_execution_identity", run_id=run_id,
                 reason="旧运行缺少 execution_sha256，按 spec-only 兼容继续")
    log.emit("run_resumed", run_id=run_id)
    return _invoke(spec, ctx, run_id, entry=prepared.entry,
                   checkpoint=checkpoint, task_input=None)


def _assert_interrupted_locked(run_id: str, *, runs_root: Path) -> None:
    run_dir = Path(runs_root) / run_id
    events_path = run_dir / "events.jsonl"
    if not events_path.exists():
        raise RunNotFoundError(f"run {run_id!r} 不存在(没有 events.jsonl)")
    persisted_status = fold_events(EventReader(events_path).all())["status"]
    if persisted_status != "running":
        raise RunConflictError(
            f"run {run_id!r} 不是可恢复的 interrupted 运行"
            f"(账本状态 {persisted_status!r})")


def lock_interrupted_run(
    run_id: str,
    *,
    runs_root: Path,
    active_controller: bool = False,
) -> None:
    """先按持久状态原子预占 interrupted run；成功后由调用方移交或释放锁。"""
    if active_controller:
        raise RunConflictError(f"run {run_id!r} 仍有活跃本地控制器,拒绝恢复")
    acquire_run_lock(run_id, runs_root=runs_root)
    try:
        _assert_interrupted_locked(run_id, runs_root=runs_root)
    except Exception:
        release_run_lock(run_id, runs_root=runs_root)
        raise


def validate_interrupted_run_locked(
    run_id: str,
    *,
    runs_root: Path,
    prepared: PreparedExecution,
) -> None:
    """调用方持有 run lock 时校验恢复身份、快照与 checkpoint。"""
    run_dir = Path(runs_root) / run_id
    events = EventReader(run_dir / "events.jsonl").all()
    if fold_events(events)["status"] != "running":
        raise RunConflictError(f"run {run_id!r} 在恢复校验期间状态已改变")
    started = next((e for e in events if e.get("type") == "run_started"), None)
    legacy = _check_persisted_execution_identity(run_id, started, prepared)
    if started is None or not started.get("spec_sha256") or legacy:
        raise SpecError(f"run {run_id!r} 缺少完整执行身份,拒绝恢复")
    _validate_resume_snapshot(run_id, run_dir, prepared)
    _validate_resume_checkpoint(run_id, run_dir)


def resume_graph(
    run_id: str,
    *,
    spec: WorkflowSpec,
    runs_root: Path,
    registry: AdapterRegistry | None = None,
    checkpoint: bool = True,
    agent_runner=None,
    prepared: PreparedExecution | None = None,
    active_controller: bool = False,
    _lock_held: bool = False,
) -> RunResult:
    """仅准入动态判定为 interrupted 的运行，并原子取得锁后恢复。"""
    if active_controller:
        if _lock_held:
            release_run_lock(run_id, runs_root=runs_root)
        raise RunConflictError(f"run {run_id!r} 仍有活跃本地控制器,拒绝恢复")
    if not _lock_held:
        acquire_run_lock(run_id, runs_root=runs_root)
    try:
        _assert_interrupted_locked(run_id, runs_root=runs_root)
        prepared = _use_prepared(spec, registry, agent_runner, prepared)
        # 成功取得权威锁，连同 running fold 构成 interrupted 判定；
        # 锁持续持有到重放结束，避免双重准入。
        return _resume_graph_locked(
            run_id, spec=spec, runs_root=runs_root, prepared=prepared,
            checkpoint=checkpoint, interrupted_only=True)
    finally:
        release_run_lock(run_id, runs_root=runs_root)


def _resume_graph_replay(
    run_id: str,
    *,
    spec: WorkflowSpec,
    runs_root: Path,
    registry: AdapterRegistry | None = None,
    checkpoint: bool = True,
    agent_runner=None,
    prepared: PreparedExecution | None = None,
) -> RunResult:
    """私有低层 checkpoint 重放；保留旧测试所需的非产品恢复语义。"""
    prepared = _use_prepared(spec, registry, agent_runner, prepared)
    acquire_run_lock(run_id, runs_root=runs_root)
    try:
        return _resume_graph_locked(
            run_id, spec=spec, runs_root=runs_root, prepared=prepared,
            checkpoint=checkpoint, interrupted_only=False)
    finally:
        release_run_lock(run_id, runs_root=runs_root)


def release_approval_run_lock(run_id: str, *, runs_root: Path) -> None:
    """释放 Web 同步预占的审批锁；重复释放安全。"""
    release_run_lock(run_id, runs_root=runs_root)


def _path_in_run(path: Path, parent: Path, *, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        root = parent.resolve(strict=True)
    except OSError as e:
        raise IntegrityError(f"审批材料 {label} 不存在或无法解析:{e}") from e
    if not resolved.is_relative_to(root):
        raise IntegrityError(f"审批材料 {label} 越出当前 run 目录:{resolved}")
    return resolved


def _verify_approval_material(run_dir: Path, events: list[dict]) -> dict:
    """锁内重验暂停节点实际展示的投影、消费产物和 Diff 摘要。"""
    paused = next((e for e in reversed(events) if e.get("type") == "run_paused"), None)
    if paused is None or not paused.get("node"):
        raise RunConflictError("运行账本没有可审批的 run_paused 节点")
    node_id = paused["node"]
    node_input = next((e for e in reversed(events)
                       if e.get("type") == "node_input" and e.get("node") == node_id), None)
    if node_input is None:
        raise IntegrityError(f"human 节点 {node_id} 缺少 node_input 审批材料")

    projection = _path_in_run(
        Path(node_input["projection_path"]), run_dir / "projections",
        label=f"{node_id}.projection")
    projection_bytes = projection.read_bytes()
    if len(projection_bytes) > PROJECTION_MAX_BYTES:
        raise ResourceLimitError(
            f"审批投影 {node_id!r} 体积 {len(projection_bytes)} 字节超过上限 "
            f"{PROJECTION_MAX_BYTES} 字节;拒绝审批")
    projection_sha256 = sha256_bytes(projection_bytes)
    if projection_sha256 != node_input.get("projection_sha256"):
        raise IntegrityError(f"human 节点 {node_id} 的审批投影哈希不符")
    # 投影是哈希锚定的"审批者看到的摘要"；事件 metadata 暂停后仍可改写，
    # 因此 Diff 摘要必须以投影内证据为准，与账本 metadata 逐项交叉验证。
    projection_evidence = parse_projection_evidence(projection_bytes)

    consumed_evidence = []
    diff_evidence = []
    evidence_covered: set[str] = set()
    done_events = [e for e in events if e.get("type") == "node_done"]
    for raw in node_input.get("consumed", []):
        if not isinstance(raw, dict):
            raise IntegrityError(f"human 节点 {node_id} 的 consumed 记录损坏")
        ref = ArtifactRef.from_dict(raw)
        _path_in_run(ref.path, run_dir / "artifacts", label=ref.name)
        read_artifact(ref)
        consumed_evidence.append({"name": ref.name, "sha256": ref.sha256})

        artifact = next((item for event in reversed(done_events)
                         for item in event.get("artifacts", [])
                         if isinstance(item, dict)
                         and item.get("name") == ref.name
                         and item.get("sha256") == ref.sha256), None)
        # 触发锚是投影证据(哈希锚定),不是账本:把账本 role 降级或伪造条目
        # sha256 不能跳过校验——投影里声明过 diff 证据的消费名必须完整验证。
        projection_says_diff = ref.name in projection_evidence
        ledger_says_diff = artifact is not None and artifact.get("role") == "diff"
        if projection_says_diff or ledger_says_diff:
            if not projection_says_diff:
                raise IntegrityError(
                    f"审批投影缺少 Diff 产物 {ref.name} 的证据摘要,拒绝批准")
            if not ledger_says_diff:
                raise IntegrityError(
                    f"Diff 产物 {ref.name} 的账本条目缺失或 role 被降级,"
                    "与审批投影证据不一致")
            metadata = artifact.get("metadata") or {}
            required = ("baseline_digest", "result_digest", "patch_digest")
            if not all(isinstance(metadata.get(key), str) and metadata[key]
                       for key in required):
                raise IntegrityError(f"Diff 产物 {ref.name} 缺少审批摘要")
            if metadata["patch_digest"] != ref.sha256:
                raise IntegrityError(f"Diff 产物 {ref.name} 的 patch_digest 与产物哈希不符")
            expected = projection_evidence.get(ref.name)
            for key in required:
                if metadata.get(key) != expected.get(key):
                    raise IntegrityError(
                        f"Diff 产物 {ref.name} 的 {key} 与审批投影中的证据不符,"
                        "账本摘要可能已被篡改")
            diff_evidence.append({
                "name": ref.name,
                "artifact_sha256": ref.sha256,
                **{key: metadata[key] for key in required},
            })
            evidence_covered.add(ref.name)

    # 触发域必须是投影证据键集的完整覆盖:consumed 摘除或改名 diff 条目
    # 不能让投影里声明的证据静默消失——那是审批者实际看到的材料。
    uncovered = set(projection_evidence) - evidence_covered
    if uncovered:
        raise IntegrityError(
            f"审批投影声明了 Diff 证据 {sorted(uncovered)},但对应产物"
            "不在暂停节点的 consumed 清单中;账本可能已被篡改,拒绝批准")

    return {
        "node": node_id,
        "projection_sha256": projection_sha256,
        "consumed": consumed_evidence,
        "diff_evidence": diff_evidence,
    }


def lock_approval_run(run_id: str, *, spec: WorkflowSpec,
                      runs_root: Path,
                      prepared: PreparedExecution | None = None) -> None:
    """原子取得审批写锁并在锁内验证 run 仍处于 paused。

    成功返回时锁保持占用，调用方必须紧接着以 ``_lock_held=True`` 调用
    approve_run；任何验证失败都会先释放锁。
    """
    run_dir = Path(runs_root) / run_id
    acquire_run_lock(run_id, runs_root=runs_root)
    try:
        if not (run_dir / "events.jsonl").exists():
            raise RunNotFoundError(f"run {run_id!r} 不存在")
        if not (run_dir / "checkpoint.sqlite").exists():
            raise RunNotFoundError(f"run {run_id!r} 没有 checkpoint.sqlite")
        events = EventReader(run_dir / "events.jsonl").all()
        started = next((e for e in events if e["type"] == "run_started"), None)
        if prepared is not None:
            _use_prepared(spec, prepared.registry, prepared.agent_runner, prepared)
            _check_persisted_execution_identity(run_id, started, prepared)
        else:
            expected = started.get("spec_sha256") if started else None
            if expected and expected != spec_fingerprint(spec):
                raise SpecError(
                    f"run {run_id!r} 的 spec_sha256 不符,拒绝批复(图被改过)")
        status = fold_events(events)["status"]
        if status != "paused":
            raise RunConflictError(
                f"run {run_id!r} 不在暂停状态(当前 {status!r}),"
                f"只有暂停在 human 节点的运行才能批复")
        _verify_approval_material(run_dir, events)
    except Exception:
        release_run_lock(run_id, runs_root=runs_root)
        raise


def approve_run(
    run_id: str,
    *,
    decision: str,
    comment: str,
    spec: WorkflowSpec,
    runs_root: Path,
    registry: AdapterRegistry | None = None,
    checkpoint: bool = True,
    agent_runner=None,
    prepared: PreparedExecution | None = None,
    _lock_held: bool = False,
) -> RunResult:
    """对暂停在 human 节点的运行给出批复并继续执行。

    decision 只认 approve/reject;reject 会让运行以 HumanRejected 失败终止。
    ``_lock_held`` 仅供 Web 在同步预占锁后交给后台线程继续。
    """
    run_dir = Path(runs_root) / run_id
    try:
        if decision not in ("approve", "reject"):
            raise SpecError(f"decision 只能是 approve/reject,得到 {decision!r}")
        prepared = _use_prepared(spec, registry, agent_runner, prepared)
        entry = prepared.entry
        registry = prepared.registry
        agent_runner = prepared.agent_runner
    except Exception:
        # Web 可能已同步预占锁；后台在线程内复核失败时也必须释放。
        if _lock_held:
            release_run_lock(run_id, runs_root=runs_root)
        raise

    if not _lock_held:
        acquire_run_lock(run_id, runs_root=runs_root)
    try:
        if not (run_dir / "events.jsonl").exists():
            raise RunNotFoundError(f"run {run_id!r} 不存在")
        if not (run_dir / "checkpoint.sqlite").exists():
            raise RunNotFoundError(f"run {run_id!r} 没有 checkpoint.sqlite")
        events = EventReader(run_dir / "events.jsonl").all()
        started = next((e for e in events if e["type"] == "run_started"), None)
        legacy_identity = _check_persisted_execution_identity(
            run_id, started, prepared)
        if fold_events(events)["status"] != "paused":
            status = fold_events(events)["status"]
            raise RunConflictError(
                f"run {run_id!r} 不在暂停状态(当前 {status!r}),"
                f"只有暂停在 human 节点的运行才能批复")
        approval_evidence = _verify_approval_material(run_dir, events)

        log = EventLog(run_dir, continue_seq=True)
        spent = _settled_spent_usd(events)
        ctx = _NodeCtx(run_dir=run_dir, log=log, registry=prepared.registry,
                       reader=EventReader(run_dir / "events.jsonl"),
                       agent_runner=prepared.agent_runner,
                       source_baseline_tokens=MappingProxyType({
                           token.node_id: token
                           for token in prepared.source_baseline_tokens
                       }),
                       timeout_s=spec.guards.timeout_s,
                       cost_ledger=CostLedger(spec.guards.max_cost_usd, spent=spent))
        if legacy_identity:
            log.emit("legacy_execution_identity", run_id=run_id,
                     spec_sha256=prepared.spec_sha256,
                     note="旧运行缺少 execution_sha256，本次按 spec-only 兼容继续")
        log.emit(
            "run_approval", run_id=run_id, decision=decision, comment=comment,
            approved_node=approval_evidence["node"],
            approved_projection_sha256=approval_evidence["projection_sha256"],
            approved_consumed=approval_evidence["consumed"],
            approved_diffs=approval_evidence["diff_evidence"],
        )
        log.emit("run_resumed", run_id=run_id)
        command = Command(resume={"decision": decision, "comment": comment})
        return _invoke(spec, ctx, run_id, entry=prepared.entry,
                       checkpoint=checkpoint, task_input=command)
    finally:
        release_run_lock(run_id, runs_root=runs_root)


@dataclass
class _HeldRunLock:
    file: object
    mutex: threading.Lock


_RUN_LOCKS_GUARD = threading.Lock()
_RUN_LOCK_MUTEXES: dict[str, threading.Lock] = {}
# Web 在请求线程预占、后台线程完成后释放，因此句柄必须支持受控跨线程交接。
_RUN_LOCK_HELD: dict[str, _HeldRunLock] = {}


def run_lock_path(run_id: str, *, runs_root: Path) -> Path:
    """返回永久稳定锁文件路径；文件存在本身不代表锁被占用。"""
    return Path(runs_root) / ".locks" / f"{run_id}.lock"


def _lock_key(run_id: str, runs_root: Path) -> str:
    return os.path.normcase(str(run_lock_path(run_id, runs_root=runs_root).absolute()))


def _try_os_lock(file) -> None:
    if os.name == "nt":
        import msvcrt
        file.seek(0)
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RunConflictError(
                "运行锁正被其他进程持有(.locks),拒绝并发操作") from exc
    else:
        import fcntl
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            raise RunConflictError(
                "运行锁正被其他进程持有(.locks),拒绝并发操作") from exc


def _unlock_os(file) -> None:
    if os.name == "nt":
        import msvcrt
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def acquire_run_lock(run_id: str, *, runs_root: Path) -> None:
    """非阻塞取得 per-run 权威锁；锁文件永久保留且绝不按 TTL 接管。"""
    path = run_lock_path(run_id, runs_root=runs_root)
    key = _lock_key(run_id, Path(runs_root))
    path.parent.mkdir(parents=True, exist_ok=True)
    with _RUN_LOCKS_GUARD:
        mutex = _RUN_LOCK_MUTEXES.setdefault(key, threading.Lock())
        already_held = key in _RUN_LOCK_HELD
    if already_held or not mutex.acquire(blocking=False):
        raise RunConflictError(f"run {run_id!r} 的运行锁正被本进程其他操作持有")
    file = None
    try:
        file = open(path, "a+b")
        if path.stat().st_size == 0:
            file.write(b"\0")
            file.flush()
        _try_os_lock(file)
        # 文件内容仅供诊断且可能来自上次持有者；权威状态是当前 OS 锁。
        # Windows 不允许在锁住首字节后截断文件，因此持锁期间不改内容。
        with _RUN_LOCKS_GUARD:
            _RUN_LOCK_HELD[key] = _HeldRunLock(file=file, mutex=mutex)
    except Exception:
        if file is not None:
            file.close()
        mutex.release()
        raise


def release_run_lock(run_id: str, *, runs_root: Path) -> None:
    """释放本进程持有的锁；稳定锁文件不删除；重复释放安全。"""
    key = _lock_key(run_id, Path(runs_root))
    with _RUN_LOCKS_GUARD:
        lock = _RUN_LOCK_HELD.pop(key, None)
    if lock is None:
        return
    try:
        _unlock_os(lock.file)
    finally:
        lock.file.close()
        lock.mutex.release()


def _invoke(spec: WorkflowSpec, ctx: _NodeCtx, run_id: str, *,
            entry: str, checkpoint: bool,
            task_input) -> RunResult:
    conn = None
    try:
        if checkpoint:
            # timeout:两个进程短暂并发时不立刻 database is locked
            conn = sqlite3.connect(ctx.run_dir / "checkpoint.sqlite",
                                   check_same_thread=False, timeout=30.0)
            checkpointer = SqliteSaver(conn)
        else:
            checkpointer = None
        app = _build_compiled_graph(spec, ctx, checkpointer)
        config = {"configurable": {"thread_id": run_id}}
        final_state = app.invoke(task_input, config)
        # human 节点的暂停:invoke 正常返回但还有待恢复的任务
        if app.get_state(config).next:
            paused_node = app.get_state(config).next[0]
            ctx.log.emit("run_paused", run_id=run_id, node=paused_node,
                         note="等待人工批准(web 界面上的批准/驳回按钮)")
            return RunResult(
                run_id=run_id,
                dir=ctx.run_dir,
                events=EventReader(ctx.run_dir / "events.jsonl"),
                final_state=dict(final_state),
                status="paused",
            )
        # 图执行返回后也要守 deadline；调用方忽略 SDK timeout 或本地处理过慢时
        # 不能把已超时的整图记成成功。
        ctx.remaining_timeout(node_id="run_done")
        ctx.log.emit("run_done", run_id=run_id,
                     nodes_done=len(ctx.reader.filter(type="node_done")))
        return RunResult(
            run_id=run_id,
            dir=ctx.run_dir,
            events=EventReader(ctx.run_dir / "events.jsonl"),
            final_state=dict(final_state),
        )
    except Exception as e:
        # 图构建/连接/执行,任何一步失败都记 run_failed——账本不许永久停在 running
        try:
            ctx.log.emit("run_failed", run_id=run_id,
                         error_type=type(e).__name__, error=str(e))
        except Exception:
            pass  # 账本本身写不进去(磁盘满?);异常照抛
        raise
    finally:
        if conn is not None:
            conn.close()
