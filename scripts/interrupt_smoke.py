# -*- coding: utf-8 -*-
"""M2 前置实验:验证 LangGraph interrupt 的「暂停—重启进程—恢复」真的可行。

ARCHITECTURE 第 8 节标了这个为未实测风险(HITL 的 human 节点依赖它),
开放 bug 集中在「循环里 interrupt 导致重复 resume」「时间旅行恢复不正确」。
本实验验证最简形态:线性图 A →(interrupt)→ B,进程重启后(用新的
sqlite 连接模拟)用 Command(resume=...) 继续跑完。

如果不可行,退路:把图拆成两段(架构第 8 节)。
"""
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


def merge_dicts(a, b):
    return {**(a or {}), **(b or {})}


class S(TypedDict, total=False):
    step: str
    arts: Annotated[dict, merge_dicts]


def node_a(state):
    return {"step": "a-done", "arts": {"a": 1}}


def human_gate(state):
    # 暂停点:等人在界面上批准
    answer = interrupt({"question": "批准继续吗?", "got": state.get("arts")})
    if answer != "approved":
        raise RuntimeError(f"被驳回:{answer}")
    return {"step": "approved"}


def node_b(state):
    return {"step": "b-done", "arts": {"b": 2}}


def build(checkpointer):
    b = StateGraph(S)
    b.add_node("a", node_a)
    b.add_node("gate", human_gate)
    b.add_node("b", node_b)
    b.add_edge(START, "a")
    b.add_edge("a", "gate")
    b.add_edge("gate", "b")
    b.add_edge("b", END)
    return b.compile(checkpointer=checkpointer)


def main():
    db = Path(tempfile.mkdtemp()) / "ckpt.sqlite"
    cfg = {"configurable": {"thread_id": "t1"}}

    # ── 进程 1:跑到暂停点 ──
    conn = sqlite3.connect(db, check_same_thread=False)
    app = build(SqliteSaver(conn))
    result = app.invoke({"step": "start"}, cfg)
    # invoke 在 interrupt 处返回;结果里有 __interrupt__ 元数据
    snaps = app.get_state(cfg)
    interrupted = snaps.next  # 待执行任务里应有 gate 的 resume
    print("after invoke: next =", snaps.next, "| values =", snaps.values)
    assert interrupted, "期望在 gate 处暂停"
    conn.close()  # 模拟进程死亡

    # ── 进程 2:新连接,恢复 ──
    conn2 = sqlite3.connect(db, check_same_thread=False)
    app2 = build(SqliteSaver(conn2))
    result2 = app2.invoke(Command(resume="approved"), cfg)
    print("after resume:", result2)
    assert result2["step"] == "b-done", result2
    assert result2["arts"] == {"a": 1, "b": 2}, result2
    assert app2.get_state(cfg).next == (), "应已跑完"
    conn2.close()

    # ── 进程 3:驳回路径 ──
    conn3 = sqlite3.connect(db, check_same_thread=False)
    app3 = build(SqliteSaver(conn3))
    cfg3 = {"configurable": {"thread_id": "t3"}}
    app3.invoke({"step": "start"}, cfg3)
    try:
        app3.invoke(Command(resume="rejected"), cfg3)
        print("rejected path: 没有抛异常(实现需自行检查 answer)")
    except RuntimeError as e:
        print("rejected path:", e)
    conn3.close()

    print("interrupt smoke: ALL OK")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
