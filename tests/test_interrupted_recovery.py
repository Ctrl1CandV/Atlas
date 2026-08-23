# -*- coding: utf-8 -*-
"""P1 core: dynamic interrupted view and race-safe resume admission."""
import json
import threading

import pytest

import atlas.engine as engine_module
import atlas.runs as runs_module
from atlas.adapters import AllCandidatesFailed, FakeProvider
from atlas.engine import (RunConflictError, acquire_run_lock, execute_graph,
                          prepare_execution, release_run_lock, resume_graph)
from atlas.events import EventReader
from atlas.integrity import IntegrityError
from atlas.runs import derive_run_status
from atlas.spec import SpecError

from conftest import TASK_TEXT, load_graph, make_registry, standard_fake


def _drop_last_failed_event(run_dir):
    path = run_dir / "events.jsonl"
    records = EventReader(path).all()
    assert records[-1]["type"] == "run_failed"
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n"
                for record in records[:-1]),
        encoding="utf-8",
    )


def _interrupted_run(tmp_path):
    spec = load_graph("three_node")
    fake = standard_fake(100)
    fake.configure("third", transport_error="simulated process loss")
    prepared = prepare_execution(spec, make_registry(fake))
    with pytest.raises(AllCandidatesFailed):
        execute_graph(spec, task=TASK_TEXT, runs_root=tmp_path,
                      prepared=prepared)
    run_dir = next(tmp_path.glob("*/events.jsonl")).parent
    _drop_last_failed_event(run_dir)
    fake.configure("third", text="recovered")
    return spec, prepared, run_dir


def _event_types(run_dir):
    return [event["type"] for event in EventReader(
        run_dir / "events.jsonl").all()]


def test_derive_interrupted_requires_free_lock_and_no_controller(tmp_path,
                                                                  monkeypatch):
    run_id = "dynamic"
    records = [{"type": "run_started", "run_id": run_id}]

    assert derive_run_status(records, run_id=run_id, runs_root=tmp_path,
                             active_controller=True) == "running"

    acquire_run_lock(run_id, runs_root=tmp_path)
    try:
        assert derive_run_status(records, run_id=run_id,
                                 runs_root=tmp_path) == "running"
    finally:
        release_run_lock(run_id, runs_root=tmp_path)

    def probe_error(*args, **kwargs):
        raise PermissionError("probe unavailable")

    monkeypatch.setattr(runs_module, "acquire_run_lock", probe_error)
    assert derive_run_status(records, run_id=run_id,
                             runs_root=tmp_path) == "running"

    monkeypatch.undo()
    assert derive_run_status(records, run_id=run_id,
                             runs_root=tmp_path) == "interrupted"
    assert not (tmp_path / run_id / "events.jsonl").exists()


@pytest.mark.parametrize(
    ("terminal_event", "expected"),
    [("run_paused", "paused"), ("run_done", "done"),
     ("run_failed", "failed")],
)
def test_derive_non_running_status_is_unchanged_without_lock_probe(
        tmp_path, monkeypatch, terminal_event, expected):
    records = [{"type": "run_started", "run_id": "terminal"},
               {"type": terminal_event, "run_id": "terminal"}]

    def forbidden_probe(*args, **kwargs):
        raise AssertionError("non-running status must not probe the lock")

    monkeypatch.setattr(runs_module, "acquire_run_lock", forbidden_probe)
    assert derive_run_status(records, run_id="terminal",
                             runs_root=tmp_path) == expected


def test_resume_rejects_paused_done_failed_and_live_without_ledger_append(tmp_path):
    cases = []

    paused_spec = load_graph("human_gate")
    paused_fake = standard_fake(100)
    paused_prepared = prepare_execution(paused_spec, make_registry(paused_fake))
    paused = execute_graph(paused_spec, task=TASK_TEXT,
                           runs_root=tmp_path / "paused",
                           prepared=paused_prepared)
    cases.append((paused.run_id, paused_spec, tmp_path / "paused",
                  paused_prepared, False))

    done_spec = load_graph("two_node")
    done_fake = standard_fake(100)
    done_prepared = prepare_execution(done_spec, make_registry(done_fake))
    done = execute_graph(done_spec, task=TASK_TEXT, runs_root=tmp_path / "done",
                         prepared=done_prepared)
    cases.append((done.run_id, done_spec, tmp_path / "done",
                  done_prepared, False))

    failed_spec = load_graph("three_node")
    failed_fake = standard_fake(100)
    failed_fake.configure("third", transport_error="failure")
    failed_prepared = prepare_execution(failed_spec, make_registry(failed_fake))
    with pytest.raises(AllCandidatesFailed):
        execute_graph(failed_spec, task=TASK_TEXT, runs_root=tmp_path / "failed",
                      prepared=failed_prepared)
    failed_dir = next((tmp_path / "failed").glob("*/events.jsonl")).parent
    cases.append((failed_dir.name, failed_spec, tmp_path / "failed",
                  failed_prepared, False))

    live_spec, live_prepared, live_dir = _interrupted_run(tmp_path / "live")
    cases.append((live_dir.name, live_spec, tmp_path / "live",
                  live_prepared, True))

    for run_id, spec, root, prepared, active_controller in cases:
        path = root / run_id / "events.jsonl"
        before = path.read_bytes()
        with pytest.raises(RunConflictError):
            resume_graph(run_id, spec=spec, runs_root=root, prepared=prepared,
                         active_controller=active_controller)
        assert path.read_bytes() == before
        assert "run_resumed" not in _event_types(root / run_id)


@pytest.mark.parametrize("invalid_part", ["snapshot", "identity", "checkpoint"])
def test_resume_validates_durable_inputs_before_run_resumed(tmp_path, invalid_part):
    spec, prepared, run_dir = _interrupted_run(tmp_path)
    events_path = run_dir / "events.jsonl"

    if invalid_part == "snapshot":
        snapshot_path = run_dir / "spec.snapshot.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot["name"] = "tampered"
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
        expected_error = SpecError
    elif invalid_part == "identity":
        records = EventReader(events_path).all()
        records[0]["execution_sha256"] = "0" * 64
        events_path.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n"
                    for record in records), encoding="utf-8")
        expected_error = SpecError
    else:
        (run_dir / "checkpoint.sqlite").write_bytes(b"not sqlite")
        expected_error = IntegrityError

    before = events_path.read_bytes()
    with pytest.raises(expected_error):
        resume_graph(run_dir.name, spec=spec, runs_root=tmp_path,
                     prepared=prepared)
    assert events_path.read_bytes() == before
    assert "run_resumed" not in _event_types(run_dir)


def test_duplicate_resume_admission_has_one_winner_and_one_append(tmp_path,
                                                                   monkeypatch):
    spec, prepared, run_dir = _interrupted_run(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    winner = []

    def blocking_invoke(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return object()

    monkeypatch.setattr(engine_module, "_invoke", blocking_invoke)

    def first_resume():
        winner.append(resume_graph(
            run_dir.name, spec=spec, runs_root=tmp_path, prepared=prepared))

    worker = threading.Thread(target=first_resume)
    worker.start()
    assert entered.wait(timeout=3)
    try:
        with pytest.raises(RunConflictError, match="运行锁"):
            resume_graph(run_dir.name, spec=spec, runs_root=tmp_path,
                         prepared=prepared)
        assert _event_types(run_dir).count("run_resumed") == 1
    finally:
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(winner) == 1
    assert _event_types(run_dir).count("run_resumed") == 1
