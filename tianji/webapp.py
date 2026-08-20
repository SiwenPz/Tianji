"""驾驶舱 Web 交互页(票 03,规格书 15 章/19.2): FastAPI+uvicorn,原生 JS 单页零构建。

- 数据=账本同源只读渲染(15.1);端口只绑回环,固定默认号,冲突顺延+1(18.2,沿用票 15)
- 布局四段+右侧抽屉(15.1 E 变体);1.5s 轮询,输入框/抽屉焦点让路(15.2 不抢输入)
- 审批双入口(15.3): 页面按钮 + 总控对话自然语言(批准/驳回),账本单一真源双向同步
- 审批卡三类: 计划确认/最终确认/权限裁决(本票只渲染入口;权限放行机械执行归票 10)
- 写操作(审批/强制干预/条目增删)须注入总控身份(env TIANJI_WORKER_ID/TIANJI_SECRET),
  未注入则页面只读
"""

from __future__ import annotations

import json
import os
import re

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import cockpit, ops, permission
from .db import connect

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
        return {
            "snapshot": snap,
            "approvals": _approvals(conn),
            "escalations": _escalations(conn),
            "stream": _stream(conn),
            "org": _org(conn),
            "readonly": _require_controller(conn) is None,
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


# ---------------------------------------------------------------- 页面(零构建单页)

@app.get("/", response_class=HTMLResponse)
def index():
    return _PAGE


_PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>天机驾驶舱</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;background:#0f1420;color:#dde3ee;font-size:13px}
#topbar{display:flex;gap:16px;align-items:center;background:#161d2e;padding:8px 14px;border-bottom:1px solid #28314a}
#topbar b{font-size:15px}
#buckets{display:flex;gap:8px;padding:8px}
.bucket{flex:1;background:#161d2e;border-radius:8px;padding:8px;min-height:120px}
.bucket h3{margin:0 0 6px;font-size:13px;color:#8fa3c8}
.card{background:#1f2940;border-radius:6px;padding:6px 8px;margin-bottom:6px;cursor:pointer}
.card.stale{opacity:.5}.card .esc{color:#ff6b6b;font-weight:bold}
.card .unread{font-weight:bold}
#flow{padding:0 8px}
.approval{background:#24311f;border:1px solid #3d5a2e;border-radius:6px;padding:8px;margin-bottom:6px;display:flex;gap:8px;align-items:center}
.approval button{cursor:pointer}
.note{padding:6px 8px;border-radius:6px;margin-bottom:6px}
.note.red{background:#3a1d24;border:1px solid #6e2b36}
.note.green{background:#1d3a24;border:1px solid #2b6e36}
#pane{display:flex;flex-direction:column;padding:8px;flex:1}
#stream{flex:1;overflow-y:auto;background:#161d2e;border-radius:8px;padding:8px;min-height:140px;max-height:40vh}
#stream div{padding:2px 0;border-bottom:1px solid #1d2740}
#inputrow{display:flex;gap:8px;margin-top:8px}
#msg{flex:1;background:#0f1420;color:#dde3ee;border:1px solid #28314a;border-radius:6px;padding:8px}
#drawer{position:fixed;right:-380px;top:0;width:360px;height:100%;background:#161d2e;border-left:1px solid #28314a;transition:right .2s;padding:12px;overflow-y:auto;z-index:9}
#drawer.open{right:0}
#drawer table{border-collapse:collapse;width:100%}
#drawer td,#drawer th{border:1px solid #28314a;padding:4px 6px;font-size:12px}
button{background:#2b3a5e;color:#dde3ee;border:0;border-radius:5px;padding:4px 10px}
</style></head><body>
<div id="topbar"><b>天机驾驶舱</b><span id="clock"></span><span id="cstat"></span>
<span id="ro" style="color:#ffb454"></span>
<span style="flex:1"></span><button onclick="toggleDrawer()">角色/条目</button></div>
<div id="buckets"></div>
<div id="flow"></div>
<div id="pane"><div id="stream"></div>
<div id="inputrow"><input id="msg" placeholder="批准 16 / 驳回 16 / 批准权限 3 …(回车发送)">
<button onclick="sendMsg()">发送</button></div></div>
<div id="drawer"><h3>角色编排与条目</h3><div id="org"></div></div>
<script>
const BUCKETS=[["attention","attention(待处理)"],["working","working(进行中)"],["done","done(已结算)"],["idle","idle(空闲)"]];
let drawerOpen=false;
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
 // 总控窗格: 消息流更新不碰输入框(15.2 输入让路)
 let sh="";
 for(const m of d.stream){sh+=`<div><small>#${m.seq} ${esc(m.sender)} ${esc(m.type)}</small> ${esc(m.reason)}</div>`}
 document.getElementById("stream").innerHTML=sh;
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
async function approve(kind,id,decision){
 const r=await j("/api/approve",{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({kind,decision,task_id:id,ruling_id:id})});
 if(r.error)alert(r.error);poll()}
async function sendMsg(){
 const el=document.getElementById("msg");const text=el.value;if(!text)return;
 const r=await j("/api/message",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text})});
 if(r.error)alert(r.error);else if(r.note)alert(r.note);
 el.value="";poll()}
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
poll();setInterval(()=>{poll()},1500);
</script></body></html>"""
