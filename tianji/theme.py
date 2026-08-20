"""主题插件(票 24,规格书 21.5): 绰号/主题沉浸,纯装饰层。

- 主题包三件套存账本 configs: 起名清单(人物名+备用地名)/说话模板/开关(默认关)
- 出厂"三国"示例主题;中途开关=账本配置项带审计
- 腔调生效两处: 总控会话=话术作提示词注入(theme_guidance,与技能包同待遇);
  驾驶舱=直接显示实例名,不额外加工
- 铁界: 主题腔调只限总控↔用户交互面;任务书/派单提示词/角色间消息高效直白
  (主题变量不进任何机制渲染路径)
- 名字耗尽: 提示进账本报账,不阻塞,不搞序号凑名
- 失效回退(21.4 fail-open): 插件关/坏=已起名字保留+话术退回大白话+审计
- 本主题=票 23 插件框架的首个模板类插件客户(试水验证框架好用与否)
"""

from __future__ import annotations

import json

from . import auth, messages, ops, plugins
from .db import now

# 出厂示例主题(21.5): 三国
BUILTIN_THEMES = {
    "三国": {
        "names": ["诸葛亮", "赵云", "关羽", "张飞", "黄忠", "马超", "庞统",
                  "司马懿", "姜维", "张辽", "郭嘉", "荀彧", "周瑜", "陆逊"],
        "fallback_names": ["成都", "洛阳", "建业", "许昌"],
        "tone": {"report": "报告主公", "assign": "且听号令",
                 "praise": "干得漂亮"},
    },
}

_PLUGIN_NAME = "主题话术"
_TONE_TEMPLATE = (
    "你是天机总控,当前主题为{theme}。对用户说话带主题腔调:"
    "汇报开场用\"{report}\",派活确认用\"{assign}\",夸奖用\"{praise}\"。"
    "腔调只限你我对话;任务书/派单/角色间消息保持高效直白。"
)
_DEFAULT_GUIDANCE = "你是天机总控。全程高效直白,有一说一。"


def _cfg(conn, key):
    row = conn.execute("SELECT value FROM configs WHERE key=?",
                       (key,)).fetchone()
    return row["value"] if row else ""


def is_enabled(conn) -> bool:
    return bool(_cfg(conn, "theme_enabled"))


def current_theme(conn) -> dict | None:
    name = _cfg(conn, "theme_enabled")
    if not name:
        return None
    row = conn.execute("SELECT value FROM configs WHERE key=?",
                       (f"theme:{name}",)).fetchone()
    if row:
        return json.loads(row["value"])
    return BUILTIN_THEMES.get(name)


def list_themes(conn) -> dict:
    custom = {r["key"][6:]: json.loads(r["value"]) for r in conn.execute(
        "SELECT key, value FROM configs WHERE key LIKE 'theme:%'").fetchall()}
    all_themes = {**BUILTIN_THEMES, **custom}
    return {"enabled": _cfg(conn, "theme_enabled") or None,
            "themes": {k: {"names": len(v["names"]),
                           "fallback": len(v.get("fallback_names", []))}
                       for k, v in all_themes.items()}}


def enable(conn, ident, name: str = "三国", request_id=None) -> dict:
    """开启主题(带审计): 注册/渲染话术模板插件(票 23 管线)+开关置位。"""
    if not auth.check_controller(conn, ident):
        raise PermissionError("主题开关仅总控身份可执行")
    theme = BUILTIN_THEMES.get(name)
    row = conn.execute("SELECT value FROM configs WHERE key=?",
                       (f"theme:{name}",)).fetchone()
    if row:
        theme = json.loads(row["value"])
    if theme is None:
        raise KeyError(f"主题 {name} 不存在(内置: {list(BUILTIN_THEMES)})")
    # 模板类插件试水: 话术经票 23 管线渲染落盘
    plugins.register(conn, ident, _PLUGIN_NAME, "template", "v1",
                     {"template": _TONE_TEMPLATE,
                      "params": {"theme": name, **theme["tone"]},
                      "target": "theme/guidance.md"},
                     request_id=f"{request_id}-p" if request_id else None)
    plugins.render_template_plugin(conn, _PLUGIN_NAME)
    conn.execute(
        "INSERT OR REPLACE INTO configs (key, value, updated_at) VALUES (?,?,?)",
        ("theme_enabled", name, now()))
    ops.audit(conn, "theme_on", {"theme": name, "by": ident["worker_id"]})
    return {"enabled": name}


def disable(conn, ident, request_id=None) -> dict:
    """中途关主题(带审计): 实例名不变(已起名保留),话术退回大白话。"""
    if not auth.check_controller(conn, ident):
        raise PermissionError("主题开关仅总控身份可执行")
    old = _cfg(conn, "theme_enabled")
    conn.execute(
        "INSERT OR REPLACE INTO configs (key, value, updated_at) VALUES (?,?,?)",
        ("theme_enabled", "", now()))
    if plugins._load(conn, _PLUGIN_NAME):
        plugins.set_enabled(conn, ident, _PLUGIN_NAME, False,
                            request_id=f"{request_id}-p" if request_id else None)
    ops.audit(conn, "theme_off", {"old": old, "by": ident["worker_id"]})
    return {"disabled": old}


def next_name(conn) -> str | None:
    """按主题清单起新实例名;耗尽→提示进账本报账,不阻塞(不搞序号凑名)。"""
    theme = current_theme(conn)
    if theme is None:
        return None
    used = {r["name"] for r in conn.execute("SELECT name FROM instances")}
    for n in theme["names"] + theme.get("fallback_names", []):
        if n not in used:
            return n
    messages.send(conn, "escalation", "theme",
                  {"reason": f"主题名单已耗尽({len(used)} 实例在用),"
                             f"请补充清单或自起名字"},
                  "controller")
    return None


def guidance(conn) -> str:
    """总控会话话术(21.5 生效处①)。fail-open: 关/坏=退回大白话+审计。"""
    if not is_enabled(conn):
        return _DEFAULT_GUIDANCE
    p = plugins._load(conn, _PLUGIN_NAME)
    if p is None or not p.get("enabled"):
        ops.audit(conn, "theme_fallback",
                  {"reason": "话术插件缺失或已关,退回普通大白话"})
        return _DEFAULT_GUIDANCE
    from .db import tianji_home
    artifact = tianji_home() / p["config"]["target"]
    try:
        if not artifact.exists():
            plugins.render_template_plugin(conn, _PLUGIN_NAME)
        return artifact.read_text(encoding="utf-8").split("\n", 1)[1].strip()
    except Exception as e:
        ops.audit(conn, "theme_fallback",
                  {"reason": f"话术渲染失败: {e},退回普通大白话"})
        return _DEFAULT_GUIDANCE
