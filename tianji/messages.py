"""消息协议(3.1/3.2/3.3): 16 种三族+recipient_role 寻址+独立游标+幂等回执。

任务状态迁移由 CLI 转换操作写审计行,不重复造消息(小类型集+审计分离)。
"""

import json
import sqlite3

from .schema import MSG_TYPES, ROLES

# 消息类型 → 合法收件角色组合(DB CHECK 兜底,主校验在这层)
MSG_RECIPIENTS = {
    "task_suggest": ("controller",),      # 助手→总控(建议任务,不能建 new)
    "dispatch": ("worker", "reviewer"),   # 分配器→实施者/审核者
    "worker_done": ("controller", "allocator"),  # 实施者→总控/分配器(唯一权威完成信号)
    "review_verdict": ("controller", "allocator"),  # 审核者→总控/分配器
    "escalation": ("controller",),        # 监控器/分配器→总控
    "grill_round": ("controller",),       # 架构师↔总控
    "grill_answer": ("controller",),
    "plan_confirm": ("architect",),       # 总控→架构师
    "plan_reject": ("architect",),
    "final_confirm": ("controller",),     # 总控(转换操作,消息存档)
    "reopen": ("controller",),
    "instance_register": None,            # 注册动作,不寻址
    "instance_unbind": None,
    "event": None,                        # 事件行,不寻址只审计(6.3)
    "architect_verdict": ("controller",), # 架构师裁判→总控(8.2)
    "architect_confirm": ("controller",), # 架构师二次确认→总控(8.2)
    "worker_help": ("controller",),       # 实施者→总控(工人求助 5.6)
    "worker_help_reply": ("worker",),     # 总控→实施者(求助答复 5.6)
    "alloc_review": ("controller",),      # 分配器→总控(可选总控评估 9.2③: 候选+任务特征)
    "alloc_review_result": ("allocator",),  # 总控评估结果→分配器(写账本参与排序)
}


class ProtocolError(Exception):
    pass


def validate_message(type_: str, recipient_role):
    if type_ not in MSG_TYPES:
        raise ProtocolError(f"非法消息类型: {type_}")
    allowed = MSG_RECIPIENTS[type_]
    if allowed is None:
        if recipient_role is not None:
            raise ProtocolError(f"{type_} 不寻址,recipient_role 必须为空")
    elif recipient_role not in allowed:
        raise ProtocolError(
            f"{type_} 的收件人只能为 {allowed},收到 {recipient_role}"
        )


def send(conn: sqlite3.Connection, type_: str, sender: str,
         payload: dict, recipient_role=None, ts: int = None) -> dict:
    """写消息行;返回回执 {seq, type, recipient_role}。"""
    validate_message(type_, recipient_role)
    if ts is None:
        from .db import now
        ts = now()
    cur = conn.execute(
        "INSERT INTO messages (ts, type, sender, recipient_role, payload) "
        "VALUES (?,?,?,?,?)",
        (ts, type_, sender, recipient_role, json.dumps(payload, ensure_ascii=False)),
    )
    seq = cur.lastrowid
    # 求助记录入实例档案(9.1): worker_help 同步更新 ability_profiles
    if type_ == "worker_help":
        claim = payload.get("claim", "") if isinstance(payload, dict) else ""
        conn.execute(
            "UPDATE ability_profiles SET help_count=help_count+1,"
            " last_help_at=?, last_help_claim=? WHERE instance_name=?",
            (ts, claim, sender))
    return {"seq": seq, "type": type_, "recipient_role": recipient_role}


def check_unread(conn: sqlite3.Connection, consumer_id: str, role: str,
                 limit: int = 100) -> list:
    """读未读(游标之后 + 收件角色匹配);不推进游标(显式 ack)。"""
    row = conn.execute("SELECT last_seq FROM cursors WHERE consumer_id=?", (consumer_id,)).fetchone()
    last = row["last_seq"] if row else 0
    rows = conn.execute(
        "SELECT seq, ts, type, sender, recipient_role, payload FROM messages "
        "WHERE seq > ? AND recipient_role IS NOT NULL AND recipient_role = ? "
        "ORDER BY seq LIMIT ?",
        (last, role, limit),
    ).fetchall()
    return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]


def ack(conn: sqlite3.Connection, consumer_id: str, up_to_seq: int) -> dict:
    """显式 ack: 单事务推进游标。"""
    row = conn.execute("SELECT last_seq FROM cursors WHERE consumer_id=?", (consumer_id,)).fetchone()
    last = row["last_seq"] if row else 0
    if up_to_seq <= last:
        return {"consumer_id": consumer_id, "last_seq": last, "already": True}
    conn.execute(
        "INSERT INTO cursors (consumer_id, last_seq) VALUES (?,?) "
        "ON CONFLICT(consumer_id) DO UPDATE SET last_seq=excluded.last_seq",
        (consumer_id, up_to_seq),
    )
    return {"consumer_id": consumer_id, "last_seq": up_to_seq}


def idempotent(conn: sqlite3.Connection, request_id: str, operation: str,
               fn) -> dict:
    """幂等回执(3.3): request_id PK,重放返回原回执不重复执行。

    必须在调用方的事务内使用;fn 为真正执行的回执产出函数。
    """
    row = conn.execute(
        "SELECT result FROM receipts WHERE request_id=?", (request_id,)
    ).fetchone()
    if row is not None:
        return {"replay": True, **json.loads(row["result"])}
    result = fn()
    conn.execute(
        "INSERT INTO receipts (request_id, operation, result) VALUES (?,?,?)",
        (request_id, operation, json.dumps(result, ensure_ascii=False)),
    )
    return result
