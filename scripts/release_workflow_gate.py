# -*- coding: utf-8 -*-
"""Offline release gate for every shipped workflow."""
from __future__ import annotations

import argparse
import json
import socket
from contextlib import contextmanager
from pathlib import Path

from atlas.adapters import AdapterRegistry
from atlas.effective import build_effective_spec
from atlas.mcp import dry_run_impl, validate_workflow_impl
from atlas.spec import spec_from_yaml_file

EXPECTED_WORKFLOW_COUNT = 6
FAKE_MODEL = "ReleaseGate:offline-model"
_MODEL_NODE_TYPES = {"llm", "research", "coding_agent"}
_RUN_INFRASTRUCTURE_DIRS = {".locks", ".trash"}


class _NoCallAdapter:
    protocol = "openai"
    max_output_tokens = 8192

    def __init__(self, counters: dict[str, int]) -> None:
        self._counters = counters

    def call(self, *args, **kwargs):
        self._counters["provider_calls"] += 1
        raise AssertionError("release workflow gate attempted a provider call")


class _StrictAgentRunner:
    runner_name = "release_gate_stub"
    nonsecret_execution_descriptor = True

    def __init__(self, counters: dict[str, int]) -> None:
        self._counters = counters

    def execution_descriptor(self, provider_ids: set[str]) -> dict:
        if provider_ids - {"ReleaseGate"}:
            raise AssertionError(f"unexpected agent providers: {sorted(provider_ids)}")
        return {
            "version": 1,
            "kind": "release_gate_stub",
            "providers": sorted(provider_ids),
        }

    def __call__(self, *args, **kwargs):
        self._counters["agent_calls"] += 1
        raise AssertionError("release workflow gate attempted to run an agent")


@contextmanager
def _deny_network():
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create_connection = socket.create_connection

    def denied(*args, **kwargs):
        raise AssertionError("release workflow gate attempted network access")

    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.create_connection = denied
    try:
        yield
    finally:
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex
        socket.create_connection = original_create_connection


def _model_overrides(spec, project_root: Path) -> dict[str, dict]:
    overrides = {}
    for node in spec.nodes:
        if node.type == "llm":
            overrides[node.id] = {"model": FAKE_MODEL, "fallback": []}
        elif node.type == "research":
            overrides[node.id] = {"model": FAKE_MODEL}
        elif node.type == "coding_agent":
            overrides[node.id] = {
                "model": FAKE_MODEL,
                "workdir": str(project_root / "demo-project"),
            }
    return overrides


def run_workflow_gate(project_root: Path) -> dict:
    """Strictly validate and dry-run all shipped workflows without active config."""
    root = Path(project_root).resolve()
    workflows_dir = root / "workflows"
    runs_dir = root / "runs"
    # 门禁对象是"随发布出货的内置示例"(meta.kind=example)。开发者的
    # 自定义图就住在 workflows/ 里,这是产品用法,不属于发布面,
    # 不应让门禁失败;解析失败的文件仍必须进入校验并报错。
    import yaml as _yaml
    files = []
    for path in sorted(workflows_dir.glob("*.yaml")):
        try:
            meta = (_yaml.safe_load(path.read_text(encoding="utf-8"))
                    or {}).get("meta") or {}
        except Exception:
            meta = {"kind": "example"}
        if meta.get("kind") == "example":
            files.append(path)
    if len(files) != EXPECTED_WORKFLOW_COUNT:
        raise AssertionError(
            f"expected {EXPECTED_WORKFLOW_COUNT} example workflows, found {len(files)}")

    initial_run_dirs = ({path.name for path in runs_dir.iterdir()
                         if path.is_dir() and path.name not in _RUN_INFRASTRUCTURE_DIRS}
                        if runs_dir.is_dir() else set())
    if initial_run_dirs:
        raise AssertionError(
            f"release workflow gate requires zero run directories: {sorted(initial_run_dirs)}")

    counters = {
        "registry_preflights": 0,
        "runner_preflights": 0,
        "provider_calls": 0,
        "agent_calls": 0,
    }
    checked: list[dict] = []

    import atlas.mcp as mcp
    old_workflows_dir, old_runs_dir = mcp.WORKFLOWS_DIR, mcp.RUNS_DIR
    mcp.WORKFLOWS_DIR, mcp.RUNS_DIR = workflows_dir, runs_dir
    try:
        with _deny_network():
            for path in files:
                spec = spec_from_yaml_file(path)
                configured_nodes = [
                    node.id for node in spec.nodes if node.type in _MODEL_NODE_TYPES]
                overrides = _model_overrides(spec, root)
                if set(overrides) != set(configured_nodes):
                    raise AssertionError(f"{path.stem}: not every model node has an override")

                validation = validate_workflow_impl(workflow_id=path.stem)
                if validation.get("valid") is not True:
                    raise AssertionError(f"{path.stem}: validation failed: {validation}")

                def registry_factory(provider_ids):
                    counters["registry_preflights"] += 1
                    if list(provider_ids) != ["ReleaseGate"]:
                        raise AssertionError(
                            f"{path.stem}: unexpected LLM providers {list(provider_ids)}")
                    registry = AdapterRegistry()
                    registry.register(
                        "ReleaseGate", ["offline-model"], _NoCallAdapter(counters))
                    return registry

                strict_runner = _StrictAgentRunner(counters)

                def agent_runner_factory(effective_spec):
                    counters["runner_preflights"] += 1
                    remaining = [
                        node.id for node in effective_spec.nodes
                        if node.type in _MODEL_NODE_TYPES and not node.model]
                    if remaining:
                        raise AssertionError(
                            f"{path.stem}: unconfigured nodes reached runner: {remaining}")
                    return strict_runner

                preview = dry_run_impl(
                    path.stem,
                    "offline release-gate preview",
                    node_overrides=overrides,
                    registry_factory=registry_factory,
                    agent_runner_factory=agent_runner_factory,
                )
                if preview.get("dry_run") is not True or "error" in preview:
                    raise AssertionError(f"{path.stem}: dry-run failed: {preview}")
                if preview.get("unconfigured_nodes") != []:
                    raise AssertionError(
                        f"{path.stem}: unconfigured nodes: {preview.get('unconfigured_nodes')}")
                execution_sha256 = preview.get("execution_sha256")
                if (not isinstance(execution_sha256, str)
                        or len(execution_sha256) != 64):
                    raise AssertionError(
                        f"{path.stem}: missing execution_sha256: {execution_sha256!r}")

                rendered = {item["node"]: item for item in preview["nodes"]}
                for node_id in configured_nodes:
                    if rendered[node_id]["model"] != FAKE_MODEL:
                        raise AssertionError(
                            f"{path.stem}.{node_id}: fake model override was not rendered")
                effective = build_effective_spec(spec, overrides)
                if effective.unconfigured_nodes:
                    raise AssertionError(
                        f"{path.stem}: effective spec is not configured")
                checked.append({
                    "workflow": path.stem,
                    "configured_nodes": configured_nodes,
                    "execution_sha256": execution_sha256,
                })
    finally:
        mcp.WORKFLOWS_DIR, mcp.RUNS_DIR = old_workflows_dir, old_runs_dir

    final_run_dirs = ({path.name for path in runs_dir.iterdir()
                       if path.is_dir() and path.name not in _RUN_INFRASTRUCTURE_DIRS}
                      if runs_dir.is_dir() else set())
    if final_run_dirs or final_run_dirs != initial_run_dirs:
        raise AssertionError(f"dry-run created run directories: {sorted(final_run_dirs)}")
    if counters["provider_calls"] != 0 or counters["agent_calls"] != 0:
        raise AssertionError(f"offline gate executed a backend: {counters}")
    if counters["registry_preflights"] != EXPECTED_WORKFLOW_COUNT:
        raise AssertionError(f"registry preflight count differs: {counters}")
    if counters["runner_preflights"] != EXPECTED_WORKFLOW_COUNT:
        raise AssertionError(f"runner preflight count differs: {counters}")

    return {
        "workflows": checked,
        "workflow_count": len(checked),
        "run_directories": 0,
        **counters,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project-root", type=Path,
        default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    print(json.dumps(run_workflow_gate(args.project_root), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
