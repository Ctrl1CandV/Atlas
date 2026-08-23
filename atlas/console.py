# -*- coding: utf-8 -*-
"""控制台输出编码兜底。

Windows 控制台代码页随系统区域设置而定（如西文系统的 cp1252）；CLI 的中文
提示在任何代码页下都必须可打印，否则 `print` 直接抛 UnicodeEncodeError。
三个控制台入口统一在启动时把标准流切到 UTF-8，errors=replace 兜底理论上的
不可编码场景。
"""
from __future__ import annotations

import sys


def force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
