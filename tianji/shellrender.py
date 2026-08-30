"""声明式壳渲染器(13.8/票34): 启动器不按具体助手名硬编码分支。

通用装配控制流(wizard._generate_launcher)只做一件事: 按实例的壳名从
RENDERERS 注册表取 renderer,把(壳条目+供应商+协议+凭据引用+模型+隔离
目录)机械渲染成启动命令与隔离配置产物。新增壳=新增 renderer 函数+
壳条目数据,不改通用控制流。

凭据解析统一走集成注册表: credential:{key_name} → {provider, key_ref}
→ 供应商条目取 base_url;无 credential 的旧账本回落旧 key:{name} 条目
(迁移映射,只读不补建)。明文 key 本体只在受保护引用位置出现:
claude=隔离目录 settings 文件;codex=launch_cmd 里 set /p 现读 key 文件;
其余壳=key 留各壳自身配置域,天机不经手。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from . import integrations

# claude 必须走 --settings 文件(全局 settings.json 的 env 进程 env 覆盖不掉,
# 2026-08-20 实证);codex 走 CODEX_HOME+config.toml
_CLAUDE_SETTINGS_TEMPLATE = {
    "env": {
        "ANTHROPIC_AUTH_TOKEN": "{token}",
        "ANTHROPIC_BASE_URL": "{base_url}",
        "ANTHROPIC_MODEL": "{model}",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "{model}",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "{model}",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "{model}",
        "ANTHROPIC_DEFAULT_FABLE_MODEL": "{model}",
    }
}

_CODEX_CONFIG = """model_provider = "wizard"
model = "{model}"
model_reasoning_effort = "medium"
disable_response_storage = true

[model_providers.wizard]
name = "{key_name}"
base_url = "{base_url}"
wire_api = "responses"
env_key = "TIANJI_WIZARD_KEY"
requires_openai_auth = true

[windows]
sandbox = "elevated"
"""


def _claude_settings(token: str, base_url: str, model: str) -> str:
    doc = json.loads(json.dumps(_CLAUDE_SETTINGS_TEMPLATE))  # deep copy
    env = doc["env"]
    env["ANTHROPIC_AUTH_TOKEN"] = token
    env["ANTHROPIC_BASE_URL"] = base_url
    env["ANTHROPIC_MODEL"] = model
    for tier in ("HAIKU", "SONNET", "OPUS", "FABLE"):
        env[f"ANTHROPIC_DEFAULT_{tier}_MODEL"] = model
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


def resolve_credential(conn, key_name):
    """凭据引用→(provider 条目, key_ref);注册表优先,旧 key: 条目兜底。

    返回 (credential 引用 dict 或 None, provider 条目或 None, key_ref, base_url);
    只查表不补建——缺失由调用方决定报错或免 key 放行(13.8 禁静默拼装)。
    """
    cred = prov = None
    key_ref = base_url = ""
    if key_name:
        cred = integrations._config(conn, f"credential:{key_name}")
        if cred is not None:
            key_ref = cred.get("key_ref") or ""
            pname = cred.get("provider", "")
            if pname:
                prov = integrations._config(
                    conn, f"integration_provider:{pname}") or {}
                base_url = prov.get("base_url", "")
        else:
            # 迁移映射: 旧命名空间只读兼容(13.8 旧条目作迁移源/读取兼容)
            row = conn.execute("SELECT value FROM configs WHERE key=?",
                               (f"key:{key_name}",)).fetchone()
            if row is not None:
                cfg = json.loads(row["value"])
                key_ref = cfg.get("key_ref") or ""
                base_url = cfg.get("base_url") or ""
    return cred, prov, key_ref, base_url


def _read_key(key_ref: str) -> str:
    """key 本体从受保护引用文件现读(附录 E.4)。

    空 key_ref=免 key 放行,返回空串;引用给了但文件读不到=fail-loud
    抛 FileNotFoundError 指路,不静默省略(静默省略会让壳拿着空 key 启动,
    故障现场离真实原因十万八千里)。
    """
    if not key_ref:
        return ""
    p = Path(key_ref)
    if not p.is_file():
        raise FileNotFoundError(
            f"key 引用文件不存在: {key_ref}(先落盘凭据文件或修正 key_ref 指向)")
    return p.read_text(encoding="utf-8").strip()


def _pool_token(conn, name: str) -> str:
    """Read pool token from configs table."""
    row = conn.execute(
        "SELECT value FROM configs WHERE key=?", (f"pool:token:{name}",)
    ).fetchone()
    return row["value"] if row else ""


def _pool_proxy_url(conn, pool_name: str = "") -> str:
    """Proxy base_url from daemon config, with optional /proxy/<pool_name> path."""
    row = conn.execute(
        "SELECT value FROM configs WHERE key=?", ("daemon.proxy_port",)
    ).fetchone()
    port = (row["value"] if row else "").strip()
    if not port:
        return ""
    url = f"http://127.0.0.1:{port}"
    if pool_name:
        url = f"{url}/proxy/{pool_name}"
    return url


# ---------------------------------------------------------------- renderers

RENDERERS: dict[str, Callable] = {}


def _shell_to_morph(shell: str, conn=None) -> str | None:
    """壳名→morph 名(读壳条目 renderer 字段;缺则内置模板兜底)。"""
    if conn is None:
        from .db import connect
        conn = connect()
    row = conn.execute(
        "SELECT value FROM configs WHERE key=?",
        (f"integration_shell:{shell}",)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT value FROM configs WHERE key=?",
            (f"shell:{shell}",)).fetchone()
    if row:
        try:
            return json.loads(row["value"]).get("renderer")
        except (json.JSONDecodeError, AttributeError):
            pass
    # 内置模板兜底
    try:
        from .adapters.template import _BUILTIN
        return _BUILTIN.get(shell, {}).get("renderer")
    except ImportError:
        pass
    return None


def renderer(*morph_names):
    """壳渲染器注册器: 一个实现可挂多个 morph 名(同形态壳的明确迁移映射)。

    新增壳=在壳条目声明 renderer=morph 名,通用控制流自动路由。
    """
    def deco(fn):
        for n in morph_names:
            RENDERERS[n] = fn
        return fn
    return deco


@renderer("claude")
def _render_claude(ctx):
    """claude(env 注入型): 隔离目录 settings 文件携带 provider env。"""
    iso = ctx["isolated_dir"]
    if not iso:
        raise ValueError("claude 实例须给 isolated_dir(settings 文件落盘位置)")
    cfg_dir = Path(iso)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    settings = cfg_dir / "settings.json"
    settings.write_text(
        _claude_settings(_read_key(ctx["key_ref"]),
                         ctx["base_url"], ctx["model"]),
        encoding="utf-8")
    return f'claude --settings "{settings}"', [str(settings)]


@renderer("codex")
def _render_codex(ctx):
    """codex(env 注入型): CODEX_HOME 隔离 + config.toml;key 不落 launch_cmd。"""
    iso = ctx["isolated_dir"]
    if not iso:
        raise ValueError("codex 实例须给 isolated_dir(CODEX_HOME)")
    cfg_dir = Path(iso)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "config.toml"
    cfg.write_text(_CODEX_CONFIG.format(
        model=ctx["model"], key_name=ctx["key_name"],
        base_url=ctx["base_url"]), encoding="utf-8")
    # key 本体不落 launch_cmd: 从 key_ref 文件现读
    launch_cmd = (f'cmd /c "set CODEX_HOME={cfg_dir}&& set /p '
                  f'TIANJI_WIZARD_KEY=<{ctx["key_ref"]}&& codex exec"')
    return launch_cmd, [str(cfg)]


@renderer("config_binding")
def _render_config_binding(ctx):
    """壳内配置型(config_binding morph): key 留各壳配置域,启动器=调壳的薄命令。

    票59: 池绑定时通过 provider_env.map 数据驱动注入 proxy/凭证环境变量,
    不碰具体壳名。
    """
    entry = ctx.get("entry") or {}
    env_name = entry.get("worker_data_root_env") or ""
    cmd = ctx["shell"]

    # thinking patch 接线(票 26,dsh 形态): 实例设了思考级别且壳模板声明
    # --patch 机制时,把 render._apply_thinking_level 写入隔离目录的
    # thinking.patch.yml 接进 launch_cmd(路径与其写入处逐字对齐)。
    _ZH2EN = {"低": "low", "中": "medium", "高": "high"}
    level = ctx.get("thinking_level") or ""
    tmap = entry.get("thinking_level_map") or {}
    rule = tmap.get(_ZH2EN.get(level, "")) or {}
    if rule.get("param") == "--patch" and ctx.get("isolated_dir"):
        patch = Path(ctx["isolated_dir"]) / "thinking.patch.yml"
        cmd = f'{cmd} --patch "{patch}"'

    # provider_env.process_env: 按 entry 内 map 模板注入 env(数据驱动)
    prov_env = (entry.get("provider_env") or {}).get("map") or {}
    key_txt = _read_key(ctx.get("key_ref", ""))
    fmt = {
        "key": key_txt,
        "model": ctx.get("model", ""),
        "base_url": ctx.get("base_url", ""),
        "protocol": "",
    }
    prefix_parts = []
    for var, tpl in prov_env.items():
        try:
            val = tpl.format(**fmt)
        except (KeyError, ValueError):
            val = ""
        if val:
            ec = var[1:] if var.startswith("$") else var
            prefix_parts.append(f"set {ec}={val}")
    if prefix_parts:
        cmd = "&& ".join(prefix_parts) + "&& " + cmd

    _arts = [ctx.get("key_ref", "")] if ctx.get("key_ref") else []
    if env_name and ctx["isolated_dir"]:
        return (f'cmd /c "set {env_name}={ctx["isolated_dir"]}&& '
                f'{cmd}"'), _arts
    return cmd, _arts

def render(conn, shell, instance="", model="", key_name="", isolated_dir="",
           entry=None, thinking_level=""):
    """通用装配控制流: 壳名→morph→renderer→(launch_cmd, artifacts)。零壳名分支。"""
    morph = _shell_to_morph(shell, conn=conn)
    if morph is None:
        raise ValueError(f"壳 {shell} 无 renderer(新壳先登记 renderer+壳条目)")
    fn = RENDERERS.get(morph)
    if fn is None:
        raise ValueError(f"壳 {shell} 的 morph={morph} 无渲染器实现")
    _, _, key_ref, base_url = resolve_credential(conn, key_name)
    # ---- 池名回退(票59) --------------------------------------
    if not key_ref and key_name and isolated_dir:
        pool_row = conn.execute(
            "SELECT key FROM configs WHERE key=?",
            (f"pool:{key_name}",)).fetchone()
        if pool_row is not None:
            token = _pool_token(conn, key_name)
            if token:
                iso = Path(isolated_dir)
                iso.mkdir(parents=True, exist_ok=True)
                tf = iso / "pool-token.key"
                tf.write_text(token, encoding="utf-8")
                key_ref = str(tf)
                base_url = _pool_proxy_url(conn, key_name) or base_url
    if entry is None:
        row = conn.execute("SELECT value FROM configs WHERE key=?",
                           (f"integration_shell:{shell}",)).fetchone()
        if row is not None:
            entry = json.loads(row["value"])
        else:
            row = conn.execute("SELECT value FROM configs WHERE key=?",
                               (f"shell:{shell}",)).fetchone()
            entry = json.loads(row["value"]) if row else {}
    return fn({"instance": instance, "shell": shell, "model": model,
               "key_name": key_name, "isolated_dir": isolated_dir,
               "entry": entry, "key_ref": key_ref, "base_url": base_url,
               "thinking_level": thinking_level})


def rerender_instance(conn, name):
    """已注册实例按当前注册表重渲染产物(重启一致性验证入口)。

    只查表渲染,不触发任何缺失配置补建;注册表缺条目时如实报错。
    """
    inst = conn.execute(
        "SELECT name, shell, model, key_name, isolated_dir, thinking_level FROM instances"
        " WHERE name=? AND is_active=1", (name,)).fetchone()
    if inst is None:
        raise ValueError(f"实例 {name} 未注册或不活跃")
    return render(conn, inst["shell"], instance=inst["name"],
                  model=inst["model"], key_name=inst["key_name"] or "",
                  isolated_dir=inst["isolated_dir"] or "",
                  thinking_level=inst["thinking_level"] if "thinking_level" in inst.keys() else "")
