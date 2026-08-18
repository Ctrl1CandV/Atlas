# -*- coding: utf-8 -*-
"""Atlas 管理命令。"""
from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from atlas.config_init import initialize_runtime_config


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas")
    sub = parser.add_subparsers(dest="command", required=True)
    init_parser = sub.add_parser("init", help="从通用模板创建缺失的本机配置")
    init_parser.add_argument("--config-dir", type=Path, default=None,
                             help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.command == "init":
        result = (initialize_runtime_config(args.config_dir)
                  if args.config_dir is not None
                  else initialize_runtime_config())
        if result.created:
            print("已创建:" + ", ".join(result.created))
        else:
            print("没有需要创建的配置。")
        if result.preserved:
            print("已保留:" + ", ".join(result.preserved))
        if result.missing_templates:
            print("缺少模板:" + ", ".join(result.missing_templates))
            return 1
        print("下一步:运行 `uv run atlas-web`，在 Web 设置页配置供应商与密钥。")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
