# -*- coding: utf-8 -*-
"""M0 真实两节点图:writer(Deepseek,openai 端点) → reviewer(SuperAI,anthropic 端点)。

花钱(约几美分)。跑完自动做 A1 语义自检并打印运行摘要。
账本落 runs/<id>/(事件流 + 产物 + 投影原文)。
"""
import sys

from atlas.config import PROJECT_ROOT
from atlas.engine import execute_graph
from atlas.m0_graph import TASK, m0_registry, m0_spec, self_check


def main() -> None:
    run = execute_graph(m0_spec(), task=TASK,
                        runs_root=PROJECT_ROOT / "runs",
                        registry=m0_registry())
    summary = self_check(run)

    print("=== M0 real two-node run ===")
    print(f"run dir : {summary['run_dir']}")
    print(f"writer  : {summary['writer_chars']} chars -> "
          f"projection {summary['projection_chars']} chars (A1 containment OK)")
    for n in summary["nodes"]:
        print(f"  {n['node']:>8}: {n['model_used']} degraded={n['degraded']} "
              f"in={n['input_tokens']} out={n['output_tokens']} {n['duration_s']}s")
    print(f"distinct vendors: {summary['models_distinct']}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
