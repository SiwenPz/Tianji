"""事件流(6.3/6.5): ingest-event 契约+归一化事件+派生状态+登记行回填+开工证据。

单事务写(事件行+派生状态+联动),校验三件套(身份=启动器 env 注入,防冒名)。
事件不带 task_id(会话域证据,任务关联由监控器按派单时间窗对账)。
"""

import hashlib
import json
import sqlite3

from . import messages
from .db import now, tx
from .ops import _activate_dispatch
from .schema import EVENT_TYPES, SESSION_STATES


class EventError(ValueError):
    pass


def _derive_state(event_type: str, is_interrupt: bool, old: str) -> str:
    """6.3 派生四态: 按事件类型映射;乱序旧事件不覆盖由调用方保证。"""
    if event_type == "session_start":
        return "idle"
    if event_type == "session_end":
        return "done"
    if event_type == "stop":
        return "working" if is_interrupt else "waiting"
    if event_type == "permission_request":
        return "waiting"
    return "working"  # user_prompt / pre_tool_use / post_tool_use / subagent_*


def _verify_identity_secret(conn, worker_id: str, secret: str) -> bool:
    """校验事件发送者的 secret 与账本记录匹配(防冒名,6.5)。

    - 总控身份: 走 auth.check_controller()。
    - 工人身份: 查该 worker 最新派单(不限状态)的 dcap_hash,恒定时间比较;
      无派单时查最新登记行的 dcap_hash;都无则拒绝。
    """
    from . import auth
    # 总控身份: 先用全局 controller 凭据校验。总控也可被派为 worker/reviewer,
    # 此时启动器注入的是派单 secret,因此校验失败后继续走下方派单凭据兜底。
    if worker_id == "总控":
        if auth.check_controller(conn, {"worker_id": worker_id, "secret": secret}):
            return True
    # 工人身份: 查最新派单(不限状态,覆盖活跃和刚结算完的 worker)
    row = conn.execute(
        "SELECT dcap_hash FROM dispatches WHERE worker_id=?"
        " ORDER BY id DESC LIMIT 1", (worker_id,)).fetchone()
    if row is not None:
        return auth.constant_time_eq(auth.secret_hash(secret), row["dcap_hash"])
    # 兜底: 查最新登记行(转录解析等场景,派单可能已清理)
    reg = conn.execute(
        "SELECT dcap_hash FROM instance_registrations WHERE instance_name=?"
        " ORDER BY id DESC LIMIT 1", (worker_id,)).fetchone()
    if reg is not None:
        return auth.constant_time_eq(auth.secret_hash(secret), reg["dcap_hash"])
    return False  # 什么都查不到,拒绝


def ingest_event(conn: sqlite3.Connection, env: dict, event: dict) -> dict:
    """归一化事件写入(6.5 契约,stdin 单行 JSON 的解析层)。"""
    import os
    from . import auth
    ident = auth.require_identity(env)
    # 防冒名(6.5): 校验 secret 摘要与派单 dcap_hash 匹配,env 中任意非空 secret 不能通过
    if not _verify_identity_secret(conn, ident["worker_id"], ident["secret"]):
        raise EventError(f"身份校验失败(secret 不匹配,worker_id={ident['worker_id']})")
    event_type = event.get("event_type")
    if event_type not in EVENT_TYPES:
        raise EventError(f"非法事件类型: {event_type}(应为 8 类公共交集)")
    session_id = str(event.get("session_id") or "")
    if not session_id:
        raise EventError("事件缺 session_id")
    payload = event.get("payload") or {}
    is_interrupt = bool(event.get("is_interrupt", False))
    worker_id = ident["worker_id"]

    with tx(conn) as c:
        msg = messages.send(
            c, "event", worker_id,
            {"worker_id": worker_id, "session_id": session_id,
             "event_type": event_type, "payload": payload,
             "is_interrupt": is_interrupt},
            None,  # 事件不寻址只审计
        )
        seq = msg["seq"]
        # 派生状态: 按 seq 单调,乱序旧事件不覆盖(6.3)
        row = c.execute(
            "SELECT state, last_seq FROM session_states WHERE session_id=?",
            (session_id,)).fetchone()
        if row is None or seq > row["last_seq"]:
            new_state = _derive_state(event_type, is_interrupt,
                                      row["state"] if row else None)
            c.execute(
                "INSERT INTO session_states (session_id, instance_name, state,"
                " last_seq, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET instance_name=excluded.instance_name,"
                " state=excluded.state, last_seq=excluded.last_seq, updated_at=excluded.updated_at",
                (session_id, worker_id, new_state, seq, now()))
        # 登记行回填(11.1/11.3): session_start 验证启动, session_end 关闭
        if event_type == "session_start":
            # 先尝试更新已 spawn 的行(正常派单流程);若无则回填新行(转录解析)
            cur = c.execute(
                "UPDATE instance_registrations SET status='active', session_id=?"
                " WHERE instance_name=? AND status='spawned' AND session_id IS NULL",
                (session_id, worker_id))
            if cur.rowcount == 0:
                dcap = hashlib.sha256(
                    f"{worker_id}:{session_id}".encode()).hexdigest()
                c.execute(
                    "INSERT INTO instance_registrations"
                    " (instance_name, status, session_id, dcap_hash, task_path,"
                    " created_at) VALUES (?, 'active', ?, ?, '', ?)",
                    (worker_id, session_id, dcap, now()))
        elif event_type == "session_end":
            c.execute(
                "UPDATE instance_registrations SET status='closed', closed_at=?"
                " WHERE instance_name=? AND status='active'",
                (now(), worker_id))
        # 开工证据(5.1): 第一次工具调用 → 派单 active + 任务 executing(联动)
        if event_type == "pre_tool_use":
            _activate_dispatch(c, worker_id)
        # 权限请求归一化(6.6,票 10): 进账本待裁决,决策入口唯一=总控
        if event_type == "permission_request":
            from . import permission
            permission.record_request(
                c, worker_id, session_id,
                str(payload.get("tool") or payload.get("tool_name") or ""),
                payload)
        return {"seq": seq, "session_id": session_id, "event_type": event_type}


def ingest_event_line(conn: sqlite3.Connection, env: dict, line: str) -> dict:
    """ingest-event 契约: 单行 JSON。"""
    line = line.strip()
    if not line:
        raise EventError("空行")
    try:
        event = json.loads(line)
    except json.JSONDecodeError as e:
        raise EventError(f"非法 JSON: {e}")
    return ingest_event(conn, env, event)


def latest_events(conn: sqlite3.Connection, worker_id: str,
                  after_seq: int = 0, limit: int = 50) -> list:
    rows = conn.execute(
        "SELECT seq, ts, type, payload FROM messages "
        "WHERE type='event' AND sender=? AND seq>? ORDER BY seq DESC LIMIT ?",
        (worker_id, after_seq, limit),
    ).fetchall()
    return [{"seq": r["seq"], "ts": r["ts"],
             **json.loads(r["payload"])} for r in rows]
