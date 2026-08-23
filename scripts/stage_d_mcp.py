# -*- coding: utf-8 -*-
"""Stage D: drive Atlas exclusively through the real MCP stdio server.

Every billable run is preceded by a dry_run through the same tool, and the
real run pins the dry-run's execution_sha256 (confirm-before-run contract).
Never reads or prints config/.env values; no secrets in output.

Phases:
  probe                     zero cost: tools/list, P6 line/column, list_workflows
  cell --workflow W --provider P [--verify-agent]
                            one matrix cell: dry_run -> run -> approve -> terminal
  custom                    save/dry_run/run/approve the stage-d custom graph
  failure                   budget cap, node timeout, bad-key via env override,
                            resume-on-done contract rejection
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
import urllib.request
from contextlib import AsyncExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = "http://127.0.0.1:8321"
RESULTS = ROOT / "runs" / "stage_d_results.jsonl"
CALL_TIMEOUT_S = 1900.0

PROVIDER_MODEL = {
    "Deepseek": "Deepseek:deepseek-v4-flash",
    "SuperAI": "SuperAI:doubao-seed-2.0-lite",
    "Kiro": "Kiro:claude-sonnet-5",
}
MODEL_NODES = {
    "code-change-review-approve": ["implementer", "reviewer"],
    "human-approval-pipeline": ["draft", "finalizer"],
    "map-reduce-document-analysis": ["lens_evidence", "lens_risk", "lens_action",
                                     "reducer"],
    "multi-vendor-debate-judge": ["analyst_a", "analyst_b", "judge"],
    "parallel-research-synthesis": ["scout_a", "scout_b", "scout_c",
                                    "synthesizer"],
    "proposal-review-repair-loop": ["author", "reviewer"],
}
TASKS = {
    "code-change-review-approve": "修复 demo-project 中的示例错误并自测",
    "human-approval-pipeline": "起草一封向客户解释延期一周的邮件,语气诚恳不推责",
    "map-reduce-document-analysis": (
        "分析这份实验记录:哪些结论站得住,哪些是侥幸,下一步实验怎么设计。"
        "实验记录:共三组,每组样本5,结果分别为 3过2败、4过1败、5过0败。"),
    "multi-vendor-debate-judge": "评估小型团队应自建工作流引擎还是使用现成方案",
    "parallel-research-synthesis": "比较三种本地工作流引擎的数据完整性设计",
    "proposal-review-repair-loop": "为一个三人团队写一份两周内上线的内部工具技术方案",
}


def log(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def append_result(row: dict) -> None:
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def web_post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        WEB + path, data=json.dumps(body).encode("utf-8"),
        headers={"X-Atlas-Request": "1", "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def open_session(stack: AsyncExitStack,
                       extra_env: dict | None = None):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    params = StdioServerParameters(
        command="uv", args=["--directory", str(ROOT), "run", "atlas-mcp"],
        env=env)
    read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(
        ClientSession(read, write, read_timeout_seconds=CALL_TIMEOUT_S))
    await session.initialize()
    return session


async def call_tool(session, name: str, args: dict) -> dict:
    result = await session.call_tool(name, args)
    texts = [c.text for c in result.content if hasattr(c, "text")]
    try:
        return json.loads("\n".join(texts))
    except (TypeError, ValueError):
        return {"_raw": "\n".join(texts), "isError": result.isError}


def read_events(run_id: str) -> list[dict]:
    path = ROOT / "runs" / str(run_id) / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def wait_terminal(session, run_id: str, timeout_s: float = 900.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        s = await call_tool(session, "atlas_get_run", {"run_id": run_id})
        if s.get("status") in ("done", "failed", "cancelled"):
            return s
        await asyncio.sleep(3)
    return {"status": "timeout", "run_id": run_id}


def tree_digest(root: Path) -> str:
    sha = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*")
                       if p.is_file() and ".git" not in p.parts):
        sha.update(path.relative_to(root).as_posix().encode("utf-8"))
        sha.update(path.read_bytes())
    return sha.hexdigest()


def compact(summary: dict) -> dict:
    totals = summary.get("totals") or {}
    return {
        "run_id": summary.get("run_id"),
        "status": summary.get("status"),
        "nodes_done": summary.get("nodes_done"),
        "input_tokens": totals.get("input_tokens"),
        "output_tokens": totals.get("output_tokens"),
        "known_actual_cost_usd": totals.get("known_actual_cost_usd"),
        "accounted_cost_usd": totals.get("accounted_cost_usd"),
        "unknown_count": totals.get("actual_cost_unknown_count"),
        "cost_usd": totals.get("cost_usd"),
        "failed_error": summary.get("failed_error"),
        "run_error": summary.get("error"),
        "models": {n: d.get("model_used")
                   for n, d in (summary.get("node_details") or {}).items()},
        "degraded": {n: d.get("degraded")
                     for n, d in (summary.get("node_details") or {}).items()},
    }


async def approve_and_wait(session, run_id: str) -> dict:
    web_post(f"/api/runs/{run_id}/approve",
             {"decision": "approve", "comment": "Stage D 批复"})
    return await wait_terminal(session, run_id)


async def phase_probe() -> None:
    async with AsyncExitStack() as stack:
        session = await open_session(stack)
        tools = await session.list_tools()
        names = sorted(t.name for t in tools.tools)
        expected = ["atlas_get_run", "atlas_list_workflows", "atlas_resume_run",
                    "atlas_run_workflow", "atlas_save_workflow",
                    "atlas_validate_workflow"]
        log({"phase": "probe", "tools": names, "tools_ok": names == expected})
        bad_yaml = ("name: p6\nnodes:\n  - id: a\n    type: llm\n"
                    "    type: human\n    prompt: x\nconsumes: [task]\n")
        v = await call_tool(session, "atlas_validate_workflow",
                            {"yaml": bad_yaml})
        err_text = json.dumps(v, ensure_ascii=False)
        log({"phase": "probe", "p6_duplicate_key": {
            "valid": v.get("valid"),
            "error_has_line": '"line"' in err_text,
            "error": (v.get("error") or "")[:300]}})
        listing = await call_tool(session, "atlas_list_workflows", {})
        log({"phase": "probe",
             "workflows": [w["id"] for w in listing.get("workflows", [])]})


async def phase_cell(workflow: str, provider: str, verify_agent: bool,
                     extra_overrides: dict | None = None,
                     model: str | None = None) -> None:
    model = model or PROVIDER_MODEL[provider]
    overrides = {n: {"model": model} for n in MODEL_NODES[workflow]}
    if extra_overrides:
        overrides.update(extra_overrides)
    task = TASKS[workflow]
    demo = ROOT / "demo-project"
    before = tree_digest(demo) if verify_agent else None
    async with AsyncExitStack() as stack:
        session = await open_session(stack)
        t0 = time.time()
        dry = await call_tool(session, "atlas_run_workflow", {
            "workflow_id": workflow, "task": task, "dry_run": True,
            "node_overrides": overrides})
        if dry.get("error") or dry.get("unconfigured_nodes"):
            row = {"phase": "cell", "workflow": workflow, "provider": provider,
                   "model": model, "outcome": "dry_run_rejected",
                   "error": dry.get("error"),
                   "unconfigured": dry.get("unconfigured_nodes")}
            append_result(row)
            log(row)
            return
        run = await call_tool(session, "atlas_run_workflow", {
            "workflow_id": workflow, "task": task, "dry_run": False,
            "node_overrides": overrides,
            "expected_execution_sha256": dry.get("execution_sha256")})
        rid = run.get("run_id")
        if run.get("status") == "paused":
            run = await approve_and_wait(session, rid)
        row = {"phase": "cell", "workflow": workflow, "provider": provider,
               "model": model, "task_chars": len(task), "outcome": "ran",
               "seconds": round(time.time() - t0, 1), "summary": compact(run)}
        if verify_agent:
            events = read_events(rid)
            approval = next((e for e in events if e["type"] == "run_approval"),
                            None)
            artifacts = sorted(
                p.name for p in (ROOT / "runs" / str(rid) / "artifacts").glob("*")
            ) if (ROOT / "runs" / str(rid) / "artifacts").is_dir() else []
            row["agent_checks"] = {
                "original_workdir_unchanged": tree_digest(demo) == before,
                "approval_recorded": approval is not None,
                "approved_diffs": (approval or {}).get("approved_diffs"),
                "has_diff_artifact": any("diff" in a for a in artifacts),
                "artifacts": artifacts,
            }
        append_result(row)
        log(row)


CUSTOM_YAML = """\
name: stage-d-custom
description: 阶段D自定义图:并行三分支汇合+有界修复循环+人工门+coding_agent
entry: [left, mid, right]
nodes:
  - id: left
    type: llm
    prompt: |
      用不超过两句话给出任务的一个侧面要点,输出纯文本,不要JSON。
    consumes: [task]
  - id: mid
    type: llm
    prompt: |
      用不超过两句话给出任务的另一个侧面要点,输出纯文本,不要JSON。
    consumes: [task]
  - id: right
    type: llm
    prompt: |
      用不超过两句话给出任务的第三个侧面要点,输出纯文本,不要JSON。
    consumes: [task]
  - id: joiner
    type: llm
    prompt: |
      汇总三个侧面要点成不超过五句的执行摘要。只输出 JSON:
      {"summary": "..."}。summary 必须非空。
    consumes: [task, left.output, mid.output, right.output]
    output_schema:
      required: [summary]
  - id: coder
    type: coding_agent
    prompt: |
      你的工作目录就是待修改的项目本身(已是目标项目的隔离副本),直接用相对路径
      查看与修改其中文件;不要去寻找原项目目录。按任务与汇总做最小合理修改,
      报告修改的文件与原因。
    consumes: [task, joiner.output]
    workdir: demo-project
    max_turns: 6
    allow_web: false
    retry: 1
  - id: checker
    type: llm
    prompt: |
      你收到实施报告与完整文本 unified diff 及三项摘要。改动真实、diff 非空且摘要
      匹配时输出 pass;只有证据缺失或明显错误才输出 fail。只输出 JSON:
      {"verdict": "pass|fail", "note": "..."}。
    consumes: [task, coder.output, coder.diff]
    output_schema:
      required: [verdict, note]
    route_field: verdict
  - id: gate
    type: human
    prompt: 审阅汇总、实施报告与 diff 摘要后批准。
    consumes: [task, joiner.output, coder.output]
edges:
  - from: left
    to: joiner
  - from: mid
    to: joiner
  - from: right
    to: joiner
  - from: joiner
    to: coder
  - from: coder
    to: checker
  - from: checker
    when: pass
    to: gate
  - from: checker
    when: fail
    to: coder
  - from: gate
    to: END
guards:
  max_iterations: 2
  timeout_s: 1500
"""

BUDGET_YAML = """\
name: stage-d-budget
nodes:
  - id: a
    type: llm
    prompt: |
      用一句话回答任务,不超过30字。
    consumes: [task]
  - id: b
    type: llm
    prompt: |
      基于上游回答再补一句话,不超过30字。
    consumes: [task, a.output]
edges:
  - from: a
    to: b
guards:
  max_cost_usd: 0.000001
"""

TIMEOUT_YAML = """\
name: stage-d-timeout
nodes:
  - id: slow
    type: llm
    timeout_s: 1
    prompt: |
      就任务写一段不少于300字的详细分析。
    consumes: [task]
edges: []
guards:
  timeout_s: 300
"""

SMOKE_YAML = """\
name: stage-d-smoke
nodes:
  - id: solo
    type: llm
    prompt: |
      用一句话回答任务,不超过30字。
    consumes: [task]
edges: []
guards:
  timeout_s: 300
"""


def _llm_override(yaml_text: str, model: str) -> dict:
    ids = re.findall(r"- id: ([A-Za-z0-9_.-]+)\n\s+type: llm", yaml_text)
    return {i: {"model": model} for i in ids}


async def save_and_run(session, workflow_id: str, yaml_text: str, task: str,
                       overrides: dict | None = None,
                       approve: bool = False) -> dict:
    saved = await call_tool(session, "atlas_save_workflow",
                            {"workflow_id": workflow_id, "yaml": yaml_text})
    if not saved.get("saved"):
        return {"stage": "save", "error": saved.get("error")}
    dry = await call_tool(session, "atlas_run_workflow", {
        "workflow_id": workflow_id, "task": task, "dry_run": True,
        "node_overrides": overrides})
    if dry.get("error") or dry.get("unconfigured_nodes"):
        return {"stage": "dry_run", "error": dry.get("error"),
                "unconfigured": dry.get("unconfigured_nodes")}
    run = await call_tool(session, "atlas_run_workflow", {
        "workflow_id": workflow_id, "task": task, "dry_run": False,
        "node_overrides": overrides,
        "expected_execution_sha256": dry.get("execution_sha256")})
    rid = run.get("run_id")
    if approve and run.get("status") == "paused":
        run = await approve_and_wait(session, rid)
    return {"stage": "done", "summary": run}


async def phase_custom() -> None:
    async with AsyncExitStack() as stack:
        session = await open_session(stack)
        task = "在 README.md 末尾追加一行文本:stage-d-custom OK"
        overrides = _llm_override(CUSTOM_YAML, PROVIDER_MODEL["Deepseek"])
        overrides["coder"] = {"model": PROVIDER_MODEL["Deepseek"]}
        result = await save_and_run(session, "stage-d-custom", CUSTOM_YAML,
                                    task, overrides, approve=True)
        rid = (result.get("summary") or {}).get("run_id")
        events = read_events(rid) if rid else []
        iterations = sorted({e.get("iteration") for e in events
                             if e["type"] == "node_started"})
        row = {"phase": "custom", "stage": result.get("stage"),
               "error": result.get("error"),
               "iterations": iterations,
               "summary": compact(result.get("summary") or {})}
        append_result(row)
        log(row)


async def phase_failure() -> None:
    rows = []
    async with AsyncExitStack() as stack:
        session = await open_session(stack)
        task = "一句话介绍本地工作流引擎的价值"
        r = await save_and_run(
            session, "stage-d-budget", BUDGET_YAML, task,
            _llm_override(BUDGET_YAML, PROVIDER_MODEL["Deepseek"]))
        rows.append({"case": "budget_cap", "stage": r.get("stage"),
                     "error": r.get("error"),
                     "summary": compact(r.get("summary") or {})})
        r = await save_and_run(
            session, "stage-d-timeout", TIMEOUT_YAML,
            "详细分析本地工作流引擎的审计价值",
            _llm_override(TIMEOUT_YAML, PROVIDER_MODEL["Deepseek"]))
        rows.append({"case": "node_timeout", "stage": r.get("stage"),
                     "error": r.get("error"),
                     "summary": compact(r.get("summary") or {})})
    async with AsyncExitStack() as stack:
        # deliberately invalid sentinel key in process env; config/.env is
        # untouched and its values never read or printed
        session = await open_session(stack, {"DEEPSEEK_API_KEY":
                                             "sk-stage-d-invalid"})
        r = await save_and_run(
            session, "stage-d-smoke", SMOKE_YAML,
            "一句话介绍本地工作流引擎的价值",
            _llm_override(SMOKE_YAML, PROVIDER_MODEL["Deepseek"]))
        rows.append({"case": "bad_key", "stage": r.get("stage"),
                     "error": r.get("error"),
                     "summary": compact(r.get("summary") or {})})
    done_rid = None
    if RESULTS.exists():
        for line in RESULTS.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            summary = row.get("summary") or {}
            if summary.get("status") == "done" and summary.get("run_id"):
                done_rid = summary["run_id"]
    resume_row = {"case": "resume_on_done", "run_id": done_rid}
    if done_rid:
        async with AsyncExitStack() as stack:
            session = await open_session(stack)
            r = await call_tool(session, "atlas_resume_run",
                                {"run_id": done_rid})
            resume_row["rejected"] = bool(r.get("error"))
            resume_row["error"] = (r.get("error") or "")[:200]
    rows.append(resume_row)
    for row in rows:
        append_result({"phase": "failure", **row})
        log({"phase": "failure", **row})


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["probe", "cell", "custom", "failure"])
    parser.add_argument("--workflow")
    parser.add_argument("--provider")
    parser.add_argument("--verify-agent", action="store_true")
    parser.add_argument("--max-output-tokens", type=int, default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--fallback", default=None,
                        help="cross-vendor fallback model for all model nodes")
    parser.add_argument("--prompt-json", default=None,
                        help='per-node prompt override, e.g. \'{"n": "text"}\'')
    parser.add_argument("--node-timeout", type=float, default=None)
    args = parser.parse_args()
    if args.phase == "probe":
        await phase_probe()
    elif args.phase == "cell":
        extra = None
        if (args.max_output_tokens or args.fallback or args.prompt_json
                or args.node_timeout):
            extra = {n: {"model": args.model or PROVIDER_MODEL[args.provider]}
                     for n in MODEL_NODES[args.workflow]}
            if args.max_output_tokens:
                for v in extra.values():
                    v["max_output_tokens"] = args.max_output_tokens
            if args.fallback:
                for v in extra.values():
                    v["fallback"] = [args.fallback]
            if args.node_timeout:
                for v in extra.values():
                    v["timeout_s"] = args.node_timeout
            if args.prompt_json:
                for node, text in json.loads(args.prompt_json).items():
                    extra.setdefault(node, {})["prompt"] = text
        await phase_cell(args.workflow, args.provider, args.verify_agent, extra,
                         args.model)
    elif args.phase == "custom":
        await phase_custom()
    elif args.phase == "failure":
        await phase_failure()


if __name__ == "__main__":
    asyncio.run(main())
