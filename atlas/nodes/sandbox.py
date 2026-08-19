# -*- coding: utf-8 -*-
"""Windows 隔离执行后端。

源码 RC 只在 Windows 10/11 上支持 agent 节点。生产执行必须通过
Windows Sandbox 能力门;未启用、未通过 canary 或网络边界不可证明时
fail-closed,绝不退回宿主机执行 writable agent。

Windows Sandbox 没有受支持的无人值守进程管理 API,因此本模块当前只
提供严格能力检测和封闭 runner 接口。真正执行需要一次性模型代理、受控
input/output mailbox 与逐 OS build canary;这些条件任一缺失就拒绝运行。
"""
from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path


class SandboxUnavailableError(RuntimeError):
    """当前主机不能证明 agent 可在受控 OS 沙箱中执行。"""


@dataclass(frozen=True)
class SandboxCapability:
    available: bool
    code: str
    message: str
    launcher: str | None = None
    os_build: str | None = None


_SANDBOX_LAUNCHERS = (
    Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsSandbox.exe",
    Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsSandboxClient.exe",
)


def detect_windows_sandbox() -> SandboxCapability:
    """零副作用能力检测;不提升权限、不自动启用 Windows 可选功能。"""
    if os.name != "nt":
        return SandboxCapability(
            False, "UNSUPPORTED_PLATFORM",
            "Atlas v0.1.0 的 agent 沙箱仅支持 Windows 10/11 x64。",
            os_build=platform.version())
    if platform.machine().lower() not in {"amd64", "x86_64"}:
        return SandboxCapability(
            False, "UNSUPPORTED_ARCH",
            f"Windows Sandbox 仅支持 x64,当前架构:{platform.machine()}",
            os_build=platform.version())

    launcher = next((str(path) for path in _SANDBOX_LAUNCHERS if path.is_file()), None)
    if launcher is None:
        return SandboxCapability(
            False, "SANDBOX_UNAVAILABLE",
            "未检测到 Windows Sandbox。请以管理员身份启用 "
            "Containers-DisposableClientVM、重启,再运行发布前 canary。",
            os_build=platform.version())

    # launcher 只证明 Windows Sandbox 可能已安装，不能证明 Atlas 拥有可靠的
    # guest 进程管理、退出码回传、一次性 broker、mailbox 或网络隔离。普通用户
    # 可写 marker 不能成为授权依据；在这些能力真正实现并审计前始终 fail-closed。
    return SandboxCapability(
        False, "SANDBOX_BACKEND_NOT_PROVISIONED",
        "检测到 Windows Sandbox launcher，但 Atlas v0.1.0 尚未提供经审计的 "
        "guest toolchain、一次性模型代理和可靠 mailbox runner。",
        launcher=launcher, os_build=platform.version())


def require_windows_sandbox(*, allow_web: bool) -> SandboxCapability:
    if allow_web:
        raise SandboxUnavailableError(
            "SANDBOX_WEB_DISABLED:源码 RC 尚未提供可审计的通用 Web egress 代理;"
            "agent 的 allow_web=true 被发布安全策略拒绝。")
    capability = detect_windows_sandbox()
    if not capability.available:
        raise SandboxUnavailableError(f"{capability.code}:{capability.message}")
    return capability


def sandbox_runner(attachment: Path, *, node_type: str, max_turns: int,
                   cwd: Path | None = None, writable: bool = True,
                   allow_web: bool = False, allowed_paths: list[str] | None = None,
                   timeout_s: float | None = None, model_ref: str = "") -> str:
    """生产 agent runner 的唯一入口。

    当前 RC 只有在受控 broker/canary 已部署时才会进入执行阶段。由于本机和
    CI 尚未具备该环境,这里故意 fail-closed;不能以宿主 CLI 冒充沙箱。
    """
    capability = require_windows_sandbox(allow_web=allow_web)
    raise SandboxUnavailableError(
        "SANDBOX_BACKEND_NOT_PROVISIONED:能力门已通过,但本构建未包含经审计的 "
        f"guest toolchain/mailbox runner(OS build {capability.os_build});拒绝宿主回退。")
