"""驾驶舱只读快照(15.5/15.1): 4桶分栏渲染+升级可见,纯只读。

为票03 Web页面复用,CLI命令只是薄壳。
不注册消费者,不推进游标,无任何写账本入口。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import sqlite3

from .db import now

BUCKET_ORDER = ("attention", "working", "done", "idle")

TAG_UPGRADE = "【升级】"
TASK_STATUS_POINT = {
    "new": "●",
    "discussing": "◐",
    "awaiting_plan_confirm": "◐",
    "awaiting_final_confirm": "◐",
    "dispatched": "◉",
    "executing": "◉",
    "reviewing": "◉",
    "archived": "◆",
    "reopened": "◆",
}


def _utcfromtimestamp(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _relative(ts: int, current: int) -> str:
    diff = max(current - ts, 0)
    if diff < 60:
        return f"{diff}s前"
    if diff < 3600:
        return f"{diff // 60}m前"
    if diff < 86400:
        return f"{diff // 3600}h前"
    return f"{diff // 86400}d前"


def _task_status_point(status: str) -> str:
    return TASK_STATUS_POINT.get(status, "●")


def _session_state_for_instance(conn: sqlite3.Connection, instance_name: str) -> dict:
    row = conn.execute(
        "SELECT session_id, state, last_seq, updated_at FROM session_states "
        "WHERE instance_name=? ORDER BY updated_at DESC LIMIT 1",
        (instance_name,),
    ).fetchone()
    return dict(row) if row else {}


def _latest_event(conn: sqlite3.Connection, instance_name: str) -> dict:
    row = conn.execute(
        "SELECT seq, ts, payload FROM messages "
        "WHERE type='event' AND sender=? ORDER BY seq DESC LIMIT 1",
        (instance_name,),
    ).fetchone()
    if not row:
        return {}
    try:
        payload = json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        payload = {"event_type": "event"}
    return {"seq": row["seq"], "ts": row["ts"], "payload": payload}


def _latest_message(conn: sqlite3.Connection, task_id: int) -> dict:
    row = conn.execute(
        "SELECT seq, ts, type, sender, payload FROM messages "
        "WHERE type!='event' AND json_extract(payload, '$.task_id')=? "
        "ORDER BY seq DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if not row:
        return {}
    try:
        payload = json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        payload = {}
    return {
        "seq": row["seq"],
        "ts": row["ts"],
        "type": row["type"],
        "sender": row["sender"],
        "payload": payload,
    }


def _latest_user_message(conn: sqlite3.Connection, instance_name: str) -> dict:
    """最近一条 user_prompt 事件(用于渲染用户最后输入)。"""
    row = conn.execute(
        "SELECT seq, ts, payload FROM messages "
        "WHERE sender=? AND type='event' AND json_extract(payload, '$.event_type')='user_prompt' "
        "ORDER BY seq DESC LIMIT 1",
        (instance_name,),
    ).fetchone()
    if not row:
        return {}
    try:
        payload = json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        return {}
    inner = payload.get("payload") or {}
    return {"seq": row["seq"], "ts": row["ts"], "type": "user_prompt", "payload": inner}


def _latest_tool_message(conn: sqlite3.Connection, instance_name: str) -> dict:
    """最近一条 pre_tool_use 事件(用于渲染最后工具操作)。"""
    row = conn.execute(
        "SELECT seq, ts, payload FROM messages "
        "WHERE sender=? AND type='event' AND json_extract(payload, '$.event_type')='pre_tool_use' "
        "ORDER BY seq DESC LIMIT 1",
        (instance_name,),
    ).fetchone()
    if not row:
        return {}
    try:
        payload = json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        return {}
    inner = payload.get("payload") or {}
    return {"seq": row["seq"], "ts": row["ts"], "type": "pre_tool_use", "payload": inner}


def _has_escalation(conn: sqlite3.Connection, task_id: int) -> bool:
    row = conn.execute(
        "SELECT COUNT(1) AS n FROM messages WHERE type='escalation' "
        "AND json_extract(payload, '$.task_id')=?",
        (task_id,),
    ).fetchone()
    return (row["n"] if row else 0) > 0


def _escalation_summary(conn: sqlite3.Connection, task_id: int) -> str:
    rows = conn.execute(
        "SELECT payload FROM messages WHERE type='escalation' "
        "AND json_extract(payload, '$.task_id')=? ORDER BY seq LIMIT 5",
        (task_id,),
    ).fetchall()
    summaries = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
            reason = payload.get("reason", "")
            if reason:
                summaries.append(reason)
        except (json.JSONDecodeError, TypeError):
            pass
    return " | ".join(summaries)


def _current_tool(conn: sqlite3.Connection, instance_name: str, event: dict) -> str:
    """当前工具/状态标签: 事件 payload 嵌套,优先取最近一条非 escalation 消息。"""
    row = conn.execute(
        "SELECT type, payload FROM messages WHERE sender=? AND type!='event' "
        "AND type!='escalation' ORDER BY seq DESC LIMIT 1",
        (instance_name,),
    ).fetchone()
    if row:
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            payload = {}
        tool = payload.get("tool_name")
        if tool:
            return tool
        content = payload.get("content")
        if content:
            return content
    inner = (event.get("payload") or {}).get("payload") or {}
    tool = inner.get("tool_name")
    if tool:
        return tool
    content = inner.get("content")
    if content:
        return content
    event_type = (event.get("payload") or {}).get("event_type") or event.get("event_type")
    if event_type == "session_start":
        return "会话启动"
    if event_type == "session_end":
        return "会话结束"
    if event_type:
        return event_type
    return "待命中"


def _classify(dispatch_status: str, session_state: str,
              has_dispatch: bool = True) -> str:
    if not has_dispatch:
        return "idle"
    # 已结算派单/会话 → done
    if dispatch_status == "done" or session_state == "done":
        return "done"
    # 活跃工作 → working
    if session_state == "working" or dispatch_status == "active":
        return "working"
    # 无活(派单终结且会话空闲) → idle
    if dispatch_status in {"stale", "requeue", "escalate"} and session_state == "idle":
        return "idle"
    # 需要人看 → attention
    return "attention"

def snapshot(conn: sqlite3.Connection) -> dict:
    current = now()
    buckets = {bucket: [] for bucket in BUCKET_ORDER}

    latest_event_by_instance = {}
    display_modes = {r[0]: r[1] for r in conn.execute(
        "SELECT name, display_mode FROM instances").fetchall()}
    quota_pcts = {}
    for _r in conn.execute(
            "SELECT key, value FROM configs WHERE key LIKE 'quota:%'").fetchall():
        try:
            quota_pcts[_r[0][6:]] = json.loads(_r[1]).get("context_pct")
        except Exception:
            pass
    for row in conn.execute(
        "SELECT sender, MAX(seq) AS seq FROM messages WHERE type='event' GROUP BY sender"
    ).fetchall():
        ev = _latest_event(conn, row["sender"])
        if ev:
            latest_event_by_instance[row["sender"]] = ev

    latest_message_by_task = {}
    for row in conn.execute(
        "SELECT json_extract(payload, '$.task_id') AS task_id, MAX(seq) AS seq "
        "FROM messages WHERE type!='event' AND json_extract(payload, '$.task_id') IS NOT NULL "
        "GROUP BY json_extract(payload, '$.task_id')"
    ).fetchall():
        msg = _latest_message(conn, row["task_id"])
        if msg:
            latest_message_by_task[row["task_id"]] = msg

    dispatches = conn.execute(
        "SELECT d.id, d.task_id, d.worker_id, d.status AS dispatch_status, "
        "d.worker_role, d.created_at AS dispatch_created_at, d.expect_min, "
        "t.title AS task_title, t.status AS task_status, t.description AS task_description "
        "FROM dispatches d JOIN tasks t ON t.id=d.task_id "
        "ORDER BY d.id DESC"
    ).fetchall()

    seen_instances = set()
    for dispatch in dispatches:
        dispatch = dict(dispatch)
        # 票 02 残留②(票 03 修): requeue/cancelled=被取代/已取消的历史派单,
        # 不出卡(其 session 滞留 working/waiting 时会假"进行中");无活实例落 idle
        if dispatch["dispatch_status"] in ("requeue", "cancelled"):
            continue
        instance_name = dispatch["worker_id"]
        seen_instances.add(instance_name)
        session = _session_state_for_instance(conn, instance_name)
        event = latest_event_by_instance.get(instance_name, {})
        message = latest_message_by_task.get(dispatch["task_id"], {})
        event_message = message if message.get("type") else (event or {})
        last_message = event_message
        last_message_ts = event_message.get("ts")
        if last_message_ts is None:
            last_message_ts = current
        bucket = _classify(
            dispatch["dispatch_status"],
            session.get("state", "idle"),
        )
        card = {
            "instance_name": instance_name,
            "display_mode": display_modes.get(instance_name, ""),
            "quota_pct": quota_pcts.get(instance_name),
            "model": (dict(conn.execute(
                "SELECT model FROM instances WHERE name=?",
                (instance_name,)).fetchone() or {}) or {}).get("model", ""),
            "dispatch_id": dispatch["id"],
            "task_id": dispatch["task_id"],
            "task_title": dispatch.get("task_title") or "",
            "status_point": _task_status_point(dispatch["task_status"]),
            "current_tool": _current_tool(conn, instance_name, event or last_message or {}),
            "last_message": last_message,
            "last_user_message": _latest_user_message(conn, instance_name),
            "last_tool_message": _latest_tool_message(conn, instance_name),
            "last_message_ts": last_message_ts,
            "relative_time": _relative(last_message_ts, current),
            "bucket": bucket,
            "has_escalation": _has_escalation(conn, dispatch["task_id"]),
            "escalation_summary": _escalation_summary(conn, dispatch["task_id"]),
            "session_state": session.get("state", "idle"),
            "dispatch_status": dispatch["dispatch_status"],
            "task_status": dispatch["task_status"],
        }
        buckets[bucket].append(card)
    for instance in conn.execute("SELECT name FROM instances ORDER BY created_at").fetchall():
        instance_name = instance["name"]
        if instance_name in seen_instances:
            continue
        session = _session_state_for_instance(conn, instance_name)
        event = latest_event_by_instance.get(instance_name, {})
        last_message_ts = event.get("ts") if event.get("ts") is not None else current
        bucket = _classify(
            None, session.get("state", "idle"), has_dispatch=False,
        )
        card = {
            "instance_name": instance_name,
            "display_mode": display_modes.get(instance_name, ""),
            "quota_pct": quota_pcts.get(instance_name),
            "model": (dict(conn.execute(
                "SELECT model FROM instances WHERE name=?",
                (instance_name,)).fetchone() or {}) or {}).get("model", ""),
            "dispatch_id": None,
            "task_id": None,
            "task_title": "(空闲)",
            "status_point": "○",
            "current_tool": _current_tool(conn, instance_name, event or {"event_type": "待命中"}),
            "last_message": {
                "type": "event",
                "payload": event.get("payload")
                or {"event_type": event.get("event_type", "待命中")},
            },
            "last_message_ts": last_message_ts,
            "relative_time": _relative(last_message_ts, current),
            "bucket": bucket,
            "has_escalation": False,
            "escalation_summary": "",
            "session_state": session.get("state", "idle"),
            "dispatch_status": None,
            "task_status": "idle",
        }
        buckets[bucket].append(card)

    for cards in buckets.values():
        cards.sort(key=lambda card: (
            0 if card["has_escalation"] else 1,
            -(card["last_message_ts"] or 0),
            card["instance_name"],
        ))
    pools = _build_pool_summary(conn)
    result = dict(buckets)
    result["pools"] = pools
    return result


def _build_pool_summary(conn: sqlite3.Connection) -> list:
    """构建池摘要: 每池名/成员数/熔断中成员数/各成员健康状态。"""
    summaries = []
    pool_rows = conn.execute(
        "SELECT key, value FROM configs "
        "WHERE key LIKE 'pool:%' AND key NOT LIKE 'pool:token:%' "
        "ORDER BY key"
    ).fetchall()
    for p in pool_rows:
        name = p["key"][len("pool:"):]
        try:
            cfg = json.loads(p["value"])
        except (json.JSONDecodeError, TypeError):
            continue
        raw_members = cfg.get("members", []) or []
        circuit = cfg.get("circuit", {}) or {}
        members_info = []
        circuit_open = 0
        for m in raw_members:
            cb_state = "closed"
            if m in circuit and isinstance(circuit[m], dict):
                cb_state = circuit[m].get("state", "closed")
            if cb_state == "open":
                circuit_open += 1
            hrow = conn.execute(
                "SELECT consecutive_failures, last_error, "
                "last_success_at, last_failure_at "
                "FROM pool_member_health WHERE pool_name=? AND member_name=?",
                (name, m)).fetchone()
            members_info.append({
                "name": m, "circuit": cb_state,
                "consecutive_failures": hrow["consecutive_failures"] if hrow else 0,
                "last_error": hrow["last_error"] if hrow else "",
            })
        summaries.append({
            "name": name, "member_count": len(raw_members),
            "circuit_open_count": circuit_open, "members": members_info,
        })
    return summaries





def render_snapshot(snapshot: dict, extra_blocks: list | None = None) -> str:
    current = now()
    header_time = _utcfromtimestamp(current).strftime("%Y-%m-%d %H:%M:%S")
    upgrade_count = 0
    card_count = 0
    for key, cards in snapshot.items():
        if key == "pools":
            continue
        for card in cards:
            card_count += 1
            if card.get("has_escalation"):
                upgrade_count += 1

    lines = [
        "天机驾驶舱只读快照",
        f"时间: {header_time}  卡片: {card_count}  升级: {upgrade_count}",
        "",
    ]

    bucket_labels = {
        "attention": "attention(待处理)",
        "working": "working(进行中)",
        "done": "done(已结算)",
        "idle": "idle(空闲)",
    }
    column_headers = ["状态", "实例", "模型/壳", "任务/派单", "当前工具", "最后消息/相对时间"]
    column_widths = [6, 12, 16, 22, 16, 34]
    def _row(cells):
        return " | ".join(str(cell).ljust(width) for cell, width in zip(cells, column_widths))

    for bucket in BUCKET_ORDER:
        cards = snapshot.get(bucket, [])
        lines.append(f"## {bucket_labels.get(bucket, bucket)} ({len(cards)})")
        if not cards:
            lines.append("(空)")
            lines.append("")
            continue
        lines.append(_row(column_headers))
        lines.append(_row(["―" * width for width in column_widths]))
        for card in cards:
            label = card["instance_name"]
            if card["has_escalation"]:
                label = TAG_UPGRADE + label
            if card.get("display_mode") == "后台":
                label = label + "·后台"
            last_message = card.get("last_message") or {}
            user_message = card.get("last_user_message") or {}
            parts = []
            if card.get("escalation_summary"):
                parts.append(card["escalation_summary"])
            if user_message.get("type") == "user_prompt":
                payload = user_message.get("payload") or {}
                content = payload.get("content")
                if content:
                    parts.append(content)
            if not parts:
                if last_message.get("type") == "event":
                    payload = last_message.get("payload") or {}
                    parts.append(payload.get("event_type") or "event")
                else:
                    parts.append(last_message.get("reason") or last_message.get("type") or str(last_message))
            reason = " | ".join(parts)
            if len(reason) > 32:
                reason = reason[:29] + "..."
            dispatch_label = f"#{card['dispatch_id']}" if card.get("dispatch_id") else "(空闲)"
            task_title = card.get("task_title") or "(空闲)"
            if len(task_title) > 18:
                task_title = task_title[:15] + "..."
            tool = card.get("current_tool") or "待命中"
            if len(tool) > 14:
                tool = tool[:11] + "..."
            lines.append(_row([
                card["status_point"],
                label,
                card.get("model") or "—",
                f"{task_title} / {dispatch_label}",
                tool,
                f"{reason} @{card['relative_time']}",
            ]))
        lines.append("")

    # 插件展示块(21.3 视图类,票 23): 只读渲染,fail-open
    if extra_blocks:
        lines.append("## 插件展示块")
        lines.extend(extra_blocks)
        lines.append("")

    # 票 57: 池摘要
    pool_data = snapshot.get("pools")
    if pool_data:
        lines.append("## 号池摘要")
        lines.append("")
        for pool in pool_data:
            status_tag = ""
            if pool["circuit_open_count"] > 0:
                status_tag = f" ⚠ 熔断中{pool['circuit_open_count']}/{pool['member_count']}"
            lines.append(f"### {pool['name']}{status_tag}")
            for m in pool["members"]:
                dot = {"closed": "●", "open": "⚠", "half_open": "◐"}.get(
                    m["circuit"], "●")
                fail_note = f"(连续 FAIL {m['consecutive_failures']})" if m.get("consecutive_failures") else ""
                lines.append(
                    f"  {dot} {m['name']} {fail_note}")
            lines.append("")

    return "\n".join(lines).strip() + "\n"
