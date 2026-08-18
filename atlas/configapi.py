# -*- coding: utf-8 -*-
"""配置面 API:供应商、密钥、模型白名单(PLAN-v2 M3)。

界线(PLAN 3.1):界面可以改"环境"(供应商/模型/密钥),不能改"图"。

安全:
- 全部走 web.py 的中间件:Host 白名单;写操作需 X-Atlas-Request 头。
  拉取模型做成 POST 正是为此——它让本机向用户指定的 URL 发请求(SSRF
  探针),GET 会被跨站 no-cors 触发。
- **读方向的响应里没有密钥值**——credential 字段是 CredentialView
  (configured/source/writable),类型上没有放值的位置(A8)。
- 新建供应商的 id 必须字母开头:它是 apiKeyEnv 的词干,环境变量名
  必须大写字母开头(dsh 教训:数字开头的 id 能过所有界面检查,
  然后在凭据层以用户看不懂的正则失败)。
- 删除供应商时凭据窄清理:只删"恰好等于本 id 派生名且确实写在 .env 里"
  的那一行;证明不了所有权就保留(防误删共享密钥)。
"""
import re
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from atlas.config import (
    CONFIG_DIR,
    ProviderConfig,
    _CONFIG_WRITE_LOCK,
    load_provider_configs,
    save_provider_configs,
)
from atlas.credentials import CredentialError, EnvStore
from atlas.discovery import discover_models
from atlas.config_init import (acknowledge_initialization_notice,
                               read_initialization_notice)

_PROVIDER_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _derive_env_name(provider_id: str) -> str:
    return provider_id.upper().replace("-", "_") + "_API_KEY"


def _provider_row(cfg: ProviderConfig, store: EnvStore) -> dict:
    v = store.view(cfg.api_key_env)
    return {
        "id": cfg.id,
        "openaiBaseUrl": cfg.openai_base_url,
        "anthropicBaseUrl": cfg.anthropic_base_url,
        "preferTransport": cfg.prefer_transport,
        "maxOutputTokens": cfg.max_output_tokens,
        "models": list(cfg.models),
        "apiKeyEnv": cfg.api_key_env,
        # 三态凭据视图;没有 value 字段(A8)
        "credential": {"configured": v.configured, "source": v.source,
                       "writable": v.writable},
    }


def register_config_routes(app, providers_path: Path | None = None,
                           env_store: EnvStore | None = None) -> None:
    path = Path(providers_path) if providers_path else (CONFIG_DIR / "providers.json")
    store = env_store or EnvStore()

    def _load() -> dict[str, ProviderConfig]:
        try:
            return load_provider_configs(path)
        except Exception as e:
            raise HTTPException(500, f"providers.json 不合法:{e}")

    def _get(pid: str) -> ProviderConfig:
        cfg = _load().get(pid)
        if cfg is None:
            raise HTTPException(404, f"没有这个供应商:{pid}")
        return cfg

    @app.get("/api/providers")
    def list_providers():
        cfgs = _load()
        return [_provider_row(c, store) for c in cfgs.values()]

    @app.get("/api/providers/{pid}")
    def get_provider(pid: str):
        return _provider_row(_get(pid), store)

    @app.post("/api/providers")
    def create_provider(body: dict):
        pid = str(body.get("id", "")).strip()
        if not _PROVIDER_ID_RE.match(pid):
            raise HTTPException(400, (
                f"供应商 id {pid!r} 不合法:字母开头,之后字母数字_-"
                f"(它是密钥变量名的词干,必须能映射成合法环境变量名)"))
        env_name = _derive_env_name(pid)
        if not _ENV_NAME_RE.match(env_name):
            raise HTTPException(400, f"内部错误:派生的变量名 {env_name!r} 不合法")
        openai_url = str(body.get("openaiBaseUrl", "") or "").strip()
        anthropic_url = str(body.get("anthropicBaseUrl", "") or "").strip()
        if not openai_url and not anthropic_url:
            raise HTTPException(400, "至少需要一个 base URL(openai 或 anthropic 兼容端点)")
        for label, url in (("openaiBaseUrl", openai_url), ("anthropicBaseUrl", anthropic_url)):
            if url and not url.startswith(("http://", "https://")):
                raise HTTPException(400, f"{label} 必须以 http:// 或 https:// 开头")
        prefer = body.get("preferTransport") or None
        if prefer is not None and prefer not in ("openai", "anthropic"):
            raise HTTPException(400, "preferTransport 只能是 'openai'/'anthropic'")
        cfgs = _load()
        if pid in cfgs:
            raise HTTPException(400, f"供应商 {pid} 已存在;id 不可改,请删除后新建")
        # 🟠6:派生名可能与其他供应商撞车(_derive 非单射,如 A-B 与 A_B)
        occupant = next((other.id for other in cfgs.values()
                         if other.api_key_env == env_name), None)
        if occupant:
            raise HTTPException(400, (
                f"密钥变量名 {env_name} 已被供应商 {occupant} 使用"
                f"(id {pid!r} 与它派生出同一个变量名)。换一个 id"))
        cap = body.get("maxOutputTokens")
        if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or cap < 1):
            raise HTTPException(400, "maxOutputTokens 必须是正整数")
        api_key = body.get("apiKey")
        if api_key is not None and not isinstance(api_key, str):
            raise HTTPException(400, "apiKey 必须是字符串")
        if api_key is not None and not api_key.strip():
            # 传了纯空格 != 没传:静默跳过会让调用方以为设了(与 key 端点同规)
            raise HTTPException(400, "apiKey 不传表示不设置;纯空格不是合法密钥")
        # 🟠3:先写密钥再落盘供应商——密钥写失败时不留半成品配置
        warnings: list[str] = []
        if api_key:
            try:
                warnings = store.upsert(env_name, api_key)
            except CredentialError as e:
                raise HTTPException(400, str(e))
        with _CONFIG_WRITE_LOCK:
            cfgs = _load()   # 锁内重读,防拿旧快照覆盖并发改动
            if pid in cfgs:
                raise HTTPException(400, f"供应商 {pid} 已存在(并发创建)")
            cfgs[pid] = ProviderConfig(
                id=pid, openai_base_url=openai_url or None,
                anthropic_base_url=anthropic_url or None,
                api_key_env=env_name, models=tuple(), prefer_transport=prefer,
                max_output_tokens=cap,
            )
            save_provider_configs(cfgs, path, lock=False)
        return {"provider": _provider_row(cfgs[pid], store), "warnings": warnings}

    @app.put("/api/providers/{pid}")
    def update_provider(pid: str, body: dict):
        cfg = _get(pid)
        cfgs = _load()
        openai_url = str(body.get("openaiBaseUrl", cfg.openai_base_url or "") or "").strip()
        anthropic_url = str(body.get("anthropicBaseUrl", cfg.anthropic_base_url or "") or "").strip()
        if not openai_url and not anthropic_url:
            raise HTTPException(400, "至少需要一个 base URL")
        for label, url in (("openaiBaseUrl", openai_url), ("anthropicBaseUrl", anthropic_url)):
            if url and not url.startswith(("http://", "https://")):
                raise HTTPException(400, f"{label} 必须以 http:// 或 https:// 开头")
        prefer = body.get("preferTransport", cfg.prefer_transport)
        if prefer == "":
            prefer = None   # 空串视同清除(前端表单清空后发的是 "")
        if prefer is not None and prefer not in ("openai", "anthropic"):
            raise HTTPException(400, "preferTransport 只能是 'openai'/'anthropic'")
        cap = body.get("maxOutputTokens", cfg.max_output_tokens)
        if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or cap < 1):
            raise HTTPException(400, "maxOutputTokens 必须是正整数")
        # id 与 apiKeyEnv 不可改(dsh:id 是跨配置的引用键,改了搬不动历史)
        with _CONFIG_WRITE_LOCK:
            cfgs = _load()
            current = cfgs.get(pid) or cfg
            cfgs[pid] = ProviderConfig(
                id=pid, openai_base_url=openai_url or None,
                anthropic_base_url=anthropic_url or None,
                api_key_env=current.api_key_env, models=current.models,
                prefer_transport=prefer, max_output_tokens=cap,
                extra=current.extra,
            )
            save_provider_configs(cfgs, path, lock=False)
            return _provider_row(cfgs[pid], store)

    @app.delete("/api/providers/{pid}")
    def delete_provider(pid: str):
        cfg = _get(pid)
        with _CONFIG_WRITE_LOCK:
            cfgs = _load()
            if len(cfgs) <= 1:
                # 删光会让 providers.json 变成空列表,load_provider_configs
                # 拒绝它,此后全部配置端点 500、只能手改文件恢复(🟠2)
                raise HTTPException(400, (
                    "至少保留一个供应商;要清空配置请直接编辑 providers.json"))
            del cfgs[pid]
            save_provider_configs(cfgs, path, lock=False)
        note = ""
        expected_env = _derive_env_name(pid)
        still_used = any(other.api_key_env == cfg.api_key_env
                         for other in cfgs.values())
        if still_used:
            note = (f"密钥 {cfg.api_key_env} 还被其他供应商引用,已保留"
                    if cfg.api_key_env == expected_env else
                    f"密钥 {cfg.api_key_env} 不是本页派生的名字,已保留")
        elif cfg.api_key_env == expected_env:
            v = store.view(expected_env)
            if v.configured and v.source == "file":
                store.remove(expected_env)
                note = f"已连带删除 .env 里的 {expected_env}"
            elif v.configured:
                note = (f"{expected_env} 来自进程环境而非 .env,"
                        f"无可删除的文件行")
            else:
                note = f"{expected_env} 未配置,无需清理"
        else:
            note = f"密钥 {cfg.api_key_env} 不是本页派生的名字,已保留"
        return {"deleted": pid, "note": note}

    @app.post("/api/providers/{pid}/key")
    def set_key(pid: str, body: dict):
        cfg = _get(pid)
        value = str(body.get("value", ""))
        if not value.strip():
            raise HTTPException(400, "留空表示保留原值;要清除请用删除供应商。纯空格不是密钥")
        try:
            warnings = store.upsert(cfg.api_key_env, value)
        except CredentialError as e:
            raise HTTPException(400, str(e))
        v = store.view(cfg.api_key_env)
        return {"apiKeyEnv": cfg.api_key_env, "credential": {
            "configured": v.configured, "source": v.source, "writable": v.writable},
            "warnings": warnings}

    @app.post("/api/providers/{pid}/discover")
    def discover(pid: str, body: dict):
        """拉取候选模型清单(不等于要全配;白名单是勾选结果)。

        支持用表单当前值探测:未保存的 base URL 与密钥都能带上——
        不然"先保存才能测试"会把半成品配置写进文件。
        """
        cfg = _get(pid)
        openai_url = str(body.get("openaiBaseUrl", cfg.openai_base_url or "") or "").strip()
        anthropic_url = str(body.get("anthropicBaseUrl", cfg.anthropic_base_url or "") or "").strip()
        api_key = str(body.get("apiKey", "") or "").strip() \
            or (store.read_value(cfg.api_key_env) or "")
        if not openai_url and not anthropic_url:
            raise HTTPException(400, "没有可用的 base URL")
        if not api_key:
            raise HTTPException(400, f"没有密钥:先填写 {cfg.api_key_env} 再拉取")
        result = discover_models(
            {"openai": openai_url or None, "anthropic": anthropic_url or None},
            api_key)
        if result.ok:
            # 已配置的模型即使 API 不再返回也保留提示(白名单是选择,不是镜像)
            return {"ok": True, "models": list(result.models), "message": ""}
        return JSONResponse(status_code=200, content={
            "ok": False, "models": [], "errorKind": result.error_kind,
            "message": result.message})

    @app.put("/api/providers/{pid}/models")
    def set_models(pid: str, body: dict):
        """写回白名单(勾选结果或手填)。顺序保留:界面怎么排,配置就怎么存。"""
        cfg = _get(pid)
        models = body.get("models")
        if not isinstance(models, list) or not all(
                isinstance(m, str) and m.strip() for m in models):
            raise HTTPException(400, "models 必须是非空字符串数组(可以为空数组=清空白名单)")
        if len(set(models)) != len(models):
            raise HTTPException(400, "models 里有重复项")
        with _CONFIG_WRITE_LOCK:
            cfgs = _load()
            current = cfgs.get(pid) or cfg
            cfgs[pid] = ProviderConfig(
                id=current.id, openai_base_url=current.openai_base_url,
                anthropic_base_url=current.anthropic_base_url,
                api_key_env=current.api_key_env, models=tuple(models),
                prefer_transport=current.prefer_transport,
                max_output_tokens=current.max_output_tokens,
                extra=current.extra,
            )
            save_provider_configs(cfgs, path, lock=False)
            return _provider_row(cfgs[pid], store)

    config_dir = path.parent

    @app.get("/api/config/initialization")
    def initialization_notice():
        try:
            return read_initialization_notice(config_dir)
        except ValueError as e:
            raise HTTPException(500, str(e)) from e

    @app.post("/api/config/initialization/ack")
    def acknowledge_initialization(body: dict):
        event_id = body.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise HTTPException(400, "event_id 必须是非空字符串")
        try:
            acknowledged = acknowledge_initialization_notice(event_id, config_dir)
        except ValueError as e:
            raise HTTPException(500, str(e)) from e
        if not acknowledged:
            raise HTTPException(409, "初始化提示已更新或不存在,未确认")
        return {"acknowledged": event_id}
