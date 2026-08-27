"""Integration registry for reusable provider, protocol, and shell entries."""

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from urllib.parse import urlsplit
from pathlib import Path

from . import auth, ops
from .db import now, tx
from .messages import idempotent


PROTOCOLS = {
    "anthropic": {
        "kind": "model_api",
        "auth_styles": ["bearer", "x-api-key"],
        "credential_env": {"bearer": "ANTHROPIC_AUTH_TOKEN"},
        "base_env": "ANTHROPIC_BASE_URL",
        "model_discovery_paths": ["/v1/models"],
    },
    "openai_chat": {
        "kind": "model_api",
        "auth_styles": ["bearer", "x-api-key"],
        "credential_env": {"bearer": "OPENAI_API_KEY"},
        "base_env": "OPENAI_BASE_URL",
        "model_discovery_paths": ["/models", "/v1/models", "/api/v1/models"],
    },
    "openai_responses": {
        "kind": "model_api",
        "auth_styles": ["bearer", "x-api-key"],
        "credential_env": {"bearer": "OPENAI_API_KEY"},
        "base_env": "OPENAI_BASE_URL",
        "model_discovery_paths": ["/models", "/v1/models", "/api/v1/models"],
    },
    "stream-json": {"kind": "assistant_session", "backend": "stream-json"},
    "acp": {"kind": "assistant_session", "backend": "acp"},
}


BUILTIN_PROVIDERS = {
    "kimi": {
        "display": "Kimi For Coding",
        "base_url": "https://api.kimi.com/coding/",
        "protocol": "anthropic",
        "auth_style": "bearer",
        "category": "中国平台",
    },
    "stepfun": {
        "display": "StepFun",
        "base_url": "https://api.stepfun.com/step_plan",
        "protocol": "anthropic",
        "auth_style": "bearer",
        "category": "中国平台",
    },
    "openrouter": {
        "display": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "protocol": "openai_chat",
        "auth_style": "bearer",
        "category": "聚合平台",
    },
    "deepseek": {
        "display": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "protocol": "openai_chat",
        "auth_style": "bearer",
        "category": "中国平台",
    },
}


def _config(conn: sqlite3.Connection, key: str):
    row = conn.execute(
        "SELECT value FROM configs WHERE key=?", (key,)
    ).fetchone()
    return json.loads(row["value"]) if row else None


def _put(conn, ident, key, value, request_id):
    ops.config_set(
        conn, ident, key, json.dumps(value, ensure_ascii=False),
        request_id=request_id)


def _provider_entry(name, base_url, protocol, auth_style="bearer",
                    builtin=False, display=""):
    if not name or "/" in name:
        raise ValueError("供应商名称须为非空且不含斜杠")
    if not base_url or not protocol:
        raise ValueError("供应商至少需要 base_url 和 API 协议")
    if protocol not in PROTOCOLS or PROTOCOLS[protocol].get("kind") != "model_api":
        raise ValueError(f"模型 API 协议不合法: {protocol}")
    return {
        "display": display or name,
        "base_url": base_url,
        "protocol": protocol,
        "auth_style": auth_style,
        "category": "内置" if builtin else "自定义",
        "builtin": builtin,
        "models": [],
        "discovered_at": 0,
        "model_discovery_paths": PROTOCOLS[protocol]["model_discovery_paths"],
        "key_ref": None,
        "credential_key": None,
    }


def register_custom_provider(conn, ident, name, base_url, protocol,
                             key_ref="", auth_style="bearer",
                             request_id=None):
    """显式登记一个可复用自定义供应商;明文 key 只允许通过 key_ref 引用。"""
    if not auth.check_controller(conn, ident):
        raise PermissionError("集成注册表变更仅总控身份可执行")
    if not request_id:
        raise ValueError("集成条目写入必须带 request_id")
    entry = _provider_entry(name, base_url, protocol, auth_style)
    entry["key_ref"] = key_ref or None
    key = f"integration_provider:{name}"
    _put(conn, ident, key, entry, request_id)
    ops.audit(conn, "integration_register",
              {"kind": "provider", "name": name, "by": ident["worker_id"]})
    return {"name": name, "key": key}


def register_credential(conn, ident, name, provider, key_ref="",
                        request_id=None):
    """credential 条目=凭据引用(名称→供应商+key 文件引用);明文不入账本。"""
    if not auth.check_controller(conn, ident):
        raise PermissionError("集成注册表变更仅总控身份可执行")
    if not request_id:
        raise ValueError("集成条目写入必须带 request_id")
    if not name or "/" in name:
        raise ValueError("凭据名称须为非空且不含斜杠")
    if not key_ref:
        raise ValueError("credential 只存 key 文件引用,须给 key_ref")
    pentry = _config(conn, f"integration_provider:{provider}")
    if pentry is None:
        raise ValueError(f"credential {name} 指向的供应商 {provider} 未登记")
    with tx(conn) as c:
        def run():
            _write_entry(c, ident, f"credential:{name}",
                         {"provider": provider, "key_ref": key_ref},
                         request_id)
            proto = (pentry or {}).get("protocol", "")
            if proto and _config(c, f"integration_protocol:{proto}") is None \
                    and proto in PROTOCOLS:
                _write_entry(c, ident, f"integration_protocol:{proto}",
                             dict(PROTOCOLS[proto]), f"{request_id}-proto")
            ops.audit(c, "integration_register",
                      {"kind": "credential", "name": name,
                       "by": ident["worker_id"]})
            return {"name": name, "key": f"credential:{name}"}
        return idempotent(c, request_id, "integration_credential", run)


def _http_json(url, headers, timeout=10):
    """GET 一个模型发现端点并解析 JSON 响应(测试经 monkeypatch 本函数注入假网络)。"""
    import urllib.request
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


_AUTH_HEADERS = {
    "bearer": lambda k: {"Authorization": "Bearer " + k},
    "x-api-key": lambda k: {"x-api-key": k},
}


def _probe_context_window(raw: dict) -> "int | None":
    """从供应商 /v1/models 返回的模型原始数据里尝试提取上下文窗口(13.1)。

    常见字段名都认;不认识就返回 None=探测不到,标"待实测"。
    """
    for f in ("context_window", "context_length", "ctx_len",
              "context_window_size", "max_context_length"):
        v = raw.get(f)
        if isinstance(v, int) and v > 0:
            return v
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
    return None


def model_entry(raw, context_window: "int | None" = None,
                display=None, pending_test=None) -> dict:
    """模型条目归一(13.1,票 48): 保证每条带 context_window 字段。

    探测(discover)可得就填数值,探测不到标"待实测";display/pending_test
    保留原有语义(人工补录=待实测模型)。所有写模型条目的地方统一走这里,
    避免两处漂移。
    """
    if isinstance(raw, dict):
        m = {"id": raw.get("id")}
        if context_window is None:
            context_window = _probe_context_window(raw)
        if display is None:
            display = raw.get("display")
        if pending_test is None:
            pending_test = raw.get("pending_test")
    else:
        m = {"id": str(raw)}
    m["context_window"] = context_window
    if context_window is None:
        m["context_window_status"] = "待实测"
    if display is not None:
        m["display"] = display
    if pending_test is not None:
        m["pending_test"] = pending_test
    return m


def discover_models(conn, ident=None, provider="", base_url="", protocol="",
                    credential="", key_value="", key_ref="", timeout=10):
    """标准模型发现(13.8/票33): 按供应商条目的协议取端点候选与认证方式,
    多端点×多认证头机械探测;成功把模型清单写进供应商条目缓存
    (models/discovered_at/discovery_source,仅总控身份);失败返回原因、不落账。
    自定义 OpenAI 兼容服务可给 base_url+protocol 保底发现(不要求先登记,
    只探测不缓存)。明文 key 只在本次请求内使用,不进账本;来源依次:
    key_value > credential(凭据引用→key 文件) > 供应商条目 key_ref。
    故意不带幂等回执——手动刷新就是要能重跑。
    """
    if provider:
        entry = _config(conn, f"integration_provider:{provider}")
        if entry is None:
            raise ValueError(f"供应商 {provider} 未登记")
        base_url = base_url or entry.get("base_url", "")
        protocol = protocol or entry.get("protocol", "")
        paths = entry.get("model_discovery_paths") \
            or PROTOCOLS.get(protocol, {}).get("model_discovery_paths", [])
        stored_key_ref = entry.get("key_ref") or ""
    else:
        paths = PROTOCOLS.get(protocol, {}).get("model_discovery_paths", [])
        stored_key_ref = ""
        if not base_url or not protocol:
            raise ValueError("保底发现须给已登记供应商名或 base_url+API 协议")
    if not paths:
        raise ValueError(f"协议 {protocol} 无模型发现端点候选")

    key = key_value or ""
    if not key and credential:
        cred = _config(conn, f"credential:{credential}")
        if cred is None:
            raise ValueError(f"credential:{credential} 未登记")
        key_ref = key_ref or cred.get("key_ref") or ""
    if not key and key_ref:
        p = Path(key_ref)
        if p.is_file():
            key = p.read_text(encoding="utf-8").strip()
    if not key and stored_key_ref:
        p = Path(stored_key_ref)
        if p.is_file():
            key = p.read_text(encoding="utf-8").strip()
    if not key:
        raise ValueError("探测要给 key(明文只在本请求内用):"
                         " key_value / credential 引用 / key_ref 文件任一")

    auth_styles = PROTOCOLS.get(protocol, {}).get(
        "auth_styles", list(_AUTH_HEADERS))
    root = base_url.rstrip("/")
    seen, attempts = set(), []
    for path in paths:
        url = root + path
        if url in seen:
            continue
        seen.add(url)
        for style in auth_styles:
            build = _AUTH_HEADERS.get(style)
            if build is None:
                continue
            try:
                data = _http_json(url, build(key), timeout=timeout)
            except Exception as e:
                attempts.append(f"{path}({style}): {e.__class__.__name__}")
                continue
            ids = [m["id"] for m in data.get("data", [])
                   if isinstance(m, dict) and m.get("id")]
            if ids:
                source = f"GET {path} ({style})"
                cached = False
                if provider and ident is not None:
                    if not auth.check_controller(conn, ident):
                        raise PermissionError(
                            "模型缓存写入仅总控身份可执行")
                    with tx(conn) as c:
                        cur = _config(c, f"integration_provider:{provider}")
                        # 13.1: 探测时顺带提取上下文窗口,拿不到标"待实测"
                        cur["models"] = [model_entry(m) for m in
                                         data.get("data", [])
                                         if isinstance(m, dict) and m.get("id")]
                        cur["discovered_at"] = now()
                        cur["discovery_source"] = source
                        _write_entry(c, ident,
                                     f"integration_provider:{provider}",
                                     cur, None)
                        ops.audit(c, "integration_discover", {
                            "provider": provider, "count": len(ids),
                            "source": source, "by": ident["worker_id"]})
                    cached = True
                return {"ok": True, "models": ids, "source": source,
                        "cached": cached}
    return {"ok": False, "models": [],
            "reason": "全部候选探测失败(" + "; ".join(attempts) + ")",
            "attempts": attempts}


def add_provider_model(conn, ident, provider, model_id, request_id=None):
    """人工补录供应商模型并标'待实测'(13.8: 探测失败允许人工录入)。"""
    if not auth.check_controller(conn, ident):
        raise PermissionError("集成注册表变更仅总控身份可执行")
    if not request_id:
        raise ValueError("集成条目写入必须带 request_id")
    if not model_id:
        raise ValueError("模型 id 不能为空")
    with tx(conn) as c:
        def run():
            entry = _config(c, f"integration_provider:{provider}")
            if entry is None:
                raise ValueError(f"供应商 {provider} 未登记")
            if any(m.get("id") == model_id for m in entry.get("models", [])):
                raise ValueError(
                    f"模型 {model_id} 已在 {provider} 清单里(刷新缓存即可)")
            entry.setdefault("models", []).append(
                model_entry({"id": model_id}, pending_test=True))
            _write_entry(c, ident, f"integration_provider:{provider}",
                         entry, request_id)
            ops.audit(c, "integration_model_add", {
                "provider": provider, "model": model_id,
                "by": ident["worker_id"]})
            return {"provider": provider, "model": model_id,
                    "pending_test": True}
        return idempotent(c, request_id, "integration_model_add", run)


def require_resolvable_credential(conn, key_name, base_url="", protocol="",
                                  key_ref=""):
    """装配前置校验(13.8): 凭据路径缺注册表条目且已给数据不足以显式
    登记时报错指路;免 key 实例(key_name 空)不要求凭据条目。

    返回解析结果 {"provider","protocol","key_ref"};不通过抛 ValueError。
    """
    if not key_name:
        return None  # 免 key 路径(如壳内置 OAuth): 无凭据可登记
    if _config(conn, f"credential:{key_name}") is not None:
        return None  # 已显式登记,直接放行
    row = conn.execute("SELECT value FROM configs WHERE key=?",
                       (f"key:{key_name}",)).fetchone()
    cfg = json.loads(row["value"]) if row else {}
    p_base = cfg.get("base_url") or base_url
    p_proto = normalize_legacy_protocol(cfg.get("protocol") or protocol)
    p_ref = key_ref or cfg.get("key_ref") or ""
    pname = cfg.get("registry_name") or provider_name(p_base or "") or key_name
    if not p_base:
        raise ValueError(
            f"集成注册表缺少供应商条目,key {key_name} 无 base_url 可解析"
            "(先 wizard provider-add 显式登记,或提供 --base-url)")
    if not p_proto:
        raise ValueError(
            f"集成注册表缺少 API 协议,key {key_name} 无法确定供应商协议"
            "(自定义供应商须显式给 API 协议)")
    if not p_ref:
        raise ValueError(
            f"集成注册表缺少 credential:{key_name}(凭据只存文件引用,"
            "须给 key_ref 或先 wizard credential-add 显式登记)")
    return {"provider": pname, "protocol": p_proto, "key_ref": p_ref}


def ensure_instance_entries(conn, ident, shell, shell_entry=None,
                            shell_source="template", key_name="",
                            base_url="", protocol="", key_ref="",
                            request_id=None):
    """实例装配的显式增量登记桥(13.8,确认生成阶段调用): 把本次用到的
    壳/供应商/协议/凭据按已给数据登记为可复用注册表条目;
    缺则建、有不覆盖(天然幂等),不生成一次性混合配置。
    """
    if not auth.check_controller(conn, ident):
        raise PermissionError("集成注册表变更仅总控身份可执行")
    bridged = []

    # Convergence fields (renderer, provider_env, ...) 来自路由器模板;
    # SHELL_ENTRY_DEFAULTS 只含装配数据(binding/protocols), 不含渲染决策。
    _convergence: dict = {}
    try:
        from .adapters.template import _BUILTIN as _templates
        _convergence = dict(_templates.get(shell, {}))
    except (ImportError, AttributeError):
        pass

    with tx(conn) as c:
        skey = f"integration_shell:{shell}"
        if _config(c, skey) is None:
            value = dict(shell_entry or {})
            # 补入收敛字段: template 里有、SHELL_ENTRY_DEFAULTS 没写的字段
            for k in ("renderer", "provider_env", "controller_settings",
                      "adapter", "capabilities"):
                if k in _convergence and k not in value:
                    value[k] = _convergence[k]
            value["source"] = shell_source
            _write_entry(c, ident, skey, value, request_id)
            bridged.append(skey)
        cred = _config(c, f"credential:{key_name}") if key_name else None
        if cred is None and key_name:
            resolved = require_resolvable_credential(
                c, key_name, base_url=base_url, protocol=protocol,
                key_ref=key_ref)
            pname, p_proto, p_ref = (
                resolved["provider"], resolved["protocol"],
                resolved["key_ref"])
            row = c.execute("SELECT value FROM configs WHERE key=?",
                            (f"key:{key_name}",)).fetchone()
            cfg = json.loads(row["value"]) if row else {}
            p_base = cfg.get("base_url") or base_url
            pkey = f"integration_provider:{pname}"
            if _config(c, pkey) is None:
                entry = _provider_entry(pname, p_base, p_proto)
                entry["source"] = "legacy"
                entry["credential_key"] = key_name
                entry["key_ref"] = p_ref
                models = cfg.get("models", [])
                if models:
                    entry["models"] = models
                    entry["discovered_at"] = now()
                _write_entry(c, ident, pkey, entry, request_id)
                bridged.append(pkey)
            _write_entry(c, ident, f"credential:{key_name}",
                         {"provider": pname, "key_ref": p_ref}, request_id)
            bridged.append(f"credential:{key_name}")
        else:
            pname = (cred or {}).get("provider", "")
            pentry = _config(c, f"integration_provider:{pname}") \
                if pname else None
            p_proto = (pentry or {}).get("protocol", "")
        # 协议条目随装配保证在场(出厂协议,机械补齐)
        if key_name and p_proto and p_proto in PROTOCOLS and \
                _config(c, f"integration_protocol:{p_proto}") is None:
            _write_entry(c, ident, f"integration_protocol:{p_proto}",
                         dict(PROTOCOLS[p_proto]), request_id)
            bridged.append(f"integration_protocol:{p_proto}")
        ops.audit(c, "integration_bridge", {
            "shell": shell, "key_name": key_name, "bridged": bridged,
            "by": ident["worker_id"]})
    return {"bridged": bridged}


def ensure_builtin_registry(conn, ident, request_id=None):
    """出厂协议与常见供应商大类入库;已存在条目不覆盖用户改动。"""
    if not auth.check_controller(conn, ident):
        raise PermissionError("集成注册表变更仅总控身份可执行")
    rid = request_id or f"builtin-{now()}"
    protocols = []
    for name, spec in PROTOCOLS.items():
        key = f"integration_protocol:{name}"
        if _config(conn, key) is None:
            _put(conn, ident, key, dict(spec), f"{rid}-{name}")
            protocols.append(name)
    providers = []
    for name, spec in BUILTIN_PROVIDERS.items():
        key = f"integration_provider:{name}"
        if _config(conn, key) is None:
            entry = _provider_entry(
                name, spec["base_url"], spec["protocol"], spec["auth_style"],
                builtin=True, display=spec["display"])
            entry["category"] = spec["category"]
            entry["builtin"] = True
            _put(conn, ident, key, entry, f"{rid}-{name}")
            providers.append(name)
    result = {"protocols": protocols, "providers": providers}
    ops.audit(conn, "integration_ensure", {**result, "by": ident["worker_id"]})
    return result


_REGISTRY_PREFIXES = (
    "integration_provider:", "integration_protocol:",
    "integration_shell:", "credential:");

def provider_name(base_url: str) -> str:
    """从常见 base URL 推导供应商名;未命中时返回空。"""
    for name, spec in BUILTIN_PROVIDERS.items():
        if base_url == spec["base_url"]:
            return name
    return ""


def _write_entry(conn, ident, key, value, request_id):
    """事务内直写配置表;外层函数负责统一审计。"""
    old = conn.execute("SELECT value FROM configs WHERE key=?", (key,)).fetchone()
    encoded = json.dumps(value, ensure_ascii=False)
    conn.execute(
        "INSERT INTO configs (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, encoded, now()))
    return {"old_value": old["value"] if old else None}


def normalize_legacy_protocol(protocol: str) -> str:
    """旧 openai 泛称映射到新协议名;未知值原样返回。"""
    return "openai_chat" if protocol == "openai" else protocol


def derive_provider_name(base_url: str) -> str:
    """自定义 URL 推导稳定且安全的供应商名;同名 URL 归同一供应商。"""
    parsed = urlsplit(base_url)
    host = re.sub(r"[^a-z0-9._-]+", "-", parsed.netloc.lower()).strip("-")
    digest = hashlib.sha1(base_url.strip().encode("utf-8")).hexdigest()[:8]
    return f"custom-{host or 'local'}-{digest}"


def _legacy_key_cfg(conn, key_name: str) -> dict:
    if not key_name:
        return {}
    row = conn.execute(
        "SELECT value FROM configs WHERE key=?", (f"key:{key_name}",)
    ).fetchone()
    return json.loads(row["value"]) if row else {}


def validate_worker_card(conn, shell: str, model: str, provider="",
                         base_url="", protocol="", key_name="",
                         shell_protocols=None) -> dict:
    """Web 提交前的注册表机械校验(13.4/13.8)。

    校验不写账本;自定义 URL 允许在通过后由调用方显式登记。
    返回解析出的 provider / protocol,供登记与落地复用。
    """
    shell_cfg = _config(conn, f"integration_shell:{shell}")
    if shell_cfg is None:
        row = conn.execute(
            "SELECT value FROM configs WHERE key=?", (f"shell:{shell}",)
        ).fetchone()
        shell_cfg = json.loads(row["value"]) if row else None
    if shell_cfg is None and shell_protocols is not None:
        shell_cfg = {"protocols": shell_protocols}
    if shell_cfg is None:
        raise ValueError(f"助手壳 {shell} 没有壳条目,不能配置实例")

    pentry = None
    pname = (provider or "").strip()
    base_url = (base_url or "").strip()
    if pname:
        pentry = _config(conn, f"integration_provider:{pname}")
        if pentry is not None:
            if base_url and pentry.get("base_url") != base_url:
                raise ValueError(
                    f"接口地址与供应商 {pname} 登记不一致"
                    f"(登记: {pentry.get('base_url', '')})")
            base_url = pentry.get("base_url", "")
            protocol = protocol or pentry.get("protocol", "")
        elif not base_url:
            raise ValueError(f"供应商 {pname} 未登记且未给接口地址")
    else:
        inferred = provider_name(base_url)
        if inferred:
            pname = inferred
            pentry = _config(conn, f"integration_provider:{pname}")

    p_proto = normalize_legacy_protocol((protocol or "").strip())
    if pentry is not None:
        p_proto = normalize_legacy_protocol(
            p_proto or pentry.get("protocol", ""))
        models = [m.get("id") for m in pentry.get("models", [])
                  if isinstance(m, dict) and m.get("id")]
        if models and model not in models:
            raise ValueError(
                f"模型 {model} 不在供应商 {pname} 清单里"
                f"(可选: {models})")
    raw_shell_protocols = shell_cfg.get("protocols", [])
    shell_protocols = {
        normalize_legacy_protocol(p) for p in raw_shell_protocols}
    if not p_proto:
        if not base_url or not shell_protocols:
            raise ValueError(
                "API 协议不能确定;请选择供应商预设或给自定义服务选协议")
        p_proto = next(iter(sorted(shell_protocols)))
    if p_proto not in shell_protocols:
        raise ValueError(
            f"协议不兼容: 壳 {shell} 支持 {sorted(shell_protocols)},"
            f" 供应商需要 {p_proto}")

    legacy_cfg = _legacy_key_cfg(conn, key_name)
    coding_plan = (pentry or {}).get("coding_plan", False) if pentry \
        else False
    coding_plan = coding_plan or bool(legacy_cfg.get("coding_plan"))
    bound_shell = (pentry or {}).get("coding_plan_shell", "") \
        if pentry else ""
    legacy_ref = str(legacy_cfg.get("key_ref") or "")
    match = re.fullmatch(r"shell:([^/]+)", legacy_ref)
    if match:
        bound_shell = bound_shell or match.group(1)
    if coding_plan:
        existing = conn.execute(
            "SELECT shell FROM instances WHERE key_name=? AND is_active=1",
            (key_name,)).fetchone() if key_name else None
        if existing:
            bound_shell = bound_shell or existing["shell"]
        if bound_shell and bound_shell != shell:
            raise ValueError(
                f"CodingPlan 跨壳: 凭据绑定 {bound_shell},不能用於壳 {shell}")

    return {"provider": pname or derive_provider_name(base_url),
            "protocol": p_proto, "base_url": base_url,
            "coding_plan": coding_plan}


def migrate_legacy_entries(conn, ident, request_id=None):
    """从旧 shell/key 条目推导集成条目;旧数据保留为兼容读取源。"""
    if not auth.check_controller(conn, ident):
        raise PermissionError("集成注册表迁移仅总控身份可执行")
    if not request_id:
        raise ValueError("集成注册表迁移必须带 request_id")
    with tx(conn) as c:
        def run():
            providers = []
            shells = []
            migrated_shells = {row["key"][6:] for row in c.execute(
                "SELECT key FROM configs WHERE key LIKE 'integration_shell:%'").fetchall()}
            migrated_providers = {value.get("credential_key", "") for value in (
                json.loads(row["value"]) for row in c.execute(
                    "SELECT value FROM configs WHERE key LIKE 'integration_provider:%'").fetchall())
                if value.get("source") == "legacy"}

            for row in c.execute(
                    "SELECT key, value FROM configs WHERE key LIKE 'key:%'"
            ).fetchall():
                legacy_key = row["key"]
                cfg = json.loads(row["value"])
                name = (cfg.get("registry_name")
                        or provider_name(cfg.get("base_url", ""))
                        or legacy_key[4:])
                target = f"integration_provider:{name}"
                current = _config(c, target)
                if current is None:
                    current = _provider_entry(
                        name, cfg.get("base_url", ""),
                        normalize_legacy_protocol(cfg.get("protocol") or "openai"))
                    current["source"] = "legacy"
                    current["credential_key"] = legacy_key[4:]
                    current["key_ref"] = cfg.get("key_ref")
                    models = cfg.get("models", [])
                    if models:
                        # 13.1(票 48): 迁移时归一模型条目,保证带 context_window
                        # 字段(旧数据没有探测值→标"待实测")
                        current["models"] = [
                            model_entry(m) if isinstance(m, dict) else
                            {"id": str(m), "context_window": None,
                             "context_window_status": "待实测"}
                            for m in models]
                        current["discovered_at"] = now()
                    _write_entry(c, ident, target, current,
                                 f"{request_id}-{legacy_key}")
                    if legacy_key[4:] not in migrated_providers:
                        providers.append(target)

            for row in c.execute(
                    "SELECT key, value FROM configs WHERE key LIKE 'shell:%'"
            ).fetchall():
                legacy_key = row["key"]
                name = legacy_key[6:]
                target = f"integration_shell:{name}"
                if _config(c, target) is None:
                    value = json.loads(row["value"])
                    value["source"] = "legacy"
                    value["legacy_entry"] = legacy_key
                    _write_entry(c, ident, target, value,
                                 f"{request_id}-{legacy_key}")
                    if name not in migrated_shells:
                        shells.append(target)
            result = {"providers": len(providers), "shells": len(shells)}
            ops.audit(c, "integration_migrate",
                      {**result, "by": ident["worker_id"]})
            return result
        return idempotent(c, request_id, "integration_migrate", run)

def registry_state(conn):
    """Web/CLI 可读的注册表快照与旧条目迁移状态(不含任何密钥本体)。"""
    entries = []
    for row in conn.execute(
            "SELECT key, value FROM configs ORDER BY key").fetchall():
        prefix = next((p for p in _REGISTRY_PREFIXES if row["key"].startswith(p)), None)
        if prefix:
            entries.append({"key": row["key"], **json.loads(row["value"])})
    migrated_keys = {json.loads(r["value"]).get("credential_key", "")
                     for r in conn.execute(
                         "SELECT value FROM configs "
                         "WHERE key LIKE 'integration_provider:%'").fetchall()}
    migrations = []
    for legacy_prefix, target_prefix in (
            ("shell:", "integration_shell:"),
            ("key:", "integration_provider:")):
        for row in conn.execute(
                "SELECT key FROM configs WHERE key LIKE ?",
                (f"{legacy_prefix}%",)).fetchall():
            legacy = row["key"]
            name = legacy[len(legacy_prefix):]
            # key 条目迁移时供应商名可被重映射(URL 归类/registry_name),
            # 以 provider 条目的 credential_key 反查为准
            if legacy_prefix == "key:":
                migrated = name in migrated_keys
            else:
                migrated = _config(conn, f"{target_prefix}{name}") is not None
            migrations.append({"legacy": legacy, "name": name,
                               "target": target_prefix,
                               "migrated": migrated})
    return {"entries": entries, "migrations": migrations}
