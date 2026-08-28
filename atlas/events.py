# -*- coding: utf-8 -*-
"""事件流:append-only JSONL,每次运行的唯一真相。

写侧每条事件立即 flush,保证界面(SSE)能实时 tail;
读侧只做全量加载后过滤——事件量级在千条内,不值得更复杂。

fold_events 是 A6 的被测对象:任何时刻,从事件流重放出的状态
必须与运行时的最终状态一致。派生数据(摘要/视图/缓存)可以随时
丢弃重建,事件流不能。
"""
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path


EVENT_FILE_MAX_BYTES = 16 * 1024 * 1024
EVENT_RECORD_MAX_BYTES = 1 * 1024 * 1024


class EventLimitError(Exception):
    """事件或事件账本超过显式资源上限。"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _parse_records(data: bytes) -> tuple[list[dict], int]:
    """按字节逐行解析,返回 (完整记录列表, 最后一条完整记录的字节偏移)。

    偏移必须是字节(中文事件里字符偏移 ≠ 字节偏移,truncate 吃的是字节)。
    尾部撕裂行(进程写到一半被 kill)不算数——它的偏移不前进,
    续写时会被截掉,保证账本永远是「完整行 + 可能的撕裂尾」。
    """
    records: list[dict] = []
    good_offset = 0
    pos = 0
    for raw_line in data.split(b"\n"):
        end = pos + len(raw_line) + 1
        stripped = raw_line.strip()
        if stripped:
            try:
                records.append(json.loads(stripped.decode("utf-8")))
                good_offset = min(end, len(data))
            except (json.JSONDecodeError, UnicodeDecodeError):
                break  # 撕裂/损坏行:后面不再可信
        pos = end
    return records, good_offset


def _last_good_seq_and_offset(path: Path) -> tuple[int, int]:
    """最后一个完整事件的 seq 与它的字节偏移。撕裂尾被忽略。"""
    if not path.exists():
        return 0, 0
    records, good_offset = _parse_records(path.read_bytes())
    last_seq = records[-1].get("seq", 0) if records else 0
    return last_seq, good_offset


class EventLog:
    """append-only 写入器。线程安全:并行节点也不会串行号。

    continue_seq=True 用于续跑:先截掉上次进程留下的撕裂尾行,
    序号从最后一个完整事件继续——账本保持单调,?after=N 不会漏事件。
    """

    def __init__(self, run_dir: Path, *, continue_seq: bool = False) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        self.path = run_dir / "events.jsonl"
        self._seq = 0
        self._lock = threading.Lock()
        if continue_seq and self.path.exists():
            last_seq, good_offset = _last_good_seq_and_offset(self.path)
            self._seq = last_seq
            with open(self.path, "r+b") as f:
                f.truncate(good_offset)   # 去掉撕裂尾,续写不会拼进坏行

    def emit(self, event_type: str, **fields) -> dict:
        with self._lock:
            next_seq = self._seq + 1
            record = {"seq": next_seq, "ts": _utc_now_iso(),
                      "type": event_type, **fields}
            encoded = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
            if len(encoded) > EVENT_RECORD_MAX_BYTES:
                raise EventLimitError(
                    f"事件 {event_type!r} 体积 {len(encoded)} 字节超过单事件上限 "
                    f"{EVENT_RECORD_MAX_BYTES} 字节;拒绝截断")
            current = self.path.stat().st_size if self.path.exists() else 0
            if current + len(encoded) > EVENT_FILE_MAX_BYTES:
                raise EventLimitError(
                    f"事件账本将超过上限 {EVENT_FILE_MAX_BYTES} 字节;拒绝截断")
            with open(self.path, "ab") as f:
                f.write(encoded)
                f.flush()
            self._seq = next_seq
        return record


class EventReader:
    """读侧。find/filter 按**顶层字段**精确匹配;撕裂尾行被跳过。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def all(self) -> list[dict]:
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size > EVENT_FILE_MAX_BYTES:
            raise EventLimitError(
                f"事件账本体积 {size} 字节超过上限 {EVENT_FILE_MAX_BYTES} 字节")
        records, _ = _parse_records(self.path.read_bytes())
        return records

    def read_from(self, offset: int = 0) -> tuple[list[dict], int]:
        """从字节偏移增量读取完整事件；撕裂尾不推进偏移。"""
        if not self.path.exists():
            return [], offset
        size = self.path.stat().st_size
        if size > EVENT_FILE_MAX_BYTES:
            raise EventLimitError(
                f"事件账本体积 {size} 字节超过上限 {EVENT_FILE_MAX_BYTES} 字节")
        if offset < 0 or offset > size:
            offset = 0
        with open(self.path, "rb") as f:
            f.seek(offset)
            data = f.read()
        records, good = _parse_records(data)
        return records, offset + good

    def filter(self, **match) -> list[dict]:
        return [
            r for r in self.all()
            if all(r.get(k) == v for k, v in match.items())
        ]

    def find(self, **match) -> dict | None:
        for r in self.all():
            if all(r.get(k) == v for k, v in match.items()):
                return r
        return None


def fold_events(records: list[dict]) -> dict:
    """从事件流重放出运行状态(A6)。

    只依赖事件本身,不读任何派生缓存;对同一份输入是纯函数。
    """
    from atlas.artifacts import artifacts_from_event

    state = {
        "run_id": None,
        "graph": None,
        "task_sha256": None,
        "spec_sha256": None,
        "status": "pending",
        "artifacts": {},   # 逻辑名 → 类型化产物条目(含 name/path/sha256,与 ArtifactRef 同构)
        "nodes_done": [],  # 每次节点完成的 node id,按完成顺序(循环会出现多次)
    }
    for r in records:
        t = r.get("type")
        if t == "run_started":
            state["run_id"] = r.get("run_id")
            state["graph"] = r.get("graph")
            state["task_sha256"] = r.get("task_sha256")
            state["spec_sha256"] = r.get("spec_sha256")
            if r.get("task_path") and r.get("task_sha256"):
                state["artifacts"]["task"] = {
                    "name": "task", "path": r["task_path"],
                    "sha256": r["task_sha256"],
                }
            state["status"] = "running"
        elif t == "run_resumed":
            state["status"] = "running"
        elif t == "node_progress":
            # P9 controller 心跳与 agent 阶段标记:纯活性信号,不携带影响
            # 终态/产物的语义。fold 显式忽略;删掉这些事件后重放结果必须
            # 与保留时完全一致(回归测试锁定,见 test_p9_heartbeat)。
            pass
        elif t == "run_summary_written":
            # S1:总结产物增强可读性,读取走专用事件/产物路径,不入 fold
            # 状态——删掉它,fold 结果必须与保留时一致(回归测试锁定)。
            pass
        elif t == "run_summary_failed":
            # S1:总结失败不改 run 终态(锚点合同);run_done/run_failed
            # 照常由原有写点决定。
            pass
        elif t == "node_failed_soft":
            # P3 软失败:节点按 on_error 策略继续/分支,不是 run 失败。
            # fold 显式忽略;删掉该事件后重放结果必须与保留时一致
            # (反例回归锁定,见 test_p3_on_error)。
            pass
        elif t in ("artifact_imported", "node_imported_reused"):
            # P7 导入血缘与复用标记:产物经字节克隆进本 run 产物库、随
            # task_input 初始 state 流转,不携带终态语义;删掉这些事件后
            # fold 结果必须与保留时一致(回归锁定,见 test_p7_import)。
            pass
        elif t == "attachment_admitted":
            # E-2A 运行附件准入:附件实体经字节克隆进产物库、随初始
            # state 流转,与导入同性质——不携带终态语义。删掉该事件后
            # fold 结果必须与保留时一致(回归锁定,见 test_e2a_attachments)。
            pass
        elif t == "fork_planned":
            # P13 fork 计划(changed/closure/import map):计划本身的
            # 执行结果由后续 node_imported_reused/node_done 体现;删掉
            # 该事件后 fold 结果必须与保留时一致(回归锁定,见
            # test_p13_fork)。
            pass
        elif t == "search_performed":
            # E-1 search 执行事实:每查询一条的活性/审计记录,真正的结果
            # 由 node_done 产物体现——不携带终态语义。fold 显式忽略;删掉
            # 这些事件后 fold 结果必须与保留时一致(回归锁定,见
            # test_e1_search 的 fold 用例)。
            pass
        elif t == "run_paused":
            state["status"] = "paused"
        elif t == "node_done":
            node = r.get("node")
            # 类型化产物:新事件带 artifacts 数组;旧事件按 output/diff 字段合成。
            # 条目进 state 时以逻辑名(name=node.output/node.diff)为键——
            # consumes 引用的就是这些名字,执行层契约不变。
            entries = artifacts_from_event(r)
            for entry in entries:
                state["artifacts"][entry["name"]] = entry
            if not any(e["name"] == f"{node}.output" for e in entries):
                # 只有事件真的带 output 时才注入(None 路径会让下游
                # ArtifactRef.from_dict 抛 TypeError;缺产物该走 WiringError)
                if r.get("output_path"):
                    state["artifacts"][f"{node}.output"] = {
                        "name": f"{node}.output",
                        "path": r.get("output_path"),
                        "sha256": r.get("output_sha256"),
                    }
            state["nodes_done"].append(node)
        elif t == "run_done":
            state["status"] = "done"
        elif t == "run_failed":
            state["status"] = "failed"
        elif t == "run_cancelled":
            # P2 协作式取消的终态;只有 controller(或锁内的 cancel 入口)写。
            state["status"] = "cancelled"
    return state
