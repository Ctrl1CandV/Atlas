# -*- coding: utf-8 -*-
"""M4 前置实验:逐模型探测思考深度能力(PLAN-v2 4.1.1)。

判据不是"参数被接受"(Kiro 实测:接受且 200 但无思考内容),
而是"响应里真的出现思考痕迹":
- OpenAI 协议:usage.completion_tokens_details.reasoning_tokens > 0
- Anthropic 协议:content 里有 thinking 块

结论写入 config/capabilities.json(kind: effort | budget | none)。
每模型一次最小调用,总成本估算 <$0.1。
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, ".")
import httpx
from dotenv import load_dotenv

load_dotenv(Path("config/.env"))
providers = json.loads(Path("config/providers.json").read_text(encoding="utf-8"))["providers"]

Q = "9.11 和 9.9 哪个大?一句话给出理由。"   # 需要一点推理,足以触发思考


def probe_openai(base, key, model):
    """OpenAI 协议:reasoning_effort。"""
    body = {"model": model, "messages": [{"role": "user", "content": Q}],
            "max_tokens": 1500, "reasoning_effort": "high"}
    r = httpx.post(base.rstrip("/") + "/chat/completions",
                   headers={"Authorization": f"Bearer {key}"},
                   json=body, timeout=120.0)
    if r.status_code == 400 and "reasoning_effort" in r.text:
        return {"kind": "none", "note": "网关拒绝 reasoning_effort 参数"}
    if r.status_code != 200:
        return {"kind": "error", "note": f"HTTP {r.status_code}: {r.text[:120]}"}
    d = r.json()
    det = (d.get("usage") or {}).get("completion_tokens_details") or {}
    reasoning = det.get("reasoning_tokens") or 0
    if reasoning > 0:
        return {"kind": "effort", "reasoning_tokens": reasoning}
    return {"kind": "none", "note": f"200 但 reasoning_tokens={reasoning}"}


def probe_anthropic(base, key, model):
    """Anthropic 协议:thinking.budget_tokens。"""
    body = {"model": model, "max_tokens": 3000,
            "messages": [{"role": "user", "content": Q}],
            "thinking": {"type": "enabled", "budget_tokens": 1024}}
    r = httpx.post(base.rstrip("/") + "/v1/messages",
                   headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                   json=body, timeout=120.0)
    if r.status_code == 400 and "thinking" in r.text.lower():
        return {"kind": "none", "note": "网关拒绝 thinking 参数"}
    if r.status_code != 200:
        return {"kind": "error", "note": f"HTTP {r.status_code}: {r.text[:120]}"}
    d = r.json()
    blocks = [b.get("type") for b in d.get("content", [])]
    if "thinking" in blocks:
        return {"kind": "budget", "blocks": blocks}
    return {"kind": "none", "note": f"200 但 blocks={blocks}(参数被静默吞掉)"}


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    results = {}
    for p in providers:
        pid = p["id"]
        key = os.environ.get(p["apiKeyEnv"], "")
        if not key:
            print(f"[{pid}] 密钥未配置,跳过")
            continue
        for model in p["models"]:
            ref = f"{pid}:{model}"
            # 按该供应商的主协议探测(openai 优先;探测发现 effort 不行时再试 anthropic)
            if p.get("openaiBaseUrl"):
                r = probe_openai(p["openaiBaseUrl"], key, model)
                if r["kind"] == "none" and p.get("anthropicBaseUrl"):
                    r2 = probe_anthropic(p["anthropicBaseUrl"], key, model)
                    r = r2 if r2["kind"] != "error" else r
            else:
                r = probe_anthropic(p["anthropicBaseUrl"], key, model)
            results[ref] = r
            print(f"{ref:36s} → {r['kind']:8s} {r.get('note', '')}")
    Path("config/capabilities.json").write_text(
        json.dumps({"_readme": "kind: effort=OpenAI reasoning_effort 档位 | "
                               "budget=Anthropic thinking.budget_tokens 数值 | "
                               "none=无思考控制。probed: 2026-08-16",
                    "capabilities": results},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    print("\n写入 config/capabilities.json")


if __name__ == "__main__":
    main()
