"""驾驶舱 Web 交互页(票 03,规格书 15 章/19.2): FastAPI+uvicorn,原生 JS 单页零构建。

- 数据=账本同源只读渲染(15.1);端口只绑回环,固定默认号,冲突顺延+1(18.2,沿用票 15)
- 布局四段+右侧抽屉(15.1 E 变体);1.5s 轮询,输入框/抽屉焦点让路(15.2 不抢输入)
- 审批双入口(15.3): 页面按钮 + 总控对话自然语言(批准/驳回),账本单一真源双向同步
- 审批卡三类: 计划确认/最终确认/权限裁决(本票只渲染入口;权限放行机械执行归票 10)
- 写操作(审批/强制干预/条目增删)须注入总控身份(env TIANJI_WORKER_ID/TIANJI_SECRET),
  未注入则页面只读
- 总控真会话(/api/ctrl/*): 按协议分发(stream-json→ClaudeStreamBackend,acp→ACPBackend);
  懒持有——web 启动不拉起,首次 send 才 start;事件 1.5s 轮询,不做 SSE;
  配置变更(mtime)自动重建后端,旧进程先 close
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from tianji import ctrlprotocols
from tianji.ctrlprotocols import BaseBackend, get_backend_class
from . import cockpit, ctrlsession, integrations, ops, permission, pool as pool_mod, wizard
from .db import connect, injected_dir, tianji_home

app = FastAPI(title="天机驾驶舱", docs_url=None, redoc_url=None)


def _ident_or_none():
    wid = os.environ.get("TIANJI_WORKER_ID")
    secret = os.environ.get("TIANJI_SECRET")
    return {"worker_id": wid, "secret": secret} if wid and secret else None


def _require_controller(conn):
    ident = _ident_or_none()
    if not ident or not ops.auth.check_controller(conn, ident):
        return None
    return ident


# ---------------------------------------------------------------- 只读数据

def _approvals(conn) -> list:
    """待审批卡四类: 计划确认/最终确认/权限裁决/兜底跳转(4.4 HITL)。"""
    cards = []
    for t in conn.execute(
            "SELECT id, title FROM tasks WHERE status='awaiting_plan_confirm'"
            " ORDER BY id").fetchall():
        cards.append({"kind": "plan", "task_id": t["id"], "title": t["title"]})
    for t in conn.execute(
            "SELECT id, title FROM tasks WHERE status='awaiting_final_confirm'"
            " ORDER BY id").fetchall():
        cards.append({"kind": "final", "task_id": t["id"], "title": t["title"]})
    for r in permission.pending(conn):
        cards.append({"kind": "permission", "ruling_id": r["id"],
                      "worker": r["worker_id"], "tool": r["tool"]})
    for r in ops.pending_force(conn):
        cards.append({"kind": "force", "approval_id": r["id"],
                      "task_id": r["task_id"],
                      "from": r["from_state"], "to": r["to_state"],
                      "reason": r["reason"],
                      "initiator": r["initiator_id"]})
    return cards


def _escalations(conn, limit: int = 20) -> list:
    """升级 note 卡(15.7): 红色;任务进 reviewing 及以后=恢复转绿。"""
    rows = conn.execute(
        "SELECT seq, ts, payload FROM messages WHERE type='escalation'"
        " ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        p = json.loads(r["payload"])
        tid = p.get("task_id")
        recovered = False
        if tid:
            t = conn.execute("SELECT status FROM tasks WHERE id=?",
                             (tid,)).fetchone()
            recovered = bool(t and t["status"] in (
                "reviewing", "awaiting_final_confirm", "archived"))
        out.append({"seq": r["seq"], "ts": r["ts"],
                    "reason": p.get("reason", ""), "task_id": tid,
                    "recovered": recovered})
    return out


def _org(conn) -> dict:
    """角色编排表+壳/Key 条目(15.6 抽屉)。"""
    cfg = {r["key"]: r["value"] for r in
           conn.execute("SELECT key, value FROM configs").fetchall()}
    insts = [dict(r) for r in conn.execute(
        "SELECT name, shell, model, key_name, display_mode, is_active"
        " FROM instances ORDER BY created_at").fetchall()]
    shells = {k[6:]: json.loads(v) for k, v in cfg.items()
              if k.startswith("shell:")}
    keys = {k[4:]: json.loads(v) for k, v in cfg.items()
            if k.startswith("key:")}
    return {
        "controller": cfg.get("controller_worker_id", ""),
        "instances": insts,
        "shells": shells,
        "keys": keys,
    }


def _stream(conn, limit: int = 30) -> list:
    """总控窗格消息流(非事件消息,新的在前)。"""
    rows = conn.execute(
        "SELECT seq, ts, type, sender, payload FROM messages"
        " WHERE type!='event' ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
    return [{"seq": r["seq"], "ts": r["ts"], "type": r["type"],
             "sender": r["sender"],
             "reason": (json.loads(r["payload"]).get("reason")
                        or json.loads(r["payload"]).get("verdict") or "")}
            for r in rows]


@app.get("/api/state")
def api_state():
    conn = connect()
    try:
        snap = cockpit.snapshot(conn)
        cards = [c for k, cl in snap.items() if k != "pools" and isinstance(cl, list)
                 for c in cl if isinstance(c, dict)]
        ctrl = conn.execute(
            "SELECT shell FROM instances WHERE name='总控'").fetchone()
        workers = conn.execute(
            "SELECT COUNT(*) n FROM instances"
            " WHERE is_active=1 AND name!='总控'").fetchone()["n"]
        return {
            "snapshot": snap,
            "approvals": _approvals(conn),
            "escalations": _escalations(conn),
            "stream": _stream(conn),
            "org": _org(conn),
            "readonly": _require_controller(conn) is None,
            # 未配置(总控壳未定或零工人)时首页顶部放提示条链去 /setup
            "configured": bool(ctrl and ctrl["shell"] != "未配置" and workers),
            "counts": {
                "sessions": len({c["instance_name"] for c in cards}),
                "escalations": sum(1 for c in cards if c["has_escalation"]),
            },
        }
    finally:
        conn.close()


# 票 15 兼容端点
@app.get("/api/snapshot")
def api_snapshot():
    conn = connect()
    try:
        return cockpit.snapshot(conn)
    finally:
        conn.close()


@app.get("/api/instance/{name}")
def api_instance(name: str):
    """会话详情视图(15.4): 该实例的派单/会话/登记行/画像明细。"""
    conn = connect()
    try:
        inst = conn.execute("SELECT * FROM instances WHERE name=?",
                            (name,)).fetchone()
        if inst is None:
            return JSONResponse({"error": "实例不存在"}, status_code=404)
        dispatches = [dict(r) for r in conn.execute(
            "SELECT id, task_id, worker_role, axis, status, expect_min,"
            " created_at, updated_at FROM dispatches WHERE worker_id=?"
            " ORDER BY id DESC LIMIT 20", (name,)).fetchall()]
        registrations = [dict(r) for r in conn.execute(
            "SELECT id, dispatch_id, status, session_id, pid, abnormal,"
            " created_at, closed_at FROM instance_registrations"
            " WHERE instance_name=? ORDER BY id DESC LIMIT 10",
            (name,)).fetchall()]
        profile = conn.execute(
            "SELECT * FROM ability_profiles WHERE instance_name=?",
            (name,)).fetchone()
        return {"instance": dict(inst), "dispatches": dispatches,
                "registrations": registrations,
                "profile": dict(profile) if profile else None}
    finally:
        conn.close()


# ---------------------------------------------------------------- 写操作(须总控身份)

@app.post("/api/approve")
async def api_approve(req: Request):
    """审批按钮(15.3): plan/final/permission 三类,账本单一真源。"""
    body = await req.json()
    conn = connect()
    try:
        ident = _require_controller(conn)
        if ident is None:
            return JSONResponse({"error": "未注入总控身份,页面只读"},
                                status_code=403)
        kind = body.get("kind")
        decision = body.get("decision")
        rid = body.get("request_id") or f"web-{kind}-{ops.now()}"
        if kind == "permission":
            r = permission.decide(conn, ident, int(body["ruling_id"]),
                                  decision == "approve",
                                  reason="驾驶舱页面审批", request_id=rid)
            return r
        task_id = int(body["task_id"])
        if kind == "plan":
            to = "dispatched" if decision == "approve" else "discussing"
        else:
            to = "archived" if decision == "approve" else "discussing"
        return ops.task_transition(conn, ident, task_id, to,
                                   reason="驾驶舱页面审批", request_id=rid)
    finally:
        conn.close()


@app.post("/api/message")
async def api_message(req: Request):
    """总控对话窗格输入(15.3 自然语言入口): 批准/驳回 <任务号|权限号>。"""
    body = await req.json()
    text = (body.get("text") or "").strip()
    conn = connect()
    try:
        ident = _require_controller(conn)
        if ident is None:
            return JSONResponse({"error": "未注入总控身份,页面只读"},
                                status_code=403)
        m = re.match(r"^(批准|驳回)\s*(?:权限)?\s*#?(\d+)\s*$", text)
        if not m:
            return {"echo": text,
                    "note": "未识别;支持: 批准 16 / 驳回 16 / 批准权限 3 / 驳回权限 3"}
        decision = "approve" if m.group(1) == "批准" else "reject"
        num = int(m.group(2))
        if "权限" in text:
            return permission.decide(conn, ident, num,
                                     decision == "approve",
                                     reason="总控对话窗格",
                                     request_id=f"web-msg-{num}-{ops.now()}")
        t = conn.execute("SELECT status FROM tasks WHERE id=?",
                         (num,)).fetchone()
        if t is None:
            return {"error": f"任务 {num} 不存在"}
        if t["status"] == "awaiting_plan_confirm":
            to = "dispatched" if decision == "approve" else "discussing"
        elif t["status"] == "awaiting_final_confirm":
            to = "archived" if decision == "approve" else "discussing"
        else:
            return {"error": f"任务 {num} 当前 {t['status']},无待审批项"}
        return ops.task_transition(conn, ident, num, to,
                                   reason="总控对话窗格审批",
                                   request_id=f"web-msg-{num}-{ops.now()}")
    finally:
        conn.close()


@app.post("/api/entry")
async def api_entry(req: Request):
    """抽屉条目管理(15.6): 壳/Key 条目增;新增标"待测试"(13.1 先测能力再入画像)。"""
    body = await req.json()
    conn = connect()
    try:
        ident = _require_controller(conn)
        if ident is None:
            return JSONResponse({"error": "未注入总控身份,页面只读"},
                                status_code=403)
        kind, name = body.get("kind"), body.get("name")
        if kind not in ("shell", "key") or not name:
            return JSONResponse({"error": "kind 须为 shell|key 且 name 必填"},
                                status_code=400)
        data = body.get("data") or {}
        data["tested"] = False  # 待测试
        return ops.config_set(conn, ident, f"{kind}:{name}",
                              json.dumps(data, ensure_ascii=False),
                              request_id=f"web-entry-{kind}-{name}-{ops.now()}")
    finally:
        conn.close()


@app.post("/api/entry/delete")
async def api_entry_delete(req: Request):
    body = await req.json()
    conn = connect()
    try:
        ident = _require_controller(conn)
        if ident is None:
            return JSONResponse({"error": "未注入总控身份,页面只读"},
                                status_code=403)
        return ops.config_delete(conn, ident,
                                 f"{body.get('kind')}:{body.get('name')}",
                                 request_id=f"web-del-{ops.now()}")
    finally:
        conn.close()


# ---- 号池 API (票55/59) -------------------------------------------

@app.get("/api/pool/list")
async def api_pool_list(req: Request):
    """列出全部池(只读,含成员摘要)。"""
    conn = connect()
    try:
        pools = pool_mod.pool_list(conn)
        return {"pools": pools}
    finally:
        conn.close()


@app.get("/api/pool/detail")
async def api_pool_detail(req: Request):
    """池详情(含成员 credential 详情)。"""
    name = (req.query_params.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name 必填"}, status_code=400)
    conn = connect()
    try:
        detail = pool_mod.pool_status(conn, name)
        members = []
        for m_name in detail.get("members", []):
            cred = integrations._config(conn, f"credential:{m_name}")
            if cred:
                prov_name = cred.get("provider", "")
                prov = integrations._config(conn, f"integration_provider:{prov_name}") or {}
                members.append({
                    "name": m_name,
                    "provider": prov_name,
                    "base_url": cred.get("base_url", prov.get("base_url", "")),
                    "protocol": cred.get("protocol", ""),
                    "models": cred.get("models", prov.get("models", [])),
                    "note": cred.get("note", ""),
                })
            else:
                members.append({"name": m_name, "not_found": True})
        detail["members_detail"] = members
        return detail
    finally:
        conn.close()


@app.post("/api/pool/create")
async def api_pool_create(req: Request):
    """建池(总控身份;幂等)。"""
    body = await req.json()
    name = (body.get("name") or "").strip()
    members = body.get("members") or []
    if not name or ":" in name:
        return JSONResponse({"error": "池名须非空且不含冒号"}, status_code=400)
    ident = _require_controller(connect())
    if ident is None:
        return JSONResponse({"error": "未注入总控身份,页面只读"}, status_code=403)
    conn = connect()
    try:
        rid = body.get("request_id") or f"web-pool-create-{ops.now()}"
        result = pool_mod.pool_create(conn, ident, name, members=members, request_id=rid)
        return result
    finally:
        conn.close()


@app.post("/api/pool/add-member")
async def api_pool_add_member(req: Request):
    """归 key 入池(总控身份;幂等)。"""
    body = await req.json()
    pool_name = (body.get("pool") or "").strip()
    credential = (body.get("credential") or "").strip()
    if not pool_name or not credential:
        return JSONResponse({"error": "pool 和 credential 必填"}, status_code=400)
    ident = _require_controller(connect())
    if ident is None:
        return JSONResponse({"error": "未注入总控身份,页面只读"}, status_code=403)
    conn = connect()
    try:
        rid = body.get("request_id") or f"web-pool-add-{ops.now()}"
        result = pool_mod.pool_add_member(conn, ident, pool_name, credential, request_id=rid)
        return result
    finally:
        conn.close()


@app.post("/api/pool/remove-member")
async def api_pool_remove_member(req: Request):
    """摘除成员(总控身份;幂等)。"""
    body = await req.json()
    pool_name = (body.get("pool") or "").strip()
    credential = (body.get("credential") or "").strip()
    if not pool_name or not credential:
        return JSONResponse({"error": "pool 和 credential 必填"}, status_code=400)
    ident = _require_controller(connect())
    if ident is None:
        return JSONResponse({"error": "未注入总控身份,页面只读"}, status_code=403)
    conn = connect()
    try:
        rid = body.get("request_id") or f"web-pool-rm-{ops.now()}"
        result = pool_mod.pool_remove_member(conn, ident, pool_name, credential, request_id=rid)
        return result
    finally:
        conn.close()


@app.post("/api/pool/rotate-token")
async def api_pool_rotate_token(req: Request):
    """令牌轮换(总控身份;幂等)。token 明文仅此一次返回。"""
    body = await req.json()
    pool_name = (body.get("name") or "").strip()
    if not pool_name:
        return JSONResponse({"error": "name 必填"}, status_code=400)
    ident = _require_controller(connect())
    if ident is None:
        return JSONResponse({"error": "未注入总控身份,页面只读"}, status_code=403)
    conn = connect()
    try:
        rid = body.get("request_id") or f"web-pool-rotate-{ops.now()}"
        result = pool_mod.pool_rotate_token(conn, ident, pool_name, request_id=rid)
        return result
    finally:
        conn.close()


@app.delete("/api/pool")
async def api_pool_delete(req: Request):
    """删池(总控身份;幂等)。"""
    body = await req.json()
    pool_name = (body.get("name") or "").strip()
    if not pool_name:
        return JSONResponse({"error": "name 必填"}, status_code=400)
    ident = _require_controller(connect())
    if ident is None:
        return JSONResponse({"error": "未注入总控身份,页面只读"}, status_code=403)
    conn = connect()
    try:
        rid = body.get("request_id") or f"web-pool-del-{ops.now()}"
        result = pool_mod.pool_delete(conn, ident, pool_name, request_id=rid)
        return result
    finally:
        conn.close()


@app.get("/api/integrations")
def api_integrations():
    """集成注册表快照与旧条目迁移状态(13.8/票42;票59:含池数据;只读,不含密钥本体)。"""
    conn = connect()
    try:
        state = integrations.registry_state(conn)
        pools = pool_mod.pool_list(conn)
        state["pools"] = pools
        return state
    finally:
        conn.close()


@app.post("/api/integrations/migrate")
async def api_integrations_migrate(req: Request):
    """注册表初始化+旧 shell:/key: 条目显式迁移(13.8;总控身份;幂等)。"""
    body = await req.json()
    conn = connect()
    try:
        ident = _require_controller(conn)
        if ident is None:
            return JSONResponse({"error": "未注入总控身份,页面只读"},
                                status_code=403)
        rid = body.get("request_id") or f"web-migrate-{ops.now()}"
        ensured = integrations.ensure_builtin_registry(conn, ident,
                                                       request_id=rid)
        migrated = integrations.migrate_legacy_entries(conn, ident,
                                                       request_id=f"{rid}-m")
        return {"ensured": ensured, "migrated": migrated}
    finally:
        conn.close()


@app.post("/api/force")
async def api_force(req: Request):
    """强制干预(15.4): 强制终止/改派(task force)/取消派单(dispatch cancel)。"""
    body = await req.json()
    conn = connect()
    try:
        ident = _require_controller(conn)
        if ident is None:
            return JSONResponse({"error": "未注入总控身份,页面只读"},
                                status_code=403)
        if body.get("dispatch_id"):
            return ops.dispatch_cancel(
                conn, ident, int(body["dispatch_id"]),
                body.get("reason", "驾驶舱强制干预"),
                request_id=f"web-cancel-{ops.now()}")
        return ops.task_force(
            conn, ident, int(body["task_id"]), body["to_state"],
            body.get("reason", "驾驶舱强制干预"),
            request_id=f"web-force-{ops.now()}")
    finally:
        conn.close()


@app.post("/api/force/approve")
async def api_force_approve(req: Request):
    """兜底跳转人审(HITL): 用户显式批准/驳回,总控身份自批被拒。"""
    body = await req.json()
    conn = connect()
    try:
        ident = _ident_or_none()
        if ident and ops.auth.check_controller(conn, ident):
            return JSONResponse(
                {"error": "审批必须由用户(人)操作,总控身份不可自批(HITL)"},
                status_code=403)
        aid = int(body.get("approval_id"))
        decision = body.get("decision")
        if decision == "approve":
            return ops.force_approve(conn, "cockpit-user", aid)
        return ops.force_reject(conn, "cockpit-user", aid)
    finally:
        conn.close()


# ---------------------------------------------------------------- 总控真会话

# 懒持有: 不在 web 启动时拉起,首次 send 才 start(进程不烧 token,但也别没事拉起)
_ctrl_session: BaseBackend | None = None
_ctrl_cfg_mtime: float = 0.0


def _read_ctrl_secret() -> str:
    """读总控身份 secret(从 ctrl-secret.txt,延迟解析路径)。"""
    p = injected_dir() / "ctrl-secret.txt"
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    return ""


def _ctrl() -> BaseBackend:
    """按 settings-controller.json 的 ctrl_session.protocol 分发后端。

    缓存命中(含测试 monkeypatch) → 直返。
    仅在 BaseBackend 缓存上做 mtime 变更检测: 文件改了 → close 旧进程→重建。
    """
    global _ctrl_session, _ctrl_cfg_mtime
    if _ctrl_session is not None:
        # 测试可 monkeypatch ControllerSession 或 BaseBackend 直接;直返。
        # 仅 BaseBackend 实例才做 mtime 变更检测(热生效)。
        if not isinstance(_ctrl_session, BaseBackend):
            return _ctrl_session
        settings = injected_dir() / "settings-controller.json"
        mtime = (settings.stat().st_mtime if settings.exists() else 0.0)
        if mtime == _ctrl_cfg_mtime:
            return _ctrl_session
        _ctrl_session.close()
    settings = injected_dir() / "settings-controller.json"
    mtime = settings.stat().st_mtime if settings.exists() else 0.0
    _ctrl_session = BaseBackend.from_config(injected_dir(), settings)
    # 票 40: 总控会话 cwd=默认项目目录(configs default_project_dir),
    # 有值则子进程在用户工作目录里启动(改项目文件不撞权限墙);无则账本根=旧行为
    if isinstance(_ctrl_session, BaseBackend):
        conn = connect()
        try:
            row = conn.execute(
                "SELECT value FROM configs WHERE key='default_project_dir'"
            ).fetchone()
            if row and row["value"]:
                _ctrl_session.cwd = str(Path(row["value"]).resolve())
        finally:
            conn.close()
    _ctrl_cfg_mtime = mtime
    return _ctrl_session


@app.post("/api/ctrl/send")
async def api_ctrl_send(req: Request):
    """发话给总控: admission-only,立即回 accepted;事件走 /api/ctrl/events 轮询。"""
    body = await req.json()
    conn = connect()
    try:
        if _require_controller(conn) is None:
            return JSONResponse({"error": "未注入总控身份,页面只读"},
                                status_code=403)
    finally:
        conn.close()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "空消息"}, status_code=400)
    _ctrl().send(text)
    return {"accepted": True}


@app.get("/api/ctrl/events")
def api_ctrl_events(after: int = 0):
    """事件游标拉增量(1.5s 轮询口径);页面加载先 after=0 拉全量接上。"""
    s = _ctrl_session
    if s is None:
        return {"events": [], "next": 0}
    ev, nxt = s.get_events(max(after, 0))
    return {"events": ev, "next": nxt}


@app.get("/api/ctrl/status")
def api_ctrl_status():
    s = _ctrl_session
    return {"alive": bool(s and s.is_alive()),
            "session_id": s.session_id if s else None}


# ---------------------------------------------------------------- 首次配置(web 点选)

def _setup_state(conn) -> dict:
    """配置页一屏数据: 总控牌/机械扫描/编制(一行一个实例)/key 条目/就绪标记。"""
    ctrl = conn.execute(
        "SELECT shell, model, key_name FROM instances WHERE name='总控'").fetchone()
    insts = [dict(r) for r in conn.execute(
        "SELECT i.name, i.shell, i.model, i.key_name, p.notes"
        " FROM instances i"
        " LEFT JOIN ability_profiles p ON p.instance_name=i.name"
        " WHERE i.is_active=1 AND i.name!='总控'"
        " ORDER BY i.created_at").fetchall()]
    keys = [r["key"][4:] for r in conn.execute(
        "SELECT key FROM configs WHERE key LIKE 'key:%'").fetchall()]
    # 集成供应商预设(票33: 配置页"选供应商→填key→获取模型→选模型"数据源)
    providers = []
    for row in conn.execute(
            "SELECT key, value FROM configs"
            " WHERE key LIKE 'integration_provider:%' ORDER BY key").fetchall():
        v = json.loads(row["value"])
        providers.append({
            "name": row["key"][len("integration_provider:"):],
            "display": v.get("display") or "",
            "base_url": v.get("base_url", ""),
            "protocol": v.get("protocol", ""),
            "category": v.get("category") or
                        ("内置" if v.get("builtin") else "自定义"),
            "builtin": bool(v.get("builtin")),
            "models": [m.get("id") for m in v.get("models", [])
                       if isinstance(m, dict) and m.get("id")]})
    scanned = wizard.scan_shells()
    # 标记哪些壳可作总控(SHELL_ENTRY_DEFAULTS 条目含 ctrl_session 块)
    ctrl_session_shells = {
        name for name, entry in wizard.SHELL_ENTRY_DEFAULTS.items()
        if "ctrl_session" in entry}
    for s in scanned:
        s["ctrl_session"] = s["name"] in ctrl_session_shells
        s["protocols"] = wizard.SHELL_ENTRY_DEFAULTS.get(
            s["name"], {}).get("protocols", [])
    dpd = conn.execute(
        "SELECT value FROM configs WHERE key='default_project_dir'"
    ).fetchone()
    return {
        "controller": ({"shell": ctrl["shell"], "model": ctrl["model"],
                        "source": "key" if ctrl["key_name"] else "builtin"}
                       if ctrl else None),
        "scanned": scanned,
        "providers": providers,
        "instances": insts,
        "keys": keys,
        "default_project_dir": dpd["value"] if dpd else "",
        "configured": bool(ctrl and ctrl["shell"] != "未配置" and insts),
        "pools": pool_mod.pool_list(conn),
    }


def _assign_key_names(conn, cards: list):
    """外接 key 自动起名: 同壳同地址复用已有条目,否则 keyN 顺延(避开已占用)。"""
    for card in cards:
        if card.get("source") != "key" or card.get("key_name"):
            continue
        row = conn.execute(
            "SELECT key, value FROM configs WHERE key LIKE 'key:%'").fetchall()
        for r in row:
            v = json.loads(r["value"])
            if v.get("base_url") == card["base_url"]:
                card["key_name"] = r["key"][4:]
                break
        else:
            n = 0
            taken = {r["key"][4:] for r in row}
            while True:
                n += 1
                if f"key{n}" not in taken:
                    card["key_name"] = f"key{n}"
                    break


def _next_instance_name(conn, role: str) -> str:
    """实例名=角色+序号(审核1/审核2/实施1…),避开已存在的名字。"""
    n = 0
    while True:
        n += 1
        name = f"{role}{n}"
        if conn.execute("SELECT 1 FROM instances WHERE name=?",
                        (name,)).fetchone() is None:
            return name


def _worker_key_cards(cards: list):
    """取真正走注册表装配的工人 key 牌;总控牌和内置牌不参与配对。"""
    return [card for card in cards
            if card.get("source") == "key"
            and not card.get("is_controller_card")]


def _validate_worker_cards(conn, cards: list):
    """提交前统一拦截非法组合;不通过就不写任何 key 或实例。"""
    checked = []
    for card in _worker_key_cards(cards):
        shell_name = card["shell"]
        defaults = wizard.SHELL_ENTRY_DEFAULTS.get(shell_name)
        checked.append(integrations.validate_worker_card(
            conn, shell_name, card["model"],
            provider=card.get("provider", ""),
            base_url=card.get("base_url", ""),
            protocol=card.get("protocol", ""),
            key_name=card.get("key_name", ""),
            shell_protocols=defaults.get("protocols") if defaults else None))
    return checked


def _request_digest(*parts) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:12]


def _register_card_entries(conn, ident, home_p: Path, card: dict,
                           checked: dict):
    """落地后显式增量登记;旧 key:* 只保留为兼容迁移源。"""
    integrations.ensure_builtin_registry(
        conn, ident,
        request_id="web-card-builtin-" + _request_digest(
            card.get("base_url", ""), checked["provider"]))
    pname = checked["provider"]
    pkey = f"integration_provider:{pname}"
    if conn.execute("SELECT 1 FROM configs WHERE key=?", (pkey,)
                    ).fetchone() is None:
        integrations.register_custom_provider(
            conn, ident, pname, checked["base_url"], checked["protocol"],
            request_id="web-card-provider-"
                       + _request_digest(pname, checked["base_url"],
                                         checked["protocol"]))
    key_name = card["key_name"]
    cred_key = f"credential:{key_name}"
    if conn.execute("SELECT 1 FROM configs WHERE key=?", (cred_key,)
                    ).fetchone() is None:
        integrations.register_credential(
            conn, ident, key_name, pname,
            key_ref=str(injected_dir() / f"{key_name}.key"),
            request_id="web-card-credential-"
                       + _request_digest(key_name, pname))


@app.get("/api/setup/state")
def api_setup_state():
    conn = connect()
    try:
        # 出厂供应商/协议预设懒入库(票33: 配置页下拉的数据源;幂等不覆盖)
        ident = _require_controller(conn)
        if ident is not None:
            integrations.ensure_builtin_registry(
                conn, ident, request_id=f"web-builtin-{ops.now()}")
        return _setup_state(conn)
    finally:
        conn.close()


@app.post("/api/integrations/discover-models")
async def api_integrations_discover(req: Request):
    """模型发现(票33): 已登记供应商或自定义 base_url+协议保底探测;
    有总控身份且命中已登记供应商时写条目缓存,否则只探测不落账。"""
    body = await req.json()
    conn = connect()
    try:
        ident = _require_controller(conn)
        return integrations.discover_models(
            conn, ident,
            provider=(body.get("provider") or "").strip(),
            base_url=(body.get("base_url") or "").strip(),
            protocol=(body.get("protocol") or "").strip(),
            credential=(body.get("credential") or "").strip(),
            key_value=(body.get("key") or "").strip())
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    finally:
        conn.close()


@app.post("/api/setup/controller")
async def api_setup_controller(req: Request):
    """选定总控助手(含可选外接 key): 总控牌就地更新(票 28 通道,不重建);
    壳须有 ctrl_session 条目才可作总控(纯数据驱动,插件化)。"""
    body = await req.json()
    shell = (body.get("shell") or "").strip()
    if not shell:
        return JSONResponse({"error": "shell 必填"}, status_code=400)
    #  defensively: 壳条目须有 ctrl_session 块才可作总控
    entry = wizard.SHELL_ENTRY_DEFAULTS.get(shell, {})
    if "ctrl_session" not in entry:
        return JSONResponse({"error":
            f"壳 {shell} 暂不支持作总控(缺 ctrl_session 配置),请选有模板的壳"
            f"(claude / kimi 等)。"}, status_code=400)
    conn = connect()
    try:
        ident = _require_controller(conn)
        if ident is None:
            return JSONResponse({"error": "未注入总控身份,页面只读"},
                                status_code=403)
        home_p = tianji_home()
        card = {"shell": shell,
                "model": (body.get("model") or "").strip() or "未配置",
                "source": body.get("source") or "builtin",
                "is_controller_card": True}
        if card["source"] == "key":
            if not body.get("key") or not body.get("base_url"):
                return JSONResponse({"error": "外接 key 要给 key 和接口地址"},
                                    status_code=400)
            card.update(key_value=body["key"], base_url=body["base_url"].strip(),
                        key_name=(body.get("key_name") or "").strip() or "主key")
        res = wizard.land_cards(conn, home_p, ident, [card])
        secret = (injected_dir() / "ctrl-secret.txt").read_text(
            encoding="utf-8").strip()
        # provider 信息统一构造;_write_controller_settings 读壳条目 provider_env
        # 决定凭据映射位置(settings_env / process_env)
        provider = None
        if card["source"] == "key":
            provider = {"key_value": card["key_value"],
                        "base_url": card["base_url"],
                        "model": card["model"]}
        settings = wizard._write_controller_settings(
            home_p, str(home_p), shell, secret,
            provider=provider, ready=True)
        return {"landed": res["landed"], "settings": settings,
                "state": _setup_state(conn)}
    finally:
        conn.close()


@app.post("/api/setup/project-dir")
async def api_setup_project_dir(req: Request):
    """默认工作目录(18.1,票 39): 任务未指定项目目录时的回退来源;
    写操作要总控身份(与配置页其他写操作一致),留空=清除。"""
    body = await req.json()
    path = (body.get("path") or "").strip()
    conn = connect()
    try:
        ident = _require_controller(conn)
        if ident is None:
            return JSONResponse({"error": "未注入总控身份,页面只读"},
                                status_code=403)
        if path:
            ops.config_set(conn, ident, "default_project_dir", path,
                           request_id=f"web-project-dir-{path}")
        else:
            ops.config_delete(conn, ident, "default_project_dir",
                              request_id="web-project-dir-clear")
        return {"default_project_dir": path, "state": _setup_state(conn)}
    finally:
        conn.close()


@app.post("/api/setup/probe")
async def api_setup_probe(req: Request):
    """拿 key+地址探测可用模型清单(13.1;配置全程唯一联网的一步,只读不落账,
    失败回 null 由用户手填)。"""
    body = await req.json()
    base_url = (body.get("base_url") or "").strip()
    key = (body.get("key") or "").strip()
    if not base_url or not key:
        return JSONResponse({"error": "base_url 和 key 都要给"}, status_code=400)
    return {"models": wizard.probe_models(base_url, key)}


@app.post("/api/setup/land")
async def api_setup_land(req: Request):
    """落地编制: key 文件/条目+总控牌走 land_cards;工人实例逐个注册
    (角色+序号命名,个数>1 顺延;同牌面已注册的算已落地只补差额=重跑幂等)。"""
    body = await req.json()
    cards = body.get("cards") or []
    if not cards:
        return JSONResponse({"error": "cards 不能为空"}, status_code=400)
    conn = connect()
    try:
        ident = _require_controller(conn)
        if ident is None:
            return JSONResponse({"error": "未注入总控身份,页面只读"},
                                status_code=403)
        home_p = tianji_home()
        for card in cards:
            if card.get("role") == "总控":
                card["is_controller_card"] = True
        _assign_key_names(conn, cards)
        # 命名后再全量校验: 同一 URL 复用旧 key 时也要检查 CodingPlan 绑定。
        try:
            checks = _validate_worker_cards(conn, cards)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        res = wizard.land_cards(conn, home_p, ident, cards)
        for card, checked in zip(_worker_key_cards(cards), checks):
            _register_card_entries(conn, ident, home_p, card, checked)
        registered = []
        for card in cards:
            if card.get("is_controller_card"):
                continue
            role = card.get("role") or "实施"
            count = max(1, int(card.get("count") or 1))
            # 同壳同模型同 key 同角色的已注册实例=已落地,只补差额
            have = conn.execute(
                "SELECT COUNT(*) n FROM instances i"
                " JOIN ability_profiles p ON p.instance_name=i.name"
                " WHERE i.is_active=1 AND i.shell=? AND i.model=?"
                " AND i.key_name=? AND p.notes LIKE ?",
                (card["shell"], card["model"], card.get("key_name", ""),
                 f"%拟定角色: {role}%")).fetchone()["n"]
            for _ in range(max(0, count - have)):
                name = _next_instance_name(conn, role)
                iso = home_p / "instances" / f"{name}-{card['shell']}"
                r = wizard.add_instance(
                    conn, ident, name, card["shell"], card["model"],
                    key_name=card.get("key_name", ""),
                    isolated_dir=str(iso),
                    skip_test=True, confirm=True, role_note=role,
                    request_id=f"web-land-{name}")
                registered.append(r["name"])
            # 票59: 落地后归池
            pool_name = (card.get("pool") or "").strip()
            if pool_name and r.get("key_name"):
                try:
                    pool_mod.pool_add_member(conn, ident, pool_name,
                        r["key_name"],
                        request_id=f"web-land-pool-{name}")
                except (KeyError, ValueError):
                    pass  # Non-fatal: pool assignment failed, instance still created
        return {**res, "registered": registered, "state": _setup_state(conn)}
    finally:
        conn.close()


# ---------------------------------------------------------------- 页面(零构建单页)

@app.get("/", response_class=HTMLResponse)
def index():
    return _PAGE


@app.get("/setup", response_class=HTMLResponse)
def setup_page():
    """首次配置页(变体 B 一屏全览): 选总控/配一张牌/编制总览实时生长。"""
    return _SETUP_PAGE


_PAGE = r"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>天机驾驶舱</title>
<style>
*{box-sizing:border-box}
html,body{height:100%;margin:0;font-family:system-ui,sans-serif;font-size:13px}
body{background:#0b0f1a;color:#dde3ee;overflow:hidden}
button{cursor:pointer;border:0;font-family:inherit;background:#26355c;color:#dde3ee;border-radius:6px;padding:5px 12px}
button:disabled{opacity:.45;cursor:not-allowed}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-thumb{background:#263352;border-radius:4px}
::-webkit-scrollbar-track{background:transparent}
/* ===== 共享零件(原型 A) ===== */
.avatar{width:34px;height:34px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;color:#fff;font-weight:600;flex:none}
.av-claude{background:#c8845a}.av-kimi{background:#5a8fc8}.av-codex{background:#6ab187}.av-cline{background:#9a6ac8}.av-atomcode{background:#c85a7a}
.av-other{background:#5a6480}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block}
.dot.working{background:#f5a623}.dot.error{background:#e5534b}.dot.idle{background:#9aa4b8}.dot.done{background:#4caf7d}
.time{color:#98a2b8;font-size:11px;flex:none}
.badge{background:#e5534b;color:#fff;border-radius:9px;font-size:11px;padding:1px 7px;flex:none}
/* ===== 三栏骨架(原型 A): 左栏 280px / 中栏弹性 / 右栏 peek clamp ===== */
#root{display:flex;height:100%}
.rail{width:280px;background:#0e1424;border-right:1px solid #1e2740;display:flex;flex-direction:column;flex:none}
.rail-list{flex:1;min-height:0;overflow-y:auto;padding-bottom:8px}
.chat{flex:1;display:flex;flex-direction:column;min-width:0;background:radial-gradient(900px 400px at 60% -10%,#14203a 0%,#0b0f1a 60%)}
.peek{width:clamp(420px,32vw,560px);background:#0e1424;border-left:1px solid #1e2740;flex:none;overflow-y:auto;transition:margin .2s}
.peek.closed{margin-right:calc(-1 * clamp(420px,32vw,560px))}
/* 左栏实例行: 卡片化,行间有缝有底,hover 变亮,选中亮边 */
.rail-item{display:flex;gap:10px;padding:10px 12px;cursor:pointer;border-radius:10px;margin:0 8px 6px;background:#131b30;border:1px solid #1c2642;transition:background .15s,border-color .15s}
.rail-item:hover{background:#1a2440;border-color:#26324e}
.rail-item.sel{background:#1c2947;border-color:#3d5a8e;box-shadow:inset 2px 0 0 #5a8fc8}
.rail h4{margin:16px 14px 6px;color:#7e93bd;font-size:11px;letter-spacing:1.5px;font-weight:600}
/* 中栏对话面 */
.chat-head{padding:10px 16px;border-bottom:1px solid #1e2740;display:flex;gap:10px;align-items:center;background:rgba(16,22,42,.8)}
.stream{flex:1;min-height:0;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}
.bubble-a{background:#1a2338;border:1px solid #22304e;border-radius:4px 14px 14px 14px;padding:9px 13px;max-width:72%;line-height:1.6;white-space:pre-wrap}
.bubble-u{background:#2b4a7e;border-radius:14px 4px 14px 14px;padding:9px 13px;max-width:72%;margin-left:auto;line-height:1.6;white-space:pre-wrap}
.sysline{color:#7e93bd;font-size:12px}
.thinking{color:#f5a623;font-size:12px}
.approve-card{background:#22301c;border:1px solid #3d5a2e;border-radius:12px;padding:10px 14px;margin-bottom:8px}
.approve-card.force{background:#2e2218;border-color:#5a3d2e}
.esc-card{background:#33181f;border:1px solid #6e2b36;border-radius:12px;padding:10px 14px;margin-bottom:8px}
.esc-card .ack{text-decoration:underline;cursor:pointer}
.unread{font-weight:bold}
.inputbar{display:flex;gap:8px;padding:12px 16px;flex:none}
.inputbar input{flex:1;background:#10162a;border:1px solid #26324e;border-radius:18px;padding:10px 16px;color:#dde3ee;outline:none}
.inputbar input:focus{border-color:#3d5a8e;box-shadow:0 0 0 2px rgba(61,90,142,.25)}
/* 按钮分档: 批准=实心绿渐变,驳回=实心红渐变,发送=蓝渐变,次级=ghost */
.btn-ok{background:linear-gradient(90deg,#2e7d32,#43a047);color:#fff;border-radius:8px;padding:6px 16px;font-weight:600}
.btn-ok:hover{filter:brightness(1.12)}
.btn-no{background:linear-gradient(90deg,#c62828,#e5534b);color:#fff;border-radius:8px;padding:6px 16px;font-weight:600}
.btn-no:hover{filter:brightness(1.12)}
.btn-ghost{background:#131b30;border:1px solid #26324e;color:#c8d2e8;border-radius:8px;padding:6px 14px;display:inline-flex;gap:6px;align-items:center}
.btn-ghost:hover{border-color:#3d5a8e;background:#1a2440;color:#fff}
.btn-ghost .ic{font-size:14px;line-height:1}
.btn-send{background:linear-gradient(90deg,#2b4a7e,#3d6ab8);color:#fff;border-radius:18px;padding:6px 18px;font-weight:600}
.btn-send:hover{filter:brightness(1.12)}
/* 标题呼吸 */
@keyframes brandGlow{0%,100%{filter:brightness(1)}50%{filter:brightness(1.4)}}
.brand{font-size:16px;letter-spacing:2px;background:linear-gradient(90deg,#8fd0ff,#7ab648);-webkit-background-clip:text;background-clip:text;color:transparent;animation:brandGlow 3.2s infinite ease-in-out}
/* 右栏 peek 详情: 渐变头像环+行式分隔,不用重边框表格 */
.peek-head{padding:18px 16px;border-bottom:1px solid #1e2740;display:flex;gap:12px;align-items:center;background:linear-gradient(180deg,#14203a 0%,#0e1424 100%)}
.peek-ring{border-radius:50%;padding:2px;background:linear-gradient(135deg,#5a8fc8,#4caf7d)}
.peek-rows{padding:6px 16px}
.peek-row{display:flex;justify-content:space-between;gap:10px;padding:9px 0;border-bottom:1px solid rgba(30,39,64,.6)}
.peek-row span:first-child{color:#7e93bd;flex:none}
.peek-acts{padding:14px 16px;display:flex;gap:8px}
.peek h4{margin:16px 16px 6px;color:#7e93bd;font-size:11px;letter-spacing:1.5px;font-weight:600}
</style></head><body>
<div id="root">
 <div class="rail">
  <div style="padding:14px 16px 6px"><b class="brand">天机驾驶舱</b>
  <div class="sysline" id="cstat" style="margin-top:4px"></div></div>
  <div class="rail-list" id="rail"></div>
 </div>
 <div class="chat">
  <div class="chat-head"><span id="ctrl-av"></span>
  <div><b>总控</b> <span id="ctrl-dot"></span><div class="sysline" id="ctrl-sub"></div></div>
  <span id="ro" style="color:#ffb454"></span><span style="flex:1"></span>
  <button class="btn-ghost" onclick="location.href='/setup'"><span class="ic">⚙️</span>配置</button>
  <button class="btn-ghost" onclick="openOrg()"><span class="ic">🗂️</span>角色/条目</button></div>
  <div id="cfgbar"></div>
  <div class="stream" id="stream"><div id="flowcards"></div></div>
  <div class="inputbar"><input id="msg" placeholder="跟总控说话 …(批准 16 / 驳回 16 也可以直接敲)">
  <button class="btn-send" onclick="sendMsg()">发送</button></div>
 </div>
 <div class="peek closed" id="peek"></div>
</div>
<script>
/* ===== 状态 ===== */
const GROUPS=[["attention","待处理"],["working","进行中"],["idle","空闲"],["done","已结算"]];
let lastState=null,peekOf=null,peekDetail=null,cockpitReadonly=true;
let SHELLS={},ORGINST=[],snapCards={},orgRoles={};
const dismissedEsc=new Set();   // 升级卡"知道了"本地确认(重渲染不再冒出)
function esc(s){return String(s??"").replace(/&/g,"&amp;").replace(/</g,"&lt;")}
async function j(u,opt){const r=await fetch(u,opt);return r.json()}
function shellOf(name){return SHELLS[name]||"other"}
function avat(name,shell,sz){
 return `<span class="avatar av-${esc(shell)}" style="${sz?`width:${sz}px;height:${sz}px`:""}">${esc((name||"?")[0])}</span>`}
/* 升级在卡片上的呈现: 已结算(done)桶的升级必然已恢复(升级恢复口径=任务进 reviewing 及以后),
   不再当"待处理"冒红点/徽章 */
function escOn(c){return c.has_escalation&&c.bucket!=="done"}
function dotCls(c){return escOn(c)?"error":({working:"working",idle:"idle",done:"done"}[c.bucket]||"error")}
/* ===== 左栏: 实例列表按桶分组(空组不显示;耗时宽度条已裁决先缺) ===== */
function railItem(c){
 const name=c.instance_name;
 const preview=escOn(c)?(c.escalation_summary||"有升级待处理")
  :(c.current_tool&&c.current_tool!=="待命中"?`正在执行: ${c.current_tool}`:(c.task_title||"(空闲)"));
 return `<div class="rail-item ${peekOf===name?"sel":""}" onclick="togglePeek('${esc(name)}')">
 <span style="position:relative">${avat(name,shellOf(name),34)}<span style="position:absolute;right:-1px;bottom:-1px"><span class="dot ${dotCls(c)}"></span></span></span>
 <span style="flex:1;min-width:0">
 <span style="display:flex;justify-content:space-between;gap:6px"><b>${esc(name)}</b><span class="time">${esc(c.relative_time||"")}</span></span>
 <span style="display:flex;justify-content:space-between;gap:6px">
 <span style="color:#8fa3c8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(preview)}</span>
 ${escOn(c)?`<span class="badge">1</span>`:""}</span></span></div>`}
/* ===== 主渲染(1.5s 轮询 /api/state) ===== */
/* 分组语义: 有待处理升级的一律进"待处理"(沿用 attention 桶语义),其余按桶 */
function groupOf(c){return escOn(c)||c.bucket==="attention"?"attention":c.bucket}
function render(d){
 lastState=d;cockpitReadonly=d.readonly;
 document.getElementById("cstat").textContent=`会话 ${d.counts.sessions} · 升级 ${d.counts.escalations}`;
 document.getElementById("ro").textContent=d.readonly?"只读(未注入总控身份)":"";
 document.getElementById("cfgbar").innerHTML=d.configured?"":
  "<div style='background:#3a1d24;border-bottom:1px solid #6e2b36;padding:8px 14px'>"
  +"⚠ 还没配置: 总控助手没选定或编制是空的。<a style='color:#ffb454' href='/setup'>去配置页点选 →</a></div>";
 SHELLS={};for(const i of d.org.instances)SHELLS[i.name]=i.shell;
 ORGINST=d.org.instances;snapCards={};
 for(const k of ["attention","working","idle","done"])
  for(const c of (d.snapshot[k]||[]))snapCards[c.instance_name]=c;
 // 总控头: 头像+状态点+壳+模型
 const ctrl=ORGINST.find(i=>i.name==="总控")||ORGINST.find(i=>i.name===d.org.controller);
 document.getElementById("ctrl-av").innerHTML=avat("总控",ctrl?ctrl.shell:"other",34);
 document.getElementById("ctrl-sub").textContent=ctrl?`${ctrl.shell} + ${ctrl.model}`:"未配置";
 // 左栏四桶(空组不显示)
 const allCards=Object.values(snapCards);
 let rh="";
 for(const [k,label] of GROUPS){
  const cards=allCards.filter(c=>groupOf(c)===k);
  if(!cards.length)continue;
  rh+=`<h4>${label} · ${cards.length}</h4>`+cards.map(railItem).join("")}
 document.getElementById("rail").innerHTML=rh;
 // 审批卡(墨绿)+升级红卡,排在对话流顶部;升级卡补 unread 类(审计缺口 3,未读加粗生效)
 let fh="";
 for(const a of d.approvals){
  if(a.kind==="force"){
   fh+=`<div class="approve-card force"><b>[兜底跳转]</b> 任务#${a.task_id}: ${esc(a.from)}→${esc(a.to)} ${esc(a.reason||"")}
   <small style="color:#7e93bd">发起: ${esc(a.initiator)}</small>
   <div style="margin-top:8px;display:flex;gap:8px">
   <button class="btn-ok" onclick="approveForce(${a.approval_id},'approve')">✓ 批准</button>
   <button class="btn-no" onclick="approveForce(${a.approval_id},'reject')">✗ 驳回</button></div></div>`}
  else{
   const tag={plan:"计划确认",final:"最终确认",permission:"权限裁决"}[a.kind];
   const what=a.kind==="permission"?`#${a.ruling_id} ${esc(a.worker)}: ${esc(a.tool)}`:`#${a.task_id} ${esc(a.title)}`;
   fh+=`<div class="approve-card"><b>[${tag}]</b> ${what}
   <div style="margin-top:8px;display:flex;gap:8px">
   <button class="btn-ok" onclick="approve('${a.kind}',${a.kind==="permission"?a.ruling_id:a.task_id},'approve')">✓ 批准</button>
   <button class="btn-no" onclick="approve('${a.kind}',${a.kind==="permission"?a.ruling_id:a.task_id},'reject')">✗ 驳回</button></div></div>`}}
 for(const e of d.escalations){
  if(dismissedEsc.has(e.seq))continue;
  if(e.recovered)
   fh+=`<div class="approve-card">✓已恢复 任务#${e.task_id??"-"}: ${esc(e.reason)}</div>`;
  else
   fh+=`<div class="esc-card unread">⚠ 任务#${e.task_id??"-"}: ${esc(e.reason)} —— <span class="ack" onclick="dismissedEsc.add(${e.seq});this.closest('.esc-card').remove()">知道了</span></div>`}
 document.getElementById("flowcards").innerHTML=fh;
 renderPeek()}
async function approve(kind,id,decision){
 const r=await j("/api/approve",{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({kind,decision,task_id:id,ruling_id:id})});
 if(r.error)alert(r.error);poll()}
async function approveForce(approval_id,decision){
 const r=await j("/api/force/approve",{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({approval_id,decision})});
 if(r.error)alert(r.error);poll()}
/* ===== 右栏 peek: 实例详情 / 角色·条目编制表(两者互斥,切换内容不叠开) ===== */
function togglePeek(name){
 if(peekOf===name){closePeek();return}
 peekOf=name;peekDetail=null;
 if(lastState)render(lastState);
 refreshDetail(name)}
/* ===== 号池管理(票59): 建池/归 key 入池/摘除/令牌/详情 ===== */
let poolData={pools:[]};
async function renderPools(){
 const box=document.getElementById("pool-list");
 const status=document.getElementById("pool-status");
 if(!box||!status)return;
 status.textContent="读取中…";
 try{
  const r=await j("/api/pool/list");
  poolData=r;
  if(!r.pools||!r.pools.length){
   status.textContent="暂无号池(总控入阵后可建池)";
   box.innerHTML=`<div class="peek-row"><span>空</span><span>点击上方"建池"创建</span></div>`;
   return}
  status.textContent=`${r.pools.length} 个池`;
  let h="";
  for(const p of r.pools){
   const members=p.members||[];
   const hasToken=p.has_token;
   h+=`<div class="peek-row" style="flex-direction:column;gap:4px">
    <div style="display:flex;justify-content:space-between;gap:6px">
     <b>${esc(p.name)}</b>
     <span class="sysline">${members.length} 成员 · ${hasToken?"有 token":"无 token"}</span>
    </div>
    <div class="sysline" style="font-size:11px">成员: ${members.length?members.map(m=>esc(m)).join(", "):"(空)"}</div>
    <div class="row" style="gap:4px;margin-top:2px">
     <select class="pool-add-sel" data-pool="${esc(p.name)}" style="flex:1">
      <option value="">归入 credential …</option>
      ${r.available_credentials||[]}.filter(c=>!members.includes(c)).map(c=>`<option value="${esc(c)}">${esc(c)}</option>`).join("")}
     </select>
     <button class="btn-ghost" onclick="addPoolMember('${esc(p.name)}')">加入</button>
     ${members.map(m=>`<button class="btn-ghost" style="padding:3px 8px;font-size:11px" onclick="removePoolMember('${esc(p.name)}','${esc(m)}')">-${esc(m)}</button>`).join("")}
    </div>
    ${hasToken?`<div class="row" style="gap:4px;margin-top:2px">
      <button class="btn-ghost" onclick="rotatePoolToken('${esc(p.name)}')">轮换 token</button>
      <button class="btn-no" onclick="deletePool('${esc(p.name)}')">删池</button>
     </div>`:`<div class="row" style="gap:4px;margin-top:2px">
      <button class="btn-no" onclick="deletePool('${esc(p.name)}')">删池</button>
     </div>`}
   </div>`}
  box.innerHTML=h;
 }catch(e){status.textContent="池读取失败"}}
async function createPool(){
 if(cockpitReadonly){alert("只读: 未注入总控身份");return}
 const name=gv("new-pool-name");
 if(!name){alert("池名不能为空");return}
 const r=await j("/api/pool/create",{method:"POST",headers:CT,body:JSON.stringify({name,members:[],request_id:"web-pool-create-"+Date.now()})});
 if(r.error){alert(r.error);return}
 if(r.token){alert("token 明文请保存: "+r.token)}
 renderPools()}
async function addPoolMember(pool){
 if(cockpitReadonly){alert("只读: 未注入总控身份");return}
 const sel=document.querySelector(`.pool-add-sel[data-pool="${pool}"]`);
 const cred=sel?sel.value:"";
 if(!cred){alert("选一个 credential");return}
 const r=await j("/api/pool/add-member",{method:"POST",headers:CT,body:JSON.stringify({pool,credential:cred,request_id:"web-pool-add-"+Date.now()})});
 if(r.error){alert(r.error);return}
 renderPools()}
async function removePoolMember(pool,cred){
 if(cockpitReadonly){alert("只读: 未注入总控身份");return}
 if(!confirm(`确认把 ${cred} 从池 ${pool} 摘除?`))return;
 const r=await j("/api/pool/remove-member",{method:"POST",headers:CT,body:JSON.stringify({pool,credential:cred,request_id:"web-pool-rm-"+Date.now()})});
 if(r.error){alert(r.error);return}
 if(r.warning)alert(r.warning);
 renderPools()}
async function rotatePoolToken(pool){
 if(cockpitReadonly){alert("只读: 未注入总控身份");return}
 if(!confirm(`确认轮换池 ${pool} 的 token? 旧 token 将作废。`))return;
 const r=await j("/api/pool/rotate-token",{method:"POST",headers:CT,body:JSON.stringify({name:pool,request_id:"web-pool-rotate-"+Date.now()})});
 if(r.error){alert(r.error);return}
 alert("新 token 请保存: "+r.token)}
async function deletePool(pool){
 if(cockpitReadonly){alert("只读: 未注入总控身份");return}
 if(!confirm(`确认删除池 ${pool}? 此操作不可逆。`))return;
 const r=await j("/api/pool",{method:"DELETE",headers:CT,body:JSON.stringify({name:pool,request_id:"web-pool-del-"+Date.now()})});
 if(r.error){alert(r.error);return}
 renderPools()}
function closePeek(){peekOf=null;peekDetail=null;if(lastState)render(lastState)}
async function refreshDetail(name){
 try{const d=await j("/api/instance/"+encodeURIComponent(name));
  if(peekOf===name){peekDetail=d;renderPeek()}}catch(e){}}
function renderPeek(){
 const pk=document.getElementById("peek");
 if(!peekOf){pk.className="peek closed";pk.innerHTML="";pk.dataset.mode="";return}
 pk.className="peek";
 if(peekOf==="__org__"){
  // 编制表不随轮询重建(注册表分区自己刷,重建会把已刷出的内容洗回"读取中"),只在打开时搭骨架
  if(pk.dataset.mode!=="org"){pk.innerHTML=orgHtml();pk.dataset.mode="org"}
  return}
 pk.dataset.mode="inst";
 pk.innerHTML=instHtml(peekOf);
 const pp=document.getElementById("peek-pool");
 if(pp && lastState){
  const pools=lastState.snapshot&&lastState.snapshot.pools;
  if(pools&&pools.length){
   let h="<h4 style='margin:10px 0 4px;color:#7e93bd'>号池健康</h4>";
   for(const pl of pools){
    const dop=pl.circuit_open_count>0?"style='color:#ff6b6b'":"";
    h+=`<div class='peek-row' ${dop}><b>${esc(pl.name)}</b>`
     +`成员${pl.member_count} 熔断${pl.circuit_open_count}</div>`;
    for(const m of pl.members){
     const dotC=m.circuit==="open"?"color:#ff6b6b":(m.circuit==="half_open"?"color:#ffb454":"color:#7e93bd");
     const failN=m.consecutive_failures?` FAIL×${m.consecutive_failures} `:"";
     h+=`<div class='peek-row'><span style='${dotC};margin-right:6px'>●</span>`
      +`<span style='flex:1'>${esc(m.name)} ${failN}${esc(m.last_error||"")}</span></div>`;
    }
   }
   pp.innerHTML=h;
  }}
function instHtml(name){
 const c=snapCards[name]||{instance_name:name};
 const shell=shellOf(name);
 const dp=peekDetail&&peekDetail.dispatches&&peekDetail.dispatches[0];
 const role=(dp&&dp.worker_role)||"";
 let dur="-";
 if(dp){const t1=dp.status==="active"?Math.floor(Date.now()/1000):(dp.updated_at||dp.created_at);
  dur=Math.max(0,Math.round((t1-dp.created_at)/60))+" min"}
 const quota=c.quota_pct!=null?`${c.quota_pct}%${c.quota_pct<20?" ⚠将尽":""}`:"-";
 const rows=[["当前任务",(c.task_title||"(空闲)")+(c.dispatch_id?" #"+c.dispatch_id:"")],
  ["当前工具",c.current_tool||"-"],["本单耗时",dur],["额度剩余",quota],
  ["最后动静",c.relative_time||"-"],
  ["升级状态",escOn(c)?`⚠ ${c.escalation_summary||"有升级"}`:"无"]];
 const did=c.dispatch_id,tid=c.task_id;
 return `<div class="peek-head"><span class="peek-ring">${avat(name,shell,44)}</span>
 <div><div style="font-size:15px"><b>${esc(name)}</b> <span class="dot ${dotCls(c)}"></span></div>
 <div class="sysline">${esc(shell)} + ${esc(c.model||"")}${role?" · "+esc(role):""}</div></div>
 <span style="flex:1"></span><button class="btn-ghost" onclick="closePeek()">收起 ×</button></div>
 <div class="peek-rows">${rows.map(([k,v])=>`<div class="peek-row"><span>${k}</span><span>${esc(v)}</span></div>`).join("")}</div>
<div id="peek-pool"></div>
 <div class="peek-acts">
 <button class="btn-ghost" ${did?"":"disabled"} onclick="nudge(${did||0})">续推 nudge</button>
 <button class="btn-no" ${(tid||did)?"":"disabled"} onclick="force(${tid||"null"},${did||"null"})">强制干预…</button></div>`
/* 续推 nudge: 无专用接口(零新增接口),走 /api/ctrl/send 请总控续推(花钱动作归总控 14.5) */
async function nudge(did){
 if(cockpitReadonly){alert("只读: 未注入总控身份");return}
 const r=await j("/api/ctrl/send",{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({text:`请续推(nudge)派单 #${did}: 问一句进展,卡住就升级(7.5 续推通道,来自驾驶舱右栏)`})});
 if(r.error)alert(r.error)}
/* 强制干预(审计缺口 2): /api/force,总控身份门控;输入目标状态=task force,留空=取消派单 */
async function force(tid,did){
 if(cockpitReadonly){alert("只读: 未注入总控身份");return}
 let body=null;
 if(tid){
  const s=prompt(`强制干预任务 #${tid}: 输入目标状态(如 discussing / archived)\n留空 = 取消当前派单 #${did}`,"");
  if(s===null)return;
  if(s.trim())body={task_id:tid,to_state:s.trim()}}
 if(!body&&did){
  if(!confirm(`确认取消派单 #${did}?`))return;
  body={dispatch_id:did}}
 if(!body)return;
 const r=await j("/api/force",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
 if(r.error)alert(r.error);poll()}
/* 角色/条目编制表: 实例行式表(角色=最新派单 worker_role,打开时取一次)+注册表四分区;不做旧的壳/key 增删表单 */
function openOrg(){
 peekOf="__org__";
 if(lastState)render(lastState);
 for(const i of ORGINST){
  if(orgRoles[i.name]!=null)continue;
  j("/api/instance/"+encodeURIComponent(i.name)).then(d=>{
   orgRoles[i.name]=(d.dispatches&&d.dispatches[0]&&d.dispatches[0].worker_role)||"";
   const el=document.getElementById("org-rows");
   if(peekOf==="__org__"&&el)el.innerHTML=orgRowsHtml()}).catch(()=>{orgRoles[i.name]=""})}
 renderRegistry()}
function orgRowsHtml(){
 let h="";
 for(const i of ORGINST){
  const role=orgRoles[i.name]||"";
  h+=`<div class="peek-row"><span>${esc(i.name)}${role?"("+esc(role)+")":""}</span><span>${esc(i.shell)} + ${esc(i.model)}</span></div>`}
 return h}
function orgHtml(){
 return `<div class="peek-head"><b>角色编排与条目</b><span style="flex:1"></span>
 <button class="btn-ghost" onclick="closePeek()">收起 ×</button></div>
 <div class="peek-rows" id="org-rows">${orgRowsHtml()}</div>
 <h4>号池管理</h4>
 <div class="peek-rows" id="pool-section">
  <div class="sysline" id="pool-status" style="padding:4px 0">读取中…</div>
  <div id="pool-list"></div>
  <div class="row" style="gap:6px;margin-top:8px">
   <input id="new-pool-name" placeholder="新池名" style="width:120px">
   <button class="btn-ghost" onclick="createPool()">建池</button>
   <button class="btn-ghost" onclick="renderPools()">刷新</button>
  </div>
 </div>
 <h4>集成注册表(四分区·含旧条目迁移)</h4>
 <div class="peek-rows" id="registry"><div class="sysline" id="registry-status" style="padding:4px 0">读取中…</div>
 <div id="registry-list"></div>
 <div class="peek-acts" style="padding:12px 0"><button class="btn-ghost" id="registry-migrate" onclick="migrateRegistry()">初始化/迁移</button></div></div>`}
async function renderRegistry(){
 const regBox=document.getElementById("registry-list");
 const regStatus=document.getElementById("registry-status");
 const btn=document.getElementById("registry-migrate");
 if(!regBox||!regStatus||!btn)return;
 btn.disabled=cockpitReadonly;
 btn.textContent=cockpitReadonly?"只读":"初始化/迁移";
 // 渲染池分区(票59)
 renderPools();
 try{
  const reg=await j("/api/integrations");
  const entries=reg.entries||[],migrations=reg.migrations||[];
  const pending=migrations.filter(x=>!x.migrated).length;
  regStatus.textContent=pending
   ?`旧条目迁移: ${migrations.length-pending}/${migrations.length} 已迁移,${pending} 待处理`
   :`旧条目迁移: ${migrations.length} 条已就绪`;
  const groups={
   protocol:{title:"Protocols",items:entries.filter(x=>x.key.startsWith("integration_protocol:"))},
   provider:{title:"Providers",items:entries.filter(x=>x.key.startsWith("integration_provider:"))},
   shell:{title:"Shells",items:entries.filter(x=>x.key.startsWith("integration_shell:"))},
   credential:{title:"Credentials",items:entries.filter(x=>x.key.startsWith("credential:"))}};
  let h="";
  for(const [kind,group] of Object.entries(groups)){
   h+=`<div data-registry-partition="${kind}"><div class="sysline" style="padding:8px 0 2px">${group.title} (${group.items.length})</div>`;
   for(const item of group.items){
    const name=item.key.split(":",2)[1];
    let config="",migrated="";
    if(kind==="protocol"){
     config=`${item.auth_style||""} · ${(item.model_discovery_paths||[]).length} 个发现端点`;
    }else if(kind==="provider"){
     config=`${item.protocol||""} · ${item.base_url||""}`;
     migrated=migrations.some(x=>x.target==="integration_provider:"&&x.name===item.credential_key&&x.migrated)?"已迁移":"登记";
    }else if(kind==="shell"){
     config=(item.protocols||[]).join(", ");
     migrated=migrations.some(x=>x.target==="integration_shell:"&&x.name===name&&x.migrated)?"已迁移":"登记";
    }else config=item.provider||"";
    h+=`<div class="peek-row"><span>${esc(name)}</span><span>${esc(config)}${migrated?" · "+migrated:""}</span></div>`}
   if(!group.items.length)h+=`<div class="peek-row"><span>空</span><span>-</span></div>`;
   h+="</div>"}
  regBox.innerHTML=h;
 }catch(e){regStatus.textContent="注册表读取失败"}}
async function migrateRegistry(){
 if(cockpitReadonly)return;
 const btn=document.getElementById("registry-migrate");
 btn.disabled=true;btn.textContent="迁移中…";
 const r=await j("/api/integrations/migrate",{method:"POST",
  headers:{"Content-Type":"application/json"},body:"{}"});
 if(r.error)alert(r.error);
 await renderRegistry()}
/* ===== 总控真会话对话面: #stream 渲染事件流,1.5s 轮询拿增量往上拼 =====
   活性信号(dsh 式 live tail): "总控正在输入…"行(小头像+橙字)、思维链橙色▸、
   工具调用单独成行⚙、正文逐 token 打字机(stream_event 增量;assistant 整段兜底) */
let ctrlNext=0,ctrlAssistantDiv=null,ctrlThinkDiv=null,ctrlStatusDiv=null,ctrlDelta=false;
function ctrlLine(){const d=document.createElement("div");
 document.getElementById("stream").appendChild(d);return d}
function ctrlStatus(txt){
 if(!ctrlStatusDiv){ctrlStatusDiv=ctrlLine();
  ctrlStatusDiv.className="sysline";ctrlStatusDiv.style.cssText="display:flex;gap:8px;align-items:center"}
 ctrlStatusDiv.innerHTML=`${avat("总控",shellOf("总控"),24)}<span class="thinking">${esc(txt)}</span>`}
function ctrlStatusOff(){if(ctrlStatusDiv){ctrlStatusDiv.remove();ctrlStatusDiv=null}}
function ctrlAssistant(){
 if(!ctrlAssistantDiv){
  const w=ctrlLine();w.style.cssText="display:flex;gap:8px;align-items:flex-start";
  w.innerHTML=avat("总控",shellOf("总控"),30);
  ctrlAssistantDiv=document.createElement("div");ctrlAssistantDiv.className="bubble-a";
  w.appendChild(ctrlAssistantDiv)}
 return ctrlAssistantDiv}
function ctrlThink(){
 if(!ctrlThinkDiv){ctrlThinkDiv=ctrlLine();ctrlThinkDiv.className="thinking";ctrlThinkDiv._t=""}
 return ctrlThinkDiv}
function toolLine(name){
 const d=ctrlLine();d.className="sysline";d.textContent=`⚙ 正在执行: ${name||""}`;
 ctrlAssistantDiv=null;ctrlThinkDiv=null}
function renderCtrl(e){
 const st=document.getElementById("stream");
 if(e.type==="system"&&e.subtype==="thinking_tokens"){
  ctrlStatus(`思考中 …(已想 ${e.estimated_tokens||"?"} tokens)`)}
 else if(e.type==="stream_event"){
  const ev=e.event||{};
  if(ev.type==="content_block_delta"){
   const d=ev.delta||{};
   if(d.type==="text_delta"&&d.text){
    ctrlStatusOff();ctrlDelta=true;
    ctrlAssistant().appendChild(document.createTextNode(d.text));}
   else if(d.type==="thinking_delta"&&d.thinking){
    ctrlDelta=true;
    const t=ctrlThink();t._t+=d.thinking;
    t.textContent="▸ 思维链: "+t._t;}
  }else if(ev.type==="content_block_start"&&(ev.content_block||{}).type==="tool_use"){
   ctrlStatusOff();toolLine(ev.content_block.name);}}
 else if(e.type==="assistant"){
  const blocks=(e.message&&e.message.content)||[];
  // 增量已渲染过的正文不重复拼;工具调用单独成行
  if(!ctrlDelta){
   const txt=blocks.filter(b=>b.type==="text").map(b=>b.text).join("");
   if(txt){ctrlStatusOff();ctrlAssistant().appendChild(document.createTextNode(txt));}}
  const thk=blocks.filter(b=>b.type==="thinking").map(b=>b.thinking).join("");
  if(thk&&!ctrlThinkDiv){const t=ctrlThink();t._t=thk;t.textContent="▸ 思维链: "+thk;}
  for(const b of blocks)if(b.type==="tool_use")toolLine(b.name);}
 else if(e.type==="result"){
  // 一轮收尾: 小结(耗时/cost),清活性状态
  ctrlAssistantDiv=null;ctrlThinkDiv=null;ctrlDelta=false;ctrlStatusOff();
  const sec=((e.duration_ms||0)/1000).toFixed(1);
  const cost=e.total_cost_usd!=null?(" · $"+e.total_cost_usd):"";
  const d=ctrlLine();d.className="sysline";d.textContent=`⏱ ${sec}s${cost}`;}
 else if(e.type==="system"&&e.subtype==="restart"){
  ctrlAssistantDiv=null;ctrlThinkDiv=null;ctrlDelta=false;ctrlStatusOff();
  const d=ctrlLine();d.className="thinking";
  d.textContent=`⟳ ${e.note||"会话进程重启,上文丢了"}`;}
 else if(e.type==="system"&&e.subtype==="error"){
  ctrlAssistantDiv=null;ctrlThinkDiv=null;ctrlDelta=false;ctrlStatusOff();
  const d=ctrlLine();
  d.innerHTML=`<div class="esc-card" style="margin-bottom:0"><b>总控出错:</b> ${esc(e.text||"未知错误")}</div>`;}
 // system 其余子类(init 等)过滤不显示
 st.scrollTop=st.scrollHeight}
async function pollCtrl(){
 try{const r=await j("/api/ctrl/events?after="+ctrlNext);
  ctrlNext=r.next;
  for(const e of r.events)renderCtrl(e)}catch(e){}}
async function pollCtrlStatus(){
 try{const r=await j("/api/ctrl/status");
  document.getElementById("ctrl-dot").innerHTML=`<span class="dot ${r.alive?"working":"idle"}"></span>`}catch(e){}}
async function sendMsg(){
 const el=document.getElementById("msg");const text=el.value;if(!text)return;
 if(/^(批准|驳回)\s*(权限)?\s*#?\d+\s*$/.test(text)){
  // 审批口令照旧走 /api/message 机械秒批
  const r=await j("/api/message",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text})});
  if(r.error)alert(r.error);else if(r.note)alert(r.note);}
 else{
  // 其余文本=跟总控说话: 本地先上墙(右侧蓝气泡)+立刻亮"正在输入"行(不等首轮轮询),回复靠轮询拼
  ctrlAssistantDiv=null;ctrlThinkDiv=null;ctrlDelta=false;
  const w=ctrlLine();w.style.display="flex";
  const b=document.createElement("div");b.className="bubble-u";b.textContent=text;w.appendChild(b);
  const st=document.getElementById("stream");st.scrollTop=st.scrollHeight;
  ctrlStatus("总控正在输入…");
  const r=await j("/api/ctrl/send",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text})});
  if(r.error){ctrlStatusOff();alert(r.error);}}
 el.value="";poll();pollCtrl()}
async function poll(){try{render(await j("/api/state"))}catch(e){}}
document.getElementById("msg").addEventListener("keydown",e=>{if(e.key==="Enter")sendMsg()});
poll();pollCtrl();pollCtrlStatus();
setInterval(()=>{poll();pollCtrl();pollCtrlStatus();
 if(peekOf==="__org__")renderRegistry();
 else if(peekOf)refreshDetail(peekOf)},1500);
</script></body></html>"""


_SETUP_PAGE = r"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>天机 · 首次配置</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;background:#0f1420;color:#dde3ee;font-size:13px}
h2{font-size:15px;margin:0 0 10px}
.panel{background:#161d2e;border-radius:8px;padding:12px}
select,input{background:#0f1420;color:#dde3ee;border:1px solid #28314a;border-radius:6px;padding:6px 8px;font-size:13px}
button{background:#2b3a5e;color:#dde3ee;border:0;border-radius:5px;padding:6px 14px;cursor:pointer}
button.primary{background:#3d5a2e}
table{border-collapse:collapse;width:100%}
td,th{border:1px solid #28314a;padding:5px 8px;font-size:12px;text-align:left}
th{color:#8fa3c8}
.muted{color:#8fa3c8}
.badge{display:inline-block;background:#24311f;border:1px solid #3d5a2e;border-radius:4px;padding:1px 6px;font-size:11px}
.badge.warn{background:#3a1d24;border-color:#6e2b36}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.field{display:flex;flex-direction:column;gap:4px;margin-bottom:8px}
.field label{color:#8fa3c8;font-size:12px}
#topbar{display:flex;gap:16px;align-items:center;background:#161d2e;padding:8px 14px;border-bottom:1px solid #28314a}
/* 入阵过场动效: 标题呼吸+进度条扫动(静态页像死了,2026-08-21 模拟反馈) */
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
@keyframes sweep{0%{left:-45%}100%{left:105%}}
.ov-title{font-size:28px;font-weight:bold;letter-spacing:2px;animation:pulse 2.2s infinite ease-in-out}
.ov-bar{width:240px;height:3px;background:#1d2740;border-radius:2px;overflow:hidden;position:relative;margin-top:20px}
.ov-bar-in{position:absolute;top:0;width:45%;height:100%;background:linear-gradient(90deg,#3d5a8e,#7ab648);animation:sweep 1.3s infinite ease-in-out}
</style></head><body>
<div id="topbar"><b>天机 · 首次配置</b><span id="stat" class="muted"></span>
<span style="flex:1"></span><button id="backbtn" class="primary" style="display:none" onclick="enterCockpit()">配齐了,去开工 →</button>
<button id="backbtn2" onclick="location.href='/'">返回驾驶舱</button></div>
<div id="overlay" style="display:none;position:fixed;left:0;top:0;right:0;bottom:0;background:#0f1420;z-index:99;flex-direction:column;align-items:center;justify-content:center">
 <div class="ov-title">吾乃天机 · 剑来</div>
 <div class="ov-bar"><div class="ov-bar-in"></div></div>
 <div class="muted" style="margin-top:12px">总控正在醒来,头一次要 10-30 秒,别慌 …</div>
 <div id="ovmsg" class="muted" style="margin-top:18px"></div>
 <a id="ovlink" href="/" style="display:none;color:#ffb454;margin-top:8px">直接进入驾驶舱 →</a>
</div>
<div style="padding:16px">
 <div class="row panel" style="margin-bottom:12px">
  <b>总控:</b><select id="c-shell"></select>
  <select id="c-src" onchange="srcToggle('c')">
   <option value="builtin">用它自己的登录/订阅(不用另配)</option>
   <option value="key">外接模型服务(我有 key)</option></select>
  <span id="c-keybox" style="display:none" class="row">
   <select id="c-provider" onchange="providerPick('c')"></select>
   <input id="c-key" placeholder="key: sk-…">
   <input id="c-url" placeholder="接口地址 https://…">
   <button onclick="doDiscover('c')">获取可用模型</button><span id="c-probemsg" class="muted"></span>
   <select id="c-modelsel" style="display:none"></select>
   <input id="c-model" placeholder="模型名(探不到就手填)">
  </span>
  <button class="primary" onclick="setCtrl()">定下总控</button>
  <span class="muted">总控是你以后的唯一入口,它再指挥其他助手</span>
  <div id="kimi-login-hint" class="badge warn" style="display:none;margin-left:8px">
   总控的 kimi 在独立环境运行(不和你平时的 kimi 混用),首次使用需要在独立环境里登录一次。若看到"Authentication required",按提示操作即可。</div>
 </div>
 <div class="panel" style="margin-bottom:12px"><h2>默认工作目录</h2>
  <div class="row">
   <input id="pd-path" placeholder="任务未指定项目目录时的回退来源;留空保存=清除" style="flex:1">
   <button onclick="setProjectDir()">保存</button>
   <span id="pd-msg" class="muted"></span>
  </div>
  <div class="muted" style="margin-top:6px">在项目目录里跑 <code>tianji start</code> 会自动把它设为默认工作目录;建任务时不指定项目目录,工人就回退到这里干活。</div>
 </div>
 <div style="display:flex;gap:12px">
  <div class="panel" style="width:400px;flex:none"><h2>配一张牌</h2>
   <div class="field"><label>助手</label><select id="w-shell"></select></div>
   <div class="field"><label>模型来源</label><select id="w-src" onchange="srcToggle('w')">
    <option value="builtin">自带/内置(免 key)</option>
    <option value="key">外接模型服务(要 key)</option></select></div>
   <div id="w-keybox" style="display:none">
    <div class="field"><label>模型服务</label><select id="w-provider" onchange="providerPick('w')"></select></div>
    <div class="field"><label>key</label><input id="w-key" placeholder="sk-…"></div>
    <div class="field"><label>接口地址</label><input id="w-url" placeholder="https://…(选了服务商自动带出)"></div>
    <button onclick="doDiscover('w')">获取可用模型</button> <span id="w-probemsg" class="muted"></span>
    <div class="field" style="margin-top:8px"><label>模型</label>
     <select id="w-modelsel" style="display:none"></select>
     <input id="w-model" placeholder="探不到就手填"></div>
   </div>
   <div class="field" id="w-builtinbox"><label>模型名</label><input id="w-bmodel" placeholder="自带模型的名字"></div>
   <div class="row">
    <div class="field"><label>角色</label><select id="w-role">
     <option>审核</option><option>实施</option><option>总控</option></select></div>
    <div class="field"><label>开几个</label><input id="w-count" type="number" value="1" min="1" style="width:70px"></div>
   </div>
   <button class="primary" onclick="addCard()">加入编制(立即落账)</button>
   <span class="muted">每张牌点这个就算配好,不用最后统一提交</span>
  </div>
  <div class="panel" style="flex:1"><h2>编制总览</h2>
   <div id="roster"></div>
   <div id="warn" style="margin-top:10px"></div>
  </div>
 </div>
</div>
<script>
let S={controller:null,scanned:[],instances:[],keys:[],configured:false};
const CT={"Content-Type":"application/json"};
function el(id){return document.getElementById(id)}
function gv(id){return el(id).value.trim()}
function esc(s){return String(s??"").replace(/&/g,"&amp;").replace(/</g,"&lt;")}
async function j(u,opt){const r=await fetch(u,opt);return r.json()}
function shellOptions(cur, ctrlOnly){
 const list = ctrlOnly ? S.scanned.filter(s => s.ctrl_session) : S.scanned;
 return list.map(s =>
  `<option value="${s.name}" ${s.supported?"":"disabled"} ${s.name===cur?"selected":""}>`
  +`${s.name}${s.supported?"":"(暂不支持)"}</option>`).join("")}
function fillShells(){
 el("c-shell").innerHTML=shellOptions(S.controller?S.controller.shell:"", true);
 el("w-shell").innerHTML=shellOptions("", false)}
function fillProviders(){
 for(const pfx of ["c","w"]){
  const sel=el(pfx+"-provider");if(!sel)continue;
  const cur=sel.value;
  const groups={};
  for(const p of (S.providers||[]))(groups[p.category]=groups[p.category]||[]).push(p);
  let h="<option value=''>自定义(自己填地址)</option>";
  for(const cat in groups)
   h+=`<optgroup label="${esc(cat)}">`+groups[cat].map(p=>
    `<option value="${esc(p.name)}" data-url="${esc(p.base_url)}" data-proto="${esc(p.protocol)}">${esc(p.display||p.name)}</option>`).join("")+"</optgroup>";
  sel.innerHTML=h;
  if(cur&&[...sel.options].some(o=>o.value===cur))sel.value=cur}}
function srcToggle(p){
 const v=el(p+"-src").value;
 el(p+"-keybox").style.display=v==="key"?"":"none";
 const bb=el(p+"-builtinbox");if(bb)bb.style.display=v==="key"?"none":""}
function curModel(p){
 const sel=el(p+"-modelsel");
 return sel.style.display==="none"?gv(p+"-model"):(sel.value||gv(p+"-model"))}
function providerPick(p){
 const sel=el(p+"-provider");
 const opt=sel.selectedOptions[0];
 if(opt&&opt.dataset.url)el(p+"-url").value=opt.dataset.url}
async function doDiscover(p){
 // 票33 闭环: 选供应商(或自定义填地址)→填 key→获取模型→选模型
 const sel=el(p+"-provider");
 const opt=sel.selectedOptions[0];
 const body={key:gv(p+"-key"),base_url:gv(p+"-url"),
  provider:(opt&&opt.value)||"",protocol:(opt&&opt.dataset.proto)||""};
 if(!body.provider&&!body.base_url){alert("选个模型服务,或自己填接口地址");return}
 if(!body.key){alert("先填 key");return}
 const msg=el(p+"-probemsg");msg.textContent="获取中…";
 const r=await j("/api/integrations/discover-models",{method:"POST",headers:CT,
  body:JSON.stringify(body)});
 if(r.error){msg.textContent=r.error;return}
 if(r.ok&&r.models.length){
  msg.textContent="探到 "+r.models.length+" 个("+r.source+")";
  const msel=el(p+"-modelsel");
  msel.innerHTML=r.models.map(m=>`<option>${esc(m)}</option>`).join("");
  msel.style.display="";el(p+"-model").style.display="none";}
 else{msg.textContent=(r.reason||"机械探测失败")+",手填一个模型名";
  el(p+"-modelsel").style.display="none";el(p+"-model").style.display="";}}
function roleOf(i){const m=/拟定角色: (\S+)/.exec(i.notes||"");return m?m[1]:""}
function render(){
 el("stat").textContent=S.configured?"已配置,可以开工":"还没配齐: 先定总控,再至少配一个工人";
 el("backbtn").style.display=S.configured?"":"none";
 el("backbtn2").style.display=S.configured?"none":"";
 let rows="";
 if(S.controller)rows+=`<tr><td>总控(兼架构/裁判)</td><td>${esc(S.controller.shell)}</td>`
  +`<td>${esc(S.controller.model)}</td><td>总控</td></tr>`;
 for(const i of S.instances)
  rows+=`<tr><td>${esc(roleOf(i))}</td><td>${esc(i.shell)}</td>`
   +`<td>${esc(i.model)}${i.key_name?" ["+esc(i.key_name)+"]":" [免key]"}</td>`
   +`<td>${esc(i.name)}</td></tr>`;
 el("roster").innerHTML=rows
  ?`<table><tr><th>角色</th><th>助手</th><th>模型</th><th>实例名</th></tr>${rows}</table>`
  :"<p class='muted'>还一张牌都没有。</p>";
 // 默认工作目录: 焦点不在输入框才刷新(不吞正在输入的路径)
 if(document.activeElement!==el("pd-path"))el("pd-path").value=S.default_project_dir||"";
 el("pd-msg").textContent="";
 // 审核两轴检查: 凑不齐两轴或两轴同源(同助手同模型)当场标红
 const rev=S.instances.filter(i=>i.name.startsWith("审核"));
 const same=rev.length>=2&&rev.every(r=>r.shell===rev[0].shell&&r.model===rev[0].model);
 el("warn").innerHTML=rev.length<2
  ?`<span class="badge warn">审核只有 ${rev.length} 轴=自查自审,质量降级,建议配两个不同源的</span>`
  :(same?`<span class="badge warn">两轴审核同源(同助手同模型),等于没交叉</span>`
        :`<span class="badge">双轴审核就绪</span>`)}
async function enterCockpit(){
 // 过场(2026-08-21 模拟反馈: 首响 10-30s,干等像卡住)——自动把"你好,天机"
 // 发给总控,过场页等着,它第一句回复到了再正式进驾驶舱
 el("overlay").style.display="flex";
 await j("/api/ctrl/send",{method:"POST",headers:CT,body:JSON.stringify({text:"你好,天机"})});
 let n=0;const t0=Date.now();
 const timer=setInterval(async()=>{
  try{
   const r=await j("/api/ctrl/events?after="+n);n=r.next;
   if(r.events.some(e=>e.type==="assistant"||e.type==="result")){
    clearInterval(timer);location.href="/";return}
   if(r.events.some(e=>e.type==="system"&&e.subtype==="error")){
    clearInterval(timer);
    const errEvt=r.events.find(e=>e.type==="system"&&e.subtype==="error");
    el("ovmsg").innerHTML=`<div style="color:#ff6b6b;padding:8px;border:1px solid #6e2b36;border-radius:6px;background:#3a1d24;max-width:500px">
     <b>总控醒来失败:</b> ${esc(errEvt.text||"")}</div>`;
    el("ovlink").style.display="";return}
  }catch(e){}
  if(Date.now()-t0>90000){clearInterval(timer);
   el("ovmsg").textContent="总控还在热身,等不及可以直接进:";
   el("ovlink").style.display="";}
 },1500)}
async function setCtrl(){
 const body={shell:el("c-shell").value,source:el("c-src").value};
 if(body.source==="key"){
  body.key=gv("c-key");body.base_url=gv("c-url");body.model=curModel("c");
  if(!body.key||!body.base_url||!body.model){alert("key/地址/模型都得给");return}}
 const r=await j("/api/setup/controller",{method:"POST",headers:CT,body:JSON.stringify(body)});
 if(r.error){alert(r.error);return}
 S=r.state;fillShells();fillProviders();render()
  // kimi 登录态提示: 选 kimi + 没外接 key -> 提示
  const kimiHint=el("kimi-login-hint");
  if(kimiHint)kimiHint.style.display=(body.shell==="kimi"&&body.source!=="key")?"":"none";}
async function addCard(){
 const src=el("w-src").value;
 const card={shell:el("w-shell").value,source:src,role:el("w-role").value,
  count:Math.max(1,parseInt(gv("w-count"))||1)};
 if(src==="key"){
  const opt=el("w-provider").selectedOptions[0];
  card.provider=(opt&&opt.value)||"";
  card.protocol=(opt&&opt.dataset.proto)||"";
  card.key_value=gv("w-key");card.base_url=gv("w-url");card.model=curModel("w");
  if(!card.key_value||!card.base_url||!card.model){alert("key/地址/模型都得给");return}}
 else{card.model=gv("w-bmodel");
  if(!card.model){alert("模型名还没填");return}}
 if(card.role==="总控")card.count=1;
 const r=await j("/api/setup/land",{method:"POST",headers:CT,body:JSON.stringify({cards:[card]})});
 if(r.error){alert(r.error);return}
 S=r.state;fillProviders();render()}
async function setProjectDir(){
 const r=await j("/api/setup/project-dir",{method:"POST",headers:CT,body:JSON.stringify({path:gv("pd-path")})});
 if(r.error){alert(r.error);return}
 el("pd-msg").textContent="已保存";S=r.state;render()}
(async function(){
 S=await j("/api/setup/state");
 fillShells();fillProviders();fillPools();srcToggle("c");srcToggle("w");kimiHintToggle();render()})();
async function assignPool(name,pool){
 if(!pool)return;
 if(cockpitReadonly){alert("只读: 未注入总控身份");return}
 const r=await j("/api/pool/add-member",{method:"POST",headers:CT,body:JSON.stringify({pool,credential:name,request_id:"web-assign-"+Date.now()})});
 if(r.error){alert(r.error);return}
 alert("已归池 "+pool+": "+name)}
function kimiHintToggle(){
 const ctrl=S.controller;const kimiHint=el("kimi-login-hint");
 if(kimiHint&&ctrl)kimiHint.style.display=(ctrl.shell=="kimi"&&ctrl.source!="key")?"":"none";
 else if(kimiHint)kimiHint.style.display="none";}
</script></body></html>"""
