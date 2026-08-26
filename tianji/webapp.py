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
from . import cockpit, ctrlsession, integrations, ops, permission, wizard
from .db import connect, tianji_home

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
    """待审批卡三类: 计划确认/最终确认/权限裁决(15.1③)。"""
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
        cards = [c for cl in snap.values() if isinstance(cl, list)
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


@app.get("/api/integrations")
def api_integrations():
    """集成注册表快照与旧条目迁移状态(13.8/票42;只读,不含密钥本体)。"""
    conn = connect()
    try:
        return integrations.registry_state(conn)
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


# ---------------------------------------------------------------- 总控真会话

# 懒持有: 不在 web 启动时拉起,首次 send 才 start(进程不烧 token,但也别没事拉起)
_ctrl_session: BaseBackend | None = None
_ctrl_cfg_mtime: float = 0.0


def _read_ctrl_secret() -> str:
    """读总控身份 secret(从 ctrl-secret.txt,延迟解析路径)。"""
    p = tianji_home() / "ctrl-secret.txt"
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
        settings = tianji_home() / "settings-controller.json"
        mtime = (settings.stat().st_mtime if settings.exists() else 0.0)
        if mtime == _ctrl_cfg_mtime:
            return _ctrl_session
        _ctrl_session.close()
    settings = tianji_home() / "settings-controller.json"
    mtime = settings.stat().st_mtime if settings.exists() else 0.0
    _ctrl_session = BaseBackend.from_config(tianji_home(), settings)
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
            key_ref=str(home_p / "keys" / f"{key_name}.key"),
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
        settings = None
        secret = (home_p / "ctrl-secret.txt").read_text(
            encoding="utf-8").strip()
        if shell == "claude":
            provider = ({"key_value": card["key_value"],
                         "base_url": card["base_url"],
                         "model": card["model"]}
                        if card["source"] == "key" else None)
            settings = wizard._write_controller_settings(
                home_p, str(home_p), shell, secret,
                provider=provider, ready=True)
        elif shell == "kimi":
            provider = ({"key_value": card["key_value"],
                         "base_url": card["base_url"],
                         "model": card["model"],
                         "key_name": card.get("key_name", "主key")}
                        if card["source"] == "key" else None)
            settings = wizard._write_controller_settings(
                home_p, str(home_p), shell, secret,
                provider=provider, ready=True)
        else:
            # 第三方壳(已过 ctrl_session 守卫): 走 generic 数据驱动分支。
            # 注意: generic 目前不接外接 key(provider 不传导),
            # 外接 key 的第三方壳支持是后续票的事。
            settings = wizard._write_controller_settings(
                home_p, str(home_p), shell, secret, ready=True)
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


_PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>天机驾驶舱</title>
<style>
/* 整页一屏: body 撑满视口不滚;每个桶独立内滚(超出才出条);总控对话面吃剩余 */
html,body{height:100%}
body{font-family:system-ui,sans-serif;margin:0;color:#dde3ee;font-size:13px;display:flex;flex-direction:column;overflow:hidden;
 background:radial-gradient(1200px 500px at 70% -10%,#16203a 0%,#0b0f1a 55%)}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-thumb{background:#263352;border-radius:4px}
::-webkit-scrollbar-track{background:transparent}
#topbar{display:flex;gap:16px;align-items:center;padding:10px 16px;border-bottom:1px solid #1e2740;position:relative;z-index:10;flex:none;
 background:rgba(22,29,46,.75)}
#topbar b{font-size:16px;letter-spacing:2px;background:linear-gradient(90deg,#8fd0ff,#7ab648);
 -webkit-background-clip:text;background-clip:text;color:transparent}
#cfgbar{flex:none}
#buckets{display:flex;gap:10px;padding:10px;flex:none}
.bucket{flex:1;background:rgba(22,29,46,.8);border:1px solid #1e2740;border-radius:10px;padding:8px 10px;
 min-height:96px;max-height:26vh;overflow-y:auto}
.bucket h3{margin:0 0 6px;font-size:12px;color:#7e93bd;letter-spacing:1px;position:sticky;top:-8px;
 background:rgba(22,29,46,.97);padding:4px 0}
.card{background:#1a2338;border-radius:8px;padding:7px 10px;margin-bottom:6px;cursor:pointer;
 border-left:3px solid #31426e;transition:background .15s}
.card:hover{background:#223052}
.card.stale{opacity:.5}.card .esc{color:#ff6b6b;font-weight:bold}
.card .unread{font-weight:bold}
#flow{padding:0 10px;flex:none;max-height:16vh;overflow-y:auto}
.approval{background:#22301c;border:1px solid #3d5a2e;border-radius:8px;padding:8px 10px;margin-bottom:6px;display:flex;gap:8px;align-items:center}
.approval button{cursor:pointer}
.note{padding:6px 10px;border-radius:8px;margin-bottom:6px}
.note.red{background:#33181f;border:1px solid #6e2b36}
.note.green{background:#17301c;border:1px solid #2b6e36}
#pane{display:flex;flex-direction:column;padding:10px;flex:1;min-height:0}
#stream{flex:1;min-height:0;overflow-y:auto;background:rgba(13,18,32,.85);border:1px solid #1e2740;
 border-radius:10px;padding:12px 14px}
#stream div{padding:4px 0;border-bottom:1px solid rgba(30,39,64,.6)}
#stream .msg-a{white-space:pre-wrap;line-height:1.6}
#stream .msg-u{color:#8fd0ff;background:#16233d;border-radius:8px;padding:6px 10px;margin:4px 0}
#inputrow{display:flex;gap:8px;margin-top:10px;flex:none}
#msg{flex:1;background:#0f1420;color:#dde3ee;border:1px solid #26324e;border-radius:8px;padding:10px}
#msg:focus{outline:none;border-color:#3d5a8e;box-shadow:0 0 0 2px rgba(61,90,142,.25)}
#drawer{position:fixed;right:-380px;top:0;width:360px;height:100%;background:#141b2c;border-left:1px solid #1e2740;transition:right .2s;padding:12px;overflow-y:auto;z-index:9}
#drawer.open{right:0}
#drawer table{border-collapse:collapse;width:100%}
#drawer td,#drawer th{border:1px solid #26324e;padding:4px 6px;font-size:12px}
button{background:#26355c;color:#dde3ee;border:0;border-radius:6px;padding:5px 12px;cursor:pointer}
button:hover{background:#31447a}
button.primary{background:linear-gradient(90deg,#3d5a2e,#4a7038)}
</style></head><body>
<div id="topbar"><b>天机驾驶舱</b><span id="clock"></span><span id="cstat"></span>
<span id="ro" style="color:#ffb454"></span>
<span style="flex:1"></span><button onclick="location.href='/setup'">配置</button>
<button onclick="toggleDrawer()">角色/条目</button></div>
<div id="cfgbar"></div>
<div id="buckets"></div>
<div id="flow"></div>
<div id="pane"><div id="stream"></div>
<div id="inputrow"><input id="msg" placeholder="批准 16 / 驳回 16 / 批准权限 3,或直接跟总控说话(回车发送)">
<button onclick="sendMsg()">发送</button></div></div>
<div id="drawer"><div style="display:flex;align-items:center"><h3 style="flex:1;margin:0">角色编排与条目</h3>
<button onclick="toggleDrawer()">收起 ×</button></div><div id="org"></div>
<div id="registry"><h4>集成注册表</h4><div id="registry-status">读取中…</div>
<div id="registry-list"></div>
<button id="registry-migrate" onclick="migrateRegistry()">初始化/迁移</button></div></div>
<script>
const BUCKETS=[["attention","attention(待处理)"],["working","working(进行中)"],["done","done(已结算)"],["idle","idle(空闲)"]];
let drawerOpen=false;
let cockpitReadonly=true;
function toggleDrawer(){drawerOpen=!drawerOpen;document.getElementById("drawer").className=drawerOpen?"open":""}
function esc(s){return String(s??"").replace(/&/g,"&amp;").replace(/</g,"&lt;")}
async function j(u,opt){const r=await fetch(u,opt);return r.json()}
function cardHtml(c){
 let lbl=esc(c.instance_name)+(c.display_mode==="后台"?"·后台":"");
 if(c.has_escalation)lbl="⚠ "+lbl;
 const msg=(c.last_message&&c.last_message.payload)?(c.last_message.payload.event_type||c.last_message.type||""):"";
 return `<div class="card ${c.session_state==="idle"?"stale":""}" onclick="detail('${esc(c.instance_name)}')">
 <span>${c.status_point||""} <b>${lbl}</b> ${esc(c.model||"")}</span><br>
 <span>${esc(c.task_title||"(空闲)")} #${c.dispatch_id||"-"}</span><br>
 <small>${esc(c.current_tool||"")} · ${esc(String(msg))} @${c.relative_time||""}</small></div>`}
function render(d){
 document.getElementById("clock").textContent=new Date().toLocaleString();
 document.getElementById("cstat").textContent=`会话 ${d.counts.sessions} · 升级 ${d.counts.escalations}`;
 document.getElementById("ro").textContent=d.readonly?"只读(未注入总控身份)":"";
 cockpitReadonly=d.readonly;
 document.getElementById("cfgbar").innerHTML=d.configured?"":
  "<div style='background:#3a1d24;border-bottom:1px solid #6e2b36;padding:8px 14px'>"
  +"⚠ 还没配置: 总控助手没选定或编制是空的。<a style='color:#ffb454' href='/setup'>去配置页点选 →</a></div>";
 let bh="";
 for(const [k,label] of BUCKETS){
  const cards=(d.snapshot[k]||[]);
  bh+=`<div class="bucket"><h3>${label} (${cards.length})</h3>`+cards.map(cardHtml).join("")+"</div>"}
 document.getElementById("buckets").innerHTML=bh;
 let fh="";
 for(const a of d.approvals){
  const tag={plan:"计划确认",final:"最终确认",permission:"权限裁决"}[a.kind];
  const what=a.kind==="permission"?`#${a.ruling_id} ${esc(a.worker)}: ${esc(a.tool)}`:`#${a.task_id} ${esc(a.title)}`;
  fh+=`<div class="approval"><b>[${tag}]</b> ${what}
  <button onclick="approve('${a.kind}',${a.kind==="permission"?a.ruling_id:a.task_id},'approve')">✓批准</button>
  <button onclick="approve('${a.kind}',${a.kind==="permission"?a.ruling_id:a.task_id},'reject')">✗驳回</button></div>`}
 for(const e of d.escalations){
  fh+=`<div class="note ${e.recovered?"green":"red"}">${e.recovered?"✓已恢复":"⚠"} 任务#${e.task_id??"-"}: ${esc(e.reason)}</div>`}
 document.getElementById("flow").innerHTML=fh||"<small style='color:#567'>无待办/升级</small>";
 // 抽屉: 焦点在内则不重建(不吞勾选)
 const dr=document.getElementById("drawer");
 if(!dr.contains(document.activeElement)){
  const o=d.org;let oh=`<p>总控/架构师/裁判: <b>${esc(o.controller)}</b></p><table><tr><th>实例</th><th>壳</th><th>模型</th><th>key</th><th>模式</th></tr>`;
  for(const i of o.instances)oh+=`<tr><td>${esc(i.name)}</td><td>${esc(i.shell)}</td><td>${esc(i.model)}</td><td>${esc(i.key_name)}</td><td>${i.display_mode}</td></tr>`;
  oh+="</table><h4>壳条目</h4><table>"+Object.keys(o.shells).map(k=>`<tr><td>${esc(k)}</td><td><button onclick="delEntry('shell','${esc(k)}')">删</button></td></tr>`).join("")+"</table>";
  oh+="<h4>Key 条目</h4><table>"+Object.keys(o.keys).map(k=>`<tr><td>${esc(k)}</td><td><button onclick="delEntry('key','${esc(k)}')">删</button></td></tr>`).join("")+"</table>";
  oh+=`<h4>新增条目</h4><select id="ek"><option>shell</option><option>key</option></select>
  <input id="en" placeholder="名称"><input id="ed" placeholder='JSON 配置(可空)'>
  <button onclick="addEntry()">增(标待测试)</button>`;
  document.getElementById("org").innerHTML=oh}}
async function renderRegistry(){
 const box=document.getElementById("registry-list");
 const status=document.getElementById("registry-status");
 const btn=document.getElementById("registry-migrate");
 btn.disabled=cockpitReadonly;
 btn.textContent=cockpitReadonly?"只读":"初始化/迁移";
 try{
  const reg=await j("/api/integrations");
  const entries=reg.entries||[], migrations=reg.migrations||[];
  const pending=migrations.filter(x=>!x.migrated).length;
  status.textContent=pending
   ?`旧条目迁移: ${migrations.length-pending}/${migrations.length} 已迁移,${pending} 待处理`
   :`旧条目迁移: ${migrations.length} 条已就绪`;
  const groups={
   protocol:{title:"Protocols",items:entries.filter(x=>x.key.startsWith("integration_protocol:"))},
   provider:{title:"Providers",items:entries.filter(x=>x.key.startsWith("integration_provider:"))},
   shell:{title:"Shells",items:entries.filter(x=>x.key.startsWith("integration_shell:"))},
   credential:{title:"Credentials",items:entries.filter(x=>x.key.startsWith("credential:"))}};
  let h="";
  for(const [kind,group] of Object.entries(groups)){
   h+=`<div data-registry-partition="${kind}"><small>${group.title} (${group.items.length})</small>`;
   h+=group.items.length?"<table><tr><th>名称</th><th>配置</th><th>迁移</th></tr>":"";
   for(const item of group.items){
    const name=item.key.split(":",2)[1];
    let config="", migrated="-";
    if(kind==="protocol"){
     config=`${item.auth_style||""} · ${(item.model_discovery_paths||[]).length} 个发现端点`;
    }else if(kind==="provider"){
     config=`${item.protocol||""} · ${item.base_url||""}`;
     migrated=migrations.some(x=>x.target==="integration_provider:"&&x.name===item.credential_key&&x.migrated)?"已迁移":"登记";
    }else if(kind==="shell"){
     config=(item.protocols||[]).join(", ");
     migrated=migrations.some(x=>x.target==="integration_shell:"&&x.name===name&&x.migrated)?"已迁移":"登记";
    }else config=item.provider||"";
    h+=`<tr><td>${esc(name)}</td><td>${esc(config)}</td><td>${esc(migrated)}</td></tr>`}
   if(group.items.length)h+="</table>";
   else h+="<div class='muted'>空</div>";
   h+="</div>"}
  box.innerHTML=h;
 }catch(e){status.textContent="注册表读取失败"}}
async function migrateRegistry(){
 if(cockpitReadonly)return;
 const btn=document.getElementById("registry-migrate");
 btn.disabled=true;btn.textContent="迁移中…";
 const r=await j("/api/integrations/migrate",{method:"POST",
  headers:{"Content-Type":"application/json"},body:"{}"});
 if(r.error)alert(r.error);
 await renderRegistry()}
async function approve(kind,id,decision){
 const r=await j("/api/approve",{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({kind,decision,task_id:id,ruling_id:id})});
 if(r.error)alert(r.error);poll()}
// ---- 总控真会话对话面: #stream 渲染 claude 事件流,1.5s 轮询拿增量往上拼 ----
// 活性信号(dsh 式 live tail): 思考 token 状态行原地刷新、思维链 dim 展示、
// 工具调用单独成行、正文逐 token 打字机(stream_event 增量;assistant 整段兜底)
let ctrlNext=0,ctrlAssistantDiv=null,ctrlThinkDiv=null,ctrlStatusDiv=null,ctrlDelta=false;
function ctrlLine(cls){const d=document.createElement("div");if(cls)d.className=cls;
 document.getElementById("stream").appendChild(d);return d}
function ctrlStatus(txt){
 if(!ctrlStatusDiv)ctrlStatusDiv=ctrlLine();
 ctrlStatusDiv.innerHTML=`<small style="color:#567">${esc(txt)}</small>`}
function ctrlStatusOff(){if(ctrlStatusDiv){ctrlStatusDiv.remove();ctrlStatusDiv=null}}
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
    if(!ctrlAssistantDiv)ctrlAssistantDiv=ctrlLine("msg-a");
    ctrlAssistantDiv.appendChild(document.createTextNode(d.text));}
   else if(d.type==="thinking_delta"&&d.thinking){
    ctrlDelta=true;
    if(!ctrlThinkDiv){ctrlThinkDiv=ctrlLine();ctrlThinkDiv._t="";}
    ctrlThinkDiv._t+=d.thinking;
    ctrlThinkDiv.innerHTML="<small style='color:#567'>思维链: "
     +esc(ctrlThinkDiv._t)+"</small>";}
  }else if(ev.type==="content_block_start"&&(ev.content_block||{}).type==="tool_use"){
   ctrlStatusOff();
   const d=ctrlLine();d.innerHTML=`<small style="color:#8fa3c8">⚙ 正在执行: ${esc(ev.content_block.name||"")}</small>`;
   ctrlAssistantDiv=null;ctrlThinkDiv=null;}}
 else if(e.type==="assistant"){
  const blocks=(e.message&&e.message.content)||[];
  // 增量已渲染过的正文不重复拼;工具调用单独成行
  if(!ctrlDelta){
   const txt=blocks.filter(b=>b.type==="text").map(b=>b.text).join("");
   if(txt){ctrlStatusOff();
    if(!ctrlAssistantDiv)ctrlAssistantDiv=ctrlLine("msg-a");
    ctrlAssistantDiv.appendChild(document.createTextNode(txt));}}
  const thk=blocks.filter(b=>b.type==="thinking").map(b=>b.thinking).join("");
  if(thk&&!ctrlThinkDiv){const d=ctrlLine();
   d.innerHTML=`<small style="color:#567">思维链: ${esc(thk)}</small>`;}
  for(const b of blocks)if(b.type==="tool_use"){
   const d=ctrlLine();d.innerHTML=`<small style="color:#8fa3c8">⚙ 正在执行: ${esc(b.name||"")}</small>`;
   ctrlAssistantDiv=null;ctrlThinkDiv=null;}}
 else if(e.type==="result"){
  // 一轮收尾: muted 小结(耗时/cost),清活性状态
  ctrlAssistantDiv=null;ctrlThinkDiv=null;ctrlDelta=false;ctrlStatusOff();
  const sec=((e.duration_ms||0)/1000).toFixed(1);
  const cost=e.total_cost_usd!=null?(" · $"+e.total_cost_usd):"";
  const d=ctrlLine();d.innerHTML=`<small style="color:#567">⏱ ${sec}s${cost}</small>`;}
 else if(e.type==="system"&&e.subtype==="restart"){
  ctrlAssistantDiv=null;ctrlThinkDiv=null;ctrlDelta=false;ctrlStatusOff();
  const d=ctrlLine();d.innerHTML=`<small style="color:#ffb454">⟳ ${esc(e.note||"会话进程重启,上文丢了")}</small>`;}
 else if(e.type==="system"&&e.subtype==="error"){
  ctrlAssistantDiv=null;ctrlThinkDiv=null;ctrlDelta=false;ctrlStatusOff();
  const d=ctrlLine();
  d.innerHTML=`<div style="color:#ff6b6b;padding:8px;border:1px solid #6e2b36;border-radius:6px;background:#3a1d24;margin:4px 0">
   <b>总控出错:</b> ${esc(e.text||"未知错误")}</div>`;}
 // system 其余子类(init 等)过滤不显示
 st.scrollTop=st.scrollHeight}
async function pollCtrl(){
 try{const r=await j("/api/ctrl/events?after="+ctrlNext);
  ctrlNext=r.next;
  for(const e of r.events)renderCtrl(e)}catch(e){}}
async function sendMsg(){
 const el=document.getElementById("msg");const text=el.value;if(!text)return;
 if(/^(批准|驳回)\\s*(权限)?\\s*#?\\d+\\s*$/.test(text)){
  // 审批口令照旧走 /api/message 机械秒批
  const r=await j("/api/message",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text})});
  if(r.error)alert(r.error);else if(r.note)alert(r.note);}
 else{
  // 其余文本=跟总控说话: 本地先上墙+立刻亮状态行(不等首轮轮询),回复靠轮询拼
  ctrlAssistantDiv=null;ctrlThinkDiv=null;ctrlDelta=false;
  const d=ctrlLine("msg-u");d.innerHTML="<b>我:</b> ";
  d.appendChild(document.createTextNode(text));
  ctrlStatus("已发给总控,等它回应 …");
  const r=await j("/api/ctrl/send",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text})});
  if(r.error)alert(r.error);}
 el.value="";poll();pollCtrl()}
async function addEntry(){
 let data={};const raw=document.getElementById("ed").value;
 try{if(raw)data=JSON.parse(raw)}catch(e){alert("JSON 不合法");return}
 const r=await j("/api/entry",{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({kind:document.getElementById("ek").value,name:document.getElementById("en").value,data})});
 if(r.error)alert(r.error);poll()}
async function delEntry(kind,name){
 const r=await j("/api/entry/delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({kind,name})});
 if(r.error)alert(r.error);poll()}
async function detail(name){
 const r=await j("/api/instance/"+encodeURIComponent(name));
 alert(JSON.stringify(r.profile?{score:r.profile.score,notes:r.profile.notes}:r,null,1))}
async function poll(){try{render(await j("/api/state"))}catch(e){}}
document.getElementById("msg").addEventListener("keydown",e=>{if(e.key==="Enter")sendMsg()});
poll();pollCtrl();renderRegistry();setInterval(()=>{poll();pollCtrl();renderRegistry()},1500);
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
 fillShells();fillProviders();srcToggle("c");srcToggle("w");kimiHintToggle();render()})();
function kimiHintToggle(){
 const ctrl=S.controller;const kimiHint=el("kimi-login-hint");
 if(kimiHint&&ctrl)kimiHint.style.display=(ctrl.shell=="kimi"&&ctrl.source!="key")?"":"none";
 else if(kimiHint)kimiHint.style.display="none";}
</script></body></html>"""
