"""权限控制框架(票 10,规格书 6.6): 决策入口唯一=总控,工人模型零参与。

permission_request 事件→账本待裁决(permission_rulings 表)→总控审批
(CLI=对话入口;驾驶舱审批按钮=渲染侧归票 03)→按壳条目 data-driven 执行:

- 钩子 allow 类(mechanism=hook_allow): 适配器应答时查账本裁决,
  有 allowed 裁决才放行,否则 deny——无头默认=天然拒绝(6.6)
- kimi 规则表(mechanism=rule_table): 裁决重生成静态规则文件,任务边界重启生效(热更不作依赖);
  无头全批缺口→静态 deny 规则+钩子拦截兜底
- cline 大类开关(mechanism=auto_approve): 裁决落 autoApprove 配置;无头挂起→超时强杀归监控器(7.x)
"""

import json
from pathlib import Path

from . import auth, ops
from .db import now, tianji_home, tx


def _permission_slot(conn, shell: str) -> dict:
    """从壳条目取 permission_slot,缺则读内置模板兜底(加载顺序: DB→模板)。

    空返回 = fail-loud 由 _rewrite_rules_file 的 mechanism 判断处理。
    """
    # 先读账本壳条目
    entry = _load_shell_entry(conn, shell)
    slot = entry.get("permission_slot")
    if slot:
        return slot
    # 兜底读内置模板(注册表优先,real code 路径里壳条目总有值)
    try:
        from .adapters.template import get_template
        tpl = get_template(shell)
        return tpl.permission_slot or {}
    except KeyError:
        return {}


def _load_shell_entry(conn, shell: str) -> dict:
    """读壳条目(6.6 data-driven): integration_shell:* 优先,shell:* 兼容。"""
    row = conn.execute("SELECT value FROM configs WHERE key=?",
                       (f"integration_shell:{shell}",)).fetchone()
    if row is not None:
        return json.loads(row["value"])
    row = conn.execute("SELECT value FROM configs WHERE key=?",
                       (f"shell:{shell}",)).fetchone()
    return json.loads(row["value"]) if row else {}


def record_request(conn, worker_id: str, session_id: str, tool: str,
                   payload: dict, dispatch_id=None) -> dict:
    """permission_request 事件归一化→待裁决(6.6)。同一待裁决去重。"""
    dup = conn.execute(
        "SELECT id FROM permission_rulings WHERE worker_id=? AND session_id=?"
        " AND tool=? AND request_payload=? AND status='pending'",
        (worker_id, session_id, tool,
         json.dumps(payload, ensure_ascii=False))).fetchone()
    if dup:
        return {"ruling_id": dup["id"], "duplicate": True}
    cur = conn.execute(
        "INSERT INTO permission_rulings (worker_id, dispatch_id, session_id,"
        " tool, request_payload, status, created_at) VALUES (?,?,?,?,?,'pending',?)",
        (worker_id, dispatch_id, session_id, tool,
         json.dumps(payload, ensure_ascii=False), now()))
    ops.audit(conn, "permission_request",
              {"ruling_id": cur.lastrowid, "worker_id": worker_id, "tool": tool})
    return {"ruling_id": cur.lastrowid, "pending": True}


def pending(conn) -> list:
    rows = conn.execute(
        "SELECT * FROM permission_rulings WHERE status='pending'"
        " ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def _rules_path(conn, worker_id: str, filename: str) -> Path:
    row = conn.execute("SELECT isolated_dir FROM instances WHERE name=?",
                       (worker_id,)).fetchone()
    base = Path(row["isolated_dir"]) if row and row["isolated_dir"] \
        else tianji_home() / "permission"
    base.mkdir(parents=True, exist_ok=True)
    return base / filename


def _rewrite_rules_file(conn, worker_id: str, shell: str):
    """按壳条目 permission_slot 写规则文件(附录 E.2 格式);任务边界重启生效(热更不作依赖)。"""
    rows = conn.execute(
        "SELECT tool, status FROM permission_rulings WHERE worker_id=?"
        " AND status IN ('allowed','denied') ORDER BY id",
        (worker_id,)).fetchall()
    allow = sorted({r["tool"] for r in rows if r["status"] == "allowed"})
    deny = sorted({r["tool"] for r in rows if r["status"] == "denied"})
    slot = _permission_slot(conn, shell)
    mechanism = slot.get("mechanism", "")
    rules_cfg = slot.get("rules") or {}
    if mechanism == "auto_approve":
        # cline 大类开关(autoApprove);无头挂起→超时强杀归监控器(7.x)
        filename = rules_cfg.get("file", "autoapprove.json")
        path = _rules_path(conn, worker_id, filename)
        path.write_text(json.dumps(
            {"autoApprove": allow, "deny": deny,
             "note": "无头挂起由监控器超时强杀兜底(7.x)"},
            ensure_ascii=False, indent=1), encoding="utf-8")
        return str(path)
    # 默认 = rule_table / hook_allow: 静态 allow/deny;无头全批缺口靠 deny 规则+钩子拦截兜底
    filename = rules_cfg.get("file", "permission-rules.json")
    path = _rules_path(conn, worker_id, filename)
    path.write_text(json.dumps(
        {"allow": allow, "deny": deny,
         "note": "任务边界重启生效(6.6,热更不作依赖)"},
        ensure_ascii=False, indent=1), encoding="utf-8")
    return str(path)


def decide(conn, ident, ruling_id: int, allow: bool, reason: str = "",
           request_id: str = None) -> dict:
    """总控审批(6.6 决策入口唯一): allow/deny→按壳条目 data-driven 执行+审计。"""
    if not auth.check_controller(conn, ident):
        raise PermissionError("权限裁决仅总控身份可执行(工人模型零参与,6.6)")
    with tx(conn) as c:
        def _do():
            r = c.execute("SELECT * FROM permission_rulings WHERE id=?",
                          (ruling_id,)).fetchone()
            if r is None:
                raise KeyError(f"裁决 {ruling_id} 不存在")
            if r["status"] != "pending":
                return {"ruling_id": ruling_id, "already": r["status"]}
            status = "allowed" if allow else "denied"
            c.execute(
                "UPDATE permission_rulings SET status=?, decided_by=?,"
                " reason=?, decided_at=? WHERE id=?",
                (status, ident["worker_id"], reason, now(), ruling_id))
            inst = c.execute("SELECT shell FROM instances WHERE name=?",
                             (r["worker_id"],)).fetchone()
            shell = inst["shell"] if inst else ""
            executed = "hook(钩子应答时读账本)"
            slot = _permission_slot(c, shell)
            # rule_table / auto_approve 机制需要写规则文件;hook_allow 不写
            if slot.get("mechanism") in ("rule_table", "auto_approve"):
                executed = _rewrite_rules_file(c, r["worker_id"], shell)
            ops.audit(c, "permission_decide",
                      {"ruling_id": ruling_id, "worker_id": r["worker_id"],
                       "tool": r["tool"], "decision": status,
                       "shell": shell, "executed": executed,
                       "by": ident["worker_id"], "reason": reason})
            return {"ruling_id": ruling_id, "decision": status,
                    "executed": executed}
        return ops._with_idem(c, request_id, "permission_decide", _do)


def hook_response(conn, shell: str, worker_id: str, tool: str) -> dict:
    """钩子允许类壳的适配器应答(6.6): 查账本裁决,无 allowed 裁决即拒。

    无头默认=天然拒绝: claude -p 等无头形态无人可问,挂起即失败即拒。

    放行格式由壳条目 permission_slot.hook_response_format 决定:
    - cc   → Claude Code 兼容 PermissionRequest 钩子应答格式
    - bare → codex 裸格式 {"decision": "allow"|"deny"}
    hook_action 决定 deny/cancel 行为。
    """
    r = conn.execute(
        "SELECT id FROM permission_rulings WHERE worker_id=? AND tool=?"
        " AND status='allowed' ORDER BY id DESC LIMIT 1",
        (worker_id, tool)).fetchone()
    allow = r is not None
    slot = _permission_slot(conn, shell)
    fmt = slot.get("hook_response_format", "cc")
    hook_action = slot.get("hook_action", "deny")
    decision = "allow" if allow else (
        "cancel" if hook_action == "cancel" else "deny")
    if fmt == "cc":
        return {"hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": decision}}}
    # bare 格式
    return {"decision": decision}
