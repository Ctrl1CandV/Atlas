# -*- coding: utf-8 -*-
"""E-1 · search 节点的检索后端层。

设计边界(PLAN-stage-e E-1):
- 封闭节点类型,Atlas 自持可插拔后端;刻意排除 provider tool-calling 形态
  (模型自主决定何时搜——不可审计、不可预算)。
- HTTP 用标准库 urllib,不新增运行时依赖。
- 搜索按调用计费而非 token,CostLedger 的费率模型盖不住:cost_usd 只取
  后端实报,拿不到就记 null,绝不冒充 $0。
- 域名过滤只看检索 API 返回的**初始 URL** 的 host(urlsplit 解析,
  `https://arxiv.org@evil.com/` 的 host 是 evil.com,不能冒充 arxiv.org);
  我们从不抓取页面本身,但跳转域名/短链可能掩盖最终落地页——这是诚实的
  已知限制,写进 skill/concepts,不暗示更强保证。
- 结果是时效性内容:search 节点天然不进 P7 复用/P13 合成导入候选
  (复用=造假);显式 imports 仍合法,但产物带 untrusted 标记,下游投影
  强制围栏(integrity.fence_untrusted)。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from atlas.spec import SEARCH_BACKENDS, SEARCH_MAX_QUERIES, SpecError

SEARCH_SNIPPET_MAX_CHARS = 2_000
BACKENDS = SEARCH_BACKENDS
DEFAULT_BACKEND = "tavily"
TAVILY_API_ENV = "TAVILY_API_KEY"
SEARXNG_BASE_URL_ENV = "ATLAS_SEARXNG_BASE_URL"
_HTTP_TIMEOUT_FLOOR_S = 1.0


class SearchBackendError(Exception):
    """检索后端网络/HTTP 失败。由引擎归类为内容类失败(可 on_error 策略化)。"""


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str
    snippet: str          # 截断至 SEARCH_SNIPPET_MAX_CHARS
    published: str | None = None


class SearchBackend(Protocol):
    def search(self, query: str, *, max_results: int,
               allowed_domains: list[str],
               timeout_s: float | None = None) -> list[SearchResult]: ...


def _http_json(*, method: str, url: str, body: dict | None = None,
               headers: dict[str, str] | None = None,
               timeout_s: float | None) -> dict:
    """一次 JSON HTTP 调用;任何网络/HTTP/解析失败都包成 SearchBackendError。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Accept": "application/json",
                                              **(headers or {})})
    timeout = max(_HTTP_TIMEOUT_FLOOR_S, timeout_s or 30.0)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except OSError:
            pass
        raise SearchBackendError(
            f"HTTP {e.code} 来自 {urllib.parse.urlsplit(url).hostname}:"
            f"{detail}") from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise SearchBackendError(
            f"{type(e).__name__} 访问 {urllib.parse.urlsplit(url).hostname} 失败:"
            f"{e}") from e
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise SearchBackendError(f"响应不是合法 JSON:{e}") from e
    if not isinstance(parsed, dict):
        raise SearchBackendError(
            f"响应顶层是 {type(parsed).__name__},需要对象")
    return parsed


def _clean_result_url(url: str) -> str | None:
    """URL scheme 白名单(http/https)+ 按 host 解析;非法/无 host 返回 None。

    userinfop 技巧(`https://arxiv.org@evil.com/`)的 hostname 是 evil.com——
    用 urlsplit().hostname 而不是字符串前缀匹配,这是审查合同点。
    """
    if not isinstance(url, str) or not url.strip():
        return None
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    return url.strip()


def _host_matches(url: str, allowed_domains: list[str]) -> bool:
    """初始 URL 的 host 是否命中白名单(精确或子域);白名单空=不过滤。"""
    if not allowed_domains:
        return True
    hostname = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    for domain in allowed_domains:
        d = domain.lower().rstrip(".")
        if hostname == d or hostname.endswith(f".{d}"):
            return True
    return False


def sanitize_results(results: list[SearchResult],
                     allowed_domains: list[str]) -> list[SearchResult]:
    """统一出口过滤:scheme 白名单 + host 解析命中;不追重定向(见模块 docstring)。

    SearXNG 没有服务端域名过滤,这份客户端过滤是它唯一的域名闸门
    (可能返回少于 max_results 条,文档如实写);Tavily 有服务端
    include_domains,这里再过一遍是纵深防御,语义一致。
    """
    out: list[SearchResult] = []
    for item in results:
        cleaned = _clean_result_url(item.url)
        if cleaned is None:
            continue
        if not _host_matches(cleaned, allowed_domains):
            continue
        out.append(item)
    return out


def resolve_search_queries(node, consumed: list) -> tuple[list[str], bool]:
    """查询词来源三级优先(PLAN E-1):
    1) YAML 显式 queries(校验期保证 ≤5);
    2) 上游 consumes 产物为 JSON 且顶层含非空 queries 字符串数组 → 取之
       (按 consumes 声明序,首个命中即用;超 5 条截断并 truncated_queries=True);
    3) 兜底:整个 prompt 文本作为单查询。
    返回 (queries, truncated)。"""
    if node.queries:
        return [q.strip() for q in node.queries], False
    from atlas.integrity import read_artifact
    for ref in consumed:
        if ref.name == "task":
            continue
        try:
            parsed = json.loads(read_artifact(ref).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(parsed, dict):
            continue
        raw = parsed.get("queries")
        if not isinstance(raw, list) or not raw:
            continue
        if not all(isinstance(q, str) and q.strip() for q in raw):
            continue
        cleaned = [q.strip() for q in raw][:SEARCH_MAX_QUERIES]
        if not cleaned:
            continue
        return cleaned, len(raw) > SEARCH_MAX_QUERIES
    return [node.prompt.strip()], False


class TavilyBackend:
    """tavily.com API;key=TAVILY_API_KEY,缺失在预检位拒绝(见 preflight)。"""

    endpoint = "https://api.tavily.com/search"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self.last_batch_cost_usd: float | None = None   # Tavily 不实报费用

    @classmethod
    def from_env(cls, environ=None) -> "TavilyBackend":
        import os
        environ = os.environ if environ is None else environ
        return cls(api_key=environ.get(TAVILY_API_ENV, ""))

    def search(self, query: str, *, max_results: int,
               allowed_domains: list[str],
               timeout_s: float | None = None) -> list[SearchResult]:
        body: dict = {"query": query, "max_results": max_results}
        if allowed_domains:
            body["include_domains"] = list(allowed_domains)
        payload = _http_json(
            method="POST", url=self.endpoint,
            body=body,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout_s=timeout_s)
        raw = payload.get("results")
        if not isinstance(raw, list):
            raise SearchBackendError("响应缺少 results 数组")
        out: list[SearchResult] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            out.append(SearchResult(
                url=str(item.get("url", "")),
                title=str(item.get("title", "")),
                snippet=str(item.get("content", ""))[:SEARCH_SNIPPET_MAX_CHARS],
                published=(str(item["published_date"])
                           if item.get("published_date") else None),
            ))
        return out


class SearxngBackend:
    """自建 SearXNG 实例的 JSON API;base-url=ATLAS_SEARXNG_BASE_URL。

    SearXNG 标准接口没有服务端域名过滤,allowed_domains 由
    sanitize_results 在结果侧过滤(可能少于 max_results,如实文档化)。
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        self.last_batch_cost_usd: float | None = None   # 自建实例无计费

    @classmethod
    def from_env(cls, environ=None) -> "SearxngBackend":
        import os
        environ = os.environ if environ is None else environ
        return cls(base_url=environ.get(SEARXNG_BASE_URL_ENV, ""))

    def search(self, query: str, *, max_results: int,
               allowed_domains: list[str],
               timeout_s: float | None = None) -> list[SearchResult]:
        params = urllib.parse.urlencode(
            {"q": query, "format": "json", "language": "zh-CN"})
        payload = _http_json(
            method="GET", url=f"{self._base_url}/search?{params}",
            timeout_s=timeout_s)
        raw = payload.get("results")
        if not isinstance(raw, list):
            raise SearchBackendError("响应缺少 results 数组")
        out: list[SearchResult] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            result = SearchResult(
                url=str(item.get("url", "")),
                title=str(item.get("title", "")),
                snippet=str(item.get("content", ""))[:SEARCH_SNIPPET_MAX_CHARS],
                published=(str(item["publishedDate"])
                           if item.get("publishedDate") else None),
            )
            # 客户端域名过滤:SearXNG 无服务端 include_domains。
            if allowed_domains and not _host_matches(
                    _clean_result_url(result.url) or "", allowed_domains):
                continue
            out.append(result)
        return out[:max_results] if allowed_domains else out


def default_backend_factory(backend_id: str, *, environ=None):
    """生产后端工厂:封闭枚举 → 从环境构造。未知 id 是规格错误。"""
    if backend_id == "tavily":
        return TavilyBackend.from_env(environ)
    if backend_id == "searxng":
        return SearxngBackend.from_env(environ)
    raise SpecError(
        f"未知检索后端 {backend_id!r};封闭枚举:{list(SEARCH_BACKENDS)}")


def preflight_search_backends(spec, *, environ=None) -> None:
    """校验期预检位(同 _resolve_models 的位置):key/base-url 缺失在
    花钱之前拒绝。dry-run 与真跑都经过 prepare_execution → 本检查,
    所以缺凭据时 dry-run 也会响亮失败,而不是等真跑才炸。"""
    import os
    environ = os.environ if environ is None else environ
    for node in spec.nodes:
        if node.type != "search":
            continue
        backend_id = node.backend or DEFAULT_BACKEND
        if backend_id == "tavily" and not environ.get(TAVILY_API_ENV):
            raise SpecError(
                f"search 节点 {node.id} 的 backend tavily 需要环境变量 "
                f"{TAVILY_API_ENV}(config/.env)。不猜、不降级——先补齐配置再跑")
        if backend_id == "searxng" and not environ.get(SEARXNG_BASE_URL_ENV):
            raise SpecError(
                f"search 节点 {node.id} 的 backend searxng 需要环境变量 "
                f"{SEARXNG_BASE_URL_ENV}(实例 JSON API 的 base URL)。"
                "不猜、不降级——先补齐配置再跑")
