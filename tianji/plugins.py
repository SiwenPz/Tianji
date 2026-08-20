"""插件机制框架(票 23,规格书 21 章): 声明式插件观。

- 注册表进账本 configs(plugin:<name>),CLI 管理+变更审计,零配置文件原则不破
- 模板类(21.3): 模板+参数→渲染生成物,版本指纹+对账重装(三态指纹:
  缺失/旧版→机械重生成;用户手工改过→不自动碰,差异报告升级总控)
- 视图类(21.3): 只读账本渲染展示块;渲染器只拿 snapshot 字典,无写账本旁路;
  运行期 fail-open(渲染异常→退回默认块+审计,不影响主流程)
- 核心边界(21.2): 插件只此两类声明式形态,逻辑代码插件 v1 不做,
  结构上就不可注册/覆盖核心机制(账本/状态机/结算/校验/消息/监控判定/重派计数)
"""

import hashlib
import json
from pathlib import Path

from . import auth, messages, ops
from .db import tianji_home

# 视图类数据源白名单(声明式: 只能选框架内置数据源,无代码执行面)
VIEW_SOURCES = ("task_status_counts", "instance_roster")

_HEADER = "<!-- tianji-plugin:{name} version:{version} fingerprint:{fp} -->"


def _key(name: str) -> str:
    return f"plugin:{name}"


def _load(conn, name: str) -> dict | None:
    row = conn.execute("SELECT value FROM configs WHERE key=?",
                       (_key(name),)).fetchone()
    return json.loads(row["value"]) if row else None


def _save(conn, plugin: dict):
    conn.execute(
        "INSERT OR REPLACE INTO configs (key, value, updated_at) VALUES (?,?,?)",
        (_key(plugin["name"]), json.dumps(plugin, ensure_ascii=False),
         ops.now()))


def register(conn, ident, name: str, ptype: str, version: str,
             config: dict = None, request_id: str = None) -> dict:
    """注册/更新插件条目(21.1): 总控专属+审计。同名=更新版本/配置。

    config: template 类须含 template(含 {参数} 占位)/params/target(相对
    TIANJI_HOME);view 类须含 source(VIEW_SOURCES 白名单之一)。
    """
    if not auth.check_controller(conn, ident):
        raise PermissionError("插件注册仅总控身份可执行")
    if ptype not in ("template", "view"):
        raise ValueError(f"插件类型只支持 template|view(逻辑代码插件 v1 不做,21.3)")
    config = config or {}
    if ptype == "template":
        if not config.get("template") or not config.get("target"):
            raise ValueError("模板类插件 config 须含 template 与 target")
    else:
        if config.get("source") not in VIEW_SOURCES:
            raise ValueError(
                f"视图类插件 source 须为内置数据源 {VIEW_SOURCES}(无代码执行面)")
    with ops.tx(conn) as c:
        def _do():
            old = _load(c, name)
            plugin = {"name": name, "type": ptype, "version": version,
                      "config": config,
                      "enabled": old.get("enabled", True) if old else True,
                      "last_fingerprint": (old or {}).get("last_fingerprint", ""),
                      "last_version": (old or {}).get("last_version", "")}
            _save(c, plugin)
            ops.audit(c, "plugin_register",
                      {"name": name, "type": ptype, "version": version,
                       "old_version": (old or {}).get("version", ""),
                       "by": ident["worker_id"]})
            return {"name": name, "registered": True, "updated": old is not None}
        return ops._with_idem(c, request_id, "plugin_register", _do)


def list_plugins(conn) -> list:
    rows = conn.execute(
        "SELECT value FROM configs WHERE key LIKE 'plugin:%'").fetchall()
    return [json.loads(r["value"]) for r in rows]


def set_enabled(conn, ident, name: str, enabled: bool,
                request_id: str = None) -> dict:
    if not auth.check_controller(conn, ident):
        raise PermissionError("插件开关仅总控身份可执行")
    with ops.tx(conn) as c:
        def _do():
            p = _load(c, name)
            if p is None:
                raise KeyError(f"插件 {name} 未注册")
            p["enabled"] = bool(enabled)
            _save(c, p)
            ops.audit(c, "plugin_set_enabled",
                      {"name": name, "enabled": bool(enabled),
                       "by": ident["worker_id"]})
            return {"name": name, "enabled": bool(enabled)}
        return ops._with_idem(c, request_id, "plugin_set_enabled", _do)


def remove(conn, ident, name: str, request_id: str = None) -> dict:
    if not auth.check_controller(conn, ident):
        raise PermissionError("插件删除仅总控身份可执行")
    with ops.tx(conn) as c:
        def _do():
            if _load(c, name) is None:
                raise KeyError(f"插件 {name} 未注册")
            c.execute("DELETE FROM configs WHERE key=?", (_key(name),))
            ops.audit(c, "plugin_remove",
                      {"name": name, "by": ident["worker_id"]})
            return {"name": name, "removed": True}
        return ops._with_idem(c, request_id, "plugin_remove", _do)


# ---------------------------------------------------------------- 模板类管线

def _artifact_path(plugin: dict) -> Path:
    return tianji_home() / plugin["config"]["target"]


def render_template_plugin(conn, name: str) -> dict:
    """渲染模板类插件生成物(21.3): 模板+参数→目标文件,文件头带版本指纹。"""
    p = _load(conn, name)
    if p is None:
        raise KeyError(f"插件 {name} 未注册")
    if p["type"] != "template":
        raise ValueError(f"插件 {name} 不是模板类")
    body = p["config"]["template"].format(**p["config"].get("params", {}))
    fp = hashlib.sha256(body.encode("utf-8")).hexdigest()
    target = _artifact_path(p)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        _HEADER.format(name=name, version=p["version"], fp=fp) + "\n" + body,
        encoding="utf-8")
    p["last_fingerprint"] = fp
    p["last_version"] = p["version"]
    _save(conn, p)
    ops.audit(conn, "plugin_render",
              {"name": name, "version": p["version"], "target": str(target)})
    return {"name": name, "rendered": True, "target": str(target)}


def _parse_header(text: str) -> dict:
    """解析生成物文件头指纹;无头/无法解析=非天机生成(用户手工件)。"""
    first = text.split("\n", 1)[0] if text else ""
    if not first.startswith("<!-- tianji-plugin:"):
        return {}
    out = {}
    for seg in first.replace("<!-- tianji-plugin:", "").replace(" -->", "").split(" "):
        if seg.startswith("version:"):
            out["version"] = seg[len("version:"):]
        elif seg.startswith("fingerprint:"):
            out["fp"] = seg[len("fingerprint:"):]
    return out


def reconcile(conn, name: str) -> dict:
    """对账(21.4,复用 17 章三态指纹): 缺失/旧版→机械重生成;用户改过→不碰+升级。

    返回 status: ok / regenerated_missing / regenerated_upgrade / user_modified。
    """
    p = _load(conn, name)
    if p is None:
        raise KeyError(f"插件 {name} 未注册")
    if p["type"] != "template":
        raise ValueError(f"插件 {name} 不是模板类")
    target = _artifact_path(p)
    if not target.exists():
        r = render_template_plugin(conn, name)
        return {"name": name, "status": "regenerated_missing",
                "target": r["target"]}
    text = target.read_text(encoding="utf-8")
    head = _parse_header(text)
    body = text.split("\n", 1)[1] if "\n" in text else ""
    cur_fp = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if not head or head.get("fp") != p["last_fingerprint"] \
            or cur_fp != p["last_fingerprint"]:
        # 用户手工改过(或非天机生成): 不自动碰,差异报告升级总控(误杀比漏报贵)
        detail = {"name": name, "target": str(target),
                  "expected_fp": p["last_fingerprint"],
                  "found_fp": head.get("fp", ""), "body_fp": cur_fp}
        ops.audit(conn, "plugin_reconcile_diff", detail)
        messages.send(conn, "escalation", "plugins",
                      {"reason": f"插件生成物被手工修改,对账不自动碰: {target}",
                       "plugin": name}, "controller")
        return {"name": name, "status": "user_modified", "target": str(target)}
    if head.get("version") != p["version"]:
        r = render_template_plugin(conn, name)
        return {"name": name, "status": "regenerated_upgrade",
                "target": r["target"]}
    return {"name": name, "status": "ok", "target": str(target)}


# ---------------------------------------------------------------- 视图类

def _render_view(plugin: dict, snapshot: dict) -> str:
    """视图渲染器: 输入只有只读 snapshot 字典(无 conn,无写账本旁路)。"""
    src = plugin["config"].get("source")
    title = plugin["config"].get("title", plugin["name"])
    if src == "task_status_counts":
        counts = {}
        for card_list in snapshot.values():
            if not isinstance(card_list, list):
                continue
            for card in card_list:
                if isinstance(card, dict) and card.get("task_status"):
                    counts[card["task_status"]] = counts.get(
                        card["task_status"], 0) + 1
        body = ", ".join(f"{k}×{v}" for k, v in sorted(counts.items())) or "(无在途)"
    elif src == "instance_roster":
        names = sorted({card["instance_name"]
                        for cl in snapshot.values() if isinstance(cl, list)
                        for card in cl
                        if isinstance(card, dict) and card.get("instance_name")})
        body = ", ".join(names) or "(无卡片实例)"
    else:
        raise ValueError(f"未知数据源 {src}")
    return f"[{title}] {body}"


def render_view_blocks(conn, snapshot: dict) -> list:
    """渲染全部启用视图类插件的展示块(21.3/21.4 运行期 fail-open)。

    单个插件渲染异常→退回默认块+审计行,不影响其余块与主流程。
    """
    blocks = []
    for p in list_plugins(conn):
        if p["type"] != "view" or not p.get("enabled"):
            continue
        try:
            blocks.append(_render_view(p, snapshot))
        except Exception as e:
            ops.audit(conn, "plugin_view_error",
                      {"name": p["name"], "error": str(e)})
            blocks.append(f"[{p['name']}] (插件渲染失败,已退回默认块)")
    return blocks
