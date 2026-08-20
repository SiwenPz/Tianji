"""权限控制框架(票 10,规格书 6.6): 决策入口唯一=总控,工人模型零参与。

permission_request 事件→账本待裁决(permission_rulings 表)→总控审批
(CLI=对话入口;驾驶舱审批按钮=渲染侧归票 03)→按壳机械执行:

- 钩子 allow 类(claude/codex/atomcode): 适配器应答时查账本裁决,
  有 allowed 裁决才放行,否则 deny——无头默认=天然拒绝(6.6)
- kimi 规则表: 裁决重生成静态规则文件,任务边界重启生效(热更不作依赖);
  无头全批缺口→静态 deny 规则+钩子拦截兜底
- cline 大类开关: 裁决落 autoApprove 配置;无头挂起→超时强杀归监控器(7.x)
"""

import json
from pathlib import Path

from . import auth, ops
from .db import now, tianji_home, tx

# 钩子 allow 类壳(6.6 放行三路之一)
HOOK_ALLOW_SHELLS = ("claude", "codex", "atomcode")


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


def _rewrite_rules_file(conn, worker_id: str):
    """kimi 规则表/cline 大类开关: 裁决落静态文件(任务边界重启生效,热更不作依赖)。"""
    rows = conn.execute(
        "SELECT tool, status FROM permission_rulings WHERE worker_id=?"
        " AND status IN ('allowed','denied') ORDER BY id",
        (worker_id,)).fetchall()
    allow = sorted({r["tool"] for r in rows if r["status"] == "allowed"})
    deny = sorted({r["tool"] for r in rows if r["status"] == "denied"})
    inst = conn.execute("SELECT shell FROM instances WHERE name=?",
                        (worker_id,)).fetchone()
    shell = inst["shell"] if inst else ""
    if shell == "cline":
        # cline 大类开关(autoApprove);无头挂起→超时强杀归监控器(7.x)
        path = _rules_path(conn, worker_id, "autoapprove.json")
        path.write_text(json.dumps(
            {"autoApprove": allow, "deny": deny,
             "note": "无头挂起由监控器超时强杀兜底(7.x)"},
            ensure_ascii=False, indent=1), encoding="utf-8")
        return str(path)
    # kimi 规则表(静态 allow/deny;无头全批缺口靠 deny 规则+钩子拦截兜底)
    path = _rules_path(conn, worker_id, "permission-rules.json")
    path.write_text(json.dumps(
        {"allow": allow, "deny": deny,
         "note": "任务边界重启生效(6.6,热更不作依赖)"},
        ensure_ascii=False, indent=1), encoding="utf-8")
    return str(path)


def decide(conn, ident, ruling_id: int, allow: bool, reason: str = "",
           request_id: str = None) -> dict:
    """总控审批(6.6 决策入口唯一): allow/deny→按壳机械执行+审计。"""
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
            if shell in ("kimi", "cline"):
                executed = _rewrite_rules_file(c, r["worker_id"])
            ops.audit(c, "permission_decide",
                      {"ruling_id": ruling_id, "worker_id": r["worker_id"],
                       "tool": r["tool"], "decision": status,
                       "shell": shell, "executed": executed,
                       "by": ident["worker_id"], "reason": reason})
            return {"ruling_id": ruling_id, "decision": status,
                    "executed": executed}
        return ops._with_idem(c, request_id, "permission_decide", _do)


def hook_response(conn, shell: str, worker_id: str, tool: str) -> dict:
    """钩子 allow 类壳的适配器应答(6.6): 查账本裁决,无 allowed 裁决即拒。

    无头默认=天然拒绝: claude -p 等无头形态无人可问,挂起即失败即拒。
    """
    r = conn.execute(
        "SELECT id FROM permission_rulings WHERE worker_id=? AND tool=?"
        " AND status='allowed' ORDER BY id DESC LIMIT 1",
        (worker_id, tool)).fetchone()
    allow = r is not None
    if shell == "claude" or shell == "atomcode":
        # Claude Code 兼容 PermissionRequest 钩子应答(atomcode 同 CC 三套)
        return {"hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow" if allow else "deny"}}}
    # codex 形态(按沙箱语义,从简)
    return {"decision": "allow" if allow else "deny"}
