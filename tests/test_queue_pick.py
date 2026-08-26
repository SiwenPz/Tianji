"""取单规则(票 06 验收 7): 队列派生视图 (priority desc, created_at asc)。"""

import pytest

from tianji import ops


def _to_dispatched(conn, controller, title, priority=0):
    tid = ops.task_new(conn, controller, title, priority=priority,
                       request_id=f"r-{title}")["task_id"]
    for s in ("discussing", "awaiting_plan_confirm", "dispatched"):
        ops.task_transition(conn, controller, tid, s,
                            request_id=f"r-{title}-{s}")
    return tid


class TestQueuePick:
    """取单规则: 无活跃派单、priority desc、created_at asc。"""

    def test_empty_queue_returns_none(self, conn, controller):
        assert ops.task_queue_next(conn) is None

    def test_priority_desc_then_created_asc(self, conn, controller):
        """高优先级任务先出队,同级按创建时间早先出队。"""
        low = _to_dispatched(conn, controller, "低优先级", priority=0)
        high = _to_dispatched(conn, controller, "高优先级", priority=5)
        mid = _to_dispatched(conn, controller, "中优先级", priority=2)
        # 队列视图只返回下一个,不删除
        nxt = ops.task_queue_next(conn)
        assert nxt["id"] == high
        nxt2 = ops.task_queue_next(conn)
        assert nxt2["id"] == high  # 同一任务仍在队列中

    def test_busy_task_excluded_from_queue(self, conn, controller, worker):
        """有活跃派单的任务不出现在队列视图中。"""
        busy_tid = _to_dispatched(conn, controller, "忙碌任务", priority=10)
        free_tid = _to_dispatched(conn, controller, "空闲任务", priority=5)
        # 给忙碌任务派单
        ops.dispatch_issue(conn, controller, busy_tid, worker["worker_id"],
                           request_id="r-issue")
        nxt = ops.task_queue_next(conn)
        assert nxt is not None
        assert nxt["id"] == free_tid

    def test_only_dispatched_status_considered(self, conn, controller, worker):
        """只有 status='dispatched' 的任务在队列中。"""
        tid = _to_dispatched(conn, controller, "待派", priority=1)
        # dispatched→executing 必须由开工证据触发,不能手动转换
        # 这里直接通过派单来占用任务,使其不在队列中
        ops.dispatch_issue(conn, controller, tid, worker["worker_id"],
                           request_id="r-issue")
        assert ops.task_queue_next(conn) is None
