# -*- coding: utf-8 -*-
"""只读观测界面(v1):FastAPI + SSE。

界面没有编辑功能、没有改图的按钮——操控发生在 harness 对话里(agent 改
YAML)或直接编辑文件。这条界线让 UI 复杂度天花板很低,也让「界面显示的
每个数字都能在事件流里找到出处」可检(红线 ②/④ 的落地)。

只绑 127.0.0.1(红线 ④):serve() 硬编码,想绑别的地址直接被拒绝。
"""
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from atlas import __version__
from atlas.adapters import build_real_registry
from atlas.artifacts import artifacts_from_event
from atlas.config import PROJECT_ROOT, load_provider_configs
from atlas.costs import fold_cost_accounting
from atlas.effective import build_effective_spec, provider_ids_for_spec
from atlas.engine import (RunConflictError, RunNotFoundError, acquire_run_lock,
                          approve_run, execute_graph, lock_approval_run,
                          lock_interrupted_run, prepare_execution,
                          prepare_production_agent_runner,
                          release_approval_run_lock, release_run_lock,
                          request_cancel, resume_graph,
                          validate_interrupted_run_locked)
from atlas.events import EventReader, fold_events
from atlas import launcher
from atlas.runs import (RunNotDeletable, build_finale, derive_run_status,
                        has_star, isolate_and_delete_run, list_run_summaries,
                        read_star, set_star)
from atlas.integrity import TASK_MAX_BYTES
from atlas.spec import SpecError, spec_from_snapshot, spec_from_yaml_file
from atlas.thinking import load_capabilities, model_capability

DEFAULT_WORKFLOWS_DIR = PROJECT_ROOT / "workflows"
DEFAULT_RUNS_DIR = PROJECT_ROOT / "runs"
DEFAULT_HOST = "127.0.0.1"   # 红线 ④:不改
DEFAULT_PORT = 8321

# run_id / workflow id 的白名单:挡住反斜杠路径穿越(Windows 上 \ 也是分隔符)
# 和把线程池占满的无效轮询
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
# 防浏览器驱动:绑定 127.0.0.1 挡不住用户浏览器里别的网页向本机发请求。
# Host 校验挡 DNS rebinding;POST 必须带自定义头(跨站 no-cors 表单带不了,
# 预检也过不了),GET 是只读的,风险只在读响应,Host 校验已覆盖。
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}

log = logging.getLogger("atlas.web")


def _check_id(value: str, what: str) -> str:
    if not _SAFE_ID_RE.match(value) or ".." in value:
        raise HTTPException(404, f"没有这个{what}:{value!r}")
    return value


def create_app(workflows_dir: Path = DEFAULT_WORKFLOWS_DIR,
               runs_dir: Path = DEFAULT_RUNS_DIR,
               registry_factory=None,
               providers_path: Path | None = None,
               env_store=None,
               agent_config_path: Path | None = None,
               agent_runner_factory=None,
               *,
               api_only: bool = False,
               web_dist_dir: Path | None = None,
               mount_mcp: bool = True) -> FastAPI:
    """registry_factory(provider_ids) → AdapterRegistry。

    生产环境不传(用 build_real_registry,真实密钥);测试注入假供应商,
    让 Web 层的每个端点都能走真实路径而不花钱。
    providers_path / env_store 同理:配置面 API 的注入点。
    ``api_only`` 只供明确不测试静态界面的 API 测试使用；生产默认仍要求
    已构建的 web/dist。``web_dist_dir`` 用于隔离该启动契约的测试。
    ``mount_mcp`` 把 MCP streamable-http 端点挂到 /mcp;测试可关闭。
    """
    app = FastAPI(title="Atlas 观测界面", version=__version__)
    workflows_dir = Path(workflows_dir)
    runs_dir = Path(runs_dir)
    _registry_factory = registry_factory or build_real_registry
    _agent_runner_factory = agent_runner_factory or (
        lambda spec: prepare_production_agent_runner(
            spec, providers_path=providers_path, env_store=env_store))
    # 删除事务很短且低频；串行化可让同进程的重复 DELETE 稳定收敛为
    # 首个成功、后续 404；稳定 .locks OS 锁保护跨进程/跨操作竞争。
    _delete_guard = threading.Lock()

    @app.exception_handler(RequestValidationError)
    async def _strip_422_input(request: Request, exc: RequestValidationError):
        """FastAPI 的 422 默认回显请求体 input——客户端刚提交的密钥若
        恰在坏请求里会被原样弹回。剥掉它(A8 补充面,M3 审查🟡10)。"""
        return JSONResponse(status_code=422,
                            content={"detail": "请求体不合法(字段类型/结构错误)"})

    @app.middleware("http")
    async def _local_only(request: Request, call_next):
        host = request.headers.get("host") or ""
        # IPv6 字面量形如 [::1]:8321,冒号在方括号内,不能直接 rsplit(":")
        if host.startswith("["):
            host = host.split("]", 1)[0] + "]"
        else:
            host = host.rsplit(":", 1)[0]
        if host not in _LOCAL_HOSTS:
            return JSONResponse(status_code=403, content={
                "detail": "界面只服务本机(Host 头不是 127.0.0.1/localhost)。"
                          "红线 ④:暴露到网络等于暴露执行权"})
        # /mcp 是 MCP harness 的 streamable-http 端点:客户端不会带
        # X-Atlas-Request,该头只对浏览器跨站有意义;Host/回环校验仍适用。
        if request.method in ("POST", "PUT", "DELETE") \
                and not request.url.path.startswith("/mcp") \
                and request.headers.get("X-Atlas-Request") != "1":
            # 浏览器里的恶意网页可以用 no-cors 发简单请求,但带不了自定义头
            return JSONResponse(status_code=403, content={
                "detail": "写操作要求 X-Atlas-Request: 1 头(防浏览器跨站驱动)"})
        return await call_next(request)

    # ── workflows ──────────────────────────────────────────────

    def _structure_tags(spec) -> list[str]:
        """从图形状推导结构标签(展示用,不参与执行)。"""
        tags: list[str] = []
        if len(spec.all_entries()) > 1:
            tags.append("多入口")
        sources = {e.source for e in spec.edges}
        # 并行 = 无条件扇出(一个节点多条无条件出边);条件出边是路由不是并行
        if any(sum(1 for e in spec.edges if e.source == s and not e.when) > 1
               for s in sources):
            tags.append("并行")
        if any(e.when for e in spec.edges):
            tags.append("条件分支")
        # 回边:b 可达 a 则 a→b 成环
        def _reaches(a: str, b: str) -> bool:
            stack = [t for e in spec.edges if e.source == a for t in [e.target]]
            seen: set[str] = set()
            while stack:
                cur = stack.pop()
                if cur == b:
                    return True
                if cur in seen or cur == "END":
                    continue
                seen.add(cur)
                stack += [e.target for e in spec.edges if e.source == cur]
            return False
        if any(e.target != "END" and _reaches(e.target, e.source) for e in spec.edges):
            tags.append("有限循环")
        types = {n.type for n in spec.nodes}
        if "human" in types:
            tags.append("人工审批")
        if "coding_agent" in types:
            tags.append("改码")
        if "research" in types:
            tags.append("调研")
        if len({n.model.partition(":")[0] for n in spec.nodes if n.model}) > 1:
            tags.append("多供应商")
        return tags

    def _load_workflow(wid: str):
        path = workflows_dir / f"{wid}.yaml"
        if not path.exists():
            raise HTTPException(404, f"没有这个工作流:{wid}")
        return spec_from_yaml_file(path)   # 校验不过 → SpecError(在 handler 里转 400)

    @app.get("/api/workflows")
    def list_workflows():
        items = []
        for path in sorted(workflows_dir.glob("*.yaml")):
            try:
                spec = spec_from_yaml_file(path)
                items.append({"id": path.stem, "name": spec.name,
                              "description": spec.description, "valid": True,
                              "error": None,
                              "meta": spec.meta.as_dict() or None,
                              "node_count": len(spec.nodes),
                              "structure_tags": _structure_tags(spec)})
            except SpecError as e:
                items.append({"id": path.stem, "name": path.stem,
                              "description": "", "valid": False, "error": str(e)})
        return items

    def _workflow_payload(spec, wid: str = "") -> dict:
        """公开的非秘密工作流视图；基础定义与历史有效快照共用。"""
        return {
            "id": wid,
            "name": spec.name,
            "description": spec.description,
            "entry": spec.entry,
            "entries": list(spec.entries) if spec.entries else [],
            # P13:fork 声明进公开视图(只观测;闭包/复用清单在运行时
            # 账本的 fork_planned 事件里,走事件接口看)
            "fork": ({"run": spec.fork.run} if spec.fork is not None else None),
            "meta": spec.meta.as_dict() or None,
            "structure_tags": _structure_tags(spec),
            "nodes": [{
                "id": n.id, "type": n.type, "model": n.model,
                "fallback": n.fallback, "prompt": n.prompt,
                "consumes": n.consumes,
                "required_fields": n.required_fields or [],
                "route_field": n.route_field,
                "max_output_tokens": n.max_output_tokens,
                "thinking": n.thinking,
                "temperature": n.temperature,
                "seed": n.seed,
                "timeout_s": n.timeout_s,
                "retry": n.retry,
                "writable": n.writable,
                "allow_web": n.allow_web,
                "workdir": n.workdir,
                "max_turns": n.max_turns,
            } for n in spec.nodes],
            "edges": [{"from": e.source, "to": e.target, "when": e.when}
                      for e in spec.edges],
            "guards": {
                "max_iterations": spec.guards.max_iterations,
                "max_iterations_effective": spec.guards.effective_max_iterations,
                "max_cost_usd": spec.guards.max_cost_usd,
                "timeout_s": spec.guards.timeout_s,
            },
        }

    def _param_defaults(spec) -> dict[str, dict]:
        """每个节点"空输入框背后"的真实生效默认值,供前端灰显。

        只有无秘密标量;唯一随配置变的是 max_output_tokens 的供应商上限
        (providers.json 的 maxOutputTokens,缺省 8192)。默认值以引擎与
        适配器的实现为准:llm 单次调用 300s,agent CLI 1800s;temperature/
        seed 留空时不发字段,由供应商自己默认。
        """
        try:
            configs = load_provider_configs()
        except Exception:
            configs = {}
        defaults: dict[str, dict] = {}
        for n in spec.nodes:
            if n.type == "llm":
                cap = None
                if n.model:
                    cfg = configs.get(n.model.partition(":")[0])
                    cap = cfg.max_output_tokens if cfg and cfg.max_output_tokens \
                        else 8192
                defaults[n.id] = {
                    "max_output_tokens": cap,   # None=模型未选,上限还定不了
                    "temperature": None,        # 留空=不发,供应商默认
                    "seed": None,
                    "timeout_s": 300,
                    "retry": 0,
                }
            elif n.type in ("research", "coding_agent"):
                defaults[n.id] = {
                    "timeout_s": 1800, "retry": 0, "max_turns": 12,
                }
        return defaults

    @app.get("/api/workflows/{wid}")
    def get_workflow(wid: str):
        _check_id(wid, "工作流")
        try:
            spec = _load_workflow(wid)
        except SpecError as e:
            raise HTTPException(400, str(e))
        return _workflow_payload(spec, wid)

    @app.delete("/api/workflows/{wid}")
    def delete_workflow(wid: str, request: Request):
        """删除图定义文件(与 MCP 的 atlas_delete_workflow 同一实现)。
        界面按钮自带确认;内置示例默认保护,需 allow_example 查询参数。"""
        from atlas.mcp import delete_workflow_impl
        _check_id(wid, "工作流")
        result = delete_workflow_impl(
            wid, confirm=True,
            allow_example="allow_example" in request.query_params,
            workflows_dir=workflows_dir)
        if not result.get("deleted"):
            status = 404 if "没有这个工作流" in result.get("error", "") else 400
            raise HTTPException(status, result.get("error", "删除失败"))
        return result

    @app.post("/api/workflows/{wid}/preview")
    def preview_run(wid: str, body: dict):
        """零成本预览本次将执行的具体规格；不分配 run_id、不建目录。

        模型未配置(示例在新机器上的正常状态)不是错误:返回
        unconfigured_nodes 清单,图与覆盖仍可预览;运行接口才会拒绝。
        """
        _check_id(wid, "工作流")
        unknown = set(body) - {"node_overrides"}
        if unknown:
            raise HTTPException(400, f"预览请求有未知字段:{sorted(unknown)}")
        try:
            base_spec = _load_workflow(wid)
            effective = build_effective_spec(base_spec, body.get("node_overrides"))
            execution_sha256 = None
            prepared = None
            if not effective.unconfigured_nodes:
                registry = _registry_factory(provider_ids_for_spec(effective.spec))
                agent_runner = _agent_runner_factory(effective.spec)
                prepared = prepare_execution(
                    effective.spec, registry, agent_runner=agent_runner)
                execution_sha256 = prepared.execution_sha256
        except Exception as e:
            raise HTTPException(400, f"运行前预览不通过:{e}")
        return {
            "effective_workflow": _workflow_payload(effective.spec, wid),
            "base_spec_sha256": effective.base_fingerprint,
            "effective_spec_sha256": effective.effective_fingerprint,
            "execution_sha256": execution_sha256,
            "bindings": list(effective.bindings),
            "overrides": list(effective.overrides),
            "unconfigured_nodes": list(effective.unconfigured_nodes),
            "prompt_overridden": list(effective.prompt_overridden),
            "param_defaults": _param_defaults(effective.spec),
        }

    # ── 运行 ───────────────────────────────────────────────────

    @app.post("/api/workflows/{wid}/run")
    def start_run(wid: str, body: dict):
        _check_id(wid, "工作流")
        unknown = set(body) - {"task", "node_overrides", "expected_execution_sha256"}
        if unknown:
            raise HTTPException(400, f"运行请求有未知字段:{sorted(unknown)}")
        task = body.get("task")
        if not isinstance(task, str) or not task.strip():
            raise HTTPException(400, "task 必须是非空字符串")
        task = task.strip()
        if len(task.encode("utf-8")) > TASK_MAX_BYTES:
            raise HTTPException(
                413, f"task 超过上限 {TASK_MAX_BYTES} 字节;拒绝截断")
        try:
            base_spec = _load_workflow(wid)
            effective = build_effective_spec(base_spec, body.get("node_overrides"))
            if effective.unconfigured_nodes:
                nodes = ", ".join(effective.unconfigured_nodes)
                raise SpecError(
                    f"节点 {nodes} 未配置模型:请在节点详情中选择,"
                    "或让 AI 写入 workflows/*.yaml")
            provider_ids = provider_ids_for_spec(effective.spec)
            registry = _registry_factory(provider_ids)
            agent_runner = _agent_runner_factory(effective.spec)
            prepared = prepare_execution(
                effective.spec, registry, agent_runner=agent_runner)
        except Exception as e:
            raise HTTPException(400, f"运行前校验不通过，未分配 run_id:{e}")

        expected_execution = body.get("expected_execution_sha256")
        if expected_execution is not None and (
                not isinstance(expected_execution, str)
                or expected_execution != prepared.execution_sha256):
            raise HTTPException(
                409, "expected_execution_sha256 与当前执行配置不符;"
                     "未分配 run_id、未创建锁或运行目录")

        try:
            run_id = launcher.start_background_run(
                effective.spec, task=task, runs_root=runs_dir,
                registry=registry, agent_runner=agent_runner,
                prepared=prepared,
                base_spec_sha256=effective.base_fingerprint,
                binding_summary=effective.bindings,
                override_summary=effective.overrides,
                logger=log)
        except RunConflictError as e:
            raise HTTPException(409, str(e)) from e
        except Exception as e:
            raise HTTPException(500, f"后台启动失败,运行未开始:{e}") from e
        return {"run_id": run_id}

    # ── runs ───────────────────────────────────────────────────

    def _run_events(rid: str, *, wait: float = 0.0) -> list[dict] | None:
        path = runs_dir / rid / "events.jsonl"
        deadline = time.time() + wait
        while True:
            if path.exists():
                return EventReader(path).all()
            if time.time() >= deadline:
                return None
            time.sleep(0.1)

    @app.get("/api/runs")
    def list_runs():
        listing = list_run_summaries(
            runs_dir, limit=10_000,
            active_ids=set(launcher.REGISTRY.active_ids()))
        return [{
            "run_id": r["run_id"],
            "graph": r["graph"],
            "status": r["status"],
            "nodes_done": len(r["nodes_done"]),
            "started": r["started"],
            # P10:star 保护标记(有标记的 run 拒绝删除/retention)
            "star": has_star(runs_dir, r["run_id"]),
        } for r in listing["runs"]]

    @app.post("/api/runs/{rid}/star")
    def star_run(rid: str, body: dict | None = None):
        """P10 write-once star 标记:任何有账本的 run(含 running)可标,
        已标 → 409;取消 star 无 API(手工删文件),防自动化误清保护。"""
        _check_id(rid, "运行")
        body = body or {}
        unknown = set(body) - {"note"}
        if unknown:
            raise HTTPException(400, f"未知字段:{sorted(unknown)}")
        note = body.get("note", "")
        if not isinstance(note, str):
            raise HTTPException(400, "note 必须是字符串")
        try:
            payload = set_star(rid, runs_root=runs_dir, note=note)
        except RunNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except FileExistsError as exc:
            raise HTTPException(409, f"运行 {rid!r} 已有 star 标记") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"starred": rid, **payload}

    @app.get("/api/runs/{rid}/star")
    def get_star(rid: str):
        _check_id(rid, "运行")
        if not (runs_dir / rid / "events.jsonl").is_file():
            raise HTTPException(404, f"没有这个运行:{rid}")
        return read_star(rid, runs_root=runs_dir)

    def _delete_run_locked(rid: str):
        """HTTP 映射层:删除语义全部在 runs.isolate_and_delete_run(P10 起
        与 retention sweep 共用同一实现),这里只做错误→状态码翻译。"""
        _check_id(rid, "运行")
        try:
            return isolate_and_delete_run(rid, runs_root=runs_dir)
        except RunConflictError as exc:
            raise HTTPException(423, str(exc)) from exc
        except RunNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RunNotDeletable as exc:
            raise HTTPException(409, str(exc)) from exc
        except (RuntimeError, OSError) as exc:
            raise HTTPException(500, str(exc)) from exc

    @app.delete("/api/runs/{rid}")
    def delete_run(rid: str):
        """仅删除 done/failed/cancelled;运行锁和重复请求都不会绕过终态复核。"""
        with _delete_guard:
            return _delete_run_locked(rid)

    def _thinking_summary(model_used: str | None, tier, reasoning_tokens,
                          reasoning_kind: str) -> dict:
        """三层思考语义(PLAN-v3 M6-D):能力 / 请求档位 / 响应证据。

        能力来自 capabilities 探测表;未探测的模型标 unprobed 而不是猜。
        响应证据按协议分:usage_reasoning_tokens=精确数量,
        thinking_block=只有存在性(value 是哨兵 1,不是 token 数)。
        """
        cap = model_capability(model_used) if model_used and ":" in model_used else "unknown"
        if cap == "unknown":
            cap = "unprobed"   # 与 /api/thinking-capabilities 同一枚举(审查 M6-minor8)
        if tier:
            requested = tier
        else:
            requested = "provider_default"
        if reasoning_kind == "usage_reasoning_tokens":
            evidence = {"kind": "reasoning_tokens", "value": reasoning_tokens}
        elif reasoning_kind == "thinking_block":
            evidence = {"kind": "thinking_block", "value": None}
        elif reasoning_tokens:
            # 旧事件没有 reasoning_kind 字段:有数量按数量报,只有 1 无法区分
            evidence = {"kind": "reasoning_tokens", "value": reasoning_tokens} \
                if reasoning_tokens > 1 else {"kind": "unknown", "value": None}
        else:
            evidence = {"kind": "none", "value": None}
        return {"capability": cap, "requested_tier": requested,
                "evidence": evidence}

    @app.get("/api/runs/{rid}")
    def get_run(rid: str):
        _check_id(rid, "运行")
        events = _run_events(rid, wait=0.3 if launcher.REGISTRY.is_active(rid) else 0.0)
        if not events:
            # 空/缺失账本不能证明 run 存在；只有本进程刚分配且线程仍登记的
            # id 才能暴露短暂的 starting 状态。
            if not launcher.REGISTRY.is_active(rid):
                raise HTTPException(404, f"没有这个运行:{rid}")
            return {"run_id": rid, "status": "starting", "nodes": [],
                    "nodes_done": [], "artifacts": {},
                    "totals": {"input_tokens": 0, "output_tokens": 0,
                               "known_actual_cost_usd": 0.0,
                               "accounted_cost_usd": 0.0,
                               "actual_cost_unknown_count": 0,
                               "outstanding_reserved_usd": 0.0,
                               "cost_usd": None},
                    "failed_error": None}
        folded = fold_events(events)
        dynamic_status = derive_run_status(
            events, run_id=rid, runs_root=runs_dir,
            active_controller=launcher.REGISTRY.is_active(rid))
        # 每个节点的最新状态(循环的节点保留最后一次,attempts 全留)
        nodes: dict[str, dict] = {}
        run_failed = folded["status"] == "failed"
        for e in events:
            t, nid = e.get("type"), e.get("node")
            if nid is None:
                continue
            slot = nodes.setdefault(nid, {"id": nid, "status": "pending",
                                          "attempts": []})
            if t == "node_input":
                slot["projection_path"] = e["projection_path"]
                slot["projection_sha256"] = e["projection_sha256"]
                slot["consumed"] = e["consumed"]
                slot["iteration"] = e["iteration"]
            elif t == "node_started":
                slot["status"] = "running"
                slot["model_requested"] = e["model_requested"]
                if e.get("runner"):
                    slot["runner"] = e["runner"]
            elif t == "model_failed":
                slot["attempts"].append({"model": e["model"], "reason": e["reason"]})
            elif t == "output_truncated":
                slot["output_truncated"] = True
            elif t == "output_json_recovered":
                slot["json_recovered"] = e.get("how", "")
            elif t == "node_failed_soft":
                # P3 软失败:节点按 on_error 继续/分支,图仍在跑
                slot.update({
                    "status": "failed_soft",
                    "error_class": e.get("error_class"),
                    "on_error": e.get("on_error"),
                    "output_path": e.get("output_path"),
                    "output_sha256": e.get("output_sha256"),
                    "artifacts": e.get("artifacts", []),
                    "iteration": e.get("iteration"),
                })
            elif t == "node_done":
                slot.update({
                    "status": "done",
                    "model_used": e["model_used"],
                    "model_requested": e["model_requested"],
                    "degraded": e["degraded"],
                    "output_truncated": e.get("output_truncated", False),
                    "output_path": e["output_path"],
                    "output_sha256": e["output_sha256"],
                    "artifacts": artifacts_from_event(e),
                    "input_tokens": e["input_tokens"],
                    "output_tokens": e["output_tokens"],
                    "reasoning_tokens": e.get("reasoning_tokens", 0),
                    "reasoning_kind": e.get("reasoning_kind", ""),
                    "thinking_tier": e.get("thinking_tier"),
                    "thinking": _thinking_summary(
                        e["model_used"], e.get("thinking_tier"),
                        e.get("reasoning_tokens", 0),
                        e.get("reasoning_kind", "")),
                    "cost_usd": e.get("cost_usd"),
                    "runner": e.get("runner", slot.get("runner")),
                    "duration_s": e["duration_s"],
                    "iteration": e["iteration"],
                    "param_audit": e.get("param_audit"),
                })
        # 启动了但没做完的节点:按运行终态标 failed(而不是永远 running)
        if folded["status"] in ("failed", "done"):
            for slot in nodes.values():
                if slot["status"] == "running":
                    slot["status"] = "failed" if run_failed else "done"
        accounting = fold_cost_accounting(events)
        totals = {
            "input_tokens": sum(e.get("input_tokens") or 0 for e in events
                                if e["type"] == "node_done"),
            "output_tokens": sum(e.get("output_tokens") or 0 for e in events
                                 if e["type"] == "node_done"),
            "known_actual_cost_usd": accounting.known_actual_usd,
            "accounted_cost_usd": accounting.accounted_usd,
            "actual_cost_unknown_count": accounting.unknown_count,
            "outstanding_reserved_usd": accounting.outstanding_reserved_usd,
            # 兼容旧 UI；存在未知费用时不得冒充完整实际总额。
            "cost_usd": (accounting.known_actual_usd
                         if accounting.unknown_count == 0 else None),
        }
        cost_warnings = [
            {"models": e.get("models"), "reason": e.get("reason")}
            for e in events if e["type"] == "cost_unknown"]
        effective_workflow = None
        try:
            effective_workflow = _workflow_payload(_spec_for_run(rid), rid)
        except HTTPException:
            # 历史旧 run 可能没有快照且对应 YAML 已删除；运行账本仍应可看。
            pass
        return {"run_id": rid, **folded, "status": dynamic_status,
                "nodes": sorted(nodes.values(), key=lambda n: n["id"]),
                "totals": totals,
                "cost_warnings": cost_warnings,
                "effective_workflow": effective_workflow,
                "failed_error": next((e.get("error") for e in reversed(events)
                                      if e["type"] == "run_failed"), None),
                # S1 终局视图:与 MCP atlas_get_run 同一构建函数,纯账本派生
                "finale": build_finale(events, runs_dir / rid)}

    @app.get("/api/runs/{rid}/events")
    def stream_events(rid: str, after: int = 0):
        _check_id(rid, "运行")
        events = _run_events(rid, wait=5.0 if launcher.REGISTRY.is_active(rid) else 0.0)
        if not events:
            raise HTTPException(404, f"没有这个运行:{rid}")
        path = runs_dir / rid / "events.jsonl"

        def gen():
            last = after
            offset = 0
            terminal_seen = False
            idle_rounds = 0
            reader = EventReader(path)
            while not terminal_seen:
                records, offset = reader.read_from(offset)
                if records:
                    # 即使 after 过滤掉了这批事件，读侧也确实有进展；idle 只表示
                    # 账本没有新增字节，不能按是否向客户端 yield 来计算。
                    idle_rounds = 0
                for r in records:
                    if r["seq"] <= last:
                        continue
                    last = r["seq"]
                    yield f"data: {json.dumps(r, ensure_ascii=False)}\n\n"
                    if r["type"] in ("run_done", "run_failed"):
                        terminal_seen = True
                if records:
                    continue
                elif not terminal_seen:
                    idle_rounds += 1
                    # 长事件空窗(推理模型一跑几分钟)期间发 SSE 注释保活,
                    # 防止中间层把空闲连接掐掉
                    if idle_rounds % 30 == 0:
                        yield ": keepalive\n\n"
                    # interrupted 是事件+实时锁派生视图，不写伪造账本事件，
                    # 也不用持久事件的 seq 命名空间推进客户端游标。
                    current = reader.all()
                    if derive_run_status(
                            current, run_id=rid, runs_root=runs_dir,
                            active_controller=launcher.REGISTRY.is_active(rid)) == "interrupted":
                        yield ('event: run_interrupted\n'
                               'data: {"type": "run_interrupted"}\n\n')
                        break
                    # 无法确认状态时 fail-closed，但连接也不能无限占用。
                    if idle_rounds > 200:   # ~60s 无新事件
                        yield 'data: {"seq": -1, "ts": "", "type": "stream_closed"}\n\n'
                        break
                    time.sleep(0.3)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    def _serve_run_file(rid: str, sub: str, filename: str) -> FileResponse:
        _check_id(rid, "运行")
        # 只允许子目录里的扁平文件名,杜绝路径穿越。
        # 白名单而非黑名单:Windows 上 "C:secret.txt" 不含 / \ ..,但
        # Path(base) / "C:secret.txt" 会整体重置成 WindowsPath("C:secret.txt")
        # (drive-relative,实测已复现),原有黑名单挡不住这条——安全审查发现的真漏洞。
        if not _SAFE_ID_RE.match(filename) or ".." in filename:
            raise HTTPException(400, "非法文件名")
        path = runs_dir / rid / sub / filename
        if not path.is_file() or not path.resolve().is_relative_to(
                (runs_dir / rid / sub).resolve()):
            raise HTTPException(404, f"没有这个文件:{sub}/{filename}")
        return FileResponse(path, media_type="text/plain; charset=utf-8")

    @app.get("/api/runs/{rid}/events.jsonl")
    def get_full_ledger(rid: str):
        """完整事件账本下载(体验债 2b:界面事件流只保留最近若干条)。"""
        _check_id(rid, "运行")
        path = runs_dir / rid / "events.jsonl"
        if not path.is_file() or not path.resolve().is_relative_to(
                (runs_dir / rid).resolve()):
            raise HTTPException(404, f"没有这个运行:{rid}")
        return FileResponse(path, media_type="application/x-ndjson")

    @app.get("/api/runs/{rid}/artifacts/{filename}")
    def get_artifact(rid: str, filename: str):
        return _serve_run_file(rid, "artifacts", filename)

    @app.get("/api/runs/{rid}/projections/{filename}")
    def get_projection(rid: str, filename: str):
        return _serve_run_file(rid, "projections", filename)

    def _spec_for_run(rid: str):
        """批复要用的 spec:优先 run 目录里的快照(M2 起每次运行都落盘),
        旧 run 退回按 graph 名扫 workflows/。指纹校验在 approve_run 里兜底。"""
        snapshot = runs_dir / rid / "spec.snapshot.json"
        if snapshot.is_file():
            try:
                return spec_from_snapshot(json.loads(
                    snapshot.read_text(encoding="utf-8")), source=str(snapshot))
            except Exception as e:
                raise HTTPException(500, f"run {rid} 的 spec 快照损坏:{e}")
        events = _run_events(rid, wait=0.5)
        graph = fold_events(events or [])["graph"] if events else None
        if not graph:
            raise HTTPException(404, f"run {rid!r} 没有图名,找不到定义")
        for path in workflows_dir.glob("*.yaml"):
            try:
                spec = spec_from_yaml_file(path)
                if spec.name == graph:
                    return spec
            except SpecError:
                continue
        raise HTTPException(404, f"workflows/ 里找不到名为 {graph!r} 的图")

    @app.post("/api/runs/{rid}/resume", status_code=202)
    def resume(rid: str):
        """仅恢复动态判定为 interrupted 的运行；paused 仍只能审批。"""
        _check_id(rid, "运行")
        try:
            lock_interrupted_run(
                rid, runs_root=runs_dir,
                active_controller=launcher.REGISTRY.is_active(rid))
        except RunConflictError as e:
            raise HTTPException(409, str(e)) from e
        except RunNotFoundError as e:
            raise HTTPException(404, str(e)) from e

        try:
            spec = _spec_for_run(rid)
            registry = _registry_factory(provider_ids_for_spec(spec))
            agent_runner = _agent_runner_factory(spec)
            prepared = prepare_execution(spec, registry, agent_runner=agent_runner)
            validate_interrupted_run_locked(
                rid, runs_root=runs_dir, prepared=prepared)
        except SpecError as e:
            release_run_lock(rid, runs_root=runs_dir)
            raise HTTPException(409, str(e)) from e
        except HTTPException:
            release_run_lock(rid, runs_root=runs_dir)
            raise
        except Exception as e:
            release_run_lock(rid, runs_root=runs_dir)
            raise HTTPException(400, f"运行后端配置不通过:{e}") from e

        def _execute_resume():
            try:
                resume_graph(
                    rid, spec=spec, runs_root=runs_dir, prepared=prepared,
                    _lock_held=True)
            except Exception:
                log.exception("run %s 后台恢复异常", rid)

        try:
            launcher.spawn_controller(rid, _execute_resume, runs_root=runs_dir)
        except Exception as e:
            raise HTTPException(500, f"后台恢复启动失败:{e}") from e
        return {"run_id": rid, "status": "running"}

    @app.post("/api/runs/{rid}/approve")
    def approve(rid: str, body: dict):
        """对暂停在 human 节点的运行给出批复(approve/reject)。"""
        _check_id(rid, "运行")
        decision = str(body.get("decision", ""))
        if decision not in ("approve", "reject"):
            raise HTTPException(400, "decision 只能是 approve/reject")
        comment = str(body.get("comment", "") or "")
        if decision == "reject" and not comment.strip():
            # 驳回不留理由 = 下游(与未来的你)无法追溯为什么否决;
            # 批准时理由可省。同步 400,不触碰运行锁。
            raise HTTPException(400, "驳回必须填写理由(comment 不能为空)")
        spec = _spec_for_run(rid)

        provider_ids = provider_ids_for_spec(spec)
        try:
            registry = _registry_factory(provider_ids)
            agent_runner = _agent_runner_factory(spec)
            prepared = prepare_execution(
                spec, registry, agent_runner=agent_runner)
        except Exception as e:
            raise HTTPException(400, f"运行后端配置不通过:{e}")

        try:
            lock_approval_run(rid, spec=spec, runs_root=runs_dir,
                              prepared=prepared)
        except RunConflictError as e:
            # RunConflictError 同时继承 RunNotFoundError；必须优先映射 409。
            raise HTTPException(409, str(e)) from e
        except RunNotFoundError as e:
            raise HTTPException(404, str(e)) from e
        except SpecError as e:
            raise HTTPException(409, str(e)) from e

        def _execute():
            try:
                approve_run(rid, decision=decision, comment=comment, spec=spec,
                            runs_root=runs_dir, prepared=prepared,
                            _lock_held=True)
            except Exception:
                # approve_run 对预占锁也在 finally 中负责释放；这里不能二次删除，
                # 否则可能误删紧随其后另一请求刚创建的新锁。
                log.exception("run %s 批复后执行异常", rid)

        try:
            launcher.spawn_controller(
                rid, _execute, runs_root=runs_dir,
                release_lock_on_start_failure=False)
        except Exception:
            release_approval_run_lock(rid, runs_root=runs_dir)
            raise
        return {"run_id": rid, "decision": decision}

    @app.post("/api/runs/{rid}/cancel")
    def cancel(rid: str, body: dict | None = None):
        """P2 协作式取消:写请求;paused/interrupted 直接锁内落终态。

        running 时返回 requested+running——在途模型调用只能等它返回或
        超时,不宣称任意时刻强杀;controller 在下一消费点终止。
        """
        _check_id(rid, "运行")
        reason = str((body or {}).get("reason", "") or "")
        try:
            result = request_cancel(
                rid, runs_root=runs_dir, reason=reason,
                active_controller=launcher.REGISTRY.is_active(rid))
        except RunConflictError as e:
            raise HTTPException(409, str(e)) from e
        except RunNotFoundError as e:
            raise HTTPException(404, str(e)) from e
        if result.get("status") == "cancelled":
            return {"run_id": rid, "status": "cancelled",
                    "request_id": result.get("request_id")}
        return {"run_id": rid, "status": "running", "requested": True,
                "request_id": result.get("request_id"),
                "note": "取消请求已落盘;在途调用等待返回,controller 将在下一节点边界终止"}

    # ── 配置面(供应商/密钥/模型白名单;PLAN-v2 M3)──────────────
    from atlas.configapi import register_config_routes
    register_config_routes(app, providers_path, env_store)

    @app.get("/api/agents-status")
    def agents_status():
        """agents.json 状态卡片(体验债 2b):只读事实,不含凭据。

        编辑仍走 config/agents.json 文件;这里回答设置页的三个问题:
        生产 agent 是否启用、CLI 预检能否通过、失败的具体原因。
        """
        from atlas.config import CONFIG_DIR, ConfigError, load_agent_config
        cfg_path = (Path(agent_config_path)
                    if agent_config_path is not None
                    else CONFIG_DIR / "agents.json")
        if not cfg_path.exists():
            return {"present": False, "runner": None, "command": None,
                    "cli_version": None, "status": "disabled",
                    "detail": "config/agents.json 不存在——生产 agent 执行"
                              "保持 fail-closed(这是刻意的安全默认)"}
        try:
            cfg = load_agent_config(cfg_path)
        except ConfigError as e:
            return {"present": True, "runner": None, "command": None,
                    "cli_version": None, "status": "error",
                    "detail": f"agents.json 无法解析:{e}"}
        if cfg.runner != "local_cli":
            return {"present": True, "runner": cfg.runner, "command": None,
                    "cli_version": None, "status": "disabled",
                    "detail": "runner 未设为 local_cli——生产 agent 执行关闭"}
        try:
            from atlas.nodes.local_cli import prepare_local_cli_runner
            runner = prepare_local_cli_runner(
                agent_config_path=cfg_path, providers_path=providers_path,
                env_store=env_store)
            version = ".".join(map(str, runner.cli_version))
            return {"present": True, "runner": cfg.runner,
                    "command": str(runner.program), "cli_version": version,
                    "status": "ready",
                    "detail": f"Claude CLI 预检通过(v{version})"}
        except Exception as e:
            return {"present": True, "runner": cfg.runner,
                    "command": getattr(getattr(cfg, "cli", None),
                                       "command", None),
                    "cli_version": None, "status": "error",
                    "detail": f"Claude CLI 预检未通过:{e}"}

    @app.get("/api/thinking-capabilities")
    def thinking_capabilities():
        """白名单里每个模型的思考能力(PLAN-v3 M6-D)。

        拉取到模型列表 ≠ 能力已探测:不在探测表里的模型标 unprobed,
        界面不得把它显示成「已配置」。探测证据字段如实转述,不加工。
        """
        caps = load_capabilities()
        out: dict[str, dict] = {}
        for pid, cfg in load_provider_configs().items():
            for model_id in cfg.models:
                ref = f"{pid}:{model_id}"
                entry = caps.get(ref)
                if entry is None:
                    out[ref] = {"kind": "unprobed"}
                else:
                    out[ref] = {
                        "kind": entry.get("kind"),
                        "evidence": ("reasoning_tokens"
                                     if entry.get("reasoning_tokens")
                                     else "thinking_block" if entry.get("blocks")
                                     else None),
                    }
        return out

    # ── MCP streamable-http 挂载 ────────────────────────────────
    # 单命令启动:atlas-web 同时服务 Web 界面与 MCP 端点。工具面与
    # stdio 入口(atlas-mcp)共用 build_mcp_server(),行为不会漂移。
    # Host/回环校验仍适用;X-Atlas-Request 对 MCP 客户端豁免(见中间件)。
    if mount_mcp:
        import contextlib
        from atlas.mcp import build_mcp_server

        # MCP streamable-http 端点。session manager 的生命周期必须由
        # FastAPI lifespan 驱动,且库内断言每个 manager 实例只能 run 一次;
        # 测试会对同一 app 反复进入 lifespan(TestClient 上下文),所以在
        # manager 未运行时才进入,退出时复位标记。
        mcp_server = build_mcp_server()
        mcp_starlette = mcp_server.streamable_http_app(json_response=True)
        mcp_manager = mcp_server._lowlevel_server._session_manager
        manager_running = False

        @contextlib.asynccontextmanager
        async def _combined_lifespan(app: FastAPI):
            nonlocal manager_running
            if manager_running:
                yield
                return
            manager_running = True
            try:
                async with mcp_manager.run():
                    yield
                # 干净关闭后复位库内的 run-once 标记,让同一 app 实例可以
                # 再次进入 lifespan(测试的 TestClient 上下文会反复进出);
                # 异常退出时不复位,复用仍会被拒绝(fail-closed)。
                mcp_manager._has_started = False
            finally:
                manager_running = False

        app.router.lifespan_context = _combined_lifespan
        # MCP 内部路由就是 "/mcp"。不能把它 Mount 进来:Mount 是全捕获,
        # 挂 "" 会抢在静态前端之前吃掉 GET /,挂 "/mcp" 又会因前缀剥离
        # 产生 /mcp → /mcp/ 重定向。改为把内部 Route 对象直接并入路由表:
        # Route 只精确匹配自己的 path,不影响 /api 与静态前端。
        # 注意 streamable_http_app() 每次调用都会新建一个 session manager
        # 并覆盖 server 上的引用,这里必须复用第一次构造的实例。
        for route in mcp_starlette.routes:
            app.router.routes.append(route)

    # ── 静态前端(构建产物)───────────────────────────────────────

    dist = (Path(web_dist_dir) if web_dist_dir is not None else
            Path(__file__).resolve().parent.parent / "web" / "dist")
    if not api_only:
        if not dist.is_dir() or not (dist / "index.html").is_file():
            raise RuntimeError(
                f"Web 构建产物缺失:{dist}。请先在项目根目录运行 "
                "`npm ci`，再运行 `npm run build`（工作目录 web/）。")
        app.mount("/", StaticFiles(directory=dist, html=True), name="web")

    return app


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """启动观测界面 + MCP streamable-http 端点。

    host 只接受 127.0.0.1(红线 ④,硬编码拒绝其他值)。
    """
    if host != DEFAULT_HOST:
        raise ValueError(
            f"界面只绑 {DEFAULT_HOST}(红线 ④:节点里有能改文件跑命令的 agent,"
            f"暴露到网络等于暴露执行权)。请求的 {host!r} 被拒绝。"
        )
    from atlas.config_init import initialize_runtime_config

    initialize_runtime_config()
    # 端口被占必须 fail-loud,而不是打印"已启动"横幅后静默退出——
    # 否则旧实例仍占着 8321(harness 连到的是旧代码),新进程误导排查。
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError as e:
        raise SystemExit(
            f"端口 {host}:{port} 已被占用({e.strerror})。最常见的原因是"
            "已有一个 atlas-web 实例在运行(它可能是旧版本代码)。"
            f"先找到并关闭它:netstat -ano | findstr :{port},"
            "再 taskkill /PID <pid> /F,然后重新启动。"
        ) from e
    finally:
        probe.close()

    import uvicorn
    print(f"Atlas Web 界面:   http://{host}:{port}")
    print(f"Atlas MCP 端点:   http://{host}:{port}/mcp (streamable-http)")
    uvicorn.run(create_app(), host=host, port=port)


def main() -> None:
    """``atlas-web`` 命令入口。"""
    from atlas.console import force_utf8_stdio

    force_utf8_stdio()
    serve()


if __name__ == "__main__":
    main()
