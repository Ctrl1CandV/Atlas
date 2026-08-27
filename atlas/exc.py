# -*- coding: utf-8 -*-
"""P3 异常分类层:治理类永不可吞,内容类可按节点 on_error 策略化。

分类只认类型层级,不认消息文本(消息会变,类型契约稳定)。未登记的
异常类型一律按治理类处理——fail-closed:宁可终止整图,不许静默吞掉。

- 治理类(governance):费用、循环守卫、取消、run deadline、规格/接线/
  路由、完整性、账本上限、审批驳回、运行锁冲突。这些是 Atlas 自身的
  控制契约,任何 on_error 策略都不得吞掉。
- 内容类(content):候选全部失败(含假成功耗尽与节点级超时耗尽候选)。
  这是「模型没有交出合格内容」,图作者可以声明 stop/continue/branch。
- agent_cli:AgentCliError 单独分类;只有显式白名单里的子类可 soft-fail。
  当前白名单为空——baseline 冻结、diff 采集、安全扫描的错误都是治理
  错误,CLI 退出码失败也先不吞(见 ROADMAP §6)。要放开必须:子类化
  AgentCliError → 加入 SOFT_FAILABLE_AGENT_ERRORS → 补正反测试。
"""
from functools import lru_cache

GOVERNANCE = "governance"
CONTENT = "content"
AGENT_CLI = "agent_cli"

# AgentCliError 中显式白名单的子类才允许 soft-fail(当前为空,见模块 docstring)
SOFT_FAILABLE_AGENT_ERRORS: frozenset[type[BaseException]] = frozenset()


@lru_cache(maxsize=1)
def _governance_classes() -> tuple[type[BaseException], ...]:
    # 延迟导入:engine/adapters 在模块加载期导入本模块,反向依赖必须在
    # 首次分类调用时才解析(那时各模块已完整加载)。
    from atlas import adapters, engine, events, integrity, spec
    return (
        engine.CostExceeded,          # 费用守卫
        engine.GuardViolation,        # 循环上限
        engine.TimeoutViolation,      # run 级 deadline
        engine.NoRouteError,          # 路由不可判定
        engine.HumanRejected,         # 审批驳回
        engine.RunConflictError,      # 运行锁/状态冲突
        adapters.RunCancelled,        # P2 协作式取消
        spec.SpecError,               # 规格/快照
        integrity.IntegrityError,     # 产物完整性
        integrity.WiringError,        # 接线缺失
        integrity.ResourceLimitError, # 投影/产物资源上限
        events.EventLimitError,       # 账本上限
    )


@lru_cache(maxsize=1)
def _content_classes() -> tuple[type[BaseException], ...]:
    from atlas import engine
    from atlas.adapters import AllCandidatesFailed
    # engine.SearchQueriesFailed(E-1):search 节点检索失败重试耗尽,
    # 与候选全部失败同属"没有交出合格内容"的内容类。
    return (AllCandidatesFailed, engine.SearchQueriesFailed)


@lru_cache(maxsize=1)
def _agent_cli_classes() -> tuple[type[BaseException], ...]:
    from atlas.nodes.agent import AgentCliError
    return (AgentCliError,)


def classify(exc: BaseException) -> str:
    """异常 → 类别(governance/content/agent_cli)。未登记类型归治理。"""
    if isinstance(exc, _agent_cli_classes()):
        return AGENT_CLI
    if isinstance(exc, _content_classes()):
        return CONTENT
    return GOVERNANCE


def can_soft_fail(exc: BaseException) -> bool:
    """该异常是否允许按节点 on_error 策略化处理(soft-fail)。

    治理类永远 False——配置了 on_error: continue 的节点在费用/守卫/
    取消/完整性错误面前照样终止整图,这是 P3 的核心不可妥协项。
    """
    kind = classify(exc)
    if kind == CONTENT:
        return True
    if kind == AGENT_CLI:
        return isinstance(exc, tuple(SOFT_FAILABLE_AGENT_ERRORS))
    return False


def error_class_name(exc: BaseException) -> str:
    """事件/展示用的异常类名(不带模块前缀)。"""
    return type(exc).__name__
