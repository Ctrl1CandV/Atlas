# -*- coding: utf-8 -*-
"""M0 第一步:确认 LangGraph 在 Windows 上能装能跑。

两件事:
1. 官方最简例子(StateGraph / START / END / compile / invoke)
2. Atlas 状态设计依赖的 reducer 语义:两个节点都往同一个 dict 键写,
   必须合并而不是覆盖——否则节点 B 返回时会把节点 A 的产物表丢掉。
"""
import sys
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph


def merge_dicts(base: dict | None, update: dict | None) -> dict:
    merged = dict(base or {})
    merged.update(update or {})
    return merged


class State(TypedDict, total=False):
    foo: str
    artifacts: Annotated[dict, merge_dicts]


def main() -> None:
    # --- 1. 官方最简例子 ---
    builder = StateGraph(State)
    builder.add_node("hello", lambda s: {"foo": f"{s['foo']}!"})
    builder.add_edge(START, "hello")
    builder.add_edge("hello", END)
    app = builder.compile()
    result = app.invoke({"foo": "hello"})
    assert result["foo"] == "hello!", result
    print(f"[1] minimal graph ok: {result['foo']}")

    # --- 2. reducer 合并语义 ---
    builder = StateGraph(State)
    builder.add_node("a", lambda s: {"artifacts": {"a.output": "AAA"}})
    builder.add_node("b", lambda s: {"artifacts": {"b.output": "BBB"}})
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    builder.add_edge("b", END)
    app = builder.compile()
    result = app.invoke({"foo": "x"})
    merged = result["artifacts"]
    assert merged == {"a.output": "AAA", "b.output": "BBB"}, (
        f"reducer 未生效,节点 B 的返回覆盖了节点 A 的产物表: {merged}"
    )
    print(f"[2] dict-reducer merge ok: {merged}")

    print("langgraph smoke: ALL OK")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
