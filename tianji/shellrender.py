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
    """key 本体从受保护引用文件现读;引用缺失/文件不在=空串如实降级。"""
    if not key_ref:
        return ""
    p = Path(key_ref)
    return p.read_text(encoding="utf-8").strip() if p.is_file() else ""


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
    """壳内配置型(config_binding morph): key 留各壳配置域,启动器=调壳的薄命令。"""
    entry = ctx.get("entry") or {}
    env_name = entry.get("worker_data_root_env") or ""
    if env_name and ctx["isolated_dir"]:
        return (f'cmd /c "set {env_name}={ctx["isolated_dir"]}&& '
                f'{ctx["shell"]}"', [])
    return ctx["shell"], []


def render(conn, shell, instance="", model="", key_name="", isolated_dir="",
           entry=None):
    """通用装配控制流: 壳名→morph→renderer→(launch_cmd, artifacts)。零壳名分支。"""
    morph = _shell_to_morph(shell, conn=conn)
    if morph is None:
        raise ValueError(f"壳 {shell} 无 renderer(新壳先登记 renderer+壳条目)")
    fn = RENDERERS.get(morph)
    if fn is None:
        raise ValueError(f"壳 {shell} 的 morph={morph} 无渲染器实现")
    _, _, key_ref, base_url = resolve_credential(conn, key_name)
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
               "entry": entry, "key_ref": key_ref, "base_url": base_url})


def rerender_instance(conn, name):
    """已注册实例按当前注册表重渲染产物(重启一致性验证入口)。

    只查表渲染,不触发任何缺失配置补建;注册表缺条目时如实报错。
    """
    inst = conn.execute(
        "SELECT name, shell, model, key_name, isolated_dir FROM instances"
        " WHERE name=? AND is_active=1", (name,)).fetchone()
    if inst is None:
        raise ValueError(f"实例 {name} 未注册或不活跃")
    return render(conn, inst["shell"], instance=inst["name"],
                  model=inst["model"], key_name=inst["key_name"] or "",
                  isolated_dir=inst["isolated_dir"] or "")
