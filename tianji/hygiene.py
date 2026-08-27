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
from pathlib import Path

from . import ops
from .db import now


def needs_cleanup(conn, worker_id: str, new_task_id: int) -> dict | None:
    """换活打扫判定(14.4): 返回 None=无需打扫,否则带上次任务信息。

    "不相关"机械判定: 上次已结算派单的任务 ≠ 本次任务(同任务=续接,不打扫)。
    """
    row = conn.execute(
        "SELECT d.id AS did, d.task_id AS tid, t.status AS tstatus,"
        " t.title AS title, d.payload AS payload, d.task_dir AS task_dir"
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
            "payload": row["payload"], "task_dir": row["task_dir"]}


def _task_dir_of(info: dict) -> str:
    """任务目录: 优先派单表 task_dir 列,旧数据回退 payload 里的备份。"""
    if info.get("task_dir"):
        return info["task_dir"]
    payload = json.loads(info["payload"]) if info.get("payload") else {}
    return payload.get("task_dir", "")


def _transcript_tail(conn, worker_id: str, dispatch_id: int, n: int) -> str:
    """摘要的转录尾部: 复用监控器断点摘要的读尾部手法(整读太贵,只留尾 N 行)。

    先按上次派单的登记行定位会话,找不到再用最新登记行兜底。
    """
    if n <= 0:
        return ""
    reg = conn.execute(
        "SELECT session_id, instance_name FROM instance_registrations"
        " WHERE instance_name=? AND dispatch_id=? ORDER BY id DESC LIMIT 1",
        (worker_id, dispatch_id)).fetchone()
    if reg is None or not reg["session_id"]:
        reg = conn.execute(
            "SELECT session_id, instance_name FROM instance_registrations"
            " WHERE instance_name=? ORDER BY id DESC LIMIT 1",
            (worker_id,)).fetchone()
    if reg is None or not reg["session_id"]:
        return ""
    inst = conn.execute(
        "SELECT shell FROM instances WHERE name=?",
        (reg["instance_name"],)).fetchone()
    shell = inst["shell"] if inst else "claude"
    try:
        from .adapters import transcript_parser
        p = transcript_parser.transcript_path(shell, reg["session_id"])
    except Exception:
        return ""
    if not p:
        return ""
    try:
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def _taskbook_text(task_dir: str) -> str:
    """任务书内容(落盘原文;目录缺失/读不了=空,摘要质量降级无损)。"""
    if not task_dir:
        return ""
    p = Path(task_dir) / "task.md"
    try:
        return p.read_text(encoding="utf-8") if p.is_file() else ""
    except Exception:
        return ""


def _artifact_names(task_dir: str) -> list:
    """产物清单(任务目录下的文件名,目录缺失=空)。"""
    if not task_dir:
        return []
    tdir = Path(task_dir)
    try:
        if not tdir.is_dir():
            return []
        return sorted(p.name for p in tdir.iterdir() if p.is_file())
    except Exception:
        return []


def cleanup(conn, worker_id: str, new_task_id: int,
            reason: str = "换活打扫") -> dict:
    """机械打扫(14.4): 摘要落账本+登记行标"已打扫"(cleaned_at)。

    摘要=转录尾部 N 行 + 任务书摘要 + 产物清单(14.4 口径,票 48 补齐),
    N 存账本配置 cleanup_tail_lines,总控可改。
    """
    info = needs_cleanup(conn, worker_id, new_task_id)
    if info is None:
        return {"cleaned": False, "reason": "无需打扫"}
    tail_n = int(ops._config(conn, "cleanup_tail_lines") or 20)
    work_dir = _task_dir_of(info)
    summary = {
        "last_task_id": info["last_task_id"],
        "last_task_title": info["last_task_title"],
        "last_task_status": info["last_task_status"],
        "last_dispatch_id": info["last_dispatch_id"],
        "report_path": work_dir,
        "transcript_tail": _transcript_tail(conn, worker_id,
                                            info["last_dispatch_id"], tail_n),
        "taskbook": _taskbook_text(work_dir),
        "artifacts": _artifact_names(work_dir),
    }
    # 登记行标"已打扫"(摘要落账本,会话记忆只是缓存)
    conn.execute(
        "UPDATE instance_registrations SET cleaned_at=?"
        " WHERE instance_name=? AND dispatch_id=?",
        (now(), worker_id, info["last_dispatch_id"]))
    ops.audit(conn, "hygiene_clean",
              {"worker_id": worker_id, "reason": reason, "summary": summary})
    return {"cleaned": True, "summary": summary}
