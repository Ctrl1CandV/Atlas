# -*- coding: utf-8 -*-
"""首次启动的本机配置初始化。

只从仓库内的通用 example 创建缺失的 active 文件；已存在文件永不覆盖。
共享函数不向 stdout 写内容，避免破坏 MCP stdio 协议。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from atlas.config import CONFIG_DIR, atomic_write_text

CONFIG_TEMPLATES = (
    ("providers.example.json", "providers.json"),
    ("models.reference.example.json", "models.reference.json"),
    ("capabilities.example.json", "capabilities.json"),
    ("pricing.example.json", "pricing.json"),
    ("agents.example.json", "agents.json"),
    (".env.example", ".env"),
)
_NOTICE_NAME = ".atlas-init-notice.json"
_JOURNAL_NAME = ".atlas-init-journal.json"
_LOCK_NAME = ".atlas-init.lock"
_STAGE_PREFIX = ".atlas-init-stage-"
_NOTICE_LOCK = threading.RLock()
_ACTIVE_NAMES = frozenset(active for _, active in CONFIG_TEMPLATES)


@dataclass(frozen=True)
class InitResult:
    created: tuple[str, ...]
    preserved: tuple[str, ...]
    missing_templates: tuple[str, ...]


@contextmanager
def _initialization_lock(config_dir: Path) -> Iterator[None]:
    """取得进程内锁和稳定文件上的 OS 排他锁。

    锁文件只创建、不写内容、不删除:Windows/POSIX 的区域锁都允许锁定
    空文件的字节范围。此前"先写种子字节再上锁"在两个进程并发初始化时,
    后来者的写会撞上前者已锁的字节 0 → PermissionError [Errno 13]
    (2026-08-23 公开 CI 两次实证);不写就不会撞。
    """
    root = Path(config_dir)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / _LOCK_NAME
    with _NOTICE_LOCK:
        with lock_path.open("a+b") as lock_file:
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _copy_to_stage(source: Path, root: Path) -> str:
    """把模板持久化到同目录 staging 文件，返回安全的 basename。"""
    fd, tmp_name = tempfile.mkstemp(
        dir=root, prefix=_STAGE_PREFIX, suffix=".tmp")
    stage = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(source.read_bytes())
            out.flush()
            os.fsync(out.fileno())
    except Exception:
        stage.unlink(missing_ok=True)
        raise
    return stage.name


def _copy_bytes_if_absent(source: Path, target: Path) -> bool:
    """原子 create-if-absent；并发创建者只有一个能链接成功。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / _copy_to_stage(source, target.parent)
    try:
        try:
            os.link(stage, target)
            return True
        except FileExistsError:
            return False
    finally:
        stage.unlink(missing_ok=True)


def _validate_event(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("初始化提示事件必须是 JSON 对象")
    event_id = value.get("event_id")
    created = value.get("created")
    if (not isinstance(event_id, str) or not event_id
            or not isinstance(created, list)
            or not all(isinstance(name, str) and name for name in created)):
        raise ValueError("初始化提示文件字段不合法")
    return {"event_id": event_id, "created": list(dict.fromkeys(created))}


def _load_notice_events_locked(config_dir: Path) -> list[dict]:
    path = config_dir / _NOTICE_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as e:
        raise ValueError(f"初始化提示文件不是合法 JSON:{e}") from e
    if not isinstance(payload, dict):
        raise ValueError("初始化提示文件必须是 JSON 对象")
    if "events" not in payload:
        # 升级时接受旧的单槽格式；下一次写入会迁移为队列。
        return [_validate_event(payload)]
    events = payload.get("events")
    if payload.get("version") != 1 or not isinstance(events, list):
        raise ValueError("初始化提示队列字段不合法")
    validated = [_validate_event(event) for event in events]
    ids = [event["event_id"] for event in validated]
    if len(ids) != len(set(ids)):
        raise ValueError("初始化提示队列包含重复 event_id")
    return validated


def _store_notice_events_locked(config_dir: Path, events: list[dict]) -> None:
    payload = {"version": 1, "events": events}
    atomic_write_text(
        config_dir / _NOTICE_NAME,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def _append_notice_locked(
    config_dir: Path, created: tuple[str, ...], event_id: str,
) -> None:
    names = sorted(set(created))
    if not names:
        return
    events = _load_notice_events_locked(config_dir)
    existing = next(
        (event for event in events if event["event_id"] == event_id), None)
    if existing is not None:
        if existing["created"] != names:
            raise ValueError("同一初始化 event_id 对应了不同文件")
        return
    events.append({"event_id": event_id, "created": names})
    _store_notice_events_locked(config_dir, events)


def _write_notice(
    config_dir: Path, created: tuple[str, ...], *, event_id: str | None = None,
) -> None:
    """向 notice 队列追加事件；相同 event_id 重试不会产生重复事件。"""
    if not created:
        return
    root = Path(config_dir)
    with _initialization_lock(root):
        _append_notice_locked(root, created, event_id or uuid.uuid4().hex)


def _validate_journal(payload: object) -> tuple[str, list[tuple[str, str]]]:
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("初始化 journal 字段不合法")
    event_id = payload.get("event_id")
    raw_entries = payload.get("entries")
    if not isinstance(event_id, str) or not event_id or not isinstance(raw_entries, list):
        raise ValueError("初始化 journal 字段不合法")
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise ValueError("初始化 journal 条目不合法")
        active = raw.get("active")
        stage = raw.get("stage")
        if (active not in _ACTIVE_NAMES or active in seen
                or not isinstance(stage, str)
                or Path(stage).name != stage
                or not stage.startswith(_STAGE_PREFIX)
                or not stage.endswith(".tmp")):
            raise ValueError("初始化 journal 条目不合法")
        seen.add(active)
        entries.append((active, stage))
    if not entries:
        raise ValueError("初始化 journal 不能为空")
    return event_id, entries


def _load_journal_locked(root: Path) -> tuple[str, list[tuple[str, str]]] | None:
    path = root / _JOURNAL_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        raise ValueError(f"初始化 journal 不是合法 JSON:{e}") from e
    return _validate_journal(payload)


def _store_journal_locked(
    root: Path, event_id: str, entries: list[tuple[str, str]],
) -> None:
    payload = {
        "version": 1,
        "event_id": event_id,
        "entries": [
            {"active": active, "stage": stage} for active, stage in entries
        ],
    }
    atomic_write_text(
        root / _JOURNAL_NAME,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def _same_file(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except (FileNotFoundError, OSError):
        return False


def _complete_journal_locked(root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """完成或恢复 journal，并在清理 journal 前幂等发布 notice。"""
    journal = _load_journal_locked(root)
    if journal is None:
        return (), ()
    event_id, entries = journal
    events = _load_notice_events_locked(root)
    published = next(
        (event for event in events if event["event_id"] == event_id), None)
    if published is not None:
        expected = sorted(published["created"])
        allowed = sorted(active for active, _ in entries)
        if any(name not in allowed for name in expected):
            raise ValueError("初始化 journal 与已发布提示不一致")
        for _, stage_name in entries:
            (root / stage_name).unlink(missing_ok=True)
        (root / _JOURNAL_NAME).unlink(missing_ok=True)
        return (), ()

    created: list[str] = []
    preserved: list[str] = []
    for active_name, stage_name in entries:
        active = root / active_name
        stage = root / stage_name
        if not stage.is_file():
            raise ValueError("初始化 journal 引用的 staging 文件不存在")
        if not active.exists():
            try:
                os.link(stage, active)
            except FileExistsError:
                pass
        # 只有 staging 的硬链接才证明是 Atlas 创建；竞争写入的用户文件保留且不误报。
        if _same_file(stage, active):
            created.append(active_name)
        else:
            preserved.append(active_name)

    _append_notice_locked(root, tuple(created), event_id)
    for _, stage_name in entries:
        (root / stage_name).unlink(missing_ok=True)
    (root / _JOURNAL_NAME).unlink(missing_ok=True)
    return tuple(created), tuple(preserved)


def initialize_runtime_config(
    config_dir: Path = CONFIG_DIR, *, write_notice: bool = True,
) -> InitResult:
    """从同目录模板补齐本机配置；幂等且绝不覆盖 active 文件。"""
    root = Path(config_dir)
    created: list[str] = []
    preserved: list[str] = []
    missing: list[str] = []
    with _initialization_lock(root):
        recovered, raced = _complete_journal_locked(root)
        created.extend(recovered)
        preserved.extend(raced)
        recovered_set = set(recovered) | set(raced)

        pending: list[tuple[str, str]] = []
        for template_name, active_name in CONFIG_TEMPLATES:
            source = root / template_name
            target = root / active_name
            if target.exists():
                if active_name not in recovered_set:
                    preserved.append(active_name)
                continue
            if not source.is_file():
                missing.append(template_name)
                continue
            if write_notice:
                pending.append((active_name, _copy_to_stage(source, root)))
            elif _copy_bytes_if_absent(source, target):
                created.append(active_name)
            else:
                preserved.append(active_name)

        if pending:
            event_id = uuid.uuid4().hex
            try:
                # journal 必须先于任何 active 文件创建持久化。
                _store_journal_locked(root, event_id, pending)
            except Exception:
                for _, stage_name in pending:
                    (root / stage_name).unlink(missing_ok=True)
                raise
            completed, raced = _complete_journal_locked(root)
            created.extend(completed)
            preserved.extend(raced)

    return InitResult(tuple(created), tuple(preserved), tuple(missing))


def read_initialization_notice(config_dir: Path = CONFIG_DIR) -> dict | None:
    """at-least-once 返回队首事件；读取本身不消费事件。"""
    root = Path(config_dir)
    with _initialization_lock(root):
        events = _load_notice_events_locked(root)
        return dict(events[0]) if events else None


def acknowledge_initialization_notice(
    event_id: str, config_dir: Path = CONFIG_DIR,
) -> bool:
    """只删除匹配 event_id 的事件，并保留所有其他（包括后来）事件。"""
    root = Path(config_dir)
    with _initialization_lock(root):
        events = _load_notice_events_locked(root)
        remaining = [event for event in events if event["event_id"] != event_id]
        if len(remaining) == len(events):
            return False
        _store_notice_events_locked(root, remaining)
        return True
