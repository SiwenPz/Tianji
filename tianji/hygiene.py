"""空闲发现与上下文卫生(票 12,规格书 14.3/14.4/14.5)。

- 空闲=派活调度输入(非回收): 软排序加分在 ops.allocator_pick(方案 A)
- 换活必打扫: 派活前检查候选工人上次已结算派单的任务是否已归档;未归档且
  与本次任务不相关(机械判定=任务不同)→先机械打扫(摘要落账本+登记行标
  "已打扫")再派活;摘要=账本结构化记录(工作痕迹真源=文件系统+账本,
  摘要质量降级无损);续接=同任务滚动,同套机制、触发场景不同
- 回收≠删实例: unbind 下线保留启动器/画像/表现分,随时可复活重启
"""

from __future__ import annotations

import json

from . import ops
from .db import now


def needs_cleanup(conn, worker_id: str, new_task_id: int) -> dict | None:
    """换活打扫判定(14.4): 返回 None=无需打扫,否则带上次任务信息。

    "不相关"机械判定: 上次已结算派单的任务 ≠ 本次任务(同任务=续接,不打扫)。
    """
    row = conn.execute(
        "SELECT d.id AS did, d.task_id AS tid, t.status AS tstatus,"
        " t.title AS title, d.payload AS payload"
        " FROM dispatches d JOIN tasks t ON t.id=d.task_id"
        " WHERE d.worker_id=? AND d.status='done'"
        " ORDER BY d.id DESC LIMIT 1", (worker_id,)).fetchone()
    if row is None:
        return None
    if row["tid"] == new_task_id:
        return None  # 同任务续接
    if row["tstatus"] == "archived":
        return None  # 上次任务已归档=已卫生
    return {"last_dispatch_id": row["did"], "last_task_id": row["tid"],
            "last_task_title": row["title"], "last_task_status": row["tstatus"],
            "payload": row["payload"]}


def cleanup(conn, worker_id: str, new_task_id: int,
            reason: str = "换活打扫") -> dict:
    """机械打扫(14.4): 摘要落账本+登记行标"已打扫"(cleaned_at)。"""
    info = needs_cleanup(conn, worker_id, new_task_id)
    if info is None:
        return {"cleaned": False, "reason": "无需打扫"}
    payload = json.loads(info["payload"]) if info["payload"] else {}
    summary = {
        "last_task_id": info["last_task_id"],
        "last_task_title": info["last_task_title"],
        "last_task_status": info["last_task_status"],
        "last_dispatch_id": info["last_dispatch_id"],
        "report_path": payload.get("task_dir", ""),
    }
    # 登记行标"已打扫"(摘要落账本,会话记忆只是缓存)
    conn.execute(
        "UPDATE instance_registrations SET cleaned_at=?"
        " WHERE instance_name=? AND dispatch_id=?",
        (now(), worker_id, info["last_dispatch_id"]))
    ops.audit(conn, "hygiene_clean",
              {"worker_id": worker_id, "reason": reason, "summary": summary})
    return {"cleaned": True, "summary": summary}
