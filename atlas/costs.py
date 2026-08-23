# -*- coding: utf-8 -*-
"""成本计算:input/output token × 单价。

纪律:拿不到费率就记 null,不填猜的数字(pricing.json 全 null 起步,
等确认过的数字填进去,守卫自然生效)。
"""
import json
import math
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from atlas.config import CONFIG_DIR

_PRICING_PATH = CONFIG_DIR / "pricing.json"
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_PRICING_PATH.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            _cache = {"prices": {}}
    return _cache.get("prices", {})


def _nonnegative_finite_number(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _valid_token_count(value: object) -> bool:
    return value is None or (
        isinstance(value, int) and not isinstance(value, bool) and value >= 0)


def compute_cost_usd(model_ref: str, input_tokens: int | None,
                     output_tokens: int | None) -> float | None:
    """按费率表算一次调用的成本。费率或 token 不可信 → None(不猜)。"""
    if not _valid_token_count(input_tokens) or not _valid_token_count(output_tokens):
        return None
    if input_tokens is None and output_tokens is None:
        return None
    prices = _load()
    provider, _, model = model_ref.partition(":")
    entry = (prices.get(model_ref)
             or prices.get(f"{provider}:*")
             or prices.get(provider))
    if not isinstance(entry, dict):
        return None
    in_per_m = _nonnegative_finite_number(entry.get("input_per_m"))
    out_per_m = _nonnegative_finite_number(entry.get("output_per_m"))
    if in_per_m is None or out_per_m is None:
        return None
    try:
        cost = ((input_tokens or 0) / 1_000_000 * in_per_m
                + (output_tokens or 0) / 1_000_000 * out_per_m)
    except OverflowError:
        return None
    return round(cost, 6) if math.isfinite(cost) else None


class CostLimitError(Exception):
    """预留或实际结算超过运行成本上限。"""


@dataclass(frozen=True)
class CostReservation:
    amount: float
    reservation_id: str


@dataclass(frozen=True)
class CostAccounting:
    """事件重放后的成本视图；accounted 是守卫采用的保守金额。"""

    known_actual_usd: float
    accounted_usd: float
    unknown_count: int
    outstanding_reserved_usd: float


def fold_cost_accounting(events: list[dict]) -> CostAccounting:
    """重放 reservation 状态机；遗留 pending 按全额未知费用结算。"""
    pending: dict[str, float] = {}
    settled_ids: set[str] = set()
    settled_nodes: set[tuple[object, object]] = set()
    known_actual = 0.0
    accounted = 0.0
    unknown_count = 0

    for event in events:
        kind = event.get("type")
        reservation_id = event.get("reservation_id")
        if kind == "cost_reserved" and isinstance(reservation_id, str):
            amount = _nonnegative_finite_number(event.get("reserved_usd"))
            if amount is not None and amount > 0 and reservation_id not in settled_ids:
                pending.setdefault(reservation_id, amount)
        elif kind == "cost_settled":
            raw_actual = event.get("actual_cost_usd", event.get("cost_usd"))
            raw_guarded = event.get("accounted_cost_usd", raw_actual)
            actual = _nonnegative_finite_number(raw_actual)
            guarded = _nonnegative_finite_number(raw_guarded)
            # 损坏的结算不得释放已持久化 reservation。actual 显式存在时
            # 必须可信；未知结算则必须明确标记并携带有限非负 accounted。
            valid_settlement = (
                (raw_actual is not None and actual is not None
                 and guarded is not None)
                or (raw_actual is None and event.get("cost_unknown") is True
                    and guarded is not None)
            )
            if isinstance(reservation_id, str):
                if reservation_id in settled_ids:
                    continue
                if not valid_settlement:
                    continue
                settled_ids.add(reservation_id)
                pending.pop(reservation_id, None)
            settled_nodes.add((event.get("node"), event.get("iteration")))
            if actual is not None:
                known_actual += actual
            if guarded is not None:
                accounted += guarded
            if event.get("cost_unknown") is True:
                unknown_count += 1
        elif (kind == "cost_unknown"
              and not isinstance(reservation_id, str)
              and event.get("attempt") is None):
            # 旧 LLM 账本只有独立 warning。新 agent warning 带 attempt，
            # 同一次调用的 cost_settled(cost_unknown=true) 才是唯一计数事实。
            unknown_count += 1

    # 兼容没有 cost_settled 的旧运行：node_done.cost_usd 是当时唯一成本事实。
    for event in events:
        if event.get("type") != "node_done":
            continue
        if (event.get("node"), event.get("iteration")) in settled_nodes:
            continue
        legacy = _nonnegative_finite_number(event.get("cost_usd"))
        if legacy is not None:
            known_actual += legacy
            accounted += legacy

    # 调用可能已抵达供应商但进程在 settlement 事件前崩溃；不得释放重用。
    outstanding = sum(pending.values())
    accounted += outstanding
    unknown_count += len(pending)
    return CostAccounting(
        known_actual_usd=round(known_actual, 6),
        accounted_usd=round(accounted, 6),
        unknown_count=unknown_count,
        outstanding_reserved_usd=round(outstanding, 6),
    )


class CostLedger:
    """线程安全的运行级成本预留—结算账本。"""

    def __init__(self, cap: float | None, *, spent: float = 0.0) -> None:
        normalized_cap = (_nonnegative_finite_number(cap)
                          if cap is not None else None)
        normalized_spent = _nonnegative_finite_number(spent)
        if cap is not None and (normalized_cap is None or normalized_cap == 0):
            raise ValueError("成本上限必须是正的有限数")
        if normalized_spent is None:
            raise ValueError("已花费金额必须是非负有限数")
        self.cap = normalized_cap
        self._spent = normalized_spent
        self._pending: dict[str, float] = {}
        self._lock = threading.Lock()

    def _new_reservation(self, amount: float) -> CostReservation:
        reservation = CostReservation(amount, uuid.uuid4().hex)
        self._pending[reservation.reservation_id] = amount
        return reservation

    def reserve(self, projected: float | None, *, description: str) -> CostReservation | None:
        if self.cap is None or projected is None:
            return None
        amount = _nonnegative_finite_number(projected)
        if amount is None:
            raise CostLimitError(f"{description}:本次预估成本不是非负有限数")
        with self._lock:
            reserved = sum(self._pending.values())
            if self._spent + reserved + amount > self.cap:
                raise CostLimitError(
                    f"{description}:已花费 ${self._spent:.4f} + 已预留 "
                    f"${reserved:.4f} + 本次预估 ${amount:.4f} > "
                    f"guards.max_cost_usd=${self.cap}"
                )
            return self._new_reservation(amount)

    def reserve_remaining(self, *, description: str) -> CostReservation | None:
        """原子预留当前全部剩余预算，供无法预估费用的 CLI 会话使用。"""
        if self.cap is None:
            return None
        with self._lock:
            reserved = sum(self._pending.values())
            remaining = self.cap - self._spent - reserved
            if remaining <= 0:
                raise CostLimitError(
                    f"{description}:已花费 ${self._spent:.4f} + 已预留 "
                    f"${reserved:.4f}，没有剩余预算")
            return self._new_reservation(remaining)

    def settle(self, reservation: CostReservation | None,
               actual: float | None, *, description: str,
               unknown_as_reserved: bool = False) -> float | None:
        """幂等结算并返回守卫计入金额；未知费用可按预留额保守结算。"""
        valid_actual = (_nonnegative_finite_number(actual)
                        if actual is not None else None)
        if self.cap is None:
            return valid_actual
        with self._lock:
            accounted = valid_actual
            if reservation is not None:
                amount = self._pending.get(reservation.reservation_id)
                if amount is None:
                    return None
                if actual is not None and valid_actual is None:
                    accounted = amount
                elif unknown_as_reserved and accounted is None:
                    accounted = amount
                self._pending.pop(reservation.reservation_id, None)
            elif actual is not None and valid_actual is None:
                raise CostLimitError(f"{description}:实际成本不是非负有限数")
            if accounted is not None:
                self._spent += accounted
                if self._spent > self.cap:
                    raise CostLimitError(
                        f"{description}:实际累计成本 ${self._spent:.4f} > "
                        f"guards.max_cost_usd=${self.cap}"
                    )
            return accounted

    @property
    def totals(self) -> tuple[float, float]:
        with self._lock:
            return self._spent, sum(self._pending.values())


def rates_known(model_ref: str) -> bool:
    """费率表能否算出该模型的成本(通配/供应商级默认也算已知)。

    dry-run 的成本帽警告(C1)用它判断"哪些计费节点费率未知";
    与 compute_cost_usd 同一套解析规则,不另立口径。
    """
    return compute_cost_usd(model_ref, 1_000_000, 1_000_000) is not None


def reload_pricing() -> None:
    global _cache
    _cache = None
