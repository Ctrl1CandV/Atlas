# -*- coding: utf-8 -*-
"""Atlas MCP 服务:6 个工具,刻意保持很小(架构第 7.1 节)。

harness 里的 agent 是这套工具的用户:写 YAML → validate(零成本)→
dry_run(零成本)→ run(真实调用才花钱)→ get_run 查账。

每个返回都带 next(下一步建议),减少 agent 来回试错。
用法:python -m atlas.mcp(stdio)。
"""
import json
import re
import threading
from pathlib import Path

from mcp.server.mcpserver.server import MCPServer

from atlas.adapters import build_real_registry
from atlas.config import PROJECT_ROOT
from atlas.costs import fold_cost_accounting
from atlas.effective import build_effective_spec, provider_ids_for_spec
from atlas.engine import (RunConflictError, RunNotFoundError, execute_graph,
                          lock_interrupted_run, prepare_execution,
                          prepare_production_agent_runner, release_run_lock,
                          resume_graph, validate_interrupted_run_locked)
from atlas.events import EventReader, fold_events
from atlas.runs import derive_run_status
from atlas.spec import (SpecError, spec_from_snapshot, spec_from_yaml,
                        spec_from_yaml_file, spec_to_snapshot)

WORKFLOWS_DIR = PROJECT_ROOT / "workflows"
RUNS_DIR = PROJECT_ROOT / "runs"

# 与 web.py 相同的 ID 白名单:拒绝路径穿越(MCP 是 harness 的主入口,
# 传来的 workflow_id/run_id 不可信)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
# Windows 保留设备名:CON.yaml 这类文件名在 Win 上会出诡异行为,直接拒
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4",
                 "COM5", "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2",
                 "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"}


def _check_id(value: str, what: str) -> str:
    if not _SAFE_ID_RE.match(value) or ".." in value:
        raise ValueError(f"没有这个{what}:{value!r}(id 只允许字母数字_.-,且不含 ..)")
    return value


def _check_new_id(value: str) -> str:
    """保存用的新 id:额外拒绝 Windows 保留名与纯点划线。"""
    _check_id(value, "工作流")
    if value.split(".")[0].upper() in _WIN_RESERVED:
        raise ValueError(f"id {value!r} 是 Windows 保留设备名,换个名字")
    if not re.search(r"[A-Za-z]", value):
        raise ValueError(f"id {value!r} 至少要含一个字母")
    return value


# 保存的进程内串行锁:检查与替换之间不是原子的,锁把窗口收窄到持锁期间
_SAVE_LOCK = threading.Lock()

server = MCPServer(
    name="atlas",
    title="Atlas 工作流引擎",
    instructions=(
        "本地多模型工作流引擎:你写 YAML 定义节点和边,引擎跑图,"
        "人在 Web 界面(http://127.0.0.1:8321)实时看每个节点的完整输入输出。"
        "纪律:先 atlas_validate_workflow,再 dry_run,过了才真跑;"
        "大图或新图必先 dry_run。"
    ),
)


def _render(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)


def _registry_factory_default(provider_ids):
    return build_real_registry(provider_ids)


# ─────────────────────────── 内部实现(可测) ───────────────────────────


def validate_workflow_impl(yaml_text: str = "", workflow_id: str = "") -> dict:
    """校验一份图定义。零成本:不解析模型、不花钱、不建目录。"""
    if not yaml_text and not workflow_id:
        return {"valid": False,
                "error": "要么传 yaml 文本,要么传 workflow_id(文件名去 .yaml)",
                "next": "补上参数再调"}
    if workflow_id:
        _check_id(workflow_id, "工作流")
    try:
        if yaml_text:
            spec = spec_from_yaml(yaml_text)
        else:
            spec = spec_from_yaml_file(WORKFLOWS_DIR / f"{workflow_id}.yaml")
    except SpecError as e:
        return {"valid": False, "error": str(e),
                "next": "按 error 里的指引改 YAML,再校验一次。零成本,随便试"}

    models = sorted({n.model for n in spec.nodes if n.type == "llm"} |
                    {f for n in spec.nodes if n.type == "llm" for f in n.fallback})
    providers = sorted({m.partition(":")[0] for m in models})
    llm_nodes = [n for n in spec.nodes if n.type == "llm"]
    node_providers = {
        n.id: n.model.partition(":")[0] for n in llm_nodes if n.model
    }
    # 异质性提示只比较已经选定模型的节点；两个空模型表示“待配置”，
    # 不是同一家供应商，更不能冒充一条独立性警告。
    same_vendor_pairs = []
    for i, a in enumerate(llm_nodes):
        for b in llm_nodes[i + 1:]:
            if (a.id in node_providers and b.id in node_providers
                    and node_providers[a.id] == node_providers[b.id]):
                same_vendor_pairs.append([a.id, b.id])
    return {
        "valid": True,
        "name": spec.name,
        "entry": spec.entry,
        "nodes": [{"id": n.id, "type": n.type, "model": n.model or "(无)",
                   "consumes": n.consumes} for n in spec.nodes],
        "edges": [{"from": e.source, "to": e.target, "when": e.when}
                  for e in spec.edges],
        "guards": {"max_iterations": spec.guards.max_iterations,
                   "max_cost_usd": spec.guards.max_cost_usd,
                   "timeout_s": spec.guards.timeout_s},
        "heterogeneity": {
            "providers_used": providers,
            "same_vendor_node_pairs": same_vendor_pairs,
            "note": ("存在同厂商节点对:交叉验证类流程里它们不算独立意见"
                     if same_vendor_pairs else "各 llm 节点厂商互不相同"),
        },
        "next": ("校验通过。先 atlas_run_workflow(dry_run=True) 渲染确认,"
                 "再真实运行。或直接把 YAML 存进 workflows/ 目录"),
    }


def save_workflow_impl(workflow_id: str, yaml_text: str,
                       expected_sha256: str = "",
                       workflows_dir: Path | None = None) -> dict:
    """受限保存(PLAN-v3 §2.6):只写 workflows/ 下的 .yaml,校验必须先过。

    安全边界:
    - id 白名单 + Windows 保留名拒绝 → 无路径穿越;
    - 新建时文件已存在 → 拒绝(不静默覆盖);
    - 更新时必须带 expected_sha256(上次读到的文件哈希)做乐观并发控制,
      不匹配 → 拒绝并回报当前哈希,绝不覆盖人工修改;
    - 原子写:同目录临时文件 + os.replace,写一半崩溃不留半份;
    - YAML 必须通过完整校验(结构/终止性/consumes)才落盘——
      没有保存"草稿"的口子,坏图进不了 workflows/。
    """
    import hashlib
    import os
    import tempfile

    try:
        _check_new_id(workflow_id)
    except ValueError as e:
        return {"saved": False, "error": str(e), "next": "id 不合法"}
    if not yaml_text.strip():
        return {"saved": False, "error": "yaml 不能为空",
                "next": "先写出图定义,用 atlas_validate_workflow 校验"}
    target = (workflows_dir or WORKFLOWS_DIR) / f"{workflow_id}.yaml"
    try:
        spec = spec_from_yaml(yaml_text, source=f"save:{workflow_id}")
    except SpecError as e:
        return {"saved": False, "error": f"校验不过,未保存:{e}",
                "next": "按 error 改 YAML;校验是零成本的,多试几次"}

    # 并发防护(审查 M6-major3):检查与替换之间不是原子的。
    # ①进程内锁串行化全部保存(MCP server 与测试同进程内的并发);
    # ②新建用 os.link 原子占位——目标已存在时立刻失败,不覆盖;
    # ③更新在锁内、替换前复核哈希,窗口收窄到锁内。
    # 跨进程的同时写入(本地单人工具的现实场景之外)由 ② 的 link 原子性兜住新建,
    # 更新的跨进程竞态在此场景下接受并如实返回结果。
    with _SAVE_LOCK:
        creating = not target.exists()
        current: str | None = None
        if not creating:
            current = hashlib.sha256(target.read_bytes()).hexdigest()
            if not expected_sha256:
                return {"saved": False, "error": (
                            f"{workflow_id} 已存在。更新必须传 expected_sha256"
                            f"(当前={current}),防止覆盖你不知道的修改"),
                        "next": "重新读文件,带上当前哈希再保存"}
            if expected_sha256 != current:
                return {"saved": False, "error": (
                            f"文件已被别人改过:expected={expected_sha256[:12]}… "
                            f"当前={current[:12]}…。拒绝覆盖"),
                        "current_sha256": current,
                        "next": "重读文件合并修改后再保存(乐观锁)"}

        warnings: list[str] = []
        llm_nodes = [n for n in spec.nodes if n.type == "llm"]
        same = set()
        for i, a in enumerate(llm_nodes):
            for b in llm_nodes[i + 1:]:
                if a.model and b.model \
                        and a.model.partition(":")[0] == b.model.partition(":")[0]:
                    same.add(f"{a.id}+{b.id}")
        if same:
            warnings.append(f"同厂商节点对 {sorted(same)}:交叉验证语义下不算独立意见")
        if spec.meta.kind == "example":
            warnings.append("meta.kind=example 声明这是示例;自定义图建议 kind: custom")

        target.parent.mkdir(parents=True, exist_ok=True)
        content = yaml_text if yaml_text.endswith("\n") else yaml_text + "\n"
        fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            if creating:
                try:
                    os.link(tmp, target)   # 原子占位:已存在 → FileExistsError
                except FileExistsError:
                    return {"saved": False, "error": (
                                f"{workflow_id} 刚被并发创建,本次未写入"),
                            "next": "重读该文件,合并后按更新流程保存"}
            else:
                # 锁内、替换前复核:expected_sha256 仍是磁盘上的内容
                if hashlib.sha256(target.read_bytes()).hexdigest() != expected_sha256:
                    return {"saved": False, "error": "替换前复核失败:文件又变了",
                            "next": "重读文件合并修改后再保存"}
                os.replace(tmp, target)
        finally:
            Path(tmp).unlink(missing_ok=True)   # link 后删 tmp 不影响 target

    from atlas.spec import spec_fingerprint
    return {
        "saved": True,
        "created": creating,
        "workflow_id": workflow_id,
        "path": str(target),
        "file_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "spec_fingerprint": spec_fingerprint(spec),
        "warnings": warnings,
        "next": ("保存成功。刷新 Web 列表可见;先 dry_run 确认渲染再真跑。"
                 "后续更新请带上本次返回的 file_sha256 作为 expected_sha256"),
    }


def _validate_task(task: object) -> str:
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task 必须是非空字符串")
    normalized = task.strip()
    from atlas.integrity import TASK_MAX_BYTES
    if len(normalized.encode("utf-8")) > TASK_MAX_BYTES:
        raise ValueError(f"task 超过上限 {TASK_MAX_BYTES} 字节;拒绝截断")
    return normalized


def dry_run_impl(workflow_id: str, task: str, node_overrides=None, *,
                 providers_path: Path | None = None, env_store=None,
                 registry_factory=None, agent_runner_factory=None) -> dict:
    """用与真跑相同的有效规格渲染，不执行、不花钱、不创建 run。"""
    _check_id(workflow_id, "工作流")
    try:
        task = _validate_task(task)
    except ValueError as e:
        return {"error": str(e), "next": "提供不超过 1 MiB 的非空任务文本"}
    try:
        base_spec = spec_from_yaml_file(WORKFLOWS_DIR / f"{workflow_id}.yaml")
        effective = build_effective_spec(base_spec, node_overrides)
        spec = effective.spec
    except SpecError as e:
        return {"error": str(e), "next": "先让有效规格校验通过"}
    renders = []
    prompt_overridden = set(effective.prompt_overridden)
    for n in spec.nodes:
        prompt_chars = len(n.prompt)
        est_task_chars = len(task) if "task" in n.consumes else None
        upstream = [c for c in n.consumes if c != "task"]
        if n.model:
            model_label = n.model
        elif n.type == "llm":
            model_label = "未配置(待选择)"
        else:
            model_label = "human/agent"
        entry = {
            "node": n.id,
            "type": n.type,
            "model": model_label,
            "prompt": n.prompt,
            # 职责文本被本次运行覆盖时必须可见:渲染的是覆盖后的全文
            "prompt_overridden": n.id in prompt_overridden,
            "prompt_chars": prompt_chars,
            "consumes": n.consumes,
            "upstream_outputs_inline": upstream,
            "est_prompt_tokens": round(prompt_chars / 3
                                       + ((est_task_chars or 0) / 3
                                          if est_task_chars is not None else 0)),
        }
        if upstream:
            entry["est_prompt_tokens_note"] = (
                "不含上游产物体积(运行时才知道);完整产物会内联,见 A1")
        if n.type == "llm":
            entry["chain"] = [n.model, *n.fallback] if n.model else []
        renders.append(entry)
    unconfigured = list(effective.unconfigured_nodes)
    execution_sha256 = None
    if not unconfigured:
        try:
            factory = registry_factory or _registry_factory_default
            registry = factory(provider_ids_for_spec(spec))
            prepare = agent_runner_factory or (
                lambda workflow: prepare_production_agent_runner(
                    workflow, providers_path=providers_path,
                    env_store=env_store))
            agent_runner = prepare(spec)
            prepared = prepare_execution(
                spec, registry, agent_runner=agent_runner)
            execution_sha256 = prepared.execution_sha256
        except Exception as e:
            return {"error": f"运行前预检不通过:{e}",
                    "next": "检查 providers.json、config/.env、agents.json 与 Claude CLI"}
    next_step = ("渲染没问题就 atlas_run_workflow(dry_run=False) 真跑;"
                 "真跑才花钱,LLM 节点中位数几美分")
    if unconfigured:
        next_step = (f"节点 {', '.join(unconfigured)} 还没选模型:先在节点里"
                     "选择(node_overrides.model)或让 AI 把模型写进 YAML,"
                     "再真跑;未配置会被零成本拒绝")
    return {
        "dry_run": True,
        "workflow": workflow_id,
        "name": spec.name,
        "task_chars": len(task),
        "nodes": renders,
        "guards": {"max_iterations_effective": spec.guards.effective_max_iterations,
                   "max_cost_usd": spec.guards.max_cost_usd,
                   "timeout_s": spec.guards.timeout_s},
        "base_spec_sha256": effective.base_fingerprint,
        "effective_spec_sha256": effective.effective_fingerprint,
        "execution_sha256": execution_sha256,
        "bindings": list(effective.bindings),
        "overrides": list(effective.overrides),
        "unconfigured_nodes": unconfigured,
        "prompt_overridden": list(effective.prompt_overridden),
        "next": next_step,
    }


def run_workflow_impl(workflow_id: str, task: str, dry_run: bool = False,
                      registry_factory=None, node_overrides=None, *,
                      providers_path: Path | None = None, env_store=None,
                      agent_runner_factory=None,
                      expected_execution_sha256: str | None = None) -> dict:
    """跑一张图。同步阻塞到完成/暂停/失败;事件实时落盘,界面实时可见。"""
    if dry_run:
        return dry_run_impl(
            workflow_id, task, node_overrides,
            providers_path=providers_path, env_store=env_store,
            registry_factory=registry_factory,
            agent_runner_factory=agent_runner_factory)
    _check_id(workflow_id, "工作流")
    try:
        task = _validate_task(task)
    except ValueError as e:
        return {"error": str(e), "next": "提供不超过 1 MiB 的非空任务文本"}
    try:
        base_spec = spec_from_yaml_file(WORKFLOWS_DIR / f"{workflow_id}.yaml")
        effective = build_effective_spec(base_spec, node_overrides)
    except SpecError as e:
        return {"error": str(e), "next": "有效规格不合法,运行未开始(零成本拒绝)"}

    spec = effective.spec
    if effective.unconfigured_nodes:
        return {"error": f"节点 {', '.join(effective.unconfigured_nodes)} "
                         "未配置模型,运行未开始(零成本拒绝)。先在节点里选择"
                         "模型(node_overrides)或让 AI 写入 workflows/*.yaml",
                "next": "示例模型需要先配置再运行;dry_run 可零成本查看"}

    factory = registry_factory or _registry_factory_default
    try:
        registry = factory(provider_ids_for_spec(spec))
        prepare = agent_runner_factory or (
            lambda workflow: prepare_production_agent_runner(
                workflow, providers_path=providers_path, env_store=env_store))
        agent_runner = prepare(spec)
        prepared = prepare_execution(spec, registry, agent_runner=agent_runner)
    except Exception as e:
        return {"error": f"供应商或运行后端配置不通过,运行未开始:{e}",
                "next": "检查 config/.env、providers.json、agents.json 与 Claude CLI"}

    if (expected_execution_sha256 is not None
            and expected_execution_sha256 != prepared.execution_sha256):
        return {
            "error": "expected_execution_sha256 与当前执行配置不符，"
                     "运行未开始(零成本拒绝)",
            "next": "重新 dry_run 获取当前 execution_sha256 后再运行",
        }

    try:
        result = execute_graph(
            spec, task=task, runs_root=RUNS_DIR, prepared=prepared,
            base_spec_sha256=effective.base_fingerprint,
            binding_summary=effective.bindings,
            override_summary=effective.overrides)
    except Exception as e:
        return {"status": "failed", "error": f"{type(e).__name__}: {e}",
                "next": "看 runs/<id>/events.jsonl 里每次 model_failed 的原因;"
                        "备用链没配的节点值得配上(跨厂商)"}

    summary = summarize_run(result.run_id)
    if result.status == "paused":
        summary["next"] = ("运行暂停在 human 节点:去 Web 界面批准或驳回,"
                           "之后用 atlas_get_run 查询后续")
    return summary


def resume_run_impl(run_id: str, registry_factory=None, *,
                    providers_path: Path | None = None, env_store=None,
                    agent_runner_factory=None) -> dict:
    """同步恢复一个动态判定为 interrupted 的运行。"""
    _check_id(run_id, "运行")
    try:
        lock_interrupted_run(run_id, runs_root=RUNS_DIR)
    except RunConflictError as e:
        return {"error": str(e),
                "next": "只有 interrupted 可恢复;paused 请在 Web 审批,终态不能恢复"}
    except RunNotFoundError as e:
        return {"error": str(e), "next": "检查运行目录和 events.jsonl"}

    run_dir = RUNS_DIR / run_id
    snapshot = run_dir / "spec.snapshot.json"
    lock_transferred = False
    try:
        if not snapshot.is_file():
            return {"error": f"run {run_id!r} 缺少 spec.snapshot.json,无法恢复",
                    "next": "只能恢复带完整规格快照和执行身份的新运行"}
        try:
            spec = spec_from_snapshot(
                json.loads(snapshot.read_text(encoding="utf-8")), source=str(snapshot))
        except Exception as e:
            return {"error": f"有效规格快照损坏:{e}",
                    "next": "保留运行目录并检查 spec.snapshot.json 完整性"}

        factory = registry_factory or _registry_factory_default
        try:
            registry = factory(provider_ids_for_spec(spec))
            prepare = agent_runner_factory or (
                lambda workflow: prepare_production_agent_runner(
                    workflow, providers_path=providers_path, env_store=env_store))
            agent_runner = prepare(spec)
            prepared = prepare_execution(spec, registry, agent_runner=agent_runner)
            validate_interrupted_run_locked(
                run_id, runs_root=RUNS_DIR, prepared=prepared)
        except (RunConflictError, RunNotFoundError, SpecError) as e:
            return {"error": str(e),
                    "next": "当前执行配置、规格身份或 checkpoint 不允许恢复"}
        except Exception as e:
            return {"error": f"供应商或运行后端配置不通过,恢复未开始:{e}",
                    "next": "恢复必须使用与原运行一致的供应商、凭据版本和 agent 后端"}

        lock_transferred = True
        try:
            result = resume_graph(
                run_id, spec=spec, runs_root=RUNS_DIR, prepared=prepared,
                _lock_held=True)
        except Exception as e:
            return {"status": "failed", "error": f"{type(e).__name__}: {e}",
                    "next": "查看 events.jsonl 中 run_resumed 之后的失败事件"}
    finally:
        if not lock_transferred:
            release_run_lock(run_id, runs_root=RUNS_DIR)

    summary = summarize_run(result.run_id)
    if result.status == "paused":
        summary["next"] = ("恢复后暂停在 human 节点:去 Web 界面批准或驳回,"
                           "不能再次调用 atlas_resume_run 绕过审批")
    return summary


def summarize_run(run_id: str) -> dict:
    _check_id(run_id, "运行")
    path = RUNS_DIR / run_id / "events.jsonl"
    if not path.exists():
        return {"error": f"没有这个运行:{run_id}", "next": "用 atlas_list_workflows 查"}
    events = EventReader(path).all()
    folded = fold_events(events)
    dynamic_status = derive_run_status(
        events, run_id=run_id, runs_root=RUNS_DIR)
    nodes = {}
    for e in events:
        if e["type"] == "node_done":
            nodes[e["node"]] = {
                "model_used": e["model_used"],
                "degraded": e["degraded"],
                "input_tokens": e.get("input_tokens"),
                "output_tokens": e.get("output_tokens"),
                "duration_s": e.get("duration_s"),
                "output_path": e["output_path"],
            }
        elif e["type"] == "run_paused":
            nodes.setdefault(e.get("node"), {})["status"] = "等待批准"
    failed = next((e for e in reversed(events) if e["type"] == "run_failed"), None)
    started = next((e for e in events if e["type"] == "run_started"), {})
    effective_workflow = None
    snapshot = RUNS_DIR / run_id / "spec.snapshot.json"
    if snapshot.is_file():
        try:
            effective_workflow = spec_to_snapshot(spec_from_snapshot(
                json.loads(snapshot.read_text(encoding="utf-8")),
                source=str(snapshot)))
        except Exception as e:
            effective_workflow = {"error": f"有效规格快照损坏:{e}"}
    accounting = fold_cost_accounting(events)
    return {
        "run_id": run_id,
        "status": dynamic_status,
        "graph": folded["graph"],
        "nodes_done": folded["nodes_done"],
        "node_details": nodes,
        "totals": {
            "input_tokens": sum(e.get("input_tokens") or 0 for e in events
                                if e["type"] == "node_done"),
            "output_tokens": sum(e.get("output_tokens") or 0 for e in events
                                 if e["type"] == "node_done"),
            "known_actual_cost_usd": accounting.known_actual_usd,
            "accounted_cost_usd": accounting.accounted_usd,
            "actual_cost_unknown_count": accounting.unknown_count,
            "outstanding_reserved_usd": accounting.outstanding_reserved_usd,
            "cost_usd": (accounting.known_actual_usd
                         if accounting.unknown_count == 0 else None),
        },
        "run_dir": str(RUNS_DIR / run_id),
        "base_spec_sha256": started.get("base_spec_sha256"),
        "effective_spec_sha256": (started.get("effective_spec_sha256")
                                  or started.get("spec_sha256")),
        "bindings": started.get("bindings", []),
        "overrides": started.get("overrides", []),
        "effective_workflow": effective_workflow,
        "failed_error": failed.get("error") if failed else None,
        "next": ("打开 Web 界面看每个节点的完整输入输出;"
                 + ("账本与产物在 run_dir,可审计" if folded["status"] != "failed"
                    else "失败原因见 failed_error 与 model_failed 事件")),
    }


# ─────────────────────────── 工具封装 ───────────────────────────


@server.tool()
def atlas_validate_workflow(yaml: str = "", workflow_id: str = "") -> str:
    """校验一份图定义(零成本)。传 yaml 全文,或已保存的 workflow_id。

    检查:YAML 语法、节点类型封闭清单、边引用、条件边与路由字段、入口、
    可达性、死环、有环必须设 max_iterations、consumes 引用、异质性提示。
    返回 JSON；校验不过时 error 会指出具体字段或图结构问题，并统一附带 YAML
    path、line、column（整图聚合错误没有唯一坐标时只返回 path）。
    两个参数都传时以 yaml 全文为准。
    """
    try:
        return _render(validate_workflow_impl(yaml, workflow_id))
    except ValueError as e:
        return _render({"valid": False, "error": str(e), "next": "id 不合法"})


@server.tool()
def atlas_save_workflow(workflow_id: str, yaml: str,
                        expected_sha256: str = "") -> str:
    """把校验通过的 YAML 保存为 workflows/<workflow_id>.yaml(零成本)。

    校验不过不保存。新建要求 id 未被占用;更新必须传 expected_sha256
    (上次读到的文件哈希)——文件被改过就拒绝,防静默覆盖。
    返回 file_sha256(下次更新用它)与 spec_fingerprint。
    """
    try:
        return _render(save_workflow_impl(workflow_id, yaml, expected_sha256))
    except ValueError as e:
        return _render({"saved": False, "error": str(e), "next": "id 不合法"})


@server.tool()
def atlas_run_workflow(workflow_id: str, task: str, dry_run: bool = False,
                       node_overrides: dict | None = None,
                       expected_execution_sha256: str | None = None) -> str:
    """运行已保存的工作流(workflows/<workflow_id>.yaml)。同步阻塞到结束。

    node_overrides 是封闭的本次运行节点参数覆盖；不改 YAML，不接受权限或拓扑字段。
    可覆盖:model/fallback/thinking/max_output_tokens/temperature/seed/
    timeout_s/retry/prompt(llm)、max_turns/timeout_s/retry/prompt/workdir
    (coding_agent;research 无 workdir)、prompt(human)。
    prompt 是完整替换本次运行的节点职责文本,不是追加;
    consumes/outputs/图结构永远不可覆盖——要长期生效就让 AI 改 YAML。
    dry_run=True 与真跑使用同一有效规格，只渲染、不花钱；
    dry_run=False 真实调用模型。暂停在 human 节点时返回 paused。
    """
    try:
        return _render(run_workflow_impl(
            workflow_id, task, dry_run, node_overrides=node_overrides,
            expected_execution_sha256=expected_execution_sha256))
    except ValueError as e:
        return _render({"error": str(e), "next": "id 不合法"})


@server.tool()
def atlas_list_workflows() -> str:
    """列出 workflows/ 里的图定义与校验状态(零成本)。"""
    items = []
    for path in sorted(WORKFLOWS_DIR.glob("*.yaml")):
        try:
            spec = spec_from_yaml_file(path)
            items.append({"id": path.stem, "name": spec.name,
                          "valid": True, "description": spec.description})
        except SpecError as e:
            items.append({"id": path.stem, "name": path.stem, "valid": False,
                          "description": "", "error": str(e)})
    return _render({"workflows": items,
                    "next": "挑一个用 atlas_run_workflow 跑,或先 validate 新图"})


@server.tool()
def atlas_get_run(run_id: str) -> str:
    """查某次运行的动态状态、每节点模型与 token、账本路径(零成本)。"""
    try:
        return _render(summarize_run(run_id))
    except ValueError as e:
        return _render({"error": str(e), "next": "id 不合法"})


@server.tool()
def atlas_resume_run(run_id: str) -> str:
    """仅恢复动态判定为 interrupted 的运行；paused 必须在 Web 审批。"""
    try:
        return _render(resume_run_impl(run_id))
    except ValueError as e:
        return _render({"error": str(e), "next": "id 不合法"})


def main() -> None:
    import asyncio
    from atlas.config_init import initialize_runtime_config

    # stdio MCP 的 stdout 是协议通道；初始化器本身不得打印。
    initialize_runtime_config()
    asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
