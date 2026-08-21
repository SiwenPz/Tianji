"""任务九态状态机(验收 2): 非法转换被拒,合法链全通。"""

import pytest

from tianji import ops


def test_illegal_transition_new_to_dispatched(conn, controller):
    """new 直跳 dispatched 被拒(4.2 转换表)。"""
    tid = ops.task_new(conn, controller, "任务", request_id="r-new")["task_id"]
    with pytest.raises(ValueError, match="非法转换"):
        ops.task_transition(conn, controller, tid, "dispatched",
                            request_id="r1")


def test_illegal_transition_archived_to_discussing(conn, controller):
    tid = ops.task_new(conn, controller, "任务", request_id="r-new")["task_id"]
    conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (tid,))
    with pytest.raises(ValueError, match="非法转换"):
        ops.task_transition(conn, controller, tid, "discussing",
                            request_id="r2")


def test_plan_reject_back_to_discussing(conn, controller):
    tid = ops.task_new(conn, controller, "任务", request_id="r-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    ops.task_transition(conn, controller, tid, "discussing", request_id="r-x")
    assert ops.task_get(conn, tid)["status"] == "discussing"


def test_dispatched_to_executing_only_by_event(conn, controller):
    """5.1: 开工证据由事件联动触发,手动转换被拒。"""
    tid = ops.task_new(conn, controller, "任务", request_id="r-new")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s, request_id=f"r-{s}")
    with pytest.raises(ValueError, match="开工证据"):
        ops.task_transition(conn, controller, tid, "executing",
                            request_id="r-x")


def test_reopen_only_from_archived(conn, controller):
    tid = ops.task_new(conn, controller, "任务", request_id="r-new")["task_id"]
    with pytest.raises(ValueError, match="非法转换"):
        ops.task_transition(conn, controller, tid, "reopened",
                            request_id="r1")


def test_force_intervention(conn, controller, worker):
    """强制干预(4.4): 总控特权例外转换+审计;其他身份被拒。"""
    tid = ops.task_new(conn, controller, "任务", request_id="r-new")["task_id"]
    with pytest.raises(PermissionError):
        ops.task_force(conn, worker, tid, "executing", "越权尝试")
    r = ops.task_force(conn, controller, tid, "executing", "极端情况",
                       request_id="r-force")
    assert r["to"] == "executing"
    aud = conn.execute(
        "SELECT action FROM audit WHERE action='force_intervention'").fetchone()
    assert aud is not None
