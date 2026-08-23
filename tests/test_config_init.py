# -*- coding: utf-8 -*-
"""首次启动配置初始化：不覆盖、幂等、并发安全与 CLI 输出。"""
from __future__ import annotations

import json
import multiprocessing
import os
import threading
from pathlib import Path

import pytest

from atlas import config_init
from atlas.cli import main as cli_main
from atlas.config_init import (
    CONFIG_TEMPLATES,
    acknowledge_initialization_notice,
    initialize_runtime_config,
    read_initialization_notice,
)


def _multiprocess_initialize(config_dir: str, ready, start, results) -> None:
    ready.put(True)
    start.wait()
    result = initialize_runtime_config(Path(config_dir))
    results.put(result.created)


def _multiprocess_notice(config_dir: str, created: str, ready, start) -> None:
    ready.put(True)
    start.wait()
    config_init._write_notice(Path(config_dir), (created,))


def _multiprocess_ack_after_load(
    config_dir: str, loaded, release, results,
) -> None:
    original_load = config_init._load_notice_events_locked

    def load_then_wait(root: Path) -> list[dict]:
        events = original_load(root)
        loaded.set()
        if not release.wait(timeout=10):
            raise TimeoutError("timed out waiting to finish notice acknowledgement")
        return events

    config_init._load_notice_events_locked = load_then_wait
    results.put(acknowledge_initialization_notice("old-event", Path(config_dir)))


def _multiprocess_notice_with_id(
    config_dir: str, started, finished,
) -> None:
    started.set()
    config_init._write_notice(
        Path(config_dir), ("agents.json",), event_id="later-event")
    finished.set()


def _write_templates(config_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    for template, active in CONFIG_TEMPLATES:
        payload = (b"# template\n" if template.startswith(".") else
                   json.dumps({"template": template}, ensure_ascii=False).encode("utf-8"))
        (config_dir / template).write_bytes(payload)


def test_initialization_copies_exact_bytes_and_agents_stay_disabled(tmp_path):
    _write_templates(tmp_path)
    (tmp_path / "agents.example.json").write_text(
        '{"runner":"fail_closed","cli":{"kind":"claude","command":"claude","extra_args":[]}}\n',
        encoding="utf-8")

    result = initialize_runtime_config(tmp_path)

    assert set(result.created) == {active for _, active in CONFIG_TEMPLATES}
    assert result.missing_templates == ()
    for template, active in CONFIG_TEMPLATES:
        assert (tmp_path / active).read_bytes() == (tmp_path / template).read_bytes()
    assert json.loads((tmp_path / "agents.json").read_text(encoding="utf-8"))["runner"] == "fail_closed"


def test_existing_files_are_never_overwritten_and_second_run_is_idempotent(tmp_path):
    _write_templates(tmp_path)
    sentinel = b"user-owned-config\n"
    (tmp_path / "providers.json").write_bytes(sentinel)

    first = initialize_runtime_config(tmp_path)
    mtimes = {active: (tmp_path / active).stat().st_mtime_ns
              for _, active in CONFIG_TEMPLATES if (tmp_path / active).exists()}
    second = initialize_runtime_config(tmp_path)

    assert (tmp_path / "providers.json").read_bytes() == sentinel
    assert "providers.json" in first.preserved
    assert second.created == ()
    assert {active: (tmp_path / active).stat().st_mtime_ns
            for _, active in CONFIG_TEMPLATES if (tmp_path / active).exists()} == mtimes


def test_missing_template_is_reported_without_active_file(tmp_path):
    _write_templates(tmp_path)
    (tmp_path / "pricing.example.json").unlink()

    result = initialize_runtime_config(tmp_path)

    assert "pricing.example.json" in result.missing_templates
    assert not (tmp_path / "pricing.json").exists()


def test_concurrent_initialization_never_overwrites(tmp_path):
    _write_templates(tmp_path)
    results = []
    barrier = threading.Barrier(4)

    def run():
        barrier.wait()
        results.append(initialize_runtime_config(tmp_path))

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(len(result.created) for result in results) == len(CONFIG_TEMPLATES)
    for template, active in CONFIG_TEMPLATES:
        assert (tmp_path / active).read_bytes() == (tmp_path / template).read_bytes()


def test_cli_init_reports_results_without_file_contents(tmp_path, capsys):
    _write_templates(tmp_path)

    code = cli_main(["init", "--config-dir", str(tmp_path)])
    output = capsys.readouterr().out

    assert code == 0
    assert "已创建" in output
    assert "atlas-web" in output
    assert "template" not in output


def _start_processes(processes, ready, start) -> None:
    for process in processes:
        process.start()
    for _ in processes:
        assert ready.get(timeout=10) is True
    start.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0


def _drain_notices(config_dir: Path) -> list[dict]:
    events = []
    while (event := read_initialization_notice(config_dir)) is not None:
        # read 是 at-least-once；只有显式 ack 才推进队列。
        assert read_initialization_notice(config_dir) == event
        events.append(event)
        assert acknowledge_initialization_notice(event["event_id"], config_dir)
    return events


def test_multiprocess_initialization_keeps_all_created_names(tmp_path):
    _write_templates(tmp_path)
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_multiprocess_initialize,
            args=(str(tmp_path), ready, start, results),
        )
        for _ in range(4)
    ]

    _start_processes(processes, ready, start)

    created = [results.get(timeout=10) for _ in processes]
    assert sum(len(names) for names in created) == len(CONFIG_TEMPLATES)
    events = _drain_notices(tmp_path)
    assert len(events) == 1
    assert set(events[0]["created"]) == {active for _, active in CONFIG_TEMPLATES}
    assert (tmp_path / config_init._LOCK_NAME).is_file()


def test_multiprocess_notice_writers_do_not_lose_events(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    names = ["providers.json", "agents.json", "pricing.json", "capabilities.json"]
    processes = [
        context.Process(
            target=_multiprocess_notice,
            args=(str(tmp_path), name, ready, start),
        )
        for name in names
    ]

    _start_processes(processes, ready, start)

    events = _drain_notices(tmp_path)
    assert len(events) == len(names)
    assert {event["created"][0] for event in events} == set(names)
    assert len({event["event_id"] for event in events}) == len(names)


def test_notice_event_id_is_deduplicated_and_old_ack_keeps_new_event(tmp_path):
    config_init._write_notice(
        tmp_path, ("providers.json",), event_id="stable-event")
    config_init._write_notice(
        tmp_path, ("providers.json",), event_id="stable-event")
    config_init._write_notice(
        tmp_path, ("agents.json",), event_id="later-event")

    assert read_initialization_notice(tmp_path)["event_id"] == "stable-event"
    assert acknowledge_initialization_notice("stable-event", tmp_path)
    assert read_initialization_notice(tmp_path) == {
        "event_id": "later-event", "created": ["agents.json"]}
    assert not acknowledge_initialization_notice("stable-event", tmp_path)
    assert read_initialization_notice(tmp_path)["event_id"] == "later-event"


def test_spawn_ack_racing_later_notice_keeps_new_event_exactly_once(tmp_path):
    config_init._write_notice(
        tmp_path, ("providers.json",), event_id="old-event")
    context = multiprocessing.get_context("spawn")
    loaded = context.Event()
    release = context.Event()
    writer_started = context.Event()
    writer_finished = context.Event()
    results = context.Queue()
    acknowledger = context.Process(
        target=_multiprocess_ack_after_load,
        args=(str(tmp_path), loaded, release, results),
    )
    writer = context.Process(
        target=_multiprocess_notice_with_id,
        args=(str(tmp_path), writer_started, writer_finished),
    )

    acknowledger.start()
    assert loaded.wait(timeout=10)
    writer.start()
    assert writer_started.wait(timeout=10)
    # The writer must be blocked behind the real process lock until ack commits.
    assert not writer_finished.wait(timeout=0.2)
    release.set()
    for process in (acknowledger, writer):
        process.join(timeout=20)
        assert process.exitcode == 0

    assert results.get(timeout=10) is True
    assert _drain_notices(tmp_path) == [
        {"event_id": "later-event", "created": ["agents.json"]}]


def test_crash_after_active_create_is_recovered_into_notice(tmp_path):
    source = tmp_path / "providers.example.json"
    source.write_bytes(b'{"providers": []}\n')
    with config_init._initialization_lock(tmp_path):
        stage_name = config_init._copy_to_stage(source, tmp_path)
        config_init._store_journal_locked(
            tmp_path, "recover-event", [("providers.json", stage_name)])
        os.link(tmp_path / stage_name, tmp_path / "providers.json")
        # 模拟在 active create-if-absent 成功后、notice 发布前进程退出。

    result = initialize_runtime_config(tmp_path)

    assert result.created == ("providers.json",)
    assert read_initialization_notice(tmp_path) == {
        "event_id": "recover-event", "created": ["providers.json"]}
    assert not (tmp_path / config_init._JOURNAL_NAME).exists()
    assert not list(tmp_path.glob(f"{config_init._STAGE_PREFIX}*"))
    initialize_runtime_config(tmp_path)
    assert json.loads((tmp_path / config_init._NOTICE_NAME).read_text(
        encoding="utf-8"))["events"] == [
            {"event_id": "recover-event", "created": ["providers.json"]}]


def test_user_sentinel_winning_create_race_is_preserved(tmp_path):
    source = tmp_path / "providers.example.json"
    source.write_bytes(b'{"template": true}\n')
    sentinel = b"user-owned-config\n"
    with config_init._initialization_lock(tmp_path):
        stage_name = config_init._copy_to_stage(source, tmp_path)
        config_init._store_journal_locked(
            tmp_path, "raced-event", [("providers.json", stage_name)])
        # 模拟用户在 journal 后、Atlas link 前创建 active 文件。
        (tmp_path / "providers.json").write_bytes(sentinel)

    result = initialize_runtime_config(tmp_path)

    assert (tmp_path / "providers.json").read_bytes() == sentinel
    assert result.created == ()
    assert result.preserved == ("providers.json",)
    assert read_initialization_notice(tmp_path) is None


def test_corrupt_notice_and_journal_fail_loud(tmp_path):
    (tmp_path / config_init._NOTICE_NAME).write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="提示文件不是合法 JSON"):
        read_initialization_notice(tmp_path)

    (tmp_path / config_init._NOTICE_NAME).unlink()
    (tmp_path / config_init._JOURNAL_NAME).write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="journal 不是合法 JSON"):
        initialize_runtime_config(tmp_path)


def test_orphan_atomic_temp_is_ignored(tmp_path):
    orphan = tmp_path / f"{config_init._NOTICE_NAME}-orphan.tmp"
    orphan.write_text("{half-written", encoding="utf-8")

    assert read_initialization_notice(tmp_path) is None
    config_init._write_notice(tmp_path, ("providers.json",), event_id="valid")
    assert read_initialization_notice(tmp_path)["event_id"] == "valid"
    assert orphan.read_text(encoding="utf-8") == "{half-written"
