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
import sys
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

from atlas.adapters import (AdapterRegistry, AllCandidatesFailed,
                            RunCancelled, call_with_fallback,
                            recover_json_object)
from atlas.exc import can_soft_fail, error_class_name
from atlas.artifacts import artifact_entry
from atlas.costs import (CostLedger, CostLimitError, compute_cost_usd,
                         fold_cost_accounting)
from atlas.events import EventLog, EventReader, fold_events
from atlas.nodes import make_agent_node_fn
from atlas.nodes.agent import AGENT_RETRY_SLEEP_S, SourceBaselineToken
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
from atlas.spec import (APPROVAL_DECISIONS, CHANGES_EDGE_KEY, FAILED_EDGE_KEY,
                        EdgeSpec, SpecError, WorkflowSpec,
                        spec_fingerprint, spec_from_snapshot, spec_to_snapshot,
                        validate_node_spec, validate_spec)


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
    # P3:每个节点「最近一次执行」的结局(ok / __failed__)。路由判定的事实源:
    # 成功与软失败各自覆写自己的键(merge_dicts 更新者胜),随 checkpoint 持久化,
    # resume 后语义不变。不能拿 .error 产物存在性代替——产物键只增不清,
    # branch 节点重入成功后残留的旧错误产物会把成功误判成软失败。
    route_facts: Annotated[dict, merge_dicts]


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


# 引擎内部的「成功」路由键前缀:__failed__ 与无条件扇出共存时,成功路径
# 不需要任何路由判定,落到这些键上(每条无条件边一个键;LangGraph 的
# path_map 值必须是可哈希单目标,扇出靠路由器返回键列表)
OK_ROUTE_KEY = "__ok__"


def _make_router(spec: WorkflowSpec, src: str, path_map: dict):
    """LangGraph 条件边的回调:读产物原文(哈希断言)→ 返回 when 键。

    路由依据与落盘真相是同一份字节(带哈希断言的读回)。
    P3:节点软失败时 route_facts 的 __failed__ 优先;P11:routed 审批的
    request_changes 走 __changes__(同一条事实通道)。成功路径上模型输出
    保留键字面量属于不可判定的路由,大声拒绝(NoRouteError 是治理错误)。
    """
    node = spec.node(src)
    reserved_keys = [k for k in (FAILED_EDGE_KEY, CHANGES_EDGE_KEY)
                     if k in path_map]

    def router(state: AtlasState) -> str:
        if reserved_keys:
            fact = (state.get("route_facts") or {}).get(src)
            if fact in reserved_keys:
                return fact
        ok_keys = sorted(k for k in path_map
                         if k.startswith(OK_ROUTE_KEY))
        if ok_keys:
            # 成功且无需路由判定:单目标返回键,多目标返回键列表(扇出)
            return ok_keys if len(ok_keys) > 1 else ok_keys[0]
        ref_dict = state.get("artifacts", {}).get(node.output_name)
        if ref_dict is None:
            raise NoRouteError(
                f"路由需要 {node.output_name},但产物库里没有它。"
                f"节点 {src} 刚执行完,不应发生——这是引擎 bug"
            )
        raw = read_artifact(ArtifactRef.from_dict(ref_dict))
        text = raw.decode("utf-8")
        parsed = None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # B1 同源宽容提取:验收(检查三)接受的"围栏/散文包着的 JSON",
            # 判路必须同样读得出,否则路由图会从"可 fallback 完成"变成硬失败。
            # 路由仍只依据落盘字节,提取是纯函数,重放结果不变。
            recovered = recover_json_object(text)
            if recovered is not None:
                parsed = recovered[0]
        if parsed is None:
            raise NoRouteError(
                f"节点 {src} 的产物不是合法 JSON,无法读取路由字段"
            )
        if not isinstance(parsed, dict):
            raise NoRouteError(
                f"节点 {src} 的产物不是 JSON 对象,无法读取路由字段"
            )
        value = _route_value(node, src, parsed, sorted(path_map))
        for reserved in reserved_keys:
            if value == reserved:
                raise NoRouteError(
                    f"节点 {src} 成功返回了保留路由值 {reserved!r}。"
                    "该值只由引擎在失败/要求修改时使用;请让模型输出其他"
                    "路由值")
        return value

    return router


# ─────────────────────────── 执行 ───────────────────────────


# ─────────────────────────── P9 controller heartbeat ───────────────────────────


HEARTBEAT_INTERVAL_ENV = "ATLAS_NODE_HEARTBEAT_INTERVAL_S"
DEFAULT_HEARTBEAT_INTERVAL_S = 30.0
MIN_HEARTBEAT_INTERVAL_S = 30.0


def resolve_heartbeat_interval(explicit: float | None = None) -> float:
    """运行级心跳间隔:显式参数 > 环境变量 > 默认 30s。

    这是运营参数,不进 YAML 图文件(图语义与运行环境解耦)。环境变量是
    跨进程一致的唯一渠道——resume/approve 续跑发生在新进程里,拿不到首次
    执行时的显式参数。环境值低于 30s 下限时大声拒绝,不静默钳制:30s 一条
    ≈ 每节点每天 2880 条事件,更快的频率必须在容量设计里重新论证。
    显式参数是进程内的可信输入(测试用小间隔驱动),不设下限。
    """
    if explicit is not None:
        return explicit
    raw = os.environ.get(HEARTBEAT_INTERVAL_ENV, "").strip()
    if not raw:
        return DEFAULT_HEARTBEAT_INTERVAL_S
    value = float(raw)
    if value < MIN_HEARTBEAT_INTERVAL_S:
        raise ValueError(
            f"{HEARTBEAT_INTERVAL_ENV}={value}s 低于下限 {MIN_HEARTBEAT_INTERVAL_S}s"
            f"(30s 一条 ≈ 每节点每天 2880 条事件,账本容量必须真实计入);"
            f"拒绝更快的频率")
    return value


class NodeHeartbeat:
    """controller 心跳:定时写 node_progress,只证明「controller 仍在等待
    这次派发返回」,不声称模型内部进度或百分比。

    生命周期与 attempt 对齐:预留钩子 set_context 更新 attempt/model 语境,
    派发窗口由调用点 begin()/end() 开合;end() 会 join 心跳线程,返回后
    任何迟到 tick 被拒绝(不写事件)——终态事件之后账本不会再出现心跳。
    线程为 daemon:controller 崩溃时进程退出不被心跳拖住。elapsed_ms 的
    基准是节点开工时刻(controller elapsed),不是 attempt 起点——用户
    看到的是「这个节点 controller 已经等了多久」,重试不该把它清零。
    """

    def __init__(self, log: EventLog, *, node: str, iteration: int,
                 interval_s: float, started_mono: float) -> None:
        self._log = log
        self._node = node
        self._iteration = iteration
        self._interval_s = interval_s
        self._started_mono = started_mono
        self._context: dict = {"phase": "waiting"}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def set_context(self, *, attempt: int, model: str,
                    phase: str = "waiting", **extra) -> None:
        """(重新)声明当前 attempt 语境;下一次 tick 起生效。"""
        with self._lock:
            self._context = {"attempt": attempt, "model": model,
                             "phase": phase, **extra}

    def mark_retry_wait(self) -> None:
        """phase 切到 retry:同一模型的传输失败重试等待期。"""
        with self._lock:
            self._context["phase"] = "retry"

    def begin(self) -> None:
        """派发窗口开启:线程已在跑则保持(语境已由 set_context 更新)。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop = threading.Event()
            thread = threading.Thread(
                target=self._loop, name=f"atlas-heartbeat-{self._node}",
                daemon=True)
            self._thread = thread
        thread.start()

    def end(self) -> None:
        """派发窗口关闭:停线程并等它收尾;幂等。此后 tick 不写事件。"""
        with self._lock:
            self._stop.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        with self._lock:
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self._interval_s):
            if not self._tick():
                break

    def _tick(self) -> bool:
        """写一条心跳;已停止时拒绝写并返回 False(测试的确定性入口)。"""
        with self._lock:
            if self._stop.is_set() or self._thread is None:
                return False
            fields = dict(self._context)
            elapsed_ms = int((time.monotonic() - self._started_mono) * 1000)
            self._log.emit("node_progress", node=self._node,
                           iteration=self._iteration,
                           elapsed_ms=elapsed_ms, **fields)
        return True


# P7 invocation identity:同一次"调用"的完整决定因素集合。算法版本化,
# 字段只增不改——升级算法时换版本号,旧账本的 invocation 永远可解释。
# v2 = v1 + required_fields(2026-08-27 审查阻塞项:output_schema 的
# 结构化验收是执行等价性的决定因子——schema 改严后,同一模型输出
# 可能从"合格"变 DegradedOutput,invocation 必须能区分这两种执行)
# v3 = v2 + inputs 按 consumes 原序(2026-08-27 审查建议采纳:
# build_projection 按同一顺序内联上游产物字节,[task,a.output] 与
# [a.output,task] 投影布局不同,是两次不同执行)
INVOCATION_ALGO_VERSION = "p7-invocation-v3"


def compute_node_invocation_sha256(*, node, prompt_sha256: str,
                                   inputs: list[dict],
                                   backend_sha256: str) -> str:
    """节点的 invocation_sha256:模型执行字段 + 有效 prompt + 有序输入哈希
    + 后端执行身份(prepared.backend_sha256:provider/runner registry
    + agent runner 的打包指纹),
    规范 JSON 后 SHA-256。inputs 是 [{name, sha256}](consumes 的实际供给),
    按 consumes 列表原序——顺序是投影布局的一部分,不排序。"""
    payload = {
        "algo": INVOCATION_ALGO_VERSION,
        "node": node.id,
        "model_ref": node.model,
        "fallback": list(node.fallback),
        "thinking": node.thinking,
        "temperature": node.temperature,
        "seed": node.seed,
        "max_output_tokens": node.max_output_tokens,
        "retry": node.retry,
        "timeout_s": node.timeout_s,
        "required_fields": list(node.required_fields or []),
        "prompt_sha256": prompt_sha256,
        "inputs": inputs,
        "backend_sha256": backend_sha256,
    }
    return sha256_bytes(json.dumps(
        payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _input_hashes_for(consumes, artifacts: Mapping) -> list[dict] | None:
    """consumes 清单 → [{name, sha256}](runtime 与静态复用判定共用同一条
    抽取规则;"task" 是初始 state 里的普通逻辑名)。任一成员缺哈希 →
    None:不可判定就诚实退出,绝不猜。"""
    out: list[dict] = []
    for name in consumes:
        ref = artifacts.get(name)
        if not isinstance(ref, dict) or not ref.get("sha256"):
            return None
        out.append({"name": name, "sha256": ref["sha256"]})
    return out


def _compute_reuse_plans(spec, import_plans, *, task_sha256,
                         backend_sha256) -> Mapping:
    """P7 自动 skip 判定(全部保守门槛,任何一条不满足就不复用):

    - 该节点的 <id>.output 被 imports 命中,且源账本记录了当时的
      invocation_sha256(旧账本没有该字段 → 不复用);
    - llm 节点、on_error=stop、无条件出边(条件/__failed__ 路由行为的
      正确性依赖真实执行产物,不赌);
    - 全部 consumes 都能由 task/导入克隆静态供给;
    - 本地重算 invocation == 源侧记录(模型/prompt/参数/后端身份/
      输入字节任一改变都不相等)。
    """
    if not import_plans:
        return MappingProxyType({})
    by_name = {p["source_name"]: p for p in import_plans}
    available: dict[str, dict] = {"task": {"name": "task",
                                           "sha256": task_sha256}}
    for p in import_plans:
        available[p["source_name"]] = p["ref"]
    conditional_sources = {e.source for e in spec.edges if e.when is not None}
    plans: dict[str, dict] = {}
    for node in spec.nodes:
        plan = by_name.get(node.output_name)
        if plan is None or not plan.get("source_invocation"):
            continue
        if node.type != "llm" or node.on_error != "stop":
            continue
        if node.id in conditional_sources:
            continue
        inputs = _input_hashes_for(list(node.consumes), available)
        if inputs is None:
            continue
        local_hash = compute_node_invocation_sha256(
            node=node,
            prompt_sha256=sha256_bytes(node.prompt.encode("utf-8")),
            inputs=inputs, backend_sha256=backend_sha256)
        if local_hash != plan["source_invocation"]:
            continue
        plans[node.id] = {"invocation": local_hash,
                          "source_run": plan["source_run"],
                          "source_name": plan["source_name"],
                          # P13:静态判定依据的输入哈希留在计划里——
                          # 跳过时刻按运行时 state 复核,预测过期就重跑
                          "inputs": inputs}
    return MappingProxyType(plans)


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
    heartbeat_interval_s: float | None = field(default=None, repr=False)
    # P7:backend 执行身份(prepared.execution_sha256;invocation 因子)与
    # 已判定的"跳过执行、直接复用导入产物"计划(node_id → plan dict)。
    backend_identity: str = field(default="", repr=False)
    reuse_plans: Mapping = field(default_factory=lambda: MappingProxyType({}),
                                 repr=False)
    cost_ledger: CostLedger | None = field(default=None, repr=False)
    _wall_start: datetime | None = field(default=None, repr=False)
    _agent_runner_raw: object = field(default=None, repr=False)
    _events_cache: tuple[int, list] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # 心跳间隔在此统一解析:显式参数 > 环境变量 > 默认 30s。三条构造
        # 路径(执行/续跑/审批续跑)都会经过这里,resume 的新进程靠环境
        # 变量与首次执行保持同一频率。
        self.heartbeat_interval_s = resolve_heartbeat_interval(
            self.heartbeat_interval_s)
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
                # agent 工厂在失败后固定 sleep AGENT_RETRY_SLEEP_S；
                # deadline 不足时禁止进入该 sleep。
                if self.timeout_s is not None and self.remaining_timeout(
                        node_id=node_id) <= AGENT_RETRY_SLEEP_S:
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

    def cancel_requested(self) -> bool:
        """P2 协作式取消:run 目录里出现 cancel.request.json 即为真。

        只做存在性检查(请求文件是触发器,不是账本内容);controller
        在消费点抛 RunCancelled,由 _invoke 统一写 run_cancelled 终态。
        """
        return (self.run_dir / CANCEL_REQUEST_FILENAME).exists()

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
        for e in self._events_once():
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
        return _settled_spent_usd(self._events_once())

    def warned_cost_unknown(self) -> bool:
        return any(e["type"] == "cost_unknown" for e in self._events_once())

    def _events_once(self) -> list[dict]:
        """同一节点执行内的多次守卫检查共享一次账本读取。

        events.jsonl 接近 16 MiB 上限时,每次全量读取都是秒级 IO;
        节点入口的 timeout/cost 检查会连读多次,这里按"事件数没变"
        缓存最近一份,事件只增不改,以长度判新是安全的。
        """
        cached = self._events_cache
        events = self.reader.all()
        if cached is not None and cached[0] == len(events):
            return cached[1]
        self._events_cache = (len(events), events)
        return events


@dataclass
class RunResult:
    run_id: str
    dir: Path
    events: EventReader
    final_state: dict
    status: str = "done"   # done | paused(human 等待批准) | cancelled(P2)

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


def _make_reused_node_fn(node, spec: WorkflowSpec, ctx: _NodeCtx):
    """P7 复用节点:零调用、零成本,只把"这段执行被导入结果顶替"如实入账。

    P13 加固:静态 skip 计划的输入哈希是"导入克隆"口径的预测;若运行时
    同名产物被真实执行覆盖(上游实际重跑、字节已变),预测即过期——
    跳过前按当次 state 复核输入哈希,不相等就委托真实执行,绝不把
    过期身份当等价跳过(合同:"不能先 pin 全部再意外跳过目标节点")。
    """
    plan = ctx.reuse_plans[node.id]
    real_fn = _NODE_FACTORIES[node.type](node, spec, ctx)

    def run(state: AtlasState) -> dict:
        runtime_inputs = _input_hashes_for(list(node.consumes),
                                           state.get("artifacts", {}))
        if runtime_inputs != plan.get("inputs"):
            return real_fn(state)
        iteration = state.get("iterations", {}).get(node.id, 0) + 1
        if ctx.cancel_requested():
            raise RunCancelled(f"节点 {node.id}(复用)执行前收到取消请求")
        ctx.log.emit(
            "node_imported_reused", run_id=ctx.run_dir.name,
            node=node.id, iteration=iteration,
            invocation_sha256=plan["invocation"],
            source_run=plan["source_run"],
            source_artifact=plan["source_name"],
            note="invocation 身份与源完全一致;本次未调用任何模型,"
                 "产物为源字节克隆(经哈希复验)")
        return {"iterations": {node.id: 1}}

    return run


def _soft_fail_node(node, ctx: _NodeCtx, exc: Exception, iteration: int) -> dict:
    """P3 内容类失败的 soft 落账:write-once 错误产物 + node_failed_soft。

    continue 与 branch 共用同一落账(错误产物进 state 产物库,消费方与
    路由器都能看到),差别只在图结构:branch 节点的 __failed__ 边由
    路由器按错误产物判定。节点没有 output 产物——依赖它的下游会在
    投影期 WiringError(治理错误,不吞)。
    """
    payload = json.dumps({
        "node": node.id,
        "iteration": iteration,
        "on_error": node.on_error,
        "error_class": error_class_name(exc),
        "error": str(exc),
        "attempts": [
            {"model": a.model, "error_type": a.error_type, "reason": a.reason}
            for a in getattr(exc, "attempts", ())],
    }, ensure_ascii=False, indent=1).encode("utf-8")
    ref = store_artifact(
        ctx.run_dir, name=f"{node.id}.error",
        filename=f"{node.id}.error.{iteration}.json", content=payload)
    entry = artifact_entry(
        name=ref.name, role="error", path=ref.path, sha256=ref.sha256,
        size_bytes=len(payload), media_type="application/json")
    ctx.log.emit(
        "node_failed_soft", node=node.id, iteration=iteration,
        on_error=node.on_error, error_class=error_class_name(exc),
        error=str(exc)[:2000], output_path=str(ref.path),
        output_sha256=ref.sha256, artifacts=[entry])
    return {"artifacts": {ref.name: entry}, "iterations": {node.id: 1},
            "route_facts": {node.id: FAILED_EDGE_KEY}}


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
        # P2 消费点:节点入口——任何花费(投影/预留/调用)之前。
        if ctx.cancel_requested():
            raise RunCancelled(f"节点 {node.id} 执行前收到取消请求")
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
        heartbeat = NodeHeartbeat(
            ctx.log, node=node.id, iteration=iteration,
            interval_s=ctx.heartbeat_interval_s, started_mono=started)

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
                # P7:node_started 携带本次调用的 invocation 身份(派发前
                # 计算完备时;输入缺失记 null,不阻塞执行)。时序仍在预算
                # 预留与持久化之后——被守卫拦截不得误报已派发。
                runtime_inputs = _input_hashes_for(
                    list(node.consumes), state["artifacts"])
                invocation = None
                if runtime_inputs is not None:
                    invocation = compute_node_invocation_sha256(
                        node=node,
                        prompt_sha256=sha256_bytes(node.prompt.encode("utf-8")),
                        inputs=runtime_inputs,
                        backend_sha256=ctx.backend_identity)
                ctx.log.emit("node_started", node=node.id, iteration=iteration,
                             model_requested=node.model,
                             invocation_sha256=invocation)
                started_emitted = True
            heartbeat.set_context(attempt=attempt, model=cand)
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
        try:
            try:
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
                    remaining_timeout=lambda: ctx.remaining_timeout(
                        node_id=node.id),
                    cancel_requested=ctx.cancel_requested,
                    heartbeat=heartbeat,
                )
            finally:
                # 兜底停心跳:派发窗口的正常开合在 call_with_fallback 内部,
                # 这里覆盖取消消费点抛 RunCancelled、意外异常等所有离开路径。
                heartbeat.end()
        except Exception as e:
            # P3:内容类失败(候选全部失败)在 stop 策略或治理类异常面前
            # 原样上抛——run_failed/run_cancelled 语义零变化;只有
            # continue/branch 配置且异常可 soft-fail 时才走软失败落账。
            if node.on_error == "stop" or not can_soft_fail(e):
                raise
            return _soft_fail_node(node, ctx, e, iteration)

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
            param_audit=(outcome.param_audit or None),
        )
        # state 里的产物与事件里的类型化条目同构(A6:重放 == 运行时状态)
        return {"artifacts": {ref.name: out_entry},
                "iterations": {node.id: 1},
                "route_facts": {node.id: "ok"}}

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
        # P2 消费点:human 入口——暂停等待审批前也尊重取消请求。
        if ctx.cancel_requested():
            raise RunCancelled(f"human 节点 {node.id} 执行前收到取消请求")
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

        # 暂停在这里。恢复时 answer = {"decision": ..., "comment": str};
        # binary 只认 approve|reject,routed 追加 request_changes(P11)。
        routed = node.approval_mode == "routed"
        legal = APPROVAL_DECISIONS if routed else ("approve", "reject")
        answer = interrupt({
            "node": node.id,
            "question": node.prompt,
            "consumed": [r.as_dict() for r in consumed],
        })
        if not isinstance(answer, dict) or answer.get("decision") not in legal:
            raise HumanRejected(
                f"human 节点 {node.id} 收到无法解析的批复:{answer!r}。"
                f"需要 {{decision: {'|'.join(legal)}, comment: ...}}"
            )
        decision = answer["decision"]
        comment = str(answer.get("comment", "") or "")
        validate_approval_decision(decision, comment)
        if decision == "reject":
            raise HumanRejected(f"human 节点 {node.id} 被人工驳回:{comment or '(无说明)'}")

        if decision == "request_changes":
            # P11:修改要求落 write-once 产物(修订节点经 consumes 引用),
            # 路由事实通道指向保留键 __changes__ 的回边;不写 node_done——
            # 审批轮不是一次成功执行,iterations 已计入循环上限。审计链:
            # 本次暂停的 run_paused → 审批方的 run_approval(request_changes)
            # 与变更产物在此落盘。
            payload = json.dumps({"decision": decision, "comment": comment},
                                 ensure_ascii=False)
            ref = store_artifact(
                ctx.run_dir,
                name=f"{node.id}.changes",
                filename=f"{node.id}.changes.{iteration}.json",
                content=payload.encode("utf-8"),
            )
            entry = artifact_entry(
                name=ref.name, role="changes", path=ref.path,
                sha256=ref.sha256, size_bytes=len(payload.encode("utf-8")),
                media_type="application/json")
            return {"artifacts": {ref.name: entry},
                    "route_facts": {node.id: CHANGES_EDGE_KEY},
                    "iterations": {node.id: 1}}

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
                "iterations": {node.id: 1},
                # 覆写最近一次结局:routed 二轮 approve 时必须清掉上一轮
                # 残留的 __changes__ 事实(merge_dicts 更新者胜;P3 同教训)
                "route_facts": {node.id: "ok"}}

    return run


# 节点类型的唯一分发入口;未知类型在此 KeyError 即 fail-closed
# (validate_spec 的封闭类型清单是第一道门,这里是第二道)。
_NODE_FACTORIES = {
    "llm": _make_node_fn,
    "human": _make_human_node_fn,
    "research": make_agent_node_fn,
    "coding_agent": make_agent_node_fn,
}


def _build_compiled_graph(spec: WorkflowSpec, ctx: _NodeCtx, checkpointer):
    builder = StateGraph(AtlasState)
    for n in spec.nodes:
        if n.id in ctx.reuse_plans:
            # P7:invocation 完全相等的节点跳过执行,直接复用导入产物
            # (P13:运行时输入复核不过就委托真实执行)
            builder.add_node(n.id, _make_reused_node_fn(n, spec, ctx))
        else:
            builder.add_node(n.id, _NODE_FACTORIES[n.type](n, spec, ctx))
    for e in spec.all_entries():      # 多入口:全部从 START 并行开跑
        builder.add_edge(START, e)

    outgoing: dict[str, list[EdgeSpec]] = {}
    for e in spec.edges:
        outgoing.setdefault(e.source, []).append(e)
    for src, edges in outgoing.items():
        # P3/P11:__failed__ 与 __changes__ 是保留条件边,与成功路径的边型
        # 共存——失败/要求修改由事实通道判定;成功扇出走 OK_ROUTE_KEY。
        reserved = [e for e in edges
                    if e.when in (FAILED_EDGE_KEY, CHANGES_EDGE_KEY)]
        conditional = [e for e in edges
                       if e.when is not None and e.when not in
                       (FAILED_EDGE_KEY, CHANGES_EDGE_KEY)]
        unconditional = [e for e in edges if e.when is None]
        if conditional or reserved:
            path_map = {e.when: (END if e.target == "END" else e.target)
                        for e in conditional}
            for e in reserved:
                path_map[e.when] = (END if e.target == "END" else e.target)
            ok_targets = ([END if e.target == "END" else e.target
                           for e in unconditional] if unconditional
                          else ([END] if not conditional else []))
            for i, target in enumerate(ok_targets):
                path_map[f"{OK_ROUTE_KEY}{i}"] = target
            builder.add_conditional_edges(src, _make_router(spec, src, path_map),
                                          path_map)
        else:
            for e in unconditional:
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
    if spec.summary is not None:
        # S1:总结模型与节点模型同一预检口径,坏引用在花钱前拒绝。
        registry.resolve(spec.summary.model)


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


def _post_run_retention_sweep(runs_root: Path) -> None:
    """P10 清扫钩子(默认关闭=env 未配置时零成本直返)。清扫失败只向
    stderr 大声记账,绝不影响刚完成的 run 的结果语义;单个候选删除失败
    留给下一轮重试(tombstone 语义保证重试安全)。"""
    try:
        from atlas.runs import apply_retention   # 函数内导入:runs 反向依赖 engine
        report = apply_retention(runs_root=Path(runs_root))
    except Exception as exc:
        print(f"[atlas] P10 retention 清扫失败(不影响本次运行):{exc!r}",
              file=sys.stderr)
        return
    if report is None:
        return
    failed = [r for r in report["results"] if not r["deleted"]]
    if failed:
        print(f"[atlas] P10 retention 有 {len(failed)} 个候选未清掉"
              f"(下一轮重试):{failed}", file=sys.stderr)


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
    heartbeat_interval_s: float | None = None,
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

        # ── P7 只读预检(在任何目录创建之前;失败不留 run 目录)──────
        # 存在性/静稳终态先查一遍——锁竞争与运行中源在这里就拒绝;
        # 字节复制仍放在锁内的 resolve_imports(下方,run 目录已可写)。
        from atlas.runs import precheck_imports   # 函数内导入:runs 反向依赖 engine
        precheck_imports(
            imports_spec=[imp for node in spec.nodes for imp in node.imports],
            runs_root=runs_root)

        # ── P13 fork 计划(同样在任何目录创建之前)──────────────────
        # 闭包/合成导入在这里静态定案;合成导入与显式 imports 走完全
        # 相同的 precheck/锁内复制/复核链。任务哈希直接算——store_artifact
        # 还没有 run 目录可用,而 task_ref.sha256 就是这段字节的 SHA-256。
        fork_plan = None
        synthesized_imports = []
        if spec.fork is not None:
            from atlas.fork import compute_fork_plan   # fork 依赖 engine 的身份函数
            fork_plan = compute_fork_plan(
                spec=spec, source_run=spec.fork.run, runs_root=Path(runs_root),
                task_sha256=sha256_bytes(task.encode("utf-8")),
                backend_sha256=prepared.backend_sha256)
            from atlas.spec import ArtifactImport
            synthesized_imports = [
                ArtifactImport(run=item["run"], name=item["name"])
                for item in fork_plan["imports"]]
            precheck_imports(imports_spec=synthesized_imports,
                             runs_root=runs_root)

        log = EventLog(run_dir)

        task_ref = store_artifact(
            run_dir, name="task", filename="task.txt", content=task.encode("utf-8"),
            max_bytes=TASK_MAX_BYTES,
        )

        # ── P7 artifact imports ──────────────────────────────────────
        # 启动准入在源 run stable lock 内完成(resolve_imports 持锁校验并
        # 复制);到这里失败会向上抛,run 不启动——不存在部分导入的运行。
        # P13:fork 合成导入与显式声明合并(同 run+name 去重,显式声明
        # 在先),同一源 run 只持一把锁。
        declared_imports = [imp for node in spec.nodes for imp in node.imports]
        _seen_imports = {(imp.run, imp.name) for imp in declared_imports}
        declared_imports += [imp for imp in synthesized_imports
                             if (imp.run, imp.name) not in _seen_imports]
        from atlas.runs import resolve_imports   # 函数内导入:runs 反向依赖 engine
        import_plans = resolve_imports(
            run_dir=run_dir, imports_spec=declared_imports, runs_root=runs_root)
        imported_artifacts = {plan["source_name"]: plan["ref"]
                              for plan in import_plans}
        reuse_plans = _compute_reuse_plans(
            spec, import_plans, task_sha256=task_ref.sha256,
            backend_sha256=prepared.backend_sha256)
        pending_lineage = list(import_plans)


        ctx = _NodeCtx(run_dir=run_dir, log=log, registry=prepared.registry,
                       reader=EventReader(run_dir / "events.jsonl"),
                       agent_runner=prepared.agent_runner,
                       source_baseline_tokens=MappingProxyType({
                           token.node_id: token
                           for token in prepared.source_baseline_tokens
                       }),
                       timeout_s=spec.guards.timeout_s,
                       cost_ledger=CostLedger(spec.guards.max_cost_usd),
                       heartbeat_interval_s=heartbeat_interval_s,
                       backend_identity=prepared.backend_sha256,
                       reuse_plans=reuse_plans)

        (run_dir / "spec.snapshot.json").write_text(
            json.dumps(spec_to_snapshot(spec), ensure_ascii=False, indent=1),
            encoding="utf-8")
        run_started_fields = {}
        if fork_plan is not None:
            # P13:fork 计划摘要进 run 身份事件——从第一个事件起就能对上
            # "这次执行打算复用什么"(完整清单在紧随的 fork_planned)
            run_started_fields["fork_source_run"] = fork_plan["source_run"]
            run_started_fields["fork_plan_sha256"] = fork_plan["fork_plan_sha256"]
        log.emit("run_started", graph=spec.name, run_id=run_id,
                 task_sha256=task_ref.sha256, task_path=str(task_ref.path),
                 spec_sha256=prepared.spec_sha256,
                 base_spec_sha256=base_spec_sha256 or prepared.spec_sha256,
                 effective_spec_sha256=prepared.spec_sha256,
                 prepared_execution_version=prepared.version,
                 backend_sha256=prepared.backend_sha256,
                 execution_sha256=prepared.execution_sha256,
                 bindings=list(binding_summary),
                 overrides=list(override_summary),
                 **run_started_fields)

        # P13 lineage:changed/closure/import map 全量入账,顺序确定;
        # fork 计划是"为什么跳过某些节点"的唯一权威解释。
        if fork_plan is not None:
            log.emit(
                "fork_planned", run_id=run_id,
                source_run=fork_plan["source_run"],
                source_status=fork_plan["source_status"],
                backend_equal=fork_plan["backend_equal"],
                task_equal=fork_plan["task_equal"],
                changed=fork_plan["changed"],
                closure=fork_plan["closure"],
                import_map=fork_plan["imports"],
                algo_version=fork_plan["algo_version"],
                fork_plan_sha256=fork_plan["fork_plan_sha256"])

        # P7 lineage:紧跟 run 身份落账,顺序确定;产物实体已在初始 state。
        for plan in pending_lineage:
            log.emit("artifact_imported", run_id=run_id,
                     source_run=plan["source_run"],
                     source_name=plan["source_name"],
                     source_sha256=plan["source_sha256"],
                     path=plan["ref"]["path"], sha256=plan["ref"]["sha256"],
                     algo_version=plan["algo_version"])

        initial_artifacts = {"task": task_ref.as_dict()}
        initial_artifacts.update(imported_artifacts)
        result = _invoke(
            spec, ctx, run_id, entry=prepared.entry, checkpoint=checkpoint,
            task_input={"task": task, "artifacts": initial_artifacts},
        )
        # P10:本次执行收尾后顺路清扫(默认 env 未配置=零成本直返);
        # 失败只记账不影响已完成 run。
        _post_run_retention_sweep(Path(runs_root))
        return result
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
                                          spent=_settled_spent_usd(events)),
                   backend_identity=prepared.backend_sha256)
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
    _test_only: bool = False,
) -> RunResult:
    """私有低层 checkpoint 重放(**测试专用**,非产品恢复路径)。

    它跳过 interrupted 动态判定(interrupted_only=False),任何持有 run
    目录的人都能重放;生产恢复必须走 :func:`resume_graph`(强制校验
    checkpoint、执行身份与终态集合)。默认拒绝执行,必须显式传
    ``_test_only=True`` ——把误用从"能跑"变成"当场响亮报错"。
    """
    if not _test_only:
        raise RunConflictError(
            "_resume_graph_replay 是测试专用重放(不做 interrupted 准入校验);"
            "生产恢复请使用 resume_graph")
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


def validate_approval_decision(decision: str, comment: str) -> None:
    """P11 三分支批复的领域校验(Web/MCP/engine 同一实现):
    decision 闭合枚举;request_changes 必须给出非空修改意见(锚点合同),
    否则修订回边没有可审计的输入。"""
    if decision not in APPROVAL_DECISIONS:
        raise SpecError(
            f"decision 只能是 {'/'.join(APPROVAL_DECISIONS)},得到 {decision!r}")
    if decision == "request_changes" and not str(comment or "").strip():
        raise SpecError(
            "request_changes 必须填写非空 comment(要求修改而没有意见,"
            "修订节点无从下手,也留不下审计)")


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

    decision 闭合枚举 approve/reject/request_changes(P11);reject 与
    request_changes 落 ledger 后行为不同:reject 让运行以 HumanRejected
    失败终止,request_changes 经保留键 __changes__ 回边修订后再次暂停。
    ``_lock_held`` 仅供 Web 在同步预占锁后交给后台线程继续。
    """
    run_dir = Path(runs_root) / run_id
    try:
        validate_approval_decision(decision, comment)
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
        # P11:binary 图拒绝 request_changes(枚举合法但该节点模式不支持),
        # 在写任何事件之前拦下
        if (decision == "request_changes"
                and spec.node(approval_evidence["node"]).approval_mode
                != "routed"):
            raise SpecError(
                f"human 节点 {approval_evidence['node']!r} 是 binary 审批,"
                "只认 approve/reject;要支持三分支请把图改为 "
                "approval_mode: routed 并接线 __changes__ 回边")

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
                       cost_ledger=CostLedger(spec.guards.max_cost_usd, spent=spent),
                       backend_identity=prepared.backend_sha256)
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
        # 不写种子字节:区域锁允许锁定空文件的字节范围,而并发进程的
        # "先写后锁"会撞上对方已锁的字节 0(PermissionError,同
        # config_init 的教训);锁文件保持空文件,权威状态是 OS 锁本身。
        _try_os_lock(file)
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


# ─────────────────── P2 协作式取消 ───────────────────

CANCEL_REQUEST_FILENAME = "cancel.request.json"


def write_cancel_request(run_dir: Path, *, reason: str = "") -> dict:
    """原子 create-if-absent 写取消请求;已存在时返回首个请求(幂等)。

    请求路径绝不等待 controller 的排他锁——这里只落触发器文件;
    终态由 controller 持锁写(或 paused/interrupted 时由本模块在锁内写)。
    """
    path = Path(run_dir) / CANCEL_REQUEST_FILENAME
    payload = {
        "request_id": uuid.uuid4().hex,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason or "",
    }
    try:
        handle = open(path, "x", encoding="utf-8")
    except FileExistsError:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            return {"already_requested": True, **existing}
        except Exception:
            return {"already_requested": True, "request_id": None}
    with handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
        handle.write("\n")
    return {"already_requested": False, **payload}


def request_cancel(run_id: str, *, runs_root: Path, reason: str = "",
                   active_controller: bool = False) -> dict:
    """取消请求的唯一领域入口(Web 与 atlas_cancel_run 共用)。

    语义:
    - done/failed/cancelled → RunConflictError(终态不可取消);
    - running(controller 活跃) → 只写请求,controller 在下一消费点终止,
      返回 requested+running(在途调用只能等它返回或超时,不宣称强杀);
    - paused/interrupted(无活跃 controller) → 拿运行锁,复核后在锁内写
      run_cancelled 终态(cancel 是此时唯一的合法写者)。
    """
    run_dir = Path(runs_root) / run_id
    events_path = run_dir / "events.jsonl"
    if not events_path.exists():
        raise RunNotFoundError(f"run {run_id!r} 不存在(没有 events.jsonl)")
    persisted = fold_events(EventReader(events_path).all())["status"]
    if persisted in ("done", "failed", "cancelled"):
        raise RunConflictError(
            f"run {run_id!r} 已是终态 {persisted!r},不能取消")

    request = write_cancel_request(run_dir, reason=reason)

    if persisted == "paused" or (persisted == "running"
                                 and not active_controller):
        # paused/interrupted:controller 不持锁,本调用在锁内成为唯一写者。
        try:
            acquire_run_lock(run_id, runs_root=runs_root)
        except RunConflictError:
            # 锁被短暂占用(approve/resume/另一 cancel 竞争):请求已落盘,
            # 持锁方会在自己的校验里看到非 paused/running 状态或消费请求。
            return {"status": "running", "requested": True, **request}
        try:
            latest = fold_events(
                EventReader(events_path).all())["status"]
            if latest in ("done", "failed", "cancelled"):
                raise RunConflictError(
                    f"run {run_id!r} 在取消时已变为终态 {latest!r}")
            EventLog(run_dir, continue_seq=True).emit(
                "run_cancelled", run_id=run_id,
                reason=reason or "取消请求(无活跃 controller,锁内直写终态)")
            return {"status": "cancelled", "requested": True, **request}
        finally:
            release_run_lock(run_id, runs_root=runs_root)

    # running 且 controller 活跃:请求已落盘,等 controller 消费。
    return {"status": "running", "requested": True, **request}


SUMMARY_NODE_ID = "run_summary"
SUMMARY_PROMPT_MAX_CHARS = 24_000


def _artifact_first_paragraph(path_str: str | None, *, cap: int = 200) -> str:
    """节点产物首段,压成单行作为总结与终局视图的「一句话回顾」原料。"""
    if not path_str:
        return "(无输出)"
    try:
        with open(path_str, "rb") as f:
            raw = f.read(cap * 8)   # 只读头部,60k 级产物不做全量 IO
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


def _execute_run_summary(spec: WorkflowSpec, ctx: _NodeCtx, run_id: str,
                         task_text: str) -> None:
    """S1 opt-in 总结调用:run_done 前一次 LLM 回顾,受 guards 约束。

    输入是账本派生的各节点摘要(模型/耗时/token/成本/输出首段),不是
    内存状态;输出是 run 级 write-once 产物 + run_summary_written 事件。
    预算与结算走与节点同款的 CostLedger 路径(有帽时先预留);失败由
    调用方记 run_summary_failed,run 终态不变——总结是增强,不是运行
    成败的一部分。窗口内挂 P9 心跳,派发前后闭合。
    """
    if ctx.reader.find(type="run_summary_written") is not None:
        # 崩溃窗口重放:产物与事件已落账、run_done 未写——不重复消费预算。
        return
    if ctx.cancel_requested():
        raise RunCancelled("run 总结调用前收到取消请求")
    model = spec.summary.model
    lines: list[str] = []
    for e in ctx.reader.filter(type="node_done"):
        cost = e.get("cost_usd")
        cost_label = "成本未知" if cost is None else f"${cost}"
        lines.append(
            f"- {e['node']}({e.get('model_used')},"
            f"耗时 {e.get('duration_s')}s,"
            f"tokens {e.get('input_tokens')}/{e.get('output_tokens')},"
            f"{cost_label}):{_artifact_first_paragraph(e.get('output_path'))}")
    prompt = (
        "你是工作流执行总结员。以下是一次工作流运行的事件账本回顾。"
        "请写一段执行总结:最终结果是什么,每个节点各做了什么,"
        "以及值得注意的降级/重试/成本情况。你的叙述是给人看的回顾,"
        "事实以事件账本为准,不要编造账本里没有的数字。\n")
    if spec.summary.prompt_hint:
        prompt += f"用户补充要求:{spec.summary.prompt_hint}\n"
    prompt += (f"工作流:{spec.name}\n"
               f"任务摘要(前 400 字):{(task_text or '')[:400]}\n"
               f"节点回顾(按完成顺序):\n" + "\n".join(lines))
    if len(prompt) > SUMMARY_PROMPT_MAX_CHARS:
        prompt = prompt[:SUMMARY_PROMPT_MAX_CHARS].rstrip() \
            + "\n…(回顾过长,已截断)"
    adapter, model_id = ctx.registry.resolve(model)
    projected = compute_cost_usd(
        model, len(prompt.encode("utf-8")) // 3,
        ctx.registry.default_max_output_tokens(model))
    try:
        if projected is None and spec.guards.max_cost_usd is not None:
            reservation = ctx.cost_ledger.reserve_remaining(
                description=f"run 总结({model})派发前检查")
        else:
            reservation = ctx.cost_ledger.reserve(
                projected, description=f"run 总结({model})派发前检查")
    except CostLimitError as e:
        raise CostExceeded(str(e)) from e
    if reservation is not None:
        ctx.log.emit(
            "cost_reserved", node=SUMMARY_NODE_ID, iteration=1, attempt=1,
            model=model, reservation_id=reservation.reservation_id,
            reserved_usd=reservation.amount)
    heartbeat = NodeHeartbeat(
        ctx.log, node=SUMMARY_NODE_ID, iteration=1,
        interval_s=ctx.heartbeat_interval_s, started_mono=time.monotonic())
    heartbeat.set_context(attempt=1, model=model)
    heartbeat.begin()
    try:
        resp = adapter.call(
            model_id, prompt, extra_body=None,
            timeout_s=ctx.call_timeout(None, SUMMARY_NODE_ID))
    except BaseException:
        heartbeat.end()
        raise
    heartbeat.end()
    usage = resp.usage
    actual = compute_cost_usd(
        model,
        usage.input_tokens if usage else None,
        usage.output_tokens if usage else None)
    unknown = actual is None
    exceeded = None
    try:
        accounted = ctx.cost_ledger.settle(
            reservation, actual,
            description=f"run 总结({model})结算",
            unknown_as_reserved=unknown)
    except CostLimitError as e:
        accounted = actual
        exceeded = e
    ctx.log.emit(
        "cost_settled", node=SUMMARY_NODE_ID, iteration=1, attempt=1,
        model=model,
        reservation_id=(reservation.reservation_id
                        if reservation is not None else None),
        actual_cost_usd=actual, accounted_cost_usd=accounted,
        cost_unknown=unknown, cost_usd=actual,
        input_tokens=usage.input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None)
    if exceeded is not None:
        raise CostExceeded(str(exceeded)) from exceeded
    ref = store_artifact(
        ctx.run_dir, name="run_summary", filename="run_summary.txt",
        content=resp.text.encode("utf-8"))
    ctx.log.emit(
        "run_summary_written", run_id=run_id, model=model,
        path=str(ref.path), sha256=ref.sha256,
        input_tokens=usage.input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
        cost_usd=actual)


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
        # S1 opt-in 总结:run_done 前一次账本派生的回顾调用。取消照常
        # 走消费点语义(RunCancelled 直落 run_cancelled);其余任何失败
        # 只记 run_summary_failed,不改 run 终态。
        if spec.summary is not None:
            try:
                _execute_run_summary(
                    spec, ctx, run_id, task_input.get("task", ""))
            except RunCancelled:
                raise
            except Exception as e:
                ctx.log.emit(
                    "run_summary_failed", run_id=run_id,
                    error_type=type(e).__name__, error=str(e))
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
    except RunCancelled as e:
        # P2 协作式取消的唯一终态写点:只有 controller(本函数所在线程,
        # 持有运行锁)能把取消落成 run_cancelled;在途调用已按各消费点
        # 尽力提前结束。未决 reservation 不释放为可再消费预算(保守计入)。
        try:
            ctx.log.emit("run_cancelled", run_id=run_id, reason=str(e))
        except Exception:
            pass  # 账本写不进去时保持与通用失败分支同款语义
        return RunResult(
            run_id=run_id,
            dir=ctx.run_dir,
            events=EventReader(ctx.run_dir / "events.jsonl"),
            final_state={},
            status="cancelled",
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
