# -*- coding: utf-8 -*-
"""E-4 冒烟:e2e 专用 atlas-web 装配器——FakeProvider 预种 runs_root 再本地服务。

零真实供应商调用(合同审查重点 2):registry_factory 只会构造 FakeProvider,
两个种子 run 都是 engine 真实执行产生的(不是手写账本):
- e2e_gate: proposer → gate(human, routed) → rev,跑到 human 门暂停;
  测试批准后由同一个 FakeProvider 注册表续跑到终态;
- e2e_done: writer → reviewer 跑完后,把 ts/duration_s 归一化为固定值,
  让终局卡片渲染跨次稳定——这是截图基线可比的前提(flake 制度:
  两连跑不一致必须根因修复,所以种子必须先做到确定性)。

种子与账本落在 e2e/.seed/(gitignored),manifest.json 记录两个 run id 供
Playwright 用例深链直达。启动后只服务 127.0.0.1(红线 ④)。
"""
import json
import os
import shutil
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent.parent
SEED_DIR = E2E_DIR / ".seed"
RUNS_DIR = SEED_DIR / "runs"
WORKFLOWS_DIR = SEED_DIR / "workflows"
MANIFEST = SEED_DIR / "manifest.json"

# 终局卡片渲染的固定原料:ts 决定「收官」时刻,duration_s 决定时长列与
# 时长条宽度。只改这两个字段;产物字节/哈希/token 数保持 engine 实况。
FIXED_TS = "2026-08-28T10:15:30"
FIXED_DURATION = {"writer": 12.0, "reviewer": 3.4}

GATE_YAML = """\
name: e2e_gate
description: E-4 冒烟:人工审批门(routed)
entry: proposer
guards:
  max_iterations: 4
nodes:
  - id: proposer
    type: llm
    model: Fake:primary
    prompt: 给出方案。
    consumes: [task]
  - id: gate
    type: human
    prompt: 请审阅方案后批复。
    consumes: [proposer.output]
    approval_mode: routed
  - id: rev
    type: llm
    model: Fake:other
    prompt: 按批复意见修订。
    consumes: [task, proposer.output, gate.changes]
edges:
  - from: proposer
    to: gate
  - from: gate
    to: END
  - from: gate
    to: rev
    when: __changes__
  - from: rev
    to: proposer
"""

DONE_YAML = """\
name: e2e_done
description: E-4 冒烟:一次跑完的普通运行
entry: writer
guards:
  max_iterations: 4
nodes:
  - id: writer
    type: llm
    model: Fake:primary
    prompt: 写总结。
    consumes: [task]
  - id: reviewer
    type: llm
    model: Fake:other
    prompt: 复核总结。
    consumes: [task, writer.output]
edges:
  - from: writer
    to: reviewer
  - from: reviewer
    to: END
"""

TASK_TEXT = "E-4 冒烟任务:验证纯键盘审批流与终局卡片渲染,全程零真实调用。"


def _fake_registry(_provider_ids=None):
    """唯一进入 registry_factory 的实现(审查重点 2 的 grep 锚点):
    e2e 服务器从头到尾不存在真实供应商注册路径。"""
    from atlas.adapters import AdapterRegistry, FakeProvider

    fake = FakeProvider()
    fake.configure("primary", text="方案要点:先取数,再核算,最后给结论。")
    fake.configure("other", text="修订按批复意见逐条落实。")
    registry = AdapterRegistry()
    fake.register_into(registry)
    return registry


def _seed() -> None:
    from atlas.engine import execute_graph
    from atlas.spec import spec_from_yaml

    shutil.rmtree(SEED_DIR, ignore_errors=True)
    WORKFLOWS_DIR.mkdir(parents=True)
    (WORKFLOWS_DIR / "e2e_gate.yaml").write_text(GATE_YAML, encoding="utf-8")
    (WORKFLOWS_DIR / "e2e_done.yaml").write_text(DONE_YAML, encoding="utf-8")

    gate = execute_graph(spec_from_yaml(GATE_YAML), task=TASK_TEXT,
                         runs_root=RUNS_DIR, registry=_fake_registry())
    if gate.status != "paused":
        raise RuntimeError(f"gate 种子运行没有停在 human 门:{gate.status}")

    done = execute_graph(spec_from_yaml(DONE_YAML), task=TASK_TEXT,
                         runs_root=RUNS_DIR, registry=_fake_registry())
    if done.status != "done":
        raise RuntimeError(f"done 种子运行没有跑到终态:{done.status}")
    _normalize_done_ledger(done.dir)

    MANIFEST.write_text(json.dumps({
        "gate_run_id": gate.run_id,
        "done_run_id": done.run_id,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[e2e-seed] gate={gate.run_id} done={done.run_id}")


def _normalize_done_ledger(run_dir: Path) -> None:
    path = run_dir / "events.jsonl"
    records = [json.loads(line) for line in
               path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for record in records:
        if "ts" in record:
            record["ts"] = FIXED_TS
        if record.get("type") == "node_done":
            record["duration_s"] = FIXED_DURATION.get(record.get("node"))
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                            for r in records), encoding="utf-8")


def _serve() -> None:
    from atlas.web import create_app

    import uvicorn

    # 与 serve() 同一启动前置(config 模板就绪);registry 面已是 Fake。
    from atlas.config_init import initialize_runtime_config

    initialize_runtime_config()
    app = create_app(workflows_dir=WORKFLOWS_DIR, runs_dir=RUNS_DIR,
                     registry_factory=_fake_registry, mount_mcp=False)
    port = int(os.environ.get("ATLAS_E2E_PORT", "8977"))
    uvicorn.run(app, host="127.0.0.1", port=port,
                log_level="warning", access_log=False)


if __name__ == "__main__":
    _seed()
    _serve()
