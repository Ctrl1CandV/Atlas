# -*- coding: utf-8 -*-
"""M0 的写死两节点图:writer(Deepseek,openai 端点) → reviewer(SuperAI,anthropic 端点)。

两个节点来自不同厂商,且刻意走两种传输协议,一次运行把两条真实链路都验证到。
不做 YAML 解析(M1),不做必填字段(真实模型 JSON 合规率不稳,假成功检测
的机器证明由 A3 用假供应商完成;这里证明传输+完整性链路)。
"""
from atlas.adapters import build_real_registry
from atlas.engine import RunResult
from atlas.integrity import sha256_bytes
from atlas.spec import EdgeSpec, NodeSpec, WorkflowSpec
from pathlib import Path

TASK = (
    "评估:在 Windows 本地部署的多模型工作流引擎里,用 SQLite 给 LangGraph 做 "
    "checkpoint 存储,可行性与风险是什么?请覆盖:并发写、文件锁、崩溃恢复、"
    "与 runs/ 目录里事件流账本的关系、以及什么时候必须换 Postgres。"
)


def m0_spec() -> WorkflowSpec:
    return WorkflowSpec(
        name="m0-two-node",
        nodes=[
            NodeSpec(
                id="writer",
                type="llm",
                model="Deepseek:deepseek-v4-flash",
                # 跨厂商失败链(架构第 5 节:备用必须来自不同供应商)。
                # M0 第二次真实运行就撞上 deepseek 空返回,链路真的用得上。
                fallback=["SuperAI:glm-5.2-t"],
                prompt=(
                    "你是技术分析师。基于下方任务材料写一份精炼的分析报告:"
                    "分节、有论据、给出明确结论。篇幅控制在 1500 字以内——"
                    "宁可精炼,务必写完结论,不要用省略号或「后略」收尾。"
                    "直接输出报告正文,不要前置寒暄。"
                ),
                consumes=["task"],
            ),
            NodeSpec(
                id="reviewer",
                type="llm",
                model="SuperAI:glm-5.3",
                fallback=["Deepseek:deepseek-v4-pro"],
                prompt=(
                    "你是审查员。你将收到任务原文与上游分析报告的完整原文。请:"
                    "1) 报出你收到的报告约多少字符(先实际数一下,这是完整性检查);"
                    "2) 用五句话概括其论点;3) 指出论证最薄弱的一处并说明为什么;"
                    "4) 给出一个值得追问的后续问题。"
                ),
                consumes=["task", "writer.output"],
            ),
        ],
        edges=[
            EdgeSpec(source="writer", target="reviewer"),
            EdgeSpec(source="reviewer", target="END"),
        ],
        entry="writer",
    )


def m0_registry():
    """Deepseek 走 openai 端点;SuperAI 刻意走 anthropic 端点,验证另一种传输。

    SuperAI 输出上限提到 16384:glm-5.3 是推理型模型,隐性思考会烧输出预算,
    8192 时两次真实运行都在思考阶段打满、可见文本为空(被假成功检测拦下)。
    """
    return build_real_registry(
        ["Deepseek", "SuperAI"],
        prefer_transport={"SuperAI": "anthropic"},
        max_output_tokens={"SuperAI": 16384},
    )


def self_check(run: RunResult) -> dict:
    """对真实运行做 A1 语义的自检:源产物字节 ⊆ 投影字节,哈希对得上。"""
    input_event = run.events.find(type="node_input", node="reviewer")
    assert input_event is not None, "reviewer 没有 node_input 事件"
    projection = Path(input_event["projection_path"]).read_bytes()
    consumed = {c["name"]: c for c in input_event["consumed"]}
    assert "writer.output" in consumed, "reviewer 没有消费 writer.output"

    src = consumed["writer.output"]
    source = Path(src["path"]).read_bytes()
    assert sha256_bytes(source) == src["sha256"], "writer 产物哈希对不上"
    assert source in projection, "writer 产物没有完整出现在 reviewer 的投影里(A1 失败)"

    done = run.events.filter(type="node_done")
    return {
        "run_id": run.run_id,
        "run_dir": str(run.dir),
        "writer_chars": len(source.decode("utf-8")),
        "projection_chars": len(projection.decode("utf-8")),
        "nodes": [
            {
                "node": e["node"],
                "model_used": e["model_used"],
                "degraded": e["degraded"],
                "input_tokens": e["input_tokens"],
                "output_tokens": e["output_tokens"],
                "duration_s": e["duration_s"],
            }
            for e in done
        ],
        "models_distinct": len({e["model_used"] for e in done}) == 2,
    }
