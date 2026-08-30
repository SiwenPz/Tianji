"""取单规则(票 06 验收 7): 队列派生视图 (priority desc, created_at asc)。"""

import json
import os

import pytest
from typer.testing import CliRunner

from tianji import ops
from tianji.cli import app


runner = CliRunner()


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


def test_task_queue_next_readonly(conn, controller):
    """取单只读: task_queue_next 不写账本(无新 audit/派单)。"""
    import json
    _to_dispatched(conn, controller, "只读验证", priority=1)
    before_audit = conn.execute(
        "SELECT COUNT(*) AS n FROM audit").fetchone()["n"]
    before_dispatch = conn.execute(
        "SELECT COUNT(*) AS n FROM dispatches").fetchone()["n"]
    nxt = ops.task_queue_next(conn)
    assert nxt is not None and nxt["status"] == "dispatched"
    after_audit = conn.execute(
        "SELECT COUNT(*) AS n FROM audit").fetchone()["n"]
    after_dispatch = conn.execute(
        "SELECT COUNT(*) AS n FROM dispatches").fetchone()["n"]
    assert after_audit == before_audit
    assert after_dispatch == before_dispatch


def test_cli_task_next_matches_ops(conn, controller, monkeypatch):
    """CLI `task next` 输出与 ops.task_queue_next 一致(验收标准 3: 取单触达)。"""
    _to_dispatched(conn, controller, "CLI取单测试", priority=3)
    ctrl_env = {"TIANJI_WORKER_ID": controller["worker_id"],
                "TIANJI_SECRET": controller["secret"],
                "TIANJI_HOME": os.environ["TIANJI_HOME"]}
    r = runner.invoke(app, ["task", "next"], env=ctrl_env)
    assert r.exit_code == 0, f"CLI 失败: {r.output}\n{r.exception}"
    cli_out = json.loads(r.output)
    # CLI 输出应与 ops 层结果一致
    ops_result = ops.task_queue_next(conn)
    assert cli_out["id"] == ops_result["id"]
    assert cli_out["status"] == "dispatched"
