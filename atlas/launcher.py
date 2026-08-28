# -*- coding: utf-8 -*-
"""P4 · 进程内共享 launcher 与 controller registry。

Web 启动、MCP run(wait=false)、Web 恢复与审批续跑都经此登记,保证同一
run 在本进程内只有一个 controller 线程。registry 只是"谁在跑"的登记表,
不是第二真相源:状态与摘要永远由事件账本派生。进程重启后登记清空,
interrupted 由 P1 的动态派生(账本 + 运行锁探测)接管。
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from atlas.engine import (RunConflictError, acquire_run_lock, execute_graph,
                          new_run_id, release_run_lock)


class ControllerRegistry:
    """同进程内 run_id → controller 线程的登记表(线程安全)。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}

    def register(self, run_id: str,
                 thread: threading.Thread | None = None) -> bool:
        """登记 controller 线程;该 run 已有任何登记(含已建未启动的线程)
        时拒绝——登记即占用,防止分配窗口内的双 controller。"""
        thread = thread or threading.current_thread()
        with self._lock:
            if run_id in self._threads:
                return False
            self._threads[run_id] = thread
            return True

    def unregister(self, run_id: str) -> None:
        with self._lock:
            self._threads.pop(run_id, None)

    def is_active(self, run_id: str) -> bool:
        """登记即活跃(与旧 _run_threads 语义一致):controller 线程体退出时
        才注销,窗口期宁可多报活跃——interrupted 判定 fail-closed 到 running。"""
        with self._lock:
            return run_id in self._threads

    def active_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._threads)


REGISTRY = ControllerRegistry()


def spawn_controller(run_id: str, target: Callable[[], Any], *,
                     runs_root: Path,
                     release_lock_on_start_failure: bool = True) -> threading.Thread:
    """在登记表覆盖下启动 daemon controller 线程。

    target 自行负责执行语义(含持锁约定);本函数只保证:线程体退出时
    注销登记;登记被拒或线程启动失败时注销并(按约定)释放运行锁,
    不留半启动状态。登记被拒不释放锁的情形只有 resume/approve 的
    调用方自持锁路径——它们传 release_lock_on_start_failure=False
    并在异常处自行释放。
    """
    def _wrapped() -> None:
        try:
            target()
        finally:
            REGISTRY.unregister(run_id)

    # 先登记再启动:消灭"已分配 run_id 但查不到活跃 controller"的窗口,
    # 并把双 controller 拒绝放在 spawn 时(fail-loud,不在 daemon 线程里炸);
    # 构造/登记/启动任一失败都注销并按约定释放锁,不留半启动状态。
    try:
        thread = threading.Thread(target=_wrapped, daemon=True)
        if not REGISTRY.register(run_id, thread):
            raise RuntimeError(f"run {run_id} 已有活跃 controller,拒绝双跑")
        thread.start()
    except Exception:
        REGISTRY.unregister(run_id)
        if release_lock_on_start_failure:
            release_run_lock(run_id, runs_root=runs_root)
        raise
    return thread


def start_background_run(spec, *, task: str, runs_root: Path, registry,
                         agent_runner=None, prepared=None,
                         heartbeat_interval_s: float | None = None,
                         attachments: tuple = (),
                         base_spec_sha256: str | None = None,
                         binding_summary=(), override_summary=(),
                         logger=None, run_id: str | None = None) -> str:
    """预检与执行身份断言之后的统一后台启动路径。

    分配 run_id → 拿稳定运行锁 → 起 controller 线程执行 execute_graph
    (锁已持有)。返回 run_id;线程启动失败会释放锁并抛出。
    """
    run_id = run_id or new_run_id()
    acquire_run_lock(run_id, runs_root=runs_root)

    def _execute() -> None:
        try:
            execute_graph(
                spec, task=task, runs_root=runs_root, registry=registry,
                run_id=run_id, agent_runner=agent_runner, prepared=prepared,
                heartbeat_interval_s=heartbeat_interval_s,
                attachments=attachments,
                base_spec_sha256=base_spec_sha256,
                binding_summary=binding_summary,
                override_summary=override_summary,
                _lock_held=True)
        except Exception:
            if logger is not None:
                logger.exception("run %s 后台执行异常", run_id)

    spawn_controller(run_id, _execute, runs_root=runs_root)
    return run_id


__all__ = ["ControllerRegistry", "REGISTRY", "spawn_controller",
           "start_background_run", "RunConflictError"]
