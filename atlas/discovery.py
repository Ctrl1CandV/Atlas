# -*- coding: utf-8 -*-
"""模型列表拉取:把供应商 API 里可见的模型摆成候选清单(不等于要全配)。

实测依据(PLAN-v2 1.1,2026-08-16):
- 五家全部可拉;Deepseek 只有 openai 端点能拉,Minimax/RightCode 只有
  anthropic 端点——所以按"供应商各自能用的协议"逐个试,不假设两个都有。
- 两家协议的响应 envelope 相同({"data": [{"id": ...}]}),解析可以共用;
  但解析失败与"不支持"必须分开报,猜错格式和"该供应商没有模型"在界面上
  无法区分(dsh 的教训)。

纪律:
- 响应体上限 4MB,**按实际读取字节计**,不信 content-length(URL 是用户填的);
- 单行畸形跳过,不整体失败;
- 401/403 明确提示检查密钥;连接类错误归 transport;
- 拉取只发生在用户点按钮时,不后台刷新(dsh 否决过的做法)。
"""
from dataclasses import dataclass

import re
import time

import httpx

MAX_BODY_BYTES = 4 * 1024 * 1024
TIMEOUT_S = 20.0
TOTAL_DEADLINE_S = 30.0   # 两个候选端点共享一个总墙钟,慢滴流拖不住线程池


@dataclass(frozen=True)
class DiscoveryResult:
    ok: bool
    models: tuple[str, ...] = ()
    error_kind: str = ""     # unsupported | auth | transport | parse | none
    message: str = ""


def _list_url(base_url: str, protocol: str) -> str:
    base = base_url.rstrip("/")
    # 前缀拼接而非 new URL:保住 /openai/v1 这类部署路径里的段(dsh 教训)
    if protocol == "openai":
        return base + "/models"
    return base + "/v1/models"


def _headers(protocol: str, api_key: str) -> dict[str, str]:
    if protocol == "openai":
        return {"Authorization": f"Bearer {api_key}"}
    return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}


def _read_capped(resp) -> bytes:
    """按实际读取字节计的上限,防 content-length 撒谎。"""
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_bytes():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise ValueError("响应超过 4MB 上限")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_model_ids(body: bytes) -> list[str]:
    import json
    data = json.loads(body.decode("utf-8", errors="replace"))
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("响应里没有 data 数组")
    ids: list[str] = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]:
            ids.append(row["id"])
        # 畸形行跳过(dsh 教训:一行坏数据不该剥夺整个清单)
    return ids


def _try_endpoint(url: str, headers: dict[str, str]) -> tuple[str, DiscoveryResult]:
    try:
        # follow_redirects=False:httpx 跨源重定向只剥 Authorization,不剥
        # 自定义头——x-api-key 会被原样转发给重定向目标,等于把密钥送给
        # 任何能让本 URL 返回 302 的人。不跟随,3xx 归 transport(M3 审查🟠1)
        with httpx.Client(timeout=TIMEOUT_S, follow_redirects=False) as client:
            with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code in (401, 403):
                    return "auth", DiscoveryResult(
                        False, error_kind="auth",
                        message=f"HTTP {resp.status_code}:密钥无效或没有权限,先检查 API key")
                if resp.status_code == 404:
                    return "unsupported", DiscoveryResult(
                        False, error_kind="unsupported",
                        message=f"HTTP 404:该端点没有模型列表,请手动填写模型")
                if resp.status_code != 200:
                    return "transport", DiscoveryResult(
                        False, error_kind="transport",
                        message=f"HTTP {resp.status_code}")
                body = _read_capped(resp)
    except ValueError as e:
        return "parse", DiscoveryResult(False, error_kind="parse", message=str(e))
    except httpx.HTTPError as e:
        # 剥 URL userinfo(用户可能把 user:pass 写进 URL,那不是存储密钥也不该回显)
        msg = re.sub(r"(://)[^/@:]+:[^/@]+@", r"\1***@", str(e))[:160]
        return "transport", DiscoveryResult(
            False, error_kind="transport",
            message=f"连接失败:{type(e).__name__}: {msg}")
    try:
        ids = _parse_model_ids(body)
    except ValueError as e:   # 含 JSONDecodeError 与缺 data 数组
        return "parse", DiscoveryResult(
            False, error_kind="parse",
            message=f"响应格式不是预期的模型清单:{str(e)[:160]}。请手动填写模型")
    if not ids:
        return "parse", DiscoveryResult(
            False, error_kind="parse",
            message="端点返回 200 但清单为空;可能协议不匹配,请手动填写模型")
    return "ok", DiscoveryResult(True, models=tuple(sorted(ids)))


def discover_models(base_urls: dict[str, str | None], api_key: str) -> DiscoveryResult:
    """按供应商各自可用的协议拉取。base_urls: {"openai": url, "anthropic": url}。

    两个端点都有就都试:第一个成功即返回;一边 404 另一边 200 是实测过的
    真实情况(Deepseek/Minimax)。两边都失败时,返回对诊断最有用的那个错误
    (auth > parse > transport > unsupported)。
    """
    candidates: list[tuple[str, DiscoveryResult]] = []
    deadline = time.monotonic() + TOTAL_DEADLINE_S
    for protocol in ("openai", "anthropic"):
        base = base_urls.get(protocol)
        if not base:
            continue
        if time.monotonic() >= deadline:
            break   # 第一个端点已经把总预算耗尽,别让第二个继续拖
        kind, result = _try_endpoint(_list_url(base, protocol),
                                     _headers(protocol, api_key))
        if result.ok:
            return result
        candidates.append((kind, result))
    if not candidates:
        return DiscoveryResult(
            False, error_kind="unsupported",
            message="该供应商没有配置任何 base URL,无法拉取;请手动填写模型")
    rank = {"auth": 0, "parse": 1, "transport": 2, "unsupported": 3}
    candidates.sort(key=lambda kv: rank.get(kv[0], 9))
    return candidates[0][1]
